from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_recipes.core import RecipesError, RecipesProject
from agent_recipes.mcp import call_tool, tool_list


ROOT = Path(__file__).resolve().parents[1]


def seed_recipe(root: Path) -> tuple[RecipesProject, dict, dict]:
    project = RecipesProject(root)
    project.init()
    project.capture("correction", "rich feedback exact governed recipe")
    review = project.compile()["created"][0]
    accepted = project.accept_review(review["review_id"])
    recipe = project.load_recipe(accepted["recipe_id"])
    lock = project.create_lock(recipe["recipe_id"], task="rich feedback test")["lock"]
    return project, recipe, lock


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if expect_ok and proc.returncode:
        raise AssertionError(proc.stderr or proc.stdout)
    return json.loads(proc.stdout or proc.stderr)


class Phase27RichFeedbackTest(unittest.TestCase):
    def test_execution_errors_are_attributed_but_do_not_degrade_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, recipe, lock = seed_recipe(Path(tmp))
            for index in range(3):
                project.capture(
                    "failure",
                    f"agent clicked the wrong control {index}",
                    lock_id=lock["lock_id"],
                    feedback_kind="execution_error",
                )

            row = project.outcome_status(recipe_id=recipe["recipe_id"])["recipes"][0]

            self.assertEqual(row["all_attributable"]["negative"], 3)
            self.assertEqual(row["policy_eligible"]["negative"], 0)
            self.assertEqual(row["non_policy_explicit"]["negative"], 3)
            self.assertEqual(row["feedback_kind_counts"]["execution_error"], 3)
            self.assertEqual(row["recommended_actions"], ["inspect_execution"])
            self.assertEqual(row["execution_recommendation"], "normal")
            status = project.outcome_status(recipe_id=recipe["recipe_id"])
            self.assertEqual(status["schema_version"], "1.1")
            self.assertIn("recipe_outdated", status["feedback_kinds"])
            self.assertFalse(status["policy"]["non_recipe_failures_can_degrade_recipe"])

    def test_recipe_errors_degrade_exact_version_and_propose_review(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, recipe, lock = seed_recipe(Path(tmp))
            project.capture("failure", "step one is wrong", lock_id=lock["lock_id"], feedback_kind="recipe_incorrect")
            project.capture("failure", "step two is missing", lock_id=lock["lock_id"], feedback_kind="missing_step")

            row = project.outcome_status(recipe_id=recipe["recipe_id"])["recipes"][0]

            self.assertEqual(row["policy_eligible"]["negative"], 2)
            self.assertEqual(row["execution_recommendation"], "degraded")
            self.assertEqual(row["recommended_actions"], ["review_recipe"])
            self.assertEqual(row["feedback_scope_counts"]["recipe"], 2)

    def test_feedback_kind_must_match_capture_type_without_writing_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, _, lock = seed_recipe(Path(tmp))
            before = project.events_path.read_bytes()

            with self.assertRaises(RecipesError) as caught:
                project.capture(
                    "success",
                    "this combination is invalid",
                    lock_id=lock["lock_id"],
                    feedback_kind="recipe_incorrect",
                )

            self.assertEqual(caught.exception.code, "AR490")
            self.assertEqual(project.events_path.read_bytes(), before)

    def test_same_text_with_different_feedback_kinds_is_not_deduplicated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project, recipe, lock = seed_recipe(Path(tmp))
            first = project.capture("failure", "same symptom", lock_id=lock["lock_id"], feedback_kind="execution_error")
            second = project.capture("failure", "same symptom", lock_id=lock["lock_id"], feedback_kind="recipe_incorrect")
            status = project.outcome_status(recipe_id=recipe["recipe_id"])

            self.assertNotEqual(first["event_id"], second["event_id"])
            self.assertEqual(status["summary"]["feedback_kind_counts"]["execution_error"], 1)
            self.assertEqual(status["summary"]["feedback_kind_counts"]["recipe_incorrect"], 1)

    def test_cli_and_mcp_expose_the_same_rich_feedback_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_root = root / "cli"
            mcp_root = root / "mcp"
            cli_root.mkdir()
            mcp_root.mkdir()
            cli_project, cli_recipe, cli_lock = seed_recipe(cli_root)
            _, mcp_recipe, mcp_lock = seed_recipe(mcp_root)

            cli_capture = run_cli(
                cli_root,
                "capture",
                "--type",
                "failure",
                "--feedback-kind",
                "retrieval_mismatch",
                "--text",
                "lookup selected the wrong recipe",
                "--lock",
                cli_lock["lock_id"],
            )
            mcp_capture = call_tool(
                "capture",
                {
                    "project": str(mcp_root),
                    "capture_type": "failure",
                    "feedback_kind": "retrieval_mismatch",
                    "text": "lookup selected the wrong recipe",
                    "lock_id": mcp_lock["lock_id"],
                },
            )
            cli_status = cli_project.outcome_status(recipe_id=cli_recipe["recipe_id"])
            mcp_status = call_tool("outcome_status", {"project": str(mcp_root), "recipe_id": mcp_recipe["recipe_id"]})

            self.assertEqual(cli_capture["feedback_kind"], "retrieval_mismatch")
            self.assertEqual(mcp_capture["feedback_kind"], "retrieval_mismatch")
            self.assertEqual(cli_status["summary"], mcp_status["summary"])
            capture_tool = next(item for item in tool_list() if item["name"] == "agent_recipes_capture")
            self.assertIn("feedback_kind", capture_tool["inputSchema"]["properties"])


if __name__ == "__main__":
    unittest.main()
