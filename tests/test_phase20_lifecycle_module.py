from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from agent_recipes.core import recipe_lifecycle_state as core_lifecycle_state
from agent_recipes.lifecycle import (
    active_recipe_ids_for_consumption,
    assert_recipe_promotable,
    recipe_content_hash,
    recipe_lifecycle_state,
)
from agent_recipes.persistence import RecipesError


REPO_ROOT = Path(__file__).resolve().parents[1]


def recipe(recipe_id: str) -> dict:
    return {
        "recipe_id": recipe_id,
        "version": 1,
        "title": "same operational recipe",
        "scope": "lifecycle test",
        "use_when": ["alpha beta"],
        "steps": ["do the safe thing"],
        "cannot_claim": ["candidate is not execution proof"],
    }


def tombstone_event(target: dict, *, tombstone_id: str = "tomb_same") -> dict:
    return {
        "event_id": "evt_tomb",
        "event_type": "recipe_tombstoned",
        "created_at": "2026-07-10T00:00:00Z",
        "payload": {
            "tombstone_id": tombstone_id,
            "recipe_id": target["recipe_id"],
            "recipe_version": target["version"],
            "recipe_hash": "recipe_hash_fixture",
            "content_hash": recipe_content_hash(target),
            "reason_kind": "correction",
            "reason": "unsafe old guidance",
            "permanent_recipe_id_retirement": True,
        },
    }


class Phase20LifecycleModuleTest(unittest.TestCase):
    def test_lifecycle_import_is_independent_and_core_reexports_same_function(self) -> None:
        code = """
import sys
from agent_recipes.lifecycle import recipe_lifecycle_state
assert 'agent_recipes.core' not in sys.modules
assert recipe_lifecycle_state.__module__ == 'agent_recipes.lifecycle'
"""
        proc = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(core_lifecycle_state, recipe_lifecycle_state)

    def test_active_tombstone_blocks_same_content_under_new_id(self) -> None:
        old = recipe("recipe_old")
        renamed = recipe("recipe_renamed")
        events = [tombstone_event(old)]

        with self.assertRaises(RecipesError) as blocked:
            assert_recipe_promotable(events, renamed)

        self.assertEqual(blocked.exception.code, "AR431")

    def test_revocation_allows_new_id_but_never_reactivates_old_id(self) -> None:
        old = recipe("recipe_old")
        renamed = recipe("recipe_renamed")
        events = [
            tombstone_event(old),
            {
                "event_id": "evt_revoke",
                "event_type": "recipe_tombstone_revoked",
                "created_at": "2026-07-10T00:01:00Z",
                "payload": {"tombstone_id": "tomb_same"},
            },
        ]

        assert_recipe_promotable(events, renamed)
        with self.assertRaises(RecipesError) as blocked_old:
            assert_recipe_promotable(events, old)

        self.assertEqual(blocked_old.exception.code, "AR430")
        state = recipe_lifecycle_state(events)
        self.assertIn("recipe_old", state["retired_recipe_ids"])
        self.assertFalse(state["blocked_content_hashes"])

    def test_active_selection_excludes_superseded_retired_and_older_series(self) -> None:
        recipes = [
            {"recipe_id": "alpha_v1", "version": 1},
            {"recipe_id": "alpha_v2", "version": 2},
            {"recipe_id": "beta", "status": "superseded"},
            {"recipe_id": "gamma", "supersedes": ["delta"]},
            {"recipe_id": "delta"},
            {"recipe_id": "retired"},
        ]

        active, inactive = active_recipe_ids_for_consumption(recipes, retired_recipe_ids={"retired"})

        self.assertEqual(active, ["alpha_v2", "gamma"])
        self.assertEqual(inactive, ["alpha_v1", "beta", "delta", "retired"])


if __name__ == "__main__":
    unittest.main()

