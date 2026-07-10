from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import (
    RecipesError,
    RecipesProject,
    claim_status,
    outcome_lock_snapshot_hash,
    recipe_bindings_from_lock,
)
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


def seed_recipe(project: Path) -> tuple[RecipesProject, dict[str, Any]]:
    core = RecipesProject(project)
    core.init()
    core.capture("correction", "outcome alpha beta exact recipe")
    compiled = core.compile()
    accepted = core.accept_review(compiled["created"][0]["review_id"])
    return core, core.load_recipe(accepted["recipe_id"])


def exact_row(core: RecipesProject, recipe_id: str) -> dict[str, Any]:
    rows = core.outcome_status(recipe_id=recipe_id)["recipes"]
    if len(rows) != 1:
        raise AssertionError(f"expected one row, got {rows}")
    return rows[0]


class Phase15OutcomeQualityTest(unittest.TestCase):
    def test_unknown_has_exact_binding_and_does_not_change_confidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, recipe = seed_recipe(Path(tmp))
            lock = core.create_lock(recipe["recipe_id"], task="unknown evidence")["lock"]

            result = core.capture("unknown", "无法判断这次结果", lock_id=lock["lock_id"])
            row = exact_row(core, recipe["recipe_id"])

            self.assertEqual(result["outcome"], "unknown")
            self.assertEqual(result["recipe_bindings"], recipe_bindings_from_lock(lock))
            self.assertEqual(row["policy_eligible"]["unknown"], 1)
            self.assertEqual(row["confidence_percent"], 50)
            self.assertEqual(row["confidence_band"], "untested")
            self.assertEqual(row["execution_recommendation"], "normal")
            self.assertTrue((core.recipes_dir / "unknowns" / f"{result['event_id']}.json").exists())

    def test_explicit_success_failure_update_exact_recipe_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, recipe = seed_recipe(Path(tmp))
            lock = core.create_lock(recipe["recipe_id"], task="mixed outcomes")["lock"]

            core.capture("success", "first result passed", lock_id=lock["lock_id"])
            core.capture("failure", "second result failed", lock_id=lock["lock_id"])
            row = exact_row(core, recipe["recipe_id"])

            self.assertEqual(row["recipe_version"], recipe["version"])
            self.assertEqual(row["recipe_hash"], recipe["recipe_hash"])
            self.assertEqual(row["policy_eligible"]["positive"], 1)
            self.assertEqual(row["policy_eligible"]["negative"], 1)
            self.assertEqual(row["confidence_percent"], 50)
            self.assertEqual(row["execution_recommendation"], "caution")

    def test_legacy_inferred_failures_warn_but_never_enforce(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, recipe = seed_recipe(Path(tmp))
            lock = core.create_lock(recipe["recipe_id"], task="legacy outcomes")["lock"]
            for index in range(3):
                core.append_event(
                    "capture",
                    {
                        "capture_type": "failure",
                        "task": "legacy",
                        "text": f"legacy failure {index}",
                        "lock_id": lock["lock_id"],
                    },
                    lock_id=lock["lock_id"],
                    idempotency_key=f"legacy-failure-{index}",
                    claim_status=claim_status(verified=["legacy fixture"]),
                )

            row = exact_row(core, recipe["recipe_id"])
            new_lock = core.create_lock(recipe["recipe_id"], task="legacy must not block")["lock"]

            self.assertEqual(row["legacy_inferred"]["negative"], 3)
            self.assertEqual(row["policy_eligible"]["negative"], 0)
            self.assertTrue(row["historical_warning"])
            self.assertEqual(row["execution_recommendation"], "normal")
            self.assertEqual(new_lock["status"], "active")

    def test_two_new_failures_degrade_next_lock_without_mutating_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, recipe = seed_recipe(Path(tmp))
            lock = core.create_lock(recipe["recipe_id"], task="degrade source")["lock"]
            recipe_path = core.recipes_dir / "recipes" / f"{recipe['recipe_id']}.json"
            before = recipe_path.read_bytes()

            core.capture("failure", "new failure one", lock_id=lock["lock_id"])
            core.capture("failure", "new failure two", lock_id=lock["lock_id"])
            row = exact_row(core, recipe["recipe_id"])
            degraded_lock = core.create_lock(recipe["recipe_id"], task="degraded follow-up")["lock"]

            self.assertEqual(row["execution_recommendation"], "degraded")
            self.assertEqual(degraded_lock["outcome_quality"]["execution_recommendation"], "degraded")
            self.assertTrue(any("人工复核" in item for item in degraded_lock["claim_limits"]))
            self.assertEqual(recipe_path.read_bytes(), before)

    def test_three_consecutive_new_failures_hold_new_lock_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, recipe = seed_recipe(Path(tmp))
            lock = core.create_lock(recipe["recipe_id"], task="hold source")["lock"]
            for index in range(3):
                core.capture("failure", f"policy failure {index}", lock_id=lock["lock_id"])

            row = exact_row(core, recipe["recipe_id"])
            with self.assertRaises(RecipesError) as caught:
                core.create_lock(recipe["recipe_id"], task="must be held")

            self.assertEqual(row["policy_eligible"]["consecutive_negative"], 3)
            self.assertEqual(row["execution_recommendation"], "hold_for_review")
            self.assertEqual(caught.exception.code, "AR440")

    def test_doctor_rejects_explicit_binding_that_disagrees_with_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core, recipe = seed_recipe(Path(tmp))
            lock = core.create_lock(recipe["recipe_id"], task="tampered outcome")["lock"]
            wrong_bindings = recipe_bindings_from_lock(lock)
            wrong_bindings[0]["recipe_hash"] = "wrong-hash"
            core.append_event(
                "capture",
                {
                    "capture_type": "failure",
                    "task": "tampered",
                    "text": "tampered binding",
                    "lock_id": lock["lock_id"],
                    "outcome": "negative",
                    "recipe_bindings": wrong_bindings,
                    "binding_source": "explicit_lock_snapshot",
                    "policy_eligible": True,
                    "lock_snapshot_hash": outcome_lock_snapshot_hash(lock["lock_id"], wrong_bindings),
                },
                lock_id=lock["lock_id"],
                idempotency_key="tampered-outcome-binding",
                claim_status=claim_status(verified=["tamper fixture"]),
            )

            doctor = core.doctor()
            readiness = core.readiness()

            self.assertEqual(doctor["status"], "error")
            self.assertTrue(any(item["code"] == "AR312" for item in doctor["errors"]))
            self.assertEqual(readiness["axes"]["outcomes"]["status"], "blocked")
            self.assertEqual(readiness["overall"], "blocked")

    def test_cli_and_mcp_expose_same_outcome_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_project = root / "cli"
            mcp_project = root / "mcp"
            cli_project.mkdir()
            mcp_project.mkdir()
            cli_core, cli_recipe = seed_recipe(cli_project)
            mcp_core, mcp_recipe = seed_recipe(mcp_project)
            cli_lock = cli_core.create_lock(cli_recipe["recipe_id"], task="adapter parity")["lock"]
            mcp_lock = mcp_core.create_lock(mcp_recipe["recipe_id"], task="adapter parity")["lock"]

            run_cli(
                cli_project,
                "capture",
                "--type",
                "unknown",
                "--text",
                "adapter parity unknown",
                "--lock",
                cli_lock["lock_id"],
            )
            call_tool(
                "capture",
                {
                    "project": str(mcp_project),
                    "capture_type": "unknown",
                    "text": "adapter parity unknown",
                    "lock_id": mcp_lock["lock_id"],
                },
            )
            cli_result = run_cli(cli_project, "outcome-status", "--recipe", cli_recipe["recipe_id"])
            mcp_result = call_tool(
                "outcome_status",
                {"project": str(mcp_project), "recipe_id": mcp_recipe["recipe_id"]},
            )

            self.assertIn("agent_recipes_outcome_status", [item["name"] for item in tool_list()])
            self.assertEqual(cli_result["summary"], mcp_result["summary"])
            self.assertEqual(cli_result["recipes"], mcp_result["recipes"])
            self.assertEqual(mcp_result["tool"], "agent_recipes_outcome_status")


if __name__ == "__main__":
    unittest.main()
