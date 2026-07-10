from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from agent_recipes.persistence import RecipesError, sha256_json


RECIPE_TOMBSTONE_REASONS = {
    "correction",
    "supersession",
    "retraction",
    "contradiction_resolution",
}
RECIPE_OPERATIONAL_CONTENT_FIELDS = (
    "title",
    "scope",
    "use_when",
    "do_not_use_when",
    "inputs_required",
    "outputs_expected",
    "steps",
    "checklist_item",
    "verified_path",
    "forbidden_path",
    "failure_signal",
    "failure_signals",
    "stop_line",
    "verification",
    "success_means",
    "failure_means",
    "cannot_claim",
    "rollback",
)
VERSIONED_RECIPE_ID_RE = re.compile(r"^(?P<base>.+?)[_-]v(?P<version>\d+)$", flags=re.IGNORECASE)


def recipe_hash(recipe: dict[str, Any]) -> str:
    content = {key: value for key, value in recipe.items() if key != "recipe_hash"}
    return sha256_json(content)


def recipe_content_hash(recipe: dict[str, Any]) -> str:
    """Hash executable recipe meaning, excluding identity and provenance metadata."""
    content = {
        key: recipe.get(key)
        for key in RECIPE_OPERATIONAL_CONTENT_FIELDS
        if key in recipe
    }
    return sha256_json(content)


def recipe_lifecycle_state(events: list[dict[str, Any]]) -> dict[str, Any]:
    tombstones_by_id: dict[str, dict[str, Any]] = {}
    revoked_ids: set[str] = set()
    errors: list[str] = []
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload") or {}
        if event_type == "recipe_tombstoned":
            tombstone_id = str(payload.get("tombstone_id") or "")
            recipe_id = str(payload.get("recipe_id") or "")
            recipe_hash_value = str(payload.get("recipe_hash") or "")
            content_hash = str(payload.get("content_hash") or "")
            reason_kind = str(payload.get("reason_kind") or "")
            if not tombstone_id or not recipe_id or not recipe_hash_value or not content_hash:
                errors.append(f"recipe_tombstoned payload 不完整：{event.get('event_id')}")
                continue
            if reason_kind not in RECIPE_TOMBSTONE_REASONS:
                errors.append(f"recipe_tombstoned reason_kind 无效：{event.get('event_id')}")
                continue
            existing = tombstones_by_id.get(tombstone_id)
            if existing and existing.get("event_id") != event.get("event_id"):
                errors.append(f"tombstone_id 重复：{tombstone_id}")
                continue
            tombstones_by_id[tombstone_id] = {
                **payload,
                "event_id": event.get("event_id"),
                "created_at": event.get("created_at"),
            }
        elif event_type == "recipe_tombstone_revoked":
            tombstone_id = str(payload.get("tombstone_id") or "")
            if not tombstone_id:
                errors.append(f"recipe_tombstone_revoked 缺 tombstone_id：{event.get('event_id')}")
                continue
            if tombstone_id not in tombstones_by_id:
                errors.append(f"recipe_tombstone_revoked 引用未知 tombstone：{tombstone_id}")
                continue
            revoked_ids.add(tombstone_id)

    tombstones: list[dict[str, Any]] = []
    retired_recipe_ids: set[str] = set()
    blocked_content_hashes: dict[str, str] = {}
    for tombstone_id, raw in tombstones_by_id.items():
        item = dict(raw)
        item["revoked"] = tombstone_id in revoked_ids
        tombstones.append(item)
        retired_recipe_ids.add(str(item["recipe_id"]))
        if not item["revoked"]:
            blocked_content_hashes[str(item["content_hash"])] = tombstone_id
    tombstones.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("tombstone_id") or "")))
    return {
        "tombstones": tombstones,
        "retired_recipe_ids": sorted(retired_recipe_ids),
        "blocked_content_hashes": blocked_content_hashes,
        "revoked_tombstone_ids": sorted(revoked_ids),
        "errors": errors,
    }


def recipe_lifecycle_summary(state: dict[str, Any]) -> dict[str, Any]:
    tombstones = state.get("tombstones", [])
    return {
        "tombstone_count": len(tombstones),
        "active_tombstone_count": sum(1 for item in tombstones if not item.get("revoked")),
        "revoked_tombstone_count": sum(1 for item in tombstones if item.get("revoked")),
        "retired_recipe_count": len(state.get("retired_recipe_ids", [])),
        "blocked_content_count": len(state.get("blocked_content_hashes", {})),
        "errors": list(state.get("errors", [])),
        "old_recipe_ids_reactivate": False,
    }


def assert_recipe_promotable(events: list[dict[str, Any]], recipe: dict[str, Any]) -> None:
    state = recipe_lifecycle_state(events)
    recipe_id = str(recipe.get("recipe_id") or "")
    if recipe_id in state["retired_recipe_ids"]:
        raise RecipesError(
            "AR430",
            "已退役 recipe id 不能重新写成正式 recipe。",
            f"recipe_id={recipe_id}",
            "创建新的 candidate recipe id，并重新走人工 review。",
        )
    content_hash = recipe_content_hash(recipe)
    if content_hash in state["blocked_content_hashes"]:
        tombstone_id = state["blocked_content_hashes"][content_hash]
        raise RecipesError(
            "AR431",
            "候选内容命中 active tombstone，停止转正。",
            f"content_hash={content_hash}, tombstone_id={tombstone_id}",
            "先人工核对；确需重新使用时显式 revoke tombstone，再用新 recipe id 重新 review。",
        )


def recipe_version_rank(recipe: dict[str, Any]) -> int:
    try:
        version = int(recipe.get("version") or 0)
    except (TypeError, ValueError):
        version = 0
    recipe_id = str(recipe.get("recipe_id") or "")
    suffix = re.search(r"[_-]v(\d+)$", recipe_id, flags=re.IGNORECASE)
    if suffix:
        version = max(version, int(suffix.group(1)))
    return version


def recipe_version_series(recipe_id: str) -> tuple[str, int] | None:
    match = VERSIONED_RECIPE_ID_RE.search(recipe_id)
    if not match:
        return None
    return match.group("base"), int(match.group("version"))


def active_recipe_ids_for_consumption(
    recipes: list[dict[str, Any]],
    *,
    retired_recipe_ids: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    recipe_ids = sorted(str(recipe.get("recipe_id") or "") for recipe in recipes if recipe.get("recipe_id"))
    inactive: set[str] = set(retired_recipe_ids or set())
    for recipe in recipes:
        recipe_id = str(recipe.get("recipe_id") or "")
        status = str(recipe.get("status") or "").casefold()
        if recipe_id and status in {"superseded", "archived", "rejected", "tombstoned", "retired"}:
            inactive.add(recipe_id)
        if recipe_id and _text_values(recipe.get("superseded_by")):
            inactive.add(recipe_id)
        for superseded_id in _text_values(recipe.get("supersedes")):
            inactive.add(superseded_id)

    series: dict[str, list[tuple[int, str]]] = {}
    for recipe_id in recipe_ids:
        if recipe_id in inactive:
            continue
        parsed = recipe_version_series(recipe_id)
        if parsed:
            base, version = parsed
            series.setdefault(base, []).append((version, recipe_id))

    for entries in series.values():
        if len(entries) <= 1:
            continue
        latest_version = max(version for version, _ in entries)
        inactive.update(recipe_id for version, recipe_id in entries if version < latest_version)

    active = [recipe_id for recipe_id in recipe_ids if recipe_id not in inactive]
    return active, sorted(recipe_id for recipe_id in inactive if recipe_id in recipe_ids)


def recipe_path_for(recipes_dir: Path, recipe_id: str) -> Path:
    return recipes_dir / "recipes" / f"{recipe_id}.json"


def recipe_exists(recipes_dir: Path, recipe_id: str) -> bool:
    return recipe_path_for(recipes_dir, recipe_id).exists()


def _text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value).strip()] if str(value).strip() else []
