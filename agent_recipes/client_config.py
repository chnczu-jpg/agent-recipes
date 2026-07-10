from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, claim_status, now_iso
from agent_recipes.install import client_like_env, cli_invocation_for, parse_json_lines, resolve_command
from agent_recipes.mcp import MCP_PROTOCOL_VERSION, TOOL_NAMES, unwrap_tool_call_result


CODEX_BEGIN = "# BEGIN agent-recipes managed MCP"
CODEX_END = "# END agent-recipes managed MCP"
HERMES_SERVER_NAME = "agent_recipes"


def install_client_config(project: Path, *, agent: str, config_path: str | Path | None = None) -> dict[str, Any]:
    root = project.resolve()
    command, prefix_args = cli_invocation_for(root)
    args = prefix_args + ["mcp", "--stdio", "--project", str(root)]
    if agent == "codex":
        path = Path(config_path).expanduser() if config_path else Path.home() / ".codex" / "config.toml"
        backup = backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(add_codex_mcp(existing, command=command, args=args, project=root), encoding="utf-8")
    elif agent == "claude":
        path = Path(config_path).expanduser() if config_path else Path.home() / ".claude.json"
        backup = backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(existing_data, dict):
            raise RecipesError("AR761", "Claude MCP 配置根对象必须是 JSON object。", str(path), "修复 Claude MCP 配置 JSON。")
        existing_data.setdefault("mcpServers", {})
        existing_data["mcpServers"]["agent-recipes"] = {
            "command": command,
            "args": args,
            "env": minimal_mcp_env(root),
        }
        path.write_text(json.dumps(existing_data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    elif agent == "hermes":
        path = Path(config_path).expanduser() if config_path else Path.home() / ".hermes" / "config.yaml"
        backup = backup_file(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(add_hermes_mcp(existing, command=command, args=args, project=root), encoding="utf-8")
    else:
        raise RecipesError("AR762", "未知 client agent。", agent, "使用 codex、claude 或 hermes。")
    smoke = smoke_mcp_command(command, args, project=root)
    return {
        "ok": True,
        "action": "install-client",
        "agent": agent,
        "config_path": str(path),
        "backup_path": str(backup) if backup else None,
        "mcp_command": [command, *args],
        "smoke": smoke,
        "claim_status": claim_status(
            verified=[
                f"已写入 {agent} 本机 MCP 配置。",
                "已备份原配置文件。" if backup else "原配置文件不存在，无需备份。",
                "已用写入的 command/args 拉起 MCP stdio server 并跑通 doctor。",
            ],
            cannot_claim=[
                "不能说真实 Codex/Claude/Hermes 客户端已经重新加载该配置。",
                "不能说插件市场分发已经完成。",
                "不能说配置已在另一台机器验证。",
            ],
        ),
    }


def add_codex_mcp(text: str, *, command: str, args: list[str], project: Path) -> str:
    env = minimal_mcp_env(project)
    cleaned = remove_toml_table(remove_managed_block(text), "mcp_servers.agent_recipes").rstrip()
    block = "\n".join(
        [
            CODEX_BEGIN,
            "[mcp_servers.agent_recipes]",
            f"command = {json.dumps(command, ensure_ascii=False)}",
            f"args = {json.dumps(args, ensure_ascii=False)}",
            "startup_timeout_sec = 120",
            "",
            "[mcp_servers.agent_recipes.env]",
            f"PATH = {json.dumps(env['PATH'])}",
            f"PYTHONDONTWRITEBYTECODE = {json.dumps(env['PYTHONDONTWRITEBYTECODE'])}",
            f"AGENT_RECIPES_MCP_DEBUG_LOG = {json.dumps(env['AGENT_RECIPES_MCP_DEBUG_LOG'], ensure_ascii=False)}",
            CODEX_END,
        ]
    )
    return (cleaned + "\n\n" + block + "\n") if cleaned else block + "\n"


def add_hermes_mcp(text: str, *, command: str, args: list[str], project: Path) -> str:
    lines = text.splitlines()
    start = find_top_level_key(lines, "mcp_servers")
    block = hermes_server_block(command=command, args=args, project=project)
    if start is None:
        cleaned = text.rstrip()
        addition = "mcp_servers:\n" + "\n".join(block) + "\n"
        return cleaned + "\n\n" + addition if cleaned else addition

    suffix = lines[start].split(":", 1)[1].strip()
    if suffix and not suffix.startswith("#"):
        raise RecipesError(
            "AR768",
            "Hermes mcp_servers 必须是顶层 map。",
            lines[start],
            "把 mcp_servers 改成多行 map 后再运行 install-client。",
        )

    end = find_next_top_level_key(lines, start + 1)
    section = lines[start + 1 : end]
    if any(line.startswith("  -") for line in section):
        raise RecipesError(
            "AR769",
            "Hermes mcp_servers 当前像 list，不是 map。",
            "mcp_servers 下发现 '- ' 条目。",
            "改成 mcp_servers.<name> map 后再运行 install-client。",
        )

    updated_section = remove_yaml_child_block(section, HERMES_SERVER_NAME)
    updated = lines[: start + 1] + updated_section + block + lines[end:]
    return "\n".join(updated).rstrip() + "\n"


def hermes_server_block(*, command: str, args: list[str], project: Path) -> list[str]:
    env = minimal_mcp_env(project)
    return [
        f"  {HERMES_SERVER_NAME}:",
        f"    command: {json.dumps(command, ensure_ascii=False)}",
        f"    args: {json.dumps(args, ensure_ascii=False)}",
        "    env:",
        f"      PATH: {json.dumps(env['PATH'])}",
        f"      PYTHONDONTWRITEBYTECODE: {json.dumps(env['PYTHONDONTWRITEBYTECODE'])}",
        f"      AGENT_RECIPES_MCP_DEBUG_LOG: {json.dumps(env['AGENT_RECIPES_MCP_DEBUG_LOG'], ensure_ascii=False)}",
        "    timeout: 120",
        "    connect_timeout: 60",
    ]


def find_top_level_key(lines: list[str], key: str) -> int | None:
    prefix = f"{key}:"
    for index, line in enumerate(lines):
        if line.startswith(prefix):
            return index
    return None


def find_next_top_level_key(lines: list[str], start: int) -> int:
    for index in range(start, len(lines)):
        line = lines[index]
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] not in (" ", "\t") and ":" in line:
            return index
    return len(lines)


def remove_yaml_child_block(lines: list[str], child_key: str) -> list[str]:
    output: list[str] = []
    index = 0
    child_prefix = f"  {child_key}:"
    while index < len(lines):
        line = lines[index]
        if line.startswith(child_prefix):
            index += 1
            while index < len(lines):
                candidate = lines[index]
                if candidate.startswith("  ") and not candidate.startswith("    ") and candidate.strip():
                    break
                index += 1
            continue
        output.append(line)
        index += 1
    while output and not output[-1].strip():
        output.pop()
    return output


def remove_managed_block(text: str) -> str:
    if CODEX_BEGIN not in text or CODEX_END not in text:
        return text
    before, rest = text.split(CODEX_BEGIN, 1)
    _, after = rest.split(CODEX_END, 1)
    return before.rstrip() + "\n" + after.lstrip()


def remove_toml_table(text: str, table: str) -> str:
    lines = text.splitlines()
    output: list[str] = []
    skipping = False
    for line in lines:
        stripped = line.strip()
        is_header = stripped.startswith("[") and stripped.endswith("]")
        header_name = stripped[1:-1] if is_header else ""
        is_target = header_name == table or header_name.startswith(f"{table}.")
        if is_target:
            skipping = True
            continue
        if skipping and is_header:
            skipping = False
        if not skipping:
            output.append(line)
    return "\n".join(output) + ("\n" if text.endswith("\n") and output else "")


def minimal_mcp_env(project: Path | None = None) -> dict[str, str]:
    debug_log = ""
    if project is not None:
        debug_log = str(project / ".recipes" / "reports" / "mcp_stdio_debug.jsonl")
    return {
        "PATH": client_like_env()["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "AGENT_RECIPES_MCP_DEBUG_LOG": debug_log,
    }


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = now_iso().replace(":", "").replace("-", "").replace("Z", "Z")
    backup = path.with_name(f"{path.name}.before-agent-recipes-{timestamp}")
    shutil.copy2(path, backup)
    return backup


def smoke_mcp_command(command: str, args: list[str], *, project: Path) -> dict[str, Any]:
    executable = resolve_command(command)
    if executable is None:
        raise RecipesError("AR763", "client MCP command 不可执行。", command, "检查配置中的 command。")
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-recipes-smoke", "version": "0.1.0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "agent_recipes_doctor", "arguments": {"project": str(project)}},
        },
    ]
    proc = subprocess.run(
        [executable, *args],
        input="".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests),
        text=True,
        capture_output=True,
        cwd=project,
        timeout=15,
        check=False,
        env={**client_like_env(), **minimal_mcp_env(project)},
    )
    if proc.returncode != 0:
        raise RecipesError("AR764", "client MCP smoke 失败。", proc.stderr[-1000:] or proc.stdout[-1000:], "检查 command/args。")
    rows = parse_json_lines(proc.stdout, code="AR765")
    by_id = {row.get("id"): row for row in rows}
    tools = [
        item.get("name")
        for item in ((by_id.get(2, {}).get("result") or {}).get("tools") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    missing_tools = [f"agent_recipes_{name}" for name in TOOL_NAMES if f"agent_recipes_{name}" not in tools]
    if missing_tools:
        raise RecipesError("AR766", "client MCP tools/list 缺少工具。", ", ".join(missing_tools), "检查 MCP server。")
    doctor = unwrap_tool_call_result(by_id.get(3, {}).get("result") or {})
    if doctor.get("status") != "ok":
        raise RecipesError("AR767", "client MCP doctor 没有返回 ok。", json.dumps(doctor, ensure_ascii=False), "先修复 doctor。")
    return {"doctor_status": doctor.get("status"), "tools_count": len(tools), "tools": tools, "launch_env_path": client_like_env()["PATH"]}
