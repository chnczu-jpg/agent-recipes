from __future__ import annotations

import json
import shutil
import subprocess
import sys
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


class Phase4Test(unittest.TestCase):
    def test_open_source_audit_and_export_exclude_runtime_state_and_sanitize_project_specific_text(self) -> None:
        export_dir = REPO_ROOT / "dist" / "test-open-source-export"
        shutil.rmtree(export_dir, ignore_errors=True)
        try:
            result = run_cli(REPO_ROOT, "open-source-export", "--output-dir", str(export_dir))

            self.assertEqual(result["action"], "open-source-export")
            self.assertTrue((export_dir / "OPEN_SOURCE_MANIFEST.json").exists())
            self.assertTrue((export_dir / "README.md").exists())
            self.assertTrue((export_dir / "LICENSE").exists())
            self.assertTrue((export_dir / "CONTRIBUTING.md").exists())
            self.assertTrue((export_dir / "SECURITY.md").exists())
            self.assertFalse((export_dir / ".recipes").exists())
            self.assertFalse((export_dir / ".agents").exists())
            self.assertFalse((export_dir / ".venv").exists())
            self.assertFalse((export_dir / "PROJECT_STATUS.md").exists())
            self.assertFalse((export_dir / "fixtures").exists())

            text = "\n".join(
                path.read_text(encoding="utf-8", errors="replace")
                for path in export_dir.rglob("*")
                if path.is_file() and path.suffix in {".md", ".py", ".txt", ".json", ""}
            )
            private_home = "/" + "Users" + "/" + "bei" + "bei"
            project_marker = "4" + "J"
            lower_project_prefix = "4" + "j" + "_"
            lower_project_suffix = "_" + "4" + "j"
            self.assertNotIn(private_home, text)
            self.assertNotIn(project_marker, text)
            self.assertNotIn(lower_project_prefix, text)
            self.assertNotIn(lower_project_suffix, text)
            self.assertIn("SampleProject", text)
            readme = (export_dir / "README.md").read_text(encoding="utf-8")
            self.assertIn("Stop teaching your agents the same lesson twice", readme)
            self.assertIn("strict no-match policy", readme.lower())
            self.assertIn("Agent Recipes is an early public release", readme)
            self.assertIn("install-client --agent codex", readme)
            self.assertIn("install-client --agent claude", readme)
            self.assertIn("install-client --agent hermes", readme)
            self.assertIn("~/.claude.json", readme)
            self.assertIn("claude mcp list", readme)
            self.assertIn("hermes mcp test agent_recipes", readme)
            self.assertIn("MIT License", (export_dir / "LICENSE").read_text(encoding="utf-8"))
            lockfile = export_dir / "requirements-phase2-adapters.lock.txt"
            if lockfile.exists():
                self.assertIn("neo4j==", lockfile.read_text(encoding="utf-8"))
            self.assertIn("不能说已经发布到 GitHub 或插件市场。", result["claim_status"]["cannot_claim"])
        finally:
            shutil.rmtree(export_dir, ignore_errors=True)

    def test_open_source_export_output_must_stay_inside_project(self) -> None:
        result = run_cli(REPO_ROOT, "open-source-export", "--output-dir", "/tmp/agent-recipes-export", expect_ok=False)

        self.assertEqual(result["code"], "AR702")
        self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])


if __name__ == "__main__":
    unittest.main()
