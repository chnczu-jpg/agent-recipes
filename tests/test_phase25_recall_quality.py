from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from agent_recipes.mcp import call_tool, tool_list


ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    command = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if expect_ok and proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(command)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    if not expect_ok and proc.returncode == 0:
        raise AssertionError(f"command unexpectedly passed: {' '.join(command)}")
    return json.loads(proc.stdout or proc.stderr)


def seed_project(project: Path) -> None:
    run_cli(project, "init")
    recipes_dir = project / ".recipes" / "recipes"
    recipes = [
        {
            "recipe_id": "recipe_alpha_v1",
            "version": 1,
            "recipe_hash": "alpha-hash",
            "title": "ALPHA portrait crop",
            "use_when": ["ALPHA crop subject"],
            "steps": ["apply ALPHA portrait crop"],
        },
        {
            "recipe_id": "recipe_beta_v1",
            "version": 1,
            "recipe_hash": "beta-hash",
            "title": "BETA audio timing",
            "use_when": ["BETA audio cue"],
            "steps": ["align BETA audio cue"],
        },
    ]
    for recipe in recipes:
        (recipes_dir / f"{recipe['recipe_id']}.json").write_text(
            json.dumps(recipe, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    cases = {
        "cases": [
            {
                "case_id": "positive_alpha",
                "query": "ALPHA portrait crop subject",
                "expect_applicable": True,
                "expected_recipe_id": "recipe_alpha_v1",
            },
            {
                "case_id": "positive_beta",
                "query": "BETA audio timing cue",
                "expect_applicable": True,
                "expected_recipe_id": "recipe_beta_v1",
            },
            {
                "case_id": "negative_tax",
                "query": "tax payroll bank transfer",
                "expect_applicable": False,
            },
            {
                "case_id": "negative_complete_workflow",
                "query": "完整后期包装全流程 大字 花字 字幕 转场 声音 一次性保证质量通过",
                "expect_applicable": False,
            },
        ]
    }
    (project / "recall_cases.json").write_text(json.dumps(cases, ensure_ascii=False, indent=2), encoding="utf-8")


class EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        text = str(payload.get("input") or "").casefold()
        if "alpha" in text:
            vector = [1.0, 0.0, 0.0]
        elif "beta" in text:
            vector = [0.0, 1.0, 0.0]
        else:
            vector = [-1.0, -1.0, 0.0]
        body = json.dumps({"data": [{"embedding": vector}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


class Phase25RecallQualityTest(unittest.TestCase):
    def test_same_corpus_projection_scores_core_cognee_and_graphiti_without_recipe_writes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_project(project)
            before = {path.name: path.read_bytes() for path in (project / ".recipes" / "recipes").glob("*.json")}

            result = run_cli(
                project,
                "recall-quality-benchmark",
                "--cases",
                "recall_cases.json",
                "--backends",
                "core,cognee,graphiti",
            )

            self.assertTrue(result["same_corpus"])
            self.assertEqual(result["corpus"]["recipe_count"], 2)
            self.assertEqual(result["case_cohort"]["case_count"], 4)
            self.assertTrue(result["backends"]["core"]["gate"]["passed"])
            self.assertTrue(result["backends"]["cognee"]["projection_only"])
            self.assertTrue(result["backends"]["graphiti"]["projection_only"])
            self.assertEqual(result["backends"]["core"]["metrics"]["pure_no_match_correct"], 2)
            after = {path.name: path.read_bytes() for path in (project / ".recipes" / "recipes").glob("*.json")}
            self.assertEqual(before, after)

    def test_qwen_requires_current_loopback_and_scores_live_queries(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_project(project)
            blocked = run_cli(
                project,
                "recall-quality-benchmark",
                "--cases",
                "recall_cases.json",
                "--backends",
                "qwen",
                expect_ok=False,
            )
            self.assertEqual(blocked["code"], "AR704")

            server = ThreadingHTTPServer(("127.0.0.1", 0), EmbeddingHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                endpoint = f"http://127.0.0.1:{server.server_port}/v1/embeddings"
                run_cli(
                    project,
                    "embedding-configure",
                    "--model",
                    "qwen3-embedding:0.6b",
                    "--endpoint",
                    endpoint,
                    "--dimensions",
                    "3",
                )
                result = run_cli(
                    project,
                    "recall-quality-benchmark",
                    "--cases",
                    "recall_cases.json",
                    "--backends",
                    "qwen",
                    "--allow-loopback",
                    "--qwen-min-score",
                    "0.5",
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

            self.assertTrue(result["backends"]["qwen"]["gate"]["passed"])
            self.assertEqual(result["backends"]["qwen"]["runtime"]["live_query_calls"], 4)
            self.assertFalse(result["backends"]["qwen"]["runtime"]["corpus_cache_hit"])
            self.assertEqual(result["backends"]["qwen"]["metrics"]["false_recall_count"], 0)

    def test_missing_expected_recipe_fails_closed_and_mcp_exposes_tool(self) -> None:
        names = {item["name"] for item in tool_list()}
        self.assertIn("agent_recipes_recall_quality_benchmark", names)
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_project(project)
            cases = json.loads((project / "recall_cases.json").read_text(encoding="utf-8"))
            cases["cases"][0]["expected_recipe_id"] = "recipe_missing_v1"
            (project / "bad_cases.json").write_text(json.dumps(cases), encoding="utf-8")

            result = call_tool(
                "recall_quality_benchmark",
                {
                    "project": str(project),
                    "cases": "bad_cases.json",
                    "backends": ["core"],
                },
            )

            self.assertEqual(result["code"], "AR703")
            self.assertFalse(result["ok"])

    def test_candidate_search_keeps_evidence_but_suppresses_broad_recommendation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_project(project)
            broad_query = "完整后期包装全流程 大字 花字 字幕 转场 声音 一次性保证质量通过"
            record = {
                "record_id": "memory_alpha",
                "target_recipe_id": "recipe_alpha_v1",
                "text": broad_query,
                "source_kind": "card",
            }
            cognee_dir = project / ".recipes" / "memory" / "cognee"
            cognee_dir.mkdir(parents=True, exist_ok=True)
            (cognee_dir / "index.jsonl").write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

            memory = run_cli(project, "memory-search", broad_query, "--adapter", "cognee")

            self.assertTrue(memory["results"])
            self.assertEqual(memory["recommendation_status"], "no_match")
            self.assertIsNone(memory["recommended_result"])

            embedding_dir = project / ".recipes" / "embeddings" / "qwen3"
            embedding_dir.mkdir(parents=True, exist_ok=True)
            embedded = {**record, "embedding": [1.0, 0.0, 0.0]}
            (embedding_dir / "index.jsonl").write_text(json.dumps(embedded, ensure_ascii=False) + "\n", encoding="utf-8")
            run_cli(
                project,
                "embedding-configure",
                "--model",
                "qwen3-embedding:0.6b",
                "--endpoint",
                "http://127.0.0.1:18080/v1/embeddings",
                "--dimensions",
                "3",
            )
            (project / "query_vector.json").write_text(json.dumps({"embedding": [1.0, 0.0, 0.0]}), encoding="utf-8")

            embedding = run_cli(
                project,
                "embedding-search",
                broad_query,
                "--response-json",
                "query_vector.json",
            )

            self.assertTrue(embedding["results"])
            self.assertEqual(embedding["recommendation_status"], "no_match")
            self.assertIsNone(embedding["recommended_result"])

    def test_failed_backend_gate_writes_report_but_returns_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_project(project)
            cases = {
                "cases": [
                    {
                        "case_id": "deliberate_wrong_expectation",
                        "query": "BETA audio timing cue",
                        "expect_applicable": True,
                        "expected_recipe_id": "recipe_alpha_v1",
                    }
                ]
            }
            (project / "failing_cases.json").write_text(json.dumps(cases), encoding="utf-8")

            result = run_cli(
                project,
                "recall-quality-benchmark",
                "--cases",
                "failing_cases.json",
                "--backends",
                "core",
                expect_ok=False,
            )

            self.assertFalse(result["ok"])
            self.assertFalse(result["gate"]["passed"])
            self.assertTrue(Path(result["report_path"]).is_file())
            self.assertIn("core 没有通过固定同语料门。", result["claim_status"]["missing_evidence"])


if __name__ == "__main__":
    unittest.main()
