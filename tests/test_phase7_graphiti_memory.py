from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.mcp import call_tool, tool_list


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly succeeded: {' '.join(cmd)}\nstdout={proc.stdout}")
    return json.loads(proc.stdout or proc.stderr)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def seed_graph_candidates(project: Path) -> None:
    fixture = project / "fixtures" / "graphiti_cards.md"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "\n\n".join(
            [
                "card_type: failure_card\n"
                "failed_path: direct recipe write from recalled memory\n"
                "failure_signal: formal recipe changed without review accept\n"
                "replacement_path: create candidate evidence and review queue item\n"
                "cannot_claim: cannot say graph candidate is verified",
                "card_type: correction_card\n"
                "before: graph memory result was treated as truth\n"
                "correction: graph memory result must stay candidate-only\n"
                "after: patch draft waits for review accept\n"
                "cannot_claim: cannot say graphiti wrote a formal recipe",
                "card_type: success_path_card\n"
                "success_path: source trace plus review queue kept recipe truth safe\n"
                "checklist_item: require source_trace and cannot_claim on graph edges",
            ]
        ),
        encoding="utf-8",
    )
    run_cli(project, "init")
    run_cli(project, "sources", "add", "fixtures/graphiti_cards.md", "--read-only")
    run_cli(project, "scan", "--depth", "shallow")
    refined = run_cli(
        project,
        "refine",
        "--query",
        "graph memory failure correction success path review",
        "--knowledge-need",
        "KN_GRAPHITI_RELATION_MEMORY",
        "--target-recipe",
        "recipe_graphiti_memory_v0",
        "--candidate-fields",
        "failed_path,failure_signal,replacement_path,checklist_item,cannot_claim",
        "--limit",
        "6",
    )
    run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
    run_cli(project, "patch-draft", "--target-recipe", "recipe_graphiti_memory_v0")


class Phase7GraphitiMemoryTest(unittest.TestCase):
    def test_graphiti_memory_index_search_status_and_doctor_are_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_graph_candidates(project)

            indexed = run_cli(project, "memory-index", "--adapter", "graphiti")

            self.assertEqual(indexed["action"], "memory-index")
            self.assertEqual(indexed["adapter"], "graphiti")
            self.assertEqual(indexed["evidence_strength"], "candidate")
            self.assertGreater(indexed["node_count"], 0)
            self.assertGreater(indexed["edge_count"], 0)
            self.assertFalse(indexed["native_runtime_verified"])
            self.assertIn("不能说 Graphiti graph candidate 已经修改正式 recipe。", indexed["claim_status"]["cannot_claim"])

            nodes_path = project / ".recipes" / "memory" / "graphiti" / "nodes.jsonl"
            edges_path = project / ".recipes" / "memory" / "graphiti" / "edges.jsonl"
            nodes = read_jsonl(nodes_path)
            edges = read_jsonl(edges_path)
            self.assertEqual(len(nodes), indexed["node_count"])
            self.assertEqual(len(edges), indexed["edge_count"])
            self.assertTrue(all(row["adapter"] == "graphiti" for row in nodes))
            self.assertTrue(all(row["evidence_status"] == "candidate" for row in nodes))
            self.assertTrue(all(row["source_trace"] for row in nodes))
            self.assertTrue(any(edge["relation_type"] == "derived_from_card" for edge in edges))
            self.assertTrue(any(edge["relation_type"] == "targets_recipe" for edge in edges))

            search = run_cli(project, "memory-search", "direct recipe write review", "--adapter", "graphiti", "--limit", "5")

            self.assertEqual(search["action"], "memory-search")
            self.assertEqual(search["adapter"], "graphiti")
            self.assertTrue(search["results"])
            self.assertTrue(all(item["evidence_status"] == "candidate" for item in search["results"]))
            self.assertIn("不能说 graph search 结果已经验证。", search["claim_status"]["cannot_claim"])

            status = run_cli(project, "memory-status", "--adapter", "graphiti")

            self.assertEqual(status["action"], "memory-status")
            self.assertEqual(status["adapters"]["graphiti"]["node_count"], len(nodes))
            self.assertEqual(status["adapters"]["graphiti"]["edge_count"], len(edges))
            self.assertTrue(status["adapters"]["graphiti"]["candidate_only"])
            self.assertFalse(status["adapters"]["graphiti"]["native_runtime_verified"])

            doctor = run_cli(project, "doctor")

            self.assertTrue(doctor["summary"]["memory_adapters"]["graphiti"]["runtime_verified"])
            self.assertEqual(doctor["summary"]["memory_adapters"]["graphiti"]["edge_count"], len(edges))
            self.assertIn("不能说 Graphiti graph candidate 已经自动进入正式 recipe。", doctor["claim_status"]["cannot_claim"])
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_graphiti_memory_v0.json").exists())

    def test_graphiti_memory_index_fails_closed_without_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            result = run_cli(project, "memory-index", "--adapter", "graphiti", expect_ok=False)

            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "AR350")
            self.assertFalse((project / ".recipes" / "memory" / "graphiti" / "status.json").exists())

    def test_mcp_exposes_graphiti_memory_adapter(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_memory_index", tool_names)
        self.assertIn("agent_recipes_memory_search", tool_names)
        self.assertIn("agent_recipes_memory_status", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_graph_candidates(project)

            indexed = call_tool("memory_index", {"project": str(project), "adapter": "graphiti"})

            self.assertEqual(indexed["tool"], "agent_recipes_memory_index")
            self.assertEqual(indexed["action"], "memory-index")
            self.assertEqual(indexed["adapter"], "graphiti")
            self.assertGreater(indexed["edge_count"], 0)

            status = call_tool("memory_status", {"project": str(project), "adapter": "graphiti"})

            self.assertEqual(status["tool"], "agent_recipes_memory_status")
            self.assertEqual(status["adapters"]["graphiti"]["adapter"], "graphiti")
            self.assertTrue(status["adapters"]["graphiti"]["candidate_only"])

    @unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
    def test_graphiti_native_probe_is_caged_local_and_claim_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
            run_cli(project, "init")

            probe = run_cli(project, "memory-native-probe", "--adapter", "graphiti", "--timeout", "30")

            self.assertEqual(probe["action"], "memory-native-probe")
            self.assertEqual(probe["adapter_name"], "graphiti")
            self.assertTrue(probe["candidate_only"])
            self.assertEqual(probe["native_status"], "available")
            self.assertTrue(probe["runtime"]["runtime_verified"])
            self.assertTrue(probe["runtime"]["network_blocked"])
            self.assertEqual(probe["runtime"]["network_attempts"], [])
            self.assertTrue(probe["runtime"]["paths_caged"])
            self.assertEqual(probe["runtime"]["env"]["GRAPHITI_TELEMETRY_ENABLED"], "false")
            self.assertEqual(probe["runtime"]["env"]["PYTHON_DOTENV_DISABLED"], "1")
            self.assertEqual(probe["runtime"]["driver"], "kuzu")
            self.assertIn("schema_built", probe["runtime"]["steps"])
            self.assertIn("node_write_read", probe["runtime"]["steps"])
            self.assertFalse(probe["runtime"]["llm_network_used"])
            self.assertIn("不能说 Graphiti native probe 已证明生产级长期记忆。", probe["claim_status"]["cannot_claim"])

            native_path = project / ".recipes" / "memory" / "graphiti" / "native_probe.json"
            self.assertTrue(native_path.exists())
            native = json.loads(native_path.read_text(encoding="utf-8"))
            self.assertEqual(native["native_status"], "available")
            runtime_root = (project / ".recipes" / "memory" / "graphiti" / "runtime").resolve()
            Path(native["runtime_root"]).resolve().relative_to(runtime_root)

            status = run_cli(project, "memory-status", "--adapter", "graphiti")
            self.assertEqual(status["adapters"]["graphiti"]["native_status"], "available")
            self.assertTrue(status["adapters"]["graphiti"]["native_runtime_verified"])

            doctor = run_cli(project, "doctor")
            self.assertIn("不能说 Graphiti native probe 已经完成生产级长期记忆或图谱质量验证。", doctor["claim_status"]["cannot_claim"])

    def test_mcp_exposes_graphiti_native_probe_and_zep_is_out_of_scope(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_memory_native_probe", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            probe = call_tool("memory_native_probe", {"project": str(project), "adapter": "graphiti", "timeout": 1})

            self.assertEqual(probe["tool"], "agent_recipes_memory_native_probe")
            self.assertEqual(probe["action"], "memory-native-probe")
            self.assertEqual(probe["adapter_name"], "graphiti")
            self.assertIn(probe["native_status"], {"available", "unavailable"})

            capabilities = run_cli(project, "capabilities")

            self.assertTrue(capabilities["adapter_runtime"]["zep"]["out_of_scope"])
            self.assertFalse(capabilities["adapter_runtime"]["zep"]["runtime_verified"])
            self.assertIn("用户已明确废弃 Zep 运行闭环。", capabilities["adapter_runtime"]["zep"]["notes"])

            doctor = run_cli(project, "doctor")

            self.assertTrue(doctor["summary"]["external_adapters"]["zep"]["out_of_scope"])
            self.assertNotIn("Zep runtime loop is still pending.", json.dumps(doctor, ensure_ascii=False))
