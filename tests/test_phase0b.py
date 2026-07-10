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
from agent_recipes.mcp import call_tool, handle_request, tool_list


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


def seed_recipe(project: Path) -> tuple[str, str]:
    core = RecipesProject(project)
    core.init()
    core.capture("correction", "执行前必须 lookup 并 lock 菜谱。")
    compiled = core.compile()
    review_id = compiled["created"][0]["review_id"]
    accepted = core.accept_review(review_id)
    return accepted["recipe_id"], accepted["recipe_hash"]


class Phase0BTest(unittest.TestCase):
    def test_install_skill_dry_run_does_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            result = run_cli(
                project,
                "install-skill",
                "--agent",
                "codex",
                "--scope",
                "project",
                "--dry-run",
            )
            self.assertTrue(result["dry_run"])
            self.assertTrue(result["files_would_write"])
            self.assertFalse((project / ".agents").exists())
            self.assertIn("claim_status", result)
            self.assertTrue(result["claim_status"]["cannot_claim"])

    def test_mcp_tool_list_and_jsonrpc_call(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            RecipesProject(project).init()
            tools = tool_list()
            tool_names = [tool["name"] for tool in tools]
            self.assertIn("agent_recipes_doctor", tool_names)
            self.assertIn("agent_recipes_lookup", tool_names)
            self.assertIn("agent_recipes_lock", tool_names)
            self.assertIn("agent_recipes_capture", tool_names)
            self.assertIn("agent_recipes_capabilities", tool_names)
            self.assertIn("agent_recipes_refine", tool_names)
            self.assertIn("agent_recipes_patch_draft", tool_names)

            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "test-client", "version": "0.1.0"},
                    },
                },
                default_project=project,
            )
            self.assertEqual(response["id"], 1)
            self.assertEqual(response["result"]["serverInfo"]["name"], "agent-recipes")
            self.assertIn("tools", response["result"]["capabilities"])

            notification_response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                },
                default_project=project,
            )
            self.assertIsNone(notification_response)

            response = handle_request(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "agent_recipes_doctor", "arguments": {"project": str(project)}},
                },
                default_project=project,
            )
            self.assertEqual(response["id"], 2)
            self.assertEqual(response["result"]["structuredContent"]["tool"], "agent_recipes_doctor")
            self.assertEqual(response["result"]["structuredContent"]["status"], "ok")
            self.assertEqual(response["result"]["content"][0]["type"], "text")

    def test_mcp_stdio_debug_log_does_not_pollute_stdout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()
            debug_log = project / ".recipes" / "reports" / "mcp_stdio_debug.jsonl"
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "stdio-test", "version": "0.1.0"},
                    },
                },
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ]
            env = os.environ.copy()
            env["AGENT_RECIPES_MCP_DEBUG_LOG"] = str(debug_log)

            proc = subprocess.run(
                [sys.executable, "-m", "agent_recipes.cli", "mcp", "--stdio", "--project", str(project)],
                input="".join(json.dumps(item, ensure_ascii=False) + "\n" for item in requests),
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
                env=env,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            rows = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
            self.assertEqual([row.get("id") for row in rows], [1, 2])
            self.assertEqual(rows[1]["result"]["tools"][0]["name"], "agent_recipes_doctor")

            events = [json.loads(line) for line in debug_log.read_text(encoding="utf-8").splitlines()]
            self.assertIn("server_start", [event["event"] for event in events])
            server_start = [event for event in events if event["event"] == "server_start"][-1]
            self.assertNotIn("argv", server_start)
            self.assertNotIn("project", server_start)
            tools_list_responses = [
                event
                for event in events
                if event["event"] == "response" and event.get("method") == "tools/list"
            ]
            self.assertEqual(tools_list_responses[-1]["tool_count"], len(tool_list()))

    def test_mcp_stdio_non_object_json_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            RecipesProject(project).init()

            proc = subprocess.run(
                [sys.executable, "-m", "agent_recipes.cli", "mcp", "--stdio", "--project", str(project)],
                input="[]\n",
                cwd=REPO_ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            response = json.loads(proc.stdout)
            self.assertEqual(response["error"]["code"], -32600)

    def test_mcp_cli_tool_fallback_supports_expanded_tool_surface(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            RecipesProject(project).init()

            result = run_cli(project, "mcp", "--tool", "capabilities")

            self.assertEqual(result["tool"], "agent_recipes_capabilities")
            self.assertEqual(result["action"], "capabilities")
            self.assertIn("claim_status", result)

    def test_core_cli_mcp_parity_for_doctor_lookup_lock_capture(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = {
                "core": root / "core",
                "cli": root / "cli",
                "mcp": root / "mcp",
            }
            for project in projects.values():
                project.mkdir()
                recipe_id, recipe_hash = seed_recipe(project)
                self.assertTrue(recipe_id)
                self.assertTrue(recipe_hash)

            core_project = RecipesProject(projects["core"])
            core_lookup = core_project.lookup("lookup lock")
            core_lock = core_project.create_lock(core_lookup["recipe"]["recipe_id"], task="parity")
            core_capture = core_project.capture(
                "success",
                "adapter parity success",
                lock_id=core_lock["lock"]["lock_id"],
                idempotency_key="parity-success",
            )
            core_doctor = core_project.doctor()

            cli_lookup = run_cli(projects["cli"], "lookup", "lookup lock")
            cli_lock = run_cli(projects["cli"], "lock", "--recipe", cli_lookup["recipe"]["recipe_id"], "--task", "parity")
            cli_capture = run_cli(
                projects["cli"],
                "capture",
                "--type",
                "success",
                "--text",
                "adapter parity success",
                "--lock",
                cli_lock["lock"]["lock_id"],
                "--idempotency-key",
                "parity-success",
            )
            cli_doctor = run_cli(projects["cli"], "doctor")

            mcp_lookup = call_tool("lookup", {"project": str(projects["mcp"]), "query": "lookup lock"})
            mcp_lock = call_tool(
                "lock",
                {"project": str(projects["mcp"]), "recipe_id": mcp_lookup["recipe"]["recipe_id"], "task": "parity"},
            )
            mcp_capture = call_tool(
                "capture",
                {
                    "project": str(projects["mcp"]),
                    "capture_type": "success",
                    "text": "adapter parity success",
                    "lock_id": mcp_lock["lock"]["lock_id"],
                    "idempotency_key": "parity-success",
                },
            )
            mcp_doctor = call_tool("doctor", {"project": str(projects["mcp"])})

            self.assertEqual(core_lookup["recipe"]["recipe_id"], cli_lookup["recipe"]["recipe_id"])
            self.assertEqual(core_lookup["recipe"]["recipe_id"], mcp_lookup["recipe"]["recipe_id"])
            self.assertEqual(core_lookup["recipe"]["recipe_hash"], cli_lookup["recipe"]["recipe_hash"])
            self.assertEqual(core_lookup["recipe"]["recipe_hash"], mcp_lookup["recipe"]["recipe_hash"])

            self.assertEqual(core_lock["lock"]["lock_id"], cli_lock["lock"]["lock_id"])
            self.assertEqual(core_lock["lock"]["lock_id"], mcp_lock["lock"]["lock_id"])

            self.assertEqual(core_capture["idempotency_status"], cli_capture["idempotency_status"])
            self.assertEqual(core_capture["idempotency_status"], mcp_capture["idempotency_status"])
            self.assertEqual(core_capture["claim_status"], cli_capture["claim_status"])
            self.assertEqual(core_capture["claim_status"], mcp_capture["claim_status"])

            self.assertEqual(core_doctor["status"], cli_doctor["status"])
            self.assertEqual(core_doctor["status"], mcp_doctor["status"])
            self.assertEqual(core_doctor["errors"], cli_doctor["errors"])
            self.assertEqual(core_doctor["errors"], mcp_doctor["errors"])
            self.assertEqual(core_doctor["claim_status"], cli_doctor["claim_status"])
            self.assertEqual(core_doctor["claim_status"], mcp_doctor["claim_status"])

    def test_lookup_tie_prefers_higher_recipe_id_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = RecipesProject(project)
            core.init()
            recipes_dir = project / ".recipes" / "recipes"
            base_recipe = {
                "title": "SampleProject Sound BGM SFX Candidate Gate",
                "steps": ["BGM SFX 声音 候选规则"],
                "checklist_item": ["BGM SFX 声音 候选规则"],
                "version": 1,
            }
            for recipe_id in ("recipe_sample_project_sound_bgm_sfx_candidate_gate_v1", "recipe_sample_project_sound_bgm_sfx_candidate_gate_v2"):
                recipe = {**base_recipe, "recipe_id": recipe_id, "recipe_hash": recipe_id}
                (recipes_dir / f"{recipe_id}.json").write_text(json.dumps(recipe), encoding="utf-8")

            selected = core.lookup("SampleProject BGM SFX 声音 候选规则")["recipe"]

            self.assertEqual(selected["recipe_id"], "recipe_sample_project_sound_bgm_sfx_candidate_gate_v2")


if __name__ == "__main__":
    unittest.main()
