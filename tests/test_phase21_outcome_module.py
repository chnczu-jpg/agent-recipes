from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_recipes.core import outcome_quality_state as core_outcome_quality_state
from agent_recipes.outcome import (
    outcome_lock_snapshot_hash,
    outcome_quality_state,
    recipe_bindings_from_lock,
)
from agent_recipes.persistence import write_json


REPO_ROOT = Path(__file__).resolve().parents[1]


def lock_doc(lock_id: str, recipe_id: str, version: int, recipe_hash: str) -> dict:
    return {
        "lock_id": lock_id,
        "recipe_ids": [recipe_id],
        "recipe_versions": [version],
        "recipe_hashes": [recipe_hash],
        "status": "active",
    }


def explicit_event(index: int, capture_type: str, lock: dict, *, snapshot_hash: str | None = None) -> dict:
    bindings = recipe_bindings_from_lock(lock)
    lock_id = lock["lock_id"]
    return {
        "event_id": f"evt_{index}",
        "event_type": "capture",
        "lock_id": lock_id,
        "payload": {
            "capture_type": capture_type,
            "lock_id": lock_id,
            "recipe_bindings": bindings,
            "binding_source": "explicit_lock_snapshot",
            "policy_eligible": True,
            "lock_snapshot_hash": snapshot_hash or outcome_lock_snapshot_hash(lock_id, bindings),
        },
    }


def legacy_event(index: int, capture_type: str, lock_id: str) -> dict:
    return {
        "event_id": f"legacy_{index}",
        "event_type": "capture",
        "lock_id": lock_id,
        "payload": {"capture_type": capture_type, "lock_id": lock_id},
    }


class Phase21OutcomeModuleTest(unittest.TestCase):
    def test_outcome_import_is_independent_and_core_reexports_same_function(self) -> None:
        code = """
import sys
from agent_recipes.outcome import outcome_quality_state
assert 'agent_recipes.core' not in sys.modules
assert outcome_quality_state.__module__ == 'agent_recipes.outcome'
"""
        proc = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(core_outcome_quality_state, outcome_quality_state)

    def test_failures_are_isolated_to_exact_recipe_version_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipes_dir = Path(tmp) / ".recipes"
            v1 = lock_doc("lock_v1", "recipe_alpha", 1, "hash_v1")
            v2 = lock_doc("lock_v2", "recipe_alpha", 2, "hash_v2")
            write_json(recipes_dir / "locks" / "lock_v1.json", v1)
            write_json(recipes_dir / "locks" / "lock_v2.json", v2)
            events = [
                explicit_event(1, "failure", v1),
                explicit_event(2, "failure", v1),
                explicit_event(3, "failure", v1),
                explicit_event(4, "success", v2),
            ]

            state = outcome_quality_state(
                recipes_dir,
                events,
                recipes=[
                    {"recipe_id": "recipe_alpha", "version": 1, "recipe_hash": "hash_v1"},
                    {"recipe_id": "recipe_alpha", "version": 2, "recipe_hash": "hash_v2"},
                ],
            )
            by_version = {row["recipe_version"]: row for row in state["recipes"]}

            self.assertEqual(by_version[1]["execution_recommendation"], "hold_for_review")
            self.assertEqual(by_version[2]["execution_recommendation"], "normal")
            self.assertEqual(by_version[2]["policy_eligible"]["negative"], 0)

    def test_unknown_is_neutral_and_success_resets_consecutive_failures(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipes_dir = Path(tmp) / ".recipes"
            lock = lock_doc("lock_alpha", "recipe_alpha", 1, "hash_alpha")
            write_json(recipes_dir / "locks" / "lock_alpha.json", lock)
            events = [
                explicit_event(1, "failure", lock),
                explicit_event(2, "failure", lock),
                explicit_event(3, "success", lock),
                explicit_event(4, "success", lock),
                explicit_event(5, "success", lock),
                explicit_event(6, "unknown", lock),
                explicit_event(7, "failure", lock),
            ]

            row = outcome_quality_state(recipes_dir, events)["recipes"][0]

            self.assertEqual(row["policy_eligible"]["unknown"], 1)
            self.assertEqual(row["policy_eligible"]["decisive"], 6)
            self.assertEqual(row["policy_eligible"]["consecutive_negative"], 1)
            self.assertEqual(row["execution_recommendation"], "degraded")
            self.assertFalse(row["unknown_changes_confidence"])

    def test_legacy_failures_warn_but_never_enforce_hold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipes_dir = Path(tmp) / ".recipes"
            lock = lock_doc("legacy_lock", "recipe_alpha", 1, "hash_alpha")
            write_json(recipes_dir / "locks" / "legacy_lock.json", lock)
            events = [legacy_event(index, "failure", "legacy_lock") for index in range(1, 4)]

            row = outcome_quality_state(recipes_dir, events)["recipes"][0]

            self.assertEqual(row["legacy_inferred"]["negative"], 3)
            self.assertEqual(row["policy_eligible"]["negative"], 0)
            self.assertTrue(row["historical_warning"])
            self.assertEqual(row["execution_recommendation"], "normal")

    def test_snapshot_mismatch_is_rejected_without_counting_outcome(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipes_dir = Path(tmp) / ".recipes"
            lock = lock_doc("lock_alpha", "recipe_alpha", 1, "hash_alpha")
            write_json(recipes_dir / "locks" / "lock_alpha.json", lock)
            event = explicit_event(1, "failure", lock, snapshot_hash="tampered")

            state = outcome_quality_state(
                recipes_dir,
                [event],
                recipes=[{"recipe_id": "recipe_alpha", "version": 1, "recipe_hash": "hash_alpha"}],
            )

            self.assertEqual(len(state["binding_errors"]), 1)
            self.assertIn("snapshot hash", state["binding_errors"][0])
            self.assertEqual(state["recipes"][0]["all_attributable"]["negative"], 0)


if __name__ == "__main__":
    unittest.main()
