from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_recipes.persistence import RecipesError, now_iso, read_json, write_json


PROJECT_SCHEMA_VERSION = "1.0"
SCHEMA_MARKER_NAME = "project_schema.json"


def schema_marker_path(recipes_dir: Path) -> Path:
    return recipes_dir / SCHEMA_MARKER_NAME


def project_schema_status(recipes_dir: Path) -> dict[str, Any]:
    marker = schema_marker_path(recipes_dir)
    if not recipes_dir.exists():
        return schema_status("uninitialized", marker, current=False, installed_version=None)
    if not marker.exists():
        return schema_status("legacy_unversioned", marker, current=False, installed_version=None)
    try:
        payload = read_json(marker, {})
    except (OSError, json.JSONDecodeError) as exc:
        result = schema_status("malformed", marker, current=False, installed_version=None)
        result["problem"] = str(exc)
        return result
    if not isinstance(payload, dict) or not isinstance(payload.get("schema_version"), str):
        result = schema_status("malformed", marker, current=False, installed_version=None)
        result["problem"] = "schema_version missing or not a string"
        return result

    installed = payload["schema_version"]
    if installed == PROJECT_SCHEMA_VERSION:
        state = "current"
    elif version_key(installed) < version_key(PROJECT_SCHEMA_VERSION):
        state = "migration_required"
    else:
        state = "unsupported_newer"
    result = schema_status(state, marker, current=state == "current", installed_version=installed)
    result["marker"] = payload
    return result


def initialize_schema_marker(recipes_dir: Path) -> tuple[Path, bool]:
    marker = schema_marker_path(recipes_dir)
    if marker.exists():
        return marker, False
    write_json(
        marker,
        {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "created_at": now_iso(),
            "installed_by": "init",
            "migration_history": [],
        },
    )
    return marker, True


def write_migrated_schema_marker(
    recipes_dir: Path,
    *,
    previous_version: str | None,
    event_log_hash_before: str,
) -> Path:
    marker = schema_marker_path(recipes_dir)
    write_json(
        marker,
        {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "created_at": now_iso(),
            "installed_by": "migrate",
            "migration_history": [
                {
                    "from": previous_version or "legacy_unversioned",
                    "to": PROJECT_SCHEMA_VERSION,
                    "event_log_hash_before": event_log_hash_before,
                    "migrated_at": now_iso(),
                    "event_log_rewritten": False,
                }
            ],
        },
    )
    return marker


def validate_migration_target(status: dict[str, Any], target: str) -> None:
    if target != PROJECT_SCHEMA_VERSION:
        raise RecipesError(
            "AR470",
            "不支持这个项目 schema 目标版本。",
            f"target={target}; supported={PROJECT_SCHEMA_VERSION}",
            f"使用 migrate --target {PROJECT_SCHEMA_VERSION}。",
        )
    state = status["state"]
    if state == "uninitialized":
        raise RecipesError("AR471", "项目尚未初始化。", ".recipes 不存在。", "先运行 agent-recipes init。")
    if state == "malformed":
        raise RecipesError(
            "AR472",
            "项目 schema 标记损坏，拒绝自动迁移。",
            str(status.get("problem") or status["marker_path"]),
            "人工修复或恢复 project_schema.json 后重试。",
        )
    if state == "unsupported_newer":
        raise RecipesError(
            "AR473",
            "项目来自更新版本，当前程序拒绝降级。",
            f"installed={status['installed_version']}; supported={PROJECT_SCHEMA_VERSION}",
            "使用支持该 schema 的更新版 Agent Recipes。",
        )


def schema_status(
    state: str,
    marker: Path,
    *,
    current: bool,
    installed_version: str | None,
) -> dict[str, Any]:
    return {
        "state": state,
        "current": current,
        "installed_version": installed_version,
        "supported_version": PROJECT_SCHEMA_VERSION,
        "migration_required": state in {"legacy_unversioned", "migration_required"},
        "marker_path": str(marker),
        "event_log_rewrite_required": False,
        "recommended_command": (
            None
            if current or state == "uninitialized"
            else f"agent-recipes migrate --target {PROJECT_SCHEMA_VERSION} --project . --json"
        ),
    }


def version_key(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (10**9,)
