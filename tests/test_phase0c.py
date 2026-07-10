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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def seed_locked_recipe(project: Path) -> tuple[str, str]:
    run_cli(project, "init")
    run_cli(project, "capture", "--type", "correction", "--text", "执行前必须 lookup 并 lock 菜谱。")
    compiled = run_cli(project, "compile")
    accepted = run_cli(project, "review", "--accept", compiled["created"][0]["review_id"])
    locked = run_cli(project, "lock", "--recipe", accepted["recipe_id"], "--task", "Phase 0C failure")
    return accepted["recipe_id"], locked["lock"]["lock_id"]


class Phase0CTest(unittest.TestCase):
    def test_scan_registered_text_source_creates_source_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "fixtures" / "notes.md"
            source.parent.mkdir()
            source.write_text(
                "# Notes\n\n执行前必须读 source truth。\n\n第二段资料用于 scan chunk。\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/notes.md", "--read-only")

            result = run_cli(project, "scan", "--depth", "shallow")

            chunks_path = project / ".recipes" / "source_index" / "chunks.jsonl"
            index_path = project / ".recipes" / "source_index" / "INDEX.md"
            self.assertEqual(result["action"], "scan")
            self.assertGreaterEqual(result["chunks_indexed"], 1)
            self.assertTrue(chunks_path.exists())
            self.assertTrue(index_path.exists())
            self.assertGreaterEqual(len(read_jsonl(chunks_path)), 1)
            self.assertIn("不能说已覆盖全部历史资料。", result["claim_status"]["cannot_claim"])

    def test_recover_third_failure_creates_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            recipe_id, lock_id = seed_locked_recipe(project)

            for attempt in range(2):
                run_cli(
                    project,
                    "capture",
                    "--type",
                    "failure",
                    "--task",
                    "同类失败阈值测试",
                    "--text",
                    f"第 {attempt + 1} 次失败，应该还不能 recover。",
                    "--lock",
                    lock_id,
                    "--idempotency-key",
                    f"failure-before-threshold-{attempt}",
                )
            below_threshold = run_cli(project, "recover", "--problem", "同类失败阈值测试", expect_ok=False)
            self.assertEqual(below_threshold["code"], "AR431")

            run_cli(
                project,
                "capture",
                "--type",
                "failure",
                "--task",
                "同类失败阈值测试",
                "--text",
                "第 3 次失败，只能生成候选补丁。",
                "--lock",
                lock_id,
                "--idempotency-key",
                "failure-at-threshold",
            )
            result = run_cli(project, "recover", "--problem", "同类失败阈值测试")

            candidate_path = project / ".recipes" / "candidates" / f"{result['patch_id']}.json"
            review_path = project / ".recipes" / "review_queue" / f"{result['review_id']}.json"
            recipe_path = project / ".recipes" / "recipes" / f"{recipe_id}.json"
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
            review = json.loads(review_path.read_text(encoding="utf-8"))

            self.assertEqual(result["failure_count"], 3)
            self.assertTrue(candidate_path.exists())
            self.assertTrue(review_path.exists())
            self.assertEqual(review["status"], "pending")
            self.assertEqual(recipe["version"], 1)
            self.assertEqual(result["target_recipe_id"], recipe_id)
            self.assertIn("不能说正式 recipe 已被修改。", result["claim_status"]["cannot_claim"])

    def test_ingest_video_transcript_creates_video_index(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            transcript = project / "fixtures" / "lesson.srt"
            transcript.parent.mkdir()
            transcript.write_text(
                "1\n00:00:01,000 --> 00:00:03,000\n第一句课程内容。\n\n"
                "2\n00:00:04,000 --> 00:00:06,000\n第二句课程内容。\n",
                encoding="utf-8",
            )
            run_cli(project, "init")

            result = run_cli(project, "ingest-video", "--transcript", "fixtures/lesson.srt")

            video_dir = project / ".recipes" / "video_index" / result["course_id"]
            chunks_path = video_dir / "chunks.jsonl"
            vtt_path = video_dir / "transcript.vtt"
            self.assertTrue(chunks_path.exists())
            self.assertTrue(vtt_path.exists())
            self.assertEqual(len(read_jsonl(chunks_path)), 2)
            self.assertIn("WEBVTT", vtt_path.read_text(encoding="utf-8"))
            self.assertIn("不能说已完成 ASR 或云端转写。", result["claim_status"]["cannot_claim"])


if __name__ == "__main__":
    unittest.main()
