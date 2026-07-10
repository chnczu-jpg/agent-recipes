from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent_recipes.persistence import RecipesError, read_json, sha256_json, stable_json


OUTCOME_CAPTURE_MAP = {
    "success": "positive",
    "failure": "negative",
    "unknown": "unknown",
}
FEEDBACK_KIND_RULES = {
    "verified_success": ("success", "recipe", True, ""),
    "partial_success": ("success", "result", True, "review_result"),
    "generic_failure": ("failure", "recipe", True, "review_recipe"),
    "retrieval_mismatch": ("failure", "retrieval", False, "review_retrieval"),
    "execution_error": ("failure", "execution", False, "inspect_execution"),
    "recipe_incorrect": ("failure", "recipe", True, "review_recipe"),
    "recipe_outdated": ("failure", "recipe", True, "review_recipe"),
    "applicability_overreach": ("failure", "retrieval", True, "review_retrieval"),
    "missing_step": ("failure", "recipe", True, "review_recipe"),
    "excessive_cost": ("failure", "cost", True, "optimize_cost"),
    "recipe_conflict": ("failure", "conflict", True, "resolve_conflict"),
    "user_correction": ("failure", "recipe", True, "review_recipe"),
    "external_dependency": ("failure", "dependency", False, "inspect_dependency"),
    "insufficient_evidence": ("unknown", "evidence", True, "collect_evidence"),
    "evaluation_blocked": ("unknown", "evaluation", True, "unblock_evaluation"),
}
DEFAULT_FEEDBACK_KIND = {"success": "verified_success", "failure": "generic_failure", "unknown": "insufficient_evidence"}
OUTCOME_POLICY = {
    "legacy_inferred_can_enforce": False,
    "explicit_binding_required_for_enforcement": True,
    "degrade_after": "2 policy-eligible failures when failures >= successes",
    "hold_after": "3 consecutive policy-eligible failures or >=3 failures at >=60% failure rate",
    "unknown_changes_confidence": False,
    "automatic_recipe_mutation": False,
    "non_recipe_failures_can_degrade_recipe": False,
}


def recipe_bindings_from_lock(lock: dict[str, Any]) -> list[dict[str, Any]]:
    recipe_ids = lock.get("recipe_ids", [])
    versions = lock.get("recipe_versions", [])
    hashes = lock.get("recipe_hashes", [])
    if not isinstance(recipe_ids, list) or not isinstance(versions, list) or not isinstance(hashes, list):
        raise RecipesError("AR415", "lock 的 recipe binding 格式错误。", f"lock_id={lock.get('lock_id')}", "重新创建 lock。")
    if not recipe_ids or len(recipe_ids) != len(versions) or len(recipe_ids) != len(hashes):
        raise RecipesError("AR415", "lock 缺少完整 recipe id/version/hash。", f"lock_id={lock.get('lock_id')}", "重新创建 lock。")
    bindings: list[dict[str, Any]] = []
    for recipe_id, version, recipe_hash_value in zip(recipe_ids, versions, hashes):
        if not recipe_id or version is None or not recipe_hash_value:
            raise RecipesError("AR415", "lock 含有空 recipe binding。", f"lock_id={lock.get('lock_id')}", "重新创建 lock。")
        bindings.append(
            {
                "recipe_id": str(recipe_id),
                "recipe_version": version,
                "recipe_hash": str(recipe_hash_value),
            }
        )
    return bindings


def outcome_lock_snapshot_hash(lock_id: str, bindings: list[dict[str, Any]]) -> str:
    return sha256_json({"lock_id": lock_id, "recipe_bindings": bindings})


def feedback_metadata(capture_type: str, feedback_kind: str | None = None) -> dict[str, Any]:
    kind = feedback_kind or DEFAULT_FEEDBACK_KIND.get(capture_type)
    rule = FEEDBACK_KIND_RULES.get(str(kind))
    if not rule or rule[0] != capture_type:
        raise RecipesError("AR490", "feedback kind 与 capture type 不兼容。", f"capture_type={capture_type}, feedback_kind={feedback_kind}", "选择与 success/failure/unknown 对应的 feedback kind。")
    return {"feedback_kind": kind, "feedback_scope": rule[1], "policy_eligible": rule[2], "recommended_action": rule[3]}


def explicit_lock_capture_payload(lock: dict[str, Any], lock_id: str, capture_type: str, feedback_kind: str | None = None) -> dict[str, Any]:
    bindings = recipe_bindings_from_lock(lock)
    payload = {
        "recipe_bindings": bindings,
        "binding_source": "explicit_lock_snapshot",
        "lock_snapshot_hash": outcome_lock_snapshot_hash(lock_id, bindings),
    }
    if capture_type in OUTCOME_CAPTURE_MAP:
        payload.update({"outcome": OUTCOME_CAPTURE_MAP[capture_type], **feedback_metadata(capture_type, feedback_kind)})
    elif feedback_kind:
        feedback_metadata(capture_type, feedback_kind)
    return payload


def outcome_binding_key(binding: dict[str, Any]) -> tuple[str, str, str]:
    return (
        str(binding.get("recipe_id") or ""),
        stable_json(binding.get("recipe_version")),
        str(binding.get("recipe_hash") or ""),
    )


def empty_outcome_counts() -> dict[str, int]:
    return {"positive": 0, "negative": 0, "unknown": 0, "decisive": 0}


def valid_outcome_binding(binding: Any) -> bool:
    return (
        isinstance(binding, dict)
        and bool(binding.get("recipe_id"))
        and binding.get("recipe_version") is not None
        and bool(binding.get("recipe_hash"))
    )


def outcome_quality_state(
    recipes_dir: Path,
    events: list[dict[str, Any]],
    *,
    recipes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    binding_errors: list[str] = []
    unattributed_events: list[str] = []

    def ensure_row(binding: dict[str, Any]) -> dict[str, Any]:
        key = outcome_binding_key(binding)
        row = rows_by_key.get(key)
        if row is None:
            row = {
                "recipe_id": str(binding.get("recipe_id") or ""),
                "recipe_version": binding.get("recipe_version"),
                "recipe_hash": str(binding.get("recipe_hash") or ""),
                "all_attributable": empty_outcome_counts(),
                "legacy_inferred": empty_outcome_counts(),
                "non_policy_explicit": empty_outcome_counts(),
                "policy_eligible": {**empty_outcome_counts(), "consecutive_negative": 0},
                "feedback_kind_counts": {},
                "feedback_scope_counts": {},
                "_recommended_actions": set(),
                "_policy_sequence": [],
            }
            rows_by_key[key] = row
        return row

    for recipe in recipes or []:
        recipe_id = str(recipe.get("recipe_id") or "")
        recipe_hash_value = str(recipe.get("recipe_hash") or "")
        if recipe_id and recipe.get("version") is not None and recipe_hash_value:
            ensure_row(
                {
                    "recipe_id": recipe_id,
                    "recipe_version": recipe.get("version"),
                    "recipe_hash": recipe_hash_value,
                }
            )

    for event in events:
        if event.get("event_type") != "capture":
            continue
        payload = event.get("payload") or {}
        capture_type = str(payload.get("capture_type") or "")
        outcome = OUTCOME_CAPTURE_MAP.get(capture_type)
        if outcome is None:
            continue
        event_id = str(event.get("event_id") or "unknown")
        lock_id = str(payload.get("lock_id") or event.get("lock_id") or "")
        explicit = payload.get("binding_source") == "explicit_lock_snapshot"
        bindings = payload.get("recipe_bindings") if explicit else None
        policy_eligible = explicit and payload.get("policy_eligible") is True
        metadata = feedback_metadata(capture_type, payload.get("feedback_kind")) if explicit else {}

        lock: dict[str, Any] = {}
        if lock_id:
            lock_path = recipes_dir / "locks" / f"{lock_id}.json"
            try:
                lock = read_json(lock_path, {})
            except (OSError, json.JSONDecodeError):
                lock = {}

        if explicit:
            if not isinstance(bindings, list) or not bindings or not all(valid_outcome_binding(item) for item in bindings):
                binding_errors.append(f"explicit outcome binding 不完整：{event_id}")
                continue
            if not lock:
                binding_errors.append(f"explicit outcome binding 找不到 lock：{event_id}")
                continue
            try:
                expected = recipe_bindings_from_lock(lock)
            except RecipesError:
                binding_errors.append(f"explicit outcome binding 对应 lock 不完整：{event_id}")
                continue
            if payload.get("lock_id") != lock_id or event.get("lock_id") != lock_id:
                binding_errors.append(f"explicit outcome lock_id 不一致：{event_id}")
                continue
            if bindings != expected:
                binding_errors.append(f"explicit outcome binding 与 lock snapshot 不一致：{event_id}")
                continue
            if payload.get("lock_snapshot_hash") != outcome_lock_snapshot_hash(lock_id, bindings):
                binding_errors.append(f"explicit outcome lock snapshot hash 不一致：{event_id}")
                continue
            if not isinstance(payload.get("policy_eligible"), bool) or payload.get("policy_eligible") != metadata["policy_eligible"]:
                binding_errors.append(f"explicit outcome policy_eligible 与 feedback kind 不一致：{event_id}")
                continue
        else:
            if not lock:
                unattributed_events.append(event_id)
                continue
            try:
                bindings = recipe_bindings_from_lock(lock)
            except RecipesError:
                unattributed_events.append(event_id)
                continue

        for binding in bindings:
            row = ensure_row(binding)
            all_counts = row["all_attributable"]
            all_counts[outcome] += 1
            if outcome != "unknown":
                all_counts["decisive"] += 1
            target = row["policy_eligible"] if policy_eligible else (row["non_policy_explicit"] if explicit else row["legacy_inferred"])
            target[outcome] += 1
            if outcome != "unknown":
                target["decisive"] += 1
            if policy_eligible:
                row["_policy_sequence"].append(outcome)
            if explicit:
                kind, scope, action = metadata["feedback_kind"], metadata["feedback_scope"], metadata["recommended_action"]
                row["feedback_kind_counts"][kind] = row["feedback_kind_counts"].get(kind, 0) + 1
                row["feedback_scope_counts"][scope] = row["feedback_scope_counts"].get(scope, 0) + 1
                if action:
                    row["_recommended_actions"].add(action)

    rows: list[dict[str, Any]] = []
    for row in rows_by_key.values():
        all_counts = row["all_attributable"]
        policy_counts = row["policy_eligible"]
        decisive = policy_counts["decisive"]
        positive = policy_counts["positive"]
        negative = policy_counts["negative"]
        confidence_percent = round(((positive + 1) / (decisive + 2)) * 100) if decisive else 50
        if decisive == 0:
            confidence_band = "untested"
            maturity = "untested"
        else:
            confidence_band = "low" if confidence_percent < 40 else ("mixed" if confidence_percent < 70 else "strong")
            if negative >= 2 and negative >= positive:
                maturity = "challenged"
            elif decisive < 3:
                maturity = "observed"
            elif decisive >= 8 and positive / decisive >= 0.8:
                maturity = "proven"
            else:
                maturity = "established"

        consecutive_negative = 0
        for item in reversed(row.pop("_policy_sequence")):
            if item == "positive":
                break
            if item == "negative":
                consecutive_negative += 1
        policy_counts["consecutive_negative"] = consecutive_negative
        policy_decisive = policy_counts["decisive"]
        policy_negative = policy_counts["negative"]
        policy_positive = policy_counts["positive"]
        failure_rate = policy_negative / policy_decisive if policy_decisive else 0.0
        if consecutive_negative >= 3 or (policy_negative >= 3 and failure_rate >= 0.6):
            recommendation = "hold_for_review"
        elif policy_negative >= 2 and policy_negative >= policy_positive:
            recommendation = "degraded"
        elif policy_negative >= 1:
            recommendation = "caution"
        else:
            recommendation = "normal"
        row.update(
            {
                "confidence_percent": confidence_percent,
                "confidence_band": confidence_band,
                "maturity": maturity,
                "execution_recommendation": recommendation,
                "historical_warning": row["legacy_inferred"]["negative"] >= 2
                and row["legacy_inferred"]["negative"] >= row["legacy_inferred"]["positive"],
                "enforcement_basis": "explicit_binding_policy_eligible_only",
                "unknown_changes_confidence": False,
                "recommended_actions": sorted(row.pop("_recommended_actions")),
            }
        )
        rows.append(row)
    rows.sort(key=lambda item: outcome_binding_key(item))
    return {
        "recipes": rows,
        "binding_errors": binding_errors,
        "unattributed_events": unattributed_events,
    }


def find_outcome_quality(
    state: dict[str, Any],
    *,
    recipe_id: str,
    recipe_version: Any,
    recipe_hash_value: str,
) -> dict[str, Any]:
    key = outcome_binding_key(
        {"recipe_id": recipe_id, "recipe_version": recipe_version, "recipe_hash": recipe_hash_value}
    )
    for row in state.get("recipes", []):
        if outcome_binding_key(row) == key:
            return row
    return {
        "recipe_id": recipe_id,
        "recipe_version": recipe_version,
        "recipe_hash": recipe_hash_value,
        "all_attributable": empty_outcome_counts(),
        "legacy_inferred": empty_outcome_counts(),
        "non_policy_explicit": empty_outcome_counts(),
        "policy_eligible": {**empty_outcome_counts(), "consecutive_negative": 0},
        "feedback_kind_counts": {},
        "feedback_scope_counts": {},
        "confidence_percent": 50,
        "confidence_band": "untested",
        "maturity": "untested",
        "execution_recommendation": "normal",
        "historical_warning": False,
        "enforcement_basis": "explicit_binding_policy_eligible_only",
        "unknown_changes_confidence": False,
        "recommended_actions": [],
    }


def outcome_quality_summary(rows: list[dict[str, Any]], *, state: dict[str, Any] | None = None) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "recipe_version_count": len(rows),
        "attributable_outcome_count": sum(item["all_attributable"]["positive"] + item["all_attributable"]["negative"] + item["all_attributable"]["unknown"] for item in rows),
        "policy_eligible_outcome_count": sum(item["policy_eligible"]["positive"] + item["policy_eligible"]["negative"] + item["policy_eligible"]["unknown"] for item in rows),
        "historical_warning_count": sum(1 for item in rows if item.get("historical_warning")),
        "degraded_count": sum(1 for item in rows if item.get("execution_recommendation") == "degraded"),
        "hard_hold_count": sum(1 for item in rows if item.get("execution_recommendation") == "hold_for_review"),
        "binding_error_count": len((state or {}).get("binding_errors", [])),
        "unattributed_event_count": len((state or {}).get("unattributed_events", [])),
    }
    kinds: dict[str, int] = {}
    actions: set[str] = set()
    for row in rows:
        for kind, count in row.get("feedback_kind_counts", {}).items():
            kinds[kind] = kinds.get(kind, 0) + count
        actions.update(row.get("recommended_actions", []))
    if kinds:
        summary.update({"feedback_kind_counts": kinds, "recommended_actions": sorted(actions)})
    return summary
