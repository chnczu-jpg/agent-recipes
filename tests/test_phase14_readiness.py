from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesProject
from agent_recipes.mcp import call_tool, tool_list


REPO_ROOT = Path(__file__).resolve().parents[1]
GOLDEN = REPO_ROOT / "tests" / "golden" / "readiness_ready_v1.json"


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


def seed_ready_project(project: Path) -> RecipesProject:
    core = RecipesProject(project)
    core.init()
    core.capture("correction", "golden readiness exact recipe")
    compiled = core.compile()
    core.accept_review(compiled["created"][0]["review_id"])
    return core


class Phase14ReadinessTest(unittest.TestCase):
    def test_ready_response_matches_golden_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = seed_ready_project(Path(tmp)).readiness()
            expected = json.loads(GOLDEN.read_text(encoding="utf-8"))

            self.assertEqual(result, expected)

    def test_no_formal_recipe_is_degraded_with_blocking_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = RecipesProject(Path(tmp))
            core.init()

            result = core.readiness()

            self.assertTrue(result["ok"])
            self.assertEqual(result["overall"], "degraded")
            self.assertEqual(result["axes"]["recipes"]["status"], "degraded")
            self.assertEqual(result["recommended_action"]["action_id"], "promote-first-recipe")
            self.assertTrue(result["recommended_action"]["blocking"])

    def test_ledger_tampering_blocks_mutation_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = seed_ready_project(project)
            events_path = project / ".recipes" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            events[-1]["prev_event_hash"] = "tampered"
            events_path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events) + "\n",
                encoding="utf-8",
            )

            result = core.readiness()

            self.assertFalse(result["ok"])
            self.assertEqual(result["overall"], "blocked")
            self.assertEqual(result["axes"]["ledger"]["status"], "blocked")
            self.assertEqual(result["recommended_action"]["action_id"], "repair-core")
            self.assertTrue(result["recommended_action"]["blocking"])

    def test_malformed_review_degrades_without_deleting_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = seed_ready_project(project)
            malformed = project / ".recipes" / "review_queue" / "review_malformed.json"
            malformed.write_text("{not-json", encoding="utf-8")

            result = core.readiness()

            self.assertEqual(result["overall"], "degraded")
            self.assertEqual(result["axes"]["review_queue"]["malformed_count"], 1)
            self.assertIn("quarantine-malformed-review", [item["action_id"] for item in result["recommended_actions"]])
            self.assertTrue(malformed.exists())

    def test_project_client_config_stays_unknown_without_real_tool_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = seed_ready_project(project)
            config = project / ".agents" / "mcp" / "agent-recipes.json"
            config.parent.mkdir(parents=True)
            config.write_text("{}\n", encoding="utf-8")

            result = core.readiness()

            self.assertEqual(result["overall"], "ready")
            self.assertEqual(result["axes"]["real_client"]["status"], "unknown")
            self.assertFalse(result["axes"]["real_client"]["fresh_tool_call_verified"])
            self.assertIn("verify-real-client", [item["action_id"] for item in result["recommended_actions"]])
            self.assertTrue(result["claim_status"]["missing_evidence"])

    def test_cli_and_mcp_match_core_and_expose_tool(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = {name: root / name for name in ("core", "cli", "mcp")}
            for project in projects.values():
                project.mkdir()
                seed_ready_project(project)

            core_result = RecipesProject(projects["core"]).readiness()
            cli_result = run_cli(projects["cli"], "readiness")
            mcp_result = call_tool("readiness", {"project": str(projects["mcp"])})

            self.assertIn("agent_recipes_readiness", [item["name"] for item in tool_list()])
            self.assertEqual(core_result, cli_result)
            self.assertEqual(core_result["overall"], mcp_result["overall"])
            self.assertEqual(core_result["axes"], mcp_result["axes"])
            self.assertEqual(core_result["recommended_action"], mcp_result["recommended_action"])
            self.assertEqual(mcp_result["tool"], "agent_recipes_readiness")


if __name__ == "__main__":
    unittest.main()
