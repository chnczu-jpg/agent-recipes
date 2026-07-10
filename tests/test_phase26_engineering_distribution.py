from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "bin" / "agent-recipes-qwen-service"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def fake_server(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import argparse
from http.server import BaseHTTPRequestHandler, HTTPServer

parser = argparse.ArgumentParser(add_help=False)
parser.add_argument('--port', type=int, required=True)
args, _ = parser.parse_known_args()

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_response(404)
            self.end_headers()
            return
        body = b'{\"status\":\"ok\"}'
        self.send_response(200)
        self.send_header('content-length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):
        return

HTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def run_service(project: Path, *args: str, expect_ok: bool = True) -> dict[str, Any]:
    command = [str(SERVICE), *args, "--project", str(project)]
    result = subprocess.run(command, cwd=ROOT, text=True, capture_output=True, check=False)
    if expect_ok and result.returncode != 0:
        raise AssertionError(f"service failed: {command}\nstdout={result.stdout}\nstderr={result.stderr}")
    if not expect_ok and result.returncode == 0:
        raise AssertionError(f"service unexpectedly passed: {command}")
    return json.loads(result.stdout)


class Phase26EngineeringDistributionTest(unittest.TestCase):
    def test_project_local_qwen_service_start_status_restart_stop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            server = root / "fake-llama-server"
            model = root / "model.gguf"
            fake_server(server)
            model.write_bytes(b"fake-model")
            port = free_port()
            try:
                started = run_service(
                    project,
                    "start",
                    "--server",
                    str(server),
                    "--model",
                    str(model),
                    "--port",
                    str(port),
                    "--timeout",
                    "5",
                )
                first_pid = started["pid"]
                self.assertTrue(started["healthy"])
                self.assertTrue(started["ownership_verified"])

                status = run_service(project, "status")
                self.assertEqual(status["pid"], first_pid)
                self.assertTrue(status["healthy"])

                restarted = run_service(
                    project,
                    "restart",
                    "--server",
                    str(server),
                    "--model",
                    str(model),
                    "--port",
                    str(port),
                    "--timeout",
                    "5",
                )
                self.assertNotEqual(restarted["pid"], first_pid)
                self.assertTrue(restarted["healthy"])

                stopped = run_service(project, "stop")
                self.assertTrue(stopped["ok"])
                self.assertFalse(stopped["running"])
                after = run_service(project, "status", expect_ok=False)
                self.assertFalse(after["healthy"])
            finally:
                run_service(project, "stop", expect_ok=True)

    def test_service_fails_closed_for_missing_model_and_pid_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            server = root / "fake-llama-server"
            fake_server(server)
            missing = run_service(
                project,
                "start",
                "--server",
                str(server),
                "--model",
                str(root / "missing.gguf"),
                expect_ok=False,
            )
            self.assertIn("model file missing", missing["error"])

            state_path = project / ".recipes" / "embeddings" / "qwen3" / "service" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "server_path": str(server),
                        "model_path": str(root / "model.gguf"),
                        "port": free_port(),
                    }
                ),
                encoding="utf-8",
            )
            mismatch = run_service(project, "stop", expect_ok=False)
            self.assertTrue(mismatch["pid_reused_or_mismatched"])
            self.assertIn("refusing to signal", mismatch["error"])

    def test_release_scripts_and_version_contract_are_explicit(self) -> None:
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        mcp = (ROOT / "agent_recipes" / "mcp.py").read_text(encoding="utf-8")
        clean = (ROOT / "bin" / "verify-clean-install").read_text(encoding="utf-8")
        upgrade = (ROOT / "bin" / "verify-upgrade-rollback").read_text(encoding="utf-8")

        package_init = (ROOT / "agent_recipes" / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('version = "0.2.0"', pyproject)
        self.assertIn('script-files = ["bin/agent-recipes-qwen-service"]', pyproject)
        self.assertIn('"version": "0.2.0"', mcp)
        self.assertIn('__version__ = "0.2.0"', package_init)
        self.assertIn('PYTHON_BIN', clean)
        self.assertIn('rollback_doctor', upgrade)
        self.assertIn('event_log_unchanged', upgrade)


if __name__ == "__main__":
    unittest.main()
