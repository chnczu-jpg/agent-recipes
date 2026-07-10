from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
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


def seed_cards(project: Path) -> None:
    source = project / "fixtures" / "embedding_source.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "\n\n".join(
            [
                "card_type: correction_card\n"
                "before: model output was trusted directly\n"
                "correction: review_queue must accept candidate card before formal recipe changes\n"
                "after: keep DeepSeek and embedding outputs candidate-only\n"
                "cannot_claim: cannot say vector search modified formal recipe",
                "card_type: failure_card\n"
                "failed_path: using a raw visual frame as verified evidence\n"
                "failure_signal: screenshot text was not OCR checked\n"
                "replacement_path: OCR first, then text model, then review\n"
                "cannot_claim: cannot say visual quality passed",
            ]
        ),
        encoding="utf-8",
    )
    run_cli(project, "init")
    run_cli(project, "sources", "add", "fixtures/embedding_source.md", "--read-only")
    run_cli(project, "scan", "--depth", "shallow")
    refined = run_cli(
        project,
        "refine",
        "--query",
        "review_queue candidate visual OCR",
        "--knowledge-need",
        "KN_QWEN_EMBEDDING",
        "--target-recipe",
        "recipe_qwen_embedding_v0",
        "--candidate-fields",
        "forbidden_path,failure_signals,checklist_item,visual_check,cannot_claim",
    )
    run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])


class Phase9QwenEmbeddingTest(unittest.TestCase):
    def test_qwen_embedding_config_status_and_fail_closed_without_replay_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_cards(project)

            rejected = run_cli(
                project,
                "embedding-configure",
                "--provider",
                "qwen3",
                "--endpoint",
                "https://example.com/api/embed",
                expect_ok=False,
            )
            self.assertEqual(rejected["code"], "AR370")

            configured = run_cli(
                project,
                "embedding-configure",
                "--provider",
                "qwen3",
                "--model",
                "qwen3-embedding:0.6b",
                "--endpoint",
                "http://127.0.0.1:11434/api/embed",
                "--dimensions",
                "1024",
            )

            self.assertEqual(configured["action"], "embedding-configure")
            self.assertEqual(configured["provider"], "qwen3")
            self.assertEqual(configured["config_status"], "configured")
            self.assertTrue(configured["candidate_only"])
            self.assertEqual(configured["model"], "qwen3-embedding:0.6b")
            self.assertIn("不能说 Qwen3 embedding 服务已经运行。", configured["claim_status"]["cannot_claim"])

            config = json.loads((project / ".recipes" / "embeddings" / "qwen3" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["endpoint"], "http://127.0.0.1:11434/api/embed")
            self.assertTrue(config["safety"]["loopback_only"])

            failed = run_cli(project, "embedding-index", "--provider", "qwen3", expect_ok=False)
            self.assertEqual(failed["code"], "AR375")

            status = run_cli(project, "embedding-status", "--provider", "qwen3")
            self.assertEqual(status["embedding_adapters"]["qwen3"]["config_status"], "configured")
            self.assertFalse(status["embedding_adapters"]["qwen3"]["runtime_verified"])
            self.assertTrue(status["embedding_adapters"]["qwen3"]["candidate_only"])

    def test_qwen_embedding_replay_index_and_search_are_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_cards(project)
            replay = project / "fixtures" / "embedding_replay.json"
            replay.write_text(
                json.dumps(
                    {
                        "rules": [
                            {"contains": "review_queue", "embedding": [1.0, 0.0, 0.0]},
                            {"contains": "visual", "embedding": [0.0, 1.0, 0.0]},
                        ],
                        "default_embedding": [0.0, 0.0, 1.0],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            query = project / "fixtures" / "query_embedding.json"
            query.write_text(json.dumps({"embedding": [1.0, 0.0, 0.0]}, ensure_ascii=False), encoding="utf-8")
            run_cli(
                project,
                "embedding-configure",
                "--provider",
                "qwen3",
                "--model",
                "qwen3-embedding:0.6b",
                "--endpoint",
                "http://127.0.0.1:11434/api/embed",
                "--dimensions",
                "3",
            )

            indexed = run_cli(project, "embedding-index", "--provider", "qwen3", "--response-json", "fixtures/embedding_replay.json")

            self.assertEqual(indexed["action"], "embedding-index")
            self.assertEqual(indexed["provider"], "qwen3")
            self.assertEqual(indexed["execution_mode"], "replay")
            self.assertGreaterEqual(indexed["indexed_count"], 2)
            self.assertTrue(indexed["candidate_only"])
            self.assertIn("不能说 replay embedding 等于真实 Qwen3-Embedding 已调用。", indexed["claim_status"]["cannot_claim"])

            index_rows = [
                json.loads(line)
                for line in (project / ".recipes" / "embeddings" / "qwen3" / "index.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertTrue(index_rows)
            self.assertTrue(all(row["evidence_strength"] == "candidate" for row in index_rows))
            self.assertTrue(all(row["source_trace"] for row in index_rows))

            searched = run_cli(
                project,
                "embedding-search",
                "review queue candidate",
                "--provider",
                "qwen3",
                "--response-json",
                "fixtures/query_embedding.json",
                "--limit",
                "3",
            )

            self.assertEqual(searched["action"], "embedding-search")
            self.assertTrue(searched["results"])
            self.assertGreaterEqual(searched["results"][0]["score"], 0.99)
            self.assertIn("review", searched["results"][0]["text"].casefold())
            self.assertIn("不能说 embedding search 结果已经验证。", searched["claim_status"]["cannot_claim"])

            status = run_cli(project, "embedding-status", "--provider", "qwen3")
            self.assertTrue(status["embedding_adapters"]["qwen3"]["runtime_verified"])
            self.assertEqual(status["embedding_adapters"]["qwen3"]["last_execution_mode"], "replay")

            capabilities = run_cli(project, "capabilities")
            self.assertTrue(capabilities["embedding_adapters"]["qwen3"]["runtime_verified"])

            doctor = run_cli(project, "doctor")
            self.assertTrue(doctor["summary"]["embedding_adapters"]["qwen3"]["runtime_verified"])
            self.assertIn("不能说 embedding search 结果已经自动进入正式 recipe。", doctor["claim_status"]["cannot_claim"])

    def test_qwen_embedding_openai_compatible_loopback_index_and_search(self) -> None:
        requests: list[dict[str, Any]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                requests.append({"path": self.path, "payload": payload})
                text = payload.get("input", "")
                if isinstance(text, list):
                    text = " ".join(str(item) for item in text)
                normalized = str(text).casefold()
                vector = [1.0, 0.0, 0.0] if "review" in normalized else [0.0, 1.0, 0.0]
                body = {"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": vector}]}
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, format: str, *args: Any) -> None:
                return

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                seed_cards(project)
                endpoint = f"http://127.0.0.1:{server.server_port}/v1/embeddings"
                run_cli(
                    project,
                    "embedding-configure",
                    "--provider",
                    "qwen3",
                    "--model",
                    "Qwen/Qwen3-Embedding-0.6B-GGUF:Q8_0",
                    "--endpoint",
                    endpoint,
                    "--dimensions",
                    "3",
                )

                indexed = run_cli(project, "embedding-index", "--provider", "qwen3", "--allow-loopback", "--timeout", "5")
                self.assertEqual(indexed["execution_mode"], "live")
                self.assertGreaterEqual(indexed["indexed_count"], 2)

                searched = run_cli(
                    project,
                    "embedding-search",
                    "review queue candidate",
                    "--provider",
                    "qwen3",
                    "--allow-loopback",
                    "--limit",
                    "3",
                    "--timeout",
                    "5",
                )
                self.assertEqual(searched["execution_mode"], "live")
                self.assertTrue(searched["results"])
                self.assertIn("review", searched["results"][0]["text"].casefold())
                self.assertTrue(all(request["path"] == "/v1/embeddings" for request in requests))
                self.assertTrue(all("Qwen/Qwen3-Embedding-0.6B-GGUF" in request["payload"]["model"] for request in requests))
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()

    def test_mcp_exposes_qwen_embedding_tools(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_embedding_configure", tool_names)
        self.assertIn("agent_recipes_embedding_status", tool_names)
        self.assertIn("agent_recipes_embedding_index", tool_names)
        self.assertIn("agent_recipes_embedding_search", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            configured = call_tool(
                "embedding_configure",
                {
                    "project": str(project),
                    "provider": "qwen3",
                    "model": "qwen3-embedding:0.6b",
                    "endpoint": "http://127.0.0.1:11434/api/embed",
                    "dimensions": 1024,
                },
            )

            self.assertEqual(configured["tool"], "agent_recipes_embedding_configure")
            self.assertEqual(configured["provider"], "qwen3")
            self.assertEqual(configured["config_status"], "configured")


if __name__ == "__main__":
    unittest.main()
