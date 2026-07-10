from __future__ import annotations

import shutil
import subprocess
import json
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, claim_status, now_iso, sha256_text, write_json


DIRECT_REQUIREMENTS = "requirements-phase2-adapters.txt"
LOCKFILE = "requirements-phase2-adapters.lock.txt"
OUT_OF_SCOPE_PACKAGES = {"zep-cloud"}
SYSTEM_BINARY_TARGETS = [
    {"name": "python3", "args": ["--version"], "required": True},
    {"name": "sqlite3", "args": ["--version"], "required": True},
    {"name": "ffmpeg", "args": ["-version"], "required": False},
    {"name": "ffprobe", "args": ["-version"], "required": False},
    {"name": "uv", "args": ["--version"], "required": False},
    {"name": "llama-server", "args": ["--version"], "required": False},
]


def adapter_lock(project: Path) -> dict[str, Any]:
    root = project.resolve()
    venv_python = root / ".venv" / "bin" / "python"
    if not venv_python.exists():
        raise RecipesError(
            "AR750",
            "找不到项目 .venv。",
            f"missing={venv_python}",
            "先创建项目本地 .venv 并安装 adapter 依赖。",
        )
    uv = shutil.which("uv")
    if uv is None:
        raise RecipesError("AR751", "找不到 uv。", "uv is not on PATH", "安装或提供 uv 后再生成 adapter lock。")
    proc = subprocess.run(
        [uv, "pip", "freeze", "--python", str(venv_python)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RecipesError("AR752", "adapter lock 导出失败。", proc.stderr[-1000:] or proc.stdout[-1000:], "检查 .venv。")
    frozen_packages = sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())
    packages = [
        line
        for line in frozen_packages
        if normalize_requirement_name(line.split("==", 1)[0]) not in OUT_OF_SCOPE_PACKAGES
    ]
    excluded_out_of_scope = [
        line
        for line in frozen_packages
        if normalize_requirement_name(line.split("==", 1)[0]) in OUT_OF_SCOPE_PACKAGES
    ]
    if not packages:
        raise RecipesError("AR753", "adapter lock 为空。", "uv pip freeze returned no packages", "检查 .venv。")
    lock_text = lockfile_text(packages)
    lock_path = root / LOCKFILE
    lock_path.write_text(lock_text, encoding="utf-8")
    direct_requirements = read_direct_requirements(root / DIRECT_REQUIREMENTS)
    installed_names = {normalize_requirement_name(line.split("==", 1)[0]) for line in packages if "==" in line}
    missing_direct = [name for name in direct_requirements if normalize_requirement_name(name) not in installed_names]
    lock_hash = sha256_text(lock_text)
    report = {
        "action": "adapter-lock",
        "generated_at": now_iso(),
        "venv_python": str(venv_python),
        "lock_path": str(lock_path),
        "lock_hash": lock_hash,
        "package_count": len(packages),
        "excluded_out_of_scope_packages": excluded_out_of_scope,
        "direct_requirements": direct_requirements,
        "missing_direct_requirements": missing_direct,
        "claim_status": claim_status(
            verified=[
                "已从项目本地 .venv 导出固定版本 package 列表。",
                "已检查 direct adapter requirements 是否出现在 lockfile。",
            ],
            missing_evidence=[] if not missing_direct else ["有 direct adapter requirement 未出现在 lockfile。"],
            cannot_claim=[
                "不能说这些版本已在另一台机器复现安装过。",
                "不能说 Homebrew 或系统二进制依赖已经被 lockfile 固化。",
                "不能说 adapter 输出质量已通过真实素材压测。",
            ],
        ),
    }
    write_json(root / ".recipes" / "reports" / "adapter_runtime_lock.json", report)
    return {
        "ok": not missing_direct,
        "action": "adapter-lock",
        "lock_path": str(lock_path),
        "report_path": str(root / ".recipes" / "reports" / "adapter_runtime_lock.json"),
        "lock_hash": lock_hash,
        "package_count": len(packages),
        "excluded_out_of_scope_packages": excluded_out_of_scope,
        "direct_requirements": direct_requirements,
        "missing_direct_requirements": missing_direct,
        "claim_status": report["claim_status"],
    }


def system_lock(project: Path) -> dict[str, Any]:
    root = project.resolve()
    binaries = [inspect_binary(item["name"], item["args"], bool(item["required"])) for item in SYSTEM_BINARY_TARGETS]
    missing_required = [item["name"] for item in binaries if item["required"] and not item["present"]]
    missing_optional = [item["name"] for item in binaries if not item["required"] and not item["present"]]
    lock_payload = {
        "binaries": binaries,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
    }
    lock_hash = sha256_text(json.dumps(lock_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    report = {
        "action": "system-lock",
        "generated_at": now_iso(),
        "lock_hash": lock_hash,
        **lock_payload,
        "claim_status": claim_status(
            verified=["已记录本机系统二进制 path/version 可复查证据。"],
            missing_evidence=[] if not missing_required else [f"required system binary missing: {name}" for name in missing_required],
            cannot_claim=[
                "不能说这些系统二进制已在另一台机器复现。",
                "不能说 Homebrew formula 已被完整 pin 住。",
                "不能说 OCR/ASR/scene cut 输出质量已通过真实素材压测。",
            ],
        ),
    }
    report_path = root / ".recipes" / "reports" / "system_runtime_lock.json"
    write_json(report_path, report)
    return {
        "ok": not missing_required,
        "action": "system-lock",
        "report_path": str(report_path),
        "lock_hash": lock_hash,
        "binaries": binaries,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "claim_status": report["claim_status"],
    }


def inspect_binary(name: str, args: list[str], required: bool) -> dict[str, Any]:
    path = shutil.which(name)
    if path is None:
        return {
            "name": name,
            "required": required,
            "present": False,
            "path": None,
            "version_line": None,
            "returncode": None,
        }
    proc = subprocess.run([path, *args], text=True, capture_output=True, check=False, timeout=10)
    output = (proc.stdout + "\n" + proc.stderr).strip()
    version_line = first_nonempty_line(output)
    return {
        "name": name,
        "required": required,
        "present": True,
        "path": path,
        "version_line": version_line,
        "returncode": proc.returncode,
    }


def first_nonempty_line(text: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:300]
    return None


def lockfile_text(packages: list[str]) -> str:
    return (
        "# Generated by agent-recipes adapter-lock.\n"
        "# Source: project-local .venv via uv pip freeze.\n"
        "# This locks Python packages only; system binaries are reported separately.\n"
        + "\n".join(packages)
        + "\n"
    )


def read_direct_requirements(path: Path) -> list[str]:
    if not path.exists():
        return []
    requirements: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        requirements.append(stripped)
    return requirements


def normalize_requirement_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")
