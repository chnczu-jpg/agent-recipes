from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesProject


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True, env: dict[str, str] | None = None) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False, env=env)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


class Phase5ClientConfigTest(unittest.TestCase):
    def test_bin_entrypoint_runs_with_minimal_path_python(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()
            run_cli(project, "capture", "--type", "correction", "--text", "执行前必须 lookup 并 lock。")
            compiled = run_cli(project, "compile")
            accepted = run_cli(project, "review", "--accept", compiled["created"][0]["review_id"])
            run_cli(project, "lock", "--recipe", accepted["recipe_id"], "--task", "minimal path smoke")

            env = {
                "HOME": str(Path.home()),
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            cmd = [
                str(REPO_ROOT / "bin" / "agent-recipes"),
                "mcp",
                "--tool",
                "doctor",
                "--project",
                str(project),
                "--json",
            ]
            proc = subprocess.run(cmd, cwd=Path("/"), text=True, capture_output=True, check=False, env=env)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            result = json.loads(proc.stdout)
            self.assertEqual(result["tool"], "agent_recipes_doctor")
            self.assertEqual(result["status"], "ok")

    def test_install_client_codex_writes_config_backup_and_smokes_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()
            config = Path(tmp) / "codex-config.toml"
            config.write_text('[mcp_servers.existing]\ncommand = "existing"\nargs = []\n', encoding="utf-8")

            result = run_cli(project, "install-client", "--agent", "codex", "--config-path", str(config))

            text = config.read_text(encoding="utf-8")
            self.assertEqual(result["action"], "install-client")
            self.assertEqual(result["agent"], "codex")
            self.assertEqual(result["smoke"]["doctor_status"], "ok")
            self.assertEqual(result["mcp_command"][0], "/usr/bin/python3")
            self.assertEqual(Path(result["mcp_command"][1]).name, "agent-recipes")
            self.assertTrue(Path(result["backup_path"]).exists())
            self.assertIn("[mcp_servers.agent_recipes]", text)
            self.assertIn("[mcp_servers.agent_recipes.env]", text)
            self.assertIn('PYTHONDONTWRITEBYTECODE = "1"', text)
            self.assertIn("AGENT_RECIPES_MCP_DEBUG_LOG", text)
            self.assertIn(str(project), text)
            self.assertNotIn("\\u", text)
            self.assertIn("不能说真实 Codex/Claude/Hermes 客户端已经重新加载该配置。", result["claim_status"]["cannot_claim"])

            run_cli(project, "install-client", "--agent", "codex", "--config-path", str(config))
            rewritten = config.read_text(encoding="utf-8")
            self.assertEqual(rewritten.count("# BEGIN agent-recipes managed MCP"), 1)
            self.assertEqual(rewritten.count("# END agent-recipes managed MCP"), 1)

    def test_install_client_claude_updates_mcp_json_and_smokes_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()
            config = Path(tmp) / ".mcp.json"
            config.write_text('{"mcpServers":{"existing":{"command":"existing","args":[]}}}\n', encoding="utf-8")

            result = run_cli(project, "install-client", "--agent", "claude", "--config-path", str(config))

            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(result["agent"], "claude")
            self.assertEqual(result["smoke"]["doctor_status"], "ok")
            self.assertTrue(Path(result["backup_path"]).exists())
            self.assertIn("agent-recipes", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["agent-recipes"]["command"], "/usr/bin/python3")
            self.assertEqual(Path(data["mcpServers"]["agent-recipes"]["args"][0]).name, "agent-recipes")
            self.assertEqual(data["mcpServers"]["agent-recipes"]["env"]["PYTHONDONTWRITEBYTECODE"], "1")
            self.assertTrue(data["mcpServers"]["agent-recipes"]["env"]["AGENT_RECIPES_MCP_DEBUG_LOG"].endswith("mcp_stdio_debug.jsonl"))

    def test_install_client_claude_default_writes_user_claude_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()
            home = Path(tmp) / "home"
            home.mkdir()
            env = {**dict(os.environ), "HOME": str(home)}

            result = run_cli(project, "install-client", "--agent", "claude", env=env)

            config = home / ".claude.json"
            legacy = home / ".claude" / ".mcp.json"
            data = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(result["config_path"], str(config))
            self.assertFalse(legacy.exists())
            self.assertIn("agent-recipes", data["mcpServers"])
            self.assertEqual(data["mcpServers"]["agent-recipes"]["command"], "/usr/bin/python3")
            self.assertEqual(Path(data["mcpServers"]["agent-recipes"]["args"][0]).name, "agent-recipes")

    def test_install_client_hermes_updates_config_yaml_and_smokes_mcp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()
            config = Path(tmp) / "config.yaml"
            config.write_text(
                'model:\n  default: test\nmcp_servers:\n  existing:\n    command: "npx"\n    args: ["existing"]\n',
                encoding="utf-8",
            )

            result = run_cli(project, "install-client", "--agent", "hermes", "--config-path", str(config))

            text = config.read_text(encoding="utf-8")
            self.assertEqual(result["agent"], "hermes")
            self.assertEqual(result["smoke"]["doctor_status"], "ok")
            self.assertTrue(Path(result["backup_path"]).exists())
            self.assertIn("mcp_servers:", text)
            self.assertIn("  existing:", text)
            self.assertIn("  agent_recipes:", text)
            self.assertIn("    connect_timeout: 60", text)
            self.assertIn("PYTHONDONTWRITEBYTECODE", text)
            self.assertIn("AGENT_RECIPES_MCP_DEBUG_LOG", text)
            self.assertIn('command: "/usr/bin/python3"', text)
            self.assertIn("      PATH:", text)

            run_cli(project, "install-client", "--agent", "hermes", "--config-path", str(config))
            rewritten = config.read_text(encoding="utf-8")
            self.assertEqual(rewritten.count("  agent_recipes:"), 1)

    def test_install_client_hermes_fails_closed_for_non_map_mcp_servers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()
            config = Path(tmp) / "config.yaml"
            config.write_text("mcp_servers: []\n", encoding="utf-8")

            result = run_cli(project, "install-client", "--agent", "hermes", "--config-path", str(config), expect_ok=False)

            self.assertEqual(result["code"], "AR768")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])


if __name__ == "__main__":
    unittest.main()
