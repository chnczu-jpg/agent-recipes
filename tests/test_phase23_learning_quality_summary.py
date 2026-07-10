from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_recipes.learning_quality import evaluate_learning_quality
from agent_recipes.mcp import TOOL_NAMES, call_tool


ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def seed_learning_project(
    projects_root: Path,
    name: str,
    *,
    family: str,
    complete_card: bool = True,
    quality_passed: bool = True,
) -> str:
    recipes_dir = projects_root / name / ".recipes"
    review_id = f"review_{name}"
    target_recipe_id = f"recipe_{name}"
    write_json(
        recipes_dir / "reports" / f"self_run_{name}.json",
        {
            "action": "self-run-benchmark",
            "ok": True,
            "checked_at": "2026-07-10T01:00:00Z",
            "target_recipe_id": target_recipe_id,
            "review_id": review_id,
            "cases": [
                {"case_id": "cards_have_source_trace_and_claim_limits", "status": "passed"},
                {"case_id": "no_direct_formal_recipe_write", "status": "passed"},
            ],
        },
    )
    card = {
        "card_id": f"card_{name}",
        "source_trace": [{"path": f"sources/{name}.md", "record_id": f"chunk_{name}"}] if complete_card else [],
        "target_fields": ["checklist_item"],
        "evidence_strength": "candidate",
        "cannot_claim": ["不能说候选已经转正。"],
    }
    write_json(recipes_dir / "source_refinery" / "cards" / "learning_atom_cards" / f"card_{name}.json", card)
    write_json(recipes_dir / "source_refinery" / "patch_drafts" / f"patch_draft_{name}.json", {"target_recipe_id": target_recipe_id})
    write_json(
        recipes_dir / "review_queue" / f"{review_id}.json",
        {
            "review_id": review_id,
            "target_recipe_id": target_recipe_id,
            "status": "accepted",
            "decided_at": "2026-07-10T01:30:00Z",
        },
    )
    write_json(
        recipes_dir / "reports" / f"candidate_quality_{name}.json",
        {
            "action": "candidate-quality-benchmark",
            "ok": quality_passed,
            "checked_at": "2026-07-10T02:00:00Z",
            "cases_path": f"candidate_quality_{name}.json",
            "cases": [
                {
                    "case_id": f"quality_{name}",
                    "review_id": review_id,
                    "status": "passed" if quality_passed else "failed",
                }
            ],
        },
    )
    return family


def cohort_doc(projects: list[tuple[str, str]]) -> dict[str, object]:
    return {
        "cohort_id": "learning_quality_test_v1",
        "required_families": sorted({family for _, family in projects}),
        "projects": [{"name": name, "family": family} for name, family in projects],
        "thresholds": {
            "min_projects": len(projects),
            "min_latest_self_run_targets": len(projects),
            "min_cards": len(projects),
            "min_accepted_review_quality_coverage": 1.0,
        },
    }


class Phase23LearningQualitySummaryTest(unittest.TestCase):
    def test_independent_evaluator_passes_complete_multifamily_cohort(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects_root = root / "pressure"
            projects = [
                ("correction_batch", seed_learning_project(projects_root, "correction_batch", family="correction")),
                ("course_batch", seed_learning_project(projects_root, "course_batch", family="course")),
                ("artifact_batch", seed_learning_project(projects_root, "artifact_batch", family="artifact")),
            ]

            report = evaluate_learning_quality(projects_root, cohort_doc(projects))

            self.assertTrue(report["ok"])
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["summary"]["card_contract_rate"], 1.0)
            self.assertEqual(report["summary"]["accepted_review_quality_coverage"], 1.0)
            self.assertEqual(report["summary"]["latest_self_run_target_failure_count"], 0)
            self.assertEqual(report["missing_required_families"], [])

    def test_evaluator_fails_closed_on_missing_trace_and_unbenchmarked_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects_root = Path(tmp) / "pressure"
            projects = [
                (
                    "unsafe_batch",
                    seed_learning_project(
                        projects_root,
                        "unsafe_batch",
                        family="correction",
                        complete_card=False,
                        quality_passed=False,
                    ),
                )
            ]

            report = evaluate_learning_quality(projects_root, cohort_doc(projects))

            self.assertFalse(report["ok"])
            self.assertEqual(report["status"], "failed")
            gate_ids = {gate["gate_id"] for gate in report["gates"] if gate["status"] == "failed"}
            self.assertIn("all_cards_have_candidate_contract", gate_ids)
            self.assertIn("accepted_reviews_have_passed_quality_evidence", gate_ids)
            self.assertEqual(report["summary"]["accepted_review_quality_gap_count"], 1)

    def test_only_latest_accepted_version_per_target_must_have_quality_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects_root = Path(tmp) / "pressure"
            family = seed_learning_project(projects_root, "versioned_batch", family="course", quality_passed=False)
            recipes_dir = projects_root / "versioned_batch" / ".recipes"
            write_json(
                recipes_dir / "review_queue" / "review_version2.json",
                {
                    "review_id": "review_version2",
                    "target_recipe_id": "recipe_versioned_batch",
                    "status": "accepted",
                    "decided_at": "2026-07-10T03:00:00Z",
                },
            )
            write_json(
                recipes_dir / "reports" / "candidate_quality_version2.json",
                {
                    "action": "candidate-quality-benchmark",
                    "ok": True,
                    "checked_at": "2026-07-10T03:10:00Z",
                    "cases_path": "candidate_quality_version2.json",
                    "cases": [{"case_id": "version2", "review_id": "review_version2", "status": "passed"}],
                },
            )

            report = evaluate_learning_quality(projects_root, cohort_doc([("versioned_batch", family)]))

            self.assertTrue(report["ok"])
            self.assertEqual(report["summary"]["accepted_review_count"], 1)
            self.assertEqual(report["summary"]["accepted_review_quality_gap_count"], 0)

    def test_cli_and_mcp_write_only_summary_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(
                [sys.executable, "-m", "agent_recipes.cli", "init", "--project", str(root), "--json"],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            projects_root = root / "pressure"
            family = seed_learning_project(projects_root, "course_batch", family="course")
            cohort_path = root / "cohort.json"
            write_json(cohort_path, cohort_doc([("course_batch", family)]))

            before_recipes = sorted((root / ".recipes" / "recipes").glob("*.json"))
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "agent_recipes.cli",
                    "learning-quality-summary",
                    "--projects-root",
                    str(projects_root),
                    "--cohort",
                    str(cohort_path),
                    "--project",
                    str(root),
                    "--json",
                ],
                cwd=ROOT,
                check=True,
                capture_output=True,
                text=True,
            )
            result = json.loads(completed.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "learning-quality-summary")
            self.assertTrue(Path(result["report_path"]).exists())
            self.assertEqual(before_recipes, sorted((root / ".recipes" / "recipes").glob("*.json")))

            self.assertIn("learning_quality_summary", TOOL_NAMES)
            mcp = call_tool(
                "agent_recipes_learning_quality_summary",
                {"projects_root": str(projects_root), "cohort": str(cohort_path)},
                project=root,
            )
            self.assertTrue(mcp["ok"])
            self.assertEqual(mcp["tool"], "agent_recipes_learning_quality_summary")


if __name__ == "__main__":
    unittest.main()
