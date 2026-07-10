from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesProject, claim_status


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


class Phase3Test(unittest.TestCase):
    def test_install_skill_project_scope_writes_local_skill_and_mcp_config_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            result = run_cli(project, "install-skill", "--agent", "codex", "--scope", "project")

            skill = project / ".agents" / "skills" / "agent-recipes" / "SKILL.md"
            source_indexer = project / ".agents" / "skills" / "source-to-recipe-indexer" / "SKILL.md"
            mcp = project / ".agents" / "mcp" / "agent-recipes.json"

            self.assertEqual(result["action"], "install-skill")
            self.assertTrue(skill.exists())
            self.assertTrue(source_indexer.exists())
            self.assertTrue(mcp.exists())
            self.assertTrue((project / ".recipes" / "events.jsonl").exists())
            self.assertIn("不能说真实 Codex/Claude/Hermes 客户端已经加载该配置。", result["claim_status"]["cannot_claim"])

            config = json.loads(mcp.read_text(encoding="utf-8"))
            server = config["mcpServers"]["agent-recipes"]
            self.assertEqual(server["command"], "/usr/bin/python3")
            self.assertEqual(Path(server["args"][0]).name, "agent-recipes")
            self.assertEqual(server["args"][1:4], ["mcp", "--stdio", "--project"])

    def test_install_skill_upgrade_is_not_blocked_by_legacy_idempotency_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            recipes = RecipesProject(project)
            old_payload = {
                "agent": "codex",
                "scope": "project",
                "skill_path": str(project / ".agents" / "skills" / "agent-recipes" / "SKILL.md"),
                "source_indexer_path": str(project / ".agents" / "skills" / "source-to-recipe-indexer" / "SKILL.md"),
                "mcp_path": str(project / ".agents" / "mcp" / "agent-recipes.json"),
                "mcp_command": [str(project / "bin" / "agent-recipes"), "mcp", "--stdio", "--project", str(project)],
            }
            recipes.append_event(
                "skill_installed",
                old_payload,
                idempotency_key=f"install-skill:codex:project:{project.resolve()}",
                lock_exempt_reason="legacy_fixture",
                claim_status=claim_status(verified=["legacy install fixture"]),
            )

            result = run_cli(project, "install-skill", "--agent", "codex", "--scope", "project")

            self.assertTrue(result["ok"])
            config = json.loads((project / ".agents" / "mcp" / "agent-recipes.json").read_text(encoding="utf-8"))
            server = config["mcpServers"]["agent-recipes"]
            self.assertEqual(server["command"], "/usr/bin/python3")
            self.assertEqual(Path(server["args"][0]).name, "agent-recipes")


if __name__ == "__main__":
    unittest.main()
