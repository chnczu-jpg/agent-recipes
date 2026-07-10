from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, RecipesProject, claim_status, command_result, now_iso, sha256_json
from agent_recipes.mcp import MCP_PROTOCOL_VERSION, TOOL_NAMES, unwrap_tool_call_result


def install_paths(project: Path, *, agent: str, scope: str) -> dict[str, Path]:
    project = project.resolve()
    if scope == "project":
        skill_path = project / ".agents" / "skills" / "agent-recipes" / "SKILL.md"
        source_indexer_path = project / ".agents" / "skills" / "source-to-recipe-indexer" / "SKILL.md"
        mcp_path = project / ".agents" / "mcp" / "agent-recipes.json"
    else:
        home = Path.home()
        skill_path = home / f".{agent}" / "skills" / "agent-recipes" / "SKILL.md"
        source_indexer_path = home / f".{agent}" / "skills" / "source-to-recipe-indexer" / "SKILL.md"
        mcp_path = home / f".{agent}" / "mcp" / "agent-recipes.json"
    return {"skill": skill_path, "source_indexer": source_indexer_path, "mcp": mcp_path}


def install_skill_dry_run(project: Path, *, agent: str, scope: str) -> dict[str, Any]:
    project = project.resolve()
    paths = install_paths(project, agent=agent, scope=scope)
    cli_command, cli_prefix_args = cli_invocation_for(project)
    return {
        "ok": True,
        "action": "install-skill",
        "dry_run": True,
        "agent": agent,
        "scope": scope,
        "files_would_write": [
            str(paths["skill"]),
            str(paths["source_indexer"]),
            str(paths["mcp"]),
        ],
        "registrations_would_add": [
            {
                "type": "mcp_server",
                "name": "agent-recipes",
                "command": cli_command,
                "args": cli_prefix_args + ["mcp", "--stdio"],
            },
            {
                "type": "skill",
                "name": "agent-recipes",
                "behavior": "Use MCP first; fall back to CLI. Run doctor before relying on recipes.",
            },
        ],
        "rollback": [
            f"Remove {paths['skill']}",
            f"Remove {paths['source_indexer']}",
            f"Remove {paths['mcp']}",
        ],
        "claim_status": claim_status(
            verified=["dry-run 已计算将写入的 skill 和 MCP 注册路径。"],
            missing_evidence=["未实际写入安装文件。"],
            cannot_claim=[
                "不能说 skill 已安装。",
                "不能说 MCP server 已注册。",
                "不能说 Codex/Claude/Hermes 已能调用本工具。",
            ],
        ),
    }


def install_skill(project: Path, *, agent: str, scope: str, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return install_skill_dry_run(project, agent=agent, scope=scope)
    project = project.resolve()
    paths = install_paths(project, agent=agent, scope=scope)
    cli_command, cli_prefix_args = cli_invocation_for(project)
    mcp_config = {
        "mcpServers": {
            "agent-recipes": {
                "command": cli_command,
                "args": cli_prefix_args + ["mcp", "--stdio", "--project", str(project)],
            }
        },
        "installed_at": now_iso(),
        "scope": scope,
        "claim_limits": [
            "This registers a local MCP command only.",
            "It does not prove a real Codex/Claude/Hermes client has loaded the config.",
        ],
    }
    planned = {
        str(paths["skill"]): agent_recipes_skill(agent=agent, project=project),
        str(paths["source_indexer"]): source_to_recipe_indexer_skill(agent=agent, project=project),
    }
    files_written: list[str] = []
    for raw_path, content in planned.items():
        path = Path(raw_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            path.write_text(content, encoding="utf-8")
            files_written.append(str(path))
    paths["mcp"].parent.mkdir(parents=True, exist_ok=True)
    existing_mcp = paths["mcp"].read_text(encoding="utf-8") if paths["mcp"].exists() else None
    mcp_text = json_text(mcp_config)
    if existing_mcp != mcp_text:
        paths["mcp"].write_text(mcp_text, encoding="utf-8")
        files_written.append(str(paths["mcp"]))

    recipes = RecipesProject(project)
    if not recipes.recipes_dir.exists():
        recipes.init()
    payload = {
        "agent": agent,
        "scope": scope,
        "skill_path": str(paths["skill"]),
        "source_indexer_path": str(paths["source_indexer"]),
        "mcp_path": str(paths["mcp"]),
        "mcp_command": [cli_command, *cli_prefix_args, "mcp", "--stdio", "--project", str(project)],
    }
    event, idem = recipes.append_event(
        "skill_installed",
        payload,
        idempotency_key=f"install-skill:{agent}:{scope}:{project}:{sha256_json(payload)}",
        lock_exempt_reason="integration_install",
        claim_status=claim_status(
            verified=["已写入 project-local skill 和 MCP 配置文件。"],
            cannot_claim=[
                "不能说真实 Codex/Claude/Hermes 客户端已经加载该配置。",
                "不能说插件市场分发已经完成。",
            ],
        ),
    )
    return command_result(
        "install-skill",
        idem,
        files_written=files_written + [str(recipes.events_path)] if idem == "created" else files_written,
        objects_created=[event["event_id"]] if idem == "created" else [],
        claim_status=event["claim_status"],
        extra={
            "agent": agent,
            "scope": scope,
            "skill_path": str(paths["skill"]),
            "source_indexer_path": str(paths["source_indexer"]),
            "mcp_path": str(paths["mcp"]),
        },
    )


def client_smoke(project: Path, *, agent: str, scope: str) -> dict[str, Any]:
    project = project.resolve()
    paths = install_paths(project, agent=agent, scope=scope)
    mcp_path = paths["mcp"]
    if not mcp_path.exists():
        raise RecipesError(
            "AR730",
            "找不到 project-local MCP 配置。",
            f"missing={mcp_path}",
            "先运行 install-skill --agent <agent> --scope project。",
        )
    try:
        config = json.loads(mcp_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RecipesError("AR731", "MCP 配置不是合法 JSON。", str(exc), "重新运行 install-skill。") from exc
    server = (config.get("mcpServers") or {}).get("agent-recipes")
    if not isinstance(server, dict):
        raise RecipesError("AR732", "MCP 配置缺少 agent-recipes server。", str(mcp_path), "重新运行 install-skill。")
    command = server.get("command")
    args = server.get("args") or []
    if not isinstance(command, str) or not command:
        raise RecipesError("AR733", "MCP command 缺失。", str(server), "重新运行 install-skill。")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise RecipesError("AR734", "MCP args 必须是字符串数组。", str(server), "重新运行 install-skill。")
    executable = resolve_command(command)
    if executable is None:
        raise RecipesError("AR735", "MCP command 不可执行。", command, "检查 CLI 路径或重新运行 install-skill。")

    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-recipes-smoke", "version": "0.2.0"},
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
        env=client_like_env(),
    )
    if proc.returncode != 0:
        raise RecipesError(
            "AR736",
            "MCP smoke 启动失败。",
            proc.stderr[-1000:] or proc.stdout[-1000:],
            "先运行 doctor，再检查 MCP command。",
        )
    responses = parse_json_lines(proc.stdout, code="AR737")
    by_id = {response.get("id"): response for response in responses}
    tools_response = by_id.get(2, {})
    doctor_response = by_id.get(3, {})
    if "error" in tools_response or "error" in doctor_response:
        raise RecipesError("AR738", "MCP smoke 返回错误。", proc.stdout[-1000:], "查看 MCP server 输出。")
    tools = [
        item.get("name")
        for item in ((tools_response.get("result") or {}).get("tools") or [])
        if isinstance(item, dict) and item.get("name")
    ]
    missing_tools = [f"agent_recipes_{name}" for name in TOOL_NAMES if f"agent_recipes_{name}" not in tools]
    if missing_tools:
        raise RecipesError("AR739", "MCP tools/list 缺少工具。", ", ".join(missing_tools), "检查 MCP tool_list。")
    doctor_result = unwrap_tool_call_result(doctor_response.get("result") or {})
    doctor_status = doctor_result.get("status")
    if doctor_status != "ok":
        raise RecipesError("AR740", "MCP doctor 没有返回 ok。", json.dumps(doctor_result, ensure_ascii=False), "先修复 doctor。")
    return {
        "ok": True,
        "action": "client-smoke",
        "agent": agent,
        "scope": scope,
        "mcp_path": str(mcp_path),
        "command_checked": [executable, *args],
        "tools": tools,
        "tools_count": len(tools),
        "doctor_status": doctor_status,
        "launch_env_path": client_like_env()["PATH"],
        "claim_status": claim_status(
            verified=[
                "已读取安装生成的 MCP 配置。",
                "已通过该配置拉起本地 MCP stdio server。",
                "已跑通 tools/list 和 agent_recipes_doctor。",
            ],
            cannot_claim=[
                "不能说真实 Codex/Claude/Hermes 客户端已经加载该配置。",
                "不能说插件市场分发已经完成。",
                "不能说外部 adapter 质量已通过真实素材压测。",
            ],
        ),
    }


def json_text(data: dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def cli_invocation_for(project: Path) -> tuple[str, list[str]]:
    python = system_python()
    cli_path = project / "bin" / "agent-recipes"
    if cli_path.exists():
        return python, [str(cli_path)]
    package_cli = Path(__file__).resolve().parents[1] / "bin" / "agent-recipes"
    if package_cli.exists():
        return python, [str(package_cli)]
    return sys.executable, ["-m", "agent_recipes.cli"]


def system_python() -> str:
    candidate = Path("/usr/bin/python3")
    if candidate.exists() and candidate.is_file():
        return str(candidate)
    return sys.executable


def resolve_command(command: str) -> str | None:
    path = Path(command)
    if path.is_absolute() or "/" in command:
        return str(path) if path.exists() and path.is_file() else None
    return shutil.which(command)


def client_like_env() -> dict[str, str]:
    env = {
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for key in ("TMPDIR", "LANG", "LC_ALL"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    return env


def parse_json_lines(text: str, *, code: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RecipesError(code, "MCP 输出不是 JSONL。", line[:500], "检查 MCP server stdout。") from exc
        if isinstance(value, dict):
            rows.append(value)
    return rows


def agent_recipes_skill(*, agent: str, project: Path) -> str:
    return f"""# Agent Recipes

Use this skill when the current project contains `.recipes/` or the user asks to use Agent Recipes.

Project root for this install: `{project}`
Agent target: `{agent}`

Rules:
- Start with `agent_recipes_readiness` through MCP. If it reports degraded outcomes, inspect `agent_recipes_outcome_status`; if blocked, run `agent_recipes_doctor`. If MCP is unavailable, use the matching CLI commands.
- Before relying on a recipe, run lookup, then lock the selected recipe.
- Capture correction, failure, success, and genuinely unknown outcomes after material results. Outcome captures must keep the exact lock binding.
- Treat an outcome hold as a stop for new execution locks; do not bypass it or rewrite the formal recipe directly.
- Treat source_refinery chunks, cards, patch drafts, OCR, ASR, scene cuts, and converted documents as candidate evidence only.
- Treat Cognee memory results as candidate evidence only; they do not bypass review_queue.
- Run `agent_recipes_recall_boundary` before relying on Cognee, Graphiti, or Qwen recall. A broken recall path must be disabled while the core recipe chain continues.
- If readiness reports evidence-hardening issues, run `agent_recipes_evidence_quarantine` status/apply. Quarantined evidence is unusable until an explicit repaired release.
- Use `agent_recipes_evidence_pack` after lock when execution context must be bounded; omitted evidence cannot support the decision.
- Do not write or modify a formal recipe directly; use review_queue.
- Do not claim client integration is live unless the real client has loaded this MCP config.

Fallback CLI:

```bash
./bin/agent-recipes readiness --project {project} --json
./bin/agent-recipes outcome-status --project {project} --json
./bin/agent-recipes recall-boundary --project {project} --json
./bin/agent-recipes evidence-quarantine --action status --project {project} --json
./bin/agent-recipes doctor --project {project} --json
./bin/agent-recipes lookup \"<task>\" --project {project} --json
./bin/agent-recipes lock --recipe <recipe_id> --task \"<task>\" --project {project} --json
./bin/agent-recipes evidence-pack --lock <lock_id> --max-bytes 65536 --privacy project_local --project {project} --json
./bin/agent-recipes capture --type failure --text \"<what happened>\" --lock <lock_id> --project {project} --json
```
"""


def source_to_recipe_indexer_skill(*, agent: str, project: Path) -> str:
    return f"""# Source To Recipe Indexer

Use this skill when the user asks to turn documents, transcripts, videos, screenshots, or lesson notes into Agent Recipes candidates.

Project root for this install: `{project}`
Agent target: `{agent}`

Workflow:
1. Run readiness, doctor, and capabilities.
2. Register or convert local sources with `sources add`, `convert-doc`, `transcribe`, `ingest-video`, `detect-scenes`, or `ocr-image`.
3. Run `scan` or `search` to create candidate chunks.
4. Run `refine` with a concrete `knowledge_need_id`, `target_recipe_id`, and allowed recipe fields.
5. Run `extract-cards`.
6. Run `patch-draft`.
7. Optionally run `cloud-configure/status/refine` to use DeepSeek as a candidate-only text brain.
8. Optionally run `embedding-configure/status/index/search` to use Qwen3-Embedding as a local candidate recall index.
9. Run `evidence-quarantine --action status`; isolate malformed candidate evidence before review.
10. Optionally run `recall-boundary`, then `memory-index`, `memory-search`, and `memory-status` to reuse candidate evidence.
11. Stop at review_queue unless the user has explicitly authorized accepting that review item.

Hard limits:
- Do not write formal recipes directly.
- Do not treat OCR/ASR/scene cuts/document conversion as verified truth.
- Do not store cloud API keys in project files.
- Do not treat DeepSeek cloud output as verified truth.
- Do not treat embedding search output as verified truth.
- Do not treat Cognee memory candidates as verified truth.
- Do not turn long summaries into recipes; unmappable chunks are `archive_index_only`.
- Preserve `source_trace`, `target_fields`, `evidence_strength`, and `cannot_claim` on every card.

Fallback CLI examples:

```bash
./bin/agent-recipes capabilities --project {project} --json
./bin/agent-recipes search \"<query>\" --project {project} --json
./bin/agent-recipes refine --query \"<query>\" --knowledge-need <id> --target-recipe <recipe_id> --candidate-fields verified_path,forbidden_path,cannot_claim --project {project} --json
./bin/agent-recipes extract-cards --project {project} --json
./bin/agent-recipes patch-draft --target-recipe <recipe_id> --project {project} --json
./bin/agent-recipes cloud-configure --provider deepseek --api-key-env AGENT_RECIPES_DEEPSEEK_API_KEY --project {project} --json
./bin/agent-recipes cloud-status --provider deepseek --project {project} --json
./bin/agent-recipes cloud-refine --provider deepseek --input <text.md> --knowledge-need <id> --target-recipe <recipe_id> --candidate-fields verified_path,forbidden_path,cannot_claim --response-json <replay.json> --project {project} --json
./bin/agent-recipes embedding-configure --provider qwen3 --model qwen3-embedding:0.6b --endpoint http://127.0.0.1:11434/api/embed --dimensions 1024 --project {project} --json
./bin/agent-recipes embedding-index --provider qwen3 --response-json <embedding-replay.json> --project {project} --json
./bin/agent-recipes embedding-search \"<query>\" --provider qwen3 --response-json <query-embedding.json> --project {project} --json
./bin/agent-recipes memory-index --adapter cognee --project {project} --json
./bin/agent-recipes recall-boundary --project {project} --json
./bin/agent-recipes evidence-quarantine --action status --project {project} --json
./bin/agent-recipes memory-search \"<query>\" --adapter cognee --project {project} --json
./bin/agent-recipes memory-status --adapter cognee --project {project} --json
./bin/agent-recipes memory-native-probe --adapter cognee --project {project} --json
./bin/agent-recipes memory-semantic-configure --adapter cognee --detect-only --project {project} --json
./bin/agent-recipes memory-semantic-probe --adapter cognee --project {project} --json
./bin/agent-recipes memory-index --adapter graphiti --project {project} --json
./bin/agent-recipes memory-search \"<query>\" --adapter graphiti --project {project} --json
./bin/agent-recipes memory-status --adapter graphiti --project {project} --json
./bin/agent-recipes memory-native-probe --adapter graphiti --project {project} --json
```
"""
