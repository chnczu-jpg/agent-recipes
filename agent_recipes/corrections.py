from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from agent_recipes.lifecycle import recipe_exists
from agent_recipes.persistence import make_id


def correction_compile_plan(
    recipes_dir: Path,
    payload: dict[str, Any],
    event_id: str,
    *,
    load_recipe: Callable[[str], dict[str, Any]],
    first_line: Callable[[str], str],
    recipe_draft: Callable[[str, str, str, list[str]], dict[str, Any]],
) -> dict[str, Any]:
    bindings = payload.get("recipe_bindings") or []
    bound_ids = list(
        dict.fromkeys(
            str(binding.get("recipe_id") or "")
            for binding in bindings
            if isinstance(binding, dict) and binding.get("recipe_id")
        )
    )
    targeted = len(bound_ids) == 1 and recipe_exists(recipes_dir, bound_ids[0])
    recipe_id = bound_ids[0] if targeted else make_id("recipe", event_id)
    correction_text = str(payload.get("text") or "")
    title = first_line(correction_text or "Untitled correction")
    if not targeted:
        return {
            "recipe_id": recipe_id,
            "draft": recipe_draft(recipe_id, title, correction_text, [event_id]),
            "reason": "从 correction capture 生成第一版候选菜谱。",
            "question": f"是否接受候选菜谱：{title}",
        }

    current = load_recipe(recipe_id)
    draft = recipe_draft(recipe_id, str(current.get("title") or title), correction_text, [event_id])
    draft["verified_path"] = []
    return {
        "recipe_id": recipe_id,
        "draft": draft,
        "reason": "从绑定现有 recipe 的 correction capture 生成定向候选补丁。",
        "question": f"是否把纠偏补进现有菜谱：{current.get('title') or recipe_id}",
    }
