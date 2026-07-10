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


class Phase5DependencyLockTest(unittest.TestCase):
    def test_system_lock_writes_binary_report_and_doctor_summary(self) -> None:
        result = run_cli(REPO_ROOT, "system-lock")

        report_path = REPO_ROOT / ".recipes" / "reports" / "system_runtime_lock.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        doctor = run_cli(REPO_ROOT, "doctor")
        names = {item["name"] for item in result["binaries"]}

        self.assertEqual(result["action"], "system-lock")
        self.assertTrue(report_path.exists())
        self.assertIn("python3", names)
        self.assertIn("sqlite3", names)
        self.assertEqual(report["lock_hash"], result["lock_hash"])
        self.assertEqual(doctor["summary"]["system_runtime_lock"]["lock_hash"], result["lock_hash"])
        self.assertIn("不能说这些系统二进制已在另一台机器复现。", result["claim_status"]["cannot_claim"])

    def test_adapter_lock_fails_closed_without_project_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            result = run_cli(project, "adapter-lock", expect_ok=False)

            self.assertEqual(result["code"], "AR750")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])

    @unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
    def test_adapter_lock_writes_pinned_lockfile_and_runtime_report(self) -> None:
        result = run_cli(REPO_ROOT, "adapter-lock")

        lock_path = REPO_ROOT / "requirements-phase2-adapters.lock.txt"
        report_path = REPO_ROOT / ".recipes" / "reports" / "adapter_runtime_lock.json"
        lock_text = lock_path.read_text(encoding="utf-8")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        doctor = run_cli(REPO_ROOT, "doctor")

        self.assertEqual(result["action"], "adapter-lock")
        self.assertTrue(result["package_count"] > 50)
        self.assertIn("faster-whisper==", lock_text)
        self.assertIn("docling==", lock_text)
        self.assertNotIn("zep-cloud==", lock_text)
        self.assertTrue(any(item.startswith("zep-cloud==") for item in report["excluded_out_of_scope_packages"]))
        self.assertEqual(report["lock_hash"], result["lock_hash"])
        self.assertEqual(doctor["summary"]["adapter_runtime_lock"]["lock_hash"], result["lock_hash"])
        self.assertEqual(report["missing_direct_requirements"], [])
        self.assertIn("不能说这些版本已在另一台机器复现安装过。", result["claim_status"]["cannot_claim"])


if __name__ == "__main__":
    unittest.main()
