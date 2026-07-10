from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, RecipesProject, recipe_content_hash, recipe_hash, write_json
from agent_recipes.mcp import call_tool, tool_list


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


def seed_recipe(project: Path, text: str = "lifecycle alpha beta recipe") -> tuple[RecipesProject, dict[str, Any]]:
    core = RecipesProject(project)
    core.init()
    core.capture("correction", text)
    compiled = core.compile()
    accepted = core.accept_review(compiled["created"][0]["review_id"])
    return core, core.load_recipe(accepted["recipe_id"])


def add_review(project: Path, proposed: dict[str, Any], suffix: str) -> str:
    patch_id = f"patch_{suffix}"
    review_id = f"review_{suffix}"
    patch = {
        "patch_id": patch_id,
        "source_event_ids": [],
        "target_recipe_id": proposed["recipe_id"],
        "proposed_change": proposed,
        "reason": "lifecycle test candidate",
        "evidence_refs": [],
        "risk": "needs_review",
        "status": "pending_review",
    }
    review = {
        "review_id": review_id,
        "blocking_level": "P0",
        "question": "accept lifecycle test candidate",
        "why_user_must_decide": "formal recipe write",
        "options": ["accept", "reject"],
        "recommendation": "accept",
        "evidence_refs": [],
        "proposed_patch_id": patch_id,
        "status": "pending",
        "decided_by": None,
        "decided_at": None,
    }
    write_json(project / ".recipes" / "candidates" / f"{patch_id}.json", patch)
    write_json(project / ".recipes" / "review_queue" / f"{review_id}.json", review)
    return review_id


class Phase13LifecycleTest(unittest.TestCase):
    def test_tombstone_requires_current_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, recipe = seed_recipe(Path(tmp))

            with self.assertRaises(RecipesError) as caught:
                core.tombstone_recipe(
                    recipe["recipe_id"],
                    lock_id=None,
                    reason_kind="correction",
                    reason="rule is wrong",
                )

            self.assertEqual(caught.exception.code, "AR410")
            self.assertFalse(core.recipe_lifecycle_status(recipe["recipe_id"])["retired"])

    def test_tombstone_removes_recipe_from_lookup_and_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core, recipe = seed_recipe(project)
            locked = core.create_lock(recipe["recipe_id"], task="retire bad recipe")
            lock_id = locked["lock"]["lock_id"]

            result = core.tombstone_recipe(
                recipe["recipe_id"],
                lock_id=lock_id,
                reason_kind="correction",
                reason="confirmed wrong rule",
            )

            self.assertEqual(result["idempotency_status"], "created")
            retired_lock = json.loads((project / ".recipes" / "locks" / f"{lock_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(retired_lock["status"], "tombstoned")
            with self.assertRaises(RecipesError) as lookup_error:
                core.lookup("lifecycle alpha beta recipe", strict=True, min_score=1)
            self.assertEqual(lookup_error.exception.code, "AR244")
            with self.assertRaises(RecipesError) as lock_error:
                core.create_lock(recipe["recipe_id"])
            self.assertEqual(lock_error.exception.code, "AR432")
            doctor = core.doctor()
            self.assertEqual(doctor["status"], "ok")
            self.assertEqual(doctor["summary"]["recipe_lifecycle"]["active_tombstone_count"], 1)

    def test_tombstone_is_idempotent_and_blocks_content_resurrection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core, recipe = seed_recipe(project)
            lock_id = core.create_lock(recipe["recipe_id"])["lock"]["lock_id"]
            first = core.tombstone_recipe(
                recipe["recipe_id"],
                lock_id=lock_id,
                reason_kind="retraction",
                reason="source was retracted",
            )
            replay = core.tombstone_recipe(
                recipe["recipe_id"],
                lock_id=lock_id,
                reason_kind="retraction",
                reason="source was retracted",
            )
            self.assertEqual(replay["idempotency_status"], "unchanged")

            proposed = {key: value for key, value in recipe.items() if key not in {"recipe_id", "recipe_hash", "version"}}
            proposed.update({"recipe_id": "recipe_reborn_copy", "version": 0})
            self.assertEqual(recipe_content_hash(proposed), first["tombstone"]["content_hash"])
            review_id = add_review(project, proposed, "reborn_blocked")
            with self.assertRaises(RecipesError) as caught:
                core.accept_review(review_id)
            self.assertEqual(caught.exception.code, "AR431")

    def test_revocation_only_allows_new_reviewed_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core, recipe = seed_recipe(project)
            lock_id = core.create_lock(recipe["recipe_id"])["lock"]["lock_id"]
            retired = core.tombstone_recipe(
                recipe["recipe_id"],
                lock_id=lock_id,
                reason_kind="contradiction_resolution",
                reason="conflicting evidence",
            )
            tombstone_id = retired["tombstone"]["tombstone_id"]

            revoked = core.revoke_recipe_tombstone(tombstone_id, reason="new evidence restored the procedure")
            self.assertEqual(revoked["idempotency_status"], "created")
            status = core.recipe_lifecycle_status(recipe["recipe_id"])
            self.assertTrue(status["retired"])
            self.assertEqual(status["summary"]["blocked_content_count"], 0)
            with self.assertRaises(RecipesError) as old_id_error:
                core.assert_recipe_can_be_promoted(recipe)
            self.assertEqual(old_id_error.exception.code, "AR430")

            proposed = {key: value for key, value in recipe.items() if key not in {"recipe_id", "recipe_hash", "version"}}
            proposed.update({"recipe_id": "recipe_reviewed_after_revocation", "version": 0})
            review_id = add_review(project, proposed, "reborn_reviewed")
            accepted = core.accept_review(review_id)
            self.assertEqual(accepted["recipe_id"], "recipe_reviewed_after_revocation")
            lookup = core.lookup("lifecycle alpha beta recipe", strict=True, min_score=1)
            self.assertEqual(lookup["recipe"]["recipe_id"], "recipe_reviewed_after_revocation")
            self.assertIn(recipe["recipe_id"], lookup["inactive_recipe_ids"])

    def test_doctor_detects_retired_recipe_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core, recipe = seed_recipe(project)
            lock_id = core.create_lock(recipe["recipe_id"])["lock"]["lock_id"]
            core.tombstone_recipe(
                recipe["recipe_id"],
                lock_id=lock_id,
                reason_kind="correction",
                reason="wrong behavior",
            )
            recipe["steps"] = ["silently rewritten retired rule"]
            recipe["recipe_hash"] = recipe_hash(recipe)
            write_json(project / ".recipes" / "recipes" / f"{recipe['recipe_id']}.json", recipe)

            doctor = core.doctor()

            self.assertEqual(doctor["status"], "error")
            self.assertTrue(any(error["code"] in {"AR309", "AR310"} for error in doctor["errors"]))

    def test_cli_and_mcp_expose_same_lifecycle_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_project = root / "cli"
            mcp_project = root / "mcp"
            cli_project.mkdir()
            mcp_project.mkdir()
            cli_core, cli_recipe = seed_recipe(cli_project)
            mcp_core, mcp_recipe = seed_recipe(mcp_project)
            cli_lock = cli_core.create_lock(cli_recipe["recipe_id"])["lock"]["lock_id"]
            mcp_lock = mcp_core.create_lock(mcp_recipe["recipe_id"])["lock"]["lock_id"]

            cli_result = run_cli(
                cli_project,
                "recipe-lifecycle",
                "--action",
                "tombstone",
                "--recipe",
                cli_recipe["recipe_id"],
                "--lock",
                cli_lock,
                "--reason",
                "parity retirement",
            )
            mcp_result = call_tool(
                "recipe_lifecycle",
                {
                    "project": str(mcp_project),
                    "action": "tombstone",
                    "recipe_id": mcp_recipe["recipe_id"],
                    "lock_id": mcp_lock,
                    "reason": "parity retirement",
                },
            )

            self.assertIn("agent_recipes_recipe_lifecycle", [item["name"] for item in tool_list()])
            self.assertEqual(cli_result["action"], mcp_result["action"])
            self.assertEqual(cli_result["tombstone"]["content_hash"], mcp_result["tombstone"]["content_hash"])
            cli_status = run_cli(
                cli_project,
                "recipe-lifecycle",
                "--action",
                "status",
                "--recipe",
                cli_recipe["recipe_id"],
            )
            mcp_status = call_tool(
                "recipe_lifecycle",
                {"project": str(mcp_project), "action": "status", "recipe_id": mcp_recipe["recipe_id"]},
            )
            self.assertTrue(cli_status["retired"])
            self.assertTrue(mcp_status["retired"])


if __name__ == "__main__":
    unittest.main()
