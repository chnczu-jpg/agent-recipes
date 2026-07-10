from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import (
    RecipesError,
    RecipesProject,
    build_execution_evidence_pack,
    write_json,
)
from agent_recipes.mcp import call_tool, tool_list


REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_SECRET = "sk-" + "abcdefghijklmnopqrstuvwxyz123456"


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


def seed_recipe_and_lock(project: Path, *, task: str = "evidence hardening") -> tuple[RecipesProject, dict[str, Any], dict[str, Any]]:
    core = RecipesProject(project)
    core.init()
    core.capture("correction", "evidence hardening alpha beta exact recipe")
    compiled = core.compile()
    accepted = core.accept_review(compiled["created"][0]["review_id"])
    recipe = core.load_recipe(accepted["recipe_id"])
    lock = core.create_lock(recipe["recipe_id"], task=task)["lock"]
    return core, recipe, lock


class Phase17EvidenceHardeningTest(unittest.TestCase):
    def test_capture_redacts_secret_before_event_hash_and_candidate_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = RecipesProject(project)
            core.init()

            result = core.capture("correction", f"never persist this credential {FAKE_SECRET}")
            raw_events = core.events_path.read_text(encoding="utf-8")
            capture_path = core.recipes_dir / "corrections" / f"{result['event_id']}.json"
            capture_text = capture_path.read_text(encoding="utf-8")
            capture_event = core.load_events()[-1]

            self.assertNotIn(FAKE_SECRET, raw_events)
            self.assertNotIn(FAKE_SECRET, capture_text)
            self.assertIn("[REDACTED:sk_token]", capture_text)
            self.assertTrue(capture_event["payload"]["persistence_redaction"]["applied"])
            self.assertEqual(core.event_hash(capture_event), capture_event["event_hash"])
            self.assertEqual(core.doctor()["status"], "ok")

    def test_authoritative_recipe_and_lock_writes_fail_closed_on_secret(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = RecipesProject(Path(tmp))
            core.init()
            recipe_path = core.recipes_dir / "recipes" / "recipe_secret.json"
            lock_path = core.recipes_dir / "locks" / "lock_secret.json"

            with self.assertRaises(RecipesError) as recipe_error:
                write_json(recipe_path, {"recipe_id": "recipe_secret", "api_key": FAKE_SECRET})
            with self.assertRaises(RecipesError) as lock_error:
                write_json(lock_path, {"lock_id": "lock_secret", "text": FAKE_SECRET})

            self.assertEqual(recipe_error.exception.code, "AR450")
            self.assertEqual(lock_error.exception.code, "AR450")
            self.assertFalse(recipe_path.exists())
            self.assertFalse(lock_path.exists())

    def test_quarantine_preserves_trace_redacts_secret_and_requires_repaired_release(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core = RecipesProject(project)
            core.init()
            bad_path = core.recipes_dir / "review_queue" / "review_bad.json"
            bad_path.write_text(f'{{"review_id":"review_bad","secret":"{FAKE_SECRET}"', encoding="utf-8")

            status = core.evidence_quarantine(action="status")
            applied = core.evidence_quarantine(action="apply")
            manifest = applied["quarantined"][0]
            payload_path = project / manifest["payload_relative_path"]

            self.assertEqual(status["issue_count"], 1)
            self.assertFalse(bad_path.exists())
            self.assertTrue(payload_path.exists())
            self.assertNotIn(FAKE_SECRET, payload_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["original_hash"], status["issues"][0]["file_hash"])
            self.assertFalse(manifest["formal_use_allowed"])
            with self.assertRaises(RecipesError) as unrepaired:
                core.evidence_quarantine(action="release", quarantine_id=manifest["quarantine_id"])
            self.assertEqual(unrepaired.exception.code, "AR455")

            write_json(
                payload_path,
                {
                    "review_id": "review_bad",
                    "status": "pending",
                    "question": "human repaired candidate",
                    "recommendation": "reject",
                },
            )
            released = core.evidence_quarantine(action="release", quarantine_id=manifest["quarantine_id"])
            after = core.evidence_quarantine(action="status")

            self.assertTrue(bad_path.exists())
            self.assertEqual(released["manifest"]["status"], "released")
            self.assertTrue(released["manifest"]["formal_use_allowed"])
            self.assertEqual(after["active_quarantine_count"], 0)
            self.assertEqual(after["released_quarantine_count"], 1)
            self.assertFalse(list((core.recipes_dir / "recipes").glob("*.json")))

    def test_evidence_pack_is_lock_bound_budgeted_private_and_explicit_about_omissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            core, recipe, lock = seed_recipe_and_lock(project)
            candidate_dir = core.recipes_dir / "evidence"
            write_json(
                candidate_dir / "large_candidate.json",
                {
                    "target_recipe_id": recipe["recipe_id"],
                    "privacy_class": "project_local",
                    "source_trace": [{"source_id": "large"}],
                    "cannot_claim": ["candidate only"],
                    "body": "x" * 12000,
                },
            )
            write_json(
                candidate_dir / "private_candidate.json",
                {
                    "target_recipe_id": recipe["recipe_id"],
                    "privacy_class": "private_only",
                    "source_trace": [{"source_id": "private"}],
                    "cannot_claim": ["private candidate only"],
                    "body": "private details",
                },
            )
            preview = build_execution_evidence_pack(
                project,
                core.recipes_dir,
                lock,
                core.load_events(),
                max_bytes=100000,
                privacy="project_local",
            )
            mandatory_bytes = sum(
                item["bytes"] for item in preview["included"] if item["record_type"] != "candidate_evidence"
            )
            budget = max(1024, mandatory_bytes + 256)
            recipe_before = (core.recipes_dir / "recipes" / f"{recipe['recipe_id']}.json").read_bytes()

            result = core.execution_evidence_pack(lock["lock_id"], max_bytes=budget, privacy="project_local")
            replay = core.execution_evidence_pack(lock["lock_id"], max_bytes=budget, privacy="project_local")
            manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
            context_text = Path(result["context_path"]).read_text(encoding="utf-8")
            reasons = {item["reason"] for item in manifest["omissions"]}

            self.assertEqual(manifest["lock_id"], lock["lock_id"])
            self.assertLessEqual(manifest["used_bytes"], manifest["max_bytes"])
            self.assertIn("budget_exceeded", reasons)
            self.assertIn("privacy_policy", reasons)
            self.assertTrue(any(item["record_type"] == "execution_lock" for item in manifest["included"]))
            self.assertTrue(any(item["record_type"] == "formal_recipe" for item in manifest["included"]))
            self.assertNotIn("private details", context_text)
            self.assertEqual(manifest["formal_recipe_written"], False)
            self.assertEqual(replay["idempotency_status"], "unchanged")
            self.assertEqual(replay["pack"]["pack_id"], manifest["pack_id"])
            self.assertEqual((core.recipes_dir / "recipes" / f"{recipe['recipe_id']}.json").read_bytes(), recipe_before)

    def test_cli_mcp_expose_quarantine_and_evidence_pack(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_project = root / "cli"
            mcp_project = root / "mcp"
            cli_project.mkdir()
            mcp_project.mkdir()
            _, _, cli_lock = seed_recipe_and_lock(cli_project, task="adapter parity")
            _, _, mcp_lock = seed_recipe_and_lock(mcp_project, task="adapter parity")

            cli_status = run_cli(cli_project, "evidence-quarantine", "--action", "status")
            mcp_status = call_tool("evidence_quarantine", {"project": str(mcp_project), "action": "status"})
            cli_pack = run_cli(
                cli_project,
                "evidence-pack",
                "--lock",
                cli_lock["lock_id"],
                "--max-bytes",
                "65536",
                "--privacy",
                "minimal",
            )
            mcp_pack = call_tool(
                "evidence_pack",
                {
                    "project": str(mcp_project),
                    "lock_id": mcp_lock["lock_id"],
                    "max_bytes": 65536,
                    "privacy": "minimal",
                },
            )
            names = [item["name"] for item in tool_list()]

            self.assertIn("agent_recipes_evidence_quarantine", names)
            self.assertIn("agent_recipes_evidence_pack", names)
            self.assertEqual(cli_status["issue_count"], mcp_status["issue_count"])
            self.assertEqual(cli_pack["pack"]["included_count"], mcp_pack["pack"]["included_count"])
            self.assertEqual(cli_pack["pack"]["omitted_count"], mcp_pack["pack"]["omitted_count"])
            self.assertEqual(mcp_pack["tool"], "agent_recipes_evidence_pack")


if __name__ == "__main__":
    unittest.main()
