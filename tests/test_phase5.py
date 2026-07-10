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


class Phase5ProductionizationTest(unittest.TestCase):
    def test_client_smoke_launches_installed_project_mcp_and_reports_claim_limits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "install-skill", "--agent", "codex", "--scope", "project")

            result = run_cli(project, "client-smoke", "--agent", "codex", "--scope", "project")

            self.assertEqual(result["action"], "client-smoke")
            self.assertEqual(result["agent"], "codex")
            self.assertEqual(result["scope"], "project")
            self.assertEqual(result["doctor_status"], "ok")
            self.assertIn("agent_recipes_doctor", result["tools"])
            self.assertIn("不能说真实 Codex/Claude/Hermes 客户端已经加载该配置。", result["claim_status"]["cannot_claim"])

    def test_client_smoke_missing_mcp_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)

            result = run_cli(
                project,
                "client-smoke",
                "--agent",
                "codex",
                "--scope",
                "project",
                expect_ok=False,
            )

            self.assertEqual(result["code"], "AR730")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])


if __name__ == "__main__":
    unittest.main()
