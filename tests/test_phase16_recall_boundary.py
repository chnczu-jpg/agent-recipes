from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, RecipesProject, write_json
from agent_recipes.mcp import call_tool, tool_list


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, no_site: bool = False, expect_ok: bool = True) -> dict[str, Any]:
    command = [sys.executable]
    if no_site:
        command.append("-S")
    command.extend(["-m", "agent_recipes.cli", *args, "--project", str(project), "--json"])
    env = os.environ.copy()
    if no_site:
        env["PYTHONNOUSERSITE"] = "1"
        for key in list(env):
            if key.startswith("AGENT_RECIPES_") or key.startswith("COGNEE_"):
                env.pop(key, None)
    proc = subprocess.run(command, cwd=REPO_ROOT, text=True, capture_output=True, check=False, env=env)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(command)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(command)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


def seed_patch_draft(core: RecipesProject) -> None:
    write_json(
        core.recipes_dir / "source_refinery" / "patch_drafts" / "patch_boundary.json",
        {
            "patch_draft_id": "patch_boundary",
            "target_recipe_id": "recipe_boundary_candidate_v0",
            "reason": "review queue candidate evidence must stay candidate only",
            "target_fields": ["checklist_item", "cannot_claim"],
            "proposed_additions": {
                "checklist_item": ["send recalled evidence through human review"],
                "cannot_claim": ["cannot claim recall wrote a formal recipe"],
            },
            "source_card_ids": ["card_boundary"],
            "cannot_claim": ["candidate patch only"],
        },
    )


class Phase16RecallBoundaryTest(unittest.TestCase):
    def test_core_chain_runs_with_python_no_site_and_all_recall_adapters_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            init = run_cli(project, "init", no_site=True)
            correction = run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                "core independent alpha beta recipe",
                no_site=True,
            )
            compiled = run_cli(project, "compile", no_site=True)
            review_id = compiled["created"][0]["review_id"]
            accepted = run_cli(project, "review", "--accept", review_id, no_site=True)
            lookup = run_cli(project, "lookup", "core independent alpha beta recipe", "--strict", no_site=True)
            lock = run_cli(
                project,
                "lock",
                "--recipe",
                accepted["recipe_id"],
                "--task",
                "no-site core chain",
                no_site=True,
            )
            success = run_cli(
                project,
                "capture",
                "--type",
                "success",
                "--text",
                "core chain passed without site packages",
                "--lock",
                lock["lock"]["lock_id"],
                no_site=True,
            )
            doctor = run_cli(project, "doctor", no_site=True)
            readiness = run_cli(project, "readiness", no_site=True)
            boundary = run_cli(project, "recall-boundary", no_site=True)

            self.assertTrue(init["ok"] and correction["ok"] and success["ok"])
            self.assertEqual(lookup["recipe"]["recipe_id"], accepted["recipe_id"])
            self.assertEqual(doctor["status"], "ok")
            self.assertEqual(readiness["overall"], "ready")
            self.assertEqual(readiness["realized_mode"], "core_only")
            self.assertEqual(readiness["axes"]["optional_adapters"]["status"], "disabled")
            self.assertEqual(readiness["axes"]["recall_boundary"]["status"], "ready")
            self.assertTrue(boundary["all_adapters_disabled"])
            self.assertEqual(boundary["realized_mode"], "core_only")
            self.assertFalse(boundary["contract"]["core_requires_recall_adapter"])

    def test_graphiti_and_qwen_outputs_stay_candidate_and_do_not_mutate_core_truth(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = RecipesProject(project)
            core.init()
            seed_patch_draft(core)
            replay = project / "embedding-replay.json"
            query_replay = project / "query-replay.json"
            write_json(replay, {"default_embedding": [1.0, 0.0]})
            write_json(query_replay, {"embedding": [1.0, 0.0]})
            recipes_before = list((core.recipes_dir / "recipes").glob("*.json"))
            outcomes_before = core.outcome_status()["summary"]["attributable_outcome_count"]

            graph_index = core.memory_index(adapter="graphiti")
            graph_search = core.memory_search("review queue candidate", adapter="graphiti")
            core.embedding_configure(model="qwen3-embedding:0.6b", dimensions=2)
            qwen_index = core.embedding_index(response_json=str(replay))
            qwen_search = core.embedding_search("review queue", response_json=str(query_replay))
            boundary = core.recall_boundary_status()

            self.assertTrue(graph_index["runtime_verified"])
            self.assertTrue(graph_search["results"])
            self.assertTrue(qwen_index["candidate_only"])
            self.assertTrue(qwen_search["results"])
            for item in [*graph_search["results"], *qwen_search["results"]]:
                self.assertEqual(item["evidence_status"], "candidate")
                self.assertTrue(item["source_trace"])
                self.assertTrue(item["cannot_claim"])
            self.assertTrue(boundary["ok"])
            self.assertEqual(boundary["violation_count"], 0)
            self.assertEqual(boundary["adapters"]["graphiti"]["status"], "active")
            self.assertEqual(boundary["adapters"]["qwen3"]["status"], "active")
            self.assertEqual(list((core.recipes_dir / "recipes").glob("*.json")), recipes_before)
            self.assertEqual(core.outcome_status()["summary"]["attributable_outcome_count"], outcomes_before)
            self.assertFalse(boundary["contract"]["can_write_formal_recipe"])
            self.assertFalse(boundary["contract"]["can_create_execution_lock"])
            self.assertFalse(boundary["contract"]["can_change_outcome_confidence"])

    def test_broken_recall_index_degrades_adapter_but_core_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = RecipesProject(project)
            core.init()
            broken = core.recipes_dir / "memory" / "cognee" / "index.jsonl"
            broken.parent.mkdir(parents=True, exist_ok=True)
            broken.write_text("{not-json\n", encoding="utf-8")

            boundary = core.recall_boundary_status()
            with self.assertRaises(RecipesError) as caught:
                core.memory_search("anything", adapter="cognee")
            core.capture("correction", "broken recall must not block core recipe")
            compiled = core.compile()
            accepted = core.accept_review(compiled["created"][0]["review_id"])
            doctor = core.doctor()
            readiness = core.readiness()

            self.assertFalse(boundary["ok"])
            self.assertEqual(boundary["status"], "degraded")
            self.assertGreater(boundary["violation_count"], 0)
            self.assertEqual(caught.exception.code, "AR342")
            self.assertTrue(accepted["recipe_id"])
            self.assertEqual(doctor["status"], "warn")
            self.assertFalse(doctor["errors"])
            self.assertTrue(any(item["code"] == "AR314" for item in doctor["warnings"]))
            self.assertEqual(readiness["axes"]["recall_boundary"]["status"], "degraded")
            self.assertFalse(readiness["axes"]["recall_boundary"]["affects_core_readiness"])
            self.assertEqual(readiness["overall"], "ready")

    def test_cli_and_mcp_expose_same_recall_boundary_contract(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_project = root / "cli"
            mcp_project = root / "mcp"
            cli_project.mkdir()
            mcp_project.mkdir()
            RecipesProject(cli_project).init()
            RecipesProject(mcp_project).init()

            cli_result = run_cli(cli_project, "recall-boundary")
            mcp_result = call_tool("recall_boundary", {"project": str(mcp_project)})

            self.assertIn("agent_recipes_recall_boundary", [item["name"] for item in tool_list()])
            self.assertEqual(cli_result["contract"], mcp_result["contract"])
            self.assertEqual(cli_result["adapters"], mcp_result["adapters"])
            self.assertEqual(cli_result["realized_mode"], mcp_result["realized_mode"])
            self.assertEqual(mcp_result["tool"], "agent_recipes_recall_boundary")


if __name__ == "__main__":
    unittest.main()
