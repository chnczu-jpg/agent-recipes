from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent_recipes.core import write_json


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


class Phase0ATest(unittest.TestCase):
    def test_concurrent_write_json_uses_unique_temp_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"

            def write_one(index: int) -> None:
                write_json(path, {"index": index})

            with ThreadPoolExecutor(max_workers=12) as pool:
                list(pool.map(write_one, range(60)))

            result = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("index", result)
            self.assertFalse(list(path.parent.glob(".state.json.*.tmp")))

    def test_phase0a_command_chain(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture = project / "fixtures" / "corrections.md"
            fixture.parent.mkdir()
            fixture.write_text("纠偏：执行前必须 lookup 并 lock 菜谱。\n", encoding="utf-8")

            init = run_cli(project, "init")
            self.assertEqual(init["idempotency_status"], "created")

            source = run_cli(project, "sources", "add", "fixtures/corrections.md", "--read-only")
            self.assertEqual(source["idempotency_status"], "created")
            self.assertIn("source", source)

            correction = run_cli(project, "capture", "--type", "correction", "--text", "执行前必须 lookup 并 lock 菜谱。")
            self.assertEqual(correction["idempotency_status"], "created")
            self.assertEqual(correction["capture_type"], "correction")

            compile_result = run_cli(project, "compile")
            self.assertEqual(compile_result["idempotency_status"], "created")
            review_id = compile_result["created"][0]["review_id"]

            accepted = run_cli(project, "review", "--accept", review_id)
            self.assertEqual(accepted["idempotency_status"], "created")
            recipe_id = accepted["recipe_id"]

            lookup = run_cli(project, "lookup", "执行前 lookup lock")
            self.assertEqual(lookup["recipe"]["recipe_id"], recipe_id)

            lock = run_cli(project, "lock", "--recipe", recipe_id, "--task", "同类任务")
            self.assertEqual(lock["idempotency_status"], "created")
            lock_id = lock["lock"]["lock_id"]

            success = run_cli(project, "capture", "--type", "success", "--text", "按菜谱执行成功。", "--lock", lock_id)
            self.assertEqual(success["idempotency_status"], "created")

            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["status"], "ok")
            self.assertIn("claim_status", doctor)
            self.assertTrue(doctor["claim_status"]["verified"])
            self.assertTrue(doctor["claim_status"]["cannot_claim"])
            self.assertIn(
                "仅凭 doctor 不能说 MCP 已注册到真实 Codex/Claude/Hermes 客户端；真实客户端加载必须另有 fresh client/tool-call 证据。",
                doctor["claim_status"]["cannot_claim"],
            )

    def test_capture_idempotency_replay_and_conflict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            first = run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                "同一个 key 第一次写入。",
                "--idempotency-key",
                "same-key",
            )
            self.assertEqual(first["idempotency_status"], "created")

            replay = run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                "同一个 key 第一次写入。",
                "--idempotency-key",
                "same-key",
            )
            self.assertEqual(replay["idempotency_status"], "replayed")

            conflict = run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                "同一个 key 但不同 payload。",
                "--idempotency-key",
                "same-key",
                expect_ok=False,
            )
            self.assertEqual(conflict["code"], "AR409")

    def test_locked_correction_compiles_into_bound_recipe_instead_of_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            first = run_cli(project, "capture", "--type", "correction", "--text", "PIP 必须读取保存位置。")
            compiled = run_cli(project, "compile")
            accepted = run_cli(project, "review", "--accept", compiled["created"][0]["review_id"])
            recipe_id = accepted["recipe_id"]
            lock = run_cli(project, "lock", "--recipe", recipe_id, "--task", "修正 PIP 位置")

            correction = run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                "找不到用户确认的精确坐标时必须停止，不能用默认模板覆盖。",
                "--lock",
                lock["lock"]["lock_id"],
            )
            self.assertEqual(correction["recipe_bindings"][0]["recipe_id"], recipe_id)

            targeted = run_cli(project, "compile")
            self.assertEqual(targeted["created"][0]["recipe_id"], recipe_id)
            patch_path = project / ".recipes" / "candidates" / f"{targeted['created'][0]['patch_id']}.json"
            patch = json.loads(patch_path.read_text(encoding="utf-8"))
            self.assertEqual(patch["target_recipe_id"], recipe_id)
            self.assertEqual(patch["proposed_change"]["recipe_id"], recipe_id)
            self.assertIn("定向候选补丁", patch["reason"])
            self.assertIn("找不到用户确认的精确坐标", patch["proposed_change"]["steps"][0])
            self.assertEqual(patch["proposed_change"]["verified_path"], [])
            formal = json.loads((project / ".recipes" / "recipes" / f"{recipe_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(formal["version"], 1)

    def test_locked_correction_rejects_unknown_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli(
                Path(tmp),
                "capture",
                "--type",
                "correction",
                "--text",
                "不能覆盖人工位置。",
                "--lock",
                "lock_missing",
                expect_ok=False,
            )
            self.assertEqual(result["code"], "AR410")

    def test_parallel_append_event_keeps_event_chain_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            start = project / "start_parallel_append"
            worker = """
import sys
import time
from pathlib import Path
from agent_recipes.core import RecipesProject

project = Path(sys.argv[1])
index = int(sys.argv[2])
start = Path(sys.argv[3])
while not start.exists():
    time.sleep(0.001)
RecipesProject(project).append_event(
    "parallel_event",
    {"index": index},
    idempotency_key=f"parallel:{index}",
    lock_exempt_reason="parallel_append_test",
)
"""
            procs = [
                subprocess.Popen(
                    [sys.executable, "-c", worker, str(project), str(index), str(start)],
                    cwd=REPO_ROOT,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                )
                for index in range(24)
            ]
            start.write_text("go", encoding="utf-8")
            for proc in procs:
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, f"stdout={stdout}\nstderr={stderr}")

            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["status"], "ok")
            events = [
                json.loads(line)
                for line in (project / ".recipes" / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([event["seq"] for event in events], list(range(1, 26)))

    def test_success_capture_requires_active_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            result = run_cli(
                project,
                "capture",
                "--type",
                "success",
                "--text",
                "不带 lock 不能成功。",
                expect_ok=False,
            )
            self.assertEqual(result["code"], "AR410")

    def test_event_log_chain_detects_tampering(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            run_cli(project, "capture", "--type", "correction", "--text", "事件链必须能发现篡改。")

            events_path = project / ".recipes" / "events.jsonl"
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
            events[1]["prev_event_hash"] = "tampered"
            events_path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events) + "\n",
                encoding="utf-8",
            )

            doctor = run_cli(project, "doctor", expect_ok=False)
            self.assertEqual(doctor["status"], "error")
            self.assertTrue(any(item["code"] in {"AR301", "AR302"} for item in doctor["errors"]))

    def test_lock_hash_mismatch_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            run_cli(project, "capture", "--type", "correction", "--text", "锁定后 recipe hash 不能静默变化。")
            compiled = run_cli(project, "compile")
            review_id = compiled["created"][0]["review_id"]
            accepted = run_cli(project, "review", "--accept", review_id)
            recipe_id = accepted["recipe_id"]
            locked = run_cli(project, "lock", "--recipe", recipe_id)
            lock_id = locked["lock"]["lock_id"]

            recipe_path = project / ".recipes" / "recipes" / f"{recipe_id}.json"
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
            recipe["recipe_hash"] = "tampered"
            recipe_path.write_text(json.dumps(recipe, ensure_ascii=False, indent=2), encoding="utf-8")

            result = run_cli(
                project,
                "capture",
                "--type",
                "success",
                "--text",
                "hash mismatch 应该 fail closed。",
                "--lock",
                lock_id,
                expect_ok=False,
            )
            self.assertEqual(result["code"], "AR411")

            doctor = run_cli(project, "doctor", expect_ok=False)
            self.assertEqual(doctor["status"], "error")
            self.assertTrue(any(item["code"] == "AR305" for item in doctor["errors"]))


if __name__ == "__main__":
    unittest.main()
