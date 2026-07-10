from __future__ import annotations

import json
import inspect
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import cognee_semantic_probe_env, cognee_semantic_runtime_root, run_cognee_semantic_probe
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


def seed_source_refinery(project: Path) -> dict[str, Any]:
    fixture = project / "fixtures" / "cognee_cards.md"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(
        "\n\n".join(
            [
                "card_type: correction_card\n"
                "before: agent treated memory lookup as verified truth\n"
                "correction: memory results are evidence candidates only\n"
                "after: send memory candidates through patch draft and review\n"
                "cannot_claim: cannot say memory lookup modified formal recipe",
                "card_type: failure_card\n"
                "failed_path: direct recipe write from recalled memory\n"
                "failure_signal: formal recipe changed without review accept\n"
                "replacement_path: create candidate evidence and review queue item\n"
                "cannot_claim: cannot say cognee candidate is verified",
            ]
        ),
        encoding="utf-8",
    )
    run_cli(project, "init")
    run_cli(project, "sources", "add", "fixtures/cognee_cards.md", "--read-only")
    run_cli(project, "scan", "--depth", "shallow")
    refined = run_cli(
        project,
        "refine",
        "--query",
        "memory candidate review cannot_claim",
        "--knowledge-need",
        "KN_COGNEE_MEMORY",
        "--target-recipe",
        "recipe_cognee_memory_v0",
        "--candidate-fields",
        "forbidden_path,failure_signal,checklist_item,cannot_claim",
        "--limit",
        "5",
    )
    run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
    return run_cli(project, "patch-draft", "--target-recipe", "recipe_cognee_memory_v0")


class Phase6CogneeMemoryTest(unittest.TestCase):
    @unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
    def test_cognee_memory_index_search_status_and_doctor_are_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
            seed_source_refinery(project)

            indexed = run_cli(project, "memory-index", "--adapter", "cognee")

            self.assertEqual(indexed["action"], "memory-index")
            self.assertEqual(indexed["adapter"], "cognee")
            self.assertGreaterEqual(indexed["indexed_count"], 3)
            self.assertEqual(indexed["evidence_strength"], "candidate")
            self.assertTrue(indexed["runtime"]["runtime_verified"])
            self.assertIn("不能说 Cognee memory candidate 已经修改正式 recipe。", indexed["claim_status"]["cannot_claim"])

            index_path = project / ".recipes" / "memory" / "cognee" / "index.jsonl"
            rows = read_jsonl(index_path)
            self.assertGreaterEqual(len(rows), 3)
            self.assertTrue(all(row["adapter"] == "cognee" for row in rows))
            self.assertTrue(all(row["evidence_status"] == "candidate" for row in rows))
            self.assertTrue(all(row["source_trace"] for row in rows))

            search = run_cli(project, "memory-search", "direct recipe write", "--adapter", "cognee", "--limit", "5")

            self.assertEqual(search["action"], "memory-search")
            self.assertEqual(search["adapter"], "cognee")
            self.assertTrue(search["results"])
            self.assertTrue(all(item["evidence_status"] == "candidate" for item in search["results"]))
            self.assertIn("不能说 memory search 结果已经验证。", search["claim_status"]["cannot_claim"])

            status = run_cli(project, "memory-status", "--adapter", "cognee")

            self.assertEqual(status["action"], "memory-status")
            self.assertEqual(status["adapters"]["cognee"]["indexed_candidates"], len(rows))
            self.assertTrue(status["adapters"]["cognee"]["runtime_verified"])
            self.assertTrue(status["adapters"]["cognee"]["candidate_only"])

            doctor = run_cli(project, "doctor")

            self.assertTrue(doctor["summary"]["memory_adapters"]["cognee"]["runtime_verified"])
            self.assertEqual(doctor["summary"]["memory_adapters"]["cognee"]["indexed_candidates"], len(rows))
            self.assertIn("不能说 Cognee memory candidate 已经自动进入正式 recipe。", doctor["claim_status"]["cannot_claim"])
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_cognee_memory_v0.json").exists())

    def test_mcp_exposes_cognee_memory_tools(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_memory_index", tool_names)
        self.assertIn("agent_recipes_memory_search", tool_names)
        self.assertIn("agent_recipes_memory_status", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            status = call_tool("memory_status", {"project": str(project), "adapter": "cognee"})

            self.assertEqual(status["tool"], "agent_recipes_memory_status")
            self.assertEqual(status["action"], "memory-status")
            self.assertTrue(status["adapters"]["cognee"]["candidate_only"])

    @unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
    def test_cognee_native_probe_is_caged_fail_closed_and_claim_limited(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
            run_cli(project, "init")

            probe = run_cli(project, "memory-native-probe", "--adapter", "cognee", "--timeout", "30")

            self.assertEqual(probe["action"], "memory-native-probe")
            self.assertEqual(probe["adapter_name"], "cognee")
            self.assertTrue(probe["candidate_only"])
            self.assertIn(probe["native_status"], {"available", "unavailable"})
            self.assertTrue(probe["runtime"]["network_blocked"])
            self.assertTrue(probe["runtime"]["paths_caged"])
            self.assertEqual(probe["runtime"]["env"]["TELEMETRY_DISABLED"], "true")
            self.assertEqual(probe["runtime"]["env"]["ENV"], "test")
            self.assertEqual(probe["runtime"]["env"]["PYTHON_DOTENV_DISABLED"], "1")
            self.assertEqual(probe["runtime"]["env"]["MOCK_EMBEDDING"], "true")
            self.assertEqual(probe["runtime"]["env"]["LITELLM_LOCAL_MODEL_COST_MAP"], "true")
            self.assertEqual(probe["runtime"]["env"]["LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS"], "true")
            self.assertIn("TIKTOKEN_CACHE_DIR", probe["runtime"]["env"])
            self.assertEqual(probe["runtime"]["network_attempts"], [])
            self.assertIn("不能说 Cognee native probe 已证明真实语义记忆质量。", probe["claim_status"]["cannot_claim"])

            native_path = project / ".recipes" / "memory" / "cognee" / "native_probe.json"
            self.assertTrue(native_path.exists())
            native = json.loads(native_path.read_text(encoding="utf-8"))
            self.assertEqual(native["native_status"], probe["native_status"])
            runtime_root = (project / ".recipes" / "memory" / "cognee" / "runtime").resolve()
            Path(native["runtime_root"]).resolve().relative_to(runtime_root)

            status = run_cli(project, "memory-status", "--adapter", "cognee")
            self.assertEqual(status["adapters"]["cognee"]["native_status"], probe["native_status"])
            self.assertTrue(status["adapters"]["cognee"]["native_probe_candidate_only"])

            doctor = run_cli(project, "doctor")
            self.assertIn("不能说 Cognee native probe 已经完成长期语义记忆或图谱闭环。", doctor["claim_status"]["cannot_claim"])

    def test_mcp_exposes_cognee_native_probe_tool(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_memory_native_probe", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            probe = call_tool("memory_native_probe", {"project": str(project), "adapter": "cognee", "timeout": 1})

            self.assertEqual(probe["tool"], "agent_recipes_memory_native_probe")
            self.assertEqual(probe["action"], "memory-native-probe")
            self.assertEqual(probe["adapter_name"], "cognee")
            self.assertIn(probe["native_status"], {"available", "unavailable"})

    @unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
    def test_cognee_semantic_probe_fails_closed_without_cloud_or_mock_embedding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
            run_cli(project, "init")

            probe = run_cli(project, "memory-semantic-probe", "--adapter", "cognee", "--timeout", "30")

            self.assertEqual(probe["action"], "memory-semantic-probe")
            self.assertEqual(probe["adapter_name"], "cognee")
            self.assertTrue(probe["candidate_only"])
            self.assertIn(probe["semantic_status"], {"available", "unavailable"})
            self.assertTrue(probe["runtime"]["network_blocked"])
            self.assertTrue(probe["runtime"]["paths_caged"])
            self.assertEqual(probe["runtime"]["env"]["TELEMETRY_DISABLED"], "true")
            self.assertEqual(probe["runtime"]["env"]["ENV"], "test")
            self.assertEqual(probe["runtime"]["env"]["PYTHON_DOTENV_DISABLED"], "1")
            self.assertEqual(probe["runtime"]["env"]["MOCK_EMBEDDING"], "false")
            self.assertEqual(probe["runtime"]["network_attempts"], [])
            self.assertIn("不能说 Cognee semantic probe 已证明生产级长期记忆。", probe["claim_status"]["cannot_claim"])

            semantic_path = project / ".recipes" / "memory" / "cognee" / "semantic_probe.json"
            self.assertTrue(semantic_path.exists())
            semantic = json.loads(semantic_path.read_text(encoding="utf-8"))
            self.assertEqual(semantic["semantic_status"], probe["semantic_status"])

            status = run_cli(project, "memory-status", "--adapter", "cognee")
            self.assertEqual(status["adapters"]["cognee"]["semantic_status"], probe["semantic_status"])
            self.assertTrue(status["adapters"]["cognee"]["semantic_probe_candidate_only"])

            doctor = run_cli(project, "doctor")
            self.assertIn("不能说 Cognee semantic probe 已经证明生产级长期记忆。", doctor["claim_status"]["cannot_claim"])

    def test_mcp_exposes_cognee_semantic_probe_tool(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_memory_semantic_probe", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            probe = call_tool("memory_semantic_probe", {"project": str(project), "adapter": "cognee", "timeout": 1})

            self.assertEqual(probe["tool"], "agent_recipes_memory_semantic_probe")
            self.assertEqual(probe["action"], "memory-semantic-probe")
            self.assertEqual(probe["adapter_name"], "cognee")
            self.assertIn(probe["semantic_status"], {"available", "unavailable"})

    def test_cognee_semantic_configure_rejects_unapproved_cloud_and_writes_loopback_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            rejected = run_cli(
                project,
                "memory-semantic-configure",
                "--adapter",
                "cognee",
                "--llm-provider",
                "custom",
                "--llm-model",
                "local-chat",
                "--llm-endpoint",
                "https://api.openai.com/v1",
                "--embedding-provider",
                "openai_compatible",
                "--embedding-model",
                "local-embed",
                "--embedding-endpoint",
                "http://127.0.0.1:8080/v1/embeddings",
                "--embedding-dimensions",
                "768",
                expect_ok=False,
            )

            self.assertEqual(rejected["code"], "AR342")
            self.assertIn("loopback", rejected["cause"])

            rejected_deepseek_host = run_cli(
                project,
                "memory-semantic-configure",
                "--adapter",
                "cognee",
                "--llm-provider",
                "deepseek",
                "--llm-model",
                "deepseek-v4-flash",
                "--llm-endpoint",
                "https://api.deepseek.com.evil.test",
                "--embedding-provider",
                "openai_compatible",
                "--embedding-model",
                "local-embed",
                "--embedding-endpoint",
                "http://127.0.0.1:8080/v1/embeddings",
                "--embedding-dimensions",
                "768",
                expect_ok=False,
            )
            self.assertEqual(rejected_deepseek_host["code"], "AR342")

            configured = run_cli(
                project,
                "memory-semantic-configure",
                "--adapter",
                "cognee",
                "--llm-provider",
                "custom",
                "--llm-model",
                "local-chat",
                "--llm-endpoint",
                "http://127.0.0.1:8080/v1",
                "--embedding-provider",
                "openai_compatible",
                "--embedding-model",
                "local-embed",
                "--embedding-endpoint",
                "http://127.0.0.1:8080/v1/embeddings",
                "--embedding-dimensions",
                "768",
            )

            self.assertEqual(configured["action"], "memory-semantic-configure")
            self.assertEqual(configured["adapter_name"], "cognee")
            self.assertEqual(configured["config_status"], "configured")
            self.assertTrue(configured["candidate_only"])
            self.assertEqual(configured["runtime_env"]["LLM_PROVIDER"], "custom")
            self.assertEqual(configured["runtime_env"]["EMBEDDING_PROVIDER"], "openai_compatible")
            self.assertNotIn("OPENAI_API_KEY", configured["runtime_env"])
            self.assertIn("不能说 semantic runtime 配置已经证明 Cognee 语义记忆可用。", configured["claim_status"]["cannot_claim"])

            config_path = project / ".recipes" / "memory" / "cognee" / "semantic_runtime.json"
            self.assertTrue(config_path.exists())
            config = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(config["config_status"], "configured")
            self.assertTrue(config["safety"]["loopback_only"])
            self.assertTrue(config["safety"]["secrets_written"] is False)

            status = run_cli(project, "memory-status", "--adapter", "cognee")
            self.assertEqual(status["adapters"]["cognee"]["semantic_config_status"], "configured")
            self.assertTrue(status["adapters"]["cognee"]["semantic_config_candidate_only"])

    def test_cognee_semantic_configure_allows_deepseek_without_storing_secret(self) -> None:
        env_name = "AGENT_RECIPES_TEST_DEEPSEEK_KEY"
        old_value = os.environ.pop(env_name, None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                run_cli(project, "init")

                configured = run_cli(
                    project,
                    "memory-semantic-configure",
                    "--adapter",
                    "cognee",
                    "--llm-provider",
                    "deepseek",
                    "--llm-model",
                    "deepseek-v4-flash",
                    "--llm-endpoint",
                    "https://api.deepseek.com",
                    "--llm-api-key-env",
                    env_name,
                    "--embedding-provider",
                    "openai_compatible",
                    "--embedding-model",
                    "qwen3-embedding:0.6b",
                    "--embedding-endpoint",
                    "http://127.0.0.1:18080/v1/embeddings",
                    "--embedding-dimensions",
                    "1024",
                )

                self.assertEqual(configured["config_status"], "configured")
                self.assertEqual(configured["runtime_env"]["AGENT_RECIPES_LLM_PROVIDER"], "deepseek")
                self.assertEqual(configured["runtime_env"]["LLM_PROVIDER"], "custom")
                self.assertEqual(configured["runtime_env"]["LLM_MODEL"], "openai/deepseek-v4-flash")
                self.assertEqual(configured["runtime_env"]["LLM_API_KEY_ENV"], env_name)
                self.assertEqual(configured["runtime_env"]["LLM_API_KEY_PRESENT"], "false")
                self.assertNotIn("LLM_API_KEY", configured["runtime_env"])

                config_path = project / ".recipes" / "memory" / "cognee" / "semantic_runtime.json"
                config = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(config["llm"]["provider"], "deepseek")
                self.assertEqual(config["llm"]["api_key_env"], env_name)
                self.assertFalse(config["llm"]["api_key_present"])
                self.assertFalse(config["safety"]["cloud_blocked"])
                self.assertEqual(config["safety"]["cloud_provider_allowlist"], ["api.deepseek.com"])
                serialized_config = json.dumps(config, ensure_ascii=False)
                self.assertNotIn('"LLM_API_KEY":', serialized_config)
                self.assertNotIn("not-a-real-test-key", serialized_config)

                os.environ[env_name] = "not-a-real-test-key"
                configured_with_key = run_cli(
                    project,
                    "memory-semantic-configure",
                    "--adapter",
                    "cognee",
                    "--llm-provider",
                    "deepseek",
                    "--llm-model",
                    "deepseek-v4-pro",
                    "--llm-endpoint",
                    "https://api.deepseek.com",
                    "--llm-api-key-env",
                    env_name,
                    "--embedding-provider",
                    "openai_compatible",
                    "--embedding-model",
                    "qwen3-embedding:0.6b",
                    "--embedding-endpoint",
                    "http://127.0.0.1:18080/v1/embeddings",
                    "--embedding-dimensions",
                    "1024",
                )
                self.assertEqual(configured_with_key["runtime_env"]["LLM_API_KEY_PRESENT"], "true")
                self.assertEqual(configured_with_key["runtime_env"]["LLM_MODEL"], "openai/deepseek-v4-pro")
                self.assertNotIn("not-a-real-test-key", json.dumps(configured_with_key, ensure_ascii=False))
        finally:
            if old_value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = old_value

    @unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
    def test_cognee_semantic_probe_fails_closed_when_deepseek_key_missing(self) -> None:
        env_name = "AGENT_RECIPES_TEST_DEEPSEEK_KEY"
        old_value = os.environ.pop(env_name, None)
        try:
            with tempfile.TemporaryDirectory() as tmp:
                project = Path(tmp)
                (project / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
                run_cli(project, "init")
                run_cli(
                    project,
                    "memory-semantic-configure",
                    "--adapter",
                    "cognee",
                    "--llm-provider",
                    "deepseek",
                    "--llm-model",
                    "deepseek-v4-flash",
                    "--llm-endpoint",
                    "https://api.deepseek.com",
                    "--llm-api-key-env",
                    env_name,
                    "--embedding-provider",
                    "openai_compatible",
                    "--embedding-model",
                    "qwen3-embedding:0.6b",
                    "--embedding-endpoint",
                    "http://127.0.0.1:18080/v1/embeddings",
                    "--embedding-dimensions",
                    "1024",
                )

                probe = run_cli(project, "memory-semantic-probe", "--adapter", "cognee", "--timeout", "5")

                self.assertEqual(probe["semantic_status"], "unavailable")
                self.assertIn(env_name, probe["runtime"]["error"])
                self.assertFalse(probe["runtime"]["dependency_status"]["cloud_llm_ready"])
                self.assertFalse(probe["runtime"]["dependency_status"]["configured"]["llm_api_key_present"])
                self.assertEqual(probe["runtime"]["env"]["LLM_API_KEY_PRESENT"], "false")
                self.assertNotIn("LLM_API_KEY", probe["runtime"]["env"])
                self.assertEqual(probe["runtime"]["network_attempts"], [])
        finally:
            if old_value is None:
                os.environ.pop(env_name, None)
            else:
                os.environ[env_name] = old_value

    def test_cognee_semantic_probe_uses_current_recall_dataset_argument(self) -> None:
        source = inspect.getsource(run_cognee_semantic_probe)

        self.assertIn("datasets=[dataset_name]", source)
        self.assertNotIn("dataset_name=dataset_name,\n        top_k=3", source)

    def test_cognee_semantic_runtime_root_is_config_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = {
                "adapter": "cognee",
                "config_status": "configured",
                "llm": {"provider": "deepseek", "model": "deepseek-v4-flash"},
                "embedding": {"provider": "openai_compatible", "model": "qwen3-embedding:0.6b", "dimensions": 1024},
            }
            changed = json.loads(json.dumps(base))
            changed["embedding"]["dimensions"] = 1536

            first = cognee_semantic_runtime_root(root, base)
            second = cognee_semantic_runtime_root(root, changed)

            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, root / ".recipes" / "memory" / "cognee" / "runtime" / "semantic")
            self.assertFalse(str(first).endswith("/runtime"))

    def test_cognee_semantic_probe_env_keeps_project_config_over_shell_overrides(self) -> None:
        old_model = os.environ.get("LLM_MODEL")
        old_endpoint = os.environ.get("LLM_ENDPOINT")
        try:
            os.environ["LLM_MODEL"] = "bad-shell-model"
            os.environ["LLM_ENDPOINT"] = "http://127.0.0.1:9999"
            config = {
                "adapter": "cognee",
                "config_status": "configured",
                "llm": {
                    "provider": "deepseek",
                    "model": "deepseek-v4-flash",
                    "endpoint": "https://api.deepseek.com",
                    "api_key_env": "AGENT_RECIPES_TEST_DEEPSEEK_KEY",
                },
                "embedding": {
                    "provider": "openai_compatible",
                    "model": "qwen3-embedding:0.6b",
                    "endpoint": "http://127.0.0.1:18080/v1/embeddings",
                    "dimensions": 1024,
                },
            }

            env = cognee_semantic_probe_env(Path("/tmp/agent-recipes-runtime"), config)

            self.assertEqual(env["LLM_MODEL"], "openai/deepseek-v4-flash")
            self.assertEqual(env["LLM_ENDPOINT"], "https://api.deepseek.com")
            self.assertEqual(env["EMBEDDING_ENDPOINT"], "http://127.0.0.1:18080/v1/embeddings")
        finally:
            if old_model is None:
                os.environ.pop("LLM_MODEL", None)
            else:
                os.environ["LLM_MODEL"] = old_model
            if old_endpoint is None:
                os.environ.pop("LLM_ENDPOINT", None)
            else:
                os.environ["LLM_ENDPOINT"] = old_endpoint

    def test_mcp_exposes_cognee_semantic_configure_tool(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_memory_semantic_configure", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            configured = call_tool(
                "memory_semantic_configure",
                {
                    "project": str(project),
                    "adapter": "cognee",
                    "detect_only": True,
                },
            )

            self.assertEqual(configured["tool"], "agent_recipes_memory_semantic_configure")
            self.assertEqual(configured["action"], "memory-semantic-configure")
            self.assertEqual(configured["adapter_name"], "cognee")
            self.assertEqual(configured["config_status"], "not_configured")
