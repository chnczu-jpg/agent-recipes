from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

from agent_recipes.core import RecipesError, RecipesProject, SCHEMA_VERSION
from agent_recipes.ledger import EVENT_SCHEMA_VERSION, EventLedger


REPO_ROOT = Path(__file__).resolve().parents[1]


class Phase19LedgerAndLightnessTest(unittest.TestCase):
    def test_ledger_import_is_independent_and_core_keeps_compatibility(self) -> None:
        code = """
import sys
from agent_recipes.ledger import EventLedger
assert 'agent_recipes.core' not in sys.modules
assert EventLedger.__module__ == 'agent_recipes.ledger'
"""
        proc = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(SCHEMA_VERSION, EVENT_SCHEMA_VERSION)

    def test_direct_ledger_interface_preserves_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            events_path = Path(tmp) / ".recipes" / "events.jsonl"

            def ensure_storage() -> None:
                events_path.parent.mkdir(parents=True, exist_ok=True)
                events_path.touch(exist_ok=True)

            ledger = EventLedger(events_path, ensure_storage)
            first, first_status = ledger.append(
                "direct_test",
                {"value": 1},
                idempotency_key="direct-key",
                lock_exempt_reason="direct_ledger_test",
            )
            replay, replay_status = ledger.append(
                "direct_test",
                {"value": 1},
                idempotency_key="direct-key",
                lock_exempt_reason="direct_ledger_test",
            )

            self.assertEqual(first_status, "created")
            self.assertEqual(replay_status, "replayed")
            self.assertEqual(first, replay)
            self.assertTrue(ledger.inspect()["ok"])
            with self.assertRaises(RecipesError) as conflict:
                ledger.append(
                    "direct_test",
                    {"value": 2},
                    idempotency_key="direct-key",
                    lock_exempt_reason="direct_ledger_test",
                )
            self.assertEqual(conflict.exception.code, "AR409")

    def test_tampered_ledger_blocks_future_mutation_without_changing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = RecipesProject(Path(tmp))
            core.init()
            events = core.load_events()
            events[0]["payload"]["phase"] = "tampered"
            core.events_path.write_text(
                "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events) + "\n",
                encoding="utf-8",
            )
            before = core.events_path.read_bytes()

            with self.assertRaises(RecipesError) as blocked:
                core.capture("correction", "must not append after tamper")
            doctor = core.doctor()

            self.assertEqual(blocked.exception.code, "AR405")
            self.assertEqual(core.events_path.read_bytes(), before)
            self.assertEqual(doctor["status"], "error")
            self.assertTrue(any(item["code"] == "AR302" for item in doctor["errors"]))

    def test_malformed_ledger_is_reported_and_blocks_future_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            core = RecipesProject(Path(tmp))
            core.init()
            with core.events_path.open("a", encoding="utf-8") as handle:
                handle.write("{broken\n")
            before = core.events_path.read_bytes()

            doctor = core.doctor()
            with self.assertRaises(RecipesError) as blocked:
                core.capture("correction", "must not append after malformed row")

            self.assertEqual(doctor["status"], "error")
            self.assertTrue(any(item["code"] == "AR299" for item in doctor["errors"]))
            self.assertEqual(blocked.exception.code, "AR405")
            self.assertEqual(core.events_path.read_bytes(), before)

    def test_lightweight_budgets_are_executable_ratchets(self) -> None:
        budget = json.loads((REPO_ROOT / "ENGINEERING_BUDGET.json").read_text(encoding="utf-8"))
        metadata = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        core_lines = len((REPO_ROOT / "agent_recipes" / "core.py").read_text(encoding="utf-8").splitlines())
        package_bytes = sum(path.stat().st_size for path in (REPO_ROOT / "agent_recipes").glob("*.py"))

        self.assertLessEqual(core_lines, budget["core_max_lines"])
        self.assertLessEqual(package_bytes, budget["package_python_max_bytes"])
        self.assertEqual(len(metadata["project"]["dependencies"]), budget["runtime_dependency_count"])
        self.assertEqual(budget["required_external_service_count"], 0)
        self.assertLessEqual(budget["wheel_max_bytes"], 262144)


if __name__ == "__main__":
    unittest.main()

