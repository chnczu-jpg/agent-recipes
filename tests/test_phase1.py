from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


class Phase1Test(unittest.TestCase):
    def test_ordinary_fixture_trial_creates_review_lock_failure_and_recover_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            seed_results = []
            for index in range(3):
                seed_results.append(
                    run_cli(
                        project,
                        "capture",
                        "--type",
                        "correction",
                        "--text",
                        f"Phase 1 ordinary seed correction {index + 1}: agent must preserve evidence before claim.",
                        "--idempotency-key",
                        f"phase1-ordinary-seed-{index + 1}",
                    )
                )
            self.assertEqual(len(seed_results), 3)

            compiled = run_cli(project, "compile", "--max-candidates", "2")
            self.assertEqual(len(compiled["created"]), 2)

            accepted = run_cli(project, "review", "--accept", compiled["created"][0]["review_id"])
            recipe_id = accepted["recipe_id"]
            locked = run_cli(project, "lock", "--recipe", recipe_id, "--task", "Phase 1 ordinary fixture")
            lock_id = locked["lock"]["lock_id"]

            for attempt in range(3):
                run_cli(
                    project,
                    "capture",
                    "--type",
                    "failure",
                    "--task",
                    "Phase 1 ordinary repeated failure",
                    "--text",
                    f"Repeat failure attempt {attempt + 1}",
                    "--lock",
                    lock_id,
                    "--idempotency-key",
                    f"phase1-ordinary-failure-{attempt + 1}",
                )
            recovered = run_cli(project, "recover", "--problem", "Phase 1 ordinary repeated failure")

            candidate_path = project / ".recipes" / "candidates" / f"{recovered['patch_id']}.json"
            review_path = project / ".recipes" / "review_queue" / f"{recovered['review_id']}.json"
            doctor = run_cli(project, "doctor")

            self.assertTrue(candidate_path.exists())
            self.assertTrue(review_path.exists())
            self.assertEqual(recovered["failure_count"], 3)
            self.assertEqual(recovered["target_recipe_id"], recipe_id)
            self.assertEqual(doctor["status"], "ok")

    def test_sample_project_slice_fixture_is_fieldized_without_hardcoding_sample_project_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source_path = project / "fixtures" / "sample_project_pip_source.md"
            source_path.parent.mkdir()
            source_path.write_text(
                "# SampleProject PIP slice fixture\n\n"
                "PIP 是信任锚点，不是第二主画面。小窗必须避开主动作，并用截图验收。\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            source = run_cli(project, "sources", "add", "fixtures/sample_project_pip_source.md", "--read-only")
            source_id = source["source"]["source_id"]
            run_cli(project, "scan", "--depth", "shallow")

            correction_text = f"""
SampleProject PIP placement slice.
source_truth_to_read:
- {source_id}
forbidden_path:
- 不要用旧坐标替代截图验收。
cannot_claim:
- 不能说 PIP 视觉质量通过，除非有截图验收。
visual_check:
- 小窗不挡主动作，且仍能看清主播表情。
stop_line: fresh agent 执行前必须先说明 PIP 不挡主动作，并声明不能只靠坐标 claim 通过。
"""
            run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                correction_text,
                "--idempotency-key",
                "phase1-4j-pip-correction",
            )
            compiled = run_cli(project, "compile", "--max-candidates", "1")
            accepted = run_cli(project, "review", "--accept", compiled["created"][0]["review_id"])
            locked = run_cli(
                project,
                "lock",
                "--recipe",
                accepted["recipe_id"],
                "--task",
                "Phase 1 SampleProject PIP fresh-agent preflight",
            )

            recipe_path = project / ".recipes" / "recipes" / f"{accepted['recipe_id']}.json"
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))

            self.assertIn(source_id, recipe["source_truth_to_read"])
            self.assertIn("不要用旧坐标替代截图验收。", recipe["forbidden_path"])
            self.assertIn("不能说 PIP 视觉质量通过，除非有截图验收。", recipe["cannot_claim"])
            self.assertIn("小窗不挡主动作", recipe["visual_check"][0])
            self.assertIn("fresh agent", recipe["stop_line"])
            self.assertIn("fresh agent", locked["lock"]["stop_lines"][0])
            self.assertEqual(run_cli(project, "doctor")["status"], "ok")


if __name__ == "__main__":
    unittest.main()
