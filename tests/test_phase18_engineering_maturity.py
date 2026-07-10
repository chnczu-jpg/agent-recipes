from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, RecipesProject
from agent_recipes.mcp import call_tool, tool_list
from agent_recipes.migration import PROJECT_SCHEMA_VERSION
from agent_recipes.persistence import RecipesError as PersistenceRecipesError
from agent_recipes.release import open_source_readme


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}")
    return json.loads(proc.stdout or proc.stderr)


class Phase18EngineeringMaturityTest(unittest.TestCase):
    def test_persistence_boundary_keeps_core_error_compatibility(self) -> None:
        self.assertIs(RecipesError, PersistenceRecipesError)

    def test_fresh_init_writes_current_schema_and_doctor_reports_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = RecipesProject(Path(tmp))
            initialized = core.init()
            status = core.migration_status()
            doctor = core.doctor()

            self.assertTrue(initialized["ok"])
            self.assertEqual(status["schema"]["state"], "current")
            self.assertEqual(status["schema"]["installed_version"], PROJECT_SCHEMA_VERSION)
            self.assertEqual(doctor["status"], "ok")
            self.assertEqual(doctor["summary"]["project_schema"]["state"], "current")

    def test_legacy_migration_appends_without_rewriting_old_events_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = RecipesProject(Path(tmp))
            core.init()
            core.capture("correction", "legacy migration alpha beta")
            (core.recipes_dir / "project_schema.json").unlink()
            before_bytes = core.events_path.read_bytes()
            before_events = core.load_events()

            status = core.migration_status()
            migrated = core.migrate()
            after_events = core.load_events()
            repeated = core.migrate()

            self.assertEqual(status["schema"]["state"], "legacy_unversioned")
            self.assertTrue(core.events_path.read_bytes().startswith(before_bytes))
            self.assertEqual(after_events[:-1], before_events)
            self.assertEqual(after_events[-1]["event_type"], "project_schema_migrated")
            self.assertFalse(migrated["event_log_rewritten"])
            self.assertEqual(repeated["idempotency_status"], "unchanged")
            self.assertEqual(len(core.load_events()), len(after_events))

    def test_malformed_future_and_unsupported_migration_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = RecipesProject(Path(tmp))
            core.init()
            with self.assertRaises(RecipesError) as unsupported:
                core.migrate(target="0.9")
            self.assertEqual(unsupported.exception.code, "AR470")

            marker = core.recipes_dir / "project_schema.json"
            marker.write_text("{bad", encoding="utf-8")
            with self.assertRaises(RecipesError) as malformed:
                core.migrate()
            self.assertEqual(malformed.exception.code, "AR472")

            marker.write_text('{"schema_version":"99.0"}\n', encoding="utf-8")
            with self.assertRaises(RecipesError) as future:
                core.migrate()
            self.assertEqual(future.exception.code, "AR473")

    def test_cli_mcp_schema_contract_and_tool_exposure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cli = run_cli(project, "migration-status")
            mcp = call_tool("agent_recipes_migration_status", {"project": str(project)})
            names = {item["name"] for item in tool_list()}

            self.assertEqual(cli["schema"]["state"], mcp["schema"]["state"])
            self.assertIn("agent_recipes_migration_status", names)
            self.assertIn("agent_recipes_migrate", names)

    def test_packaging_metadata_and_ci_include_isolated_install(self) -> None:
        metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        smoke = REPO_ROOT / "bin" / "verify-clean-install"

        self.assertEqual(metadata["project"]["scripts"]["agent-recipes"], "agent_recipes.cli:main")
        self.assertEqual(metadata["project"]["dependencies"], [])
        self.assertIn("verify-clean-install", ci)
        self.assertTrue(smoke.exists())
        self.assertIn("migration-status", open_source_readme())
        self.assertIn("migrate --target 1.0", open_source_readme())

    def test_project_install_initializes_schema_but_does_not_auto_migrate_legacy_projects(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = Path(tmp) / "fresh"
            fresh.mkdir()
            installed = run_cli(fresh, "install-skill", "--agent", "codex", "--scope", "project")
            self.assertTrue(installed["ok"])
            self.assertEqual(RecipesProject(fresh).migration_status()["schema"]["state"], "current")

            legacy = Path(tmp) / "legacy"
            legacy_core = RecipesProject(legacy)
            legacy_core.init()
            (legacy_core.recipes_dir / "project_schema.json").unlink()
            run_cli(legacy, "install-skill", "--agent", "codex", "--scope", "project")
            self.assertEqual(legacy_core.migration_status()["schema"]["state"], "legacy_unversioned")


if __name__ == "__main__":
    unittest.main()
