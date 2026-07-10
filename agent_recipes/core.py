from __future__ import annotations

import importlib.util
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from agent_recipes.execution import (
    LOOKUP_QUERY_PHRASES,
    LOOKUP_STOPWORDS,
    LOOKUP_TERM_ALIASES,
    build_execution_lock,
    execution_lock_id,
    load_lookup_priority_rules,
    lookup_applicability,
    lookup_execution_policy,
    lookup_priority_bonus_for_recipe,
    lookup_priority_rule_matches_query,
    lookup_priority_term_in_query,
    lookup_query_is_overbroad_single_recipe_request,
    lookup_query_is_subtitle_ocr_asr_repair,
    lookup_query_term_is_negated,
    lookup_query_terms,
    lookup_split_term_is_useful,
    lookup_term_alternatives,
    lookup_term_is_searchable,
    rank_recipes_for_lookup,
    recipe_declares_subtitle_ocr_asr_scope,
    recipe_is_lookup_guardrail,
    recipe_lookup_haystack,
    recall_no_match_reason,
    retire_active_execution_locks,
    retire_stale_execution_locks,
    validate_execution_lock,
)
from agent_recipes.corrections import correction_compile_plan
from agent_recipes.ledger import EVENT_SCHEMA_VERSION, EventLedger
from agent_recipes.lifecycle import (
    RECIPE_TOMBSTONE_REASONS,
    active_recipe_ids_for_consumption,
    assert_recipe_promotable,
    recipe_content_hash,
    recipe_exists,
    recipe_hash,
    recipe_lifecycle_state,
    recipe_lifecycle_summary,
    recipe_path_for,
    recipe_version_rank,
)
from agent_recipes.migration import (
    PROJECT_SCHEMA_VERSION,
    initialize_schema_marker,
    project_schema_status,
    schema_marker_path,
    validate_migration_target,
    write_migrated_schema_marker,
)
from agent_recipes.outcome import (
    OUTCOME_POLICY,
    empty_outcome_counts,
    explicit_lock_capture_payload,
    find_outcome_quality,
    outcome_binding_key,
    outcome_lock_snapshot_hash,
    outcome_quality_state,
    outcome_quality_summary,
    recipe_bindings_from_lock,
    valid_outcome_binding,
)
from agent_recipes.persistence import (
    RecipesError,
    annotate_persistence_redaction,
    file_sha256,
    make_id,
    now_iso,
    read_json,
    read_jsonl,
    read_optional_json,
    read_optional_jsonl,
    redact_sensitive_text,
    redact_sensitive_value,
    sha256_json,
    sha256_text,
    stable_json,
    write_json,
    write_jsonl,
    write_text_redacted,
)


SCHEMA_VERSION = EVENT_SCHEMA_VERSION


class RecipesProject:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.recipes_dir = self.root / ".recipes"
        self.events_path = self.recipes_dir / "events.jsonl"
        self._ledger = EventLedger(self.events_path, self.ensure_dirs)

    def ensure_dirs(self) -> list[str]:
        dirs = [
            self.recipes_dir,
            self.recipes_dir / "recipes",
            self.recipes_dir / "candidates",
            self.recipes_dir / "review_queue",
            self.recipes_dir / "corrections",
            self.recipes_dir / "failures",
            self.recipes_dir / "successes",
            self.recipes_dir / "unknowns",
            self.recipes_dir / "evidence",
            self.recipes_dir / "video_index",
            self.recipes_dir / "source_index",
            self.recipes_dir / "source_refinery",
            self.recipes_dir / "source_refinery" / "chunks",
            self.recipes_dir / "source_refinery" / "fusion",
            self.recipes_dir / "source_refinery" / "deep_read_plans",
            self.recipes_dir / "source_refinery" / "patch_drafts",
            self.recipes_dir / "source_refinery" / "normalized" / "markdown",
            self.recipes_dir / "source_refinery" / "normalized" / "transcripts",
            self.recipes_dir / "source_refinery" / "normalized" / "keyframes",
            self.recipes_dir / "source_refinery" / "normalized" / "ocr",
            self.recipes_dir / "source_refinery" / "cards" / "correction_cards",
            self.recipes_dir / "source_refinery" / "cards" / "run_chain_cards",
            self.recipes_dir / "source_refinery" / "cards" / "failure_cards",
            self.recipes_dir / "source_refinery" / "cards" / "learning_atom_cards",
            self.recipes_dir / "source_refinery" / "cards" / "visual_example_cards",
            self.recipes_dir / "memory",
            self.recipes_dir / "memory" / "cognee",
            self.recipes_dir / "memory" / "cognee" / "runtime",
            self.recipes_dir / "cloud",
            self.recipes_dir / "cloud" / "deepseek",
            self.recipes_dir / "embeddings",
            self.recipes_dir / "embeddings" / "qwen3",
            self.recipes_dir / "lifecycle",
            self.recipes_dir / "locks",
            self.recipes_dir / "quarantine",
            self.recipes_dir / "evidence_packs",
            self.recipes_dir / "reports",
        ]
        written: list[str] = []
        for path in dirs:
            if not path.exists():
                path.mkdir(parents=True, exist_ok=True)
                written.append(str(path))
        seed_files = {
            "START_HERE.md": "# Agent Recipes\n\nRun `agent-recipes doctor --json` first.\n",
            "PROJECT_PROFILE.md": "# Project Profile\n\nStatus: Phase 0A local project.\n",
            "KNOWLEDGE_MAP.md": "# Knowledge Map\n\nStatus: not generated in Phase 0A.\n",
            "sources.yaml": "sources: []\n",
        }
        for name, content in seed_files.items():
            path = self.recipes_dir / name
            if not path.exists():
                write_text_redacted(path, content)
                written.append(str(path))
        if not self.events_path.exists():
            self.events_path.touch()
            written.append(str(self.events_path))
        return written

    def load_events(self) -> list[dict[str, Any]]:
        return self._ledger.load()

    def event_hash(self, event: dict[str, Any]) -> str:
        return self._ledger.event_hash(event)

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        actor: str = "codex",
        session_id: str | None = None,
        lock_id: str | None = None,
        lock_exempt_reason: str | None = None,
        causation_id: str | None = None,
        idempotency_key: str | None = None,
        claim_status: dict[str, list[str]] | None = None,
    ) -> tuple[dict[str, Any], str]:
        return self._ledger.append(
            event_type,
            payload,
            actor=actor,
            session_id=session_id,
            lock_id=lock_id,
            lock_exempt_reason=lock_exempt_reason,
            causation_id=causation_id,
            idempotency_key=idempotency_key,
            claim_status=claim_status,
        )

    def init(self) -> dict[str, Any]:
        files = self.ensure_dirs()
        marker_path, marker_created = initialize_schema_marker(self.recipes_dir)
        if marker_created:
            files.append(str(marker_path))
        payload = {"project_root": str(self.root), "phase": "0A"}
        event, idem = self.append_event(
            "project_initialized",
            payload,
            idempotency_key=f"init:{self.root}",
            lock_exempt_reason="project_initialization",
            claim_status={
                "verified": ["已创建或确认 .recipes Phase 0A 目录。"],
                "inferred": [],
                "missing_evidence": [],
                "cannot_claim": ["不能说项目已经有正式菜谱。"],
            },
        )
        return command_result(
            "init",
            idem,
            files_written=files,
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
        )

    def migration_status(self) -> dict[str, Any]:
        status = project_schema_status(self.recipes_dir)
        return {
            "ok": status["state"] not in {"malformed", "unsupported_newer"},
            "action": "migration-status",
            "schema": status,
            "claim_status": claim_status(
                verified=[
                    f"已读取项目 schema 状态：{status['state']}。",
                    "项目迁移不要求重写 events.jsonl。",
                ],
                missing_evidence=(
                    ["项目尚未初始化。"]
                    if status["state"] == "uninitialized"
                    else (["尚未执行所需迁移。"] if status["migration_required"] else [])
                ),
                cannot_claim=[
                    "migration-status 只读，不表示迁移已经执行。",
                    "当前机器验证不能代替真实第二台机器复现。",
                ],
            ),
        }

    def migrate(self, *, target: str = PROJECT_SCHEMA_VERSION) -> dict[str, Any]:
        status = project_schema_status(self.recipes_dir)
        validate_migration_target(status, target)
        if status["current"]:
            return command_result(
                "migrate",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=[f"项目 schema 已是 {PROJECT_SCHEMA_VERSION}，未重复写入。"],
                    cannot_claim=["不能说本次重写了历史事件。"],
                ),
                extra={"schema": status, "event_log_rewritten": False},
            )

        event_log_hash_before = file_sha256(self.events_path)
        marker_path = schema_marker_path(self.recipes_dir)
        previous_marker = marker_path.read_bytes() if marker_path.exists() else None
        try:
            write_migrated_schema_marker(
                self.recipes_dir,
                previous_version=status.get("installed_version"),
                event_log_hash_before=event_log_hash_before,
            )
            event, idem = self.append_event(
                "project_schema_migrated",
                {
                    "from": status.get("installed_version") or "legacy_unversioned",
                    "to": PROJECT_SCHEMA_VERSION,
                    "event_log_hash_before": event_log_hash_before,
                    "event_log_rewritten": False,
                },
                idempotency_key=f"project-schema:{PROJECT_SCHEMA_VERSION}",
                lock_exempt_reason="project_schema_migration",
                claim_status=claim_status(
                    verified=[f"项目 schema 已迁移到 {PROJECT_SCHEMA_VERSION}。", "旧事件未被重写。"],
                    cannot_claim=["不能说旧项目已在真实第二台机器复现。"],
                ),
            )
        except Exception:
            if previous_marker is None:
                marker_path.unlink(missing_ok=True)
            else:
                marker_path.write_bytes(previous_marker)
            raise
        current = project_schema_status(self.recipes_dir)
        return command_result(
            "migrate",
            idem,
            files_written=[str(marker_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "schema": current,
                "event_log_hash_before": event_log_hash_before,
                "event_log_rewritten": False,
            },
        )

    def add_source(self, source_path: str, permission: str = "read-only") -> dict[str, Any]:
        self.ensure_dirs()
        path = Path(source_path)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.exists() or not path.is_file():
            raise RecipesError(
                "AR210",
                "授权资料源不存在。",
                f"找不到文件：{path}",
                "传入真实存在的文件路径。",
            )
        digest = file_sha256(path)
        source = {
            "source_id": make_id("src", str(path), digest),
            "path": str(path),
            "source_type": "file",
            "permission": permission,
            "allow_cloud": False,
            "allow_transcript": False,
            "expires_at": None,
            "hash": digest,
            "status": "active",
        }
        sources_path = self.recipes_dir / "source_index" / "sources.json"
        sources = read_json(sources_path, [])
        if any(item["source_id"] == source["source_id"] for item in sources):
            return command_result(
                "sources add",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=["资料源已经登记，未重复写入。"],
                    cannot_claim=["不能说已扫描该资料源内容。"],
                ),
                extra={"source": source},
            )
        payload = {"source": source}
        event, idem = self.append_event(
            "source_added",
            payload,
            idempotency_key=f"source:add:{source['source_id']}",
            lock_exempt_reason="source_registration",
            claim_status=claim_status(
                verified=[f"已读取 source 文件 hash：{path}"],
                cannot_claim=["不能说该 source 已被扫描或编译成菜谱。"],
            ),
        )
        sources.append(source)
        write_json(sources_path, sources)
        self.write_sources_yaml(sources)
        files = [str(sources_path), str(self.recipes_dir / "sources.yaml")]
        return command_result(
            "sources add",
            idem,
            files_written=files,
            objects_created=[source["source_id"], event["event_id"]],
            claim_status=event["claim_status"],
            extra={"source": source},
        )

    def write_sources_yaml(self, sources: list[dict[str, Any]]) -> None:
        lines = ["sources:"]
        for source in sources:
            lines.append(f"  - source_id: {source['source_id']}")
            lines.append(f"    path: {source['path']}")
            lines.append(f"    permission: {source['permission']}")
            lines.append(f"    hash: {source['hash']}")
            lines.append(f"    status: {source['status']}")
        if len(lines) == 1:
            lines[0] = "sources: []"
        write_text_redacted(self.recipes_dir / "sources.yaml", "\n".join(lines) + "\n")

    def capture(
        self,
        capture_type: str,
        text: str,
        *,
        task: str = "",
        lock_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        lock: dict[str, Any] | None = None
        if capture_type in {"success", "failure", "unknown"}:
            if not lock_id:
                raise RecipesError(
                    "AR410",
                    "success/failure/unknown capture 必须绑定 active lock。",
                    "没有 lock_id，就无法证明 agent 当时依据哪份菜谱执行。",
                    "先运行 agent-recipes lookup，再运行 agent-recipes lock。",
                )
            lock = self.validate_lock(lock_id)
            lock_exempt_reason = None
        elif capture_type == "correction":
            lock = self.validate_lock(lock_id) if lock_id else None
            lock_exempt_reason = None if lock else "capture_correction_before_recipe"
        else:
            raise RecipesError(
                "AR220",
                "不支持的 capture type。",
                f"收到：{capture_type}",
                "使用 correction、success、failure 或 unknown。",
            )
        payload = {
            "capture_type": capture_type,
            "task": task,
            "text": text,
            "lock_id": lock_id,
        }
        if lock is not None:
            payload.update(explicit_lock_capture_payload(lock, str(lock_id), capture_type))
        if capture_type == "failure":
            payload["problem_fingerprint"] = problem_fingerprint(task or text)
        event, idem = self.append_event(
            "capture",
            payload,
            lock_id=lock_id,
            lock_exempt_reason=lock_exempt_reason,
            idempotency_key=idempotency_key or f"capture:v2:{capture_type}:{lock_id or 'none'}:{sha256_text(text)}",
            claim_status=claim_status(
                verified=["已把 capture 写入 append-only events.jsonl。"],
                cannot_claim=["不能说这条 capture 已经变成正式菜谱。"],
            ),
        )
        folder = {
            "correction": "corrections",
            "success": "successes",
            "failure": "failures",
            "unknown": "unknowns",
        }[capture_type]
        path = self.recipes_dir / folder / f"{event['event_id']}.json"
        stored_payload = event.get("payload", payload)
        write_json(path, {"event_id": event["event_id"], **stored_payload})
        bound_statuses: list[dict[str, Any]] = []
        if lock is not None:
            status = self.outcome_status()
            binding_keys = {outcome_binding_key(item) for item in payload["recipe_bindings"]}
            bound_statuses = [
                item
                for item in status["recipes"]
                if outcome_binding_key(item) in binding_keys
            ]
        return command_result(
            "capture",
            idem,
            files_written=[str(path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "event_id": event["event_id"],
                "capture_type": capture_type,
                "outcome": payload.get("outcome"),
                "recipe_bindings": stored_payload.get("recipe_bindings", []),
                "persistence_redaction": stored_payload.get("persistence_redaction"),
                "outcome_status": bound_statuses,
            },
        )

    def scan(self, *, depth: str = "shallow") -> dict[str, Any]:
        self.ensure_dirs()
        if depth not in {"shallow", "medium"}:
            raise RecipesError(
                "AR250",
                "scan depth 不支持。",
                f"收到：{depth}",
                "使用 agent-recipes scan --depth shallow 或 --depth medium。",
            )
        sources_path = self.recipes_dir / "source_index" / "sources.json"
        sources = read_json(sources_path, [])
        active_sources = [
            source
            for source in sources
            if source.get("status") == "active" and source.get("source_type") == "file"
        ]
        if not active_sources:
            raise RecipesError(
                "AR251",
                "没有可扫描的授权资料源。",
                "source_index/sources.json 里没有 active file source。",
                "先运行 agent-recipes sources add <file> --read-only。",
            )

        max_chars = 900 if depth == "shallow" else 1800
        chunks: list[dict[str, Any]] = []
        source_summaries: list[dict[str, Any]] = []
        for source in active_sources:
            path = Path(source["path"])
            if not path.exists() or not path.is_file():
                raise RecipesError(
                    "AR252",
                    "授权资料源文件不存在。",
                    f"找不到文件：{path}",
                    "修复 sources.json，或重新运行 sources add。",
                )
            current_hash = file_sha256(path)
            if current_hash != source.get("hash"):
                raise RecipesError(
                    "AR253",
                    "授权资料源 hash 已变化。",
                    f"{path}: registered={source.get('hash')}, current={current_hash}",
                    "重新确认资料源后再运行 sources add。",
                )
            text = source_text_for_index(path, path.read_text(encoding="utf-8"))
            source_chunks = chunk_text_source(
                source_id=source["source_id"],
                source_path=str(path),
                text=text,
                max_chars=max_chars,
            )
            chunks.extend(source_chunks)
            source_summaries.append(
                {
                    "source_id": source["source_id"],
                    "path": str(path),
                    "hash": current_hash,
                    "chunk_count": len(source_chunks),
                }
            )

        chunks_path = self.recipes_dir / "source_index" / "chunks.jsonl"
        index_path = self.recipes_dir / "source_index" / "INDEX.md"
        summary_path = self.recipes_dir / "source_index" / "scan_summary.json"
        summary = {
            "depth": depth,
            "sources": source_summaries,
            "chunk_count": len(chunks),
            "claim_limits": [
                "只扫描 source_index 中登记且 hash 未变化的本地文本文件。",
                "没有扫描全项目历史、外部链接或视频原始文件。",
            ],
        }
        write_jsonl(chunks_path, chunks)
        write_json(summary_path, summary)
        write_text_redacted(index_path, source_index_markdown(summary))

        payload = {"depth": depth, "sources": source_summaries, "chunk_count": len(chunks)}
        event, idem = self.append_event(
            "sources_scanned",
            payload,
            idempotency_key=f"scan:{depth}:{sha256_json(source_summaries)}",
            lock_exempt_reason="scan_registered_sources",
            claim_status=claim_status(
                verified=["已读取 source_index 中列出的本地文件路径和 hash，并生成 source_index chunks。"],
                cannot_claim=[
                    "不能说已覆盖全部历史资料。",
                    "不能说生成的 source_index 已自动变成正式菜谱。",
                ],
            ),
        )
        return command_result(
            "scan",
            idem,
            files_written=[str(chunks_path), str(index_path), str(summary_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"depth": depth, "sources_scanned": len(source_summaries), "chunks_indexed": len(chunks)},
        )

    def recover(self, problem: str, *, idempotency_key: str | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        if not problem.strip():
            raise RecipesError("AR430", "recover problem 不能为空。", "没有可判定的 problem_fingerprint。", "传入 --problem。")
        fingerprint = problem_fingerprint(problem)
        events = self.load_events()
        matching_failures = matching_failure_events(events, problem, fingerprint)
        if len(matching_failures) < 3:
            raise RecipesError(
                "AR431",
                "同类失败还没达到三次阈值。",
                f"problem_fingerprint={fingerprint}，当前匹配 failure={len(matching_failures)}。",
                "继续按 active lock capture failure；第三次后再运行 recover。",
            )

        failure_event_ids = [event["event_id"] for event in matching_failures]
        related_recipe_ids = self.recipe_ids_from_failure_events(matching_failures)
        target_recipe_id = related_recipe_ids[0] if related_recipe_ids else make_id("recipe", "recover", fingerprint)
        base_recipe = self.load_recipe(target_recipe_id) if recipe_exists(self.recipes_dir, target_recipe_id) else None
        patch_id = make_id("patch_recover", fingerprint, failure_event_ids, target_recipe_id)
        review_id = make_id("review", patch_id)
        draft = recover_recipe_draft(
            target_recipe_id=target_recipe_id,
            problem=problem,
            fingerprint=fingerprint,
            failures=matching_failures,
            base_recipe=base_recipe,
        )
        patch = {
            "patch_id": patch_id,
            "patch_type": "recover_candidate",
            "problem_fingerprint": fingerprint,
            "source_event_ids": failure_event_ids,
            "target_recipe_id": target_recipe_id,
            "proposed_change": draft,
            "reason": "三次同类 failure capture 达到 recover 阈值，只生成候选补丁。",
            "evidence_refs": failure_event_ids,
            "risk": "needs_review",
            "status": "pending_review",
        }
        review = {
            "review_id": review_id,
            "blocking_level": "P0",
            "question": f"是否接受 recover 候选补丁：{first_line(problem)}",
            "why_user_must_decide": "三次同类失败说明现有菜谱可能缺规则；接受后才允许生成正式 recipe version。",
            "options": ["accept", "reject", "supersede"],
            "recommendation": "review",
            "evidence_refs": failure_event_ids,
            "proposed_patch_id": patch_id,
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
        }
        candidate_path = self.recipes_dir / "candidates" / f"{patch_id}.json"
        review_path = self.recipes_dir / "review_queue" / f"{review_id}.json"
        write_json(candidate_path, patch)
        write_json(review_path, review)
        payload = {
            "problem": problem,
            "problem_fingerprint": fingerprint,
            "failure_event_ids": failure_event_ids,
            "patch_id": patch_id,
            "review_id": review_id,
            "target_recipe_id": target_recipe_id,
        }
        event, idem = self.append_event(
            "recover_candidate_created",
            payload,
            idempotency_key=idempotency_key or f"recover:{patch_id}",
            lock_exempt_reason="recover_candidate_only",
            claim_status=claim_status(
                verified=["已确认同类 failure capture 达到三次阈值，并生成 candidate patch + review item。"],
                cannot_claim=[
                    "不能说 recover 已修复问题。",
                    "不能说正式 recipe 已被修改。",
                ],
            ),
        )
        return command_result(
            "recover",
            idem,
            files_written=[str(candidate_path), str(review_path), str(self.events_path)],
            objects_created=[patch_id, review_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "problem_fingerprint": fingerprint,
                "failure_count": len(matching_failures),
                "patch_id": patch_id,
                "review_id": review_id,
                "target_recipe_id": target_recipe_id,
            },
        )

    def capabilities(self) -> dict[str, Any]:
        active_module_names = [
            "markitdown",
            "docling",
            "whisperx",
            "faster_whisper",
            "cognee",
            "fastembed",
            "ollama",
            "llama_cpp",
            "sentence_transformers",
            "transformers",
            "torch",
            "graphiti_core",
            "zep_cloud",
            "scenedetect",
            "paddleocr",
            "surya",
        ]
        module_names = [*active_module_names, "zep_cloud"]
        modules = {name: module_available(name) for name in module_names}
        project_python = self.project_python()
        project_modules = check_modules_with_python(project_python, module_names) if project_python else {}
        project_executables = project_python_executables(
            project_python,
            ["whisperx", "surya_ocr", "paddleocr", "scenedetect", "markitdown", "docling"],
        )
        adapter_runtime = external_adapter_runtime_evidence(self.recipes_dir, self.load_events())
        adapter_module_names = {
            "markitdown": "markitdown",
            "docling": "docling",
            "faster-whisper": "faster_whisper",
            "whisperx": "whisperx",
            "pyscenedetect": "scenedetect",
            "paddleocr": "paddleocr",
            "surya": "surya",
            "cognee": "cognee",
            "graphiti": "graphiti_core",
            "zep": "zep_cloud",
        }
        for adapter_name, module_name in adapter_module_names.items():
            status = adapter_runtime.setdefault(
                adapter_name,
                {
                    "runtime_verified": False,
                    "runtime_events": 0,
                    "candidate_only": True,
                    "notes": [],
                },
            )
            status["dependency_available"] = bool(modules.get(module_name) or project_modules.get(module_name))
            status["python_module_available"] = status["dependency_available"]
        if "whisperx" in adapter_runtime:
            adapter_runtime["whisperx"]["dependency_available"] = bool(project_modules.get("whisperx") and project_executables.get("whisperx"))
            adapter_runtime["whisperx"]["project_executable"] = project_executables.get("whisperx")
        if "surya" in adapter_runtime:
            adapter_runtime["surya"]["dependency_available"] = bool(
                project_modules.get("surya") and project_executables.get("surya_ocr") and shutil.which("llama-server")
            )
            adapter_runtime["surya"]["project_executable"] = project_executables.get("surya_ocr")
            adapter_runtime["surya"]["llama_server"] = shutil.which("llama-server")
        if "paddleocr" in adapter_runtime:
            adapter_runtime["paddleocr"]["project_executable"] = project_executables.get("paddleocr")
        if "pyscenedetect" in adapter_runtime:
            adapter_runtime["pyscenedetect"]["project_executable"] = project_executables.get("scenedetect")
        memory_adapters = memory_adapter_evidence(self.recipes_dir, self.load_events())
        cloud_adapters = cloud_adapter_evidence(self.recipes_dir, self.load_events())
        embedding_adapters = embedding_adapter_evidence(self.recipes_dir, self.load_events())
        binaries = {
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
            "sqlite3": shutil.which("sqlite3"),
            "llama-server": shutil.which("llama-server"),
        }
        source_refinery_tools = {
            "course-skill-draft": {
                "candidate_only": True,
                "can_write_formal_recipe": False,
                "can_accept_review": False,
                "can_claim_skill_learned": False,
                "claim_status": "timestamped_course_step_candidate_only",
                "plain": "把带时间码的课程片段和软件功能地图组合成逐步 candidate；不能写正式菜谱，不能证明 agent 已会操作。",
            },
            "completeness-audit": {
                "candidate_only": True,
                "can_write_formal_recipe": False,
                "can_accept_review": False,
                "can_claim_skill_learned": False,
                "claim_status": "structure_domain_execution_gate_only",
                "plain": "区分概念说明、逐步软件操作和 fresh-agent 执行证据；分数通过仍不等于真实软件质量通过。",
            },
            "review-triage": {
                "candidate_only": True,
                "can_write_formal_recipe": False,
                "can_accept_review": False,
                "claim_status": "candidate_judge_only",
                "plain": "把 review_queue 候选分层；只能建议动作，不能证明质量通过。",
            },
            "review-packet": {
                "candidate_only": True,
                "can_write_formal_recipe": False,
                "can_accept_review": False,
                "claim_status": "readable_review_material_only",
                "plain": "把候选整理成人能看的审核包；不能替人审完，不能读取外部 source 原文总结。",
            },
            "candidate-quality-benchmark": {
                "candidate_only": True,
                "can_write_formal_recipe": False,
                "can_accept_review": False,
                "claim_status": "candidate_quality_check_only",
                "plain": "检查候选卡片和 proposed patch 是否满足本地 case；通过也不等于值得收成正式菜谱。",
            },
            "target-suggestions": {
                "candidate_only": True,
                "can_write_formal_recipe": False,
                "can_accept_review": False,
                "claim_status": "next_narrow_task_suggestion_only",
                "plain": "从 review 历史反推下一轮窄目标；不能说明建议已经执行或质量合格。",
            },
        }
        consumption_tools = {
            "lookup-pressure": {
                "can_execute_recipe": False,
                "can_create_execution_lock": False,
                "claim_status": "lookup_applicability_pressure_only",
                "plain": "压测 lookup 适用边界；通过不等于 recipe 已执行或真实任务质量通过。",
            },
            "lock-pressure": {
                "can_execute_recipe": False,
                "can_create_execution_lock": True,
                "claim_status": "execution_lock_pressure_only",
                "plain": "压测 lookup 后能不能正确创建或阻止 execution lock；lock 不等于任务完成。",
            },
            "consumption-coverage": {
                "can_execute_recipe": False,
                "can_create_execution_lock": False,
                "claim_status": "coverage_report_only",
                "plain": "盘点正式 recipe 有没有真实 lookup/lock 压测通过证据；覆盖通过不等于任务执行或质量合格。",
            },
            "real-pressure-summary": {
                "can_execute_recipe": False,
                "can_create_execution_lock": False,
                "claim_status": "pressure_dashboard_only",
                "plain": "汇总多个真实压测项目的报告和缺口，并写出可读 Markdown；只能指导下一轮压测，不能证明任务质量通过。",
            },
            "duplicate-governance": {
                "can_execute_recipe": False,
                "can_create_execution_lock": False,
                "can_merge_or_supersede": False,
                "claim_status": "duplicate_governance_candidate_only",
                "plain": "整理重复/近重复菜谱互相抢召回的风险；只能给治理候选动作，不能自动合并、废弃或证明任务质量通过。",
            },
            "output-quality-benchmark": {
                "can_execute_recipe": False,
                "can_create_execution_lock": False,
                "can_launch_agent": False,
                "claim_status": "provided_output_quality_check_only",
                "plain": "只评分已经保存的 agent 原始输出，看它是否有证据、边界、锁定和可执行下一步；不能启动 agent，不能证明真实任务质量通过。",
            },
        }
        return {
            "ok": True,
            "action": "capabilities",
            "optional_python_modules": modules,
            "project_python": str(project_python) if project_python else None,
            "project_python_modules": project_modules,
            "project_executables": project_executables,
            "adapter_runtime": adapter_runtime,
            "memory_adapters": memory_adapters,
            "cloud_adapters": cloud_adapters,
            "embedding_adapters": embedding_adapters,
            "source_refinery_tools": source_refinery_tools,
            "consumption_tools": consumption_tools,
            "local_binaries": binaries,
            "claim_status": claim_status(
                verified=["已检查当前 Python、项目 .venv Python module spec、本地 binary path、adapter runtime receipts、memory adapter、cloud adapter、embedding adapter、source_refinery 裁判工具和 consumption 压测工具边界。"],
                missing_evidence=[
                    name
                    for name in active_module_names
                    if not (modules.get(name) or project_modules.get(name))
                ],
                cannot_claim=[
                    "不能说依赖可用就等于 adapter 已完成真实任务验收。",
                    "不能说 memory adapter candidate 已经验证或进入正式 recipe。",
                    "不能说 cloud adapter candidate 已经验证或进入正式 recipe。",
                    "不能说 embedding adapter candidate 已经验证或进入正式 recipe。",
                    "不能说 source_refinery 裁判工具已经替代人工 review。",
                    "不能说 consumption 压测工具已经执行真实任务或证明质量通过。",
                    "不能说缺失依赖已安装。",
                ],
            ),
        }

    def project_python(self) -> Path | None:
        candidates = [
            self.root / ".venv" / "bin" / "python",
            self.root / ".venv" / "Scripts" / "python.exe",
        ]
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None

    def require_project_python(self) -> Path:
        python = self.project_python()
        if not python:
            raise RecipesError(
                "AR290",
                "项目本地 Python runtime 不存在。",
                "找不到 .venv/bin/python。",
                "先创建项目本地 .venv，或用 capabilities 查看依赖状态。",
            )
        return python

    def convert_doc(self, input_path: str, *, adapter: str = "markitdown") -> dict[str, Any]:
        if adapter not in {"markitdown", "docling"}:
            raise RecipesError("AR291", "convert-doc adapter 不支持。", f"收到：{adapter}", "使用 markitdown 或 docling。")
        source_path = resolve_existing_file(self.root, input_path, "AR292", "待转换文档不存在。")
        python = self.require_project_python()
        script = adapter_script("convert_doc", adapter)
        result = run_adapter_json(
            python,
            script,
            {"path": str(source_path), "adapter": adapter},
            error_code="AR293",
            problem=f"{adapter} 文档转换失败。",
        )
        text = result.get("markdown", "")
        if not text.strip():
            raise RecipesError("AR294", "文档转换结果为空。", f"adapter={adapter}, path={source_path}", "换一个 adapter 或检查输入文件。")
        self.ensure_dirs()
        output_id = make_id("doc", adapter, str(source_path), file_sha256(source_path), sha256_text(text))
        out_dir = self.recipes_dir / "source_refinery" / "normalized" / "markdown"
        markdown_path = out_dir / f"{output_id}.md"
        metadata_path = out_dir / f"{output_id}.json"
        write_text_redacted(markdown_path, text)
        metadata = {
            "normalized_id": output_id,
            "adapter": adapter,
            "input_path": str(source_path),
            "input_hash": file_sha256(source_path),
            "markdown_path": str(markdown_path),
            "claim_limits": [
                "文档转换只证明资料已转成 Markdown。",
                "不能说转换内容已进入正式 recipe。",
                "不能说转换结果没有解析错误。",
            ],
        }
        write_json(metadata_path, metadata)
        payload = {"adapter": adapter, "normalized_id": output_id, "input_path": str(source_path), "markdown_path": str(markdown_path)}
        event, idem = self.append_event(
            "external_doc_converted",
            payload,
            idempotency_key=f"convert-doc:{adapter}:{output_id}",
            lock_exempt_reason="external_adapter_normalization",
            claim_status=claim_status(
                verified=[f"已用 {adapter} 把本地文档转换为 Markdown。"],
                cannot_claim=[
                    "不能说文档内容已被吸收进正式菜谱。",
                    "不能说转换质量已人工通过。",
                ],
            ),
        )
        return command_result(
            "convert-doc",
            idem,
            files_written=[str(markdown_path), str(metadata_path), str(self.events_path)],
            objects_created=[output_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"normalized_id": output_id, "markdown_path": str(markdown_path), "adapter": adapter},
        )

    def detect_scenes(self, video_path: str, *, adapter: str = "pyscenedetect") -> dict[str, Any]:
        if adapter != "pyscenedetect":
            raise RecipesError("AR295", "detect-scenes adapter 不支持。", f"收到：{adapter}", "使用 pyscenedetect。")
        source_path = resolve_existing_file(self.root, video_path, "AR296", "待切分视频不存在。")
        python = self.require_project_python()
        result = run_adapter_json(
            python,
            adapter_script("detect_scenes", adapter),
            {"path": str(source_path), "adapter": adapter},
            error_code="AR297",
            problem="PySceneDetect 场景切分失败。",
        )
        self.ensure_dirs()
        scene_id = make_id("scenes", adapter, str(source_path), file_sha256(source_path), result.get("scenes", []))
        out_dir = self.recipes_dir / "source_refinery" / "normalized" / "keyframes"
        scenes_path = out_dir / f"{scene_id}.json"
        metadata = {
            "scene_id": scene_id,
            "adapter": adapter,
            "input_path": str(source_path),
            "input_hash": file_sha256(source_path),
            "scenes": result.get("scenes", []),
            "scene_count": len(result.get("scenes", [])),
            "claim_limits": [
                "场景切分只证明本地视频被 adapter 分析过。",
                "不能说切分结果是最终剪辑结构。",
                "不能说视觉质量通过。",
            ],
        }
        write_json(scenes_path, metadata)
        payload = {"adapter": adapter, "scene_id": scene_id, "input_path": str(source_path), "scene_count": metadata["scene_count"]}
        event, idem = self.append_event(
            "external_scenes_detected",
            payload,
            idempotency_key=f"detect-scenes:{adapter}:{scene_id}",
            lock_exempt_reason="external_adapter_video_indexing",
            claim_status=claim_status(
                verified=["已用 PySceneDetect 对本地视频做场景切分。"],
                cannot_claim=[
                    "不能说场景切分结果已验证为正确剪辑结构。",
                    "不能说视频视觉质量通过。",
                ],
            ),
        )
        return command_result(
            "detect-scenes",
            idem,
            files_written=[str(scenes_path), str(self.events_path)],
            objects_created=[scene_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"scene_id": scene_id, "scene_count": metadata["scene_count"], "adapter": adapter},
        )

    def transcribe(self, input_path: str, *, adapter: str = "faster-whisper", model: str = "tiny.en") -> dict[str, Any]:
        if adapter not in {"faster-whisper", "whisperx"}:
            raise RecipesError("AR298", "transcribe adapter 不支持。", f"收到：{adapter}", "当前本地实现使用 faster-whisper 或 whisperx。")
        source_path = resolve_existing_file(self.root, input_path, "AR299", "待转写音视频不存在。")
        python = self.require_project_python()
        result = run_adapter_json(
            python,
            adapter_script("transcribe", adapter),
            {"path": str(source_path), "adapter": adapter, "model": model},
            error_code="AR300",
            problem=f"{adapter} 转写失败。",
            timeout=900,
        )
        self.ensure_dirs()
        transcript_id = make_id("asr", adapter, model, str(source_path), file_sha256(source_path), result.get("segments", []))
        out_dir = self.recipes_dir / "source_refinery" / "normalized" / "transcripts"
        transcript_path = out_dir / f"{transcript_id}.json"
        vtt_path = out_dir / f"{transcript_id}.vtt"
        metadata = {
            "transcript_id": transcript_id,
            "adapter": adapter,
            "model": model,
            "input_path": str(source_path),
            "input_hash": file_sha256(source_path),
            "segments": result.get("segments", []),
            "text": result.get("text", ""),
            "claim_limits": [
                "ASR 只证明本地转写已运行。",
                "不能说转写文本完全正确。",
                "不能说转写内容已进入正式 recipe。",
            ],
        }
        write_json(transcript_path, metadata)
        write_text_redacted(vtt_path, segments_as_vtt(result.get("segments", [])))
        payload = {"adapter": adapter, "model": model, "transcript_id": transcript_id, "input_path": str(source_path), "segments": len(result.get("segments", []))}
        event, idem = self.append_event(
            "external_transcript_created",
            payload,
            idempotency_key=f"transcribe:{adapter}:{model}:{transcript_id}",
            lock_exempt_reason="external_adapter_transcription",
            claim_status=claim_status(
                verified=[f"已用 {adapter} 对本地音视频做 ASR。"],
                cannot_claim=[
                    "不能说 ASR 文本完全正确。",
                    "不能说 ASR 内容已吸收进正式菜谱。",
                ],
            ),
        )
        return command_result(
            "transcribe",
            idem,
            files_written=[str(transcript_path), str(vtt_path), str(self.events_path)],
            objects_created=[transcript_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"transcript_id": transcript_id, "segments": len(result.get("segments", [])), "adapter": adapter, "model": model},
        )

    def ocr_image(self, input_path: str, *, adapter: str = "paddleocr") -> dict[str, Any]:
        if adapter not in {"paddleocr", "surya"}:
            raise RecipesError("AR302", "ocr-image adapter 不支持。", f"收到：{adapter}", "使用 paddleocr 或 surya。")
        source_path = resolve_existing_file(self.root, input_path, "AR303", "待 OCR 图片不存在。")
        python = self.require_project_python()
        result = run_adapter_json(
            python,
            adapter_script("ocr_image", adapter),
            {"path": str(source_path), "adapter": adapter},
            error_code="AR304",
            problem=f"{adapter} OCR 失败。",
            timeout=900,
        )
        text = result.get("text", "").strip()
        if not text:
            raise RecipesError(
                "AR305",
                "OCR 没有抽出文字。",
                f"adapter={adapter}, path={source_path}",
                "换一张更清晰的图片、换 adapter，或只把图片归档为 index candidate。",
            )
        self.ensure_dirs()
        ocr_id = make_id("ocr", adapter, str(source_path), file_sha256(source_path), sha256_text(text))
        out_dir = self.recipes_dir / "source_refinery" / "normalized" / "ocr"
        metadata_path = out_dir / f"{ocr_id}.json"
        text_path = out_dir / f"{ocr_id}.txt"
        metadata = {
            "ocr_id": ocr_id,
            "adapter": adapter,
            "input_path": str(source_path),
            "input_hash": file_sha256(source_path),
            "text": text,
            "texts": result.get("texts", []),
            "blocks": result.get("blocks", []),
            "claim_limits": [
                "OCR 只证明本地图片被 adapter 解析并抽出文字。",
                "不能说 OCR 文字完全正确。",
                "不能说 OCR 内容已进入正式 recipe。",
            ],
        }
        write_json(metadata_path, metadata)
        write_text_redacted(text_path, text + "\n")
        payload = {"adapter": adapter, "ocr_id": ocr_id, "input_path": str(source_path), "text_length": len(text)}
        event, idem = self.append_event(
            "external_ocr_created",
            payload,
            idempotency_key=f"ocr-image:{adapter}:{ocr_id}",
            lock_exempt_reason="external_adapter_ocr",
            claim_status=claim_status(
                verified=[f"已用 {adapter} 对本地图片做 OCR，并写入 normalized/ocr。"],
                cannot_claim=[
                    "不能说 OCR 文本完全正确。",
                    "不能说 OCR 内容已吸收进正式菜谱。",
                ],
            ),
        )
        return command_result(
            "ocr-image",
            idem,
            files_written=[str(metadata_path), str(text_path), str(self.events_path)],
            objects_created=[ocr_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"ocr_id": ocr_id, "adapter": adapter, "text_path": str(text_path), "text_length": len(text)},
        )

    def memory_index(self, *, adapter: str = "cognee") -> dict[str, Any]:
        if adapter == "graphiti":
            self.ensure_dirs()
            nodes, edges = graphiti_memory_graph(self.recipes_dir)
            if not nodes or not edges:
                raise RecipesError(
                    "AR350",
                    "没有可写入 Graphiti graph candidate index 的 source_refinery 关系。",
                    "source_refinery cards / patch drafts / review items 为空，或没有可连接的 target/source 关系。",
                    "先运行 refine、extract-cards 或 patch-draft。",
                )
            graph_dir = self.recipes_dir / "memory" / "graphiti"
            nodes_path = graph_dir / "nodes.jsonl"
            edges_path = graph_dir / "edges.jsonl"
            status_path = graph_dir / "status.json"
            write_jsonl(nodes_path, nodes)
            write_jsonl(edges_path, edges)
            status = {
                "adapter": "graphiti",
                "candidate_only": True,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "nodes_path": str(nodes_path),
                "edges_path": str(edges_path),
                "runtime_verified": True,
                "native_runtime_verified": False,
                "native_status": "not_used",
                "updated_at": now_iso(),
                "claim_limits": [
                    "Graphiti local v0 只提供 evidence relationship candidates。",
                    "没有调用原生 Graphiti 记忆/图谱服务。",
                    "不能直接写正式 recipe。",
                    "不能替代 review_queue。",
                ],
            }
            write_json(status_path, status)
            payload = {
                "adapter": adapter,
                "node_count": len(nodes),
                "edge_count": len(edges),
                "nodes_hash": sha256_json(nodes),
                "edges_hash": sha256_json(edges),
                "native_runtime_verified": False,
            }
            event, idem = self.append_event(
                "memory_graph_indexed",
                payload,
                idempotency_key=f"memory-index:{adapter}:{payload['nodes_hash']}:{payload['edges_hash']}",
                lock_exempt_reason="memory_candidate_graph_indexing",
                claim_status=claim_status(
                    verified=["已用 Graphiti adapter 本地 v0 生成 graph candidate index。"],
                    cannot_claim=[
                        "不能说 Graphiti graph candidate 已经修改正式 recipe。",
                        "不能说 Graphiti graph search 结果已经验证。",
                        "不能说已调用原生 Graphiti 长期记忆或图谱服务。",
                    ],
                ),
            )
            return command_result(
                "memory-index",
                idem,
                files_written=[str(nodes_path), str(edges_path), str(status_path), str(self.events_path)],
                objects_created=[event["event_id"]] if idem == "created" else [],
                claim_status=event["claim_status"],
                extra={
                    "adapter": adapter,
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "nodes_path": str(nodes_path),
                    "edges_path": str(edges_path),
                    "status_path": str(status_path),
                    "runtime_verified": True,
                    "native_runtime_verified": False,
                    "evidence_strength": "candidate",
                },
            )
        if adapter != "cognee":
            raise RecipesError("AR330", "memory-index adapter 不支持。", f"收到：{adapter}", "当前本地 v0 支持 cognee 或 graphiti。")
        self.ensure_dirs()
        runtime = run_cognee_runtime_probe(self.root, self.require_project_python())
        records = cognee_memory_records(self.recipes_dir)
        if not records:
            raise RecipesError(
                "AR331",
                "没有可写入 Cognee memory candidate index 的 source_refinery 记录。",
                "source_refinery cards / patch drafts / review items 为空。",
                "先运行 refine、extract-cards 或 patch-draft。",
            )
        index_path = self.recipes_dir / "memory" / "cognee" / "index.jsonl"
        status_path = self.recipes_dir / "memory" / "cognee" / "status.json"
        runtime_path = self.recipes_dir / "memory" / "cognee" / "runtime_probe.json"
        write_jsonl(index_path, records)
        write_json(runtime_path, runtime)
        status = {
            "adapter": "cognee",
            "candidate_only": True,
            "indexed_candidates": len(records),
            "index_path": str(index_path),
            "runtime_verified": bool(runtime.get("runtime_verified")),
            "runtime_probe_path": str(runtime_path),
            "updated_at": now_iso(),
            "claim_limits": [
                "Cognee memory v0 只提供 evidence candidate。",
                "不能直接写正式 recipe。",
                "不能替代 review_queue。",
            ],
        }
        write_json(status_path, status)
        payload = {
            "adapter": adapter,
            "indexed_count": len(records),
            "index_hash": sha256_json(records),
            "runtime_verified": bool(runtime.get("runtime_verified")),
            "runtime_version": runtime.get("version"),
        }
        event, idem = self.append_event(
            "memory_indexed",
            payload,
            idempotency_key=f"memory-index:{adapter}:{payload['index_hash']}",
            lock_exempt_reason="memory_candidate_indexing",
            claim_status=claim_status(
                verified=["已用 Cognee adapter 本地 v0 生成 memory candidate index。"],
                cannot_claim=[
                    "不能说 Cognee memory candidate 已经修改正式 recipe。",
                    "不能说 Cognee search 结果已经验证。",
                    "不能说 Cognee 已完成长期语义记忆或图谱闭环。",
                ],
            ),
        )
        return command_result(
            "memory-index",
            idem,
            files_written=[str(index_path), str(status_path), str(runtime_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "adapter": adapter,
                "indexed_count": len(records),
                "index_path": str(index_path),
                "status_path": str(status_path),
                "runtime": runtime,
                "evidence_strength": "candidate",
            },
        )

    def memory_search(self, query: str, *, adapter: str = "cognee", limit: int = 5) -> dict[str, Any]:
        if limit < 1:
            raise RecipesError("AR333", "memory-search limit 必须大于 0。", f"收到：{limit}", "传入正整数。")
        if adapter == "graphiti":
            graph_dir = self.recipes_dir / "memory" / "graphiti"
            try:
                nodes = read_jsonl(graph_dir / "nodes.jsonl")
                edges = read_jsonl(graph_dir / "edges.jsonl")
            except (OSError, json.JSONDecodeError) as exc:
                raise RecipesError(
                    "AR353",
                    "Graphiti graph candidate index 已损坏，停止 recall。",
                    str(exc),
                    "运行 agent-recipes recall-boundary；修复或重建该候选索引。",
                ) from exc
            records = graphiti_search_records(nodes, edges)
            if not records:
                raise RecipesError(
                    "AR352",
                    "没有 Graphiti graph candidate index。",
                    f"找不到或为空：{graph_dir}",
                    "先运行 agent-recipes memory-index --adapter graphiti。",
                )
            ranked = rank_evidence_candidates(
                query,
                records,
                priority_rules=load_lookup_priority_rules(self.recipes_dir),
            )[:limit]
            no_match_reason = recall_no_match_reason(query)
            payload = {"adapter": adapter, "query": query, "result_count": len(ranked), "graph_dir": str(graph_dir), "recommendation_status": "no_match" if no_match_reason else "candidate_only"}
            event, idem = self.append_event(
                "memory_graph_searched",
                payload,
                idempotency_key=f"memory-search:{adapter}:{sha256_json(payload)}",
                lock_exempt_reason="memory_candidate_graph_search",
                claim_status=claim_status(
                    verified=["已从 Graphiti graph candidate index 搜索候选关系。"],
                    missing_evidence=[] if ranked else ["没有命中 Graphiti graph candidate。"],
                    cannot_claim=[
                        "不能说 graph search 结果已经验证。",
                        "不能说 graph search 结果已经进入正式 recipe。",
                    ],
                ),
            )
            return {
                "ok": True,
                "action": "memory-search",
                "idempotency_status": idem,
                "adapter": adapter,
                "query": query,
                "results": ranked,
                "recommended_result": None if no_match_reason or not ranked else ranked[0],
                "recommendation_status": "no_match" if no_match_reason else "candidate_only",
                "no_match_reason": no_match_reason,
                "events": [event["event_id"]] if idem == "created" else [],
                "claim_status": event["claim_status"],
            }
        if adapter != "cognee":
            raise RecipesError("AR332", "memory-search adapter 不支持。", f"收到：{adapter}", "当前本地 v0 支持 cognee 或 graphiti。")
        index_path = self.recipes_dir / "memory" / "cognee" / "index.jsonl"
        try:
            records = read_jsonl(index_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise RecipesError(
                "AR342",
                "Cognee memory candidate index 已损坏，停止 recall。",
                str(exc),
                "运行 agent-recipes recall-boundary；修复或重建该候选索引。",
            ) from exc
        if not records:
            raise RecipesError(
                "AR334",
                "没有 Cognee memory candidate index。",
                f"找不到或为空：{index_path}",
                "先运行 agent-recipes memory-index --adapter cognee。",
            )
        ranked = rank_evidence_candidates(
            query,
            records,
            priority_rules=load_lookup_priority_rules(self.recipes_dir),
        )[:limit]
        no_match_reason = recall_no_match_reason(query)
        payload = {"adapter": adapter, "query": query, "result_count": len(ranked), "index_path": str(index_path), "recommendation_status": "no_match" if no_match_reason else "candidate_only"}
        event, idem = self.append_event(
            "memory_searched",
            payload,
            idempotency_key=f"memory-search:{adapter}:{sha256_json(payload)}",
            lock_exempt_reason="memory_candidate_search",
            claim_status=claim_status(
                verified=["已从 Cognee memory candidate index 搜索候选证据。"],
                missing_evidence=[] if ranked else ["没有命中 Cognee memory candidate。"],
                cannot_claim=[
                    "不能说 memory search 结果已经验证。",
                    "不能说 memory search 结果已经进入正式 recipe。",
                ],
            ),
        )
        return {
            "ok": True,
            "action": "memory-search",
            "idempotency_status": idem,
            "adapter": adapter,
            "query": query,
            "results": ranked,
            "recommended_result": None if no_match_reason or not ranked else ranked[0],
            "recommendation_status": "no_match" if no_match_reason else "candidate_only",
            "no_match_reason": no_match_reason,
            "events": [event["event_id"]] if idem == "created" else [],
            "claim_status": event["claim_status"],
        }

    def memory_status(self, *, adapter: str = "all") -> dict[str, Any]:
        if adapter not in {"all", "cognee", "graphiti"}:
            raise RecipesError("AR335", "memory-status adapter 不支持。", f"收到：{adapter}", "使用 all、cognee 或 graphiti。")
        adapters = memory_adapter_evidence(self.recipes_dir, self.load_events())
        if adapter in {"cognee", "graphiti"}:
            adapters = {adapter: adapters[adapter]}
        missing_evidence: list[str] = []
        if adapter == "cognee" and not adapters.get("cognee", {}).get("indexed_candidates"):
            missing_evidence.append("Cognee memory candidate index 为空或未生成。")
        if adapter == "graphiti" and not adapters.get("graphiti", {}).get("edge_count"):
            missing_evidence.append("Graphiti graph candidate index 为空或未生成。")
        if adapter == "all":
            if not adapters.get("cognee", {}).get("indexed_candidates"):
                missing_evidence.append("Cognee memory candidate index 为空或未生成。")
            if not adapters.get("graphiti", {}).get("edge_count"):
                missing_evidence.append("Graphiti graph candidate index 为空或未生成。")
        return {
            "ok": True,
            "action": "memory-status",
            "adapter": adapter,
            "adapters": adapters,
            "claim_status": claim_status(
                verified=["已读取本地 memory adapter 状态文件和事件证据。"],
                missing_evidence=missing_evidence,
                cannot_claim=[
                    "不能说 memory adapter 结果已经验证。",
                    "不能说 memory adapter 可以绕过 review_queue。",
                ],
            ),
        }

    def recall_boundary_status(self) -> dict[str, Any]:
        self.ensure_dirs()
        state = recall_boundary_state(self.recipes_dir, self.load_events())
        return {
            "ok": not state["violations"],
            "action": "recall-boundary",
            "schema_version": "1.0",
            **state,
            "claim_status": claim_status(
                verified=[
                    "已检查 Cognee、Graphiti、Qwen recall 派生索引及其候选字段。",
                    "已确认 recall 合同禁止直接写 formal recipe、创建 lock 或改变 outcome。",
                ],
                missing_evidence=list(state["violations"]),
                cannot_claim=[
                    "recall active 只说明候选索引存在，不说明召回质量合格。",
                    "recall 结果不能绕过 review_queue、strict lookup 或 exact lock。",
                    "本状态不能证明第三方 native 服务正在运行。",
                ],
            ),
        }

    def evidence_quarantine(
        self,
        *,
        action: str = "status",
        quarantine_id: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        action = action.strip().casefold()
        if action == "status":
            state = evidence_quarantine_state(self.root, self.recipes_dir)
            return {
                "ok": not state["authoritative_secret_findings"],
                "action": "evidence-quarantine",
                "operation": "status",
                **state,
                "claim_status": claim_status(
                    verified=["已扫描候选/派生 JSON、JSONL 和 .recipes 文本中的凭证模式。"],
                    missing_evidence=[item["reason"] for item in state["issues"]],
                    cannot_claim=[
                        "status 只报告问题，不会自动移动或修复文件。",
                        "quarantine 不等于证据已修复或可进入 review。",
                    ],
                ),
            }
        if action == "apply":
            state = evidence_quarantine_state(self.root, self.recipes_dir)
            if state["authoritative_secret_findings"]:
                raise RecipesError(
                    "AR451",
                    "权威账本、正式 recipe 或 lock 含敏感凭证，不能自动隔离。",
                    f"finding_count={len(state['authoritative_secret_findings'])}",
                    "先人工修复权威真相并保持 hash/lifecycle 一致，再运行 doctor。",
                )
            quarantined: list[dict[str, Any]] = []
            files_written: list[str] = []
            for issue in state["issues"]:
                source_path = self.root / issue["relative_path"]
                if not source_path.exists():
                    continue
                quarantine_id_value = make_id(
                    "quarantine",
                    issue["relative_path"],
                    issue["file_hash"],
                    issue["reason_kind"],
                )
                quarantine_dir = self.recipes_dir / "quarantine" / quarantine_id_value
                manifest_path = quarantine_dir / "manifest.json"
                if manifest_path.exists():
                    quarantined.append(read_json(manifest_path, {}))
                    continue
                quarantine_dir.mkdir(parents=True, exist_ok=True)
                payload_path = quarantine_dir / f"payload{source_path.suffix}"
                original_hash = file_sha256(source_path)
                os.replace(source_path, payload_path)
                raw_text = payload_path.read_text(encoding="utf-8", errors="replace")
                redaction_report = write_text_redacted(payload_path, raw_text)
                payload_path.chmod(0o600)
                manifest = {
                    "quarantine_id": quarantine_id_value,
                    "status": "quarantined",
                    "original_relative_path": issue["relative_path"],
                    "payload_relative_path": str(payload_path.relative_to(self.root)),
                    "original_hash": original_hash,
                    "quarantined_hash": file_sha256(payload_path),
                    "reason_kind": issue["reason_kind"],
                    "reason": issue["reason"],
                    "redaction": redaction_report,
                    "created_at": now_iso(),
                    "formal_use_allowed": False,
                }
                write_json(manifest_path, manifest)
                quarantined.append(manifest)
                files_written.extend([str(payload_path), str(manifest_path)])
            payload = {
                "quarantine_ids": [item.get("quarantine_id") for item in quarantined],
                "quarantined_count": len(quarantined),
            }
            event, idem = self.append_event(
                "evidence_quarantined",
                payload,
                idempotency_key=f"evidence-quarantine:{sha256_json(payload)}",
                lock_exempt_reason="evidence_quarantine_governance",
                claim_status=claim_status(
                    verified=["已把 malformed 或含凭证的候选文件移出 active evidence path。"],
                    cannot_claim=["不能说被隔离证据已修复、已审核或可以进入正式 recipe。"],
                ),
            )
            return command_result(
                "evidence-quarantine",
                idem,
                files_written=[*files_written, str(self.events_path)],
                objects_created=[item.get("quarantine_id") for item in quarantined if item.get("quarantine_id")],
                claim_status=event["claim_status"],
                extra={"operation": "apply", "quarantined": quarantined},
            )
        if action != "release":
            raise RecipesError("AR452", "evidence-quarantine action 不支持。", action, "使用 status、apply 或 release。")
        if not quarantine_id:
            raise RecipesError("AR453", "release 缺 quarantine id。", "没有 quarantine_id。", "先运行 evidence-quarantine --action status。")
        quarantine_dir = self.recipes_dir / "quarantine" / quarantine_id
        manifest_path = quarantine_dir / "manifest.json"
        manifest = read_json(manifest_path, {})
        if not manifest:
            raise RecipesError("AR454", "quarantine item 不存在。", quarantine_id, "先运行 evidence-quarantine --action status。")
        if manifest.get("status") == "released":
            return command_result(
                "evidence-quarantine",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(verified=["quarantine item 已释放。"]),
                extra={"operation": "release", "manifest": manifest},
            )
        payload_path = self.root / str(manifest.get("payload_relative_path") or "")
        validation_errors = validate_candidate_evidence_file(payload_path)
        if validation_errors:
            raise RecipesError(
                "AR455",
                "quarantine payload 尚未修好，拒绝释放。",
                "; ".join(validation_errors),
                f"修复 {payload_path} 后重新运行 release。",
            )
        target = safe_project_relative_path(self.root, str(manifest.get("original_relative_path") or ""))
        if target.exists():
            raise RecipesError("AR456", "原 active path 已存在，拒绝覆盖。", str(target), "先人工比较两个文件。")
        if payload_path.suffix == ".json":
            write_json(payload_path, read_json(payload_path, {}))
        elif payload_path.suffix == ".jsonl":
            write_jsonl(payload_path, read_jsonl(payload_path))
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(payload_path, target)
        manifest.update(
            {
                "status": "released",
                "released_at": now_iso(),
                "released_hash": file_sha256(target),
                "formal_use_allowed": True,
            }
        )
        write_json(manifest_path, manifest)
        event, idem = self.append_event(
            "evidence_quarantine_released",
            {"quarantine_id": quarantine_id, "restored_relative_path": str(target.relative_to(self.root))},
            idempotency_key=f"evidence-quarantine-release:{quarantine_id}:{manifest['released_hash']}",
            lock_exempt_reason="evidence_quarantine_governance",
            claim_status=claim_status(
                verified=["已验证 payload 可解析，并由显式 release 恢复 active path。"],
                cannot_claim=["释放只恢复候选证据，不等于 review accept 或正式 recipe 变更。"],
            ),
        )
        return command_result(
            "evidence-quarantine",
            idem,
            files_written=[str(target), str(manifest_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            objects_updated=[quarantine_id],
            claim_status=event["claim_status"],
            extra={"operation": "release", "manifest": manifest},
        )

    def execution_evidence_pack(
        self,
        lock_id: str,
        *,
        max_bytes: int = 65536,
        privacy: str = "project_local",
    ) -> dict[str, Any]:
        if max_bytes < 1024:
            raise RecipesError("AR460", "evidence pack budget 太小。", f"max_bytes={max_bytes}", "至少传入 1024 bytes。")
        if privacy not in {"minimal", "project_local"}:
            raise RecipesError("AR461", "evidence pack privacy 不支持。", privacy, "使用 minimal 或 project_local。")
        lock = self.validate_lock(lock_id)
        pack = build_execution_evidence_pack(
            self.root,
            self.recipes_dir,
            lock,
            self.load_events(),
            max_bytes=max_bytes,
            privacy=privacy,
        )
        pack_dir = self.recipes_dir / "evidence_packs" / pack["pack_id"]
        manifest_path = pack_dir / "manifest.json"
        context_path = pack_dir / "context.jsonl"
        if manifest_path.exists() and context_path.exists():
            existing = read_json(manifest_path, {})
            return command_result(
                "evidence-pack",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(verified=["相同 lock/budget/privacy 的 evidence pack 已存在。"]),
                extra={"pack": existing, "manifest_path": str(manifest_path), "context_path": str(context_path)},
            )
        write_jsonl(context_path, pack.pop("records"))
        write_json(manifest_path, pack)
        event, idem = self.append_event(
            "execution_evidence_pack_created",
            {
                "pack_id": pack["pack_id"],
                "lock_id": lock_id,
                "max_bytes": max_bytes,
                "used_bytes": pack["used_bytes"],
                "included_count": pack["included_count"],
                "omitted_count": pack["omitted_count"],
                "privacy": privacy,
            },
            lock_id=lock_id,
            idempotency_key=f"evidence-pack:{pack['pack_id']}",
            claim_status=claim_status(
                verified=["已生成 lock-bound、预算受限且记录省略原因的 execution evidence pack。"],
                cannot_claim=[
                    "evidence pack 不是任务完成证明或输出质量验收。",
                    "被省略证据不能用于本次正式判断，除非重建更大或更高权限的 pack。",
                ],
            ),
        )
        return command_result(
            "evidence-pack",
            idem,
            files_written=[str(context_path), str(manifest_path), str(self.events_path)],
            objects_created=[pack["pack_id"], event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"pack": pack, "manifest_path": str(manifest_path), "context_path": str(context_path)},
        )

    def memory_native_probe(self, *, adapter: str = "cognee", timeout: int = 20) -> dict[str, Any]:
        if timeout < 1:
            raise RecipesError("AR339", "memory-native-probe timeout 必须大于 0。", f"收到：{timeout}", "传入正整数秒。")
        if adapter == "graphiti":
            self.ensure_dirs()
            python = self.project_python()
            if python is None:
                runtime = graphiti_native_probe_unavailable(
                    self.root,
                    timeout=timeout,
                    reason="project_python_missing",
                    detail="找不到项目 .venv/bin/python。",
                )
            else:
                runtime = run_graphiti_native_probe(self.root, python, timeout=timeout)
            native_path = self.recipes_dir / "memory" / "graphiti" / "native_probe.json"
            write_json(native_path, runtime)
            native_status = runtime.get("native_status", "unavailable")
            payload = {
                "adapter": adapter,
                "native_status": native_status,
                "runtime_root": runtime.get("runtime_root"),
                "mode": runtime.get("mode"),
                "paths_caged": bool(runtime.get("paths_caged")),
                "network_blocked": bool(runtime.get("network_blocked")),
                "probe_hash": sha256_json(
                    {
                        "native_status": native_status,
                        "mode": runtime.get("mode"),
                        "paths_caged": bool(runtime.get("paths_caged")),
                        "network_blocked": bool(runtime.get("network_blocked")),
                        "error_type": runtime.get("error_type"),
                    }
                ),
            }
            verified = ["已完成 Graphiti native probe 本地安全检查，并写入本地 probe 状态。"]
            missing = [] if native_status == "available" else ["Graphiti native local lifecycle probe 未通过或不可用。"]
            event, idem = self.append_event(
                "memory_native_probe_checked",
                payload,
                idempotency_key=f"memory-native-probe:{adapter}:{payload['probe_hash']}",
                lock_exempt_reason="memory_native_probe_candidate_only",
                claim_status=claim_status(
                    verified=verified,
                    missing_evidence=missing,
                    cannot_claim=[
                        "不能说 Graphiti native probe 已证明生产级长期记忆。",
                        "不能说 Graphiti native probe 已验证 LLM 抽取质量。",
                        "不能说 Graphiti native probe 已经修改正式 recipe。",
                    ],
                ),
            )
            return command_result(
                "memory-native-probe",
                idem,
                files_written=[str(native_path), str(self.events_path)],
                objects_created=[event["event_id"]] if idem == "created" else [],
                claim_status=event["claim_status"],
                extra={
                    "adapter_name": adapter,
                    "native_status": native_status,
                    "runtime": runtime,
                    "native_probe_path": str(native_path),
                    "candidate_only": True,
                },
            )
        if adapter != "cognee":
            raise RecipesError("AR338", "memory-native-probe adapter 不支持。", f"收到：{adapter}", "当前支持 cognee 或 graphiti。")
        self.ensure_dirs()
        python = self.project_python()
        if python is None:
            runtime = cognee_native_probe_unavailable(
                self.root,
                timeout=timeout,
                reason="project_python_missing",
                detail="找不到项目 .venv/bin/python。",
            )
        else:
            runtime = run_cognee_native_probe(self.root, python, timeout=timeout)
        native_path = self.recipes_dir / "memory" / "cognee" / "native_probe.json"
        write_json(native_path, runtime)
        native_status = runtime.get("native_status", "unavailable")
        payload = {
            "adapter": adapter,
            "native_status": native_status,
            "runtime_root": runtime.get("runtime_root"),
            "mode": runtime.get("mode"),
            "paths_caged": bool(runtime.get("paths_caged")),
            "network_blocked": bool(runtime.get("network_blocked")),
            "probe_hash": sha256_json(
                {
                    "native_status": native_status,
                    "mode": runtime.get("mode"),
                    "paths_caged": bool(runtime.get("paths_caged")),
                    "network_blocked": bool(runtime.get("network_blocked")),
                    "error_type": runtime.get("error_type"),
                }
            ),
        }
        verified = ["已完成 Cognee native probe 安全检查，并写入本地 probe 状态。"]
        missing = [] if native_status == "available" else ["Cognee native remember/recall session probe 未通过或不可用。"]
        event, idem = self.append_event(
            "memory_native_probe_checked",
            payload,
            idempotency_key=f"memory-native-probe:{adapter}:{payload['probe_hash']}",
            lock_exempt_reason="memory_native_probe_candidate_only",
            claim_status=claim_status(
                verified=verified,
                missing_evidence=missing,
                cannot_claim=[
                    "不能说 Cognee native probe 已证明真实语义记忆质量。",
                    "不能说 Cognee native probe 已经完成长期语义记忆或图谱闭环。",
                    "不能说 Cognee native probe 可以绕过 review_queue 或直接写正式 recipe。",
                ],
            ),
        )
        return command_result(
            "memory-native-probe",
            idem,
            files_written=[str(native_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "adapter_name": adapter,
                "native_status": native_status,
                "candidate_only": True,
                "runtime": runtime,
                "native_probe_path": str(native_path),
            },
        )

    def memory_semantic_probe(self, *, adapter: str = "cognee", timeout: int = 30) -> dict[str, Any]:
        if adapter != "cognee":
            raise RecipesError("AR340", "memory-semantic-probe adapter 不支持。", f"收到：{adapter}", "当前只支持 cognee。")
        if timeout < 1:
            raise RecipesError("AR341", "memory-semantic-probe timeout 必须大于 0。", f"收到：{timeout}", "传入正整数秒。")
        self.ensure_dirs()
        python = self.project_python()
        if python is None:
            runtime = cognee_semantic_probe_unavailable(
                self.root,
                timeout=timeout,
                reason="project_python_missing",
                detail="找不到项目 .venv/bin/python。",
            )
        else:
            runtime = run_cognee_semantic_probe(self.root, python, timeout=timeout)
        semantic_path = self.recipes_dir / "memory" / "cognee" / "semantic_probe.json"
        write_json(semantic_path, runtime)
        semantic_status = runtime.get("semantic_status", "unavailable")
        payload = {
            "adapter": adapter,
            "semantic_status": semantic_status,
            "runtime_root": runtime.get("runtime_root"),
            "mode": runtime.get("mode"),
            "paths_caged": bool(runtime.get("paths_caged")),
            "network_blocked": bool(runtime.get("network_blocked")),
            "probe_hash": sha256_json(
                {
                    "semantic_status": semantic_status,
                    "mode": runtime.get("mode"),
                    "paths_caged": bool(runtime.get("paths_caged")),
                    "network_blocked": bool(runtime.get("network_blocked")),
                    "error_type": runtime.get("error_type"),
                    "dependency_status": runtime.get("dependency_status"),
                }
            ),
        }
        verified = ["已完成 Cognee semantic probe 安全门禁检查，并写入本地 probe 状态。"]
        missing = [] if semantic_status == "available" else ["Cognee semantic probe 未通过或本机缺少真实本地语义运行条件。"]
        event, idem = self.append_event(
            "memory_semantic_probe_checked",
            payload,
            idempotency_key=f"memory-semantic-probe:{adapter}:{payload['probe_hash']}",
            lock_exempt_reason="memory_semantic_probe_candidate_only",
            claim_status=claim_status(
                verified=verified,
                missing_evidence=missing,
                cannot_claim=[
                    "不能说 Cognee semantic probe 已证明生产级长期记忆。",
                    "不能说 Cognee semantic probe 已经可以替代 Agent Recipes 的 source_refinery/review_queue。",
                    "不能说 Cognee semantic probe 可以绕过 review_queue 或直接写正式 recipe。",
                ],
            ),
        )
        return command_result(
            "memory-semantic-probe",
            idem,
            files_written=[str(semantic_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "adapter_name": adapter,
                "semantic_status": semantic_status,
                "candidate_only": True,
                "runtime": runtime,
                "semantic_probe_path": str(semantic_path),
            },
        )

    def memory_semantic_configure(
        self,
        *,
        adapter: str = "cognee",
        detect_only: bool = False,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_endpoint: str | None = None,
        llm_api_key_env: str | None = "AGENT_RECIPES_DEEPSEEK_API_KEY",
        embedding_provider: str | None = None,
        embedding_model: str | None = None,
        embedding_endpoint: str | None = None,
        embedding_dimensions: int | str | None = None,
    ) -> dict[str, Any]:
        if adapter != "cognee":
            raise RecipesError("AR342", "memory-semantic-configure adapter 不支持。", f"收到：{adapter}", "当前只支持 cognee。")
        self.ensure_dirs()
        config_path = self.recipes_dir / "memory" / "cognee" / "semantic_runtime.json"
        if detect_only:
            config = detect_cognee_semantic_runtime(self.root, self.project_python())
        else:
            config = build_cognee_semantic_runtime_config(
                llm_provider=llm_provider,
                llm_model=llm_model,
                llm_endpoint=llm_endpoint,
                llm_api_key_env=llm_api_key_env,
                embedding_provider=embedding_provider,
                embedding_model=embedding_model,
                embedding_endpoint=embedding_endpoint,
                embedding_dimensions=embedding_dimensions,
            )
        write_json(config_path, config)
        payload = {
            "adapter": adapter,
            "config_status": config.get("config_status"),
            "config_hash": semantic_runtime_config_hash(config),
            "detect_only": bool(detect_only),
        }
        event, idem = self.append_event(
            "memory_semantic_runtime_configured",
            payload,
            idempotency_key=f"memory-semantic-configure:{adapter}:{payload['config_hash']}",
            lock_exempt_reason="memory_semantic_runtime_config_candidate_only",
            claim_status=claim_status(
                verified=["已写入 Cognee semantic runtime 项目本地配置/检测报告。"],
                missing_evidence=[] if config.get("config_status") == "configured" else ["尚未写入可运行的本地 semantic runtime 配置。"],
                cannot_claim=[
                    "不能说 semantic runtime 配置已经证明 Cognee 语义记忆可用。",
                    "不能说本地模型服务或 DeepSeek API 已经可用。",
                    "不能说配置可以绕过 review_queue 或直接写正式 recipe。",
                ],
            ),
        )
        return command_result(
            "memory-semantic-configure",
            idem,
            files_written=[str(config_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "adapter_name": adapter,
                "config_status": config.get("config_status"),
                "candidate_only": True,
                "semantic_runtime_config_path": str(config_path),
                "runtime_env": public_cognee_probe_env(cognee_semantic_runtime_env(config)),
                "detected": config.get("detected", {}),
            },
        )

    def cloud_configure(
        self,
        *,
        provider: str = "deepseek",
        model: str = "deepseek-v4-flash",
        pro_model: str = "deepseek-v4-pro",
        base_url: str = "https://api.deepseek.com",
        api_key_env: str = "AGENT_RECIPES_DEEPSEEK_API_KEY",
    ) -> dict[str, Any]:
        if provider != "deepseek":
            raise RecipesError("AR360", "cloud provider 不支持。", f"收到：{provider}", "当前只支持 deepseek。")
        if looks_like_secret(api_key_env):
            raise RecipesError(
                "AR360",
                "不能把 API key 直接写进配置。",
                "api-key-env 看起来像真实 secret。",
                "把 key 放进环境变量，只传环境变量名，例如 AGENT_RECIPES_DEEPSEEK_API_KEY。",
            )
        if not safe_env_name(api_key_env):
            raise RecipesError("AR360", "API key 环境变量名不安全。", f"收到：{api_key_env}", "使用大写字母、数字和下划线。")
        base_url = base_url.rstrip("/")
        if not endpoint_is_deepseek_api(base_url):
            raise RecipesError("AR360", "DeepSeek base-url 不在允许范围。", base_url, "使用 https://api.deepseek.com。")
        if model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise RecipesError("AR360", "DeepSeek model 不支持。", model, "使用 deepseek-v4-flash 或 deepseek-v4-pro。")
        if pro_model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise RecipesError("AR360", "DeepSeek pro-model 不支持。", pro_model, "使用 deepseek-v4-pro。")
        self.ensure_dirs()
        config_path = self.recipes_dir / "cloud" / "deepseek" / "config.json"
        config = {
            "provider": "deepseek",
            "config_status": "configured",
            "candidate_only": True,
            "model": model,
            "pro_model": pro_model,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "api_key_stored": False,
            "api_key_present": bool(os.environ.get(api_key_env)),
            "vision_supported": False,
            "updated_at": now_iso(),
            "safety": {
                "cloud_adapter": True,
                "explicit_network_required": True,
                "secrets_written": False,
                "text_only": True,
                "review_queue_required": True,
            },
            "claim_limits": [
                "DeepSeek cloud adapter 只处理文字，不直接处理图片或视频画面。",
                "DeepSeek cloud adapter 输出只生成 candidate cards。",
                "不能直接写正式 recipe，必须经过 review_queue。",
            ],
        }
        write_json(config_path, config)
        payload = {
            "provider": provider,
            "config_status": "configured",
            "model": model,
            "pro_model": pro_model,
            "base_url": base_url,
            "api_key_env": api_key_env,
            "api_key_present": bool(os.environ.get(api_key_env)),
            "vision_supported": False,
            "config_hash": sha256_json(redact_cloud_config(config)),
        }
        event, idem = self.append_event(
            "cloud_adapter_configured",
            payload,
            idempotency_key=f"cloud-configure:{provider}:{payload['config_hash']}",
            lock_exempt_reason="cloud_adapter_candidate_only_config",
            claim_status=claim_status(
                verified=["已写入 DeepSeek cloud adapter 项目本地配置，未写入 API key。"],
                missing_evidence=[] if payload["api_key_present"] else [f"环境变量 {api_key_env} 当前未设置。"],
                cannot_claim=[
                    "不能说 DeepSeek cloud adapter 已经调用成功。",
                    "不能说 DeepSeek cloud adapter 输出已经进入正式 recipe。",
                    "不能说 DeepSeek 支持视觉输入。",
                ],
            ),
        )
        return command_result(
            "cloud-configure",
            idem,
            files_written=[str(config_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "provider": provider,
                "config_status": "configured",
                "candidate_only": True,
                "api_key_present": payload["api_key_present"],
                "vision_supported": False,
                "config_path": str(config_path),
            },
        )

    def cloud_status(self, *, provider: str = "all") -> dict[str, Any]:
        if provider not in {"all", "deepseek"}:
            raise RecipesError("AR361", "cloud-status provider 不支持。", f"收到：{provider}", "使用 all 或 deepseek。")
        adapters = cloud_adapter_evidence(self.recipes_dir, self.load_events())
        if provider == "deepseek":
            adapters = {"deepseek": adapters["deepseek"]}
        missing = []
        if provider in {"all", "deepseek"}:
            deepseek = adapters.get("deepseek", {})
            if deepseek.get("config_status") != "configured":
                missing.append("DeepSeek cloud adapter 尚未配置。")
            elif not deepseek.get("api_key_present"):
                missing.append(f"DeepSeek API key 环境变量 {deepseek.get('api_key_env')} 当前未设置。")
        return {
            "ok": True,
            "action": "cloud-status",
            "provider": provider,
            "cloud_adapters": adapters,
            "claim_status": claim_status(
                verified=["已读取 cloud adapter 项目本地配置、secret presence 和 runtime receipts。"],
                missing_evidence=missing,
                cannot_claim=[
                    "不能说 cloud adapter 输出已经验证。",
                    "不能说 cloud adapter 可以绕过 review_queue。",
                    "不能说 DeepSeek 文本模型支持原始图片或视频画面输入。",
                ],
            ),
        }

    def cloud_refine(
        self,
        *,
        provider: str = "deepseek",
        input_path: str,
        knowledge_need_id: str,
        target_recipe_id: str,
        candidate_fields: list[str],
        response_json: str | None = None,
        allow_network: bool = False,
        model: str | None = None,
        timeout: int = 60,
    ) -> dict[str, Any]:
        if provider != "deepseek":
            raise RecipesError("AR362", "cloud-refine provider 不支持。", f"收到：{provider}", "当前只支持 deepseek。")
        if timeout < 1:
            raise RecipesError("AR362", "cloud-refine timeout 必须大于 0。", f"收到：{timeout}", "传入正整数秒。")
        self.ensure_dirs()
        source_path = resolve_existing_file(self.root, input_path, "AR363", "cloud-refine 输入文件不存在。")
        config = read_deepseek_cloud_config(self.root)
        if config.get("config_status") != "configured":
            raise RecipesError("AR364", "DeepSeek cloud adapter 尚未配置。", "找不到 .recipes/cloud/deepseek/config.json。", "先运行 cloud-configure。")
        text = source_path.read_text(encoding="utf-8")
        if not text.strip():
            raise RecipesError("AR363", "cloud-refine 输入文本为空。", str(source_path), "传入 OCR/ASR/Markdown 等文字文件。")
        allowed_fields = normalize_candidate_fields(candidate_fields)
        selected_model = model or config.get("model") or "deepseek-v4-flash"
        if selected_model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise RecipesError("AR365", "DeepSeek model 不支持。", str(selected_model), "使用 deepseek-v4-flash 或 deepseek-v4-pro。")
        replay_path: Path | None = None
        execution_mode = "live"
        if response_json:
            replay_path = resolve_existing_file(self.root, response_json, "AR363", "cloud-refine replay response 不存在。")
            response = read_json(replay_path, {})
            execution_mode = "replay"
        else:
            if not allow_network:
                raise RecipesError(
                    "AR365",
                    "cloud-refine 默认不允许联网。",
                    "没有提供 --response-json，且未显式传入 --allow-network。",
                    "本地测试用 --response-json；真实调用时显式加 --allow-network。",
                )
            response = call_deepseek_cards_api(
                text=text,
                config=config,
                model=selected_model,
                candidate_fields=allowed_fields,
                knowledge_need_id=knowledge_need_id,
                target_recipe_id=target_recipe_id,
                timeout=timeout,
            )
        cards_payload = normalize_cloud_cards_response(response)
        if not cards_payload:
            raise RecipesError("AR366", "DeepSeek cloud-refine 没有生成卡片。", "响应里没有 cards。", "检查 prompt 或 replay response。")
        run_id = make_id("cloud_run", provider, execution_mode, str(source_path), file_sha256(source_path), selected_model, cards_payload)
        run_path = self.recipes_dir / "cloud" / "deepseek" / "runs" / f"{run_id}.json"
        status_path = self.recipes_dir / "cloud" / "deepseek" / "status.json"
        cards, card_files, counts = cards_from_cloud_response(
            recipes_dir=self.recipes_dir,
            provider=provider,
            model=selected_model,
            run_id=run_id,
            source_path=source_path,
            source_hash=file_sha256(source_path),
            source_text=text,
            cards_payload=cards_payload,
            knowledge_need_id=knowledge_need_id,
            target_recipe_id=target_recipe_id,
            allowed_fields=allowed_fields,
            execution_mode=execution_mode,
        )
        for card, card_path in zip(cards, card_files, strict=True):
            write_json(card_path, card)
        index_path = self.recipes_dir / "source_refinery" / "cards" / "cards.jsonl"
        existing_cards = read_jsonl(index_path)
        existing_ids = {card.get("card_id") for card in existing_cards}
        write_jsonl(index_path, existing_cards + [card for card in cards if card["card_id"] not in existing_ids])
        run_receipt = {
            "run_id": run_id,
            "provider": provider,
            "model": selected_model,
            "execution_mode": execution_mode,
            "candidate_only": True,
            "input_path": str(source_path),
            "input_hash": file_sha256(source_path),
            "response_json": str(replay_path) if replay_path else None,
            "card_ids": [card["card_id"] for card in cards],
            "card_counts": counts,
            "network_used": execution_mode == "live",
            "api_key_stored": False,
            "vision_supported": False,
            "updated_at": now_iso(),
            "claim_limits": [
                "cloud-refine 只生成 source_refinery candidate cards。",
                "不能说 cloud-refine 输出已经验证。",
                "不能说 replay 响应等于真实 DeepSeek API 已调用。",
                "不能写正式 recipe，不能绕过 review_queue。",
            ],
        }
        write_json(run_path, run_receipt)
        write_json(
            status_path,
            {
                "provider": provider,
                "candidate_only": True,
                "runtime_verified": True,
                "runtime_events": len(
                    [
                        event
                        for event in self.load_events()
                        if event.get("event_type") == "cloud_text_refined"
                        and event.get("payload", {}).get("provider") == provider
                    ]
                )
                + 1,
                "last_run_id": run_id,
                "last_execution_mode": execution_mode,
                "last_model": selected_model,
                "updated_at": now_iso(),
            },
        )
        payload = {
            "provider": provider,
            "model": selected_model,
            "execution_mode": execution_mode,
            "run_id": run_id,
            "card_ids": [card["card_id"] for card in cards],
            "card_counts": counts,
            "input_hash": file_sha256(source_path),
            "response_hash": sha256_json(cards_payload),
        }
        event, idem = self.append_event(
            "cloud_text_refined",
            payload,
            idempotency_key=f"cloud-refine:{provider}:{run_id}",
            lock_exempt_reason="cloud_adapter_candidate_cards",
            claim_status=claim_status(
                verified=["已把 DeepSeek cloud adapter 输出写成 source_refinery candidate cards。"],
                cannot_claim=[
                    "不能说 DeepSeek cloud adapter 输出已经验证。",
                    "不能说 DeepSeek cloud adapter 已经修改正式 recipe。",
                    "不能说 replay 响应等于真实 DeepSeek API 已调用。",
                    "不能说 DeepSeek 文本模型看过原始图片或视频画面。",
                ],
            ),
        )
        return command_result(
            "cloud-refine",
            idem,
            files_written=[str(run_path), str(status_path), str(index_path), *[str(path) for path in card_files], str(self.events_path)],
            objects_created=[run_id, *[card["card_id"] for card in cards], event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "provider": provider,
                "model": selected_model,
                "execution_mode": execution_mode,
                "candidate_only": True,
                "run_id": run_id,
                "card_count": len(cards),
                "card_counts": counts,
                "card_ids": [card["card_id"] for card in cards],
                "run_path": str(run_path),
            },
        )

    def embedding_configure(
        self,
        *,
        provider: str = "qwen3",
        model: str = "qwen3-embedding:0.6b",
        endpoint: str = "http://127.0.0.1:11434/api/embed",
        dimensions: int = 1024,
    ) -> dict[str, Any]:
        if provider != "qwen3":
            raise RecipesError("AR370", "embedding provider 不支持。", f"收到：{provider}", "当前只支持 qwen3。")
        if not endpoint_is_loopback(endpoint):
            raise RecipesError("AR370", "embedding endpoint 不安全。", f"endpoint must be loopback，本次收到：{endpoint}", "使用 http://127.0.0.1 或 http://localhost。")
        if not is_qwen3_embedding_model(model):
            raise RecipesError("AR370", "embedding model 不符合当前阶段。", model, "使用 qwen3-embedding:0.6b。")
        if dimensions <= 0:
            raise RecipesError("AR370", "embedding dimensions 必须大于 0。", str(dimensions), "传入正整数。")
        self.ensure_dirs()
        config_path = self.recipes_dir / "embeddings" / "qwen3" / "config.json"
        config = {
            "provider": provider,
            "config_status": "configured",
            "candidate_only": True,
            "model": model,
            "endpoint": endpoint,
            "dimensions": dimensions,
            "updated_at": now_iso(),
            "safety": {
                "loopback_only": True,
                "cloud_blocked": True,
                "review_queue_required": True,
            },
            "claim_limits": [
                "embedding 配置只说明本地连接参数已记录。",
                "不能说 Qwen3 embedding 服务已经运行。",
                "不能说 embedding 搜索结果已经验证。",
            ],
        }
        write_json(config_path, config)
        payload = {
            "provider": provider,
            "config_status": "configured",
            "model": model,
            "endpoint": endpoint,
            "dimensions": dimensions,
            "config_hash": sha256_json({k: v for k, v in config.items() if k != "updated_at"}),
        }
        event, idem = self.append_event(
            "embedding_adapter_configured",
            payload,
            idempotency_key=f"embedding-configure:{provider}:{payload['config_hash']}",
            lock_exempt_reason="embedding_adapter_candidate_only_config",
            claim_status=claim_status(
                verified=["已写入 Qwen3 embedding 项目本地配置。"],
                cannot_claim=[
                    "不能说 Qwen3 embedding 服务已经运行。",
                    "不能说 embedding 搜索结果已经验证。",
                    "不能说 embedding 结果可以绕过 review_queue。",
                ],
            ),
        )
        return command_result(
            "embedding-configure",
            idem,
            files_written=[str(config_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "provider": provider,
                "config_status": "configured",
                "candidate_only": True,
                "model": model,
                "endpoint": endpoint,
                "dimensions": dimensions,
                "config_path": str(config_path),
            },
        )

    def embedding_status(self, *, provider: str = "all") -> dict[str, Any]:
        if provider not in {"all", "qwen3"}:
            raise RecipesError("AR371", "embedding-status provider 不支持。", f"收到：{provider}", "使用 all 或 qwen3。")
        adapters = embedding_adapter_evidence(self.recipes_dir, self.load_events())
        if provider == "qwen3":
            adapters = {"qwen3": adapters["qwen3"]}
        missing = []
        if provider in {"all", "qwen3"}:
            qwen = adapters.get("qwen3", {})
            if qwen.get("config_status") != "configured":
                missing.append("Qwen3 embedding adapter 尚未配置。")
            elif not qwen.get("runtime_verified"):
                missing.append("Qwen3 embedding index 尚未生成，或本地服务尚未验证。")
        return {
            "ok": True,
            "action": "embedding-status",
            "provider": provider,
            "embedding_adapters": adapters,
            "claim_status": claim_status(
                verified=["已读取 embedding adapter 配置、索引和 runtime receipts。"],
                missing_evidence=missing,
                cannot_claim=[
                    "不能说 embedding search 结果已经验证。",
                    "不能说 embedding search 结果已经自动进入正式 recipe。",
                    "不能说 embedding 可以替代 DeepSeek 抽卡或 review_queue。",
                ],
            ),
        }

    def embedding_index(
        self,
        *,
        provider: str = "qwen3",
        response_json: str | None = None,
        allow_loopback: bool = False,
        timeout: int = 60,
    ) -> dict[str, Any]:
        if provider != "qwen3":
            raise RecipesError("AR372", "embedding-index provider 不支持。", f"收到：{provider}", "当前只支持 qwen3。")
        if timeout < 1:
            raise RecipesError("AR372", "embedding-index timeout 必须大于 0。", str(timeout), "传入正整数秒。")
        self.ensure_dirs()
        config = read_qwen_embedding_config(self.root)
        if config.get("config_status") != "configured":
            raise RecipesError("AR373", "Qwen3 embedding adapter 尚未配置。", "找不到 .recipes/embeddings/qwen3/config.json。", "先运行 embedding-configure。")
        records = cognee_memory_records(self.recipes_dir)
        if not records:
            raise RecipesError("AR374", "没有可索引的候选记忆记录。", "source_refinery cards / patch drafts / review items 为空。", "先运行 refine/extract-cards 或 cloud-refine。")
        execution_mode = "live"
        replay: dict[str, Any] | None = None
        if response_json:
            replay_path = resolve_existing_file(self.root, response_json, "AR374", "embedding replay response 不存在。")
            replay = read_json(replay_path, {})
            execution_mode = "replay"
        elif not allow_loopback:
            raise RecipesError(
                "AR375",
                "embedding-index 默认不调用本地模型服务。",
                "没有提供 --response-json，且未显式传入 --allow-loopback。",
                "本地测试用 --response-json；真实调用 Qwen3/Ollama 时显式加 --allow-loopback。",
            )
        rows: list[dict[str, Any]] = []
        for record in records:
            vector = (
                replay_embedding_for_text(replay or {}, record.get("text", ""))
                if replay is not None
                else call_loopback_embedding(config, record.get("text", ""), timeout=timeout)
            )
            rows.append(
                {
                    **record,
                    "embedding_provider": provider,
                    "embedding_model": config.get("model"),
                    "embedding": vector,
                    "embedding_dimensions": len(vector),
                    "embedding_execution_mode": execution_mode,
                    "evidence_strength": "candidate",
                    "cannot_claim": list(
                        dict.fromkeys(
                            record.get("cannot_claim", [])
                            + [
                                "不能说 embedding record 已经验证。",
                                "不能说 embedding record 已经进入正式 recipe。",
                            ]
                        )
                    ),
                }
            )
        index_path = self.recipes_dir / "embeddings" / "qwen3" / "index.jsonl"
        status_path = self.recipes_dir / "embeddings" / "qwen3" / "status.json"
        write_jsonl(index_path, rows)
        status = {
            "provider": provider,
            "candidate_only": True,
            "runtime_verified": True,
            "indexed_count": len(rows),
            "index_path": str(index_path),
            "last_execution_mode": execution_mode,
            "model": config.get("model"),
            "dimensions": len(rows[0]["embedding"]) if rows else config.get("dimensions"),
            "updated_at": now_iso(),
        }
        write_json(status_path, status)
        payload = {
            "provider": provider,
            "indexed_count": len(rows),
            "index_hash": sha256_json([{k: v for k, v in row.items() if k != "embedding"} for row in rows]),
            "execution_mode": execution_mode,
            "model": config.get("model"),
        }
        event, idem = self.append_event(
            "embedding_indexed",
            payload,
            idempotency_key=f"embedding-index:{provider}:{payload['index_hash']}:{execution_mode}",
            lock_exempt_reason="embedding_candidate_indexing",
            claim_status=claim_status(
                verified=["已生成 Qwen3 embedding candidate index。"],
                cannot_claim=[
                    "不能说 embedding search 结果已经验证。",
                    "不能说 replay embedding 等于真实 Qwen3-Embedding 已调用。",
                    "不能说 embedding index 已经修改正式 recipe。",
                ],
            ),
        )
        return command_result(
            "embedding-index",
            idem,
            files_written=[str(index_path), str(status_path), str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "provider": provider,
                "execution_mode": execution_mode,
                "candidate_only": True,
                "indexed_count": len(rows),
                "index_path": str(index_path),
            },
        )

    def embedding_search(
        self,
        query: str,
        *,
        provider: str = "qwen3",
        response_json: str | None = None,
        allow_loopback: bool = False,
        limit: int = 5,
        timeout: int = 60,
    ) -> dict[str, Any]:
        if provider != "qwen3":
            raise RecipesError("AR376", "embedding-search provider 不支持。", f"收到：{provider}", "当前只支持 qwen3。")
        if limit < 1:
            raise RecipesError("AR376", "embedding-search limit 必须大于 0。", str(limit), "传入正整数。")
        config = read_qwen_embedding_config(self.root)
        index_path = self.recipes_dir / "embeddings" / "qwen3" / "index.jsonl"
        try:
            rows = read_jsonl(index_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise RecipesError(
                "AR380",
                "Qwen3 embedding candidate index 已损坏，停止 recall。",
                str(exc),
                "运行 agent-recipes recall-boundary；修复或重建该候选索引。",
            ) from exc
        if not rows:
            raise RecipesError("AR377", "没有 Qwen3 embedding index。", f"找不到或为空：{index_path}", "先运行 embedding-index。")
        execution_mode = "live"
        if response_json:
            replay_path = resolve_existing_file(self.root, response_json, "AR377", "embedding query replay response 不存在。")
            query_vector = replay_query_embedding(read_json(replay_path, {}))
            execution_mode = "replay"
        elif allow_loopback:
            query_vector = call_loopback_embedding(config, query, timeout=timeout)
        else:
            raise RecipesError(
                "AR378",
                "embedding-search 默认不调用本地模型服务。",
                "没有提供 --response-json，且未显式传入 --allow-loopback。",
                "本地测试用 --response-json；真实调用 Qwen3/Ollama 时显式加 --allow-loopback。",
            )
        ranked = []
        for row in rows:
            score = cosine_similarity(query_vector, normalize_embedding_vector(row.get("embedding")))
            item = {k: v for k, v in row.items() if k != "embedding"}
            item["score"] = score
            ranked.append(item)
        ranked.sort(key=lambda item: (item["score"], item.get("record_id") or ""), reverse=True)
        ranked = ranked[:limit]
        no_match_reason = recall_no_match_reason(query)
        payload = {"provider": provider, "query": query, "result_count": len(ranked), "execution_mode": execution_mode, "recommendation_status": "no_match" if no_match_reason else "candidate_only"}
        event, idem = self.append_event(
            "embedding_searched",
            payload,
            idempotency_key=f"embedding-search:{provider}:{sha256_json(payload)}",
            lock_exempt_reason="embedding_candidate_search",
            claim_status=claim_status(
                verified=["已从 Qwen3 embedding candidate index 搜索候选证据。"],
                missing_evidence=[] if ranked else ["没有命中 embedding candidate。"],
                cannot_claim=[
                    "不能说 embedding search 结果已经验证。",
                    "不能说 embedding search 结果已经自动进入正式 recipe。",
                    "不能说 replay embedding 等于真实 Qwen3-Embedding 已调用。",
                ],
            ),
        )
        return {
            "ok": True,
            "action": "embedding-search",
            "idempotency_status": idem,
            "provider": provider,
            "query": query,
            "execution_mode": execution_mode,
            "candidate_only": True,
            "results": ranked,
            "recommended_result": None if no_match_reason or not ranked else ranked[0],
            "recommendation_status": "no_match" if no_match_reason else "candidate_only",
            "no_match_reason": no_match_reason,
            "events": [event["event_id"]] if idem == "created" else [],
            "claim_status": event["claim_status"],
        }

    def quality_benchmark(
        self,
        *,
        qwen_response_json: str | None = None,
        allow_loopback: bool = False,
        limit: int = 5,
        timeout: int = 60,
    ) -> dict[str, Any]:
        if limit < 1:
            raise RecipesError("AR390", "quality-benchmark limit 必须大于 0。", str(limit), "传入正整数。")
        self.ensure_dirs()
        cases: list[dict[str, Any]] = []
        cases.append(
            quality_rank_case(
                "source_search_source_trace",
                "source",
                "source_trace",
                self.search_records(kind="all"),
                ["source_trace"],
                limit=limit,
            )
        )
        cognee_records = read_jsonl(self.recipes_dir / "memory" / "cognee" / "index.jsonl")
        cases.append(
            quality_rank_case(
                "cognee_memory_review_gate",
                "cognee",
                "patch draft review accept",
                cognee_records,
                ["patch", "review"],
                limit=limit,
            )
        )
        graph_dir = self.recipes_dir / "memory" / "graphiti"
        graphiti_records = graphiti_search_records(read_jsonl(graph_dir / "nodes.jsonl"), read_jsonl(graph_dir / "edges.jsonl"))
        cases.append(
            quality_rank_case(
                "graphiti_patch_review_relationship",
                "graphiti",
                "patch draft review target recipe",
                graphiti_records,
                ["patch", "review", "recipe"],
                limit=limit,
            )
        )
        combined_records = self.search_records(kind="all") + cognee_records + graphiti_records
        cases.append(
            quality_false_recall_case(
                "false_recall_public_release",
                "combined",
                "tax filing payroll invoice bank transfer",
                combined_records,
                limit=limit,
            )
        )
        cases.append(self.qwen_quality_case(qwen_response_json=qwen_response_json, allow_loopback=allow_loopback, limit=limit, timeout=timeout))
        cases.append(quality_review_flow_case(self.recipes_dir))
        passed = sum(1 for case in cases if case.get("status") == "passed")
        failed = sum(1 for case in cases if case.get("status") == "failed")
        blocked = sum(1 for case in cases if case.get("status") == "blocked")
        skipped = sum(1 for case in cases if case.get("status") == "skipped")
        scorable = passed + failed
        score = (passed / scorable) if scorable else 0.0
        graphiti_status = read_json(self.recipes_dir / "memory" / "graphiti" / "status.json", {})
        report = {
            "ok": failed == 0,
            "action": "quality-benchmark",
            "candidate_only": True,
            "quality_score": score,
            "summary": {
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "skipped": skipped,
                "scorable": scorable,
            },
            "cases": cases,
            "production_notes": [
                "本地基准只测候选检索和 review gate，不证明生产级质量。",
                "Graphiti 当前本地 probe 使用 Kuzu；如果走生产级图谱，后续要评估 Neo4j 或 FalkorDB。",
                "Qwen/Cognee/Graphiti 结果仍是 candidate，不能绕过 review_queue。",
            ],
            "graphiti_native_warnings": graphiti_status.get("native_warnings", []),
        }
        report_hash = sha256_json(report)
        report_id = make_id("quality", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "quality_benchmark_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "quality_score": score,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "skipped": skipped,
            },
            idempotency_key=f"quality-benchmark:{report_hash}",
            lock_exempt_reason="local_quality_benchmark",
            claim_status=claim_status(
                verified=["已生成本地质量基准报告。"],
                missing_evidence=quality_missing_evidence(cases),
                cannot_claim=[
                    "不能说一次本地基准证明生产级质量。",
                    "不能说 candidate 检索结果已经是真实结论。",
                    "不能说 Qwen/Cognee/Graphiti 可以绕过 review_queue。",
                ],
            ),
        )
        return command_result(
            "quality-benchmark",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def recall_quality_benchmark(
        self,
        *,
        cases_path: str,
        backends: list[str] | None = None,
        allow_loopback: bool = False,
        limit: int = 5,
        min_score: int = 2,
        qwen_min_score: float = 0.55,
        timeout: int = 60,
    ) -> dict[str, Any]:
        from agent_recipes.recall_quality import run_recall_quality_benchmark

        return run_recall_quality_benchmark(
            self,
            cases_path=cases_path,
            backends=backends or ["core", "cognee", "graphiti", "qwen"],
            allow_loopback=allow_loopback,
            limit=limit,
            min_score=min_score,
            qwen_min_score=qwen_min_score,
            timeout=timeout,
            candidate_ranker=lambda query, records: rank_evidence_candidates(
                query,
                records,
                priority_rules=load_lookup_priority_rules(self.recipes_dir),
            ),
            embedding_caller=lambda config, text: call_loopback_embedding(config, text, timeout=timeout),
            embedding_config_reader=read_qwen_embedding_config,
            cosine_similarity=cosine_similarity,
        )

    def self_run_benchmark(
        self,
        *,
        query: str,
        knowledge_need_id: str,
        target_recipe_id: str,
        candidate_fields: list[str],
        min_cards: int = 1,
        limit: int = 20,
        kind: str = "all",
        scan_depth: str = "shallow",
        source_path_contains: list[str] | None = None,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        if not query.strip():
            raise RecipesError("AR394", "self-run-benchmark query 不能为空。", "没有 query 就无法检索候选 source。", "传入 --query。")
        if min_cards < 1:
            raise RecipesError("AR395", "self-run-benchmark min-cards 必须大于 0。", str(min_cards), "传入正整数。")
        formal_recipe_path = recipe_path_for(self.recipes_dir, target_recipe_id)
        formal_existed_before = formal_recipe_path.exists()

        scan_result = self.scan(depth=scan_depth)
        search_result = self.search(query, limit=limit, kind=kind, source_path_contains=source_path_contains)
        refined = self.refine(
            query=query,
            knowledge_need_id=knowledge_need_id,
            target_recipe_id=target_recipe_id,
            candidate_fields=candidate_fields,
            limit=limit,
            kind=kind,
            source_path_contains=source_path_contains,
        )
        cards = self.extract_cards(refinement_id=str(refined["refinement_id"]))
        drafted = self.patch_draft(target_recipe_id=target_recipe_id)

        review_id = str(drafted.get("review_id") or "")
        patch_draft_id = str(drafted.get("patch_draft_id") or "")
        review = read_json(self.recipes_dir / "review_queue" / f"{review_id}.json", {}) if review_id else {}
        patch_draft = read_json(self.recipes_dir / "source_refinery" / "patch_drafts" / f"{patch_draft_id}.json", {}) if patch_draft_id else {}
        card_docs = [
            read_card_by_id(self.recipes_dir, card_id)
            for card_id in cards.get("card_ids", [])
        ]
        cases = self_run_benchmark_cases(
            scan_result=scan_result,
            search_result=search_result,
            refined=refined,
            cards=card_docs,
            card_counts=cards.get("card_counts", {}),
            patch_draft=patch_draft,
            review=review,
            min_cards=min_cards,
            formal_existed_before=formal_existed_before,
            formal_exists_after=formal_recipe_path.exists(),
        )
        passed = sum(1 for case in cases if case.get("status") == "passed")
        failed = sum(1 for case in cases if case.get("status") == "failed")
        blocked = sum(1 for case in cases if case.get("status") == "blocked")
        scorable = passed + failed
        score = (passed / scorable) if scorable else 0.0
        report = {
            "ok": failed == 0 and blocked == 0,
            "action": "self-run-benchmark",
            "candidate_only": True,
            "self_run_score": score,
            "summary": {
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "scorable": scorable,
                "case_count": len(cases),
            },
            "query": query,
            "knowledge_need_id": knowledge_need_id,
            "target_recipe_id": target_recipe_id,
            "candidate_fields": normalize_candidate_fields(candidate_fields),
            "source_path_contains": normalize_source_path_filters(source_path_contains),
            "refinement_id": refined.get("refinement_id"),
            "patch_draft_id": patch_draft_id,
            "review_id": review_id,
            "cases": cases,
            "production_notes": [
                "self-run-benchmark 只证明 Agent Recipes 自己跑了 source_refinery 链路。",
                "产物必须停在 review_queue；不能自动变成正式 recipe。",
                "这不是人工质量验收，也不是生产级覆盖率证明。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("self_run", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "self_run_benchmark_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "self_run_score": score,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "review_id": review_id,
            },
            idempotency_key=f"self-run-benchmark:{report_hash}",
            lock_exempt_reason="local_self_run_benchmark",
            claim_status=claim_status(
                verified=["已生成 Agent Recipes 自跑链路基准报告。"],
                missing_evidence=benchmark_missing_evidence(cases),
                cannot_claim=[
                    "不能说 self-run-benchmark 通过就证明菜谱质量通过。",
                    "不能说 review_queue item 已经被接受。",
                    "不能说 Codex 人工总结等于系统自跑。",
                ],
            ),
        )
        return command_result(
            "self-run-benchmark",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def repeat_error_benchmark(self, *, cases_path: str, min_cases: int = 5, min_improvements: int = 3) -> dict[str, Any]:
        self.ensure_dirs()
        if min_cases < 1 or min_improvements < 1:
            raise RecipesError("AR396", "repeat-error-benchmark 阈值必须大于 0。", f"min_cases={min_cases}; min_improvements={min_improvements}", "传入正整数。")
        resolved_cases_path = resolve_existing_file(self.root, cases_path, "AR397", "repeat-error-benchmark cases 文件不存在。")
        cases_doc = read_json(resolved_cases_path, {})
        raw_cases = cases_doc.get("cases") if isinstance(cases_doc, dict) else cases_doc
        if not isinstance(raw_cases, list) or not raw_cases:
            raise RecipesError("AR398", "repeat-error-benchmark cases 必须是非空列表。", str(resolved_cases_path), "写入 cases 数组。")
        cases = [repeat_error_case(raw_case) for raw_case in raw_cases]
        evidence_metadata = repeat_error_evidence_metadata(cases_doc)
        improved = sum(1 for case in cases if case.get("status") == "improved")
        failed = sum(1 for case in cases if case.get("status") == "failed")
        blocked = sum(1 for case in cases if case.get("status") == "blocked")
        scorable = improved + failed
        score = (improved / scorable) if scorable else 0.0
        enough_cases = len(cases) >= min_cases
        enough_improvements = improved >= min_improvements
        threshold_cases = [
            benchmark_case(
                "min_cases",
                "passed" if enough_cases else "failed",
                f"cases={len(cases)}; min_cases={min_cases}",
                [] if enough_cases else [f"repeat-error cases 不足：{len(cases)} / {min_cases}"],
            ),
            benchmark_case(
                "min_improvements",
                "passed" if enough_improvements else "failed",
                f"improved={improved}; min_improvements={min_improvements}",
                [] if enough_improvements else [f"旧错改善不足：{improved} / {min_improvements}"],
            ),
        ]
        all_cases = cases + threshold_cases
        ok = failed == 0 and blocked == 0 and enough_cases and enough_improvements
        report_claim_status = repeat_error_claim_status(all_cases, evidence_metadata)
        report = {
            "ok": ok,
            "action": "repeat-error-benchmark",
            "candidate_only": True,
            "evidence_mode": evidence_metadata["evidence_mode"],
            "evidence_notes": evidence_metadata["notes"],
            "raw_evidence_paths": evidence_metadata["raw_evidence_paths"],
            "ab_outputs_generated_by_benchmark": False,
            "fresh_generation_in_this_run": False,
            "repeat_error_score": score,
            "summary": {
                "improved": improved,
                "failed": failed,
                "blocked": blocked,
                "scorable": scorable,
                "case_count": len(cases),
                "min_cases": min_cases,
                "min_improvements": min_improvements,
            },
            "cases_path": str(resolved_cases_path),
            "cases": all_cases,
            "production_notes": [
                "repeat-error-benchmark 只评分已提供的 A/B 输出证据，不启动 fresh agent。",
                "无菜谱输出和有菜谱输出必须来自外部真实对照或明确 fixture。",
                "通过不等于未来所有 agent 都不会重复旧错。",
            ],
            "claim_status": report_claim_status,
        }
        report_hash = sha256_json(report)
        report_id = make_id("repeat_error", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "repeat_error_benchmark_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "repeat_error_score": score,
                "improved": improved,
                "failed": failed,
                "blocked": blocked,
            },
            idempotency_key=f"repeat-error-benchmark:{report_hash}",
            lock_exempt_reason="local_repeat_error_benchmark",
            claim_status=report_claim_status,
        )
        return command_result(
            "repeat-error-benchmark",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def output_quality_benchmark(self, *, cases_path: str, min_cases: int = 1, min_passed: int = 1) -> dict[str, Any]:
        self.ensure_dirs()
        if min_cases < 1 or min_passed < 1:
            raise RecipesError("AR470", "output-quality-benchmark 阈值必须大于 0。", f"min_cases={min_cases}; min_passed={min_passed}", "传入正整数。")
        resolved_cases_path = resolve_existing_file(self.root, cases_path, "AR471", "output-quality-benchmark cases 文件不存在。")
        cases_doc = read_json(resolved_cases_path, {})
        raw_cases = cases_doc.get("cases") if isinstance(cases_doc, dict) else cases_doc
        if not isinstance(raw_cases, list) or not raw_cases:
            raise RecipesError("AR472", "output-quality-benchmark cases 必须是非空列表。", str(resolved_cases_path), "写入 cases 数组。")
        cases = [output_quality_case(raw_case) for raw_case in raw_cases]
        evidence_metadata = output_quality_evidence_metadata(cases_doc)
        passed = sum(1 for case in cases if case.get("status") == "passed")
        failed = sum(1 for case in cases if case.get("status") == "failed")
        blocked = sum(1 for case in cases if case.get("status") == "blocked")
        scorable = passed + failed
        score = (passed / scorable) if scorable else 0.0
        enough_cases = len(cases) >= min_cases
        enough_passed = passed >= min_passed
        threshold_cases = [
            benchmark_case(
                "min_cases",
                "passed" if enough_cases else "failed",
                f"cases={len(cases)}; min_cases={min_cases}",
                [] if enough_cases else [f"output-quality cases 不足：{len(cases)} / {min_cases}"],
            ),
            benchmark_case(
                "min_passed",
                "passed" if enough_passed else "failed",
                f"passed={passed}; min_passed={min_passed}",
                [] if enough_passed else [f"真实输出质量通过数不足：{passed} / {min_passed}"],
            ),
        ]
        all_cases = cases + threshold_cases
        ok = failed == 0 and blocked == 0 and enough_cases and enough_passed
        report_claim_status = output_quality_claim_status(all_cases, evidence_metadata)
        report = {
            "ok": ok,
            "action": "output-quality-benchmark",
            "candidate_only": True,
            "evidence_mode": evidence_metadata["evidence_mode"],
            "evidence_notes": evidence_metadata["notes"],
            "raw_evidence_paths": evidence_metadata["raw_evidence_paths"],
            "outputs_generated_by_benchmark": False,
            "fresh_generation_in_this_run": False,
            "output_quality_score": score,
            "summary": {
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "scorable": scorable,
                "case_count": len(cases),
                "min_cases": min_cases,
                "min_passed": min_passed,
            },
            "cases_path": str(resolved_cases_path),
            "cases": all_cases,
            "production_notes": [
                "output-quality-benchmark 只评分已保存的 agent 输出，不启动 agent。",
                "它检查证据、边界、锁定和可执行性信号，不替代人工验收。",
                "通过不等于真实 SampleProject 或其他项目任务已完成，也不等于视频/音频质量通过。",
            ],
            "claim_status": report_claim_status,
        }
        report_hash = sha256_json(report)
        report_id = make_id("output_quality", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "output_quality_benchmark_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "output_quality_score": score,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
            },
            idempotency_key=f"output-quality-benchmark:{report_hash}",
            lock_exempt_reason="local_output_quality_benchmark",
            claim_status=report_claim_status,
        )
        return command_result(
            "output-quality-benchmark",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def lookup_pressure(self, *, cases_path: str) -> dict[str, Any]:
        self.ensure_dirs()
        resolved_cases_path = resolve_existing_file(self.root, cases_path, "AR392", "lookup-pressure cases 文件不存在。")
        cases_doc = read_json(resolved_cases_path, {})
        raw_cases = cases_doc.get("cases") if isinstance(cases_doc, dict) else cases_doc
        if not isinstance(raw_cases, list) or not raw_cases:
            raise RecipesError("AR393", "lookup-pressure cases 必须是非空列表。", str(resolved_cases_path), "写入 cases 数组。")

        cases = [lookup_pressure_case(self, raw_case) for raw_case in raw_cases]
        passed = sum(1 for case in cases if case.get("status") == "passed")
        failed = sum(1 for case in cases if case.get("status") == "failed")
        blocked = sum(1 for case in cases if case.get("status") == "blocked")
        scorable = passed + failed
        score = (passed / scorable) if scorable else 0.0
        duplicate_shadow_count = sum(1 for case in cases if case.get("shadowed_expected_recipe"))
        official_pressure_evidence = pressure_cases_path_is_project_local(self.root, resolved_cases_path)
        report = {
            "ok": failed == 0 and blocked == 0,
            "action": "lookup-pressure",
            "candidate_only": True,
            "official_pressure_evidence": official_pressure_evidence,
            "pressure_evidence_scope": "project" if official_pressure_evidence else "external_cases",
            "pressure_score": score,
            "summary": {
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "scorable": scorable,
                "case_count": len(cases),
                "duplicate_shadow_count": duplicate_shadow_count,
            },
            "cases_path": str(resolved_cases_path),
            "cases": cases,
            "production_notes": [
                "lookup-pressure 只测 recipe lookup 适用边界，不证明真实任务质量。",
                "负例失败通常表示当前 lookup 可能把窄菜谱过度套用到不该覆盖的场景。",
                "失败报告只能进入 review/改进，不得直接改正式 recipe。",
                "duplicate_shadow_count 表示期望 recipe 也在候选里，但被另一条更高或同分 recipe 盖住；这需要人工治理 merge/supersede/priority，不自动改正式 recipe。",
                "cases_path 不在项目目录内时，该报告只能作为模拟记录，不能冲抵 real-pressure-summary 缺口。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("lookup_pressure", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "lookup_pressure_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "pressure_score": score,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "duplicate_shadow_count": duplicate_shadow_count,
                "official_pressure_evidence": official_pressure_evidence,
            },
            idempotency_key=f"lookup-pressure:{report_hash}",
            lock_exempt_reason="local_lookup_pressure",
            claim_status=claim_status(
                verified=["已生成 lookup 适用边界压力测试报告。"],
                missing_evidence=lookup_pressure_missing_evidence(cases),
                cannot_claim=[
                    "不能说 lookup-pressure 通过就证明真实任务质量。",
                    "不能说 lookup 命中等于 recipe 适用于该任务。",
                    "不能把过度套用失败直接改成正式 recipe。",
                ],
            ),
        )
        return command_result(
            "lookup-pressure",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def lock_pressure(self, *, cases_path: str) -> dict[str, Any]:
        self.ensure_dirs()
        resolved_cases_path = resolve_existing_file(self.root, cases_path, "AR460", "lock-pressure cases 文件不存在。")
        cases_doc = read_json(resolved_cases_path, {})
        raw_cases = cases_doc.get("cases") if isinstance(cases_doc, dict) else cases_doc
        if not isinstance(raw_cases, list) or not raw_cases:
            raise RecipesError("AR461", "lock-pressure cases 必须是非空列表。", str(resolved_cases_path), "写入 cases 数组。")

        cases = [lock_pressure_case(self, raw_case) for raw_case in raw_cases]
        passed = sum(1 for case in cases if case.get("status") == "passed")
        failed = sum(1 for case in cases if case.get("status") == "failed")
        blocked = sum(1 for case in cases if case.get("status") == "blocked")
        scorable = passed + failed
        score = (passed / scorable) if scorable else 0.0
        duplicate_shadow_count = sum(1 for case in cases if case.get("shadowed_expected_recipe"))
        official_pressure_evidence = pressure_cases_path_is_project_local(self.root, resolved_cases_path)
        report = {
            "ok": failed == 0 and blocked == 0,
            "action": "lock-pressure",
            "execution_lock_only": True,
            "official_pressure_evidence": official_pressure_evidence,
            "pressure_evidence_scope": "project" if official_pressure_evidence else "external_cases",
            "lock_pressure_score": score,
            "summary": {
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "scorable": scorable,
                "case_count": len(cases),
                "locks_created_or_reused": sum(1 for case in cases if case.get("lock_id")),
                "locks_prevented": sum(1 for case in cases if case.get("lock_status") == "prevented"),
                "duplicate_shadow_count": duplicate_shadow_count,
            },
            "cases_path": str(resolved_cases_path),
            "cases": cases,
            "production_notes": [
                "lock-pressure 只验证 lookup --strict 后能不能创建 execution lock。",
                "通过不等于 recipe 已执行，也不等于真实任务质量通过。",
                "负例必须阻止 lock，防止窄菜谱被套到不该覆盖的任务。",
                "duplicate_shadow_count 表示期望 recipe 也在候选里，但被另一条更高或同分 recipe 盖住；这需要人工治理 merge/supersede/priority，不自动创建期望 lock。",
                "cases_path 不在项目目录内时，该报告只能作为模拟记录，不能冲抵 real-pressure-summary 缺口。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("lock_pressure", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "lock_pressure_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "lock_pressure_score": score,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "lock_ids": [case.get("lock_id") for case in cases if case.get("lock_id")],
                "duplicate_shadow_count": duplicate_shadow_count,
                "official_pressure_evidence": official_pressure_evidence,
            },
            idempotency_key=f"lock-pressure:{report_hash}",
            lock_exempt_reason="local_lock_pressure",
            claim_status=claim_status(
                verified=["已生成 lock 消费压力测试报告。"],
                missing_evidence=lock_pressure_missing_evidence(cases),
                cannot_claim=[
                    "不能说 lock-pressure 通过就证明 recipe 已执行。",
                    "不能说 lock-pressure 通过就证明真实任务质量。",
                    "不能说 execution lock 等于用户验收通过。",
                ],
            ),
        )
        return command_result(
            "lock-pressure",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def consumption_coverage(self) -> dict[str, Any]:
        self.ensure_dirs()
        recipes = self.load_recipes()
        lifecycle = recipe_lifecycle_state(self.load_events())
        recipe_ids, inactive_recipe_ids = active_recipe_ids_for_consumption(
            recipes,
            retired_recipe_ids=set(lifecycle["retired_recipe_ids"]),
        )
        coverage_scope = consumption_coverage_scope(self.root, recipe_ids)
        coverage_recipe_ids = coverage_scope["recipe_ids"]
        coverage = consumption_coverage_rows(self.root, self.recipes_dir, coverage_recipe_ids)
        missing_lookup = [row["recipe_id"] for row in coverage if not row["lookup_passed"]]
        missing_lock = [row["recipe_id"] for row in coverage if not row["lock_passed"]]
        report = {
            "ok": not missing_lookup and not missing_lock,
            "action": "consumption-coverage",
            "candidate_only": True,
            "summary": {
                "recipe_count": len(coverage_recipe_ids),
                "active_recipe_count": len(recipe_ids),
                "lookup_passed_covered": len(coverage_recipe_ids) - len(missing_lookup),
                "lock_passed_covered": len(coverage_recipe_ids) - len(missing_lock),
                "missing_lookup_count": len(missing_lookup),
                "missing_lock_count": len(missing_lock),
                "inactive_recipe_count": len(inactive_recipe_ids),
            },
            "coverage_scope": coverage_scope,
            "recipes": coverage,
            "active_recipe_ids": recipe_ids,
            "coverage_recipe_ids": coverage_recipe_ids,
            "inactive_recipe_ids": inactive_recipe_ids,
            "missing_lookup_recipe_ids": missing_lookup,
            "missing_lock_recipe_ids": missing_lock,
            "production_notes": [
                "consumption-coverage 只盘点 lookup/lock pressure 覆盖证据。",
                "默认按全部 active recipe 计算；只有 case 文件显式声明 coverage_scope 时才按诊断范围计算。",
                "被显式 supersede 或同一 _vN 系列中低于最新版本的 recipe 只保留为历史，不强制作当前消费覆盖。",
                "正例 lock-pressure 已经包含 strict lookup，所以也计入 lookup 覆盖。",
                "通过不等于 recipe 已执行，也不等于真实任务质量通过。",
                "cases 文件本身不是通过证据；必须有 passed report 才算覆盖。",
                "scoped coverage 通过也只能证明该诊断范围，不证明项目全部 recipe 都被覆盖。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("consumption_coverage", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "consumption_coverage_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "recipe_count": len(coverage_recipe_ids),
                "active_recipe_count": len(recipe_ids),
                "coverage_scope": coverage_scope.get("mode"),
                "missing_lookup_count": len(missing_lookup),
                "missing_lock_count": len(missing_lock),
            },
            idempotency_key=f"consumption-coverage:{report_hash}",
            lock_exempt_reason="local_consumption_coverage",
            claim_status=claim_status(
                verified=["已生成真实消费覆盖报告。"],
                missing_evidence=consumption_coverage_missing_evidence(report),
                cannot_claim=[
                    "不能说 consumption coverage 通过就证明 recipe 已执行。",
                    "不能说 consumption coverage 通过就证明真实任务质量。",
                    "不能说有 cases 文件就等于覆盖已通过。",
                    "scoped coverage 不能代表项目全部 recipe 已覆盖。",
                ],
            ),
        )
        return command_result(
            "consumption-coverage",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def real_pressure_summary(self, *, projects_root: str = ".recipes_real_tests", name_contains: str | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        root_path = Path(projects_root)
        if not root_path.is_absolute():
            root_path = self.root / root_path
        root_path = root_path.resolve()
        if not root_path.exists() or not root_path.is_dir():
            raise RecipesError(
                "AR472",
                "real-pressure-summary projects root 不存在。",
                str(root_path),
                "传入存在的 --projects-root，或先生成 .recipes_real_tests。",
            )

        projects = real_pressure_project_rows(root_path, name_contains=name_contains)
        pressure_gaps = real_pressure_gaps(projects)
        quality_warnings = real_pressure_quality_warnings(projects)
        manual_governance_items = real_pressure_manual_governance_items(projects)
        manual_governance_readiness = real_pressure_manual_governance_readiness(manual_governance_items)
        report = {
            "ok": True,
            "action": "real-pressure-summary",
            "candidate_only": True,
            "projects_root": str(root_path),
            "name_contains": name_contains,
            "summary": {
                "project_count": len(projects),
                "projects_with_recipes": sum(1 for project in projects if project["recipe_count"] > 0),
                "formal_recipe_count": sum(int(project["recipe_count"]) for project in projects),
                "projects_with_consumption_coverage": sum(
                    1 for project in projects if project["reports"]["consumption_coverage"]["report_count"] > 0
                ),
                "projects_with_latest_coverage_ok": sum(
                    1 for project in projects if project["reports"]["consumption_coverage"]["latest_ok"] is True
                ),
                "projects_with_output_quality": sum(
                    1 for project in projects if project["reports"]["output_quality"]["report_count"] > 0
                ),
                "projects_with_latest_output_quality_ok": sum(
                    1 for project in projects if project["reports"]["output_quality"]["latest_ok"] is True
                ),
                "pressure_gap_count": len(pressure_gaps),
                "ignored_simulation_report_count": real_pressure_ignored_simulation_report_count(projects),
                "quality_warning_count": len(quality_warnings),
                "manual_governance_required_count": len(manual_governance_items),
                "manual_governance_ready_count": sum(
                    1 for item in manual_governance_readiness if item.get("ready_for_human_decision") is True
                ),
                "manual_governance_not_ready_count": sum(
                    1 for item in manual_governance_readiness if item.get("ready_for_human_decision") is not True
                ),
            },
            "projects": projects,
            "pressure_gaps": pressure_gaps,
            "quality_warnings": quality_warnings,
            "manual_governance_items": manual_governance_items,
            "manual_governance_readiness": manual_governance_readiness,
            "production_notes": [
                "real-pressure-summary 只汇总本地真实压测报告，不重新拆资料。",
                "缺口列表用于决定下一轮压测范围，不能自动接受 review 或改正式 recipe。",
                "coverage/benchmark 通过不等于真实任务执行通过，也不等于最终质量合格。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("real_pressure_summary", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        markdown_path = self.recipes_dir / "reports" / f"{report_id}.md"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["markdown_path"] = str(markdown_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        write_text_redacted(markdown_path, real_pressure_summary_markdown(report))
        event, idem = self.append_event(
            "real_pressure_summary_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "project_count": len(projects),
                "pressure_gap_count": len(pressure_gaps),
            },
            idempotency_key=f"real-pressure-summary:{report_hash}",
            lock_exempt_reason="local_real_pressure_summary",
            claim_status=claim_status(
                verified=["已生成真实压测总看板报告。"],
                missing_evidence=real_pressure_missing_evidence(projects, pressure_gaps),
                cannot_claim=[
                    "不能说 real-pressure-summary 通过就证明真实任务质量。",
                    "不能说缺口列表已经被修复。",
                    "不能说汇总报告替代人工 review 或 live task 验收。",
                ],
            ),
        )
        return command_result(
            "real-pressure-summary",
            idem,
            files_written=[str(report_path), str(markdown_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def duplicate_governance(self) -> dict[str, Any]:
        self.ensure_dirs()
        risks = duplicate_governance_risks(self.recipes_dir)
        report = {
            "ok": True,
            "action": "duplicate-governance",
            "candidate_only": True,
            "summary": {
                "shadow_risk_count": len(risks),
                "human_governance_required": len(risks),
            },
            "risks": risks,
            "decision_matrix": duplicate_governance_decision_matrix(),
            "candidate_priority_rules": duplicate_governance_candidate_priority_rules(risks),
            "what_if_validation_cases": duplicate_governance_validation_case_templates(risks),
            "production_notes": [
                "duplicate-governance 只读取 lookup/lock pressure 报告里的 shadow 风险。",
                "它不会自动 merge、supersede、reject 或修改正式 recipe。",
                "candidate_priority_rules 只是治理草案，不会改变 lookup 排序。",
                "风险通过只说明已生成治理候选包，不证明真实任务质量通过。",
            ],
            "cannot_claim": [
                "不能说 duplicate-governance 已经合并或废弃任何 recipe。",
                "不能说治理候选报告替代人工 canonical/merge/supersede 决策。",
                "不能说没有风险就证明大池召回质量通过。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("duplicate_governance", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        markdown_path = self.recipes_dir / "reports" / f"{report_id}.md"
        what_if_cases_path = self.recipes_dir / "reports" / f"{report_id}_what_if_cases.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["markdown_path"] = str(markdown_path)
        report["what_if_cases_path"] = str(what_if_cases_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        write_json(what_if_cases_path, duplicate_governance_what_if_cases_doc(report))
        write_text_redacted(markdown_path, duplicate_governance_markdown(report))
        event, idem = self.append_event(
            "duplicate_governance_report_created",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "shadow_risk_count": len(risks),
            },
            idempotency_key=f"duplicate-governance:{report_hash}",
            lock_exempt_reason="duplicate_governance_candidate_only",
            claim_status=claim_status(
                verified=["已生成重复/近重复菜谱治理候选报告。"],
                missing_evidence=[] if risks else ["没有发现 shadow 风险；这不等于未来不会出现重复菜谱。"],
                cannot_claim=[
                    "不能说 duplicate-governance 已经合并或废弃任何 recipe。",
                    "不能说治理候选报告替代人工 canonical/merge/supersede 决策。",
                    "不能说没有风险就证明大池召回质量通过。",
                ],
            ),
        )
        return command_result(
            "duplicate-governance",
            idem,
            files_written=[str(report_path), str(markdown_path), str(what_if_cases_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def candidate_quality_benchmark(self, *, cases_path: str) -> dict[str, Any]:
        self.ensure_dirs()
        resolved_cases_path = resolve_existing_file(self.root, cases_path, "AR420", "candidate-quality-benchmark cases 文件不存在。")
        cases_doc = read_json(resolved_cases_path, {})
        raw_cases = cases_doc.get("cases") if isinstance(cases_doc, dict) else cases_doc
        if not isinstance(raw_cases, list) or not raw_cases:
            raise RecipesError("AR421", "candidate-quality-benchmark cases 必须是非空列表。", str(resolved_cases_path), "写入 cases 数组。")

        cases = [candidate_quality_case(self, raw_case) for raw_case in raw_cases]
        passed = sum(1 for case in cases if case.get("status") == "passed")
        failed = sum(1 for case in cases if case.get("status") == "failed")
        blocked = sum(1 for case in cases if case.get("status") == "blocked")
        scorable = passed + failed
        score = (passed / scorable) if scorable else 0.0
        report = {
            "ok": failed == 0 and blocked == 0,
            "action": "candidate-quality-benchmark",
            "candidate_only": True,
            "candidate_quality_score": score,
            "summary": {
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
                "scorable": scorable,
                "case_count": len(cases),
            },
            "cases_path": str(resolved_cases_path),
            "cases": cases,
            "production_notes": [
                "candidate-quality-benchmark 只检查 pending review/candidate patch 的质量边界，不接受 review。",
                "必需词缺失通常表示漏召回或候选字段抽取不完整。",
                "禁用词命中通常表示误召回或候选混入无关资料。",
                "通过报告仍然不能证明真实任务质量或生产级召回质量。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("candidate_quality", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "candidate_quality_benchmark_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "candidate_quality_score": score,
                "passed": passed,
                "failed": failed,
                "blocked": blocked,
            },
            idempotency_key=f"candidate-quality-benchmark:{report_hash}",
            lock_exempt_reason="local_candidate_quality_benchmark",
            claim_status=claim_status(
                verified=["已生成 pending review 候选质量基准报告。"],
                missing_evidence=candidate_quality_missing_evidence(cases),
                cannot_claim=[
                    "不能说 candidate-quality-benchmark 通过就证明真实任务质量。",
                    "不能说 pending review item 已经被接受。",
                    "不能说候选补丁已经写入正式 recipe。",
                ],
            ),
        )
        return command_result(
            "candidate-quality-benchmark",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def completeness_audit(
        self,
        *,
        input_path: str,
        subject_type: str,
        requirements_path: str | None = None,
        software_map_path: str | None = None,
        execution_evidence_path: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        if subject_type not in {"skill", "course"}:
            raise RecipesError(
                "AR432",
                "completeness-audit subject-type 不支持。",
                f"收到：{subject_type}",
                "使用 skill 或 course。",
            )
        resolved_input = resolve_existing_file(self.root, input_path, "AR433", "completeness-audit 输入文件不存在。")
        subject = read_json(resolved_input, None)
        if not isinstance(subject, dict):
            raise RecipesError(
                "AR434",
                "completeness-audit 输入必须是 JSON object。",
                str(resolved_input),
                "传入正式 recipe、candidate patch 或课程拆解清单 JSON。",
            )
        requirements: dict[str, Any] = {}
        resolved_requirements: Path | None = None
        if requirements_path:
            resolved_requirements = resolve_existing_file(
                self.root,
                requirements_path,
                "AR435",
                "completeness-audit requirements 文件不存在。",
            )
            requirements = read_json(resolved_requirements, None)
            if not isinstance(requirements, dict):
                raise RecipesError(
                    "AR436",
                    "completeness-audit requirements 必须是 JSON object。",
                    str(resolved_requirements),
                    "写入 requirements 数组。",
                )

        software_map: dict[str, Any] = {}
        resolved_software_map: Path | None = None
        if software_map_path:
            resolved_software_map = resolve_existing_file(
                self.root,
                software_map_path,
                "AR437",
                "completeness-audit software map 文件不存在。",
            )
            software_map = read_json(resolved_software_map, None)
            if not isinstance(software_map, dict):
                raise RecipesError(
                    "AR438",
                    "completeness-audit software map 必须是 JSON object。",
                    str(resolved_software_map),
                    "写入 software_id、version_scope 和 functions 数组。",
                )

        execution_evidence: dict[str, Any] | None = None
        resolved_execution_evidence: Path | None = None
        if execution_evidence_path:
            resolved_execution_evidence = resolve_existing_file(
                self.root,
                execution_evidence_path,
                "AR451",
                "completeness-audit execution evidence 文件不存在。",
            )
            execution_evidence = read_json(resolved_execution_evidence, None)
            if not isinstance(execution_evidence, dict):
                raise RecipesError(
                    "AR452",
                    "completeness-audit execution evidence 必须是 JSON object。",
                    str(resolved_execution_evidence),
                    "写入 fresh_agent、clean_start、recipe_only、attempt_count、passed 和 evidence_paths。",
                )
            payload = subject.get("proposed_change")
            if isinstance(payload, dict):
                payload["fresh_execution_evidence"] = execution_evidence
            else:
                subject["fresh_execution_evidence"] = execution_evidence

        report = completeness_audit_report(
            subject,
            subject_type=subject_type,
            requirements=requirements,
            software_map=software_map,
            input_path=resolved_input,
            requirements_path=resolved_requirements,
            software_map_path=resolved_software_map,
            execution_evidence_path=resolved_execution_evidence,
            project_root=self.root,
        )
        report_hash = sha256_json(report)
        report_id = make_id("completeness", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "completeness_audit_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "subject_type": subject_type,
                "status": report["status"],
                "score": report["score"],
                "hard_gates_passed": report["hard_gates_passed"],
            },
            idempotency_key=f"completeness-audit:{report_hash}",
            lock_exempt_reason="local_completeness_audit",
            claim_status=claim_status(
                verified=["已按固定规则生成技能/课程完整性审计报告。"],
                missing_evidence=report["missing_evidence"],
                cannot_claim=[
                    "不能说完整性分数证明内容正确。",
                    "不能说课程结构完整就证明已经学会或覆盖整套课程。",
                    "不能说技能结构完整就证明已在真实软件和真实产物中跑通。",
                    "没有 requirements 时，不能说已检查该技能特有的关键动作。",
                    "没有逐步软件操作、功能地图和 fresh-agent 一次跑通证据时，不能说 agent 已经学会该技能。",
                ],
            ),
        )
        return command_result(
            "completeness-audit",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def course_skill_draft(
        self,
        *,
        transcript_path: str,
        spec_path: str,
        software_map_path: str,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        resolved_transcript = resolve_existing_file(
            self.root,
            transcript_path,
            "AR439",
            "course-skill-draft transcript 文件不存在。",
        )
        resolved_spec = resolve_existing_file(
            self.root,
            spec_path,
            "AR440",
            "course-skill-draft spec 文件不存在。",
        )
        resolved_software_map = resolve_existing_file(
            self.root,
            software_map_path,
            "AR441",
            "course-skill-draft software map 文件不存在。",
        )
        spec = read_json(resolved_spec, None)
        software_map = read_json(resolved_software_map, None)
        if not isinstance(spec, dict):
            raise RecipesError(
                "AR442",
                "course-skill-draft spec 必须是 JSON object。",
                str(resolved_spec),
                "写入 skill_id、title、software_id 和 steps。",
            )
        if not isinstance(software_map, dict):
            raise RecipesError(
                "AR443",
                "course-skill-draft software map 必须是 JSON object。",
                str(resolved_software_map),
                "写入 software_id、version_scope 和 functions。",
            )
        raw_steps = spec.get("steps") if isinstance(spec.get("steps"), list) else []
        if not raw_steps:
            raise RecipesError(
                "AR444",
                "course-skill-draft spec 没有步骤范围。",
                str(resolved_spec),
                "至少写一条 order、function_id、start、end。",
            )
        required_function_ids = unique_text(
            [
                str(step.get("function_id", "")).strip()
                for step in raw_steps
                if isinstance(step, dict) and str(step.get("function_id", "")).strip()
            ]
        )
        map_check = completeness_software_function_map_check(
            software_map,
            expected_software_id=str(spec.get("software_id", "")).strip(),
            required_function_ids=required_function_ids,
        )
        if not map_check["passed"]:
            raise RecipesError(
                "AR445",
                "course-skill-draft software map 不完整。",
                stable_json(map_check),
                "补齐 spec 引用的功能及用途、结果、失败信号和来源。",
            )

        transcript_text = resolved_transcript.read_text(encoding="utf-8")
        cues = timestamped_transcript_cues(transcript_text)
        if not cues:
            raise RecipesError(
                "AR446",
                "course-skill-draft 没读到时间码片段。",
                str(resolved_transcript),
                "使用 [开始秒-结束秒] 文本 格式。",
            )
        functions = {
            str(item.get("function_id")): item
            for item in software_map.get("functions", [])
            if isinstance(item, dict)
        }
        candidate_steps: list[dict[str, Any]] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                raise RecipesError(
                    "AR447",
                    "course-skill-draft step 必须是 JSON object。",
                    f"step {index}",
                    "写入 order、function_id、start、end。",
                )
            order = raw_step.get("order")
            function_id = str(raw_step.get("function_id", "")).strip()
            start = float_or_zero(raw_step.get("start"))
            end = float_or_zero(raw_step.get("end"))
            if order != index or not function_id or end <= start:
                raise RecipesError(
                    "AR448",
                    "course-skill-draft step 顺序或时间范围无效。",
                    stable_json(raw_step),
                    "order 从 1 连续递增，并确保 end 大于 start。",
                )
            selected = [cue for cue in cues if cue["end"] > start and cue["start"] < end]
            if not selected:
                raise RecipesError(
                    "AR449",
                    "course-skill-draft step 时间范围没有课程文字。",
                    f"step={index}, range={start:.2f}-{end:.2f}",
                    "修正 spec 的 start/end，不能凭空补步骤。",
                )
            function = functions[function_id]
            action = " ".join(cue["text"] for cue in selected)
            candidate_steps.append(
                {
                    "order": index,
                    "action": action,
                    "function_id": function_id,
                    "function_name": function.get("name"),
                    "purpose": function.get("purpose"),
                    "ui_action": function.get("ui_action"),
                    "expected_state": function.get("expected_state"),
                    "verification": function.get("verification") or function.get("expected_state"),
                    "failure_signals": function.get("failure_signals"),
                    "fallback": function.get("fallback") or "停止并进入人工复核。",
                    "source_trace": [
                        {
                            "path": str(resolved_transcript),
                            "timestamp": f"{start:.2f}-{end:.2f}",
                            "cue_count": len(selected),
                        }
                    ],
                }
            )

        skill_id = str(spec.get("skill_id", "")).strip()
        if not skill_id:
            raise RecipesError(
                "AR450",
                "course-skill-draft spec 缺 skill_id。",
                str(resolved_spec),
                "给这条候选技能一个稳定的 skill_id。",
            )
        candidate = {
            "candidate_type": "course_skill_draft",
            "status": "candidate",
            "skill_id": skill_id,
            "title": str(spec.get("title") or skill_id),
            "software_id": spec.get("software_id"),
            "execution_context": str(
                spec.get("execution_context") or "真实剪映专业版干净时间线；只按本 candidate 执行"
            ),
            "use_when": text_values(spec.get("use_when")),
            "do_not_use_when": text_values(spec.get("do_not_use_when")),
            "inputs_required": text_values(spec.get("inputs_required")) or ["课程指定素材", "干净测试时间线"],
            "steps": candidate_steps,
            "verification": unique_text(
                ["在真实剪映专业版干净时间线逐步执行，并保留时间线截图和预览证据。"]
                + [str(step["verification"]) for step in candidate_steps]
            ),
            "success_means": text_values(spec.get("success_means"))
            or ["逐步预期状态全部出现，并通过 fresh-agent 一次执行复核。"],
            "failure_signals": unique_text([str(step["failure_signals"]) for step in candidate_steps]),
            "fallback_allowed": unique_text([str(step["fallback"]) for step in candidate_steps]),
            "source_trace": [
                {
                    "path": str(resolved_transcript),
                    "timestamp": f"{min(step['start'] for step in raw_steps):.2f}-{max(step['end'] for step in raw_steps):.2f}",
                }
            ],
            "source_truth_to_read": [str(resolved_transcript), str(resolved_software_map)],
            "evidence_refs": [str(resolved_transcript), str(resolved_spec), str(resolved_software_map)],
            "verified_path": [str(resolved_transcript), str(resolved_software_map)],
            "software_map_path": str(resolved_software_map),
            "course_segment_coverage_complete": True,
            "course_full_coverage_complete": bool(spec.get("course_full_coverage_complete", False)),
            "cannot_claim": [
                "不能说课程候选步骤已经在真实软件中跑通。",
                "不能说这条 candidate 已经写入正式 recipe。",
                "不能说一个技能片段已经覆盖整套课程。",
            ],
        }
        candidate_hash = sha256_json(candidate)
        draft_id = make_id("course_skill_draft", candidate_hash)
        draft_path = self.recipes_dir / "candidates" / "course_skill_drafts" / f"{draft_id}.json"
        candidate["draft_id"] = draft_id
        candidate["candidate_hash"] = candidate_hash
        candidate["draft_path"] = str(draft_path)
        write_json(draft_path, candidate)
        event, idem = self.append_event(
            "course_skill_draft_created",
            {
                "draft_id": draft_id,
                "skill_id": skill_id,
                "step_count": len(candidate_steps),
                "candidate_hash": candidate_hash,
            },
            idempotency_key=f"course-skill-draft:{candidate_hash}",
            lock_exempt_reason="candidate_course_extraction_only",
            claim_status=claim_status(
                verified=["已从指定课程时间码生成逐步 candidate，并绑定软件功能地图。"],
                missing_evidence=["还没有 fresh agent 从干净时间线只按候选步骤一次跑通。"],
                cannot_claim=candidate["cannot_claim"],
            ),
        )
        return command_result(
            "course-skill-draft",
            idem,
            files_written=[str(draft_path), str(self.events_path)],
            objects_created=[draft_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "status": "candidate",
                "draft_id": draft_id,
                "draft_path": str(draft_path),
                "step_count": len(candidate_steps),
                "formal_recipe_written": False,
                "candidate": candidate,
            },
        )

    def qwen_quality_case(
        self,
        *,
        qwen_response_json: str | None,
        allow_loopback: bool,
        limit: int,
        timeout: int,
    ) -> dict[str, Any]:
        if not qwen_response_json and not allow_loopback:
            return {
                "case_id": "qwen_embedding_recall",
                "adapter": "qwen3",
                "status": "skipped",
                "passed": False,
                "query": "review_queue candidate formal recipe",
                "missing_evidence": ["Qwen quality case needs --qwen-response-json or --allow-loopback."],
                "cannot_claim": ["不能说本次 benchmark 已验证 Qwen embedding 搜索准确率。"],
            }
        try:
            result = self.embedding_search(
                "review_queue candidate formal recipe",
                provider="qwen3",
                response_json=qwen_response_json,
                allow_loopback=allow_loopback,
                limit=limit,
                timeout=timeout,
            )
        except RecipesError as exc:
            return {
                "case_id": "qwen_embedding_recall",
                "adapter": "qwen3",
                "status": "blocked",
                "passed": False,
                "query": "review_queue candidate formal recipe",
                "missing_evidence": [exc.cause],
                "cannot_claim": ["不能说本次 benchmark 已验证 Qwen embedding 搜索准确率。"],
            }
        results = result.get("results", [])
        text = " ".join(quality_result_text(item) for item in results[:1]).casefold()
        matched = [term for term in ["review", "candidate"] if term in text]
        passed = bool(results) and len(matched) == 2
        return {
            "case_id": "qwen_embedding_recall",
            "adapter": "qwen3",
            "status": "passed" if passed else "failed",
            "passed": passed,
            "query": result.get("query"),
            "execution_mode": result.get("execution_mode"),
            "expected_terms": ["review", "candidate"],
            "matched_terms": matched,
            "top_result": quality_top_result(results),
            "cannot_claim": ["不能说 Qwen embedding 命中结果已经进入正式 recipe。"],
        }

    def search(self, query: str, *, limit: int = 5, kind: str = "all", source_path_contains: list[str] | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        if limit < 1:
            raise RecipesError("AR280", "search limit 必须大于 0。", f"收到：{limit}", "传入正整数。")
        if kind not in {"all", "source", "video"}:
            raise RecipesError("AR281", "search kind 不支持。", f"收到：{kind}", "使用 all/source/video。")
        filters = normalize_source_path_filters(source_path_contains)
        records = filter_records_by_source_path(self.search_records(kind=kind), filters)
        ranked = rank_evidence_candidates(query, records)[:limit]
        return {
            "ok": True,
            "action": "search",
            "query": query,
            "kind": kind,
            "source_path_contains": filters,
            "results": ranked,
            "claim_status": claim_status(
                verified=["已从本地 source_index/video_index 读取候选片段并按关键词打分。"],
                missing_evidence=[] if ranked else ["没有命中候选片段。"],
                cannot_claim=[
                    "不能说检索结果已经验证。",
                    "不能说候选片段已经进入正式 recipe。",
                    "不能说已接入 Cognee/Graphiti/Zep 等外部检索系统。",
                ],
            ),
        }

    def search_records(self, *, kind: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if kind in {"all", "source"}:
            for row in read_jsonl(self.recipes_dir / "source_index" / "chunks.jsonl"):
                records.append(
                    {
                        "record_type": "source_chunk",
                        "record_id": row.get("chunk_id"),
                        "source_id": row.get("source_id"),
                        "path": row.get("path"),
                        "text": row.get("text", ""),
                        "evidence_status": "candidate",
                    }
                )
        if kind in {"all", "video"}:
            for chunks_path in (self.recipes_dir / "video_index").glob("*/chunks.jsonl"):
                for row in read_jsonl(chunks_path):
                    records.append(
                        {
                            "record_type": "video_chunk",
                            "record_id": row.get("chunk_id"),
                            "source_id": row.get("course_id"),
                            "path": row.get("transcript_path"),
                            "start": row.get("start"),
                            "end": row.get("end"),
                            "text": row.get("text", ""),
                            "evidence_status": "candidate",
                        }
                    )
        return records

    def refine(
        self,
        *,
        query: str,
        knowledge_need_id: str,
        target_recipe_id: str,
        candidate_fields: list[str],
        limit: int = 20,
        kind: str = "all",
        source_path_contains: list[str] | None = None,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        if limit < 1:
            raise RecipesError("AR282", "refine limit 必须大于 0。", f"收到：{limit}", "传入正整数。")
        if kind not in {"all", "source", "video"}:
            raise RecipesError("AR283", "refine kind 不支持。", f"收到：{kind}", "使用 all/source/video。")
        allowed_fields = normalize_candidate_fields(candidate_fields)
        filters = normalize_source_path_filters(source_path_contains)
        records = rank_refinement_records(query, filter_records_by_source_path(self.search_records(kind=kind), filters))[:limit]
        refined: list[dict[str, Any]] = []
        mapped_count = 0
        archive_count = 0
        for record in records:
            inferred_fields = infer_candidate_fields(record.get("text", ""))
            fields = [field for field in inferred_fields if not allowed_fields or field in allowed_fields]
            status = "mapped" if fields and target_recipe_id and knowledge_need_id else "archive_index_only"
            if status == "mapped":
                mapped_count += 1
            else:
                archive_count += 1
            refined.append(
                {
                    "refined_chunk_id": make_id("refined", record.get("record_id"), knowledge_need_id, target_recipe_id, fields),
                    "source_record": record,
                    "source_trace": source_trace_for_record(record),
                    "knowledge_need_id": knowledge_need_id if status == "mapped" else None,
                    "target_recipe_id": target_recipe_id if status == "mapped" else None,
                    "candidate_fields": fields,
                    "evidence_strength": "candidate",
                    "status": status,
                    "archive_reason": None if status == "mapped" else "chunk did not map to allowed recipe fields",
                    "cannot_claim": [
                        "不能说 refined chunk 已经验证。",
                        "不能说 refined chunk 已经进入正式 recipe。",
                    ],
                }
            )
        refinement_id = make_id("refinement", query, knowledge_need_id, target_recipe_id, allowed_fields, [row["refined_chunk_id"] for row in refined])
        chunks_path = self.recipes_dir / "source_refinery" / "chunks" / f"{refinement_id}.jsonl"
        latest_path = self.recipes_dir / "source_refinery" / "chunks" / "latest.json"
        write_jsonl(chunks_path, refined)
        write_json(
            latest_path,
            {
                "refinement_id": refinement_id,
                "chunks_path": str(chunks_path),
                "mapped_count": mapped_count,
                "archive_index_only_count": archive_count,
            },
        )
        payload = {
            "refinement_id": refinement_id,
            "query": query,
            "knowledge_need_id": knowledge_need_id,
            "target_recipe_id": target_recipe_id,
            "candidate_fields": allowed_fields,
            "source_path_contains": filters,
            "mapped_count": mapped_count,
            "archive_index_only_count": archive_count,
        }
        event, idem = self.append_event(
            "source_refined",
            payload,
            idempotency_key=f"refine:{refinement_id}",
            lock_exempt_reason="source_refinery_candidate_mapping",
            claim_status=claim_status(
                verified=["已把本地 source/video chunk 映射为 source_refinery refined chunks。"],
                cannot_claim=[
                    "不能说 refine 输出已经进入正式 recipe。",
                    "不能说 archive_index_only 的资料已经被吸收。",
                ],
            ),
        )
        return command_result(
            "refine",
            idem,
            files_written=[str(chunks_path), str(latest_path), str(self.events_path)],
            objects_created=[refinement_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "refinement_id": refinement_id,
                "mapped_count": mapped_count,
                "archive_index_only_count": archive_count,
            },
        )

    def extract_cards(self, *, refinement_id: str | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        if not refinement_id:
            latest = read_json(self.recipes_dir / "source_refinery" / "chunks" / "latest.json", {})
            refinement_id = latest.get("refinement_id")
        if not refinement_id:
            raise RecipesError("AR284", "没有可抽卡的 refinement。", "source_refinery/chunks/latest.json 不存在。", "先运行 agent-recipes refine。")
        chunks_path = self.recipes_dir / "source_refinery" / "chunks" / f"{refinement_id}.jsonl"
        refined_chunks = read_jsonl(chunks_path)
        if not refined_chunks:
            raise RecipesError("AR285", "refinement 没有 chunk。", f"找不到或为空：{chunks_path}", "重新运行 refine。")
        cards: list[dict[str, Any]] = []
        files: list[str] = []
        counts: dict[str, int] = {}
        for chunk in refined_chunks:
            if chunk.get("status") != "mapped":
                continue
            text = chunk.get("source_record", {}).get("text", "")
            card_type = infer_card_type(text)
            payload = parse_field_blocks(text)
            cannot_claim = payload.get("cannot_claim") or chunk.get("cannot_claim") or ["不能说卡片已经进入正式 recipe。"]
            card = {
                "card_id": make_id("card", card_type, chunk.get("refined_chunk_id"), chunk.get("target_recipe_id"), text),
                "card_type": card_type,
                "source_chunk_ids": [chunk.get("refined_chunk_id")],
                "source_trace": chunk.get("source_trace", []),
                "knowledge_need_id": chunk.get("knowledge_need_id"),
                "target_recipe_id": chunk.get("target_recipe_id"),
                "target_fields": chunk.get("candidate_fields", []),
                "evidence_strength": "candidate",
                "extracted_payload": payload,
                "source_quote": clip_source_quote(text),
                "cannot_claim": cannot_claim,
                "status": "candidate",
            }
            cards.append(card)
            counts[card_type] = counts.get(card_type, 0) + 1
            card_path = self.recipes_dir / "source_refinery" / "cards" / card_dir_for_type(card_type) / f"{card['card_id']}.json"
            write_json(card_path, card)
            files.append(str(card_path))
        if not cards:
            raise RecipesError("AR286", "没有可生成的卡片。", "refinement 中没有 mapped chunk。", "调整 refine 的 query 或 candidate fields。")
        index_path = self.recipes_dir / "source_refinery" / "cards" / "cards.jsonl"
        existing_cards = read_jsonl(index_path)
        merged_cards = merge_cards_by_id(existing_cards, cards)
        write_jsonl(index_path, merged_cards)
        files.append(str(index_path))
        latest_path = self.recipes_dir / "source_refinery" / "cards" / "latest.json"
        payload = {"refinement_id": refinement_id, "card_ids": [card["card_id"] for card in cards], "card_counts": counts}
        write_json(latest_path, payload)
        files.append(str(latest_path))
        event, idem = self.append_event(
            "refinery_cards_extracted",
            payload,
            idempotency_key=f"extract-cards:{sha256_json(payload)}",
            lock_exempt_reason="source_refinery_card_extraction",
            claim_status=claim_status(
                verified=["已按固定 schema 生成 source_refinery cards。"],
                cannot_claim=[
                    "不能说卡片已经修改正式 recipe。",
                    "不能说卡片内容已经通过用户审查。",
                ],
            ),
        )
        return command_result(
            "extract-cards",
            idem,
            files_written=files + [str(self.events_path)],
            objects_created=[card["card_id"] for card in cards] + ([event["event_id"]] if idem == "created" else []),
            claim_status=event["claim_status"],
            extra={"refinement_id": refinement_id, "card_counts": counts, "card_ids": [card["card_id"] for card in cards]},
        )

    def patch_draft(self, *, target_recipe_id: str) -> dict[str, Any]:
        self.ensure_dirs()
        all_cards = [
            card
            for card in read_jsonl(self.recipes_dir / "source_refinery" / "cards" / "cards.jsonl")
            if card.get("target_recipe_id") == target_recipe_id and card.get("status") == "candidate"
        ]
        cards = latest_cards_for_target(self.recipes_dir, target_recipe_id, all_cards)
        if not cards:
            raise RecipesError(
                "AR287",
                "没有可生成 patch draft 的卡片。",
                f"target_recipe_id={target_recipe_id}",
                "先运行 extract-cards，或检查 target recipe id。",
            )
        proposed_additions = recipe_additions_from_cards(cards)
        review_hints = source_refinery_review_hints(cards, proposed_additions)
        plain_language_summary = source_refinery_plain_review_summary(target_recipe_id, cards, proposed_additions, review_hints)
        patch_draft_id = make_id("patch_draft", target_recipe_id, [card["card_id"] for card in cards], proposed_additions)
        draft_path = self.recipes_dir / "source_refinery" / "patch_drafts" / f"{patch_draft_id}.json"
        patch_draft = {
            "patch_draft_id": patch_draft_id,
            "target_recipe_id": target_recipe_id,
            "target_fields": sorted(proposed_additions),
            "source_card_ids": [card["card_id"] for card in cards],
            "proposed_additions": proposed_additions,
            "reason": "source_refinery cards mapped to recipe fields.",
            "evidence_strength": "candidate",
            "needs_user_review": True,
            "review_hints": review_hints,
            "plain_language_summary": plain_language_summary,
            "cannot_claim": [
                "不能说 patch draft 已经修改正式 recipe。",
                "不能说 source_refinery 候选已经通过用户审查。",
            ],
            "status": "pending_review",
        }
        write_json(draft_path, patch_draft)

        recipe = recipe_from_patch_draft(target_recipe_id, patch_draft, cards)
        patch_id = make_id("patch_refinery", patch_draft_id)
        review_id = make_id("review", patch_id)
        candidate_path = self.recipes_dir / "candidates" / f"{patch_id}.json"
        review_path = self.recipes_dir / "review_queue" / f"{review_id}.json"
        candidate_patch = {
            "patch_id": patch_id,
            "patch_type": "source_refinery_patch_draft",
            "source_patch_draft_id": patch_draft_id,
            "source_card_ids": patch_draft["source_card_ids"],
            "target_recipe_id": target_recipe_id,
            "proposed_change": recipe,
            "reason": "从 source_refinery patch draft 生成待审候选补丁。",
            "evidence_refs": patch_draft["source_card_ids"],
            "risk": "split_recommended" if review_hints.get("split_recommended") else "needs_review",
            "plain_language_summary": plain_language_summary,
            "status": "pending_review",
        }
        review = {
            "review_id": review_id,
            "blocking_level": "P0",
            "question": f"是否接受 source_refinery patch draft：{target_recipe_id}",
            "why_user_must_decide": "接受后才会生成或修改正式 recipe version。",
            "options": ["accept", "reject", "split", "supersede"],
            "recommendation": "split_before_accept" if review_hints.get("split_recommended") else "review",
            "review_hints": review_hints,
            "plain_language_summary": plain_language_summary,
            "evidence_refs": patch_draft["source_card_ids"],
            "proposed_patch_id": patch_id,
            "source_patch_draft_id": patch_draft_id,
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
        }
        write_json(candidate_path, candidate_patch)
        write_json(review_path, review)
        payload = {"patch_draft_id": patch_draft_id, "patch_id": patch_id, "review_id": review_id, "target_recipe_id": target_recipe_id}
        event, idem = self.append_event(
            "recipe_patch_drafted",
            payload,
            idempotency_key=f"patch-draft:{patch_draft_id}",
            lock_exempt_reason="source_refinery_patch_draft",
            claim_status=claim_status(
                verified=["已从 source_refinery cards 生成 RecipePatchDraft 和 review item。"],
                cannot_claim=[
                    "不能说 patch draft 已经修改正式 recipe。",
                    "不能说 review item 已经被接受。",
                ],
            ),
        )
        return command_result(
            "patch-draft",
            idem,
            files_written=[str(draft_path), str(candidate_path), str(review_path), str(self.events_path)],
            objects_created=[patch_draft_id, patch_id, review_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"patch_draft_id": patch_draft_id, "patch_id": patch_id, "review_id": review_id, "target_recipe_id": target_recipe_id},
        )

    def knowledge_fusion(self, *, target_recipe_id: str) -> dict[str, Any]:
        self.ensure_dirs()
        cards = merge_cards_by_id(
            [],
            [
                card
                for card in read_jsonl(self.recipes_dir / "source_refinery" / "cards" / "cards.jsonl")
                if card.get("target_recipe_id") == target_recipe_id and card.get("status") == "candidate"
            ],
        )
        if not cards:
            raise RecipesError(
                "AR410",
                "没有可融合的 source_refinery cards。",
                f"target_recipe_id={target_recipe_id}",
                "先运行 refine/extract-cards，或检查 target recipe id。",
            )

        candidates = knowledge_fusion_candidates(cards, target_recipe_id)
        if not candidates:
            candidates = [
                knowledge_fusion_candidate(
                    "archive_index_only",
                    target_recipe_id,
                    cards,
                    reason="No safe merge/split/conflict/deep-read candidate was found; keep cards searchable only.",
                    details={"card_count": len(cards)},
                )
            ]
        candidate_counts: dict[str, int] = {}
        for candidate in candidates:
            candidate_counts[candidate["candidate_type"]] = candidate_counts.get(candidate["candidate_type"], 0) + 1

        fusion_id = make_id("fusion", target_recipe_id, candidates)
        fusion_path = self.recipes_dir / "source_refinery" / "fusion" / f"{fusion_id}.json"
        fusion_doc = {
            "fusion_id": fusion_id,
            "target_recipe_id": target_recipe_id,
            "candidate_only": True,
            "source_card_ids": sorted({card.get("card_id") for card in cards if card.get("card_id")}),
            "candidate_counts": candidate_counts,
            "candidates": candidates,
            "status": "pending_review",
            "cannot_claim": [
                "不能说 knowledge_fusion candidate 已经修改正式 recipe。",
                "不能说 knowledge_fusion candidate 已经通过用户审查。",
            ],
        }
        write_json(fusion_path, fusion_doc)

        index_path = self.recipes_dir / "source_refinery" / "fusion" / "fusion.jsonl"
        existing = [row for row in read_jsonl(index_path) if row.get("fusion_id") != fusion_id]
        write_jsonl(index_path, existing + [fusion_doc])

        patch_id = make_id("patch_fusion", fusion_id)
        review_id = make_id("review", patch_id)
        candidate_path = self.recipes_dir / "candidates" / f"{patch_id}.json"
        review_path = self.recipes_dir / "review_queue" / f"{review_id}.json"
        candidate_patch = {
            "patch_id": patch_id,
            "patch_type": "knowledge_fusion_candidate_set",
            "source_fusion_id": fusion_id,
            "source_card_ids": fusion_doc["source_card_ids"],
            "target_recipe_id": target_recipe_id,
            "candidate_counts": candidate_counts,
            "fusion_candidates": candidates,
            "reason": "knowledge_fusion candidates require human merge/split/supersede/reject review before any formal recipe change.",
            "evidence_refs": fusion_doc["source_card_ids"],
            "risk": "needs_review",
            "status": "pending_review",
        }
        review = {
            "review_id": review_id,
            "blocking_level": "P0",
            "question": f"如何处理 knowledge_fusion candidates：{target_recipe_id}",
            "why_user_must_decide": "融合候选只能建议合并、拆分、替换、冲突或深读，不能直接生成正式 recipe。",
            "options": ["merge", "split", "supersede", "reject"],
            "recommendation": "review",
            "review_hints": {
                "candidate_counts": candidate_counts,
                "must_not_accept_directly": True,
                "needs_deep_read": candidate_counts.get("needs_deep_read", 0),
                "conflicts": candidate_counts.get("conflict_candidate", 0),
            },
            "evidence_refs": fusion_doc["source_card_ids"],
            "proposed_patch_id": patch_id,
            "source_fusion_id": fusion_id,
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
        }
        write_json(candidate_path, candidate_patch)
        write_json(review_path, review)
        payload = {
            "fusion_id": fusion_id,
            "patch_id": patch_id,
            "review_id": review_id,
            "target_recipe_id": target_recipe_id,
            "candidate_counts": candidate_counts,
        }
        event, idem = self.append_event(
            "knowledge_fusion_candidates_created",
            payload,
            idempotency_key=f"knowledge-fusion:{fusion_id}",
            lock_exempt_reason="knowledge_fusion_candidate_only",
            claim_status=claim_status(
                verified=["已从 source_refinery cards 生成 knowledge_fusion candidates 和 review item。"],
                cannot_claim=[
                    "不能说 knowledge_fusion candidate 已经修改正式 recipe。",
                    "不能说 knowledge_fusion candidate 已经通过用户审查。",
                    "不能说 knowledge_fusion 已经自动判断真理。",
                ],
            ),
        )
        return command_result(
            "knowledge-fusion",
            idem,
            files_written=[str(fusion_path), str(index_path), str(candidate_path), str(review_path), str(self.events_path)],
            objects_created=[fusion_id, patch_id, review_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "candidate_only": True,
                "fusion_id": fusion_id,
                "patch_id": patch_id,
                "review_id": review_id,
                "target_recipe_id": target_recipe_id,
                "candidate_counts": candidate_counts,
            },
        )

    def deep_read_plan(self, *, fusion_id: str) -> dict[str, Any]:
        self.ensure_dirs()
        fusion_path = self.recipes_dir / "source_refinery" / "fusion" / f"{fusion_id}.json"
        if not fusion_path.exists():
            raise RecipesError(
                "AR430",
                "knowledge_fusion 记录不存在。",
                f"fusion_id={fusion_id}",
                "先运行 agent-recipes knowledge-fusion。",
            )
        fusion = read_json(fusion_path, {})
        target_recipe_id = str(fusion.get("target_recipe_id") or "")
        candidates = [
            candidate
            for candidate in fusion.get("candidates", [])
            if isinstance(candidate, dict) and candidate.get("candidate_type") == "needs_deep_read"
        ]
        if not candidates:
            raise RecipesError(
                "AR431",
                "没有 needs_deep_read 候选可转成深读计划。",
                f"fusion_id={fusion_id}",
                "只有 knowledge_fusion 明确标出 needs_deep_read 时才能生成深读计划。",
            )

        tasks: list[dict[str, Any]] = []
        for candidate in candidates:
            tasks.extend(deep_read_tasks_from_candidate(self.recipes_dir, fusion_id, target_recipe_id, candidate))
        plan_id = make_id("deep_read_plan", fusion_id, tasks)
        plan_path = self.recipes_dir / "source_refinery" / "deep_read_plans" / f"{plan_id}.json"
        plan_doc = {
            "plan_id": plan_id,
            "fusion_id": fusion_id,
            "target_recipe_id": target_recipe_id,
            "candidate_only": True,
            "status": "candidate",
            "task_count": len(tasks),
            "tasks": tasks,
            "cannot_claim": [
                "不能说 deep-read plan 已经完成深读。",
                "不能说 deep-read plan 已经修改正式 recipe。",
                "不能说 deep-read plan 已经通过用户审查。",
            ],
        }
        write_json(plan_path, plan_doc)
        index_path = self.recipes_dir / "source_refinery" / "deep_read_plans" / "plans.jsonl"
        existing = [row for row in read_jsonl(index_path) if row.get("plan_id") != plan_id]
        write_jsonl(index_path, existing + [plan_doc])
        event, idem = self.append_event(
            "deep_read_plan_created",
            {
                "plan_id": plan_id,
                "fusion_id": fusion_id,
                "target_recipe_id": target_recipe_id,
                "task_count": len(tasks),
            },
            idempotency_key=f"deep-read-plan:{plan_id}",
            lock_exempt_reason="deep_read_plan_candidate_only",
            claim_status=claim_status(
                verified=["已把 needs_deep_read fusion candidate 转成下一轮窄范围深读计划。"],
                cannot_claim=[
                    "不能说 deep-read plan 已经完成深读。",
                    "不能说 deep-read plan 已经修改正式 recipe。",
                    "不能说 deep-read plan 已经通过用户审查。",
                ],
            ),
        )
        return command_result(
            "deep-read-plan",
            idem,
            files_written=[str(plan_path), str(index_path), str(self.events_path)],
            objects_created=[plan_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={
                "candidate_only": True,
                "plan_id": plan_id,
                "fusion_id": fusion_id,
                "target_recipe_id": target_recipe_id,
                "task_count": len(tasks),
                "tasks": tasks,
            },
        )

    def target_suggestions(
        self,
        *,
        target_recipe_id: str | None = None,
        status: str = "rejected",
        min_reviews: int = 1,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        if status not in {"rejected", "pending", "all"}:
            raise RecipesError(
                "AR440",
                "target-suggestions status 不支持。",
                f"status={status}",
                "使用 rejected、pending 或 all。",
            )
        if min_reviews < 1:
            raise RecipesError("AR441", "min_reviews 必须大于 0。", f"min_reviews={min_reviews}", "传入正整数。")

        groups: dict[tuple[str, str], dict[str, Any]] = {}
        scanned_reviews = 0
        matched_reviews = 0
        for review_path in sorted((self.recipes_dir / "review_queue").glob("*.json")):
            review = read_json(review_path, {})
            scanned_reviews += 1
            review_status = str(review.get("status") or "")
            if status != "all" and review_status != status:
                continue
            patch_id = str(review.get("proposed_patch_id") or "")
            patch_path = self.recipes_dir / "candidates" / f"{patch_id}.json" if patch_id else None
            patch = read_json(patch_path, {}) if patch_path and patch_path.exists() else {}
            if not patch:
                continue
            parent_target_id = str(patch.get("target_recipe_id") or "")
            if target_recipe_id and parent_target_id != target_recipe_id:
                continue
            review_groups = target_suggestion_groups_from_review(self.recipes_dir, review, patch)
            if not review_groups:
                continue
            matched_reviews += 1
            for group in review_groups:
                key = (str(group.get("parent_target_recipe_id") or ""), str(group.get("source_path_contains") or ""))
                merged = groups.setdefault(
                    key,
                    {
                        "parent_target_recipe_id": group.get("parent_target_recipe_id"),
                        "source_path_contains": group.get("source_path_contains"),
                        "source_paths": [],
                        "review_ids": [],
                        "patch_ids": [],
                        "source_card_ids": [],
                        "candidate_fields": [],
                        "decision_reasons": [],
                        "proposed_value_count": 0,
                    },
                )
                merged["source_paths"].extend(group.get("source_paths", []))
                merged["review_ids"].extend(group.get("review_ids", []))
                merged["patch_ids"].extend(group.get("patch_ids", []))
                merged["source_card_ids"].extend(group.get("source_card_ids", []))
                merged["candidate_fields"].extend(group.get("candidate_fields", []))
                merged["decision_reasons"].extend(group.get("decision_reasons", []))
                merged["proposed_value_count"] += int(group.get("proposed_value_count") or 0)

        if scanned_reviews == 0:
            raise RecipesError(
                "AR442",
                "没有 review_queue 可分析。",
                "review_queue 目录里没有 review item。",
                "先运行 patch-draft、knowledge-fusion 或 self-run-benchmark。",
            )

        suggestions = [
            target_suggestion_from_group(group)
            for group in groups.values()
            if len(unique_text(group.get("review_ids", []))) >= min_reviews
        ]
        suggestions = [suggestion for suggestion in suggestions if suggestion.get("source_path_contains")]
        suggestions.sort(
            key=lambda item: (
                -int(item.get("review_count") or 0),
                -int(item.get("proposed_value_count") or 0),
                str(item.get("suggested_target_recipe_id") or ""),
            )
        )
        if not suggestions:
            raise RecipesError(
                "AR443",
                "没有足够证据生成窄目标建议。",
                f"matched_reviews={matched_reviews}, min_reviews={min_reviews}",
                "先积累 pending/rejected review，或降低 --min-reviews。",
            )

        report = {
            "ok": True,
            "action": "target-suggestions",
            "candidate_only": True,
            "status_filter": status,
            "target_recipe_id": target_recipe_id,
            "scanned_reviews": scanned_reviews,
            "matched_reviews": matched_reviews,
            "suggestion_count": len(suggestions),
            "suggestions": suggestions,
            "cannot_claim": [
                "不能说 target suggestion 已经执行。",
                "不能说 target suggestion 已经生成正式 recipe。",
                "不能说 rejected review 里的证据已经被自动判定为正确。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("target_suggestions", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "target_suggestions_created",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "target_recipe_id": target_recipe_id,
                "status_filter": status,
                "suggestion_count": len(suggestions),
                "suggested_target_recipe_ids": [item["suggested_target_recipe_id"] for item in suggestions],
            },
            idempotency_key=f"target-suggestions:{report_hash}",
            lock_exempt_reason="target_suggestions_candidate_only",
            claim_status=claim_status(
                verified=["已从 review_queue/candidate patch 历史生成下一轮窄目标候选建议。"],
                cannot_claim=[
                    "不能说 target suggestion 已经执行。",
                    "不能说 target suggestion 已经写入正式 recipe。",
                    "不能说 target suggestion 替代人工 review。",
                ],
            ),
        )
        return command_result(
            "target-suggestions",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def review_triage(
        self,
        *,
        target_recipe_id: str | None = None,
        target_prefix: str | None = None,
        status: str = "pending",
        min_values: int = 2,
        max_values: int = 40,
        latest_per_target: bool = True,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        if status not in {"pending", "rejected", "all"}:
            raise RecipesError(
                "AR450",
                "review-triage status 不支持。",
                f"status={status}",
                "使用 pending、rejected 或 all。",
            )
        if min_values < 0 or max_values < 1 or min_values > max_values:
            raise RecipesError(
                "AR451",
                "review-triage 数值阈值不合法。",
                f"min_values={min_values}, max_values={max_values}",
                "确保 0 <= min_values <= max_values。",
            )

        review_records = review_triage_records(
            self.recipes_dir,
            self.load_events(),
            status=status,
            target_recipe_id=target_recipe_id,
            target_prefix=target_prefix,
            latest_per_target=latest_per_target,
        )
        if not review_records:
            raise RecipesError(
                "AR452",
                "没有可裁判的 review item。",
                f"status={status}, target_recipe_id={target_recipe_id}, target_prefix={target_prefix}",
                "先运行 self-run-benchmark、patch-draft 或 target-suggestions 生成 review item。",
            )

        items = [
            review_triage_item(record, min_values=min_values, max_values=max_values)
            for record in review_records
        ]
        bucket_counts: dict[str, int] = {}
        action_counts: dict[str, int] = {}
        for item in items:
            bucket_counts[item["triage_bucket"]] = bucket_counts.get(item["triage_bucket"], 0) + 1
            action_counts[item["recommended_action"]] = action_counts.get(item["recommended_action"], 0) + 1
        report = {
            "ok": True,
            "action": "review-triage",
            "candidate_only": True,
            "status_filter": status,
            "target_recipe_id": target_recipe_id,
            "target_prefix": target_prefix,
            "latest_per_target": latest_per_target,
            "min_values": min_values,
            "max_values": max_values,
            "summary": {
                "review_count": len(items),
                "bucket_counts": bucket_counts,
                "action_counts": action_counts,
            },
            "items": items,
            "cannot_claim": [
                "不能说 review-triage 已经接受或拒绝 review。",
                "不能说 review-triage 证明候选内容质量通过。",
                "不能说 triage bucket 可以替代人工 review。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("review_triage", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        event, idem = self.append_event(
            "review_triage_ran",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "review_count": len(items),
                "bucket_counts": bucket_counts,
                "action_counts": action_counts,
                "target_recipe_id": target_recipe_id,
                "target_prefix": target_prefix,
            },
            idempotency_key=f"review-triage:{report_hash}",
            lock_exempt_reason="review_triage_candidate_only",
            claim_status=claim_status(
                verified=["已从 review_queue/candidate patch/source_trace 生成候选分层裁判报告。"],
                cannot_claim=[
                    "不能说 review-triage 已经接受或拒绝 review。",
                    "不能说 review-triage 已经写入正式 recipe。",
                    "不能说 review-triage 替代人工质量判断。",
                ],
            ),
        )
        return command_result(
            "review-triage",
            idem,
            files_written=[str(report_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def review_packet(
        self,
        *,
        target_recipe_id: str | None = None,
        target_prefix: str | None = None,
        status: str = "pending",
        min_values: int = 2,
        max_values: int = 40,
        latest_per_target: bool = True,
        max_items: int = 20,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        if status not in {"pending", "rejected", "all"}:
            raise RecipesError(
                "AR453",
                "review-packet status 不支持。",
                f"status={status}",
                "使用 pending、rejected 或 all。",
            )
        if min_values < 0 or max_values < 1 or min_values > max_values:
            raise RecipesError(
                "AR454",
                "review-packet 数值阈值不合法。",
                f"min_values={min_values}, max_values={max_values}",
                "确保 0 <= min_values <= max_values。",
            )
        if max_items < 1:
            raise RecipesError("AR455", "max_items 必须大于 0。", f"max_items={max_items}", "传入正整数。")

        records = review_triage_records(
            self.recipes_dir,
            self.load_events(),
            status=status,
            target_recipe_id=target_recipe_id,
            target_prefix=target_prefix,
            latest_per_target=latest_per_target,
        )
        if not records:
            raise RecipesError(
                "AR456",
                "没有可生成审核包的 review item。",
                f"status={status}, target_recipe_id={target_recipe_id}, target_prefix={target_prefix}",
                "先运行 self-run-benchmark、patch-draft、target-suggestions 或 review-triage。",
            )

        triage_items = [review_triage_item(record, min_values=min_values, max_values=max_values) for record in records]
        combined = list(zip(records, triage_items, strict=False))
        combined.sort(
            key=lambda pair: (
                review_packet_bucket_rank(str(pair[1].get("triage_bucket") or "")),
                str(pair[1].get("target_recipe_id") or ""),
                str(pair[1].get("review_id") or ""),
            )
        )
        limited = combined[:max_items]
        items = [review_packet_item(record, triage_item) for record, triage_item in limited]
        bucket_counts: dict[str, int] = {}
        action_counts: dict[str, int] = {}
        for item in items:
            bucket = str(item.get("triage_bucket") or "")
            action = str(item.get("recommended_action") or "")
            bucket_counts[bucket] = bucket_counts.get(bucket, 0) + 1
            action_counts[action] = action_counts.get(action, 0) + 1

        report = {
            "ok": True,
            "action": "review-packet",
            "candidate_only": True,
            "status_filter": status,
            "target_recipe_id": target_recipe_id,
            "target_prefix": target_prefix,
            "latest_per_target": latest_per_target,
            "min_values": min_values,
            "max_values": max_values,
            "max_items": max_items,
            "summary": {
                "review_count": len(items),
                "available_review_count": len(combined),
                "bucket_counts": bucket_counts,
                "action_counts": action_counts,
            },
            "items": items,
            "cannot_claim": [
                "不能说 review-packet 已经接受或拒绝 review。",
                "不能说 review-packet 证明候选内容质量通过。",
                "不能说 review-packet 读取或总结了外部 source 原文。",
            ],
        }
        report_hash = sha256_json(report)
        report_id = make_id("review_packet", report_hash)
        report_path = self.recipes_dir / "reports" / f"{report_id}.json"
        markdown_path = self.recipes_dir / "reports" / f"{report_id}.md"
        report["report_id"] = report_id
        report["report_path"] = str(report_path)
        report["markdown_path"] = str(markdown_path)
        report["report_hash"] = report_hash
        report["checked_at"] = now_iso()
        write_json(report_path, report)
        write_text_redacted(markdown_path, review_packet_markdown(report))
        event, idem = self.append_event(
            "review_packet_created",
            {
                "report_id": report_id,
                "report_hash": report_hash,
                "review_count": len(items),
                "available_review_count": len(combined),
                "bucket_counts": bucket_counts,
                "action_counts": action_counts,
                "target_recipe_id": target_recipe_id,
                "target_prefix": target_prefix,
            },
            idempotency_key=f"review-packet:{report_hash}",
            lock_exempt_reason="review_packet_candidate_only",
            claim_status=claim_status(
                verified=["已把 review_queue/candidate patch/triage 证据整理成人类可读审核包。"],
                cannot_claim=[
                    "不能说 review-packet 已经接受或拒绝 review。",
                    "不能说 review-packet 已经写入正式 recipe。",
                    "不能说 review-packet 替代人工质量判断。",
                ],
            ),
        )
        return command_result(
            "review-packet",
            idem,
            files_written=[str(report_path), str(markdown_path), str(self.events_path)],
            objects_created=[report_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra=report,
        )

    def ingest_video_transcript(
        self,
        transcript_path: str,
        *,
        video_path: str | None = None,
        extract_keyframes: bool = False,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        path = Path(transcript_path)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.exists() or not path.is_file():
            raise RecipesError(
                "AR260",
                "transcript 文件不存在。",
                f"找不到文件：{path}",
                "传入真实存在的 .srt 或 .vtt 文件。",
            )
        if path.suffix.lower() not in {".srt", ".vtt"}:
            raise RecipesError(
                "AR261",
                "Phase 0C 只支持 --transcript 的 .srt/.vtt。",
                f"收到：{path.suffix}",
                "先把视频转成本地 transcript，再运行 ingest-video --transcript。",
            )
        digest = file_sha256(path)
        raw = path.read_text(encoding="utf-8-sig")
        cues = parse_transcript(raw)
        if not cues:
            raise RecipesError(
                "AR262",
                "transcript 里没有解析到字幕片段。",
                f"文件：{path}",
                "检查 .srt/.vtt 是否包含时间轴。",
            )
        course_id = make_id("video", str(path), digest)
        video_dir = self.recipes_dir / "video_index" / course_id
        video: dict[str, Any] | None = None
        if video_path:
            resolved_video = Path(video_path)
            if not resolved_video.is_absolute():
                resolved_video = self.root / resolved_video
            resolved_video = resolved_video.resolve()
            if not resolved_video.exists() or not resolved_video.is_file():
                raise RecipesError(
                    "AR263",
                    "video 文件不存在。",
                    f"找不到文件：{resolved_video}",
                    "传入本地存在的视频文件，或只传 --transcript。",
                )
            video = {"path": str(resolved_video), "hash": file_sha256(resolved_video)}
        chunks = [
            {
                "chunk_id": make_id("video_chunk", course_id, cue["index"], cue["start"], cue["end"], cue["text"]),
                "course_id": course_id,
                "cue_index": cue["index"],
                "start": cue["start"],
                "end": cue["end"],
                "text": cue["text"],
                "transcript_path": str(path),
                "transcript_hash": digest,
                "keyframe_path": None,
            }
            for cue in cues
        ]
        keyframes: list[dict[str, Any]] = []
        files: list[str] = []
        if extract_keyframes:
            if not video:
                raise RecipesError(
                    "AR264",
                    "--extract-keyframes 需要 --video。",
                    "没有本地视频文件就不能抽关键帧。",
                    "传入 --video <file>，或去掉 --extract-keyframes。",
                )
            keyframes = extract_keyframes_with_ffmpeg(Path(video["path"]), video_dir / "keyframes", cues[: min(3, len(cues))])
            keyframe_by_index = {item["cue_index"]: item for item in keyframes}
            for chunk in chunks:
                keyframe = keyframe_by_index.get(chunk["cue_index"])
                if keyframe:
                    chunk["keyframe_path"] = keyframe["path"]
            files.extend(item["path"] for item in keyframes)
        metadata = {
            "course_id": course_id,
            "transcript_path": str(path),
            "transcript_hash": digest,
            "video": video,
            "cue_count": len(cues),
            "keyframe_count": len(keyframes),
            "claim_limits": [
                "只解析本地 transcript 文件。",
                "没有对 mp4 做 ASR，也没有调用云服务。",
                "关键帧只证明本地视频可抽帧，不证明视觉质量通过。",
            ],
        }
        chunks_path = video_dir / "chunks.jsonl"
        metadata_path = video_dir / "metadata.json"
        vtt_path = video_dir / "transcript.vtt"
        index_path = video_dir / "INDEX.md"
        write_jsonl(chunks_path, chunks)
        write_json(metadata_path, metadata)
        write_text_redacted(vtt_path, transcript_as_vtt(cues))
        write_text_redacted(index_path, video_index_markdown(metadata))
        files.extend([str(chunks_path), str(metadata_path), str(vtt_path), str(index_path), str(self.events_path)])
        payload = {
            "course_id": course_id,
            "transcript_path": str(path),
            "transcript_hash": digest,
            "video": video,
            "cue_count": len(cues),
            "keyframe_count": len(keyframes),
        }
        event, idem = self.append_event(
            "transcript_ingested",
            payload,
            idempotency_key=f"ingest-video:transcript:{course_id}:{sha256_json(video)}:keyframes={bool(keyframes)}",
            lock_exempt_reason="transcript_indexing",
            claim_status=claim_status(
                verified=[
                    "已解析本地 .srt/.vtt transcript，并生成 video_index。",
                    *(
                        ["已用本地 ffmpeg 从本地视频抽取关键帧。"]
                        if keyframes
                        else []
                    ),
                ],
                cannot_claim=[
                    *(
                        []
                        if keyframes
                        else ["不能说已处理视频原始文件。"]
                    ),
                    "不能说已完成 ASR 或云端转写。",
                    "不能说关键帧视觉质量已经通过。",
                ],
            ),
        )
        return command_result(
            "ingest-video",
            idem,
            files_written=files,
            objects_created=[course_id, event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"course_id": course_id, "cue_count": len(cues), "keyframe_count": len(keyframes)},
        )

    def recipe_ids_from_failure_events(self, failures: list[dict[str, Any]]) -> list[str]:
        recipe_ids: list[str] = []
        for event in failures:
            lock_id = event.get("lock_id") or event.get("payload", {}).get("lock_id")
            if not lock_id:
                continue
            lock_path = self.recipes_dir / "locks" / f"{lock_id}.json"
            if not lock_path.exists():
                continue
            lock = read_json(lock_path, {})
            for recipe_id in lock.get("recipe_ids", []):
                if recipe_id not in recipe_ids:
                    recipe_ids.append(recipe_id)
        return recipe_ids

    def compile(self, *, max_candidates: int | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        if max_candidates is not None and max_candidates < 1:
            raise RecipesError(
                "AR270",
                "max-candidates 必须大于 0。",
                f"收到：{max_candidates}",
                "传入正整数，或不传 --max-candidates。",
            )
        events = self.load_events()
        created: list[dict[str, Any]] = []
        files: list[str] = []
        for event in events:
            payload = event.get("payload", {})
            if event.get("event_type") != "capture" or payload.get("capture_type") != "correction":
                continue
            patch_id = make_id("patch", event["event_id"])
            candidate_path = self.recipes_dir / "candidates" / f"{patch_id}.json"
            if candidate_path.exists():
                continue
            review_id = make_id("review", patch_id)
            plan = correction_compile_plan(
                self.recipes_dir,
                payload,
                event["event_id"],
                load_recipe=self.load_recipe,
                first_line=first_line,
                recipe_draft=recipe_draft,
            )
            recipe_id, draft = plan["recipe_id"], plan["draft"]
            patch = {
                "patch_id": patch_id,
                "source_event_ids": [event["event_id"]],
                "target_recipe_id": recipe_id,
                "proposed_change": draft,
                "reason": plan["reason"],
                "evidence_refs": [event["event_id"]],
                "risk": "needs_review",
                "status": "pending_review",
            }
            review = {
                "review_id": review_id,
                "blocking_level": "P0",
                "question": plan["question"],
                "why_user_must_decide": "接受后会生成正式 recipe version。",
                "options": ["accept", "reject"],
                "recommendation": "accept",
                "evidence_refs": [event["event_id"]],
                "proposed_patch_id": patch_id,
                "status": "pending",
                "decided_by": None,
                "decided_at": None,
            }
            write_json(candidate_path, patch)
            review_path = self.recipes_dir / "review_queue" / f"{review_id}.json"
            write_json(review_path, review)
            files.extend([str(candidate_path), str(review_path)])
            created.append({"patch_id": patch_id, "review_id": review_id, "recipe_id": recipe_id})
            if max_candidates is not None and len(created) >= max_candidates:
                break
        if not created:
            return command_result(
                "compile",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=["没有发现新的 correction capture 需要编译。"],
                    cannot_claim=["不能说没有待处理问题，只能说本次 compile 没有新候选。"],
                ),
            )
        payload = {"created": created}
        event, idem = self.append_event(
            "candidate_compiled",
            payload,
            idempotency_key=f"compile:{sha256_json(created)}",
            lock_exempt_reason="compile_from_capture",
            claim_status=claim_status(
                verified=["已从 correction capture 生成 candidate patch 和 review item。"],
                cannot_claim=["不能说候选菜谱已经正式生效。"],
            ),
        )
        return command_result(
            "compile",
            idem,
            files_written=files + [str(self.events_path)],
            objects_created=[item["patch_id"] for item in created] + [item["review_id"] for item in created] + [event["event_id"]],
            claim_status=event["claim_status"],
            extra={"created": created},
        )

    def accept_review(self, review_id: str, *, lock_id: str | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        review_path = self.recipes_dir / "review_queue" / f"{review_id}.json"
        if not review_path.exists():
            raise RecipesError("AR230", "review item 不存在。", f"找不到 {review_id}", "先运行 agent-recipes compile。")
        review = read_json(review_path, {})
        if review.get("status") == "accepted":
            return command_result(
                "review accept",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=["review item 已经 accepted，未重复晋升 recipe。"],
                    cannot_claim=[],
                ),
                extra={"review": review},
            )
        if review.get("status") not in {"pending"}:
            raise RecipesError(
                "AR417",
                "review item 已有最终决策，不能改写历史。",
                f"review_id={review_id}, status={review.get('status')}",
                "如需变更，重新生成候选或创建 supersede/recover 补丁。",
            )
        patch_path = self.recipes_dir / "candidates" / f"{review['proposed_patch_id']}.json"
        patch = read_json(patch_path, None)
        if not patch:
            raise RecipesError("AR231", "candidate patch 不存在。", f"找不到 {review['proposed_patch_id']}", "重新运行 compile。")
        if patch.get("patch_type") == "knowledge_fusion_candidate_set":
            raise RecipesError(
                "AR416",
                "knowledge_fusion review 不能用普通 accept 直接接受。",
                "fusion 候选必须明确选择 merge、split、supersede 或 reject，不能绕过决策类型写正式 recipe。",
                "改用 agent-recipes review --merge/--split/--supersede，或 review --reject。",
            )
        if source_refinery_review_requires_split(review, patch):
            raise RecipesError(
                "AR419",
                "这个 source_refinery 候选太大，不能直接 accept。",
                "review_hints 建议 split_before_accept；直接接受会把过多规则混进同一个正式 recipe。",
                "先运行 review --split 或 review --reject，必要时重新 refine 更窄的 knowledge_need。",
            )
        target_recipe_id = patch["target_recipe_id"]
        proposed = patch["proposed_change"]
        if proposed.get("recipe_id") != target_recipe_id:
            raise RecipesError(
                "AR232",
                "candidate patch 的 recipe_id 不一致。",
                f"target_recipe_id={target_recipe_id}, proposed.recipe_id={proposed.get('recipe_id')}",
                "重新生成 candidate patch。",
            )
        initial_promotion = not recipe_exists(self.recipes_dir, target_recipe_id)
        if not initial_promotion:
            if not lock_id:
                raise RecipesError(
                    "AR411",
                    "修改已有正式菜谱必须带 active lock。",
                    "没有 lock_id，无法证明修改基于哪份 recipe version。",
                    "先运行 lookup + lock，再 accept 修改。",
                )
            lock = self.validate_lock(lock_id)
            if target_recipe_id not in lock.get("recipe_ids", []):
                raise RecipesError(
                    "AR415",
                    "active lock 没有覆盖目标 recipe，停止写入。",
                    f"lock_id={lock_id}, target_recipe_id={target_recipe_id}",
                    "先对目标 recipe 运行 lookup + lock，再 accept 修改。",
                )
            lock_exempt_reason = None
        else:
            lock_exempt_reason = "initial_recipe_promotion"
        if initial_promotion:
            recipe = proposed
            recipe["version"] = 1
        else:
            current = self.load_recipe(target_recipe_id)
            recipe = merge_recipe_update(current, proposed)
            recipe["version"] = int(current.get("version", 0)) + 1
        recipe["recipe_hash"] = recipe_hash(recipe)
        self.assert_recipe_can_be_promoted(recipe)
        recipe_path = recipe_path_for(self.recipes_dir, recipe["recipe_id"])
        payload = {
            "decision": "accept",
            "review_id": review_id,
            "patch_id": patch["patch_id"],
            "recipe_id": recipe["recipe_id"],
            "recipe_hash": recipe["recipe_hash"],
        }
        event, idem = self.append_event(
            "review_decided",
            payload,
            lock_id=lock_id,
            lock_exempt_reason=lock_exempt_reason,
            idempotency_key=f"review:accept:{review_id}",
            claim_status=claim_status(
                verified=["用户或授权主控接受 review item 后生成正式 recipe version。"],
                cannot_claim=["不能说 recipe 已在真实任务中验证成功。"],
            ),
        )
        write_json(recipe_path, recipe)
        review["status"] = "accepted"
        review["decided_by"] = "codex"
        review["decided_at"] = event["created_at"]
        review["recipe_id"] = recipe["recipe_id"]
        write_json(review_path, review)
        retired_lock_paths = self.retire_stale_locks_for_recipe(
            recipe["recipe_id"],
            recipe["recipe_hash"],
            superseded_at=event["created_at"],
            superseded_by_event_id=event["event_id"],
        )
        return command_result(
            "review accept",
            idem,
            files_written=[str(recipe_path), str(review_path), *retired_lock_paths, str(self.events_path)],
            objects_created=[recipe["recipe_id"], event["event_id"]],
            objects_updated=[Path(path).stem for path in retired_lock_paths],
            claim_status=event["claim_status"],
            extra={"recipe_id": recipe["recipe_id"], "recipe_hash": recipe["recipe_hash"]},
        )

    def decide_fusion_review(self, review_id: str, *, decision: str, lock_id: str | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        if decision not in {"merge", "split", "supersede"}:
            raise RecipesError(
                "AR416",
                "knowledge_fusion review 决策类型不支持。",
                f"decision={decision}",
                "使用 --merge、--split、--supersede 或 --reject。",
            )
        status_by_decision = {"merge": "merged", "split": "split", "supersede": "superseded"}
        final_status = status_by_decision[decision]
        review_path = self.recipes_dir / "review_queue" / f"{review_id}.json"
        if not review_path.exists():
            raise RecipesError("AR233", "review item 不存在。", f"找不到 {review_id}", "先运行 agent-recipes knowledge-fusion。")
        review = read_json(review_path, {})
        if review.get("status") == final_status:
            return command_result(
                f"review {decision}",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=[f"review item 已经 {final_status}，未重复写入 recipe。"],
                    cannot_claim=["不能说重复执行产生了新的正式 recipe。"],
                ),
                extra={"review": review, "created_recipe_ids": review.get("created_recipe_ids", [])},
            )
        if review.get("status") not in {"pending"}:
            raise RecipesError(
                "AR417",
                "review item 已有最终决策，不能改写历史。",
                f"review_id={review_id}, status={review.get('status')}",
                "如需变更，重新生成候选或创建 supersede/recover 补丁。",
            )
        patch_id = review.get("proposed_patch_id")
        patch_path = self.recipes_dir / "candidates" / f"{patch_id}.json" if patch_id else None
        patch = read_json(patch_path, {}) if patch_path and patch_path.exists() else {}
        if not patch:
            raise RecipesError("AR231", "candidate patch 不存在。", f"找不到 {patch_id}", "重新运行 knowledge-fusion。")
        if patch.get("patch_type") != "knowledge_fusion_candidate_set":
            if patch.get("patch_type") == "source_refinery_patch_draft" and decision in {"split", "supersede"}:
                return self.decide_source_refinery_review(
                    review_id,
                    review_path=review_path,
                    review=review,
                    patch_path=patch_path,
                    patch=patch,
                    decision=decision,
                    lock_id=lock_id,
                )
            raise RecipesError(
                "AR418",
                "这个 review 不是 knowledge_fusion 候选。",
                f"patch_type={patch.get('patch_type')}",
                "普通候选继续用 review --accept/--reject；fusion 候选用 --merge/--split/--supersede。",
            )

        target_recipe_id = str(patch.get("target_recipe_id") or "")
        source_fusion_id = str(patch.get("source_fusion_id") or "")
        recipes_to_write: list[dict[str, Any]] = []
        lock_exempt_reason: str | None = None
        objects_updated: list[str] = []
        if decision == "merge":
            recipe = recipe_from_fusion_merge(target_recipe_id, patch)
            initial_promotion = not recipe_exists(self.recipes_dir, target_recipe_id)
            if initial_promotion:
                lock_exempt_reason = "initial_fusion_recipe_promotion"
                recipe["version"] = 1
            else:
                if not lock_id:
                    raise RecipesError(
                        "AR411",
                        "修改已有正式菜谱必须带 active lock。",
                        "没有 lock_id，无法证明修改基于哪份 recipe version。",
                        "先运行 lookup + lock，再 review --merge。",
                    )
                lock = self.validate_lock(lock_id)
                if target_recipe_id not in lock.get("recipe_ids", []):
                    raise RecipesError(
                        "AR415",
                        "active lock 没有覆盖目标 recipe，停止写入。",
                        f"lock_id={lock_id}, target_recipe_id={target_recipe_id}",
                        "先对目标 recipe 运行 lookup + lock，再 review --merge。",
                    )
                current = self.load_recipe(target_recipe_id)
                recipe = merge_recipe_update(current, recipe)
                recipe["version"] = int(current.get("version", 0)) + 1
                objects_updated.append(target_recipe_id)
            recipe["recipe_hash"] = recipe_hash(recipe)
            recipes_to_write.append(recipe)
        elif decision == "split":
            recipes_to_write = recipes_from_fusion_split(target_recipe_id, patch)
            lock_exempt_reason = "knowledge_fusion_split_new_recipes"
        else:
            if not recipe_exists(self.recipes_dir, target_recipe_id):
                raise RecipesError(
                    "AR419",
                    "supersede 需要已有正式 recipe。",
                    f"target_recipe_id={target_recipe_id}",
                    "先 review --merge 生成初版，或改用 review --split。",
                )
            if not lock_id:
                raise RecipesError(
                    "AR411",
                    "supersede 已有正式菜谱必须带 active lock。",
                    "没有 lock_id，无法证明替换建议基于哪份 recipe version。",
                    "先运行 lookup + lock，再 review --supersede。",
                )
            lock = self.validate_lock(lock_id)
            if target_recipe_id not in lock.get("recipe_ids", []):
                raise RecipesError(
                    "AR415",
                    "active lock 没有覆盖目标 recipe，停止写入。",
                    f"lock_id={lock_id}, target_recipe_id={target_recipe_id}",
                    "先对目标 recipe 运行 lookup + lock，再 review --supersede。",
                )
            recipe = recipe_from_fusion_supersede(target_recipe_id, patch)
            recipe["version"] = 1
            recipe["recipe_hash"] = recipe_hash(recipe)
            recipes_to_write.append(recipe)

        if not recipes_to_write:
            raise RecipesError(
                "AR420",
                "knowledge_fusion 没有可写入的正式 recipe 候选。",
                f"review_id={review_id}, decision={decision}",
                "先 reject，或补充更多 source_refinery cards 后重新运行 knowledge-fusion。",
            )

        for recipe in recipes_to_write:
            self.assert_recipe_can_be_promoted(recipe)

        recipe_ids = [str(recipe["recipe_id"]) for recipe in recipes_to_write]
        payload = {
            "decision": decision,
            "review_id": review_id,
            "patch_id": patch["patch_id"],
            "target_recipe_id": target_recipe_id,
            "source_fusion_id": source_fusion_id,
            "recipe_ids": recipe_ids,
            "recipe_hashes": [recipe["recipe_hash"] for recipe in recipes_to_write],
        }
        event, idem = self.append_event(
            "knowledge_fusion_review_decided",
            payload,
            lock_id=lock_id,
            lock_exempt_reason=lock_exempt_reason,
            idempotency_key=f"review:{decision}:{review_id}:{lock_id or ''}",
            claim_status=claim_status(
                verified=[f"已通过 review --{decision} 处理 knowledge_fusion 候选，并写入正式 recipe 文件。"],
                cannot_claim=[
                    f"不能说 knowledge_fusion {decision} 已经在真实任务中验证。",
                    "不能说未被本次 review 决策覆盖的候选已经自动吸收。",
                ],
            ),
        )

        recipe_paths: list[Path] = []
        for recipe in recipes_to_write:
            recipe_path = recipe_path_for(self.recipes_dir, str(recipe["recipe_id"]))
            write_json(recipe_path, recipe)
            recipe_paths.append(recipe_path)

        review["status"] = final_status
        review["decision"] = decision
        review["decided_by"] = "codex"
        review["decided_at"] = event["created_at"]
        review["created_recipe_ids"] = recipe_ids
        write_json(review_path, review)

        patch["status"] = final_status
        patch["decision"] = decision
        patch["decided_at"] = event["created_at"]
        patch["created_recipe_ids"] = recipe_ids
        if patch_path:
            write_json(patch_path, patch)

        fusion_files = self.mark_fusion_decided(source_fusion_id, decision=decision, review_id=review_id, recipe_ids=recipe_ids)
        files_written = [str(path) for path in recipe_paths]
        if patch_path:
            files_written.append(str(patch_path))
        files_written.extend([str(review_path), *fusion_files, str(self.events_path)])
        extra: dict[str, Any] = {"created_recipe_ids": recipe_ids}
        if decision == "merge":
            extra["recipe_id"] = recipe_ids[0]
            extra["recipe_hash"] = recipes_to_write[0]["recipe_hash"]
        return command_result(
            f"review {decision}",
            idem,
            files_written=files_written,
            objects_created=recipe_ids + ([event["event_id"]] if idem == "created" else []),
            objects_updated=objects_updated,
            claim_status=event["claim_status"],
            extra=extra,
        )

    def decide_source_refinery_review(
        self,
        review_id: str,
        *,
        review_path: Path,
        review: dict[str, Any],
        patch_path: Path | None,
        patch: dict[str, Any],
        decision: str,
        lock_id: str | None,
    ) -> dict[str, Any]:
        target_recipe_id = str(patch.get("target_recipe_id") or "")
        recipes_to_write: list[dict[str, Any]]
        lock_exempt_reason: str | None = None
        if decision == "split":
            recipes_to_write = recipes_from_source_refinery_split(target_recipe_id, patch)
            lock_exempt_reason = "source_refinery_split_new_recipes"
        elif decision == "supersede":
            if not recipe_exists(self.recipes_dir, target_recipe_id):
                raise RecipesError(
                    "AR419",
                    "supersede 需要已有正式 recipe。",
                    f"target_recipe_id={target_recipe_id}",
                    "先 review --accept 生成初版，或改用 review --split。",
                )
            if not lock_id:
                raise RecipesError(
                    "AR411",
                    "supersede 已有正式菜谱必须带 active lock。",
                    "没有 lock_id，无法证明替换建议基于哪份 recipe version。",
                    "先运行 lookup + lock，再 review --supersede。",
                )
            lock = self.validate_lock(lock_id)
            if target_recipe_id not in lock.get("recipe_ids", []):
                raise RecipesError(
                    "AR415",
                    "active lock 没有覆盖目标 recipe，停止写入。",
                    f"lock_id={lock_id}, target_recipe_id={target_recipe_id}",
                    "先对目标 recipe 运行 lookup + lock，再 review --supersede。",
                )
            recipes_to_write = [recipe_from_source_refinery_supersede(target_recipe_id, patch)]
        else:
            raise RecipesError(
                "AR418",
                "source_refinery review 决策类型不支持。",
                f"decision={decision}",
                "普通候选支持 accept/reject/split/supersede。",
            )

        if not recipes_to_write:
            raise RecipesError(
                "AR420",
                "source_refinery 没有可写入的拆分 recipe 候选。",
                f"review_id={review_id}, decision={decision}",
                "先 reject，或重新 refine 更窄的 knowledge_need。",
            )

        for recipe in recipes_to_write:
            self.assert_recipe_can_be_promoted(recipe)

        recipe_ids = [str(recipe["recipe_id"]) for recipe in recipes_to_write]
        payload = {
            "decision": decision,
            "review_id": review_id,
            "patch_id": patch.get("patch_id"),
            "target_recipe_id": target_recipe_id,
            "recipe_ids": recipe_ids,
            "recipe_hashes": [recipe["recipe_hash"] for recipe in recipes_to_write],
        }
        event, idem = self.append_event(
            "source_refinery_review_decided",
            payload,
            lock_id=lock_id,
            lock_exempt_reason=lock_exempt_reason,
            idempotency_key=f"review:{decision}:{review_id}:{lock_id or ''}",
            claim_status=claim_status(
                verified=[f"已通过 review --{decision} 处理 source_refinery 候选，并写入正式 recipe 文件。"],
                cannot_claim=[
                    f"不能说 source_refinery {decision} 已经在真实任务中验证。",
                    "不能说未被本次 review 决策覆盖的候选已经自动吸收。",
                ],
            ),
        )

        recipe_paths: list[Path] = []
        for recipe in recipes_to_write:
            recipe_path = recipe_path_for(self.recipes_dir, str(recipe["recipe_id"]))
            write_json(recipe_path, recipe)
            recipe_paths.append(recipe_path)

        final_status = "split" if decision == "split" else "superseded"
        review["status"] = final_status
        review["decision"] = decision
        review["decided_by"] = "codex"
        review["decided_at"] = event["created_at"]
        review["created_recipe_ids"] = recipe_ids
        write_json(review_path, review)

        patch["status"] = final_status
        patch["decision"] = decision
        patch["decided_at"] = event["created_at"]
        patch["created_recipe_ids"] = recipe_ids
        if patch_path:
            write_json(patch_path, patch)

        files_written = [str(path) for path in recipe_paths]
        if patch_path:
            files_written.append(str(patch_path))
        files_written.extend([str(review_path), str(self.events_path)])
        return command_result(
            f"review {decision}",
            idem,
            files_written=files_written,
            objects_created=recipe_ids + ([event["event_id"]] if idem == "created" else []),
            claim_status=event["claim_status"],
            extra={"created_recipe_ids": recipe_ids},
        )

    def mark_fusion_decided(self, fusion_id: str, *, decision: str, review_id: str, recipe_ids: list[str]) -> list[str]:
        if not fusion_id:
            return []
        changed: list[str] = []
        fusion_path = self.recipes_dir / "source_refinery" / "fusion" / f"{fusion_id}.json"
        if fusion_path.exists():
            fusion_doc = read_json(fusion_path, {})
            fusion_doc["status"] = f"{decision}_reviewed"
            fusion_doc["decision"] = decision
            fusion_doc["review_id"] = review_id
            fusion_doc["created_recipe_ids"] = recipe_ids
            write_json(fusion_path, fusion_doc)
            changed.append(str(fusion_path))
        index_path = self.recipes_dir / "source_refinery" / "fusion" / "fusion.jsonl"
        if index_path.exists():
            rows = read_jsonl(index_path)
            updated_rows: list[dict[str, Any]] = []
            touched = False
            for row in rows:
                if row.get("fusion_id") == fusion_id:
                    row = dict(row)
                    row["status"] = f"{decision}_reviewed"
                    row["decision"] = decision
                    row["review_id"] = review_id
                    row["created_recipe_ids"] = recipe_ids
                    touched = True
                updated_rows.append(row)
            if touched:
                write_jsonl(index_path, updated_rows)
                changed.append(str(index_path))
        return changed

    def reject_review(self, review_id: str, *, reason: str = "") -> dict[str, Any]:
        self.ensure_dirs()
        review_path = self.recipes_dir / "review_queue" / f"{review_id}.json"
        if not review_path.exists():
            raise RecipesError("AR233", "review item 不存在。", f"找不到 {review_id}", "先运行 agent-recipes compile 或 patch-draft。")
        review = read_json(review_path, {})
        if review.get("status") == "accepted":
            raise RecipesError(
                "AR234",
                "已接受的 review 不能用 reject 改回。",
                f"review_id={review_id}",
                "如需撤销，创建 supersede/recover 候选补丁，不要改写历史。",
            )
        if review.get("status") == "rejected":
            return command_result(
                "review reject",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=["review item 已经 rejected，未重复写入。"],
                    cannot_claim=["不能说 rejection 已删除候选历史。"],
                ),
                extra={"review": review},
            )
        patch_id = review.get("proposed_patch_id")
        patch_path = self.recipes_dir / "candidates" / f"{patch_id}.json" if patch_id else None
        patch = read_json(patch_path, {}) if patch_path and patch_path.exists() else {}
        payload = {
            "decision": "reject",
            "review_id": review_id,
            "patch_id": patch_id,
            "reason": reason,
        }
        event, idem = self.append_event(
            "review_decided",
            payload,
            lock_exempt_reason="review_rejection_no_recipe_write",
            idempotency_key=f"review:reject:{review_id}:{sha256_text(reason)}",
            claim_status=claim_status(
                verified=["已把 review item 标记为 rejected，未生成正式 recipe。"],
                cannot_claim=[
                    "不能说 rejected 候选已删除。",
                    "不能说正式 recipe 已被修改。",
                ],
            ),
        )
        review["status"] = "rejected"
        review["decision_reason"] = reason
        review["decided_by"] = "codex"
        review["decided_at"] = event["created_at"]
        write_json(review_path, review)
        files = [str(review_path), str(self.events_path)]
        if patch_path and patch:
            patch["status"] = "rejected"
            patch["decision_reason"] = reason
            write_json(patch_path, patch)
            files.insert(0, str(patch_path))
        return command_result(
            "review reject",
            idem,
            files_written=files,
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"review": review},
        )

    def recipe_lifecycle_status(self, recipe_id: str | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        state = recipe_lifecycle_state(self.load_events())
        tombstones = state["tombstones"]
        if recipe_id:
            tombstones = [item for item in tombstones if item.get("recipe_id") == recipe_id]
        return {
            "ok": not state["errors"],
            "action": "recipe-lifecycle status",
            "recipe_id": recipe_id,
            "retired": bool(recipe_id and recipe_id in state["retired_recipe_ids"]),
            "tombstones": tombstones,
            "active_tombstones": [item for item in tombstones if not item.get("revoked")],
            "revoked_tombstones": [item for item in tombstones if item.get("revoked")],
            "summary": recipe_lifecycle_summary(state),
            "errors": state["errors"],
            "claim_status": claim_status(
                verified=["已从 append-only event log 重建 recipe lifecycle 状态。"],
                cannot_claim=[
                    "不能说 revocation 会直接恢复旧 recipe；旧 recipe id 永久退役。",
                    "不能说 lifecycle status 代替人工 review。",
                ],
            ),
        }

    def tombstone_recipe(
        self,
        recipe_id: str,
        *,
        lock_id: str | None,
        reason_kind: str,
        reason: str,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        if reason_kind not in RECIPE_TOMBSTONE_REASONS:
            raise RecipesError(
                "AR433",
                "recipe tombstone reason-kind 不支持。",
                f"reason_kind={reason_kind}",
                f"使用：{', '.join(sorted(RECIPE_TOMBSTONE_REASONS))}",
            )
        if not reason.strip():
            raise RecipesError(
                "AR433",
                "recipe tombstone 必须说明具体原因。",
                "reason 为空。",
                "传入 --reason，说明为什么这条正式 recipe 必须退役。",
            )
        recipe = self.load_recipe(recipe_id)
        state = recipe_lifecycle_state(self.load_events())
        existing = next(
            (item for item in state["tombstones"] if item.get("recipe_id") == recipe_id),
            None,
        )
        if existing:
            return command_result(
                "recipe-lifecycle tombstone",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=["recipe id 已经永久退役，未重复创建 tombstone。"],
                    cannot_claim=["不能说重复 tombstone 会恢复或改写旧 recipe。"],
                ),
                extra={"tombstone": existing, "recipe_id": recipe_id},
            )
        if not lock_id:
            raise RecipesError(
                "AR410",
                "撤销正式 recipe 必须带 active lock。",
                "没有 lock_id，无法证明撤销的是哪份 recipe version/hash。",
                "先对目标 recipe 运行 lookup + lock，再执行 recipe-lifecycle --tombstone。",
            )
        lock = self.validate_lock(lock_id)
        if recipe_id not in lock.get("recipe_ids", []):
            raise RecipesError(
                "AR415",
                "active lock 没有覆盖待撤销 recipe。",
                f"lock_id={lock_id}, recipe_id={recipe_id}",
                "先对目标 recipe 创建 active lock。",
            )
        content_hash = recipe_content_hash(recipe)
        tombstone_id = make_id("tomb", recipe_id, recipe["recipe_hash"], content_hash)
        payload = {
            "tombstone_id": tombstone_id,
            "recipe_id": recipe_id,
            "recipe_version": recipe.get("version"),
            "recipe_hash": recipe["recipe_hash"],
            "content_hash": content_hash,
            "reason_kind": reason_kind,
            "reason": reason.strip(),
            "permanent_recipe_id_retirement": True,
        }
        event, idem = self.append_event(
            "recipe_tombstoned",
            payload,
            lock_id=lock_id,
            idempotency_key=f"recipe:tombstone:{tombstone_id}",
            claim_status=claim_status(
                verified=["已用 active lock 撤销正式 recipe，并把 tombstone 写入 append-only event log。"],
                cannot_claim=[
                    "不能说 recipe 文件已删除；历史文件被保留。",
                    "不能说 tombstone revocation 会直接恢复旧 recipe id。",
                ],
            ),
        )
        retired_lock_paths = self.retire_active_locks_for_recipe(
            recipe_id,
            status="tombstoned",
            retired_at=event["created_at"],
            retired_by_event_id=event["event_id"],
        )
        tombstone = dict(payload)
        tombstone.update({"event_id": event["event_id"], "created_at": event["created_at"], "revoked": False})
        return command_result(
            "recipe-lifecycle tombstone",
            idem,
            files_written=[*retired_lock_paths, str(self.events_path)],
            objects_created=[tombstone_id, event["event_id"]] if idem == "created" else [],
            objects_updated=[Path(path).stem for path in retired_lock_paths],
            claim_status=event["claim_status"],
            extra={"tombstone": tombstone, "recipe_id": recipe_id},
        )

    def revoke_recipe_tombstone(self, tombstone_id: str, *, reason: str) -> dict[str, Any]:
        self.ensure_dirs()
        if not reason.strip():
            raise RecipesError(
                "AR433",
                "撤销 tombstone 必须说明具体原因。",
                "reason 为空。",
                "传入 --reason；撤销后仍需新候选和人工 review，旧 recipe 不会恢复。",
            )
        state = recipe_lifecycle_state(self.load_events())
        tombstone = next((item for item in state["tombstones"] if item.get("tombstone_id") == tombstone_id), None)
        if not tombstone:
            raise RecipesError(
                "AR434",
                "recipe tombstone 不存在。",
                f"tombstone_id={tombstone_id}",
                "先运行 recipe-lifecycle --status 查看真实 tombstone id。",
            )
        if tombstone.get("revoked"):
            return command_result(
                "recipe-lifecycle revoke",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=["tombstone 已经 revoked，未重复写入。"],
                    cannot_claim=["不能说旧 recipe id 已恢复。"],
                ),
                extra={"tombstone": tombstone},
            )
        payload = {
            "tombstone_id": tombstone_id,
            "recipe_id": tombstone["recipe_id"],
            "content_hash": tombstone["content_hash"],
            "reason": reason.strip(),
            "old_recipe_id_remains_retired": True,
        }
        event, idem = self.append_event(
            "recipe_tombstone_revoked",
            payload,
            lock_exempt_reason="explicit_tombstone_revocation_without_recipe_reactivation",
            idempotency_key=f"recipe:tombstone:revoke:{tombstone_id}",
            claim_status=claim_status(
                verified=["已解除该 content hash 的防复活阻断；旧 recipe id 仍永久退役。"],
                cannot_claim=[
                    "不能说旧 recipe 已重新生效。",
                    "相同内容必须使用新 recipe id，并重新走 candidate + review 才能转正。",
                ],
            ),
        )
        return command_result(
            "recipe-lifecycle revoke",
            idem,
            files_written=[str(self.events_path)],
            objects_created=[event["event_id"]] if idem == "created" else [],
            claim_status=event["claim_status"],
            extra={"tombstone_id": tombstone_id, "recipe_id": tombstone["recipe_id"]},
        )

    def assert_recipe_can_be_promoted(self, recipe: dict[str, Any]) -> None:
        assert_recipe_promotable(self.load_events(), recipe)

    def lookup(self, query: str, *, strict: bool = False, min_score: int = 2) -> dict[str, Any]:
        self.ensure_dirs()
        all_recipes = self.load_recipes()
        state = recipe_lifecycle_state(self.load_events())
        result = lookup_execution_policy(
            query,
            all_recipes,
            retired_recipe_ids=set(state["retired_recipe_ids"]),
            recipes_dir=self.recipes_dir,
            strict=strict,
            min_score=min_score,
        )
        selected = result["recipe"]
        applicability = result["applicability"]
        return {
            "ok": True,
            "action": "lookup",
            "query": query,
            "recipe": selected,
            "applicability": applicability,
            "candidates": result["candidates"],
            "inactive_recipe_ids": result["inactive_recipe_ids"],
            "claim_status": claim_status(
                verified=[f"已从 recipes/ 读取 recipe：{selected['recipe_id']}"],
                missing_evidence=[] if applicability["status"] == "strong" else ["top recipe 未达到强适用阈值。"],
                cannot_claim=[
                    "不能说该 recipe 已适合所有相似任务，只能说 lookup 命中。",
                    "弱匹配不能直接 lock；需要 strict lookup 或 lock --query 通过。",
                ],
            ),
        }

    def create_lock(
        self,
        recipe_id: str,
        *,
        task: str = "",
        session_id: str | None = None,
        query: str | None = None,
        min_score: int = 2,
    ) -> dict[str, Any]:
        self.ensure_dirs()
        recipe = self.load_recipe(recipe_id)
        lifecycle = recipe_lifecycle_state(self.load_events())
        active_recipe_ids, inactive_recipe_ids = active_recipe_ids_for_consumption(
            self.load_recipes(),
            retired_recipe_ids=set(lifecycle["retired_recipe_ids"]),
        )
        if recipe_id not in active_recipe_ids:
            raise RecipesError(
                "AR432",
                "inactive 或已退役 recipe 不能创建 execution lock。",
                f"recipe_id={recipe_id}; inactive_recipe_ids={inactive_recipe_ids}",
                "重新 lookup active recipe，或创建新候选并通过人工 review。",
            )
        applicability: dict[str, Any] | None = None
        if query:
            lookup = self.lookup(query, strict=True, min_score=min_score)
            lookup_recipe_id = str(lookup["recipe"].get("recipe_id") or "")
            if lookup_recipe_id != recipe_id:
                raise RecipesError(
                    "AR421",
                    "lock query 命中的 recipe 与待锁 recipe 不一致。",
                    f"query={query}; lookup={lookup_recipe_id}; lock={recipe_id}",
                    "重新 lookup 后锁定命中的 recipe，或补充更精确的菜谱。",
                )
            applicability = lookup.get("applicability", {})
        outcome_state = outcome_quality_state(self.recipes_dir, self.load_events(), recipes=self.load_recipes())
        outcome_quality = find_outcome_quality(
            outcome_state,
            recipe_id=recipe_id,
            recipe_version=recipe.get("version"),
            recipe_hash_value=str(recipe.get("recipe_hash") or ""),
        )
        recommendation = str(outcome_quality.get("execution_recommendation") or "normal")
        if recommendation == "hold_for_review":
            raise RecipesError(
                "AR440",
                "该 recipe 的新执行已暂停，必须先人工复核。",
                (
                    f"recipe_id={recipe_id}; exact_version={recipe.get('version')}; "
                    f"policy_failures={outcome_quality.get('policy_eligible', {}).get('negative', 0)}; "
                    f"consecutive_failures={outcome_quality.get('policy_eligible', {}).get('consecutive_negative', 0)}"
                ),
                f"./bin/agent-recipes outcome-status --recipe {recipe_id} --project . --json",
            )
        sid = session_id or os.environ.get("AGENT_RECIPES_SESSION_ID", "local")
        lock_id = execution_lock_id(recipe, task=task, session_id=sid)
        lock_path = self.recipes_dir / "locks" / f"{lock_id}.json"
        if lock_path.exists():
            lock = read_json(lock_path, {})
            return command_result(
                "lock",
                "unchanged",
                files_written=[],
                objects_created=[],
                claim_status=claim_status(
                    verified=["active lock 已存在，未重复创建。"],
                    cannot_claim=[],
                ),
                extra={"lock": lock},
            )
        lock = build_execution_lock(
            recipe,
            lock_id=lock_id,
            task=task,
            session_id=sid,
            outcome_quality=outcome_quality,
            query=query,
            applicability=applicability,
        )
        payload = {"lock": lock}
        event, idem = self.append_event(
            "lock_created",
            payload,
            idempotency_key=f"lock:create:{lock_id}",
            lock_exempt_reason="lock_creation",
            claim_status=claim_status(
                verified=["已锁定 recipe id/version/hash 和 claim limits。"],
                cannot_claim=["不能说任务已完成，只能说执行前依据已锁定。"],
            ),
        )
        write_json(lock_path, lock)
        return command_result(
            "lock",
            idem,
            files_written=[str(lock_path), str(self.events_path)],
            objects_created=[lock_id, event["event_id"]],
            claim_status=event["claim_status"],
            extra={"lock": lock},
        )

    def outcome_status(self, *, recipe_id: str | None = None) -> dict[str, Any]:
        self.ensure_dirs()
        state = outcome_quality_state(self.recipes_dir, self.load_events(), recipes=self.load_recipes())
        rows = state["recipes"]
        if recipe_id:
            rows = [item for item in rows if item.get("recipe_id") == recipe_id]
        return {
            "ok": not state["binding_errors"],
            "action": "outcome-status",
            "schema_version": "1.0",
            "recipe_filter": recipe_id,
            "summary": outcome_quality_summary(rows, state=state),
            "recipes": rows,
            "binding_errors": state["binding_errors"],
            "unattributed_events": state["unattributed_events"],
            "policy": dict(OUTCOME_POLICY),
            "claim_status": claim_status(
                verified=["已按 recipe id/version/hash 统计可归因 success/failure/unknown。"],
                missing_evidence=[
                    f"有 {len(state['unattributed_events'])} 条旧结果无法追到完整 lock。"
                ] if state["unattributed_events"] else [],
                cannot_claim=[
                    "历史 lock 推导结果可以提示风险，但不能自动降级或暂停 recipe。",
                    "unknown 不算成功也不算失败，不改变置信度。",
                    "自动降级不会修改、接受、tombstone 或 supersede 正式 recipe。",
                ],
            ),
        }

    def validate_lock(self, lock_id: str) -> dict[str, Any]:
        lifecycle = recipe_lifecycle_state(self.load_events())
        return validate_execution_lock(
            self.recipes_dir,
            lock_id,
            retired_recipe_ids=set(lifecycle["retired_recipe_ids"]),
            load_recipe=self.load_recipe,
        )

    def retire_active_locks_for_recipe(
        self,
        recipe_id: str,
        *,
        status: str,
        retired_at: str,
        retired_by_event_id: str,
    ) -> list[str]:
        return retire_active_execution_locks(
            self.recipes_dir,
            recipe_id,
            status=status,
            retired_at=retired_at,
            retired_by_event_id=retired_by_event_id,
        )

    def retire_stale_locks_for_recipe(
        self,
        recipe_id: str,
        recipe_hash: str,
        *,
        superseded_at: str,
        superseded_by_event_id: str,
    ) -> list[str]:
        return retire_stale_execution_locks(
            self.recipes_dir,
            recipe_id,
            recipe_hash,
            superseded_at=superseded_at,
            superseded_by_event_id=superseded_by_event_id,
        )

    def readiness(self) -> dict[str, Any]:
        self.ensure_dirs()
        doctor = self.doctor()
        events = self.load_events()
        lifecycle = recipe_lifecycle_state(events)
        recipes = self.load_recipes()
        active_recipe_ids, inactive_recipe_ids = active_recipe_ids_for_consumption(
            recipes,
            retired_recipe_ids=set(lifecycle["retired_recipe_ids"]),
        )
        review = review_queue_readiness(self.recipes_dir)
        adapter = adapter_candidate_readiness(self.recipes_dir, events)
        recall_boundary = recall_boundary_state(self.recipes_dir, events)
        evidence_hardening = evidence_quarantine_state(self.root, self.recipes_dir)
        outcomes = outcome_quality_state(self.recipes_dir, events, recipes=recipes)
        active_outcome_rows = [
            item
            for item in outcomes["recipes"]
            if item.get("recipe_id") in active_recipe_ids
            and any(
                recipe.get("recipe_id") == item.get("recipe_id")
                and recipe.get("recipe_hash") == item.get("recipe_hash")
                for recipe in recipes
            )
        ]
        outcome_summary = outcome_quality_summary(active_outcome_rows, state=outcomes)
        client_configured = (self.root / ".agents" / "mcp" / "agent-recipes.json").exists()

        error_codes = {str(item.get("code") or "") for item in doctor.get("errors", [])}
        ledger_errors = sorted(code for code in error_codes if code in {"AR300", "AR301", "AR302", "AR303"})
        lifecycle_errors = sorted(code for code in error_codes if code in {"AR307", "AR308", "AR309", "AR310", "AR311"})
        outcome_errors = sorted(code for code in error_codes if code == "AR312")
        other_doctor_errors = sorted(error_codes - set(ledger_errors) - set(lifecycle_errors) - set(outcome_errors))

        axes = {
            "ledger": {
                "status": "blocked" if ledger_errors else "ready",
                "required": True,
                "event_count": len(events),
                "error_codes": ledger_errors,
            },
            "lifecycle": {
                "status": "blocked" if lifecycle_errors or lifecycle["errors"] else "ready",
                "required": True,
                "tombstone_count": len(lifecycle["tombstones"]),
                "retired_recipe_count": len(lifecycle["retired_recipe_ids"]),
                "error_codes": lifecycle_errors,
            },
            "recipes": {
                "status": "ready" if active_recipe_ids else "degraded",
                "required": True,
                "active_count": len(active_recipe_ids),
                "inactive_count": len(inactive_recipe_ids),
            },
            "review_queue": {
                "status": "degraded" if review["malformed_count"] else "ready",
                "required": False,
                "pending_count": review["pending_count"],
                "total_count": review["total_count"],
                "malformed_count": review["malformed_count"],
            },
            "outcomes": {
                "status": "blocked" if outcome_errors else (
                    "degraded"
                    if outcome_summary["hard_hold_count"]
                    or outcome_summary["degraded_count"]
                    or outcome_summary["historical_warning_count"]
                    else "ready"
                ),
                "required": True,
                **outcome_summary,
                "legacy_inferred_can_enforce": False,
            },
            "recall_boundary": {
                "status": recall_boundary["status"],
                "required": True,
                "affects_core_readiness": False,
                "all_adapters_disabled": recall_boundary["all_adapters_disabled"],
                "active_adapter_count": recall_boundary["active_adapter_count"],
                "disabled_adapter_count": recall_boundary["disabled_adapter_count"],
                "violation_count": recall_boundary["violation_count"],
                "core_requires_recall_adapter": False,
            },
            "evidence_hardening": {
                "status": "blocked" if evidence_hardening["authoritative_secret_finding_count"] else (
                    "degraded"
                    if evidence_hardening["issue_count"] or evidence_hardening["active_quarantine_count"]
                    else "ready"
                ),
                "required": True,
                "affects_core_readiness": False,
                "issue_count": evidence_hardening["issue_count"],
                "active_quarantine_count": evidence_hardening["active_quarantine_count"],
                "secret_finding_count": evidence_hardening["secret_finding_count"],
                "authoritative_secret_finding_count": evidence_hardening["authoritative_secret_finding_count"],
                "pre_persistence_redaction": True,
            },
            "optional_adapters": {
                "status": "ready" if adapter["runtime_verified_count"] else "disabled",
                "required": False,
                "runtime_verified_count": adapter["runtime_verified_count"],
                "candidate_only": True,
                "affects_core_readiness": False,
            },
            "real_client": {
                "status": "unknown" if client_configured else "disabled",
                "required": False,
                "project_config_present": client_configured,
                "fresh_tool_call_verified": False,
            },
        }

        core_blocked = bool(ledger_errors or lifecycle_errors or lifecycle["errors"] or outcome_errors or other_doctor_errors)
        core_degraded = (
            not active_recipe_ids
            or bool(review["malformed_count"])
            or bool(outcome_summary["hard_hold_count"])
            or bool(outcome_summary["degraded_count"])
            or bool(outcome_summary["historical_warning_count"])
        )
        overall = "blocked" if core_blocked else ("degraded" if core_degraded else "ready")
        realized_mode = "core_plus_optional_candidates" if adapter["runtime_verified_count"] else "core_only"

        recommended_actions: list[dict[str, Any]] = []
        if core_blocked:
            recommended_actions.append(
                readiness_action(
                    "repair-core",
                    priority="P0",
                    blocking=True,
                    command="./bin/agent-recipes doctor --project . --json",
                    reason="核心账本、生命周期或 doctor 检查存在错误；所有正式 mutation 必须停止。",
                )
            )
        if not active_recipe_ids:
            recommended_actions.append(
                readiness_action(
                    "promote-first-recipe",
                    priority="P0",
                    blocking=True,
                    command="./bin/agent-recipes compile --project . --json",
                    reason="当前没有 active formal recipe；系统只能收集候选，不能提供执行依据。",
                )
            )
        if outcome_errors:
            recommended_actions.append(
                readiness_action(
                    "repair-outcome-bindings",
                    priority="P0",
                    blocking=True,
                    command="./bin/agent-recipes doctor --project . --json",
                    reason="新结果的 recipe id/version/hash 与 lock 不一致；正式 mutation 必须停止。",
                )
            )
        elif outcome_summary["hard_hold_count"] or outcome_summary["degraded_count"] or outcome_summary["historical_warning_count"]:
            recommended_actions.append(
                readiness_action(
                    "review-outcome-quality",
                    priority="P1",
                    blocking=False,
                    command="./bin/agent-recipes outcome-status --project . --json",
                    reason="部分 active recipe 有失败风险；逐条复核，只有新显式结果会触发自动执法。",
                )
            )
        if recall_boundary["violations"]:
            recommended_actions.append(
                readiness_action(
                    "disable-broken-recall",
                    priority="P1",
                    blocking=False,
                    command="./bin/agent-recipes recall-boundary --project . --json",
                    reason="可选 recall 派生索引违反候选合同；停用对应 recall，核心治理链可继续运行。",
                )
            )
        if evidence_hardening["issue_count"]:
            recommended_actions.append(
                readiness_action(
                    "quarantine-bad-evidence",
                    priority="P1",
                    blocking=False,
                    command="./bin/agent-recipes evidence-quarantine --action apply --project . --json",
                    reason="候选/派生证据 malformed 或含凭证；先移出 active path，核心链可继续。",
                )
            )
        elif evidence_hardening["active_quarantine_count"]:
            recommended_actions.append(
                readiness_action(
                    "review-quarantine",
                    priority="P2",
                    blocking=False,
                    command="./bin/agent-recipes evidence-quarantine --action status --project . --json",
                    reason="存在已隔离证据；修好后必须显式 release 才能回到 active path。",
                )
            )
        if review["malformed_count"]:
            recommended_actions.append(
                readiness_action(
                    "quarantine-malformed-review",
                    priority="P0",
                    blocking=True,
                    command="./bin/agent-recipes readiness --project . --json",
                    reason="review_queue 存在无法解析的条目；不要继续 review mutation。",
                )
            )
        elif review["pending_count"]:
            recommended_actions.append(
                readiness_action(
                    "review-pending-candidates",
                    priority="P1",
                    blocking=False,
                    command="./bin/agent-recipes review-packet --project . --json",
                    reason="存在 pending candidate；正式 recipe 不会自动改变。",
                )
            )
        if client_configured:
            recommended_actions.append(
                readiness_action(
                    "verify-real-client",
                    priority="P2",
                    blocking=False,
                    command="./bin/agent-recipes client-smoke --agent codex --scope project --project . --json",
                    reason="项目配置存在，但没有 fresh real-client tool-call 证据。",
                )
            )
        if not recommended_actions:
            recommended_actions.append(
                readiness_action(
                    "lookup-strict",
                    priority="P2",
                    blocking=False,
                    command="./bin/agent-recipes lookup '<task>' --strict --project . --json",
                    reason="核心可用；下一步应对真实任务做 strict lookup，而不是直接执行。",
                )
            )

        missing_evidence: list[str] = []
        if client_configured:
            missing_evidence.append("尚无 fresh real-client tool call 证明当前客户端已加载新增 readiness 工具。")
        if not active_recipe_ids:
            missing_evidence.append("没有 active formal recipe 可供 lookup/lock。")
        return {
            "ok": overall != "blocked",
            "action": "readiness",
            "schema_version": "1.3",
            "overall": overall,
            "realized_mode": realized_mode,
            "contracts": {
                "authoritative_truth": "hash_chained_events_plus_hash_verified_formal_recipes",
                "derived_assets_are_authoritative": False,
                "read_policy": "degrade_with_disclosure",
                "mutation_policy": "fail_closed",
            },
            "axes": axes,
            "recommended_action": recommended_actions[0],
            "recommended_actions": recommended_actions,
            "claim_status": claim_status(
                verified=[
                    "已分别检查 ledger、lifecycle、formal recipes、outcomes、recall boundary、evidence hardening、review queue、optional adapters 和 project client config。",
                    f"readiness overall={overall}, realized_mode={realized_mode}。",
                ],
                missing_evidence=missing_evidence,
                cannot_claim=[
                    "readiness=ready 不等于具体 recipe 适用于当前任务；仍须 strict lookup + lock。",
                    "optional adapter ready 不等于其候选已转成正式 recipe。",
                    "project client config 存在不等于真实 Codex/Claude/Hermes 已加载 MCP。",
                    "readiness 不证明真实任务输出质量或用户验收。",
                ],
            ),
        }

    def doctor(self) -> dict[str, Any]:
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        if not self.recipes_dir.exists():
            errors.append({"code": "AR001", "message": ".recipes 不存在。"})
            return doctor_report(errors, warnings, root=self.root)
        schema = project_schema_status(self.recipes_dir)
        if schema["state"] in {"malformed", "unsupported_newer"}:
            errors.append({"code": "AR317", "message": f"项目 schema 不可安全读取：{schema['state']}"})
        elif schema["migration_required"]:
            warnings.append({"code": "AR318", "message": f"项目 schema 需要显式迁移：{schema['state']}"})
        ledger_inspection = self._ledger.inspect()
        events = ledger_inspection["events"]
        errors.extend(ledger_inspection["errors"])
        lifecycle = recipe_lifecycle_state(events)
        for lifecycle_error in lifecycle["errors"]:
            errors.append({"code": "AR307", "message": lifecycle_error})
        for tombstone in lifecycle["tombstones"]:
            recipe_id = str(tombstone.get("recipe_id") or "")
            recipe_path = recipe_path_for(self.recipes_dir, recipe_id)
            if not recipe_path.exists():
                errors.append({"code": "AR308", "message": f"tombstone 引用的 recipe 不存在：{recipe_id}"})
                continue
            recipe = read_json(recipe_path, {})
            if recipe.get("recipe_hash") != tombstone.get("recipe_hash"):
                errors.append({"code": "AR309", "message": f"已退役 recipe hash 被改写：{recipe_id}"})
            if recipe_content_hash(recipe) != tombstone.get("content_hash"):
                errors.append({"code": "AR310", "message": f"已退役 recipe 内容被改写：{recipe_id}"})
        retired_recipe_ids = set(lifecycle["retired_recipe_ids"])
        for lock_path in (self.recipes_dir / "locks").glob("*.json"):
            lock = read_json(lock_path, {})
            if lock.get("status") == "active":
                for recipe_id, expected_hash in zip(lock.get("recipe_ids", []), lock.get("recipe_hashes", [])):
                    if recipe_id in retired_recipe_ids:
                        errors.append({"code": "AR311", "message": f"active lock 引用已退役 recipe：{lock.get('lock_id')}"})
                        continue
                    try:
                        recipe = self.load_recipe(recipe_id)
                    except RecipesError:
                        errors.append({"code": "AR304", "message": f"lock 引用的 recipe 不存在：{lock.get('lock_id')}"})
                        continue
                    if recipe.get("recipe_hash") != expected_hash:
                        errors.append({"code": "AR305", "message": f"lock 引用的 recipe hash 已变化：{lock.get('lock_id')}"})
        outcomes = outcome_quality_state(self.recipes_dir, events, recipes=self.load_recipes())
        for outcome_error in outcomes["binding_errors"]:
            errors.append({"code": "AR312", "message": outcome_error})
        for event_id in outcomes["unattributed_events"]:
            warnings.append({"code": "AR313", "message": f"旧 outcome 无法归因到完整 lock：{event_id}"})
        recall_boundary = recall_boundary_state(self.recipes_dir, events)
        for violation in recall_boundary["violations"]:
            warnings.append({"code": "AR314", "message": violation})
        evidence_hardening = evidence_quarantine_state(self.root, self.recipes_dir)
        for finding in evidence_hardening["authoritative_secret_findings"]:
            errors.append({"code": "AR315", "message": f"权威文件含凭证模式：{finding['relative_path']}"})
        for issue in evidence_hardening["issues"]:
            warnings.append({"code": "AR316", "message": f"候选证据需隔离：{issue['relative_path']} ({issue['reason_kind']})"})
        sources = read_json(self.recipes_dir / "source_index" / "sources.json", [])
        for source in sources:
            if not Path(source["path"]).exists():
                warnings.append({"code": "AR306", "message": f"source 文件已不存在：{source['path']}"})
        return doctor_report(errors, warnings, root=self.root, events=events, sources=sources)

    def load_recipe(self, recipe_id: str) -> dict[str, Any]:
        path = recipe_path_for(self.recipes_dir, recipe_id)
        if not path.exists():
            raise RecipesError("AR241", "recipe 不存在。", f"找不到 {recipe_id}", "先运行 review --accept。")
        return read_json(path, {})

    def load_recipes(self) -> list[dict[str, Any]]:
        recipes = []
        for path in (self.recipes_dir / "recipes").glob("*.json"):
            recipes.append(read_json(path, {}))
        return recipes



def empty_claim_status() -> dict[str, list[str]]:
    return {"verified": [], "inferred": [], "missing_evidence": [], "cannot_claim": []}


def claim_status(
    *,
    verified: list[str] | None = None,
    inferred: list[str] | None = None,
    missing_evidence: list[str] | None = None,
    cannot_claim: list[str] | None = None,
) -> dict[str, list[str]]:
    return {
        "verified": verified or [],
        "inferred": inferred or [],
        "missing_evidence": missing_evidence or [],
        "cannot_claim": cannot_claim or [],
    }


def command_result(
    action: str,
    idempotency_status: str,
    *,
    files_written: list[str],
    objects_created: list[str],
    claim_status: dict[str, list[str]],
    objects_updated: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = {
        "ok": True,
        "action": action,
        "idempotency_status": idempotency_status,
        "files_written": files_written,
        "objects_created": objects_created,
        "objects_updated": objects_updated or [],
        "previous_hash": None,
        "new_hash": None,
        "rollback": "Phase 0A uses append-only events; add a tombstone/supersede event instead of deleting history.",
        "claim_status": claim_status,
    }
    if extra:
        result.update(extra)
    return result


def readiness_action(
    action_id: str,
    *,
    priority: str,
    blocking: bool,
    command: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "action_id": action_id,
        "priority": priority,
        "blocking": blocking,
        "command": command,
        "reason": reason,
    }


def review_queue_readiness(recipes_dir: Path) -> dict[str, int]:
    total = 0
    pending = 0
    malformed = 0
    for path in sorted((recipes_dir / "review_queue").glob("*.json")):
        total += 1
        try:
            item = read_json(path, {})
        except (OSError, json.JSONDecodeError):
            malformed += 1
            continue
        if not isinstance(item, dict) or not item.get("review_id"):
            malformed += 1
            continue
        if item.get("status") == "pending":
            pending += 1
    return {
        "total_count": total,
        "pending_count": pending,
        "malformed_count": malformed,
    }


def adapter_candidate_readiness(recipes_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    verified_names: set[str] = set()
    external = external_adapter_runtime_evidence(recipes_dir, events)
    verified_names.update(str(name) for name in external.get("runtime_verified_adapters", []))
    memory = memory_adapter_evidence(recipes_dir, events)
    for name, state in memory.items():
        if isinstance(state, dict) and any(
            bool(state.get(key))
            for key in ("runtime_verified", "native_runtime_verified", "semantic_runtime_verified")
        ):
            verified_names.add(str(name))
    cloud = cloud_adapter_evidence(recipes_dir, events)
    for name, state in cloud.items():
        if isinstance(state, dict) and bool(state.get("runtime_verified") or state.get("runtime_events")):
            verified_names.add(str(name))
    embedding = embedding_adapter_evidence(recipes_dir, events)
    for name, state in embedding.items():
        if isinstance(state, dict) and bool(state.get("runtime_verified") or state.get("runtime_events")):
            verified_names.add(str(name))
    return {
        "runtime_verified_count": len(verified_names),
        "runtime_verified_names": sorted(verified_names),
    }


def recall_boundary_state(recipes_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    contract = {
        "activation_policy": "explicit_command_only",
        "input_authority": "candidate_evidence_only",
        "output_authority": "candidate_evidence_only",
        "core_requires_recall_adapter": False,
        "core_auto_invokes_recall": False,
        "can_write_formal_recipe": False,
        "can_accept_review": False,
        "can_create_execution_lock": False,
        "can_change_recipe_lifecycle": False,
        "can_change_outcome_confidence": False,
        "review_queue_required_before_formal_use": True,
        "failure_policy": "disable_affected_recall_path_core_continues",
    }
    specs = {
        "cognee": {
            "row_files": [("index", recipes_dir / "memory" / "cognee" / "index.jsonl")],
            "json_files": [("status", recipes_dir / "memory" / "cognee" / "status.json")],
            "event_types": {
                "memory_indexed",
                "memory_searched",
                "memory_native_probe_checked",
                "memory_semantic_probe_checked",
                "memory_semantic_runtime_configured",
            },
        },
        "graphiti": {
            "row_files": [
                ("nodes", recipes_dir / "memory" / "graphiti" / "nodes.jsonl"),
                ("edges", recipes_dir / "memory" / "graphiti" / "edges.jsonl"),
            ],
            "json_files": [("status", recipes_dir / "memory" / "graphiti" / "status.json")],
            "event_types": {"memory_graph_indexed", "memory_graph_searched", "memory_native_probe_checked"},
        },
        "qwen3": {
            "row_files": [("index", recipes_dir / "embeddings" / "qwen3" / "index.jsonl")],
            "json_files": [
                ("config", recipes_dir / "embeddings" / "qwen3" / "config.json"),
                ("status", recipes_dir / "embeddings" / "qwen3" / "status.json"),
            ],
            "event_types": {"embedding_adapter_configured", "embedding_indexed", "embedding_searched"},
        },
    }
    adapters: dict[str, Any] = {}
    all_violations: list[str] = []
    for adapter_name, spec in specs.items():
        violations: list[str] = []
        rows: list[dict[str, Any]] = []
        present_files = 0
        for kind, path in spec["row_files"]:
            loaded, load_error = read_optional_jsonl(path)
            if path.exists():
                present_files += 1
            if load_error:
                violations.append(f"{adapter_name} {kind} 无法解析：{load_error}")
            rows.extend(loaded)
        json_docs: list[tuple[str, dict[str, Any]]] = []
        for kind, path in spec["json_files"]:
            loaded, load_error = read_optional_json(path, {})
            if path.exists():
                present_files += 1
            if load_error:
                violations.append(f"{adapter_name} {kind} 无法解析：{load_error}")
            if isinstance(loaded, dict) and loaded:
                json_docs.append((kind, loaded))
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                violations.append(f"{adapter_name} row {index} 不是 JSON object。")
                continue
            if row.get("evidence_status") != "candidate" or row.get("evidence_strength") != "candidate":
                violations.append(f"{adapter_name} row {index} 未保持 candidate 状态。")
            if not isinstance(row.get("source_trace"), list) or not row.get("source_trace"):
                violations.append(f"{adapter_name} row {index} 缺 source_trace。")
            cannot_claim = row.get("cannot_claim")
            if not isinstance(cannot_claim, list) or not cannot_claim:
                violations.append(f"{adapter_name} row {index} 缺 cannot_claim。")
        for kind, doc in json_docs:
            if doc.get("candidate_only") is False:
                violations.append(f"{adapter_name} {kind} 把 candidate_only 标成 false。")
        matching_events = [
            event
            for event in events
            if event.get("event_type") in spec["event_types"]
            and (
                event.get("payload", {}).get("adapter") == adapter_name
                or event.get("payload", {}).get("provider") == adapter_name
            )
        ]
        has_index_event = any(
            event.get("event_type") in {"memory_indexed", "memory_graph_indexed", "embedding_indexed"}
            for event in matching_events
        )
        if has_index_event and not rows:
            violations.append(f"{adapter_name} 有 index event，但候选索引缺失或为空。")
        if violations:
            adapter_status = "degraded"
        elif rows:
            adapter_status = "active"
        elif present_files or matching_events:
            adapter_status = "configured_or_probed"
        else:
            adapter_status = "disabled"
        adapter_result = {
            "status": adapter_status,
            "candidate_only": True,
            "stored_candidate_count": len(rows),
            "runtime_event_count": len(matching_events),
            "contract_violation_count": len(violations),
            "contract_violations": violations,
            "affects_core_readiness": False,
        }
        adapters[adapter_name] = adapter_result
        all_violations.extend(violations)
    active_count = sum(1 for item in adapters.values() if item["status"] == "active")
    disabled_count = sum(1 for item in adapters.values() if item["status"] == "disabled")
    return {
        "status": "degraded" if all_violations else "ready",
        "realized_mode": "core_only" if disabled_count == len(adapters) else "core_plus_optional_recall_candidates",
        "all_adapters_disabled": disabled_count == len(adapters),
        "adapter_count": len(adapters),
        "active_adapter_count": active_count,
        "disabled_adapter_count": disabled_count,
        "contract": contract,
        "excluded_adapters": {
            "zep": {
                "status": "out_of_scope",
                "runtime_integration": False,
                "reason": "user explicitly rejected the Zep runtime loop",
            }
        },
        "adapters": adapters,
        "violation_count": len(all_violations),
        "violations": all_violations,
    }


EVIDENCE_ACTIVE_ROOTS = (
    "candidates",
    "review_queue",
    "source_refinery",
    "memory",
    "embeddings",
    "evidence",
    "video_index",
    "source_index",
)
PERSISTENCE_TEXT_SUFFIXES = {".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".vtt", ".srt"}


def safe_project_relative_path(root: Path, relative_path: str) -> Path:
    path = (root / relative_path).resolve()
    try:
        relative = path.relative_to(root.resolve())
    except ValueError as exc:
        raise RecipesError("AR457", "quarantine 原路径越过项目边界。", relative_path, "人工检查 manifest。") from exc
    if len(relative.parts) < 3 or relative.parts[0] != ".recipes" or relative.parts[1] not in EVIDENCE_ACTIVE_ROOTS:
        raise RecipesError("AR457", "quarantine 原路径不属于候选/派生证据区。", relative_path, "人工检查 manifest。")
    return path


def validate_candidate_evidence_file(path: Path) -> list[str]:
    if not path.exists() or not path.is_file():
        return [f"文件不存在：{path}"]
    try:
        if path.suffix == ".json":
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, (dict, list)):
                return ["JSON 顶层必须是 object 或 array。"]
        elif path.suffix == ".jsonl":
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    return [f"JSONL 第 {line_number} 行不是 object。"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return [f"无法解析：{type(exc).__name__}: {exc}"]
    return []


def persistence_secret_findings(recipes_dir: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not recipes_dir.exists():
        return findings
    for path in sorted(recipes_dir.rglob("*")):
        if not path.is_file() or path.suffix not in PERSISTENCE_TEXT_SUFFIXES:
            continue
        if path.name.startswith(".") and path.name.endswith(".lock"):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        _, report = redact_sensitive_text(text)
        if not report["count"]:
            continue
        relative = path.relative_to(recipes_dir)
        authoritative = relative.parts[0] in {"recipes", "locks", "lifecycle"} or relative.name == "events.jsonl"
        findings.append(
            {
                "relative_path": str(path.relative_to(recipes_dir.parent)),
                "reason_kind": "secret_exposure",
                "reason": f"检测到 {report['count']} 个凭证模式；rules={report['rules']}",
                "rule_names": report["rules"],
                "finding_count": report["count"],
                "authoritative": authoritative,
                "file_hash": file_sha256(path),
                "byte_size": path.stat().st_size,
            }
        )
    return findings


def evidence_integrity_issues(root: Path, recipes_dir: Path) -> list[dict[str, Any]]:
    issues_by_path: dict[str, dict[str, Any]] = {}
    for dirname in EVIDENCE_ACTIVE_ROOTS:
        base = recipes_dir / dirname
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
                continue
            errors = validate_candidate_evidence_file(path)
            if not errors:
                continue
            relative_path = str(path.relative_to(root))
            issues_by_path[relative_path] = {
                "relative_path": relative_path,
                "reason_kind": "malformed_candidate_evidence",
                "reason": "; ".join(errors),
                "file_hash": file_sha256(path),
                "byte_size": path.stat().st_size,
                "formal_use_allowed": False,
            }
    for finding in persistence_secret_findings(recipes_dir):
        if finding["authoritative"]:
            continue
        relative_path = finding["relative_path"]
        existing = issues_by_path.get(relative_path)
        if existing:
            existing["reason_kind"] = "malformed_or_secret_candidate_evidence"
            existing["reason"] = f"{existing['reason']}; {finding['reason']}"
        else:
            issues_by_path[relative_path] = {
                "relative_path": relative_path,
                "reason_kind": "secret_candidate_evidence",
                "reason": finding["reason"],
                "file_hash": finding["file_hash"],
                "byte_size": finding["byte_size"],
                "formal_use_allowed": False,
            }
    return sorted(issues_by_path.values(), key=lambda item: item["relative_path"])


def evidence_quarantine_state(root: Path, recipes_dir: Path) -> dict[str, Any]:
    manifests: list[dict[str, Any]] = []
    quarantine_root = recipes_dir / "quarantine"
    for path in sorted(quarantine_root.glob("*/manifest.json")) if quarantine_root.exists() else []:
        value, error = read_optional_json(path, {})
        if error:
            manifests.append({"status": "manifest_malformed", "manifest_path": str(path.relative_to(root))})
        elif value:
            manifests.append(value)
    secret_findings = persistence_secret_findings(recipes_dir)
    authoritative = [item for item in secret_findings if item["authoritative"]]
    issues = evidence_integrity_issues(root, recipes_dir)
    return {
        "issue_count": len(issues),
        "issues": issues,
        "active_quarantine_count": sum(1 for item in manifests if item.get("status") == "quarantined"),
        "released_quarantine_count": sum(1 for item in manifests if item.get("status") == "released"),
        "quarantine_items": manifests,
        "secret_finding_count": len(secret_findings),
        "authoritative_secret_finding_count": len(authoritative),
        "authoritative_secret_findings": authoritative,
    }


def load_candidate_file_for_pack(path: Path) -> tuple[Any, list[str]]:
    errors = validate_candidate_evidence_file(path)
    if errors:
        return None, errors
    if path.suffix == ".json":
        return read_json(path, {}), []
    if path.suffix == ".jsonl":
        return read_jsonl(path), []
    return None, ["unsupported candidate file"]


def build_execution_evidence_pack(
    root: Path,
    recipes_dir: Path,
    lock: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    max_bytes: int,
    privacy: str,
) -> dict[str, Any]:
    root = root.resolve()
    lock_id = str(lock.get("lock_id") or "")
    recipe_ids = [str(item) for item in lock.get("recipe_ids", [])]
    mandatory_records: list[dict[str, Any]] = [
        {"record_type": "execution_lock", "source": f".recipes/locks/{lock_id}.json", "content": lock}
    ]
    evidence_event_ids: set[str] = set()
    for recipe_id in recipe_ids:
        recipe = read_json(recipe_path_for(recipes_dir, recipe_id), {})
        mandatory_records.append(
            {
                "record_type": "formal_recipe",
                "source": str(recipe_path_for(recipes_dir, recipe_id).relative_to(root)),
                "content": recipe,
            }
        )
        evidence_event_ids.update(str(item) for item in recipe.get("evidence_refs", []) if item)
        evidence_event_ids.update(str(item) for item in recipe.get("related_events", []) if item)
    for event in events:
        if event.get("event_type") == "execution_evidence_pack_created":
            continue
        if event.get("lock_id") == lock_id or event.get("event_id") in evidence_event_ids:
            mandatory_records.append(
                {"record_type": "event", "source": f".recipes/events.jsonl#{event.get('event_id')}", "content": event}
            )

    candidate_paths: list[Path] = []
    for dirname in ("review_queue", "source_refinery", "memory", "embeddings", "evidence"):
        base = recipes_dir / dirname
        if not base.exists():
            continue
        candidate_paths.extend(
            path
            for path in sorted(base.rglob("*"))
            if path.is_file() and path.suffix in {".json", ".jsonl"}
        )

    candidate_records: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    for path in candidate_paths:
        content, errors = load_candidate_file_for_pack(path)
        source = str(path.relative_to(root))
        if errors:
            omissions.append({"source": source, "reason": "malformed", "detail": errors[0]})
            continue
        serialized = stable_json(content)
        if recipe_ids and not any(recipe_id in serialized for recipe_id in recipe_ids):
            continue
        privacy_class = content.get("privacy_class") if isinstance(content, dict) else None
        if privacy == "minimal":
            omissions.append({"source": source, "reason": "privacy_policy", "detail": "minimal excludes candidate files"})
            continue
        if privacy_class == "private_only":
            omissions.append({"source": source, "reason": "privacy_policy", "detail": "private_only excluded from project_local pack"})
            continue
        candidate_records.append({"record_type": "candidate_evidence", "source": source, "content": content})

    quarantine_state = evidence_quarantine_state(root, recipes_dir)
    for item in quarantine_state["quarantine_items"]:
        if item.get("status") == "quarantined":
            omissions.append(
                {
                    "source": item.get("original_relative_path"),
                    "reason": "quarantined",
                    "detail": item.get("reason_kind"),
                }
            )

    included_records: list[dict[str, Any]] = []
    included: list[dict[str, Any]] = []
    used_bytes = 0
    redaction_count = 0

    def prepared(record: dict[str, Any]) -> tuple[dict[str, Any], int, int]:
        safe_record, report = redact_sensitive_value(record)
        safe_record = annotate_persistence_redaction(safe_record, report)
        size = len((stable_json(safe_record) + "\n").encode("utf-8"))
        return safe_record, size, int(report["count"])

    for record in mandatory_records:
        safe_record, size, count = prepared(record)
        if used_bytes + size > max_bytes:
            raise RecipesError(
                "AR462",
                "evidence pack budget 连 lock/recipe 核心证据都装不下。",
                f"required_bytes>{max_bytes}; source={record['source']}",
                "提高 --max-bytes；不能省略 lock 或 formal recipe。",
            )
        included_records.append(safe_record)
        included.append({"source": record["source"], "record_type": record["record_type"], "bytes": size})
        used_bytes += size
        redaction_count += count
    for record in candidate_records:
        safe_record, size, count = prepared(record)
        if used_bytes + size > max_bytes:
            omissions.append({"source": record["source"], "reason": "budget_exceeded", "detail": f"candidate_bytes={size}"})
            continue
        included_records.append(safe_record)
        included.append({"source": record["source"], "record_type": record["record_type"], "bytes": size})
        used_bytes += size
        redaction_count += count

    pack_id = make_id(
        "evidence_pack",
        lock_id,
        privacy,
        max_bytes,
        [(item["source"], item["bytes"]) for item in included],
        [(item.get("source"), item.get("reason")) for item in omissions],
    )
    return {
        "pack_id": pack_id,
        "status": "candidate_execution_context",
        "lock_id": lock_id,
        "privacy": privacy,
        "max_bytes": max_bytes,
        "used_bytes": used_bytes,
        "included_count": len(included),
        "omitted_count": len(omissions),
        "included": included,
        "omissions": omissions,
        "redaction_count": redaction_count,
        "records": included_records,
        "formal_recipe_written": False,
        "claim_limits": [
            "Pack contents are context evidence, not proof of task completion or quality.",
            "Omitted evidence is unavailable to this pack and cannot support its decisions.",
        ],
    }


def doctor_report(
    errors: list[dict[str, str]],
    warnings: list[dict[str, str]],
    *,
    root: Path,
    events: list[dict[str, Any]] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    status = "error" if errors else ("warn" if warnings else "ok")
    events = events or []
    sources = sources or []
    recipes_dir = root / ".recipes"
    schema = project_schema_status(recipes_dir)
    phase0c = phase0c_evidence(recipes_dir, events)
    refinery = source_refinery_evidence(recipes_dir)
    memory_adapters = memory_adapter_evidence(recipes_dir, events)
    cloud_adapters = cloud_adapter_evidence(recipes_dir, events)
    embedding_adapters = embedding_adapter_evidence(recipes_dir, events)
    external_adapters = external_adapter_runtime_evidence(recipes_dir, events)
    adapter_runtime_lock = adapter_runtime_lock_evidence(recipes_dir)
    system_runtime_lock = system_runtime_lock_evidence(recipes_dir)
    lifecycle = recipe_lifecycle_state(events)
    recall_boundary = recall_boundary_state(recipes_dir, events)
    evidence_hardening = evidence_quarantine_state(root, recipes_dir)
    formal_recipes: list[dict[str, Any]] = []
    for path in sorted((recipes_dir / "recipes").glob("*.json")):
        try:
            formal_recipes.append(read_json(path, {}))
        except (OSError, json.JSONDecodeError):
            continue
    outcomes = outcome_quality_state(recipes_dir, events, recipes=formal_recipes)
    verified = [
        "已读取 .recipes/events.jsonl 并检查 seq/prev_event_hash/event_hash。",
        "已检查 active lock 引用的 recipe hash。",
        "已检查 source 文件是否仍存在。",
        f"已读取项目 schema 状态：{schema['state']}。",
    ]
    if phase0c["scan"]:
        verified.append("已检测到 Phase 0C scan 证据：sources_scanned event + source_index/chunks.jsonl。")
    if phase0c["recover"]:
        verified.append("已检测到 Phase 0C recover 证据：recover_candidate_created event。")
    if phase0c["video"]:
        verified.append("已检测到 Phase 0C ingest-video 证据：transcript_ingested event + video_index chunks。")
    if refinery["has_activity"]:
        verified.append("已检查 source_refinery refined chunks、cards 和 patch drafts。")
    if memory_adapters["cognee"]["has_activity"]:
        verified.append("已检查 Cognee memory candidate index 和 runtime probe 证据。")
    if memory_adapters["cognee"].get("native_status") != "not_checked":
        verified.append("已检查 Cognee native probe 证据，并保持 candidate-only 边界。")
    if memory_adapters["cognee"].get("semantic_status") != "not_checked":
        verified.append("已检查 Cognee semantic probe 证据，并保持 candidate-only 边界。")
    if memory_adapters["cognee"].get("semantic_config_status") != "not_checked":
        verified.append("已检查 Cognee semantic runtime 配置/检测报告，并保持 candidate-only 边界。")
    if memory_adapters["graphiti"]["has_activity"]:
        verified.append("已检查 Graphiti local graph candidate index，并保持 candidate-only 边界。")
    if memory_adapters["graphiti"].get("native_status") not in {"not_checked", "not_used"}:
        verified.append("已检查 Graphiti native probe 证据，并保持 candidate-only 边界。")
    if cloud_adapters["deepseek"].get("config_status") == "configured":
        verified.append("已检查 DeepSeek cloud adapter 配置，未暴露 API key。")
    if cloud_adapters["deepseek"].get("runtime_events"):
        verified.append("已检查 DeepSeek cloud adapter runtime receipts，且输出仍按 candidate 处理。")
    if embedding_adapters["qwen3"].get("config_status") == "configured":
        verified.append("已检查 Qwen3 embedding adapter 配置，并保持 loopback-only 边界。")
    if embedding_adapters["qwen3"].get("runtime_events"):
        verified.append("已检查 Qwen3 embedding index/search receipts，且输出仍按 candidate 处理。")
    if external_adapters["runtime_verified_adapters"]:
        verified.append("已检查 external adapter runtime receipts，且这些输出仍按 candidate 处理。")
    if adapter_runtime_lock["present"]:
        verified.append("已检查 adapter runtime lock 报告。")
    if system_runtime_lock["present"]:
        verified.append("已检查 system runtime lock 报告。")
    if lifecycle["tombstones"]:
        verified.append("已从 append-only events 重建 recipe tombstone/revocation 状态并检查防复活边界。")
    if any(item["all_attributable"]["decisive"] or item["all_attributable"]["unknown"] for item in outcomes["recipes"]):
        verified.append("已按 recipe id/version/hash 检查 success/failure/unknown 的归因和执法边界。")
    verified.append("已检查 Cognee、Graphiti、Qwen recall 均不能写 formal recipe、创建 lock 或改变 outcome。")
    verified.append("已检查候选证据格式、quarantine 状态和落盘凭证模式。")
    cannot_claim = [
        "仅凭 doctor 不能说 MCP 已注册到真实 Codex/Claude/Hermes 客户端；真实客户端加载必须另有 fresh client/tool-call 证据。",
        "不能说插件真实安装完成。",
        "不能说菜谱已覆盖全部项目历史。",
    ]
    if not phase0c["complete"]:
        cannot_claim.insert(0, "不能说 Phase 0C 已完成。")
        if not phase0c["recover"]:
            cannot_claim.append("不能说 recover 已真实生成候选补丁。")
        if not phase0c["scan"]:
            cannot_claim.append("不能说 scan 已真实生成 source_index。")
        if not phase0c["video"]:
            cannot_claim.append("不能说 ingest-video 已真实生成 video_index。")
    if refinery["has_activity"]:
        cannot_claim.append("不能说 source_refinery 候选卡片或 patch draft 已全部验证或吸收。")
    if memory_adapters["cognee"]["has_activity"]:
        cannot_claim.append("不能说 Cognee memory candidate 已经自动进入正式 recipe。")
    if memory_adapters["cognee"].get("native_status") != "not_checked":
        cannot_claim.append("不能说 Cognee native probe 已经完成长期语义记忆或图谱闭环。")
    if memory_adapters["cognee"].get("semantic_status") != "not_checked":
        cannot_claim.append("不能说 Cognee semantic probe 已经证明生产级长期记忆。")
    if memory_adapters["cognee"].get("semantic_config_status") != "not_checked":
        cannot_claim.append("不能说 Cognee semantic runtime 配置已经证明本地模型服务可用。")
    if memory_adapters["graphiti"]["has_activity"]:
        cannot_claim.append("不能说 Graphiti graph candidate 已经自动进入正式 recipe。")
    if memory_adapters["graphiti"].get("native_status") not in {"not_checked", "not_used"}:
        cannot_claim.append("不能说 Graphiti native probe 已经完成生产级长期记忆或图谱质量验证。")
    if cloud_adapters["deepseek"].get("config_status") == "configured" or cloud_adapters["deepseek"].get("runtime_events"):
        cannot_claim.append("不能说 DeepSeek cloud adapter 输出已经自动进入正式 recipe。")
        cannot_claim.append("不能说 DeepSeek 文本模型支持原始图片或视频画面输入。")
    if embedding_adapters["qwen3"].get("config_status") == "configured" or embedding_adapters["qwen3"].get("runtime_events"):
        cannot_claim.append("不能说 embedding search 结果已经自动进入正式 recipe。")
        cannot_claim.append("不能说 Qwen3 embedding 可以替代 DeepSeek 抽卡或 review_queue。")
    if external_adapters["runtime_verified_adapters"]:
        cannot_claim.append("不能说外部 adapter 输出已经自动变成正式 recipe。")
    if not adapter_runtime_lock["present"]:
        cannot_claim.append("不能说 adapter Python 依赖已经被 lockfile 固化。")
    if not system_runtime_lock["present"]:
        cannot_claim.append("不能说系统二进制依赖已经被 lock 报告固化。")
    cannot_claim.append("不能把历史 lock 推导结果当成自动暂停 recipe 的依据；执法只看新显式绑定结果。")
    cannot_claim.append("不能把 recall adapter active 当成召回质量已通过；它们仍然只产候选证据。")
    cannot_claim.append("quarantine 只表示坏证据已移出 active path，不表示它已修复或审核通过。")
    return {
        "ok": not errors,
        "status": status,
        "report_id": make_id("doctor", str(root), len(events), errors, warnings),
        "checked_at": now_iso(),
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "events": len(events),
            "sources": len(sources),
            "phase0c": phase0c,
            "source_refinery": refinery,
            "memory_adapters": memory_adapters,
            "cloud_adapters": cloud_adapters,
            "embedding_adapters": embedding_adapters,
            "external_adapters": external_adapters,
            "adapter_runtime_lock": adapter_runtime_lock,
            "system_runtime_lock": system_runtime_lock,
            "recipe_lifecycle": recipe_lifecycle_summary(lifecycle),
            "outcomes": outcome_quality_summary(outcomes["recipes"], state=outcomes),
            "recall_boundary": {
                "status": recall_boundary["status"],
                "realized_mode": recall_boundary["realized_mode"],
                "all_adapters_disabled": recall_boundary["all_adapters_disabled"],
                "active_adapter_count": recall_boundary["active_adapter_count"],
                "disabled_adapter_count": recall_boundary["disabled_adapter_count"],
                "violation_count": recall_boundary["violation_count"],
                "core_requires_recall_adapter": False,
            },
            "evidence_hardening": {
                "issue_count": evidence_hardening["issue_count"],
                "active_quarantine_count": evidence_hardening["active_quarantine_count"],
                "released_quarantine_count": evidence_hardening["released_quarantine_count"],
                "secret_finding_count": evidence_hardening["secret_finding_count"],
                "authoritative_secret_finding_count": evidence_hardening["authoritative_secret_finding_count"],
                "pre_persistence_redaction": True,
            },
            "project_schema": schema,
        },
        "claim_status": claim_status(
            verified=verified,
            missing_evidence=[] if sources else ["尚未登记授权 source。"],
            cannot_claim=cannot_claim,
        ),
        "next_actions": [] if not errors else ["先修复 errors，再运行 doctor。"],
    }


def first_line(text: str, *, max_chars: int = 96) -> str:
    stripped = " ".join(text.strip().split())
    if len(stripped) <= max_chars:
        return stripped or "Untitled recipe"
    cut = stripped[:max_chars].rsplit(" ", 1)[0].strip()
    return cut or stripped[:max_chars].strip() or "Untitled recipe"


def recipe_draft(recipe_id: str, title: str, correction_text: str, event_ids: list[str]) -> dict[str, Any]:
    draft = {
        "recipe_id": recipe_id,
        "version": 0,
        "title": title,
        "scope": "Phase 0A generated recipe from a correction capture.",
        "use_when": [title],
        "do_not_use_when": ["没有读 source truth 或没有 active lock。"],
        "inputs_required": ["task", "source_truth"],
        "outputs_expected": ["按菜谱执行后的 capture。"],
        "source_truth_to_read": event_ids,
        "verified_path": [correction_text],
        "forbidden_path": ["不要绕过 lookup/lock 直接 claim 完成。"],
        "steps": [correction_text],
        "failure_signals": ["重复出现同类纠偏。"],
        "stop_line": "执行前必须 lookup 并 lock 这条 recipe。",
        "verification": ["执行后运行 capture，并用 doctor 检查 claim_status。"],
        "success_means": ["同类任务不再重复这条纠偏。"],
        "failure_means": ["再次触发同类纠偏或 failure capture。"],
        "cannot_claim": ["不能说 recipe 已覆盖全部历史资料。"],
        "rollback": ["生成 supersede/tombstone event，不删除旧版本。"],
        "evidence_refs": event_ids,
        "related_events": event_ids,
        "open_questions": [],
    }
    apply_field_overrides(draft, correction_text)
    return draft


FIELD_ALIASES = {
    "failure_signal": "failure_signals",
    "failure_signals": "failure_signals",
    "current_gap": "failure_signals",
    "recurring_failure": "failure_signals",
    "steps": "checklist_item",
    "checklist_item": "checklist_item",
    "checklist_items": "checklist_item",
    "check": "checklist_item",
    "expected_output": "checklist_item",
    "acceptance_check": "checklist_item",
    "stop_after": "checklist_item",
    "verified_path": "verified_path",
    "forbidden_path": "forbidden_path",
    "wrong_behavior": "forbidden_path",
    "fallback_allowed": "fallback_allowed",
    "fallback_forbidden": "fallback_forbidden",
    "prompt_rule": "prompt_rule",
    "prompt_rules": "prompt_rule",
    "good_example": "good_example",
    "correct_behavior": "checklist_item",
    "bad_example": "bad_example",
    "visual_check": "visual_check",
    "cannot_claim": "cannot_claim",
    "pressure_test": "pressure_test",
    "source_truth_to_read": "source_truth_to_read",
    "must_read_sources": "source_truth_to_read",
    "first_patch_target": "source_truth_to_read",
    "evidence_refs": "evidence_refs",
    "applies_to": "evidence_refs",
}


MARKDOWN_TABLE_FIELD_ALIASES = {
    "acceptance_check": "checklist_item",
    "best_future_use": "checklist_item",
    "blocked_use": "forbidden_path",
    "cannot_claim": "cannot_claim",
    "concrete_candidate_action": "checklist_item",
    "current_candidate_value": "checklist_item",
    "decision": "checklist_item",
    "evidence_gap": "failure_signals",
    "hard_blockers": "failure_signals",
    "next_validation_needed": "checklist_item",
    "observed_module_class": "visual_check",
    "observed_phenomenon": "checklist_item",
    "possible_sample_project_use": "checklist_item",
    "possible_project_use": "checklist_item",
    "source_evidence": "verified_path",
    "status": "cannot_claim",
    "strongest_evidence": "verified_path",
    "use": "checklist_item",
    "visual_readback": "visual_check",
    "when_to_reopen": "checklist_item",
}


FIELD_HEADER_PATTERN = re.compile(r"([A-Za-z][A-Za-z0-9 _-]{0,80}):\s*(.*)")


def normalize_field_key(value: str) -> str:
    return re.sub(r"[\s_-]+", "_", value.strip().casefold()).strip("_")


def parse_field_header(line: str) -> tuple[str, str] | None:
    header = re.fullmatch(FIELD_HEADER_PATTERN, line)
    if not header:
        return None
    return normalize_field_key(header.group(1)), header.group(2).strip()


def apply_field_overrides(draft: dict[str, Any], text: str) -> None:
    parsed = parse_field_blocks(text)
    for raw_field, values in parsed.items():
        if raw_field == "stop_line":
            if values:
                draft["stop_line"] = values[0]
            continue
        field = FIELD_ALIASES.get(raw_field)
        if not field:
            continue
        existing = draft.get(field, [])
        if not isinstance(existing, list):
            existing = [str(existing)]
        draft[field] = list(dict.fromkeys(existing + values))


def parse_field_blocks(text: str) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    current_field: str | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = parse_field_header(line)
        if header:
            current_field, value = header
            parsed.setdefault(current_field, [])
            if value:
                parsed[current_field].extend(split_inline_field_values(value))
            continue
        if current_field and line.startswith(("- ", "* ")):
            parsed.setdefault(current_field, []).extend(split_inline_field_values(line[2:].strip()))
            continue
        current_field = None
    return {field: [value for value in values if value] for field, values in parsed.items()}


def split_inline_field_values(value: str) -> list[str]:
    cleaned = value.strip()
    if not cleaned:
        return []
    parts = [part.strip() for part in re.split(r"\s+-\s+", cleaned) if part.strip()]
    return parts or [cleaned]


def normalize_problem(text: str) -> str:
    return " ".join(text.casefold().split())


def problem_fingerprint(text: str) -> str:
    return make_id("problem", normalize_problem(text))


STRUCTURED_SECTION_FIELD_KEYS = set(FIELD_ALIASES) | {
    "after",
    "before",
    "card_type",
    "correction",
    "inputs",
    "outputs",
    "replacement_path",
    "steps",
    "status",
    "verification",
}


def field_keys_in_lines(lines: list[str]) -> set[str]:
    keys: set[str] = set()
    for line in lines:
        header = parse_field_header(line.strip())
        if header and header[0] in STRUCTURED_SECTION_FIELD_KEYS:
            keys.add(header[0])
    return keys


def markdown_sections(text: str) -> list[tuple[int, int, list[str]]]:
    lines = text.splitlines()
    if not lines:
        return [(1, 1, [])]
    sections: list[tuple[int, int, list[str]]] = []
    current: list[str] = []
    start_line = 1
    for line_no, line in enumerate(lines, start=1):
        starts_new_section = bool(re.match(r"^#{1,3}\s+\S", line)) and bool(current)
        if starts_new_section:
            sections.append((start_line, line_no - 1, current))
            current = [line]
            start_line = line_no
            continue
        if not current:
            start_line = line_no
        current.append(line)
    sections.append((start_line, len(lines), current))
    return sections


def source_text_for_index(path: Path, text: str) -> str:
    if path.suffix.casefold() != ".json":
        return text
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return text
    lines = structured_json_candidate_lines(data)
    if not lines:
        return text
    return "# Structured JSON Candidate Lines\n\n" + "\n".join(lines) + "\n\n# Raw JSON\n\n" + text


def structured_json_candidate_lines(data: Any, *, max_items: int = 260) -> list[str]:
    items: list[tuple[str, str]] = []
    collect_json_leaf_items(data, "", items, max_items=max_items)
    lines: list[str] = []
    for path, value in items:
        field = field_for_json_leaf(path, value)
        if not field:
            continue
        lines.append(f"{field}: {path}: {value}")
    return unique_text(lines)


def collect_json_leaf_items(data: Any, prefix: str, items: list[tuple[str, str]], *, max_items: int) -> None:
    if len(items) >= max_items:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            collect_json_leaf_items(value, child_prefix, items, max_items=max_items)
            if len(items) >= max_items:
                return
        return
    if isinstance(data, list):
        if all(not isinstance(item, (dict, list)) for item in data):
            joined = ", ".join(clean_json_leaf_value(item) for item in data if clean_json_leaf_value(item))
            if joined and prefix:
                items.append((prefix, joined))
            return
        for index, value in enumerate(data):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            collect_json_leaf_items(value, child_prefix, items, max_items=max_items)
            if len(items) >= max_items:
                return
        return
    value = clean_json_leaf_value(data)
    if value and prefix:
        items.append((prefix, value))


def clean_json_leaf_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    cleaned = " ".join(str(value).split())
    if len(cleaned) > 240:
        return cleaned[:237].rstrip() + "..."
    return cleaned


def field_for_json_leaf(path: str, value: str) -> str | None:
    path_key = normalize_json_path_key(path)
    if path_key in JSON_IGNORED_PATH_KEYS:
        return None
    direct_field = JSON_DIRECT_PATH_FIELDS.get(path_key)
    if direct_field:
        return direct_field
    haystack = normalize_problem(f"{path} {value}")
    if any(token in haystack for token in JSON_IGNORED_TERMS):
        return None
    if any(token in haystack for token in JSON_FORBIDDEN_TERMS):
        return "forbidden_path"
    if any(token in haystack for token in JSON_FAILURE_TERMS):
        return "failure_signals"
    if any(token in haystack for token in JSON_VISUAL_TERMS):
        return "visual_check"
    if any(token in haystack for token in JSON_VERIFIED_TERMS):
        return "verified_path"
    if any(token in haystack for token in JSON_CHECKLIST_TERMS):
        return "checklist_item"
    return None


def normalize_json_path_key(path: str) -> str:
    cleaned = re.sub(r"\[\d+\]", "", path)
    return cleaned.rsplit(".", 1)[-1].casefold()


JSON_IGNORED_PATH_KEYS = {
    "id",
    "project_id",
    "script_id",
    "script_title",
    "shot_id",
    "shot_index",
    "index",
    "track_id",
    "presenter_treatment",
    "query_or_prompt",
    "primary_material_type",
    "candidate_material_types",
    "provider_candidates",
    "cost_boundary",
    "safe_fallback",
    "start_seconds",
    "end_seconds",
    "duration_seconds",
    "total_duration_seconds",
    "generated_at",
    "created_at",
    "executed_at",
    "source_plan_created_at",
    "width",
    "height",
    "fps",
    "asset_width",
    "asset_height",
    "asset_duration_seconds",
    "schema_version",
    "source_qa_status",
    "requested_actions",
    "total_actions",
    "manual_actions",
    "execution_id",
    "plan_id",
    "issue_key",
    "expected_gate_to_rerun",
    "expected_recipe_id",
    "input_source",
    "lookup_query",
    "recipe_pool_source",
    "test_id",
}


JSON_DIRECT_PATH_FIELDS = {
    "cannot_claim": "cannot_claim",
    "required_evidence": "checklist_item",
    "required_layer_stack": "visual_check",
}


JSON_IGNORED_TERMS = [
    "sound_strategy",
    "bgm",
    "narration_audio",
    "voice_audio",
    "voice_or_avatar_audio",
    "presenter_source",
    "presenter_original",
    "local_avatar",
    "avataradapter",
    "真人音频驱动",
    "bgm_audio",
    "render_outputs.preview",
    "summary.failed",
    "template_render_engine",
    "template_screenshot_engine",
]


JSON_VISUAL_TERMS = [
    "visual",
    "visual_strategy",
    "caption_strategy",
    "big_subtitle",
    "card_fullscreen",
    "keyword_card",
    "template_card",
    "template_label",
    "template_id",
    "role_label",
    "source_kind",
    "source_path",
    "thumbnail",
    "foreground",
    "background",
    "font",
    "color",
    "opacity",
    "keyframe",
    "presenter",
    "pip",
    "强字卡",
    "重点字卡",
    "标题卡",
    "大字",
    "花字",
    "首帧",
    "主画面",
    "模板",
]


JSON_FAILURE_TERMS = [
    "issue",
    "issues",
    "warning",
    "error",
    "failed",
    "failure",
    "blocking",
    "severity",
    "message",
    "weak",
    "slow",
    "偏弱",
    "不够快",
    "缺少",
    "失败",
    "拦截",
]


JSON_CHECKLIST_TERMS = [
    "recommendation",
    "repair",
    "primary_action",
    "action_label",
    "action",
    "intent",
    "strategy",
    "request",
    "fallback",
    "review",
    "check",
    "recommend",
    "建议",
    "强化",
]


JSON_VERIFIED_TERMS = [
    "artifact",
    "artifacts",
    "rendered_clip",
    "source_asset",
    "template_html",
    "final.mp4",
    "qa_report",
    "timeline.json",
    "assets_used",
    "shot_plan",
]


JSON_FORBIDDEN_TERMS = [
    "cannot_claim",
    "does_not_prove",
    "forbidden",
    "reject",
    "must_not",
    "不能说",
    "不要",
    "不得",
]


def chunk_text_source(*, source_id: str, source_path: str, text: str, max_chars: int) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []

    def append_chunk(body_lines: list[str], start_line: int, end_line: int) -> None:
        body = "\n".join(body_lines).strip()
        if not body:
            return
        index = len(chunks) + 1
        chunks.append(
            {
                "chunk_id": make_id("chunk", source_id, index, sha256_text(body)),
                "source_id": source_id,
                "path": source_path,
                "chunk_index": index,
                "start_line": start_line,
                "end_line": end_line,
                "hash": sha256_text(body),
                "text": body,
            }
        )

    def append_paragraph_chunks(section_lines: list[str], section_start_line: int) -> None:
        current: list[str] = []
        current_start_line = section_start_line

        def flush(end_line: int) -> None:
            nonlocal current, current_start_line
            append_chunk(current, current_start_line, end_line)
            current = []
            current_start_line = end_line + 1

        for offset, line in enumerate(section_lines):
            line_no = section_start_line + offset
            if not line.strip():
                flush(line_no - 1)
                current_start_line = line_no + 1
                continue
            if not current:
                current_start_line = line_no
            current.append(line)
            if len("\n".join(current)) >= max_chars:
                flush(line_no)
        flush(section_start_line + len(section_lines) - 1)

    for section_start, section_end, section_lines in markdown_sections(text):
        body = "\n".join(section_lines).strip()
        starts_with_heading = bool(section_lines and re.match(r"^#{1,3}\s+\S", section_lines[0]))
        if (
            starts_with_heading
            and len(field_keys_in_lines(section_lines)) >= 2
            and len(body) <= max_chars * 3
        ):
            append_chunk(section_lines, section_start, section_end)
            continue
        append_paragraph_chunks(section_lines, section_start)
    return chunks


def source_index_markdown(summary: dict[str, Any]) -> str:
    lines = [
        "# Source Index",
        "",
        f"Depth: {summary['depth']}",
        f"Chunks: {summary['chunk_count']}",
        "",
        "## Sources",
    ]
    for source in summary["sources"]:
        lines.append(f"- `{source['source_id']}` chunks={source['chunk_count']} path={source['path']}")
    lines.extend(
        [
            "",
            "## Claim Limits",
        ]
    )
    for limit in summary["claim_limits"]:
        lines.append(f"- {limit}")
    return "\n".join(lines) + "\n"


def matching_failure_events(events: list[dict[str, Any]], problem: str, fingerprint: str) -> list[dict[str, Any]]:
    normalized = normalize_problem(problem)
    matches: list[dict[str, Any]] = []
    for event in events:
        payload = event.get("payload", {})
        if event.get("event_type") != "capture" or payload.get("capture_type") != "failure":
            continue
        if payload.get("problem_fingerprint") == fingerprint:
            matches.append(event)
            continue
        haystack = normalize_problem(f"{payload.get('task', '')} {payload.get('text', '')}")
        if normalized and normalized in haystack:
            matches.append(event)
    return matches


def recover_recipe_draft(
    *,
    target_recipe_id: str,
    problem: str,
    fingerprint: str,
    failures: list[dict[str, Any]],
    base_recipe: dict[str, Any] | None,
) -> dict[str, Any]:
    failure_event_ids = [event["event_id"] for event in failures]
    failure_summary = f"三次同类失败触发 recover：{first_line(problem)} ({fingerprint})"
    if base_recipe:
        draft = {k: v for k, v in base_recipe.items() if k != "recipe_hash"}
        draft["version"] = int(base_recipe.get("version", 0)) + 1
        draft["source_truth_to_read"] = list(dict.fromkeys(draft.get("source_truth_to_read", []) + failure_event_ids))
        draft["evidence_refs"] = list(dict.fromkeys(draft.get("evidence_refs", []) + failure_event_ids))
        draft["related_events"] = list(dict.fromkeys(draft.get("related_events", []) + failure_event_ids))
        draft["failure_signals"] = list(dict.fromkeys(draft.get("failure_signals", []) + [failure_summary]))
        draft["forbidden_path"] = list(
            dict.fromkeys(draft.get("forbidden_path", []) + [f"不要在未处理 {fingerprint} 前重复同一路径。"])
        )
        draft["steps"] = list(dict.fromkeys(draft.get("steps", []) + [failure_summary]))
        draft["verification"] = list(
            dict.fromkeys(draft.get("verification", []) + ["再次执行前必须说明这三次 failure 如何被新规则覆盖。"])
        )
        draft["cannot_claim"] = list(
            dict.fromkeys(draft.get("cannot_claim", []) + ["不能说 recover 候选补丁已证明问题解决。"])
        )
        return draft
    return recipe_draft(
        target_recipe_id,
        f"Recover: {first_line(problem)}",
        failure_summary,
        failure_event_ids,
    )


TIME_RE = re.compile(
    r"(?P<start>(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3})\s*-->\s*"
    r"(?P<end>(?:\d{2}:)?\d{2}:\d{2}[,.]\d{3})"
)


def normalize_timestamp(value: str) -> str:
    value = value.replace(",", ".")
    if value.count(":") == 1:
        value = f"00:{value}"
    return value


def parse_transcript(raw: str) -> list[dict[str, Any]]:
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    cues: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        match = TIME_RE.search(line)
        if not match:
            index += 1
            continue
        start = normalize_timestamp(match.group("start"))
        end = normalize_timestamp(match.group("end"))
        index += 1
        text_lines: list[str] = []
        while index < len(lines):
            current = lines[index].strip()
            if not current:
                break
            if TIME_RE.search(current):
                index -= 1
                break
            text_lines.append(current)
            index += 1
        text = " ".join(text_lines).strip()
        if text:
            cues.append({"index": len(cues) + 1, "start": start, "end": end, "text": text})
        index += 1
    return cues


def transcript_as_vtt(cues: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for cue in cues:
        lines.append(str(cue["index"]))
        lines.append(f"{cue['start']} --> {cue['end']}")
        lines.append(cue["text"])
        lines.append("")
    return "\n".join(lines)


def video_index_markdown(metadata: dict[str, Any]) -> str:
    lines = [
        "# Video Index",
        "",
        f"Course ID: `{metadata['course_id']}`",
        f"Transcript: {metadata['transcript_path']}",
        f"Cues: {metadata['cue_count']}",
        "",
        "## Claim Limits",
    ]
    for limit in metadata["claim_limits"]:
        lines.append(f"- {limit}")
    return "\n".join(lines) + "\n"


def phase0c_evidence(recipes_dir: Path, events: list[dict[str, Any]]) -> dict[str, bool]:
    event_types = {event.get("event_type") for event in events}
    scan = "sources_scanned" in event_types and (recipes_dir / "source_index" / "chunks.jsonl").exists()
    recover = "recover_candidate_created" in event_types
    video_chunks = list((recipes_dir / "video_index").glob("*/chunks.jsonl")) if (recipes_dir / "video_index").exists() else []
    video = "transcript_ingested" in event_types and bool(video_chunks)
    return {"scan": scan, "recover": recover, "video": video, "complete": scan and recover and video}


def source_refinery_evidence(recipes_dir: Path) -> dict[str, Any]:
    refinery_dir = recipes_dir / "source_refinery"
    refined_files = list((refinery_dir / "chunks").glob("refinement_*.jsonl")) if refinery_dir.exists() else []
    card_files = list((refinery_dir / "cards").glob("*_cards/*.json")) if refinery_dir.exists() else []
    patch_drafts = list((refinery_dir / "patch_drafts").glob("*.json")) if refinery_dir.exists() else []
    fusion_files = list((refinery_dir / "fusion").glob("fusion_*.json")) if refinery_dir.exists() else []
    deep_read_plans = list((refinery_dir / "deep_read_plans").glob("deep_read_plan_*.json")) if refinery_dir.exists() else []
    target_suggestion_reports = list((recipes_dir / "reports").glob("target_suggestions_*.json")) if recipes_dir.exists() else []
    review_triage_reports = list((recipes_dir / "reports").glob("review_triage_*.json")) if recipes_dir.exists() else []
    review_packet_reports = list((recipes_dir / "reports").glob("review_packet_*.json")) if recipes_dir.exists() else []
    fusion_candidates = 0
    for fusion_path in fusion_files:
        fusion_candidates += len(read_json(fusion_path, {}).get("candidates", []))
    target_suggestions = 0
    for report_path in target_suggestion_reports:
        target_suggestions += int(read_json(report_path, {}).get("suggestion_count") or 0)
    review_triage_items = 0
    for report_path in review_triage_reports:
        review_triage_items += int(read_json(report_path, {}).get("summary", {}).get("review_count") or 0)
    review_packet_items = 0
    for report_path in review_packet_reports:
        review_packet_items += int(read_json(report_path, {}).get("summary", {}).get("review_count") or 0)
    return {
        "refinements": len(refined_files),
        "cards": len(card_files),
        "patch_drafts": len(patch_drafts),
        "fusions": len(fusion_files),
        "fusion_candidates": fusion_candidates,
        "deep_read_plans": len(deep_read_plans),
        "target_suggestion_reports": len(target_suggestion_reports),
        "target_suggestions": target_suggestions,
        "review_triage_reports": len(review_triage_reports),
        "review_triage_items": review_triage_items,
        "review_packet_reports": len(review_packet_reports),
        "review_packet_items": review_packet_items,
        "has_activity": bool(refined_files or card_files or patch_drafts or fusion_files or deep_read_plans or target_suggestion_reports or review_triage_reports or review_packet_reports),
    }


def cloud_adapter_evidence(recipes_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    config = read_json(recipes_dir / "cloud" / "deepseek" / "config.json", {})
    status = read_json(recipes_dir / "cloud" / "deepseek" / "status.json", {})
    matching = [
        event
        for event in events
        if event.get("event_type") == "cloud_text_refined"
        and event.get("payload", {}).get("provider") == "deepseek"
    ]
    api_key_env = str(config.get("api_key_env") or "AGENT_RECIPES_DEEPSEEK_API_KEY")
    api_key_present = bool(os.environ.get(api_key_env)) if config.get("config_status") == "configured" else False
    last_event = matching[-1] if matching else {}
    return {
        "deepseek": {
            "provider": "deepseek",
            "config_status": config.get("config_status", "not_configured"),
            "candidate_only": True,
            "cloud_adapter": True,
            "text_only": True,
            "vision_supported": False,
            "model": config.get("model"),
            "pro_model": config.get("pro_model"),
            "base_url": config.get("base_url"),
            "api_key_env": api_key_env if config else None,
            "api_key_present": api_key_present,
            "api_key_stored": False,
            "runtime_verified": bool(matching or status.get("runtime_verified")),
            "runtime_events": len(matching),
            "last_execution_mode": status.get("last_execution_mode") or last_event.get("payload", {}).get("execution_mode"),
            "last_model": status.get("last_model") or last_event.get("payload", {}).get("model"),
            "last_run_id": status.get("last_run_id") or last_event.get("payload", {}).get("run_id"),
            "notes": [
                "cloud adapter output is candidate only",
                "DeepSeek V4 Flash/Pro are treated as text LLM adapters, not vision adapters",
                "API key is read from environment only and not stored in project files",
            ],
        }
    }


def embedding_adapter_evidence(recipes_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    config, _ = read_optional_json(recipes_dir / "embeddings" / "qwen3" / "config.json", {})
    status, _ = read_optional_json(recipes_dir / "embeddings" / "qwen3" / "status.json", {})
    index_path = recipes_dir / "embeddings" / "qwen3" / "index.jsonl"
    index_rows, _ = read_optional_jsonl(index_path)
    matching = [
        event
        for event in events
        if event.get("event_type") in {"embedding_indexed", "embedding_searched"}
        and event.get("payload", {}).get("provider") == "qwen3"
    ]
    index_events = [event for event in matching if event.get("event_type") == "embedding_indexed"]
    search_events = [event for event in matching if event.get("event_type") == "embedding_searched"]
    return {
        "qwen3": {
            "provider": "qwen3",
            "config_status": config.get("config_status", "not_configured"),
            "candidate_only": True,
            "local_adapter": True,
            "loopback_only": True,
            "model": config.get("model"),
            "endpoint": config.get("endpoint"),
            "dimensions": config.get("dimensions"),
            "runtime_verified": bool(index_events or status.get("runtime_verified")),
            "runtime_events": len(index_events),
            "search_events": len(search_events),
            "indexed_count": status.get("indexed_count") or len(index_rows),
            "index_path": str(index_path) if index_path.exists() else None,
            "last_execution_mode": status.get("last_execution_mode"),
            "ollama_binary": shutil.which("ollama"),
            "llama_server_binary": shutil.which("llama-server"),
            "notes": [
                "embedding output is candidate-only recall evidence",
                "Qwen3-Embedding-0.6B is used as the preferred lightweight local target",
                "loopback service calls require explicit allow-loopback",
            ],
        }
    }


def read_qwen_embedding_config(root: Path) -> dict[str, Any]:
    config = read_json(root / ".recipes" / "embeddings" / "qwen3" / "config.json", {})
    if config.get("provider") != "qwen3":
        return {}
    return config


def is_qwen3_embedding_model(model: str) -> bool:
    return "qwen3-embedding" in str(model or "").casefold()


def replay_embedding_for_text(replay: dict[str, Any], text: str) -> list[float]:
    lowered = normalize_problem(text)
    for rule in replay.get("rules", []) if isinstance(replay.get("rules"), list) else []:
        if not isinstance(rule, dict):
            continue
        contains = normalize_problem(str(rule.get("contains") or ""))
        if contains and contains in lowered:
            return normalize_embedding_vector(rule.get("embedding"))
    if "embedding" in replay:
        return normalize_embedding_vector(replay.get("embedding"))
    return normalize_embedding_vector(replay.get("default_embedding"))


def replay_query_embedding(replay: dict[str, Any]) -> list[float]:
    return normalize_embedding_vector(replay.get("embedding") or replay.get("default_embedding"))


def normalize_embedding_vector(value: Any) -> list[float]:
    if not isinstance(value, list):
        raise RecipesError("AR379", "embedding replay 缺少向量。", "需要 embedding 或 default_embedding 数组。", "检查 response-json。")
    vector: list[float] = []
    for item in value:
        try:
            vector.append(float(item))
        except (TypeError, ValueError) as exc:
            raise RecipesError("AR379", "embedding 向量包含非数字。", repr(item), "检查 response-json。") from exc
    if not vector:
        raise RecipesError("AR379", "embedding 向量为空。", "需要至少一个数字。", "检查 response-json。")
    return vector


def cosine_similarity(left: list[float], right: list[float]) -> float:
    size = min(len(left), len(right))
    if size == 0:
        return 0.0
    dot = sum(left[index] * right[index] for index in range(size))
    left_norm = sum(left[index] * left[index] for index in range(size)) ** 0.5
    right_norm = sum(right[index] * right[index] for index in range(size)) ** 0.5
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def call_loopback_embedding(config: dict[str, Any], text: str, *, timeout: int) -> list[float]:
    endpoint = str(config.get("endpoint") or "")
    if not endpoint_is_loopback(endpoint):
        raise RecipesError("AR375", "embedding endpoint 不安全。", endpoint, "只允许 loopback endpoint。")
    body = {"model": config.get("model") or "qwen3-embedding:0.6b", "input": text}
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-1000:]
        raise RecipesError("AR375", "本地 Qwen3 embedding 服务调用失败。", f"HTTP {exc.code}: {detail}", "确认 Ollama 或 llama.cpp Qwen3-Embedding 服务已启动。") from exc
    except (urllib.error.URLError, TimeoutError, http.client.HTTPException) as exc:
        raise RecipesError("AR375", "本地 Qwen3 embedding 服务调用失败。", str(exc), "确认 Ollama 或 llama.cpp Qwen3-Embedding 服务已启动。") from exc
    if isinstance(payload.get("data"), list) and payload["data"]:
        first = payload["data"][0]
        if isinstance(first, dict) and isinstance(first.get("embedding"), list):
            return normalize_embedding_vector(first["embedding"])
    if isinstance(payload.get("embeddings"), list) and payload["embeddings"]:
        return normalize_embedding_vector(payload["embeddings"][0])
    if isinstance(payload.get("embedding"), list):
        return normalize_embedding_vector(payload["embedding"])
    raise RecipesError("AR375", "本地 embedding 服务返回结构不支持。", stable_json(payload)[:1000], "检查 endpoint 是否是 Ollama /api/embed 或 OpenAI-compatible /v1/embeddings。")


def looks_like_secret(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("sk-", "ak-", "rk-")) or bool(re.fullmatch(r"[a-z0-9_-]{24,}", lowered) and "_" not in value)


def safe_env_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z_][A-Z0-9_]*", value or ""))


def redact_cloud_config(config: dict[str, Any]) -> dict[str, Any]:
    redacted = json.loads(json.dumps(config, ensure_ascii=False))
    redacted.pop("updated_at", None)
    redacted.pop("api_key", None)
    redacted["api_key_present"] = bool(redacted.get("api_key_present"))
    return redacted


def read_deepseek_cloud_config(root: Path) -> dict[str, Any]:
    config = read_json(root / ".recipes" / "cloud" / "deepseek" / "config.json", {})
    if config.get("provider") != "deepseek":
        return {}
    return config


def normalize_cloud_cards_response(response: dict[str, Any]) -> list[dict[str, Any]]:
    if "choices" in response:
        try:
            content = response["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RecipesError("AR366", "DeepSeek API 响应结构不符合预期。", str(exc), "检查 DeepSeek 返回内容。") from exc
        try:
            response = json.loads(content)
        except json.JSONDecodeError as exc:
            raise RecipesError("AR366", "DeepSeek API 没有返回合法 JSON。", content[:1000], "重试或降低输入复杂度。") from exc
    cards = response.get("cards") if isinstance(response, dict) else None
    if not isinstance(cards, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in cards:
        if isinstance(raw, dict):
            normalized.append(raw)
    return normalized


def call_deepseek_cards_api(
    *,
    text: str,
    config: dict[str, Any],
    model: str,
    candidate_fields: list[str],
    knowledge_need_id: str,
    target_recipe_id: str,
    timeout: int,
) -> dict[str, Any]:
    api_key_env = str(config.get("api_key_env") or "AGENT_RECIPES_DEEPSEEK_API_KEY")
    api_key = os.environ.get(api_key_env)
    if not api_key:
        raise RecipesError("AR365", "DeepSeek API key 未配置。", f"环境变量 {api_key_env} 为空。", f"设置 {api_key_env} 后再显式运行 --allow-network。")
    base_url = str(config.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if not endpoint_is_deepseek_api(base_url):
        raise RecipesError("AR365", "DeepSeek base-url 不在允许范围。", base_url, "使用 https://api.deepseek.com。")
    endpoint = f"{base_url}/chat/completions"
    system = (
        "You extract Agent Recipes source_refinery cards from text. "
        "Return only JSON with a cards array. "
        "Each card must include card_type, target_fields, extracted_payload, source_quote, cannot_claim. "
        "Allowed card_type values: correction_card, run_chain_card, failure_card, learning_atom_card, visual_example_card. "
        "All output is candidate-only and must not claim formal recipe changes."
    )
    user = {
        "knowledge_need_id": knowledge_need_id,
        "target_recipe_id": target_recipe_id,
        "candidate_fields": candidate_fields,
        "text": text[:120000],
    }
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_tokens": 4096,
    }
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[-2000:]
        raise RecipesError("AR365", "DeepSeek API 调用失败。", f"HTTP {exc.code}: {detail}", "检查余额、模型名、rate limit 或稍后重试。") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RecipesError("AR365", "DeepSeek API 调用失败。", str(exc), "检查网络或稍后重试。") from exc
    except json.JSONDecodeError as exc:
        raise RecipesError("AR365", "DeepSeek API 返回不是合法 JSON。", str(exc), "稍后重试。") from exc


def cards_from_cloud_response(
    *,
    recipes_dir: Path,
    provider: str,
    model: str,
    run_id: str,
    source_path: Path,
    source_hash: str,
    source_text: str,
    cards_payload: list[dict[str, Any]],
    knowledge_need_id: str,
    target_recipe_id: str,
    allowed_fields: list[str],
    execution_mode: str,
) -> tuple[list[dict[str, Any]], list[Path], dict[str, int]]:
    cards: list[dict[str, Any]] = []
    files: list[Path] = []
    counts: dict[str, int] = {}
    for index, item in enumerate(cards_payload, start=1):
        card_type = str(item.get("card_type") or "learning_atom_card")
        if card_type not in CARD_TYPES:
            card_type = "learning_atom_card"
        raw_fields = item.get("target_fields") or infer_candidate_fields(stable_json(item))
        if not isinstance(raw_fields, list):
            raw_fields = [str(raw_fields)]
        target_fields = [field for field in normalize_candidate_fields([str(field) for field in raw_fields]) if not allowed_fields or field in allowed_fields]
        if not target_fields:
            target_fields = allowed_fields[:1] or ["cannot_claim"]
        extracted_payload = normalize_cloud_payload(item.get("extracted_payload") or {})
        cannot_claim = normalize_claim_limit_list(item.get("cannot_claim")) or normalize_claim_limit_list(
            extracted_payload.get("cannot_claim")
        ) or [
            "不能说 cloud adapter 输出已经进入正式 recipe。"
        ]
        source_quote = str(item.get("source_quote") or "").strip()
        if not source_quote:
            source_quote = "\n".join(extracted_payload.get("source_quote", [])).strip()
        source_trace = [
            {
                "source_kind": "cloud_refine_input",
                "provider": provider,
                "model": model,
                "execution_mode": execution_mode,
                "run_id": run_id,
                "path": str(source_path),
                "source_hash": source_hash,
                "source_quote": source_quote,
            }
        ]
        card = {
            "card_id": make_id("card", "cloud", provider, model, run_id, index, card_type, extracted_payload, source_quote),
            "card_type": card_type,
            "provider": provider,
            "model": model,
            "execution_mode": execution_mode,
            "cloud_run_id": run_id,
            "source_chunk_ids": [run_id],
            "source_trace": source_trace,
            "knowledge_need_id": knowledge_need_id,
            "target_recipe_id": target_recipe_id,
            "target_fields": target_fields,
            "evidence_strength": "candidate",
            "extracted_payload": extracted_payload,
            "cannot_claim": cannot_claim,
            "status": "candidate",
            "claim_limits": [
                "cloud card 是 candidate，不是 verified truth。",
                "不能直接写正式 recipe。",
                "必须经过 review_queue。",
            ],
        }
        if not extracted_payload:
            card["extracted_payload"] = {
                "source_quote": [source_quote or source_text[:200]],
                "cannot_claim": cannot_claim,
            }
        cards.append(card)
        counts[card_type] = counts.get(card_type, 0) + 1
        files.append(recipes_dir / "source_refinery" / "cards" / card_dir_for_type(card_type) / f"{card['card_id']}.json")
    return cards, files, counts


def normalize_cloud_payload(payload: Any) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key, value in payload.items():
        normalized_key = normalize_field_key(str(key))
        clean_key = FIELD_ALIASES.get(normalized_key, normalized_key)
        values = normalize_claim_limit_list(value) if clean_key == "cannot_claim" else normalize_string_list(value)
        if values:
            normalized[clean_key] = values
    return normalized


def normalize_claim_limit_list(value: Any) -> list[str]:
    return [text for text in normalize_string_list(value) if claim_limit_is_useful(text)]


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def claim_limit_is_useful(limit: str) -> bool:
    text = candidate_quality_normalized_text(str(limit)).strip().casefold()
    compact = re.sub(r"[\s\\/_:：,，.。;；-]+", "", text)
    if not compact:
        return False
    if compact in {"false", "true", "none", "null", "na", "n/a", "unknown", "未知", "无", "空"}:
        return False
    return len(compact) >= 6


def proposed_claim_limit_values(values: list[str]) -> list[str]:
    return [
        value
        for value in values
        if claim_limit_is_useful(value)
        and claim_limit_has_boundary_language(value)
        and not looks_like_claim_status_path_noise(value)
    ]


def claim_limit_has_boundary_language(limit: str) -> bool:
    text = str(limit).casefold()
    normalized = candidate_quality_normalized_text(text)
    compact = re.sub(r"[\s\\/_:：,，.。;；-]+", "", normalized)
    return any(
        marker in text or marker in normalized or marker in compact
        for marker in [
            "不能说",
            "不能 claim",
            "不能claim",
            "不能把",
            "不能等于",
            "不等于",
            "cannot claim",
            "cannot say",
            "candidate only",
            "candidate seed only",
            "draft only",
            "evidence only",
            "does not prove",
            "does_not_prove",
            "not prove",
            "not verified",
            "without review",
        ]
    )


def looks_like_claim_status_path_noise(limit: str) -> bool:
    text = " ".join(str(limit).split()).casefold()
    if not text:
        return False
    if "status: readable" in text or " - status:" in text:
        return True
    if re.search(r"(^|[`\\s])(?:docs|archive|fixtures|/users)/[^\\s`]+\\.(?:md|json|txt|csv|yaml|yml)", text):
        return True
    return False


def cognee_memory_records(recipes_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def add_record(source_kind: str, source_id: str, text: str, source_trace: list[dict[str, Any]], extra: dict[str, Any]) -> None:
        body = " ".join(text.split())
        if not body:
            return
        record_id = make_id("mem", "cognee", source_kind, source_id, sha256_text(body))
        if record_id in seen_ids:
            return
        seen_ids.add(record_id)
        records.append(
            {
                "record_id": record_id,
                "memory_record_id": record_id,
                "record_type": "memory_candidate",
                "adapter": "cognee",
                "source_kind": source_kind,
                "source_id": source_id,
                "text": body,
                "source_trace": source_trace or [{"source_kind": source_kind, "source_id": source_id}],
                "evidence_status": "candidate",
                "evidence_strength": "candidate",
                "cannot_claim": [
                    "不能说 Cognee memory candidate 已经验证。",
                    "不能说 Cognee memory candidate 已经进入正式 recipe。",
                ],
                **extra,
            }
        )

    for chunk_path in sorted((recipes_dir / "source_refinery" / "chunks").glob("refinement_*.jsonl")):
        for chunk in read_jsonl(chunk_path):
            source_id = str(chunk.get("refined_chunk_id") or "")
            text = chunk.get("source_record", {}).get("text", "")
            add_record(
                "refined_chunk",
                source_id,
                text,
                chunk.get("source_trace", []),
                {
                    "target_recipe_id": chunk.get("target_recipe_id"),
                    "target_fields": chunk.get("candidate_fields", []),
                    "knowledge_need_id": chunk.get("knowledge_need_id"),
                    "status": chunk.get("status"),
                },
            )

    for card in read_jsonl(recipes_dir / "source_refinery" / "cards" / "cards.jsonl"):
        source_id = str(card.get("card_id") or "")
        text = memory_text_from_mapping(
            {
                "card_type": card.get("card_type"),
                "target_fields": card.get("target_fields", []),
                "payload": card.get("extracted_payload", {}),
                "cannot_claim": card.get("cannot_claim", []),
            }
        )
        add_record(
            "card",
            source_id,
            text,
            card.get("source_trace", []),
            {
                "target_recipe_id": card.get("target_recipe_id"),
                "target_fields": card.get("target_fields", []),
                "knowledge_need_id": card.get("knowledge_need_id"),
                "card_type": card.get("card_type"),
            },
        )

    for draft_path in sorted((recipes_dir / "source_refinery" / "patch_drafts").glob("*.json")):
        draft = read_json(draft_path, {})
        source_id = str(draft.get("patch_draft_id") or draft_path.stem)
        text = memory_text_from_mapping(
            {
                "reason": draft.get("reason"),
                "target_fields": draft.get("target_fields", []),
                "proposed_additions": draft.get("proposed_additions", {}),
                "cannot_claim": draft.get("cannot_claim", []),
            }
        )
        add_record(
            "patch_draft",
            source_id,
            text,
            [{"source_kind": "patch_draft", "source_id": source_id, "path": str(draft_path)}],
            {
                "target_recipe_id": draft.get("target_recipe_id"),
                "target_fields": draft.get("target_fields", []),
                "source_card_ids": draft.get("source_card_ids", []),
            },
        )

    for review_path in sorted((recipes_dir / "review_queue").glob("*.json")):
        review = read_json(review_path, {})
        if not review.get("source_patch_draft_id"):
            continue
        source_id = str(review.get("review_id") or review_path.stem)
        text = memory_text_from_mapping(
            {
                "question": review.get("question"),
                "why_user_must_decide": review.get("why_user_must_decide"),
                "recommendation": review.get("recommendation"),
                "status": review.get("status"),
            }
        )
        add_record(
            "review_item",
            source_id,
            text,
            [{"source_kind": "review_item", "source_id": source_id, "path": str(review_path)}],
            {
                "target_recipe_id": review.get("target_recipe_id"),
                "source_patch_draft_id": review.get("source_patch_draft_id"),
                "review_status": review.get("status"),
            },
        )

    records.sort(key=lambda item: (item.get("source_kind") or "", item.get("source_id") or ""))
    return records


def graphiti_memory_graph(recipes_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records = cognee_memory_records(recipes_dir)
    nodes: dict[str, dict[str, Any]] = {}
    edges: dict[str, dict[str, Any]] = {}

    def normalized_trace(source_trace: list[dict[str, Any]] | None, fallback_kind: str, fallback_id: str) -> list[dict[str, Any]]:
        return source_trace or [{"source_kind": fallback_kind, "source_id": fallback_id}]

    def add_node(
        node_type: str,
        source_id: str,
        label: str,
        text: str,
        source_trace: list[dict[str, Any]] | None,
        extra: dict[str, Any] | None = None,
    ) -> str:
        clean_source_id = source_id or label or node_type
        node_id = make_id("gnode", node_type, clean_source_id)
        trace = normalized_trace(source_trace, node_type, clean_source_id)
        body = " ".join(str(text or label or clean_source_id).split())
        if node_id not in nodes:
            nodes[node_id] = {
                "node_id": node_id,
                "record_id": node_id,
                "adapter": "graphiti",
                "node_type": node_type,
                "source_id": clean_source_id,
                "label": label or clean_source_id,
                "text": body,
                "source_trace": trace,
                "evidence_status": "candidate",
                "evidence_strength": "candidate",
                "cannot_claim": [
                    "不能说 Graphiti graph candidate 已经验证。",
                    "不能说 Graphiti graph candidate 已经进入正式 recipe。",
                ],
            }
        if extra:
            for key, value in extra.items():
                if value not in (None, [], ""):
                    nodes[node_id].setdefault(key, value)
        return node_id

    def add_edge(
        source_node_id: str,
        target_node_id: str,
        relation_type: str,
        text: str,
        source_trace: list[dict[str, Any]] | None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        if not source_node_id or not target_node_id:
            return
        edge_id = make_id("gedge", source_node_id, relation_type, target_node_id, text)
        if edge_id in edges:
            return
        trace = normalized_trace(source_trace, "graph_edge", edge_id)
        edges[edge_id] = {
            "edge_id": edge_id,
            "record_id": edge_id,
            "adapter": "graphiti",
            "source_node_id": source_node_id,
            "target_node_id": target_node_id,
            "relation_type": relation_type,
            "text": " ".join(str(text).split()),
            "source_trace": trace,
            "evidence_status": "candidate",
            "evidence_strength": "candidate",
            "cannot_claim": [
                "不能说 Graphiti graph edge 已经验证。",
                "不能说 Graphiti graph edge 已经进入正式 recipe。",
            ],
        }
        if extra:
            for key, value in extra.items():
                if value not in (None, [], ""):
                    edges[edge_id][key] = value

    for record in records:
        source_kind = str(record.get("source_kind") or "memory_candidate")
        source_id = str(record.get("source_id") or record.get("record_id") or "")
        source_trace = record.get("source_trace", [])
        text = str(record.get("text") or "")
        card_type = str(record.get("card_type") or "")
        node_type = card_type if source_kind == "card" and card_type else source_kind
        candidate_node = add_node(
            node_type,
            source_id,
            f"{node_type}:{source_id}",
            text,
            source_trace,
            {
                "source_kind": source_kind,
                "target_recipe_id": record.get("target_recipe_id"),
                "target_fields": record.get("target_fields", []),
                "knowledge_need_id": record.get("knowledge_need_id"),
                "card_type": card_type or None,
            },
        )
        target_recipe_id = record.get("target_recipe_id")
        if target_recipe_id:
            recipe_node = add_node(
                "recipe",
                str(target_recipe_id),
                str(target_recipe_id),
                f"recipe {target_recipe_id}",
                source_trace,
            )
            add_edge(
                candidate_node,
                recipe_node,
                "targets_recipe",
                f"{node_type} targets recipe {target_recipe_id}. {text}",
                source_trace,
                {"target_recipe_id": target_recipe_id},
            )
        knowledge_need_id = record.get("knowledge_need_id")
        if knowledge_need_id:
            need_node = add_node(
                "knowledge_need",
                str(knowledge_need_id),
                str(knowledge_need_id),
                f"knowledge need {knowledge_need_id}",
                source_trace,
            )
            add_edge(
                candidate_node,
                need_node,
                "answers_knowledge_need",
                f"{node_type} answers knowledge need {knowledge_need_id}. {text}",
                source_trace,
                {"knowledge_need_id": knowledge_need_id},
            )
        for field in record.get("target_fields", []) or []:
            field_node = add_node("target_field", str(field), str(field), f"target field {field}", source_trace)
            add_edge(
                candidate_node,
                field_node,
                "touches_field",
                f"{node_type} touches target field {field}. {text}",
                source_trace,
                {"target_field": field},
            )
        if card_type:
            type_node = add_node("card_type", card_type, card_type, f"card type {card_type}", source_trace)
            add_edge(
                candidate_node,
                type_node,
                "classified_as",
                f"{source_kind} classified as {card_type}. {text}",
                source_trace,
                {"card_type": card_type},
            )
        for source_card_id in record.get("source_card_ids", []) or []:
            card_node = add_node("card", str(source_card_id), str(source_card_id), f"source card {source_card_id}", source_trace)
            add_edge(
                candidate_node,
                card_node,
                "derived_from_card",
                f"{node_type} derived from card {source_card_id}. {text}",
                source_trace,
                {"source_card_id": source_card_id},
            )
        source_patch_draft_id = record.get("source_patch_draft_id")
        if source_patch_draft_id:
            draft_node = add_node(
                "patch_draft",
                str(source_patch_draft_id),
                str(source_patch_draft_id),
                f"patch draft {source_patch_draft_id}",
                source_trace,
            )
            add_edge(
                candidate_node,
                draft_node,
                "reviews_patch_draft",
                f"{node_type} reviews patch draft {source_patch_draft_id}. {text}",
                source_trace,
                {"source_patch_draft_id": source_patch_draft_id},
            )
        for trace in source_trace or []:
            trace_id = str(trace.get("source_id") or trace.get("path") or trace.get("source_kind") or "")
            if not trace_id:
                continue
            trace_kind = str(trace.get("source_kind") or "source_trace")
            trace_node = add_node(trace_kind, trace_id, trace_id, f"{trace_kind} {trace_id}", [trace])
            add_edge(
                candidate_node,
                trace_node,
                "evidenced_by",
                f"{node_type} is evidenced by {trace_kind} {trace_id}.",
                [trace],
                {"trace_kind": trace_kind, "trace_id": trace_id},
            )

    node_rows = sorted(nodes.values(), key=lambda item: (item.get("node_type") or "", item.get("node_id") or ""))
    edge_rows = sorted(edges.values(), key=lambda item: (item.get("relation_type") or "", item.get("edge_id") or ""))
    return node_rows, edge_rows


def graphiti_search_records(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    node_labels = {node.get("node_id"): node.get("label") for node in nodes}
    for node in nodes:
        item = dict(node)
        item["result_type"] = "node"
        item["record_id"] = node.get("node_id")
        records.append(item)
    for edge in edges:
        item = dict(edge)
        source_label = node_labels.get(edge.get("source_node_id"), edge.get("source_node_id"))
        target_label = node_labels.get(edge.get("target_node_id"), edge.get("target_node_id"))
        item["result_type"] = "edge"
        item["record_id"] = edge.get("edge_id")
        item["text"] = " ".join(
            str(part)
            for part in [
                source_label,
                edge.get("relation_type"),
                target_label,
                edge.get("text"),
            ]
            if part
        )
        records.append(item)
    return records


def memory_text_from_mapping(value: Any) -> str:
    parts: list[str] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            return
        if isinstance(item, list):
            for child in item:
                visit(child)
            return
        if isinstance(item, dict):
            for key, child in item.items():
                parts.append(str(key))
                visit(child)
            return
        parts.append(str(item))

    visit(value)
    return " ".join(parts)


def graphiti_probe_env(runtime_root: Path) -> dict[str, str]:
    home = runtime_root / "home"
    cache = runtime_root / "cache"
    data = runtime_root / "data"
    logs = runtime_root / "logs"
    for path in [home, cache, data, logs]:
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_CACHE_HOME": str(cache),
        "GRAPHITI_TELEMETRY_ENABLED": "false",
        "PYTHON_DOTENV_DISABLED": "1",
        "GRAPHITI_NATIVE_PROBE": "true",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "GROQ_API_KEY": "",
        "VOYAGE_API_KEY": "",
    }


def public_graphiti_probe_env(env: dict[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in env.items()
        if key
        in {
            "HOME",
            "XDG_CACHE_HOME",
            "GRAPHITI_TELEMETRY_ENABLED",
            "PYTHON_DOTENV_DISABLED",
            "GRAPHITI_NATIVE_PROBE",
        }
    }


def run_graphiti_native_probe(root: Path, python: Path, *, timeout: int = 20) -> dict[str, Any]:
    runtime_root = root / ".recipes" / "memory" / "graphiti" / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    probe_env = graphiti_probe_env(runtime_root)
    env.update(probe_env)
    public_env = public_graphiti_probe_env(probe_env)
    script = r'''
import asyncio, json, os, pathlib, socket, sys, traceback, warnings

payload = json.loads(sys.argv[1])
runtime_root = pathlib.Path(payload["runtime_root"]).resolve()
for key, value in payload["env"].items():
    os.environ[key] = value

network_attempts = []
original_connect = socket.socket.connect
original_create_connection = socket.create_connection

def blocked_connect(self, address):
    network_attempts.append(repr(address))
    raise RuntimeError(f"network disabled during Agent Recipes Graphiti native probe: {address!r}")

def blocked_create_connection(address, *args, **kwargs):
    network_attempts.append(repr(address))
    raise RuntimeError(f"network disabled during Agent Recipes Graphiti native probe: {address!r}")

socket.socket.connect = blocked_connect
socket.create_connection = blocked_create_connection

def under(path_value, root_value):
    try:
        pathlib.Path(path_value).resolve().relative_to(root_value)
        return True
    except Exception:
        return False

async def main():
    from graphiti_core import Graphiti
    from graphiti_core.cross_encoder.client import CrossEncoderClient
    from graphiti_core.driver.kuzu_driver import KuzuDriver
    from graphiti_core.embedder.client import EmbedderClient
    from graphiti_core.llm_client.client import LLMClient
    from graphiti_core.llm_client.config import LLMConfig
    from graphiti_core.nodes import EntityNode

    class AgentRecipesLocalEmbedder(EmbedderClient):
        async def create(self, input_data):
            return [0.01] * 8

        async def create_batch(self, input_data_list):
            return [[0.01] * 8 for _ in input_data_list]

    class AgentRecipesLocalLLM(LLMClient):
        def __init__(self):
            super().__init__(
                LLMConfig(
                    model="agent-recipes-local-stub",
                    api_key="local-only",
                    base_url="http://127.0.0.1/agent-recipes-local-stub",
                ),
                cache=False,
            )

        async def _generate_response(self, messages, response_model=None, max_tokens=0, model_size=None):
            raise RuntimeError("Agent Recipes Graphiti native probe must not call an LLM")

    class AgentRecipesLocalCrossEncoder(CrossEncoderClient):
        async def rank(self, query, passages):
            return [(passage, 1.0 / (index + 1)) for index, passage in enumerate(passages)]

    db_path = runtime_root / "graphiti_native.kuzu"
    steps = []
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        graph = Graphiti(
            graph_driver=KuzuDriver(str(db_path)),
            llm_client=AgentRecipesLocalLLM(),
            embedder=AgentRecipesLocalEmbedder(),
            cross_encoder=AgentRecipesLocalCrossEncoder(),
        )
        steps.append("graphiti_initialized")
        await graph.build_indices_and_constraints()
        steps.append("schema_built")
        node = EntityNode(
            uuid="agent-recipes-graphiti-native-probe-node",
            name="Agent Recipes Graphiti native probe node",
            group_id="agent-recipes-native-probe",
        )
        await graph.nodes.entity.save(node)
        loaded = await graph.nodes.entity.get_by_uuid(node.uuid)
        steps.append("node_write_read")
        await graph.close()
        steps.append("closed")
    return {
        "steps": steps,
        "node_name": loaded.name,
        "db_path": str(db_path),
        "warnings": [str(item.message) for item in caught],
    }

env_paths = [
    os.environ["HOME"],
    os.environ["XDG_CACHE_HOME"],
    str(runtime_root),
]
paths_caged = all(under(path, runtime_root) for path in env_paths)
try:
    outcome = asyncio.run(main())
    native_ok = (
        outcome.get("node_name") == "Agent Recipes Graphiti native probe node"
        and "schema_built" in outcome.get("steps", [])
        and "node_write_read" in outcome.get("steps", [])
        and not network_attempts
        and paths_caged
    )
    result = {
        "native_status": "available" if native_ok else "unavailable",
        "runtime_verified": bool(native_ok),
        "adapter": "graphiti",
        "mode": "native_graphiti_kuzu_local_lifecycle_probe",
        "candidate_only": True,
        "driver": "kuzu",
        "db_path": outcome.get("db_path"),
        "steps": outcome.get("steps", []),
        "node_name": outcome.get("node_name"),
        "warnings": outcome.get("warnings", []),
        "llm_network_used": False,
    }
except Exception as exc:
    result = {
        "native_status": "unavailable",
        "runtime_verified": False,
        "adapter": "graphiti",
        "mode": "native_graphiti_kuzu_local_lifecycle_probe",
        "candidate_only": True,
        "driver": "kuzu",
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback_tail": traceback.format_exc()[-2000:],
        "llm_network_used": False,
    }

result.update({
    "runtime_root": str(runtime_root),
    "paths_caged": paths_caged,
    "network_blocked": True,
    "network_attempts": network_attempts,
    "env": payload["public_env"],
    "claim_limits": [
        "native probe 只证明 Graphiti package、Kuzu driver、schema 和节点写读能在本地安全跑通。",
        "不能证明 Graphiti LLM 抽取、真实 embedding 质量、生产级图谱质量或长期记忆质量。",
        "不能写正式 recipe，不能绕过 review_queue。",
    ],
})
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
socket.socket.connect = original_connect
socket.create_connection = original_create_connection
'''
    try:
        proc = subprocess.run(
            [
                str(python),
                "-c",
                script,
                json.dumps(
                    {"env": probe_env, "public_env": public_env, "runtime_root": str(runtime_root)},
                    ensure_ascii=False,
                ),
            ],
            cwd=runtime_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return graphiti_native_probe_unavailable(
            root,
            timeout=timeout,
            reason="timeout",
            detail=f"Graphiti native probe timed out after {timeout}s.",
            network_attempts=[],
        )
    if proc.returncode != 0:
        return graphiti_native_probe_unavailable(
            root,
            timeout=timeout,
            reason="subprocess_failed",
            detail=(proc.stderr or proc.stdout)[-2000:],
        )
    try:
        result = parse_adapter_json(proc.stdout)
    except json.JSONDecodeError:
        return graphiti_native_probe_unavailable(
            root,
            timeout=timeout,
            reason="invalid_json",
            detail=proc.stdout[-1000:],
        )
    if proc.stderr.strip():
        result.setdefault("stderr_warnings", [])
        result["stderr_warnings"].append(proc.stderr.strip()[-1000:])
    return result


def graphiti_native_probe_unavailable(
    root: Path,
    *,
    timeout: int,
    reason: str,
    detail: str,
    network_attempts: list[str] | None = None,
) -> dict[str, Any]:
    runtime_root = root / ".recipes" / "memory" / "graphiti" / "runtime"
    probe_env = graphiti_probe_env(runtime_root)
    return {
        "native_status": "unavailable",
        "runtime_verified": False,
        "adapter": "graphiti",
        "mode": "native_graphiti_kuzu_local_lifecycle_probe",
        "candidate_only": True,
        "driver": "kuzu",
        "runtime_root": str(runtime_root),
        "paths_caged": True,
        "network_blocked": True,
        "network_attempts": network_attempts or [],
        "env": public_graphiti_probe_env(probe_env),
        "timeout": timeout,
        "error_type": reason,
        "error": detail,
        "llm_network_used": False,
        "claim_limits": [
            "native probe 未通过，只能说明 Graphiti native runtime 当前不可用。",
            "不能证明 Graphiti LLM 抽取、真实 embedding 质量、生产级图谱质量或长期记忆质量。",
        ],
    }


def run_cognee_runtime_probe(root: Path, python: Path) -> dict[str, Any]:
    runtime_root = root / ".recipes" / "memory" / "cognee" / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    probe_env = cognee_probe_env(runtime_root)
    env.update(probe_env)
    script = r'''
import contextlib, json, os, sys
payload=json.loads(sys.argv[1])
for key, value in payload["env"].items():
    os.environ[key]=value
with contextlib.redirect_stdout(sys.stderr):
    import cognee
print(json.dumps({
    "runtime_verified": True,
    "adapter": "cognee",
    "version": getattr(cognee, "__version__", None),
    "module_file": getattr(cognee, "__file__", None),
    "mode": "import_probe_only",
    "candidate_only": True,
    "configured_runtime_root": payload["runtime_root"],
}, ensure_ascii=False))
'''
    proc = subprocess.run(
        [str(python), "-c", script, json.dumps({"env": probe_env, "runtime_root": str(runtime_root)}, ensure_ascii=False)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        env=env,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RecipesError("AR336", "Cognee runtime probe 失败。", (proc.stderr or proc.stdout)[-2000:], "运行 capabilities 检查 cognee 依赖。")
    try:
        result = parse_adapter_json(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RecipesError("AR337", "Cognee runtime probe 没有返回合法 JSON。", proc.stdout[-1000:], "检查 Cognee import 输出。") from exc
    result["logs_dir"] = probe_env["COGNEE_LOGS_DIR"]
    result["system_root_directory"] = probe_env["SYSTEM_ROOT_DIRECTORY"]
    result["data_root_directory"] = probe_env["DATA_ROOT_DIRECTORY"]
    result["cache_root_directory"] = probe_env["CACHE_ROOT_DIRECTORY"]
    result["claim_limits"] = [
        "runtime probe 只证明 Cognee package 可被项目 .venv 导入。",
        "不能说 Cognee 已完成 semantic graph 或 LLM 检索。",
    ]
    return result


def run_cognee_native_probe(root: Path, python: Path, *, timeout: int = 20) -> dict[str, Any]:
    runtime_root = root / ".recipes" / "memory" / "cognee" / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    probe_env = cognee_probe_env(runtime_root, native=True)
    tokenizer_cache = litellm_tokenizer_cache_dir(python)
    if tokenizer_cache:
        probe_env["TIKTOKEN_CACHE_DIR"] = str(tokenizer_cache)
        probe_env["CUSTOM_TIKTOKEN_CACHE_DIR"] = str(tokenizer_cache)
    env.update(probe_env)
    script = r'''
import asyncio, json, os, pathlib, socket, sys, traceback

payload = json.loads(sys.argv[1])
runtime_root = pathlib.Path(payload["runtime_root"]).resolve()
for key, value in payload["env"].items():
    os.environ[key] = value

network_attempts = []
original_connect = socket.socket.connect
original_create_connection = socket.create_connection

def blocked_connect(self, address):
    network_attempts.append(repr(address))
    raise RuntimeError(f"network disabled during Agent Recipes Cognee native probe: {address!r}")

def blocked_create_connection(address, *args, **kwargs):
    network_attempts.append(repr(address))
    raise RuntimeError(f"network disabled during Agent Recipes Cognee native probe: {address!r}")

socket.socket.connect = blocked_connect
socket.create_connection = blocked_create_connection

def under(path_value, root_value):
    try:
        pathlib.Path(path_value).resolve().relative_to(root_value)
        return True
    except Exception:
        return False

async def main():
    import cognee

    session_id = f"agent-recipes-cognee-native-probe-{os.getpid()}"
    remembered = await cognee.remember(
        "agent recipes native probe question\ncandidate-only native probe answer",
        dataset_name="agent_recipes_native_probe",
        session_id=session_id,
        self_improvement=False,
    )
    recalled = await cognee.recall(
        "candidate-only native probe",
        session_id=session_id,
        scope="session",
        top_k=3,
        auto_route=False,
    )
    return {
        "remember": remembered.to_dict() if hasattr(remembered, "to_dict") else {"status": str(remembered)},
        "recall_count": len(recalled or []),
        "recall_sources": [
            getattr(item, "source", None) or (item.get("source") if isinstance(item, dict) else None)
            for item in (recalled or [])
        ],
    }

env_paths = [
    os.environ["HOME"],
    os.environ["COGNEE_LOGS_DIR"],
    os.environ["DATA_ROOT_DIRECTORY"],
    os.environ["SYSTEM_ROOT_DIRECTORY"],
    os.environ["CACHE_ROOT_DIRECTORY"],
]
paths_caged = all(under(path, runtime_root) for path in env_paths)
try:
    outcome = asyncio.run(main())
    remember_status = (outcome.get("remember") or {}).get("status")
    native_ok = remember_status in {"session_stored", "completed"} and outcome.get("recall_count", 0) > 0 and not network_attempts
    result = {
        "native_status": "available" if native_ok else "unavailable",
        "runtime_verified": bool(native_ok),
        "adapter": "cognee",
        "mode": "native_session_probe_mock_embedding",
        "candidate_only": True,
        "remember_status": remember_status,
        "recall_count": outcome.get("recall_count", 0),
        "recall_sources": outcome.get("recall_sources", []),
    }
except Exception as exc:
    result = {
        "native_status": "unavailable",
        "runtime_verified": False,
        "adapter": "cognee",
        "mode": "native_session_probe_mock_embedding",
        "candidate_only": True,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback_tail": traceback.format_exc()[-2000:],
    }

result.update({
    "runtime_root": str(runtime_root),
    "paths_caged": paths_caged,
    "network_blocked": True,
    "network_attempts": network_attempts,
    "env": payload["public_env"],
    "claim_limits": [
        "native probe 使用 MOCK_EMBEDDING=true，只证明本地 session remember/recall 安全冒烟。",
        "不能证明真实 semantic graph、真实 embedding 质量或 LLM 检索质量。",
        "不能写正式 recipe，不能绕过 review_queue。",
    ],
})
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
socket.socket.connect = original_connect
socket.create_connection = original_create_connection
'''
    public_env = public_cognee_probe_env(probe_env)
    try:
        proc = subprocess.run(
            [
                str(python),
                "-c",
                script,
                json.dumps(
                    {"env": probe_env, "public_env": public_env, "runtime_root": str(runtime_root)},
                    ensure_ascii=False,
                ),
            ],
            cwd=runtime_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return cognee_native_probe_unavailable(
            root,
            timeout=timeout,
            reason="timeout",
            detail=(adapter_output_text(exc.stderr) + adapter_output_text(exc.stdout))[-2000:],
            probe_env=probe_env,
        )
    except OSError as exc:
        return cognee_native_probe_unavailable(
            root,
            timeout=timeout,
            reason="os_error",
            detail=str(exc),
            probe_env=probe_env,
        )
    if proc.returncode != 0:
        return cognee_native_probe_unavailable(
            root,
            timeout=timeout,
            reason="subprocess_failed",
            detail=(proc.stderr or proc.stdout)[-2000:],
            probe_env=probe_env,
        )
    try:
        result = parse_adapter_json(proc.stdout)
    except json.JSONDecodeError:
        return cognee_native_probe_unavailable(
            root,
            timeout=timeout,
            reason="invalid_json",
            detail=proc.stdout[-2000:],
            probe_env=probe_env,
        )
    stderr_warnings = []
    if "PermissionDeniedError" in proc.stderr:
        stderr_warnings.append(
            "Cognee logged a dataset permission warning during session probe; session recall still succeeded."
        )
    result["timeout_seconds"] = timeout
    result["stderr_warnings"] = stderr_warnings
    result["stdout_tail"] = proc.stdout[-1000:]
    result["stderr_tail"] = proc.stderr[-1000:]
    return result


def run_cognee_semantic_probe(root: Path, python: Path, *, timeout: int = 30) -> dict[str, Any]:
    semantic_config = read_cognee_semantic_runtime_config(root)
    runtime_root = cognee_semantic_runtime_root(root, semantic_config)
    runtime_root.mkdir(parents=True, exist_ok=True)
    probe_env = cognee_semantic_probe_env(runtime_root, semantic_config)
    tokenizer_cache = litellm_tokenizer_cache_dir(python)
    if tokenizer_cache:
        probe_env["TIKTOKEN_CACHE_DIR"] = str(tokenizer_cache)
        probe_env["CUSTOM_TIKTOKEN_CACHE_DIR"] = str(tokenizer_cache)
    dependency_status = cognee_semantic_dependency_status(python, probe_env)
    missing = cognee_semantic_missing_reasons(dependency_status)
    if missing:
        return cognee_semantic_probe_unavailable(
            root,
            timeout=timeout,
            reason="missing_local_semantic_runtime",
            detail="；".join(missing),
            probe_env=probe_env,
            dependency_status=dependency_status,
        )

    env = os.environ.copy()
    env.update(probe_env)
    script = r'''
import asyncio, json, os, pathlib, socket, sys, traceback

payload = json.loads(sys.argv[1])
runtime_root = pathlib.Path(payload["runtime_root"]).resolve()
for key, value in payload["env"].items():
    os.environ[key] = value

network_attempts = []
loopback_attempts = []
allowed_external_attempts = []
allow_loopback = bool(payload.get("allow_loopback"))
allowed_external_hosts = set(payload.get("allowed_external_hosts", []))
allowed_external_ips = set()
original_connect = socket.socket.connect
original_create_connection = socket.create_connection
original_getaddrinfo = socket.getaddrinfo

for allowed_host in allowed_external_hosts:
    try:
        for info in original_getaddrinfo(allowed_host, None):
            sockaddr = info[4]
            if sockaddr:
                allowed_external_ips.add(str(sockaddr[0]))
    except Exception as exc:
        network_attempts.append(f"getaddrinfo({allowed_host!r}) failed: {exc!r}")

def is_loopback_address(address):
    host = address[0] if isinstance(address, tuple) and address else address
    return str(host) in {"127.0.0.1", "localhost", "::1"}

def is_allowed_external_address(address):
    host = address[0] if isinstance(address, tuple) and address else address
    return str(host) in allowed_external_hosts or str(host) in allowed_external_ips

def blocked_connect(self, address):
    if allow_loopback and is_loopback_address(address):
        loopback_attempts.append(repr(address))
        return original_connect(self, address)
    if is_allowed_external_address(address):
        allowed_external_attempts.append(repr(address))
        return original_connect(self, address)
    network_attempts.append(repr(address))
    raise RuntimeError(f"external network disabled during Agent Recipes Cognee semantic probe: {address!r}")

def blocked_create_connection(address, *args, **kwargs):
    if allow_loopback and is_loopback_address(address):
        loopback_attempts.append(repr(address))
        return original_create_connection(address, *args, **kwargs)
    if is_allowed_external_address(address):
        allowed_external_attempts.append(repr(address))
        return original_create_connection(address, *args, **kwargs)
    network_attempts.append(repr(address))
    raise RuntimeError(f"external network disabled during Agent Recipes Cognee semantic probe: {address!r}")

socket.socket.connect = blocked_connect
socket.create_connection = blocked_create_connection

def under(path_value, root_value):
    try:
        pathlib.Path(path_value).resolve().relative_to(root_value)
        return True
    except Exception:
        return False

async def main():
    import cognee

    dataset_name = f"agent_recipes_semantic_probe_{os.getpid()}"
    remembered = await cognee.remember(
        "Agent Recipes semantic probe: memory candidates must stay candidate-only until review accept.",
        dataset_name=dataset_name,
        self_improvement=False,
    )
    recalled = await cognee.recall(
        "candidate-only review accept",
        datasets=[dataset_name],
        top_k=3,
        auto_route=False,
    )
    return {
        "remember": remembered.to_dict() if hasattr(remembered, "to_dict") else {"status": str(remembered)},
        "recall_count": len(recalled or []),
    }

env_paths = [
    os.environ["HOME"],
    os.environ["COGNEE_LOGS_DIR"],
    os.environ["DATA_ROOT_DIRECTORY"],
    os.environ["SYSTEM_ROOT_DIRECTORY"],
    os.environ["CACHE_ROOT_DIRECTORY"],
]
paths_caged = all(under(path, runtime_root) for path in env_paths)
try:
    outcome = asyncio.run(main())
    remember_status = (outcome.get("remember") or {}).get("status")
    semantic_ok = remember_status in {"completed", "stored", "accepted"} and outcome.get("recall_count", 0) > 0 and not network_attempts
    result = {
        "semantic_status": "available" if semantic_ok else "unavailable",
        "runtime_verified": bool(semantic_ok),
        "adapter": "cognee",
        "mode": "semantic_probe_real_embedding_preflight",
        "candidate_only": True,
        "remember_status": remember_status,
        "recall_count": outcome.get("recall_count", 0),
    }
except Exception as exc:
    result = {
        "semantic_status": "unavailable",
        "runtime_verified": False,
        "adapter": "cognee",
        "mode": "semantic_probe_real_embedding_preflight",
        "candidate_only": True,
        "error_type": type(exc).__name__,
        "error": str(exc),
        "traceback_tail": traceback.format_exc()[-2000:],
    }

result.update({
    "runtime_root": str(runtime_root),
    "paths_caged": paths_caged,
    "network_blocked": True,
    "network_attempts": network_attempts,
    "loopback_allowed": allow_loopback,
    "loopback_attempts": loopback_attempts,
    "external_network_allowlist": sorted(allowed_external_hosts),
    "allowed_external_attempts": allowed_external_attempts,
    "dependency_status": payload["dependency_status"],
    "env": payload["public_env"],
    "claim_limits": [
        "semantic probe 不使用 MOCK_EMBEDDING=true。",
        "不能证明生产级长期记忆、图谱质量或 LLM 检索质量。",
        "不能写正式 recipe，不能绕过 review_queue。",
    ],
})
print(json.dumps(result, ensure_ascii=False, sort_keys=True))
socket.socket.connect = original_connect
socket.create_connection = original_create_connection
socket.getaddrinfo = original_getaddrinfo
'''
    public_env = public_cognee_probe_env(probe_env)
    allowed_external_hosts = []
    if dependency_status.get("cloud_llm_options", {}).get("deepseek"):
        allowed_external_hosts.append("api.deepseek.com")
    try:
        proc = subprocess.run(
            [
                str(python),
                "-c",
                script,
                json.dumps(
                    {
                        "env": probe_env,
                        "public_env": public_env,
                        "runtime_root": str(runtime_root),
                        "dependency_status": dependency_status,
                        "allow_loopback": semantic_config.get("config_status") == "configured",
                        "allowed_external_hosts": allowed_external_hosts,
                    },
                    ensure_ascii=False,
                ),
            ],
            cwd=runtime_root,
            text=True,
            capture_output=True,
            check=False,
            env=env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return cognee_semantic_probe_unavailable(
            root,
            timeout=timeout,
            reason="timeout",
            detail=(adapter_output_text(exc.stderr) + adapter_output_text(exc.stdout))[-2000:],
            probe_env=probe_env,
            dependency_status=dependency_status,
        )
    except OSError as exc:
        return cognee_semantic_probe_unavailable(
            root,
            timeout=timeout,
            reason="os_error",
            detail=str(exc),
            probe_env=probe_env,
            dependency_status=dependency_status,
        )
    if proc.returncode != 0:
        return cognee_semantic_probe_unavailable(
            root,
            timeout=timeout,
            reason="subprocess_failed",
            detail=(proc.stderr or proc.stdout)[-2000:],
            probe_env=probe_env,
            dependency_status=dependency_status,
        )
    try:
        result = parse_adapter_json(proc.stdout)
    except json.JSONDecodeError:
        return cognee_semantic_probe_unavailable(
            root,
            timeout=timeout,
            reason="invalid_json",
            detail=proc.stdout[-2000:],
            probe_env=probe_env,
            dependency_status=dependency_status,
        )
    result["timeout_seconds"] = timeout
    result["stdout_tail"] = proc.stdout[-1000:]
    result["stderr_tail"] = proc.stderr[-1000:]
    return result


def cognee_native_probe_unavailable(
    root: Path,
    *,
    timeout: int,
    reason: str,
    detail: str,
    probe_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    runtime_root = root / ".recipes" / "memory" / "cognee" / "runtime"
    probe_env = probe_env or cognee_probe_env(runtime_root, native=True)
    return {
        "native_status": "unavailable",
        "runtime_verified": False,
        "adapter": "cognee",
        "mode": "native_session_probe_mock_embedding",
        "candidate_only": True,
        "runtime_root": str(runtime_root),
        "paths_caged": cognee_paths_caged(runtime_root, probe_env),
        "network_blocked": True,
        "network_attempts": [],
        "timeout_seconds": timeout,
        "error_type": reason,
        "error": detail,
        "env": public_cognee_probe_env(probe_env),
        "claim_limits": [
            "native probe 未通过，只能说明当前 Cognee 原生 remember/recall 不可用或未证明。",
            "不能证明真实 semantic graph、真实 embedding 质量或 LLM 检索质量。",
            "不能写正式 recipe，不能绕过 review_queue。",
        ],
    }


def cognee_semantic_probe_unavailable(
    root: Path,
    *,
    timeout: int,
    reason: str,
    detail: str,
    probe_env: dict[str, str] | None = None,
    dependency_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime_root = root / ".recipes" / "memory" / "cognee" / "runtime"
    runtime_root.mkdir(parents=True, exist_ok=True)
    probe_env = probe_env or cognee_semantic_probe_env(runtime_root, read_cognee_semantic_runtime_config(root))
    return {
        "semantic_status": "unavailable",
        "runtime_verified": False,
        "adapter": "cognee",
        "mode": "semantic_probe_real_embedding_preflight",
        "candidate_only": True,
        "runtime_root": str(runtime_root),
        "paths_caged": cognee_paths_caged(runtime_root, probe_env),
        "network_blocked": True,
        "network_attempts": [],
        "loopback_allowed": bool((dependency_status or {}).get("configured", {}).get("llm_endpoint_loopback") or (dependency_status or {}).get("configured", {}).get("embedding_endpoint_loopback")),
        "loopback_attempts": [],
        "timeout_seconds": timeout,
        "error_type": reason,
        "error": detail,
        "dependency_status": dependency_status or {},
        "env": public_cognee_probe_env(probe_env),
        "claim_limits": [
            "semantic probe 不使用 MOCK_EMBEDDING=true。",
            "当前没有证明生产级长期记忆、图谱质量或 LLM 检索质量。",
            "不能写正式 recipe，不能绕过 review_queue。",
        ],
    }


def cognee_paths_caged(runtime_root: Path, probe_env: dict[str, str]) -> bool:
    resolved_root = runtime_root.resolve()
    keys = ["HOME", "COGNEE_LOGS_DIR", "DATA_ROOT_DIRECTORY", "SYSTEM_ROOT_DIRECTORY", "CACHE_ROOT_DIRECTORY"]
    for key in keys:
        try:
            Path(probe_env[key]).resolve().relative_to(resolved_root)
        except Exception:
            return False
    return True


def public_cognee_probe_env(probe_env: dict[str, str]) -> dict[str, str]:
    keys = [
        "TELEMETRY_DISABLED",
        "ENV",
        "PYTHON_DOTENV_DISABLED",
        "MOCK_EMBEDDING",
        "CACHING",
        "CACHE_BACKEND",
        "AUTO_FEEDBACK",
        "USAGE_LOGGING",
        "COGNEE_TRACING_ENABLED",
        "ENABLE_BACKEND_ACCESS_CONTROL",
        "LITELLM_LOCAL_MODEL_COST_MAP",
        "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS",
        "COGNEE_SEMANTIC_PROBE",
        "AGENT_RECIPES_LLM_PROVIDER",
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_ENDPOINT",
        "LLM_API_KEY_ENV",
        "LLM_API_KEY_PRESENT",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_ENDPOINT",
        "TIKTOKEN_CACHE_DIR",
    ]
    return {key: probe_env[key] for key in keys if key in probe_env}


def litellm_tokenizer_cache_dir(python: Path) -> Path | None:
    venv_root = python.parent.parent
    lib_dir = venv_root / "lib"
    candidates = sorted(lib_dir.glob("python*/site-packages/litellm/litellm_core_utils/tokenizers"))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def cognee_probe_env(runtime_root: Path, *, native: bool = False) -> dict[str, str]:
    env = {
        "HOME": str(runtime_root / "home"),
        "COGNEE_LOGS_DIR": str(runtime_root / "logs"),
        "DATA_ROOT_DIRECTORY": str(runtime_root / "data"),
        "SYSTEM_ROOT_DIRECTORY": str(runtime_root / "system"),
        "CACHE_ROOT_DIRECTORY": str(runtime_root / "cache"),
        "ENABLE_BACKEND_ACCESS_CONTROL": "false",
        "CACHING": "false",
        "COGNEE_TRACING_ENABLED": "false",
        "TELEMETRY_DISABLED": "true",
        "ENV": "test",
        "PYTHON_DOTENV_DISABLED": "1",
        "LOG_LEVEL": "ERROR",
        "LITELLM_LOG": "ERROR",
        "LITELLM_SET_VERBOSE": "False",
    }
    if native:
        env.update(
            {
                "CACHING": "true",
                "CACHE_BACKEND": "sqlite",
                "AUTO_FEEDBACK": "false",
                "USAGE_LOGGING": "false",
                "MOCK_EMBEDDING": "true",
                "LITELLM_LOCAL_MODEL_COST_MAP": "true",
                "LITELLM_LOCAL_ANTHROPIC_BETA_HEADERS": "true",
                "DB_PROVIDER": "sqlite",
                "VECTOR_DB_PROVIDER": "lancedb",
                "OPENAI_API_KEY": "",
                "LLM_API_KEY": "",
                "EMBEDDING_API_KEY": "",
                "ANTHROPIC_API_KEY": "",
                "GEMINI_API_KEY": "",
                "MISTRAL_API_KEY": "",
            }
        )
    return env


def build_cognee_semantic_runtime_config(
    *,
    llm_provider: str | None,
    llm_model: str | None,
    llm_endpoint: str | None,
    llm_api_key_env: str | None,
    embedding_provider: str | None,
    embedding_model: str | None,
    embedding_endpoint: str | None,
    embedding_dimensions: int | str | None,
) -> dict[str, Any]:
    llm_provider = require_text(llm_provider, "LLM provider").lower()
    llm_model = require_text(llm_model, "LLM model")
    embedding_provider = require_text(embedding_provider, "embedding provider").lower()
    embedding_model = require_text(embedding_model, "embedding model")
    if llm_provider not in {"custom", "ollama", "llama_cpp", "deepseek"}:
        raise RecipesError("AR342", "semantic runtime 配置不安全。", f"LLM provider 必须是本地 provider 或 deepseek，收到：{llm_provider}", "使用 custom/ollama/llama_cpp/deepseek。")
    if embedding_provider not in {"openai_compatible", "ollama", "fastembed"}:
        raise RecipesError(
            "AR342",
            "semantic runtime 配置不安全。",
            f"embedding provider 必须是本地 provider，收到：{embedding_provider}",
            "使用 openai_compatible/ollama/fastembed。",
        )
    llm_endpoint_value = (llm_endpoint or "").strip()
    embedding_endpoint_value = (embedding_endpoint or "").strip()
    llm_api_key_env = (llm_api_key_env or "AGENT_RECIPES_DEEPSEEK_API_KEY").strip()
    deepseek_cloud = llm_provider == "deepseek"
    if deepseek_cloud:
        if not safe_env_name(llm_api_key_env):
            raise RecipesError("AR342", "semantic runtime API key env 不安全。", llm_api_key_env, "只传环境变量名，例如 AGENT_RECIPES_DEEPSEEK_API_KEY。")
        if llm_model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
            raise RecipesError("AR342", "DeepSeek semantic runtime model 不支持。", llm_model, "使用 deepseek-v4-flash 或 deepseek-v4-pro。")
        if not endpoint_is_deepseek_api(llm_endpoint_value):
            raise RecipesError("AR342", "DeepSeek semantic runtime endpoint 不在允许范围。", llm_endpoint_value, "使用 https://api.deepseek.com。")
    elif llm_provider in {"custom", "ollama"}:
        require_loopback_endpoint(llm_endpoint_value, "LLM endpoint")
    if embedding_provider in {"openai_compatible", "ollama"}:
        require_loopback_endpoint(embedding_endpoint_value, "embedding endpoint")
    try:
        dimensions = int(embedding_dimensions or 0)
    except (TypeError, ValueError) as exc:
        raise RecipesError("AR342", "semantic runtime 配置不完整。", "embedding dimensions 必须是正整数。", "传入 --embedding-dimensions。") from exc
    if dimensions <= 0:
        raise RecipesError("AR342", "semantic runtime 配置不完整。", "embedding dimensions 必须是正整数。", "传入 --embedding-dimensions。")
    config = {
        "adapter": "cognee",
        "config_status": "configured",
        "candidate_only": True,
        "updated_at": now_iso(),
        "llm": {
            "provider": llm_provider,
            "model": llm_model,
            "endpoint": llm_endpoint_value or None,
            "api_key_env": llm_api_key_env if deepseek_cloud else None,
            "api_key_present": bool(os.environ.get(llm_api_key_env)) if deepseek_cloud else None,
        },
        "embedding": {
            "provider": embedding_provider,
            "model": embedding_model,
            "endpoint": embedding_endpoint_value or None,
            "dimensions": dimensions,
        },
        "safety": {
            "loopback_only": not deepseek_cloud,
            "embedding_loopback_only": True,
            "cloud_blocked": not deepseek_cloud,
            "cloud_provider_allowlist": ["api.deepseek.com"] if deepseek_cloud else [],
            "secrets_written": False,
            "project_local_config": True,
        },
        "claim_limits": [
            "semantic runtime config 只说明连接参数已记录。",
            "不能证明本地模型服务已经启动。",
            "不能证明 DeepSeek API 已经可用或已调用。",
            "不能证明 Cognee 语义记忆、图谱质量或 LLM 检索质量。",
        ],
    }
    config["runtime_env"] = public_cognee_probe_env(cognee_semantic_runtime_env(config))
    return config


def detect_cognee_semantic_runtime(root: Path, python: Path | None) -> dict[str, Any]:
    detected = {
        "project_model_files": project_model_file_candidates(root),
        "llama_server_cache": llama_server_cache_list(),
        "project_python": str(python) if python else None,
    }
    return {
        "adapter": "cognee",
        "config_status": "not_configured",
        "candidate_only": True,
        "updated_at": now_iso(),
        "detected": detected,
        "safety": {
            "loopback_only": True,
            "cloud_blocked": True,
            "secrets_written": False,
            "project_local_config": True,
        },
        "claim_limits": [
            "detect-only 只记录本机线索，不代表 semantic runtime 已配置。",
            "不能证明本地模型服务已经启动。",
        ],
    }


def read_cognee_semantic_runtime_config(root: Path) -> dict[str, Any]:
    config = read_json(root / ".recipes" / "memory" / "cognee" / "semantic_runtime.json", {})
    if config.get("adapter") != "cognee":
        return {}
    return config


def semantic_runtime_config_hash(config: dict[str, Any]) -> str:
    stable = json.loads(json.dumps(config, ensure_ascii=False))
    stable.pop("updated_at", None)
    return sha256_json(stable)


def cognee_semantic_runtime_root(root: Path, semantic_config: dict[str, Any]) -> Path:
    config_hash = semantic_runtime_config_hash(semantic_config)[:16] if semantic_config else "unconfigured"
    return root / ".recipes" / "memory" / "cognee" / "runtime" / "semantic" / config_hash


def cognee_semantic_runtime_env(config: dict[str, Any]) -> dict[str, str]:
    if config.get("config_status") != "configured":
        return {}
    llm = config.get("llm") or {}
    embedding = config.get("embedding") or {}
    env: dict[str, str] = {}
    llm_provider = str(llm.get("provider") or "")
    llm_model = str(llm.get("model") or "")
    if llm_provider == "deepseek":
        env["AGENT_RECIPES_LLM_PROVIDER"] = "deepseek"
        env["LLM_PROVIDER"] = "custom"
        llm_model = cognee_deepseek_litellm_model(llm_model)
        api_key_env = str(llm.get("api_key_env") or "AGENT_RECIPES_DEEPSEEK_API_KEY")
        env["LLM_API_KEY_ENV"] = api_key_env
        api_key = os.environ.get(api_key_env, "")
        env["LLM_API_KEY_PRESENT"] = "true" if api_key else "false"
        if api_key:
            env["LLM_API_KEY"] = api_key
    elif llm_provider:
        env["LLM_PROVIDER"] = llm_provider
    if llm_model:
        env["LLM_MODEL"] = llm_model
    if llm.get("endpoint"):
        env["LLM_ENDPOINT"] = str(llm["endpoint"])
    if embedding.get("provider"):
        env["EMBEDDING_PROVIDER"] = str(embedding["provider"])
    if embedding.get("model"):
        env["EMBEDDING_MODEL"] = str(embedding["model"])
    if embedding.get("endpoint"):
        env["EMBEDDING_ENDPOINT"] = str(embedding["endpoint"])
    if embedding.get("dimensions"):
        env["EMBEDDING_DIMENSIONS"] = str(embedding["dimensions"])
    return env


def require_text(value: str | None, label: str) -> str:
    text = (value or "").strip()
    if not text:
        raise RecipesError("AR342", "semantic runtime 配置不完整。", f"{label} 不能为空。", "传入完整本地 runtime 参数。")
    return text


def require_loopback_endpoint(value: str, label: str) -> None:
    if not endpoint_is_loopback(value):
        raise RecipesError("AR342", "semantic runtime 配置不安全。", f"{label} must be loopback，本次收到：{value or '<empty>'}", "使用 http://127.0.0.1 或 http://localhost。")


def project_model_file_candidates(root: Path) -> list[str]:
    ignored = {".git", ".venv", ".recipes", "dist", "__pycache__"}
    candidates: list[str] = []
    for current, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in ignored]
        for filename in filenames:
            if filename.endswith((".gguf", ".safetensors")):
                candidates.append(str((Path(current) / filename).resolve()))
                if len(candidates) >= 50:
                    return candidates
    return candidates


def llama_server_cache_list() -> list[str]:
    if not shutil.which("llama-server"):
        return []
    proc = subprocess.run(
        ["llama-server", "--cache-list"],
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    text = proc.stdout if proc.returncode == 0 else proc.stderr
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            lines.append(stripped)
    return lines[:100]


def cognee_semantic_probe_env(runtime_root: Path, semantic_config: dict[str, Any] | None = None) -> dict[str, str]:
    env = cognee_probe_env(runtime_root, native=True)
    env["MOCK_EMBEDDING"] = "false"
    env["COGNEE_SEMANTIC_PROBE"] = "true"
    env.update(cognee_semantic_runtime_env(semantic_config or {}))
    for key in [
        "LLM_PROVIDER",
        "LLM_MODEL",
        "LLM_ENDPOINT",
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_ENDPOINT",
        "HUGGINGFACE_TOKENIZER",
        "LLAMA_CPP_MODEL_PATH",
    ]:
        value = os.environ.get(key)
        if value and key not in env:
            env[key] = value
    return env


def cognee_semantic_dependency_status(python: Path, semantic_env: dict[str, str] | None = None) -> dict[str, Any]:
    module_names = [
        "cognee",
        "lancedb",
        "fastembed",
        "ollama",
        "llama_cpp",
        "sentence_transformers",
        "transformers",
        "torch",
    ]
    semantic_env = semantic_env or os.environ
    modules = check_modules_with_python(python, module_names)
    agent_llm_provider = semantic_env.get("AGENT_RECIPES_LLM_PROVIDER", "").strip().lower()
    llm_provider = semantic_env.get("LLM_PROVIDER", "").strip().lower()
    embedding_provider = semantic_env.get("EMBEDDING_PROVIDER", "").strip().lower()
    llm_model = semantic_env.get("LLM_MODEL", "").strip()
    llm_endpoint = semantic_env.get("LLM_ENDPOINT", "").strip()
    llm_api_key_env = semantic_env.get("LLM_API_KEY_ENV", "").strip()
    llm_api_key_present = bool(semantic_env.get("LLM_API_KEY", "").strip())
    embedding_model = semantic_env.get("EMBEDDING_MODEL", "").strip()
    llama_cpp_model = semantic_env.get("LLAMA_CPP_MODEL_PATH", "").strip()
    embedding_dimensions = semantic_env.get("EMBEDDING_DIMENSIONS", "").strip()
    local_embeddings = {
        "fastembed": embedding_provider == "fastembed" and modules.get("fastembed") and bool(embedding_model),
        "ollama": embedding_provider == "ollama" and bool(shutil.which("ollama")) and bool(embedding_model),
        "openai_compatible_loopback": embedding_provider == "openai_compatible"
        and bool(embedding_model)
        and endpoint_is_loopback(semantic_env.get("EMBEDDING_ENDPOINT", "")),
    }
    local_llm = {
        "ollama": llm_provider == "ollama" and bool(shutil.which("ollama")) and bool(llm_model),
        "llama_cpp": llm_provider == "llama_cpp" and modules.get("llama_cpp") and bool(llama_cpp_model) and Path(llama_cpp_model).exists(),
        "custom_loopback": llm_provider == "custom" and bool(llm_model) and endpoint_is_loopback(semantic_env.get("LLM_ENDPOINT", "")),
    }
    cloud_llm = {
        "deepseek": agent_llm_provider == "deepseek"
        and llm_provider == "custom"
        and is_cognee_deepseek_litellm_model(llm_model)
        and endpoint_is_deepseek_api(llm_endpoint)
        and llm_api_key_present,
    }
    return {
        "python": str(python),
        "modules": modules,
        "binaries": {
            "ollama": shutil.which("ollama"),
            "llama-server": shutil.which("llama-server"),
        },
        "configured": {
            "agent_recipes_llm_provider": agent_llm_provider or None,
            "llm_provider": llm_provider or None,
            "llm_model": llm_model or None,
            "llm_endpoint_loopback": endpoint_is_loopback(semantic_env.get("LLM_ENDPOINT", "")),
            "llm_endpoint_deepseek_allowed": endpoint_is_deepseek_api(llm_endpoint),
            "llm_api_key_env": llm_api_key_env or None,
            "llm_api_key_present": llm_api_key_present,
            "embedding_provider": embedding_provider or None,
            "embedding_model": embedding_model or None,
            "embedding_dimensions_set": bool(embedding_dimensions),
            "embedding_endpoint_loopback": endpoint_is_loopback(semantic_env.get("EMBEDDING_ENDPOINT", "")),
            "llama_cpp_model_path_exists": bool(llama_cpp_model and Path(llama_cpp_model).exists()),
        },
        "local_embedding_ready": any(local_embeddings.values()),
        "local_llm_ready": any(local_llm.values()),
        "cloud_llm_ready": any(cloud_llm.values()),
        "local_embedding_options": local_embeddings,
        "local_llm_options": local_llm,
        "cloud_llm_options": cloud_llm,
    }


def cognee_semantic_missing_reasons(status: dict[str, Any]) -> list[str]:
    modules = status.get("modules", {})
    configured = status.get("configured", {})
    missing: list[str] = []
    if not modules.get("cognee"):
        missing.append("项目 .venv 里找不到 cognee。")
    if not modules.get("lancedb"):
        missing.append("项目 .venv 里找不到本地 vector store 依赖 lancedb。")
    if not status.get("local_embedding_ready"):
        missing.append(
            "没有可确认的本地 embedding 配置；需要 fastembed/ollama/openai_compatible loopback 之一，并明确 EMBEDDING_PROVIDER、EMBEDDING_MODEL、必要维度。"
        )
    if not status.get("local_llm_ready") and not status.get("cloud_llm_ready"):
        if configured.get("agent_recipes_llm_provider") == "deepseek" and not configured.get("llm_api_key_present"):
            missing.append(
                f"DeepSeek LLM 已配置，但环境变量 {configured.get('llm_api_key_env') or 'AGENT_RECIPES_DEEPSEEK_API_KEY'} 当前未设置。"
            )
        missing.append(
            "没有可确认的 LLM 配置；需要本地 ollama/llama_cpp/loopback custom，或明确允许的 DeepSeek 云 LLM。"
        )
    if configured.get("llm_provider") in {None, "openai"} and configured.get("agent_recipes_llm_provider") != "deepseek":
        missing.append("LLM_PROVIDER 没有改成本地 provider 或 DeepSeek 受控 provider，不能让 Cognee 回落到 OpenAI。")
    if configured.get("embedding_provider") in {None, "openai"}:
        missing.append("EMBEDDING_PROVIDER 没有改成本地 provider，不能让 Cognee 回落到 OpenAI。")
    return missing


def endpoint_is_loopback(value: str | None) -> bool:
    if not value:
        return False
    lowered = value.strip().lower()
    return lowered.startswith("http://127.0.0.1") or lowered.startswith("http://localhost") or lowered.startswith("http://[::1]")


def endpoint_is_deepseek_api(value: str | None) -> bool:
    if not value:
        return False
    try:
        parsed = urllib.parse.urlparse(value.strip())
    except Exception:
        return False
    return parsed.scheme == "https" and parsed.hostname == "api.deepseek.com"


DEEPSEEK_AGENT_RECIPES_MODELS = {"deepseek-v4-flash", "deepseek-v4-pro"}


def cognee_deepseek_litellm_model(model: str) -> str:
    model = str(model or "").strip()
    if model.startswith("openai/") or model.startswith("deepseek/"):
        return model
    return f"openai/{model}"


def is_cognee_deepseek_litellm_model(model: str) -> bool:
    model = str(model or "").strip()
    if model in DEEPSEEK_AGENT_RECIPES_MODELS:
        return True
    if model.startswith("openai/"):
        return model.removeprefix("openai/") in DEEPSEEK_AGENT_RECIPES_MODELS
    return model in {"deepseek/deepseek-chat", "deepseek/deepseek-reasoner"}


def memory_adapter_evidence(recipes_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    index_path = recipes_dir / "memory" / "cognee" / "index.jsonl"
    status_path = recipes_dir / "memory" / "cognee" / "status.json"
    runtime_path = recipes_dir / "memory" / "cognee" / "runtime_probe.json"
    native_path = recipes_dir / "memory" / "cognee" / "native_probe.json"
    semantic_path = recipes_dir / "memory" / "cognee" / "semantic_probe.json"
    semantic_config_path = recipes_dir / "memory" / "cognee" / "semantic_runtime.json"
    graphiti_dir = recipes_dir / "memory" / "graphiti"
    graphiti_nodes_path = graphiti_dir / "nodes.jsonl"
    graphiti_edges_path = graphiti_dir / "edges.jsonl"
    graphiti_status_path = graphiti_dir / "status.json"
    graphiti_native_path = graphiti_dir / "native_probe.json"
    records, _ = read_optional_jsonl(index_path)
    status, _ = read_optional_json(status_path, {})
    runtime, _ = read_optional_json(runtime_path, {})
    native, _ = read_optional_json(native_path, {})
    semantic, _ = read_optional_json(semantic_path, {})
    semantic_config, _ = read_optional_json(semantic_config_path, {})
    graphiti_nodes, _ = read_optional_jsonl(graphiti_nodes_path)
    graphiti_edges, _ = read_optional_jsonl(graphiti_edges_path)
    graphiti_status, _ = read_optional_json(graphiti_status_path, {})
    graphiti_native, _ = read_optional_json(graphiti_native_path, {})
    index_events = [
        event
        for event in events
        if event.get("event_type") == "memory_indexed"
        and event.get("payload", {}).get("adapter") == "cognee"
    ]
    search_events = [
        event
        for event in events
        if event.get("event_type") == "memory_searched"
        and event.get("payload", {}).get("adapter") == "cognee"
    ]
    native_events = [
        event
        for event in events
        if event.get("event_type") == "memory_native_probe_checked"
        and event.get("payload", {}).get("adapter") == "cognee"
    ]
    semantic_events = [
        event
        for event in events
        if event.get("event_type") == "memory_semantic_probe_checked"
        and event.get("payload", {}).get("adapter") == "cognee"
    ]
    semantic_config_events = [
        event
        for event in events
        if event.get("event_type") == "memory_semantic_runtime_configured"
        and event.get("payload", {}).get("adapter") == "cognee"
    ]
    graphiti_index_events = [
        event
        for event in events
        if event.get("event_type") == "memory_graph_indexed"
        and event.get("payload", {}).get("adapter") == "graphiti"
    ]
    graphiti_search_events = [
        event
        for event in events
        if event.get("event_type") == "memory_graph_searched"
        and event.get("payload", {}).get("adapter") == "graphiti"
    ]
    graphiti_native_events = [
        event
        for event in events
        if event.get("event_type") == "memory_native_probe_checked"
        and event.get("payload", {}).get("adapter") == "graphiti"
    ]
    return {
        "cognee": {
            "candidate_only": True,
            "runtime_verified": bool(status.get("runtime_verified") or runtime.get("runtime_verified") or index_events),
            "runtime_events": len(index_events),
            "search_events": len(search_events),
            "indexed_candidates": len(records),
            "index_path": str(index_path) if index_path.exists() else None,
            "status_path": str(status_path) if status_path.exists() else None,
            "runtime_probe_path": str(runtime_path) if runtime_path.exists() else None,
            "native_probe_path": str(native_path) if native_path.exists() else None,
            "semantic_probe_path": str(semantic_path) if semantic_path.exists() else None,
            "semantic_config_path": str(semantic_config_path) if semantic_config_path.exists() else None,
            "native_status": native.get("native_status") or (native_events[-1].get("payload", {}).get("native_status") if native_events else "not_checked"),
            "native_runtime_verified": bool(native.get("runtime_verified")),
            "native_warnings": native.get("stderr_warnings", []),
            "native_probe_events": len(native_events),
            "native_probe_candidate_only": True,
            "semantic_status": semantic.get("semantic_status") or (semantic_events[-1].get("payload", {}).get("semantic_status") if semantic_events else "not_checked"),
            "semantic_runtime_verified": bool(semantic.get("runtime_verified")),
            "semantic_probe_events": len(semantic_events),
            "semantic_probe_candidate_only": True,
            "semantic_missing": semantic.get("error") if semantic.get("semantic_status") == "unavailable" else None,
            "semantic_config_status": semantic_config.get("config_status") or (semantic_config_events[-1].get("payload", {}).get("config_status") if semantic_config_events else "not_checked"),
            "semantic_config_events": len(semantic_config_events),
            "semantic_config_candidate_only": True,
            "runtime_version": runtime.get("version"),
            "has_activity": bool(records or index_events or search_events or native_events or native or semantic_events or semantic or semantic_config_events or semantic_config),
            "notes": [
                "Cognee memory outputs are evidence candidates only.",
                "v0 uses an explicit runtime probe plus Agent Recipes local candidate index.",
                "native probe uses a caged subprocess with telemetry disabled and mock embedding.",
                "semantic probe keeps mock embedding disabled and fails closed unless local embedding plus local LLM or allowed DeepSeek cloud LLM are configured.",
                "semantic runtime config only records provider settings; it does not prove model/API service quality.",
                "not a verified semantic graph or formal recipe write path",
            ],
        },
        "graphiti": {
            "adapter": "graphiti",
            "candidate_only": True,
            "runtime_verified": bool(graphiti_status.get("runtime_verified") or graphiti_index_events),
            "runtime_events": len(graphiti_index_events),
            "search_events": len(graphiti_search_events),
            "node_count": len(graphiti_nodes),
            "edge_count": len(graphiti_edges),
            "nodes_path": str(graphiti_nodes_path) if graphiti_nodes_path.exists() else None,
            "edges_path": str(graphiti_edges_path) if graphiti_edges_path.exists() else None,
            "status_path": str(graphiti_status_path) if graphiti_status_path.exists() else None,
            "native_probe_path": str(graphiti_native_path) if graphiti_native_path.exists() else None,
            "native_runtime_verified": bool(graphiti_native.get("runtime_verified") or graphiti_status.get("native_runtime_verified")),
            "native_status": graphiti_native.get("native_status")
            or graphiti_status.get("native_status")
            or (graphiti_native_events[-1].get("payload", {}).get("native_status") if graphiti_native_events else "not_used" if graphiti_status else "not_checked"),
            "native_probe_events": len(graphiti_native_events),
            "native_probe_candidate_only": True,
            "native_warnings": graphiti_native.get("warnings", []) + graphiti_native.get("stderr_warnings", []),
            "has_activity": bool(graphiti_nodes or graphiti_edges or graphiti_index_events or graphiti_search_events or graphiti_status or graphiti_native or graphiti_native_events),
            "notes": [
                "Graphiti local v0 outputs are evidence relationship candidates only.",
                "v0 builds Agent Recipes local nodes/edges from source_refinery evidence.",
                "native Graphiti probe uses project-local Kuzu plus local stub clients when checked.",
                "not a formal recipe write path",
            ],
        }
    }


def external_adapter_runtime_evidence(recipes_dir: Path, events: list[dict[str, Any]]) -> dict[str, Any]:
    adapters = {
        "markitdown": {"event_types": {"external_doc_converted"}},
        "docling": {"event_types": {"external_doc_converted"}},
        "faster-whisper": {"event_types": {"external_transcript_created"}},
        "whisperx": {"event_types": {"external_transcript_created"}},
        "pyscenedetect": {"event_types": {"external_scenes_detected"}},
        "paddleocr": {"event_types": {"external_ocr_created"}},
        "surya": {"event_types": {"external_ocr_created"}},
        "cognee": {"event_types": {"memory_indexed"}},
        "graphiti": {"event_types": {"memory_graph_indexed", "memory_native_probe_checked"}},
        "zep": {"event_types": set()},
    }
    result: dict[str, Any] = {}
    runtime_verified: list[str] = []
    for adapter_name, config in adapters.items():
        matching = [
            event
            for event in events
            if event.get("event_type") in config["event_types"]
            and event.get("payload", {}).get("adapter") == adapter_name
        ]
        notes = ["adapter output is candidate only"]
        if adapter_name == "cognee" and matching:
            notes.append("memory candidate index runtime receipt exists")
        if adapter_name == "graphiti" and matching:
            if any(event.get("event_type") == "memory_graph_indexed" for event in matching):
                notes.append("local graph candidate runtime receipt exists")
            if any(event.get("event_type") == "memory_native_probe_checked" for event in matching):
                notes.append("native Graphiti probe receipt exists")
        if adapter_name == "zep":
            notes.append("用户已明确废弃 Zep 运行闭环。")
        if adapter_name in {"cognee", "graphiti"} and not matching:
            notes.append("dependency status only; no runtime integration command in Phase 2 local v0")
        if matching:
            runtime_verified.append(adapter_name)
        result[adapter_name] = {
            "runtime_verified": bool(matching),
            "runtime_events": len(matching),
            "candidate_only": True,
            "out_of_scope": adapter_name == "zep",
            "notes": notes,
        }
    normalized = recipes_dir / "source_refinery" / "normalized"
    result["runtime_verified_adapters"] = sorted(runtime_verified)
    result["normalized_outputs"] = {
        "markdown": len(list((normalized / "markdown").glob("*.md"))) if normalized.exists() else 0,
        "transcripts": len(list((normalized / "transcripts").glob("*.json"))) if normalized.exists() else 0,
        "scenes": len(list((normalized / "keyframes").glob("scenes_*.json"))) if normalized.exists() else 0,
        "ocr": len(list((normalized / "ocr").glob("ocr_*.json"))) if normalized.exists() else 0,
    }
    return result


def adapter_runtime_lock_evidence(recipes_dir: Path) -> dict[str, Any]:
    report = read_json(recipes_dir / "reports" / "adapter_runtime_lock.json", {})
    if not report:
        return {"present": False}
    return {
        "present": True,
        "lock_path": report.get("lock_path"),
        "lock_hash": report.get("lock_hash"),
        "package_count": report.get("package_count"),
        "missing_direct_requirements": report.get("missing_direct_requirements", []),
    }


def system_runtime_lock_evidence(recipes_dir: Path) -> dict[str, Any]:
    report = read_json(recipes_dir / "reports" / "system_runtime_lock.json", {})
    if not report:
        return {"present": False}
    binaries = report.get("binaries") or []
    present_binaries = [item.get("name") for item in binaries if item.get("present")]
    return {
        "present": True,
        "lock_hash": report.get("lock_hash"),
        "present_binaries": present_binaries,
        "missing_required": report.get("missing_required", []),
        "missing_optional": report.get("missing_optional", []),
    }


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def check_modules_with_python(python: Path, names: list[str]) -> dict[str, bool]:
    script = (
        "import importlib.util, json, sys; "
        "names=json.loads(sys.argv[1]); "
        "print(json.dumps({n: importlib.util.find_spec(n) is not None for n in names}, sort_keys=True))"
    )
    proc = subprocess.run(
        [str(python), "-c", script, json.dumps(names)],
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        return {name: False for name in names}
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {name: False for name in names}


def project_python_executables(python: Path | None, names: list[str]) -> dict[str, str | None]:
    if not python:
        return {name: None for name in names}
    bin_dir = python.parent
    result: dict[str, str | None] = {}
    for name in names:
        candidate = bin_dir / name
        result[name] = str(candidate) if candidate.exists() and os.access(candidate, os.X_OK) else None
    return result


def resolve_existing_file(root: Path, value: str, code: str, problem: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.exists() or not path.is_file():
        raise RecipesError(code, problem, f"找不到文件：{path}", "传入真实存在的本地文件。")
    return path


def run_adapter_json(
    python: Path,
    script: str,
    payload: dict[str, Any],
    *,
    error_code: str,
    problem: str,
    timeout: int = 300,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [str(python), "-c", script, json.dumps(payload, ensure_ascii=False)],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        output = (adapter_output_text(exc.stderr) + adapter_output_text(exc.stdout))[-2000:]
        raise RecipesError(error_code, problem, f"adapter timeout after {timeout}s. {output}", "检查 adapter 输入、模型下载或本机依赖。") from exc
    except OSError as exc:
        raise RecipesError(error_code, problem, str(exc), "运行 capabilities 查看 adapter runtime 和 .venv/bin/python。") from exc
    if proc.returncode != 0:
        raise RecipesError(error_code, problem, (proc.stderr or proc.stdout)[-2000:], "运行 capabilities 查看 adapter 依赖。")
    try:
        return parse_adapter_json(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RecipesError(error_code, problem, f"adapter 没有返回合法 JSON：{proc.stdout[-1000:]}", "检查 adapter 输出。") from exc


def parse_adapter_json(stdout: str) -> dict[str, Any]:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        for line in reversed(stdout.splitlines()):
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                return json.loads(stripped)
        raise


def adapter_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def adapter_script(kind: str, adapter: str) -> str:
    if kind == "convert_doc" and adapter == "markitdown":
        return r'''
import contextlib, json, sys
payload=json.loads(sys.argv[1])
with contextlib.redirect_stdout(sys.stderr):
    from markitdown import MarkItDown
    result=MarkItDown().convert(payload["path"])
print(json.dumps({"markdown": result.text_content}, ensure_ascii=False))
'''
    if kind == "convert_doc" and adapter == "docling":
        return r'''
import contextlib, json, sys
payload=json.loads(sys.argv[1])
with contextlib.redirect_stdout(sys.stderr):
    from docling.document_converter import DocumentConverter
    result=DocumentConverter().convert(payload["path"])
print(json.dumps({"markdown": result.document.export_to_markdown()}, ensure_ascii=False))
'''
    if kind == "detect_scenes" and adapter == "pyscenedetect":
        return r'''
import contextlib, json, sys
payload=json.loads(sys.argv[1])
scenes=[]
with contextlib.redirect_stdout(sys.stderr):
    from scenedetect import ContentDetector, detect
    for start, end in detect(payload["path"], ContentDetector()):
        scenes.append({
            "start": start.get_timecode(),
            "end": end.get_timecode(),
            "start_seconds": start.get_seconds(),
            "end_seconds": end.get_seconds(),
        })
print(json.dumps({"scenes": scenes}, ensure_ascii=False))
'''
    if kind == "transcribe" and adapter == "faster-whisper":
        return r'''
import contextlib, json, sys
payload=json.loads(sys.argv[1])
rows=[]
texts=[]
with contextlib.redirect_stdout(sys.stderr):
    from faster_whisper import WhisperModel
    model=WhisperModel(payload.get("model") or "tiny.en", device="cpu", compute_type="int8")
    segments, info = model.transcribe(payload["path"], beam_size=1)
    for segment in segments:
        text=segment.text.strip()
        rows.append({"start": float(segment.start), "end": float(segment.end), "text": text})
        texts.append(text)
print(json.dumps({"language": getattr(info, "language", None), "text": " ".join(texts).strip(), "segments": rows}, ensure_ascii=False))
'''
    if kind == "transcribe" and adapter == "whisperx":
        return r'''
import json, subprocess, sys, tempfile
from pathlib import Path
payload=json.loads(sys.argv[1])
exe=Path(sys.executable).with_name("whisperx")
if not exe.exists():
    print(f"whisperx not found beside {sys.executable}", file=sys.stderr)
    sys.exit(2)
out_dir=Path(tempfile.mkdtemp(prefix="agent_recipes_whisperx_"))
cmd=[
    str(exe),
    payload["path"],
    "--model", payload.get("model") or "tiny.en",
    "--device", "cpu",
    "--compute_type", "int8",
    "--language", "en",
    "--no_align",
    "--output_dir", str(out_dir),
    "--output_format", "json",
    "--verbose", "False",
]
proc=subprocess.run(cmd, text=True, capture_output=True, check=False, timeout=900)
if proc.returncode != 0:
    print((proc.stderr or proc.stdout)[-2000:], file=sys.stderr)
    sys.exit(proc.returncode)
json_files=list(out_dir.glob("*.json"))
if not json_files:
    print(f"whisperx did not write json under {out_dir}", file=sys.stderr)
    sys.exit(3)
data=json.loads(json_files[0].read_text(encoding="utf-8"))
rows=[]
texts=[]
for segment in data.get("segments", []):
    text=str(segment.get("text", "")).strip()
    rows.append({"start": float(segment.get("start", 0.0)), "end": float(segment.get("end", 0.0)), "text": text})
    if text:
        texts.append(text)
print(json.dumps({"language": data.get("language"), "text": " ".join(texts).strip(), "segments": rows}, ensure_ascii=False))
'''
    if kind == "ocr_image" and adapter == "paddleocr":
        return r'''
import contextlib, json, sys
payload=json.loads(sys.argv[1])
with contextlib.redirect_stdout(sys.stderr):
    from paddleocr import PaddleOCR
    ocr=PaddleOCR(lang="en", use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False)
    result=ocr.predict(payload["path"])
texts=[]
blocks=[]
for item in result:
    data=None
    if hasattr(item, "json"):
        data=item.json
    elif hasattr(item, "to_json"):
        data=item.to_json()
    else:
        data=item
    if isinstance(data, str):
        data=json.loads(data)
    res=data.get("res", data) if isinstance(data, dict) else {}
    item_texts=[str(value).strip() for value in res.get("rec_texts", []) if str(value).strip()]
    texts.extend(item_texts)
    blocks.append({
        "texts": item_texts,
        "scores": res.get("rec_scores", []),
        "boxes": res.get("rec_boxes", []),
    })
print(json.dumps({"text": "\n".join(texts).strip(), "texts": texts, "blocks": blocks}, ensure_ascii=False))
'''
    if kind == "ocr_image" and adapter == "surya":
        return r'''
import html, json, re, subprocess, sys, tempfile
from pathlib import Path
payload=json.loads(sys.argv[1])
exe=Path(sys.executable).with_name("surya_ocr")
if not exe.exists():
    print(f"surya_ocr not found beside {sys.executable}", file=sys.stderr)
    sys.exit(2)
out_dir=Path(tempfile.mkdtemp(prefix="agent_recipes_surya_"))
proc=subprocess.run([str(exe), payload["path"], "--output_dir", str(out_dir)], text=True, capture_output=True, check=False, timeout=900)
if proc.returncode != 0:
    print((proc.stderr or proc.stdout)[-2000:], file=sys.stderr)
    sys.exit(proc.returncode)
json_files=list(out_dir.rglob("results.json"))
if not json_files:
    print(f"surya did not write results.json under {out_dir}", file=sys.stderr)
    sys.exit(3)
data=json.loads(json_files[0].read_text(encoding="utf-8"))
texts=[]
blocks=[]
def strip_html(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(value or ""))).strip()
for pages in data.values():
    for page in pages:
        for block in page.get("blocks", []):
            block_text=strip_html(block.get("html", ""))
            if block_text:
                texts.append(block_text)
            blocks.append({
                "text": block_text,
                "label": block.get("label"),
                "confidence": block.get("confidence"),
                "bbox": block.get("bbox"),
                "skipped": block.get("skipped"),
                "error": block.get("error"),
            })
print(json.dumps({"text": "\n".join(texts).strip(), "texts": texts, "blocks": blocks}, ensure_ascii=False))
'''
    raise RecipesError("AR301", "未知 adapter script。", f"{kind}:{adapter}", "检查 adapter 参数。")


def seconds_to_vtt(value: float) -> str:
    hours = int(value // 3600)
    minutes = int((value % 3600) // 60)
    seconds = value % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"


def segments_as_vtt(segments: list[dict[str, Any]]) -> str:
    lines = ["WEBVTT", ""]
    for index, segment in enumerate(segments, start=1):
        lines.append(str(index))
        lines.append(f"{seconds_to_vtt(float(segment['start']))} --> {seconds_to_vtt(float(segment['end']))}")
        lines.append(segment.get("text", ""))
        lines.append("")
    return "\n".join(lines)


def rank_evidence_candidates(
    query: str,
    records: list[dict[str, Any]],
    *,
    priority_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    tokens = lookup_query_terms(query)
    if not tokens:
        tokens = [token for token in re.split(r"\s+", normalize_problem(query)) if token]
    ranked: list[dict[str, Any]] = []
    for record in records:
        text = normalize_problem(record.get("text", ""))
        path = normalize_problem(str(record.get("path") or ""))
        filename = normalize_problem(Path(str(record.get("path") or "")).name)
        score = 0
        matched_terms: list[str] = []
        for token in tokens:
            alternatives = lookup_term_alternatives(token)
            if token and any(alternative in text for alternative in alternatives):
                score += 2
                matched_terms.append(token)
            if token and any(alternative in path for alternative in alternatives):
                score += 1
            if token and any(alternative in filename for alternative in alternatives):
                score += 3
        if normalize_problem(query) and normalize_problem(query) in text:
            score += 5
        recipe_id = str(record.get("target_recipe_id") or record.get("recipe_id") or "")
        priority_bonus, applied_rules = lookup_priority_bonus_for_recipe(
            recipe_id=recipe_id,
            query=query,
            base_score=score,
            priority_rules=priority_rules or [],
        )
        if score or priority_bonus:
            candidate = dict(record)
            candidate["score"] = score + priority_bonus
            candidate["base_score"] = score
            candidate["priority_bonus"] = priority_bonus
            candidate["priority_rules_applied"] = applied_rules
            candidate["matched_terms"] = matched_terms
            ranked.append(candidate)
    ranked.sort(
        key=lambda item: (item["score"], item.get("base_score", item["score"]), item.get("record_id") or ""),
        reverse=True,
    )
    return ranked


def rank_refinement_records(query: str, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    scored = {item["record_id"]: item for item in rank_evidence_candidates(query, records)}
    ranked: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        item["score"] = scored.get(record.get("record_id"), {}).get("score", 0)
        ranked.append(item)
    ranked.sort(key=lambda item: (item["score"], item.get("record_id") or ""), reverse=True)
    return ranked


def normalize_source_path_filters(source_path_contains: list[str] | None) -> list[str]:
    if not source_path_contains:
        return []
    return unique_text([normalize_problem(str(item)) for item in source_path_contains if str(item).strip()])


def filter_records_by_source_path(records: list[dict[str, Any]], filters: list[str]) -> list[dict[str, Any]]:
    if not filters:
        return records
    filtered: list[dict[str, Any]] = []
    for record in records:
        path = normalize_problem(str(record.get("path") or ""))
        filename = normalize_problem(Path(str(record.get("path") or "")).name)
        if any(marker in path or marker in filename for marker in filters):
            filtered.append(record)
    return filtered


def quality_result_text(record: dict[str, Any]) -> str:
    parts = [
        record.get("text"),
        record.get("title"),
        record.get("label"),
        record.get("record_id"),
        record.get("source_kind"),
        record.get("evidence_status"),
        record.get("evidence_strength"),
        record.get("target_recipe_id"),
        record.get("source_patch_draft_id"),
        " ".join(str(field) for field in record.get("target_fields", []) or []),
    ]
    return " ".join(str(part) for part in parts if part)


def quality_top_result(results: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not results:
        return None
    top = results[0]
    return {
        "record_id": top.get("record_id"),
        "record_type": top.get("record_type") or top.get("result_type") or top.get("source_kind"),
        "score": top.get("score"),
        "text": quality_result_text(top)[:500],
    }


def quality_rank_case(
    case_id: str,
    adapter: str,
    query: str,
    records: list[dict[str, Any]],
    expected_terms: list[str],
    *,
    limit: int,
) -> dict[str, Any]:
    if not records:
        return {
            "case_id": case_id,
            "adapter": adapter,
            "status": "blocked",
            "passed": False,
            "query": query,
            "expected_terms": expected_terms,
            "matched_terms": [],
            "missing_evidence": [f"{adapter} candidate index is empty."],
            "cannot_claim": [f"不能说 {adapter} 搜索质量已验证。"],
        }
    ranked = rank_evidence_candidates(query, records)[:limit]
    top_text = " ".join(quality_result_text(item) for item in ranked[:1]).casefold()
    matched = [term for term in expected_terms if term.casefold() in top_text]
    passed = bool(ranked) and len(matched) == len(expected_terms)
    return {
        "case_id": case_id,
        "adapter": adapter,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "query": query,
        "expected_terms": expected_terms,
        "matched_terms": matched,
        "result_count": len(ranked),
        "top_result": quality_top_result(ranked),
        "cannot_claim": [f"不能说 {adapter} 命中结果已经验证为真。"],
    }


def quality_false_recall_case(
    case_id: str,
    adapter: str,
    query: str,
    records: list[dict[str, Any]],
    *,
    limit: int,
) -> dict[str, Any]:
    ranked = rank_evidence_candidates(query, records)[:limit]
    passed = not ranked
    return {
        "case_id": case_id,
        "adapter": adapter,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "query": query,
        "expected_no_results": True,
        "result_count": len(ranked),
        "top_result": quality_top_result(ranked),
        "cannot_claim": ["不能说一次无命中就证明没有误召回风险。"],
    }


def quality_review_flow_case(recipes_dir: Path) -> dict[str, Any]:
    reviews = [read_json(path, {}) for path in sorted((recipes_dir / "review_queue").glob("*.json"))]
    source_reviews = [review for review in reviews if review.get("source_patch_draft_id")]
    accepted = [review for review in source_reviews if review.get("status") == "accepted" and review.get("recipe_id")]
    missing_recipes = [
        review.get("recipe_id")
        for review in accepted
        if review.get("recipe_id") and not recipe_exists(recipes_dir, str(review.get("recipe_id")))
    ]
    recover_reviews = [review for review in reviews if "recover" in str(review.get("question", "")).casefold()]
    passed = bool(accepted) and not missing_recipes
    return {
        "case_id": "review_gate_patch_to_recipe",
        "adapter": "review_queue",
        "status": "passed" if passed else "blocked",
        "passed": passed,
        "accepted_source_patch_reviews": len(accepted),
        "pending_recover_reviews": sum(1 for review in recover_reviews if review.get("status") == "pending"),
        "missing_recipes_after_accept": missing_recipes,
        "missing_evidence": [] if passed else ["No accepted source_refinery patch review with a formal recipe was found."],
        "cannot_claim": ["不能说 review accept 后的 recipe 已经在真实任务中跑赢。"],
    }


def quality_missing_evidence(cases: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for case in cases:
        if case.get("status") in {"blocked", "skipped", "failed"}:
            missing.extend(str(item) for item in case.get("missing_evidence", []) if item)
            if case.get("status") == "failed":
                missing.append(f"quality case failed: {case.get('case_id')}")
    return unique_text(missing)


def benchmark_case(case_id: str, status: str, detail: str, missing_evidence: list[str] | None = None) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "status": status,
        "passed": status in {"passed", "improved"},
        "detail": detail,
        "missing_evidence": missing_evidence or [],
        "cannot_claim": ["不能说 benchmark case 通过就证明生产级质量。"],
    }


def benchmark_missing_evidence(cases: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for case in cases:
        if case.get("status") in {"blocked", "failed", "skipped"}:
            missing.append(f"benchmark case not passed: {case.get('case_id')}")
            missing.extend(str(item) for item in case.get("missing_evidence", []) if item)
            missing.extend(str(item) for item in case.get("failure_reasons", []) if item)
    return unique_text(missing)


def read_card_by_id(recipes_dir: Path, card_id: str) -> dict[str, Any]:
    for path in (recipes_dir / "source_refinery" / "cards").glob(f"*/{card_id}.json"):
        return read_json(path, {})
    for card in read_jsonl(recipes_dir / "source_refinery" / "cards" / "cards.jsonl"):
        if str(card.get("card_id") or "") == card_id:
            return card
    return {}


def self_run_benchmark_cases(
    *,
    scan_result: dict[str, Any],
    search_result: dict[str, Any],
    refined: dict[str, Any],
    cards: list[dict[str, Any]],
    card_counts: dict[str, Any],
    patch_draft: dict[str, Any],
    review: dict[str, Any],
    min_cards: int,
    formal_existed_before: bool,
    formal_exists_after: bool,
) -> list[dict[str, Any]]:
    card_count = len([card for card in cards if card])
    proposed_value_count = int((patch_draft.get("review_hints") or {}).get("proposed_value_count") or 0)
    schema_missing: list[str] = []
    for card in cards:
        card_id = str(card.get("card_id") or "unknown_card")
        for field in ["source_trace", "target_fields", "evidence_strength", "cannot_claim"]:
            if not card.get(field):
                schema_missing.append(f"{card_id} missing {field}")
    no_direct_write = (formal_existed_before and formal_exists_after) or (not formal_existed_before and not formal_exists_after)
    cases = [
        benchmark_case(
            "scan_registered_sources",
            "passed" if int(scan_result.get("chunks_indexed") or 0) > 0 else "failed",
            f"chunks_indexed={scan_result.get('chunks_indexed')}",
            [] if int(scan_result.get("chunks_indexed") or 0) > 0 else ["scan 没有生成 source chunks。"],
        ),
        benchmark_case(
            "search_candidate_sources",
            "passed" if search_result.get("results") else "failed",
            f"result_count={len(search_result.get('results') or [])}",
            [] if search_result.get("results") else ["search 没有返回候选资料。"],
        ),
        benchmark_case(
            "refine_mapped_chunks",
            "passed" if int(refined.get("mapped_count") or 0) > 0 else "failed",
            f"mapped_count={refined.get('mapped_count')}; archive_index_only_count={refined.get('archive_index_only_count')}",
            [] if int(refined.get("mapped_count") or 0) > 0 else ["refine 没有把 chunk 映射成 candidate fields。"],
        ),
        benchmark_case(
            "extract_cards_minimum",
            "passed" if card_count >= min_cards else "failed",
            f"card_count={card_count}; min_cards={min_cards}; card_counts={card_counts}",
            [] if card_count >= min_cards else [f"cards 不足：{card_count} / {min_cards}"],
        ),
        benchmark_case(
            "cards_have_source_trace_and_claim_limits",
            "passed" if card_count > 0 and not schema_missing else "failed",
            f"checked_cards={card_count}",
            schema_missing,
        ),
        benchmark_case(
            "patch_draft_created",
            "passed" if patch_draft.get("status") == "pending_review" and patch_draft.get("needs_user_review") else "failed",
            f"patch_draft_id={patch_draft.get('patch_draft_id')}; status={patch_draft.get('status')}",
            [] if patch_draft.get("status") == "pending_review" else ["没有 pending_review patch draft。"],
        ),
        benchmark_case(
            "patch_draft_has_candidate_values",
            "passed" if proposed_value_count > 0 else "failed",
            f"proposed_value_count={proposed_value_count}",
            [] if proposed_value_count > 0 else ["patch draft 没有任何可审核的候选内容。"],
        ),
        benchmark_case(
            "review_queue_pending",
            "passed" if review.get("status") == "pending" and review.get("source_patch_draft_id") else "failed",
            f"review_id={review.get('review_id')}; status={review.get('status')}",
            [] if review.get("status") == "pending" else ["patch draft 没有停在 pending review_queue。"],
        ),
        benchmark_case(
            "no_direct_formal_recipe_write",
            "passed" if no_direct_write else "failed",
            f"formal_existed_before={formal_existed_before}; formal_exists_after={formal_exists_after}",
            [] if no_direct_write else ["self-run 过程中出现未经过 review accept 的正式 recipe 写入。"],
        ),
    ]
    return cases


def repeat_error_case(raw_case: Any) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        return {
            "case_id": "invalid_case",
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["repeat-error case must be an object."],
        }
    case_id = str(raw_case.get("case_id") or raw_case.get("id") or "unnamed_case")
    without_text = repeat_case_text(raw_case, "without_recipe")
    with_text = repeat_case_text(raw_case, "with_recipe")
    error_terms = [str(term) for term in raw_case.get("error_terms", []) if str(term).strip()]
    improvement_terms = [str(term) for term in raw_case.get("improvement_terms", []) if str(term).strip()]
    if not without_text or not with_text or not error_terms or not improvement_terms:
        return {
            "case_id": case_id,
            "status": "blocked",
            "passed": False,
            "old_error": raw_case.get("old_error"),
            "missing_evidence": ["case needs without_recipe_output, with_recipe_output, error_terms, and improvement_terms."],
            "cannot_claim": ["不能说该旧错已经被对照验证。"],
        }
    without_lower = without_text.casefold()
    with_lower = with_text.casefold()
    baseline_error_terms = [term for term in error_terms if term.casefold() in without_lower]
    remaining_error_terms = [term for term in error_terms if term.casefold() in with_lower]
    matched_improvement_terms = [term for term in improvement_terms if term.casefold() in with_lower]
    min_improvement_terms = int(raw_case.get("min_improvement_terms") or len(improvement_terms))
    failure_reasons: list[str] = []
    if not baseline_error_terms:
        failure_reasons.append("without_recipe output did not show the old error terms.")
    if len(remaining_error_terms) >= len(baseline_error_terms):
        failure_reasons.append("with_recipe output did not reduce old error terms.")
    if len(matched_improvement_terms) < min_improvement_terms:
        failure_reasons.append(f"with_recipe output missed improvement terms: {len(matched_improvement_terms)} / {min_improvement_terms}")
    passed = not failure_reasons
    return {
        "case_id": case_id,
        "status": "improved" if passed else "failed",
        "passed": passed,
        "old_error": raw_case.get("old_error"),
        "baseline_error_terms": baseline_error_terms,
        "remaining_error_terms": remaining_error_terms,
        "improvement_terms": improvement_terms,
        "matched_improvement_terms": matched_improvement_terms,
        "failure_reasons": failure_reasons,
        "cannot_claim": ["不能说这个 A/B case 证明未来不会再犯同类错。"],
    }


def repeat_case_text(raw_case: dict[str, Any], prefix: str) -> str:
    direct = raw_case.get(f"{prefix}_output") or raw_case.get(f"{prefix}_text")
    if isinstance(direct, str):
        return direct
    nested = raw_case.get(prefix)
    if isinstance(nested, dict):
        text = nested.get("text") or nested.get("output")
        if isinstance(text, str):
            return text
    return ""


def repeat_error_evidence_metadata(cases_doc: Any) -> dict[str, Any]:
    if not isinstance(cases_doc, dict):
        return {
            "evidence_mode": "provided_ab_outputs",
            "notes": [],
            "raw_evidence_paths": [],
        }
    raw_mode = str(cases_doc.get("evidence_mode") or cases_doc.get("ab_evidence_mode") or "").strip()
    return {
        "evidence_mode": raw_mode or "provided_ab_outputs",
        "notes": normalize_string_list(cases_doc.get("notes")),
        "raw_evidence_paths": normalize_string_list(
            cases_doc.get("raw_evidence_paths")
            or cases_doc.get("ab_raw_evidence_paths")
            or cases_doc.get("source_paths")
        ),
    }


def repeat_error_claim_status(all_cases: list[dict[str, Any]], evidence_metadata: dict[str, Any]) -> dict[str, list[str]]:
    missing = benchmark_missing_evidence(all_cases)
    if not evidence_metadata.get("raw_evidence_paths"):
        missing.append(
            "repeat-error cases 缺 raw_evidence_paths；只能 claim 已评分提供的 A/B 文本，不能 claim fresh-agent 来源。"
        )
    return claim_status(
        verified=[
            "已生成 repeat-error A/B 基准报告。",
            "benchmark 自身未生成 without_recipe/with_recipe 输出，只评分 cases 文件里的文本。",
        ],
        missing_evidence=missing,
        cannot_claim=[
            "不能说 repeat-error-benchmark 本轮启动 fresh agent。",
            "不能说 A/B fixture 通过就证明所有真实 agent 都会变好。",
            "不能说 repeat-error benchmark 替代人工质量评审。",
            "不能说 benchmark 已经执行了 SampleProject 或其他项目任务。",
            "不能把 stored A/B 输出说成本轮新生成输出，除非另有原始对照证据。",
        ],
    )


def output_quality_case(raw_case: Any) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        return {
            "case_id": "invalid_case",
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["output-quality case must be an object."],
        }
    case_id = str(raw_case.get("case_id") or raw_case.get("id") or "unnamed_case")
    output_text = output_quality_text(raw_case)
    if not output_text:
        return {
            "case_id": case_id,
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["case needs output, actual_output, with_recipe_output, or nested answer/text."],
            "cannot_claim": ["不能说该真实输出已经被质量裁判验证。"],
        }
    text_lower = output_text.casefold()
    required_terms = normalize_string_list(raw_case.get("required_terms"))
    forbidden_terms = normalize_string_list(raw_case.get("forbidden_terms"))
    required_any_groups = output_quality_required_any_groups(raw_case.get("required_any_terms"))
    matched_required_terms = [term for term in required_terms if term.casefold() in text_lower]
    min_required_terms = int(raw_case.get("min_required_terms") or len(required_terms))
    min_required_terms = max(0, min_required_terms)
    missing_required_terms = [term for term in required_terms if term.casefold() not in text_lower]
    forbidden_terms_present = [term for term in forbidden_terms if term.casefold() in text_lower]
    matched_any_groups: list[dict[str, Any]] = []
    missing_any_groups: list[list[str]] = []
    for group in required_any_groups:
        matched = [term for term in group if term.casefold() in text_lower]
        if matched:
            matched_any_groups.append({"options": group, "matched": matched})
        else:
            missing_any_groups.append(group)

    metadata_failures: list[str] = []
    expected_recipe_id = str(raw_case.get("expected_recipe_id") or "").strip()
    actual_recipe_id = output_quality_field(raw_case, "lookup_recipe_id") or output_quality_field(raw_case, "recipe_id")
    if expected_recipe_id:
        if actual_recipe_id:
            if actual_recipe_id != expected_recipe_id:
                metadata_failures.append(f"expected_recipe_id={expected_recipe_id}; actual_recipe_id={actual_recipe_id}")
        elif expected_recipe_id.casefold() not in text_lower:
            metadata_failures.append(f"expected_recipe_id={expected_recipe_id}; actual_recipe_id=missing")

    expected_lock_raw = raw_case.get("expected_lock")
    actual_lock_id = output_quality_field(raw_case, "lock_id")
    if expected_lock_raw is not None:
        expected_lock = bool(expected_lock_raw)
        has_lock = bool(actual_lock_id)
        if expected_lock and not has_lock:
            metadata_failures.append("expected_lock=true; lock_id=missing")
        if not expected_lock and has_lock:
            metadata_failures.append(f"expected_lock=false; lock_id={actual_lock_id}")

    failure_reasons: list[str] = []
    if len(matched_required_terms) < min_required_terms:
        failure_reasons.append(f"required terms matched {len(matched_required_terms)} / {min_required_terms}")
    if missing_any_groups:
        failure_reasons.append(f"required-any groups missing {len(missing_any_groups)}")
    if forbidden_terms_present:
        failure_reasons.append(f"forbidden terms present: {', '.join(forbidden_terms_present)}")
    failure_reasons.extend(metadata_failures)

    total_checks = max(1, min_required_terms + len(required_any_groups) + len(forbidden_terms) + (1 if expected_recipe_id else 0) + (1 if expected_lock_raw is not None else 0))
    passed_checks = min(len(matched_required_terms), min_required_terms)
    passed_checks += len(matched_any_groups)
    passed_checks += len(forbidden_terms) - len(forbidden_terms_present)
    if expected_recipe_id and not any(reason.startswith("expected_recipe_id=") for reason in metadata_failures):
        passed_checks += 1
    if expected_lock_raw is not None and not any(reason.startswith("expected_lock=") for reason in metadata_failures):
        passed_checks += 1
    case_score = passed_checks / total_checks
    passed = not failure_reasons
    return {
        "case_id": case_id,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "output_quality_score": case_score,
        "task": raw_case.get("task") or raw_case.get("prompt"),
        "actual_recipe_id": actual_recipe_id,
        "actual_lock_id": actual_lock_id,
        "required_terms": required_terms,
        "matched_required_terms": matched_required_terms,
        "missing_required_terms": [] if len(matched_required_terms) >= min_required_terms else missing_required_terms,
        "required_any_terms": required_any_groups,
        "matched_any_groups": matched_any_groups,
        "missing_any_groups": missing_any_groups,
        "forbidden_terms": forbidden_terms,
        "forbidden_terms_present": forbidden_terms_present,
        "failure_reasons": failure_reasons,
        "missing_evidence": failure_reasons,
        "cannot_claim": ["不能说这个输出质量 case 证明真实任务已经完成或质量通过。"],
    }


def output_quality_text(raw_case: dict[str, Any]) -> str:
    text_parts: list[str] = []
    for key in ("output", "actual_output", "with_recipe_output", "answer", "text"):
        value = raw_case.get(key)
        if isinstance(value, str):
            text_parts.append(value)
        elif isinstance(value, dict):
            text_parts.extend(output_quality_dict_text(value))
    for key in ("actual", "with_recipe", "with_recipe_lookup_lock"):
        value = raw_case.get(key)
        if isinstance(value, dict):
            text_parts.extend(output_quality_dict_text(value))
    return "\n".join(part for part in text_parts if part.strip())


def output_quality_dict_text(value: dict[str, Any]) -> list[str]:
    text_parts: list[str] = []
    for key in ("answer", "text", "output", "lookup_recipe_id", "recipe_id", "lock_id"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            text_parts.append(item)
    return text_parts


def output_quality_field(raw_case: dict[str, Any], field: str) -> str:
    value = raw_case.get(field)
    if isinstance(value, str) and value.strip():
        return value.strip()
    for key in ("output", "actual_output", "with_recipe_output", "actual", "with_recipe", "with_recipe_lookup_lock"):
        nested = raw_case.get(key)
        if isinstance(nested, dict):
            nested_value = nested.get(field)
            if isinstance(nested_value, str) and nested_value.strip():
                return nested_value.strip()
    return ""


def output_quality_required_any_groups(value: Any) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    groups: list[list[str]] = []
    for group in value:
        items = normalize_string_list(group)
        if items:
            groups.append(items)
    return groups


def output_quality_evidence_metadata(cases_doc: Any) -> dict[str, Any]:
    if not isinstance(cases_doc, dict):
        return {
            "evidence_mode": "provided_agent_outputs",
            "notes": [],
            "raw_evidence_paths": [],
        }
    raw_mode = str(cases_doc.get("evidence_mode") or cases_doc.get("output_evidence_mode") or "").strip()
    return {
        "evidence_mode": raw_mode or "provided_agent_outputs",
        "notes": normalize_string_list(cases_doc.get("notes")),
        "raw_evidence_paths": normalize_string_list(
            cases_doc.get("raw_evidence_paths")
            or cases_doc.get("output_raw_evidence_paths")
            or cases_doc.get("source_paths")
        ),
    }


def output_quality_claim_status(all_cases: list[dict[str, Any]], evidence_metadata: dict[str, Any]) -> dict[str, list[str]]:
    missing = benchmark_missing_evidence(all_cases)
    if not evidence_metadata.get("raw_evidence_paths"):
        missing.append(
            "output-quality cases 缺 raw_evidence_paths；只能 claim 已评分提供的输出文本，不能 claim fresh-agent 来源。"
        )
    return claim_status(
        verified=[
            "已生成 output-quality 真实输出质量裁判报告。",
            "benchmark 自身未启动 agent，只评分 cases 文件里的已保存输出。",
        ],
        missing_evidence=missing,
        cannot_claim=[
            "不能说 output-quality-benchmark 本轮启动 fresh agent。",
            "不能说输出质量裁判通过就证明真实任务已完成。",
            "不能说输出质量裁判通过就证明视频、音频或用户可见质量通过。",
            "不能说 output-quality benchmark 替代人工 review。",
            "不能把 stored 输出说成本轮新生成输出，除非另有原始对照证据。",
        ],
    )


GOVERNANCE_EXPECTED_RECIPE_PLACEHOLDER = "CHOOSE_AFTER_HUMAN_GOVERNANCE"


def is_unfilled_governance_placeholder(recipe_id: str) -> bool:
    return recipe_id.strip() == GOVERNANCE_EXPECTED_RECIPE_PLACEHOLDER


def unfilled_governance_placeholder_case(*, case_id: str, query: str, command: str, expected_recipe_id: str) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "query": query,
        "status": "blocked",
        "passed": False,
        "expected_recipe_id": expected_recipe_id,
        "missing_evidence": [
            f"{command} case 仍然使用 {GOVERNANCE_EXPECTED_RECIPE_PLACEHOLDER}；必须先完成人工治理并填入 canonical recipe id。",
        ],
        "cannot_claim": [
            "不能说 what-if 模板已经完成治理。",
            "不能把未替换占位符的 case 当成压测通过证据。",
        ],
    }


def lookup_pressure_case(project: RecipesProject, raw_case: Any) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        return {
            "case_id": "invalid_case",
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["lookup-pressure case must be an object."],
        }
    case_id = str(raw_case.get("case_id") or raw_case.get("id") or "unnamed_case")
    query = str(raw_case.get("query") or "").strip()
    if not query:
        return {
            "case_id": case_id,
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["lookup-pressure case query is required."],
        }
    expect_applicable = bool(raw_case.get("expect_applicable", True))
    expected_recipe_id = str(raw_case.get("expected_recipe_id") or "")
    if is_unfilled_governance_placeholder(expected_recipe_id):
        return unfilled_governance_placeholder_case(
            case_id=case_id,
            query=query,
            command="lookup-pressure",
            expected_recipe_id=expected_recipe_id,
        )
    overreach_recipe_id = str(raw_case.get("overreach_recipe_id") or "")
    allow_other_recipe = bool(raw_case.get("allow_other_recipe", False))
    required_terms = [str(term) for term in raw_case.get("required_terms", []) if str(term).strip()]
    forbidden_terms = [str(term) for term in raw_case.get("forbidden_terms", []) if str(term).strip()]
    min_score = int(raw_case.get("min_score") or 2)
    try:
        lookup = project.lookup(query, strict=True, min_score=min_score)
    except RecipesError as exc:
        if exc.code == "AR242" and not expect_applicable:
            return {
                "case_id": case_id,
                "query": query,
                "status": "passed",
                "passed": True,
                "expect_applicable": False,
                "selected_recipe_id": None,
                "expected_recipe_id": expected_recipe_id or None,
                "overreach_recipe_id": overreach_recipe_id or None,
                "allow_other_recipe": allow_other_recipe,
                "required_terms": required_terms,
                "matched_required_terms": [],
                "missing_required_terms": [],
                "forbidden_terms": forbidden_terms,
                "matched_forbidden_terms": [],
                "failure_reasons": [],
                "no_match_reason": exc.problem,
                "min_score": min_score,
                "cannot_claim": ["不能说 no-match 证明未来不会误召回。"],
            }
        if exc.code == "AR242" and expect_applicable:
            return {
                "case_id": case_id,
                "query": query,
                "status": "failed",
                "passed": False,
                "expect_applicable": True,
                "selected_recipe_id": None,
                "expected_recipe_id": expected_recipe_id or None,
                "overreach_recipe_id": overreach_recipe_id or None,
                "allow_other_recipe": allow_other_recipe,
                "required_terms": required_terms,
                "matched_required_terms": [],
                "missing_required_terms": required_terms,
                "forbidden_terms": forbidden_terms,
                "matched_forbidden_terms": [],
                "failure_reasons": [f"no sufficient recipe: {exc.cause or exc.problem}"],
                "min_score": min_score,
                "cannot_claim": ["不能说 lookup-pressure 已验证该正例。"],
            }
        return {
            "case_id": case_id,
            "query": query,
            "status": "blocked",
            "passed": False,
            "missing_evidence": [exc.problem],
            "cannot_claim": ["不能说 lookup-pressure 已验证该 case。"],
        }
    recipe = lookup.get("recipe", {})
    recipe_id = str(recipe.get("recipe_id") or "")
    recipe_text = stable_json(recipe).casefold()
    matched_required = [term for term in required_terms if term.casefold() in recipe_text]
    missing_required = [term for term in required_terms if term.casefold() not in recipe_text]
    matched_forbidden = [term for term in forbidden_terms if term.casefold() in recipe_text]

    failures: list[str] = []
    shadowed_expected = None
    if expect_applicable:
        if expected_recipe_id and recipe_id != expected_recipe_id:
            failures.append(f"expected {expected_recipe_id}, got {recipe_id}")
            shadowed_expected = lookup_shadowed_expected_recipe(lookup, expected_recipe_id=expected_recipe_id, selected_recipe_id=recipe_id)
        for term in missing_required:
            failures.append(f"selected recipe missing required term: {term}")
        for term in matched_forbidden:
            failures.append(f"selected recipe contains forbidden term: {term}")
    else:
        if overreach_recipe_id and recipe_id == overreach_recipe_id:
            failures.append(f"overreach: selected narrow recipe {recipe_id} for out-of-scope query")
        elif expected_recipe_id and recipe_id == expected_recipe_id:
            failures.append(f"overreach: selected explicitly disallowed recipe {recipe_id}")
        elif not allow_other_recipe:
            failures.append(f"negative case selected out-of-scope recipe {recipe_id}")

    passed = not failures
    result = {
        "case_id": case_id,
        "query": query,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "expect_applicable": expect_applicable,
        "selected_recipe_id": recipe_id,
        "expected_recipe_id": expected_recipe_id or None,
        "overreach_recipe_id": overreach_recipe_id or None,
        "allow_other_recipe": allow_other_recipe,
        "required_terms": required_terms,
        "matched_required_terms": matched_required,
        "missing_required_terms": missing_required,
        "forbidden_terms": forbidden_terms,
        "matched_forbidden_terms": matched_forbidden,
        "failure_reasons": failures,
        "min_score": min_score,
        "applicability": lookup.get("applicability", {}),
        "claim_status": lookup.get("claim_status", {}),
        "cannot_claim": ["不能说 lookup 命中等于该 recipe 适用于该 case。"],
    }
    if allow_other_recipe and not expect_applicable:
        result["cannot_claim"].append("允许命中非禁止 recipe")
    if shadowed_expected:
        result["shadowed_expected_recipe"] = shadowed_expected
    return result


def lookup_pressure_missing_evidence(cases: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for case in cases:
        if case.get("status") == "blocked":
            missing.extend(str(item) for item in case.get("missing_evidence", []) if item)
        if case.get("status") == "failed":
            missing.append(f"lookup-pressure case failed: {case.get('case_id')}")
            missing.extend(str(item) for item in case.get("failure_reasons", []) if item)
    return unique_text(missing)


def lookup_shadowed_expected_recipe(lookup: dict[str, Any], *, expected_recipe_id: str, selected_recipe_id: str) -> dict[str, Any] | None:
    if not expected_recipe_id or not selected_recipe_id or expected_recipe_id == selected_recipe_id:
        return None
    candidates = lookup.get("candidates")
    if not isinstance(candidates, list):
        return None
    expected = next(
        (candidate for candidate in candidates if isinstance(candidate, dict) and candidate.get("recipe_id") == expected_recipe_id),
        None,
    )
    if not expected:
        return None
    selected = next(
        (candidate for candidate in candidates if isinstance(candidate, dict) and candidate.get("recipe_id") == selected_recipe_id),
        {},
    )
    return {
        "status": "duplicate_shadow_risk",
        "expected_recipe_id": expected_recipe_id,
        "selected_recipe_id": selected_recipe_id,
        "expected_score": expected.get("score"),
        "selected_score": selected.get("score"),
        "expected_matched_terms": expected.get("matched_terms") or [],
        "selected_matched_terms": selected.get("matched_terms") or [],
        "cannot_claim": [
            "不能自动判定该保留 expected 还是 selected。",
            "不能自动 merge/supersede 正式 recipe。",
            "需要人工决定 canonical recipe、priority 或 evidence-only 边界。",
        ],
    }


def lock_pressure_case(project: RecipesProject, raw_case: Any) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        return {
            "case_id": "invalid_case",
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["lock-pressure case must be an object."],
        }
    case_id = str(raw_case.get("case_id") or raw_case.get("id") or "unnamed_case")
    query = str(raw_case.get("query") or "").strip()
    if not query:
        return {
            "case_id": case_id,
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["lock-pressure case query is required."],
        }
    expect_lock = bool(raw_case.get("expect_lock", raw_case.get("expect_applicable", True)))
    expected_recipe_id = str(raw_case.get("expected_recipe_id") or raw_case.get("recipe_id") or "")
    if is_unfilled_governance_placeholder(expected_recipe_id):
        return unfilled_governance_placeholder_case(
            case_id=case_id,
            query=query,
            command="lock-pressure",
            expected_recipe_id=expected_recipe_id,
        )
    overreach_recipe_id = str(raw_case.get("overreach_recipe_id") or "")
    min_score = int(raw_case.get("min_score") or 2)
    task = str(raw_case.get("task") or f"lock-pressure:{case_id}")

    if expect_lock and not expected_recipe_id:
        return {
            "case_id": case_id,
            "query": query,
            "status": "blocked",
            "passed": False,
            "expect_lock": True,
            "missing_evidence": ["positive lock-pressure case needs expected_recipe_id or recipe_id."],
            "cannot_claim": ["不能说 lock-pressure 已验证该正例。"],
        }

    try:
        lookup = project.lookup(query, strict=True, min_score=min_score)
    except RecipesError as exc:
        if not expect_lock and exc.code == "AR242":
            return {
                "case_id": case_id,
                "query": query,
                "status": "passed",
                "passed": True,
                "expect_lock": False,
                "selected_recipe_id": None,
                "expected_recipe_id": expected_recipe_id or None,
                "overreach_recipe_id": overreach_recipe_id or None,
                "lock_status": "prevented",
                "no_lock_reason": exc.problem,
                "failure_reasons": [],
                "cannot_claim": ["不能说一次 no-lock 就证明未来不会误锁。"],
            }
        return {
            "case_id": case_id,
            "query": query,
            "status": "failed" if expect_lock else "blocked",
            "passed": False,
            "expect_lock": expect_lock,
            "selected_recipe_id": None,
            "expected_recipe_id": expected_recipe_id or None,
            "overreach_recipe_id": overreach_recipe_id or None,
            "lock_status": "not_created",
            "failure_reasons": [f"lookup failed before lock: {exc.cause or exc.problem}"],
            "cannot_claim": ["不能说 lock-pressure 已验证该 case。"],
        }

    selected_recipe_id = str(lookup.get("recipe", {}).get("recipe_id") or "")
    if not expect_lock:
        failures: list[str] = []
        if overreach_recipe_id and selected_recipe_id == overreach_recipe_id:
            failures.append(f"overreach: lookup would lock narrow recipe {selected_recipe_id}")
        elif expected_recipe_id and selected_recipe_id == expected_recipe_id:
            failures.append(f"overreach: lookup selected explicitly disallowed recipe {selected_recipe_id}")
        else:
            failures.append(f"negative case still produced strong lookup: {selected_recipe_id}")
        return {
            "case_id": case_id,
            "query": query,
            "status": "failed",
            "passed": False,
            "expect_lock": False,
            "selected_recipe_id": selected_recipe_id,
            "expected_recipe_id": expected_recipe_id or None,
            "overreach_recipe_id": overreach_recipe_id or None,
            "lock_status": "not_created",
            "failure_reasons": failures,
            "applicability": lookup.get("applicability"),
            "cannot_claim": ["不能说负例已正确阻止 lock。"],
        }

    if selected_recipe_id != expected_recipe_id:
        result = {
            "case_id": case_id,
            "query": query,
            "status": "failed",
            "passed": False,
            "expect_lock": True,
            "selected_recipe_id": selected_recipe_id,
            "expected_recipe_id": expected_recipe_id,
            "lock_status": "not_created",
            "failure_reasons": [f"expected {expected_recipe_id}, got {selected_recipe_id}"],
            "applicability": lookup.get("applicability"),
            "cannot_claim": ["不能说 lock-pressure 已验证该正例。"],
        }
        shadowed_expected = lookup_shadowed_expected_recipe(lookup, expected_recipe_id=expected_recipe_id, selected_recipe_id=selected_recipe_id)
        if shadowed_expected:
            result["shadowed_expected_recipe"] = shadowed_expected
        return result

    try:
        locked = project.create_lock(expected_recipe_id, task=task, query=query, min_score=min_score)
    except RecipesError as exc:
        return {
            "case_id": case_id,
            "query": query,
            "status": "failed",
            "passed": False,
            "expect_lock": True,
            "selected_recipe_id": selected_recipe_id,
            "expected_recipe_id": expected_recipe_id,
            "lock_status": "not_created",
            "failure_reasons": [f"lock failed: {exc.cause or exc.problem}"],
            "applicability": lookup.get("applicability"),
            "cannot_claim": ["不能说 lock-pressure 已验证该正例。"],
        }
    lock = locked.get("lock", {}) if isinstance(locked.get("lock"), dict) else {}
    return {
        "case_id": case_id,
        "query": query,
        "status": "passed",
        "passed": True,
        "expect_lock": True,
        "selected_recipe_id": selected_recipe_id,
        "expected_recipe_id": expected_recipe_id,
        "lock_id": lock.get("lock_id"),
        "lock_status": str(locked.get("idempotency_status") or "created"),
        "task": task,
        "applicability": lock.get("applicability") or lookup.get("applicability"),
        "failure_reasons": [],
        "cannot_claim": ["不能说 lock 创建就代表 recipe 已执行或质量通过。"],
    }


def lock_pressure_missing_evidence(cases: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for case in cases:
        if case.get("status") == "blocked":
            missing.extend(str(item) for item in case.get("missing_evidence", []) if item)
        if case.get("status") == "failed":
            missing.append(f"lock-pressure case failed: {case.get('case_id')}")
            missing.extend(str(item) for item in case.get("failure_reasons", []) if item)
    return unique_text(missing)


def consumption_coverage_scope(root: Path, active_recipe_ids: list[str]) -> dict[str, Any]:
    scoped_ids: list[str] = []
    scope_files: list[str] = []
    ignored_ids: list[str] = []
    modes: list[str] = []
    active = set(active_recipe_ids)
    for case_path in consumption_case_paths(root):
        data = read_json(case_path, {})
        scope = data.get("coverage_scope") if isinstance(data, dict) else None
        if not scope:
            continue
        if isinstance(scope, str):
            scope = {"mode": scope}
        if not isinstance(scope, dict):
            raise RecipesError("AR482", "coverage_scope 格式不正确。", str(case_path), "使用 {\"mode\":\"case_targets\"} 或 {\"mode\":\"explicit_recipe_ids\"}。")
        mode = str(scope.get("mode") or "").strip()
        if mode not in {"case_targets", "explicit_recipe_ids"}:
            raise RecipesError("AR483", "coverage_scope.mode 不支持。", f"{case_path}: {mode}", "使用 case_targets 或 explicit_recipe_ids。")
        modes.append(mode)
        scope_files.append(str(case_path))
        if mode == "explicit_recipe_ids":
            raw_ids = scope.get("recipe_ids")
            candidate_ids = [str(item) for item in raw_ids] if isinstance(raw_ids, list) else []
        else:
            candidate_ids = []
            for case in pressure_cases_from_path(case_path):
                if not coverage_case_is_positive(case):
                    continue
                recipe_id = str(case.get("expected_recipe_id") or case.get("recipe_id") or "")
                if recipe_id:
                    candidate_ids.append(recipe_id)
        for recipe_id in candidate_ids:
            if recipe_id in active:
                scoped_ids.append(recipe_id)
            elif recipe_id:
                ignored_ids.append(recipe_id)
    if not scope_files:
        return {
            "mode": "all_active",
            "recipe_ids": active_recipe_ids,
            "source_case_files": [],
            "cannot_claim": ["不能说全量 coverage 通过等于 recipe 已执行或质量通过。"],
        }
    scoped_ids = unique_text(scoped_ids)
    if not scoped_ids:
        raise RecipesError("AR484", "coverage_scope 没有匹配到 active recipe。", ", ".join(scope_files), "检查 coverage_scope 或 cases 里的 expected_recipe_id。")
    return {
        "mode": "scoped",
        "scope_modes": unique_text(modes),
        "recipe_ids": scoped_ids,
        "active_recipe_count": len(active_recipe_ids),
        "source_case_files": unique_text(scope_files),
        "ignored_recipe_ids": unique_text(ignored_ids),
        "cannot_claim": [
            "不能说 scoped coverage 覆盖了项目全部 recipe。",
            "不能说诊断池 coverage 通过等于真实任务质量通过。",
        ],
    }


def coverage_case_is_positive(case: dict[str, Any]) -> bool:
    return bool(case.get("expect_applicable", case.get("expect_lock", True))) or bool(case.get("expect_lock", False))


def consumption_coverage_rows(root: Path, recipes_dir: Path, recipe_ids: list[str]) -> list[dict[str, Any]]:
    lookup_case_map: dict[str, list[str]] = {recipe_id: [] for recipe_id in recipe_ids}
    lock_case_map: dict[str, list[str]] = {recipe_id: [] for recipe_id in recipe_ids}
    for case_path in consumption_case_paths(root):
        for case in pressure_cases_from_path(case_path):
            recipe_id = str(case.get("expected_recipe_id") or case.get("recipe_id") or "")
            if recipe_id not in lookup_case_map:
                continue
            if bool(case.get("expect_applicable", case.get("expect_lock", True))):
                lookup_case_map[recipe_id].append(str(case_path))
            if bool(case.get("expect_lock", False)) or "lock_pressure" in case_path.name:
                if bool(case.get("expect_lock", case.get("expect_applicable", True))):
                    lock_case_map[recipe_id].append(str(case_path))

    lookup_passed: dict[str, list[str]] = {recipe_id: [] for recipe_id in recipe_ids}
    lock_passed: dict[str, list[str]] = {recipe_id: [] for recipe_id in recipe_ids}
    negative_prevented = 0
    for report_path in sorted((recipes_dir / "reports").glob("lookup_pressure_*.json")):
        report = read_json(report_path, {})
        if not pressure_report_counts_as_official_evidence(root, report):
            continue
        for case in report.get("cases", []) if isinstance(report.get("cases"), list) else []:
            if case.get("status") != "passed" or not bool(case.get("expect_applicable", True)):
                continue
            recipe_id = str(case.get("expected_recipe_id") or case.get("selected_recipe_id") or "")
            if recipe_id in lookup_passed:
                lookup_passed[recipe_id].append(str(report_path))
    for report_path in sorted((recipes_dir / "reports").glob("lock_pressure_*.json")):
        report = read_json(report_path, {})
        if not pressure_report_counts_as_official_evidence(root, report):
            continue
        for case in report.get("cases", []) if isinstance(report.get("cases"), list) else []:
            expect_lock = bool(case.get("expect_lock", True))
            if case.get("status") == "passed" and not expect_lock and case.get("lock_status") == "prevented":
                negative_prevented += 1
            if case.get("status") != "passed" or not expect_lock or not case.get("lock_id"):
                continue
            recipe_id = str(case.get("expected_recipe_id") or case.get("selected_recipe_id") or "")
            if recipe_id in lock_passed:
                lock_passed[recipe_id].append(str(report_path))
            if recipe_id in lookup_passed:
                lookup_passed[recipe_id].append(str(report_path))

    rows: list[dict[str, Any]] = []
    for recipe_id in recipe_ids:
        rows.append(
            {
                "recipe_id": recipe_id,
                "lookup_case_files": unique_text(lookup_case_map.get(recipe_id, [])),
                "lock_case_files": unique_text(lock_case_map.get(recipe_id, [])),
                "lookup_passed_reports": unique_text(lookup_passed.get(recipe_id, [])),
                "lock_passed_reports": unique_text(lock_passed.get(recipe_id, [])),
                "lookup_passed": bool(lookup_passed.get(recipe_id)),
                "lock_passed": bool(lock_passed.get(recipe_id)),
                "candidate_only": True,
                "cannot_claim": ["不能说 coverage 通过等于 recipe 已执行或质量通过。"],
            }
        )
    if rows:
        rows[0]["negative_locks_prevented_total"] = negative_prevented
    return rows


def consumption_case_paths(root: Path) -> list[Path]:
    paths: list[Path] = []
    for pattern in [
        "lookup_pressure_cases*.json",
        "lock_pressure_cases*.json",
        "lookup_lock_cases*.json",
        "*lookup_pressure_cases*.json",
        "*lock_pressure_cases*.json",
        "*lookup_lock_cases*.json",
    ]:
        paths.extend(path for path in root.glob(pattern) if path.is_file())
    return sorted(set(paths))


def pressure_cases_from_path(path: Path) -> list[dict[str, Any]]:
    data = read_json(path, {})
    raw_cases = data.get("cases") if isinstance(data, dict) else data
    if not isinstance(raw_cases, list):
        return []
    return [case for case in raw_cases if isinstance(case, dict)]


def pressure_cases_path_is_project_local(root: Path, cases_path: Path) -> bool:
    try:
        cases_path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def pressure_report_counts_as_official_evidence(root: Path, report: dict[str, Any]) -> bool:
    if report.get("official_pressure_evidence") is False:
        return False
    cases_path = report.get("cases_path")
    if not cases_path:
        return True
    return pressure_cases_path_is_project_local(root, Path(str(cases_path)))


def consumption_coverage_missing_evidence(report: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for recipe_id in report.get("missing_lookup_recipe_ids", []):
        missing.append(f"recipe missing passed lookup pressure: {recipe_id}")
    for recipe_id in report.get("missing_lock_recipe_ids", []):
        missing.append(f"recipe missing passed lock pressure: {recipe_id}")
    return missing


def real_pressure_project_rows(projects_root: Path, *, name_contains: str | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    needle = (name_contains or "").strip()
    for recipes_dir in sorted(projects_root.glob("*/.recipes")):
        project_root = recipes_dir.parent
        if needle and needle not in project_root.name and needle not in str(project_root):
            continue
        recipe_paths = sorted((recipes_dir / "recipes").glob("*.json")) if (recipes_dir / "recipes").exists() else []
        recipe_ids = [str(read_json(path, {}).get("recipe_id") or path.stem) for path in recipe_paths]
        reports = {
            "lookup_pressure": real_pressure_report_family(recipes_dir, "lookup_pressure_*.json"),
            "lock_pressure": real_pressure_report_family(recipes_dir, "lock_pressure_*.json"),
            "consumption_coverage": real_pressure_report_family(recipes_dir, "consumption_coverage_*.json"),
            "candidate_quality": real_pressure_report_family(recipes_dir, "candidate_quality_*.json"),
            "repeat_error": real_pressure_report_family(recipes_dir, "repeat_error_*.json"),
            "output_quality": real_pressure_report_family(recipes_dir, "output_quality_*.json"),
            "review_packet": real_pressure_report_family(recipes_dir, "review_packet_*.json"),
            "duplicate_governance": real_pressure_report_family(recipes_dir, "duplicate_governance_*.json"),
        }
        rows.append(
            {
                "project_name": project_root.name,
                "project_path": str(project_root),
                "recipe_count": len(recipe_ids),
                "recipe_ids": recipe_ids,
                "source_refinery": source_refinery_evidence(recipes_dir),
                "reports": reports,
            }
        )
    return rows


def real_pressure_report_family(recipes_dir: Path, pattern: str) -> dict[str, Any]:
    paths = sorted((recipes_dir / "reports").glob(pattern)) if (recipes_dir / "reports").exists() else []
    records: list[dict[str, Any]] = []
    ignored_simulation_count = 0
    require_project_local_cases = pattern in {"lookup_pressure_*.json", "lock_pressure_*.json"}
    for path in paths:
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        if require_project_local_cases and not pressure_report_counts_as_official_evidence(recipes_dir.parent, data):
            ignored_simulation_count += 1
            continue
        records.append(
            {
                "path": str(path),
                "report_id": data.get("report_id") or path.stem,
                "ok": data.get("ok"),
                "checked_at": data.get("checked_at"),
                "summary": data.get("summary", {}),
                "markdown_path": data.get("markdown_path"),
                "what_if_cases_path": data.get("what_if_cases_path"),
                "evidence_mode": data.get("evidence_mode"),
                "ab_outputs_generated_by_benchmark": data.get("ab_outputs_generated_by_benchmark"),
                "outputs_generated_by_benchmark": data.get("outputs_generated_by_benchmark"),
                "fresh_generation_in_this_run": data.get("fresh_generation_in_this_run"),
                "claim_status": data.get("claim_status", {}),
                "failure_reasons_sample": report_failure_reasons_sample(data),
                "governance_decision_options": governance_decision_options_sample(data),
                "governance_candidate_recipe_ids": governance_candidate_recipe_ids(data),
            }
        )
    latest = sorted(records, key=lambda item: (str(item.get("checked_at") or ""), str(item.get("path") or "")))[-1] if records else {}
    return {
        "report_count": len(records),
        "passed_report_count": sum(1 for record in records if record.get("ok") is True),
        "failed_report_count": sum(1 for record in records if record.get("ok") is False),
        "latest_report_id": latest.get("report_id"),
        "latest_report_path": latest.get("path"),
        "latest_markdown_path": latest.get("markdown_path"),
        "latest_what_if_cases_path": latest.get("what_if_cases_path"),
        "latest_checked_at": latest.get("checked_at"),
        "latest_ok": latest.get("ok"),
        "latest_summary": latest.get("summary") or {},
        "latest_evidence_mode": latest.get("evidence_mode"),
        "latest_ab_outputs_generated_by_benchmark": latest.get("ab_outputs_generated_by_benchmark"),
        "latest_outputs_generated_by_benchmark": latest.get("outputs_generated_by_benchmark"),
        "latest_fresh_generation_in_this_run": latest.get("fresh_generation_in_this_run"),
        "latest_claim_status": latest.get("claim_status") or {},
        "latest_failure_reasons_sample": latest.get("failure_reasons_sample") or [],
        "latest_governance_decision_options": latest.get("governance_decision_options") or [],
        "latest_governance_candidate_recipe_ids": latest.get("governance_candidate_recipe_ids") or [],
        "ignored_simulation_count": ignored_simulation_count,
    }


def report_failure_reasons_sample(report: dict[str, Any], *, limit: int = 5) -> list[str]:
    reasons: list[str] = []
    cases = report.get("cases", [])
    if not isinstance(cases, list):
        return reasons
    for case in cases:
        if not isinstance(case, dict):
            continue
        if case.get("status") == "passed":
            continue
        for reason in case.get("failure_reasons", []) if isinstance(case.get("failure_reasons"), list) else []:
            if isinstance(reason, str) and reason not in reasons:
                reasons.append(reason)
            if len(reasons) >= limit:
                return reasons
    return reasons


def governance_decision_options_sample(report: dict[str, Any], *, limit: int = 4) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    matrix = report.get("decision_matrix", [])
    if not isinstance(matrix, list):
        return options
    for option in matrix:
        if not isinstance(option, dict):
            continue
        options.append(
            {
                "action": option.get("action"),
                "plain": option.get("plain"),
                "use_when": option.get("use_when"),
                "tradeoff": option.get("tradeoff"),
                "can_auto_apply": option.get("can_auto_apply"),
                "requires_human_decision": option.get("requires_human_decision"),
                "post_decision_validation": option.get("post_decision_validation", []),
            }
        )
        if len(options) >= limit:
            break
    return options


def governance_candidate_recipe_ids(report: dict[str, Any]) -> list[str]:
    recipe_ids: list[str] = []
    validation_cases = report.get("what_if_validation_cases", [])
    if not isinstance(validation_cases, list):
        return recipe_ids
    for case in validation_cases:
        if not isinstance(case, dict):
            continue
        candidates = case.get("candidate_recipe_ids", [])
        if not isinstance(candidates, list):
            continue
        for candidate in candidates:
            if isinstance(candidate, str) and candidate not in recipe_ids:
                recipe_ids.append(candidate)
    return recipe_ids


def real_pressure_gaps(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for project in projects:
        project_name = str(project.get("project_name") or "")
        recipe_count = int(project.get("recipe_count") or 0)
        reports = project.get("reports", {})
        coverage = reports.get("consumption_coverage", {})
        lookup = reports.get("lookup_pressure", {})
        lock = reports.get("lock_pressure", {})
        candidate_quality = reports.get("candidate_quality", {})
        repeat_error = reports.get("repeat_error", {})
        if recipe_count > 0 and int(coverage.get("report_count") or 0) == 0:
            gaps.append({"project": project_name, "gap": "missing_consumption_coverage", "severity": "medium"})
        if coverage.get("latest_ok") is False:
            gaps.append({"project": project_name, "gap": "latest_consumption_coverage_failed", "severity": "high"})
        if lookup.get("latest_ok") is False:
            gaps.append({"project": project_name, "gap": "latest_lookup_pressure_failed", "severity": "high"})
        if int((lookup.get("latest_summary") or {}).get("duplicate_shadow_count") or 0) > 0:
            gaps.append({"project": project_name, "gap": "duplicate_shadow_risk", "severity": "high"})
        if lock.get("latest_ok") is False:
            gaps.append({"project": project_name, "gap": "latest_lock_pressure_failed", "severity": "high"})
        if int((lock.get("latest_summary") or {}).get("duplicate_shadow_count") or 0) > 0:
            gaps.append({"project": project_name, "gap": "duplicate_lock_shadow_risk", "severity": "high"})
        if (
            recipe_count > 0
            and int(candidate_quality.get("report_count") or 0) > 0
            and int(candidate_quality.get("passed_report_count") or 0) == 0
        ):
            gaps.append({"project": project_name, "gap": "no_passed_candidate_quality_report", "severity": "medium"})
        if recipe_count > 0 and int(candidate_quality.get("report_count") or 0) == 0 and project.get("source_refinery", {}).get("patch_drafts"):
            gaps.append({"project": project_name, "gap": "patch_drafts_without_candidate_quality_report", "severity": "medium"})
        if int(repeat_error.get("report_count") or 0) == 0 and recipe_count > 0:
            gaps.append({"project": project_name, "gap": "no_repeat_error_ab_report", "severity": "low"})
    return gaps


def real_pressure_quality_warnings(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for project in projects:
        if not isinstance(project, dict):
            continue
        project_name = str(project.get("project_name") or "")
        reports = project.get("reports", {})
        candidate_quality = reports.get("candidate_quality", {}) if isinstance(reports, dict) else {}
        review_packet = reports.get("review_packet", {}) if isinstance(reports, dict) else {}
        if not isinstance(candidate_quality, dict):
            continue
        if (
            int(candidate_quality.get("report_count") or 0) > 0
            and candidate_quality.get("latest_ok") is False
            and int(candidate_quality.get("passed_report_count") or 0) > 0
        ):
            warnings.append(
                {
                    "project": project_name,
                    "warning": "latest_candidate_quality_failed_but_prior_pass_exists",
                    "severity": "medium",
                    "report_family": "candidate_quality",
                    "latest_report_id": candidate_quality.get("latest_report_id"),
                    "latest_failure_reasons_sample": candidate_quality.get("latest_failure_reasons_sample") or [],
                    "latest_review_packet_id": review_packet.get("latest_report_id") if isinstance(review_packet, dict) else None,
                    "latest_review_packet_markdown_path": review_packet.get("latest_markdown_path") if isinstance(review_packet, dict) else None,
                    "latest_review_packet_summary": review_packet.get("latest_summary") if isinstance(review_packet, dict) else {},
                    "latest_review_packet_plain": real_pressure_review_packet_plain(
                        review_packet.get("latest_summary") if isinstance(review_packet, dict) else {}
                    ),
                    "next_pressure_actions": real_pressure_quality_warning_next_actions(
                        failure_reasons=candidate_quality.get("latest_failure_reasons_sample") or [],
                        review_packet_summary=review_packet.get("latest_summary") if isinstance(review_packet, dict) else {},
                    ),
                    "passed_report_count": candidate_quality.get("passed_report_count"),
                    "failed_report_count": candidate_quality.get("failed_report_count"),
                    "plain": "这个池子以前有质量报告通过，但最新一次 candidate-quality 失败了；不能只看曾经通过。",
                }
            )
    return warnings


def real_pressure_review_packet_plain(summary: Any) -> str:
    if not isinstance(summary, dict) or not summary:
        return ""
    review_count = int(summary.get("review_count") or 0)
    bucket_counts = summary.get("bucket_counts") if isinstance(summary.get("bucket_counts"), dict) else {}
    action_counts = summary.get("action_counts") if isinstance(summary.get("action_counts"), dict) else {}
    parts: list[str] = []
    if review_count:
        parts.append(f"审核包里有 {review_count} 条候选")
    if bucket_counts:
        buckets = "，".join(f"{key}={value}" for key, value in sorted(bucket_counts.items()))
        parts.append(f"分层：{buckets}")
    if action_counts:
        actions = "，".join(f"{key}={value}" for key, value in sorted(action_counts.items()))
        parts.append(f"建议动作：{actions}")
    return "；".join(parts)


def real_pressure_quality_warning_next_actions(*, failure_reasons: Any, review_packet_summary: Any) -> list[str]:
    actions: list[str] = []
    reasons = [str(reason) for reason in failure_reasons] if isinstance(failure_reasons, list) else []
    summary = review_packet_summary if isinstance(review_packet_summary, dict) else {}
    bucket_counts = summary.get("bucket_counts") if isinstance(summary.get("bucket_counts"), dict) else {}
    action_counts = summary.get("action_counts") if isinstance(summary.get("action_counts"), dict) else {}

    if any(reason.startswith("missing required source path:") for reason in reasons):
        actions.append(
            "让菜谱按 missing required source path 做 source-path scoped self-run-benchmark；不要把缺来源的候选收成正式菜谱。"
        )
    if any("proposed value count below minimum" in reason for reason in reasons):
        actions.append(
            "让菜谱重跑更窄的 refine/extract-cards/patch-draft，并用 candidate-quality 的 min_proposed_value_count 复测。"
        )
    if int(bucket_counts.get("thin_candidate") or 0) > 0 or int(action_counts.get("reject_or_archive_until_more_evidence") or 0) > 0:
        actions.append(
            "当前 thin_candidate 只能当失败证据；下一轮应缩小 source/path 范围或触发 deep-read-plan 补证据。"
        )
    if int(bucket_counts.get("evidence_index_only") or 0) > 0 or int(action_counts.get("keep_as_evidence_index_or_reject_review") or 0) > 0:
        actions.append(
            "evidence_index_only 只能保留为来源索引；下一轮用它的 source_trace/target_fields 生成更窄候选。"
        )
    if not actions:
        actions.append(
            "先复跑 review-triage/review-packet，确认候选分层；再决定是否补 source-scoped self-run 或 candidate-quality。"
        )
    actions.append("补完后必须复跑 candidate-quality-benchmark、review-packet 和 real-pressure-summary。")
    return list(dict.fromkeys(actions))


def real_pressure_manual_governance_items(projects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for project in projects:
        if not isinstance(project, dict):
            continue
        project_name = str(project.get("project_name") or "")
        reports = project.get("reports", {})
        duplicate_governance = reports.get("duplicate_governance", {}) if isinstance(reports, dict) else {}
        if not isinstance(duplicate_governance, dict):
            continue
        latest_summary = duplicate_governance.get("latest_summary") or {}
        if not isinstance(latest_summary, dict):
            continue
        required = int(latest_summary.get("human_governance_required") or 0)
        if required <= 0:
            continue
        items.append(
            {
                "project": project_name,
                "reason": "duplicate_shadow_governance_required",
                "severity": "high",
                "human_governance_required": required,
                "shadow_risk_count": int(latest_summary.get("shadow_risk_count") or 0),
                "latest_report_id": duplicate_governance.get("latest_report_id"),
                "latest_markdown_path": duplicate_governance.get("latest_markdown_path"),
                "latest_what_if_cases_path": duplicate_governance.get("latest_what_if_cases_path"),
                "candidate_recipe_ids": duplicate_governance.get("latest_governance_candidate_recipe_ids") or [],
                "decision_options": duplicate_governance.get("latest_governance_decision_options") or [],
                "plain": "这里有近重复菜谱互相遮挡，系统不能自动决定合并、废弃、加优先级或拆范围。",
            }
        )
    return items


def real_pressure_manual_governance_readiness(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected_actions = {
        "merge_or_supersede",
        "mark_narrow_recipe_evidence_only",
        "add_explicit_priority_rule",
        "split_broad_recipe_scope",
    }
    readiness: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        missing: list[str] = []
        project_name = str(item.get("project") or "")
        if not item.get("latest_report_id"):
            missing.append("missing latest duplicate-governance report id")
        if not item.get("latest_markdown_path"):
            missing.append("missing readable duplicate-governance Markdown packet")
        if not item.get("latest_what_if_cases_path"):
            missing.append("missing what-if validation case template file")
        candidate_ids = item.get("candidate_recipe_ids")
        if not isinstance(candidate_ids, list) or len([candidate for candidate in candidate_ids if candidate]) < 2:
            missing.append("missing at least two candidate recipe ids")
        options = item.get("decision_options")
        option_actions: set[str] = set()
        if not isinstance(options, list) or not options:
            missing.append("missing governance decision options")
            options = []
        for option in options:
            if not isinstance(option, dict):
                continue
            action = str(option.get("action") or "")
            if action:
                option_actions.add(action)
            if option.get("can_auto_apply") is not False:
                missing.append(f"{action or 'unknown option'} is not explicitly can_auto_apply=false")
            validations = option.get("post_decision_validation")
            if not isinstance(validations, list) or not validations:
                missing.append(f"{action or 'unknown option'} missing post-decision validation")
        for action in sorted(expected_actions - option_actions):
            missing.append(f"missing governance option: {action}")
        ready = len(missing) == 0
        readiness.append(
            {
                "project": project_name,
                "ready_for_human_decision": ready,
                "missing_evidence": missing,
                "candidate_recipe_ids": list(candidate_ids) if isinstance(candidate_ids, list) else [],
                "decision_actions": sorted(option_actions),
                "latest_report_id": item.get("latest_report_id"),
                "latest_markdown_path": item.get("latest_markdown_path"),
                "latest_what_if_cases_path": item.get("latest_what_if_cases_path"),
                "next_human_action": "选择 canonical recipe / 治理路线，然后填 what-if cases 再复跑 lookup-pressure、lock-pressure、consumption-coverage、real-pressure-summary。",
                "plain": (
                    "治理材料已齐，可以让人拍板；系统仍不能自动选择。"
                    if ready
                    else "治理材料还不齐，先补 duplicate-governance 包或 what-if 验收模板。"
                ),
                "cannot_claim": [
                    "不能说 readiness=true 就代表 duplicate shadow 已修复。",
                    "不能说系统已经选择 canonical recipe。",
                    "不能把未填 CHOOSE_AFTER_HUMAN_GOVERNANCE 的 what-if 模板当通过证据。",
                ],
            }
        )
    return readiness


def real_pressure_ignored_simulation_report_count(projects: list[dict[str, Any]]) -> int:
    total = 0
    for project in projects:
        reports = project.get("reports", {}) if isinstance(project, dict) else {}
        if not isinstance(reports, dict):
            continue
        for family in reports.values():
            if isinstance(family, dict):
                total += int(family.get("ignored_simulation_count") or 0)
    return total


def real_pressure_missing_evidence(projects: list[dict[str, Any]], gaps: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    if not projects:
        missing.append("no .recipes real-test projects found")
    for gap in gaps[:20]:
        missing.append(f"{gap.get('project')}: {gap.get('gap')}")
    if len(gaps) > 20:
        missing.append(f"{len(gaps) - 20} more pressure gaps omitted from claim_status")
    return missing


REAL_PRESSURE_GAP_LABELS = {
    "missing_consumption_coverage": "缺消费覆盖报告",
    "latest_consumption_coverage_failed": "最新消费覆盖没过",
    "latest_lookup_pressure_failed": "最新 lookup 压测没过",
    "duplicate_shadow_risk": "重复/遮挡风险",
    "latest_lock_pressure_failed": "最新 lock 压测没过",
    "duplicate_lock_shadow_risk": "lock 重复/遮挡风险",
    "no_passed_candidate_quality_report": "没有通过的候选质量报告",
    "patch_drafts_without_candidate_quality_report": "有补丁草稿但没跑候选质量报告",
    "no_repeat_error_ab_report": "缺少 repeat-error A/B 评分证据",
}


REAL_PRESSURE_GAP_REPORT_FAMILY = {
    "missing_consumption_coverage": "consumption_coverage",
    "latest_consumption_coverage_failed": "consumption_coverage",
    "latest_lookup_pressure_failed": "lookup_pressure",
    "duplicate_shadow_risk": "duplicate_governance",
    "latest_lock_pressure_failed": "lock_pressure",
    "duplicate_lock_shadow_risk": "duplicate_governance",
    "no_passed_candidate_quality_report": "candidate_quality",
    "patch_drafts_without_candidate_quality_report": "candidate_quality",
    "no_repeat_error_ab_report": "repeat_error",
}


def real_pressure_summary_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    projects = report.get("projects") if isinstance(report.get("projects"), list) else []
    gaps = report.get("pressure_gaps") if isinstance(report.get("pressure_gaps"), list) else []
    warnings = report.get("quality_warnings") if isinstance(report.get("quality_warnings"), list) else []
    manual_items = report.get("manual_governance_items") if isinstance(report.get("manual_governance_items"), list) else []
    readiness = report.get("manual_governance_readiness") if isinstance(report.get("manual_governance_readiness"), list) else []
    by_project = {str(project.get("project_name") or ""): project for project in projects if isinstance(project, dict)}
    lines = [
        "# 真实压测总看板",
        "",
        f"Report: `{report.get('report_id')}`",
        "",
        "## 一眼结论",
        "",
        f"- 压测项目数：`{summary.get('project_count', 0)}`",
        f"- 正式菜谱数：`{summary.get('formal_recipe_count', 0)}`",
        f"- 有消费覆盖报告的项目：`{summary.get('projects_with_consumption_coverage', 0)}`",
        f"- 最新覆盖通过的项目：`{summary.get('projects_with_latest_coverage_ok', 0)}`",
        f"- 有真实输出质量裁判的项目：`{summary.get('projects_with_output_quality', 0)}`",
        f"- 最新真实输出质量裁判通过的项目：`{summary.get('projects_with_latest_output_quality_ok', 0)}`",
        f"- 当前压力缺口：`{summary.get('pressure_gap_count', len(gaps))}`",
        f"- 质量警告：`{summary.get('quality_warning_count', len(warnings))}`",
        f"- 需要人工治理：`{summary.get('manual_governance_required_count', len(manual_items))}`",
        f"- 治理材料已齐：`{summary.get('manual_governance_ready_count', 0)}`",
        f"- 治理材料未齐：`{summary.get('manual_governance_not_ready_count', 0)}`",
        f"- 已忽略临时模拟报告：`{summary.get('ignored_simulation_report_count', 0)}`",
        "- 这个报告只做裁判汇总，不拆资料、不接受 review、不改正式 recipe。",
        "",
        "## 不能 claim",
        "",
        "- 不能说这个总看板通过就证明真实任务质量通过。",
        "- 不能说缺口已经修复。",
        "- 不能说它替代人工 review、真实执行或最终视觉验收。",
        "",
    ]
    if warnings:
        lines.extend(["## 质量警告", ""])
        for warning in warnings:
            if not isinstance(warning, dict):
                continue
            line = f"- `{warning.get('project')}`：{warning.get('plain')} 最新报告：`{warning.get('latest_report_id')}`。"
            reasons = warning.get("latest_failure_reasons_sample")
            if isinstance(reasons, list) and reasons:
                sample = "；".join(str(reason) for reason in reasons[:3])
                line += f" 原因样本：{sample}。"
            review_packet_id = warning.get("latest_review_packet_id")
            review_packet_path = warning.get("latest_review_packet_markdown_path")
            if review_packet_id:
                line += f" 最新审核包：`{review_packet_id}`。"
            if review_packet_path:
                line += f" 审核包 Markdown：`{review_packet_path}`。"
            review_packet_plain = warning.get("latest_review_packet_plain")
            if review_packet_plain:
                line += f" 审核包结论：{review_packet_plain}。"
            lines.append(line)
            next_actions = warning.get("next_pressure_actions")
            if isinstance(next_actions, list) and next_actions:
                lines.append("  - 下一轮压测建议：")
                for action in next_actions[:5]:
                    lines.append(f"    - {action}")
        lines.append("")
    if readiness:
        lines.extend(["## 治理准备度", ""])
        for item in readiness:
            if not isinstance(item, dict):
                continue
            status = "可让人拍板" if item.get("ready_for_human_decision") is True else "材料还缺"
            lines.append(f"- `{item.get('project')}`：{status}。{item.get('plain')}")
            candidate_ids = item.get("candidate_recipe_ids")
            if isinstance(candidate_ids, list) and candidate_ids:
                lines.append(f"  - 候选菜谱：`{'`, `'.join(str(recipe_id) for recipe_id in candidate_ids)}`")
            actions = item.get("decision_actions")
            if isinstance(actions, list) and actions:
                lines.append(f"  - 可选治理动作：`{'`, `'.join(str(action) for action in actions)}`")
            missing = item.get("missing_evidence")
            if isinstance(missing, list) and missing:
                lines.append(f"  - 还缺：{'; '.join(str(part) for part in missing[:6])}")
            if item.get("latest_markdown_path"):
                lines.append(f"  - 可读治理包：`{item.get('latest_markdown_path')}`")
            if item.get("latest_what_if_cases_path"):
                lines.append(f"  - what-if 模板：`{item.get('latest_what_if_cases_path')}`")
            lines.append(f"  - 下一步人工动作：{item.get('next_human_action')}")
            lines.append("  - 注意：readiness=true 只代表材料齐，不代表问题已修。")
        lines.append("")
    if manual_items:
        lines.extend(["## 人工治理入口", ""])
        for item in manual_items:
            if not isinstance(item, dict):
                continue
            line = (
                f"- `{item.get('project')}`：{item.get('plain')} "
                f"治理报告：`{item.get('latest_report_id')}`。"
            )
            if item.get("latest_markdown_path"):
                line += f" 可读报告：`{item.get('latest_markdown_path')}`。"
            if item.get("latest_what_if_cases_path"):
                line += f" what-if 模板：`{item.get('latest_what_if_cases_path')}`。"
            lines.append(line)
            candidate_ids = item.get("candidate_recipe_ids")
            if isinstance(candidate_ids, list) and candidate_ids:
                lines.append(f"  - 候选菜谱：`{'`, `'.join(str(recipe_id) for recipe_id in candidate_ids)}`")
            options = item.get("decision_options")
            if isinstance(options, list) and options:
                lines.append("  - 可选路线：")
                for option in options:
                    if not isinstance(option, dict):
                        continue
                    action = option.get("action") or "unknown"
                    plain = option.get("plain") or ""
                    tradeoff = option.get("tradeoff") or ""
                    can_auto = option.get("can_auto_apply")
                    lines.append(f"    - `{action}`：{plain} 代价：{tradeoff} 自动执行：`{can_auto}`")
                    validations = option.get("post_decision_validation")
                    if isinstance(validations, list) and validations:
                        validation_text = "；".join(str(step) for step in validations[:4])
                        lines.append(f"      - 选后验收：{validation_text}")
        lines.append("")
    lines.append("## 压力缺口")
    if not gaps:
        lines.extend(
            [
                "",
                "- 这次没有汇总到压力缺口。",
                "- 这只说明当前本地报告没报缺口，不等于真实生产质量已经证明。",
            ]
        )
        return "\n".join(lines) + "\n"

    lines.extend(["", "### 根因归并", ""])
    for group in real_pressure_gap_groups(gaps):
        lines.append(
            f"- `{group['project']}`：`{group['gap_count']}` 个缺口。大白话：{group['plain']}"
        )
    lines.extend(["", "### 缺口明细"])

    for index, gap in enumerate(gaps, start=1):
        if not isinstance(gap, dict):
            continue
        project_name = str(gap.get("project") or "unknown")
        gap_key = str(gap.get("gap") or "unknown")
        severity = str(gap.get("severity") or "unknown")
        label = REAL_PRESSURE_GAP_LABELS.get(gap_key, gap_key)
        project = by_project.get(project_name, {})
        reports = project.get("reports") if isinstance(project.get("reports"), dict) else {}
        family_name = REAL_PRESSURE_GAP_REPORT_FAMILY.get(gap_key)
        family = reports.get(family_name, {}) if family_name else {}
        latest_report_id = family.get("latest_report_id") if isinstance(family, dict) else None
        latest_ok = family.get("latest_ok") if isinstance(family, dict) else None
        latest_markdown_path = family.get("latest_markdown_path") if isinstance(family, dict) else None
        latest_summary = family.get("latest_summary") if isinstance(family, dict) else {}

        lines.extend(
            [
                "",
                f"### {index}. `{project_name}`",
                "",
                f"- 缺口：{label}",
                f"- 严重度：`{severity}`",
                f"- 相关报告类型：`{family_name or 'unknown'}`",
            ]
        )
        if latest_report_id:
            lines.append(f"- 最新相关报告：`{latest_report_id}`，ok=`{latest_ok}`")
        ignored_simulation_count = int(family.get("ignored_simulation_count") or 0) if isinstance(family, dict) else 0
        if ignored_simulation_count:
            lines.append(f"- 已忽略临时模拟报告：`{ignored_simulation_count}`")
        if latest_markdown_path:
            lines.append(f"- 可读报告：`{latest_markdown_path}`")
        if latest_summary:
            lines.append(f"- 最新摘要：`{json.dumps(latest_summary, ensure_ascii=False, sort_keys=True)}`")
        lines.append(f"- 大白话：{real_pressure_gap_plain(gap_key)}")
    return "\n".join(lines) + "\n"


def real_pressure_gap_groups(gaps: list[Any]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for gap in gaps:
        if not isinstance(gap, dict):
            continue
        project = str(gap.get("project") or "unknown")
        grouped.setdefault(project, []).append(str(gap.get("gap") or "unknown"))
    groups: list[dict[str, Any]] = []
    for project in sorted(grouped):
        gap_keys = grouped[project]
        groups.append(
            {
                "project": project,
                "gap_count": len(gap_keys),
                "gaps": gap_keys,
                "plain": real_pressure_gap_group_plain(gap_keys),
            }
        )
    return groups


def real_pressure_gap_group_plain(gap_keys: list[str]) -> str:
    if "duplicate_shadow_risk" in gap_keys or "duplicate_lock_shadow_risk" in gap_keys:
        return "核心像是重复/遮挡治理问题，覆盖、lookup、lock 的失败多半是它带出来的症状。"
    if "missing_consumption_coverage" in gap_keys:
        return "核心是还缺消费证据，先补 lookup/lock 覆盖再谈质量。"
    if "no_repeat_error_ab_report" in gap_keys:
        return "核心是还没用外部 A/B 输出证明菜谱能减少老错误；如果要 claim fresh-agent，还要另有原始对照记录。"
    return "这些缺口还没有归并成明确根因，需要继续看对应报告。"


def real_pressure_gap_plain(gap_key: str) -> str:
    return {
        "missing_consumption_coverage": "这个测试池有正式菜谱，但还没有证明它能被 lookup/lock 消费到。",
        "latest_consumption_coverage_failed": "最新覆盖报告说有菜谱还没被成功找到或锁住。",
        "latest_lookup_pressure_failed": "有查询没有命中预期菜谱，或者命中了不该命中的菜谱。",
        "duplicate_shadow_risk": "两条菜谱太像，系统把本该选的那条盖过去了，需要人工决定合并、拆范围或加优先级。",
        "latest_lock_pressure_failed": "lookup 之后的锁定环节还有失败，不能说执行前锁定链路稳了。",
        "duplicate_lock_shadow_risk": "锁定阶段也被近重复菜谱影响，不能自动决定谁是主菜谱。",
        "no_passed_candidate_quality_report": "候选质量还没有通过，不能把找到资料等同于可用菜谱。",
        "patch_drafts_without_candidate_quality_report": "有补丁草稿，但还没跑质量门。",
        "no_repeat_error_ab_report": "还没提供无菜谱/有菜谱对照输出给 repeat-error-benchmark 评分；如果要 claim fresh-agent，必须另有原始输出证据。",
    }.get(gap_key, "这是一个还没被系统解释清楚的压力缺口，需要看对应 JSON 报告。")


def duplicate_governance_risks(recipes_dir: Path) -> list[dict[str, Any]]:
    reports_dir = recipes_dir / "reports"
    if not reports_dir.exists():
        return []
    risks: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    report_paths = [
        path
        for path in [
            latest_official_pressure_report_path(reports_dir, "lookup_pressure_*.json"),
            latest_official_pressure_report_path(reports_dir, "lock_pressure_*.json"),
        ]
        if path is not None
    ]
    for report_path in report_paths:
        report = read_json(report_path, {})
        if not isinstance(report, dict):
            continue
        action = str(report.get("action") or report_path.stem)
        report_id = str(report.get("report_id") or report_path.stem)
        cases = report.get("cases")
        if not isinstance(cases, list):
            continue
        for case in cases:
            if not isinstance(case, dict):
                continue
            shadow = case.get("shadowed_expected_recipe")
            if not isinstance(shadow, dict):
                continue
            expected_recipe_id = str(shadow.get("expected_recipe_id") or case.get("expected_recipe_id") or "")
            selected_recipe_id = str(shadow.get("selected_recipe_id") or case.get("selected_recipe_id") or "")
            case_id = str(case.get("case_id") or "")
            key = (case_id, expected_recipe_id, selected_recipe_id)
            if key in seen:
                continue
            seen.add(key)
            risks.append(
                {
                    "risk_type": "duplicate_shadow_risk",
                    "case_id": case_id,
                    "query": case.get("query"),
                    "source_action": action,
                    "source_report_id": report_id,
                    "source_report_path": str(report_path),
                    "expected_recipe_id": expected_recipe_id,
                    "selected_recipe_id": selected_recipe_id,
                    "expected_score": shadow.get("expected_score"),
                    "selected_score": shadow.get("selected_score"),
                    "expected_matched_terms": shadow.get("expected_matched_terms") or [],
                    "selected_matched_terms": shadow.get("selected_matched_terms") or [],
                    "recommended_action": "human_governance_required",
                    "candidate_actions": [
                        "merge_or_supersede",
                        "mark_narrow_recipe_evidence_only",
                        "add_explicit_priority_rule",
                        "split_broad_recipe_scope",
                    ],
                    "cannot_claim": [
                        "cannot auto merge or supersede formal recipes",
                        "cannot decide canonical recipe without human governance",
                        "cannot claim large-pool recall quality passed while this risk remains",
                    ],
                }
            )
    return risks


def duplicate_governance_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    risks = report.get("risks") if isinstance(report.get("risks"), list) else []
    decision_matrix = report.get("decision_matrix") if isinstance(report.get("decision_matrix"), list) else []
    candidate_priority_rules = report.get("candidate_priority_rules") if isinstance(report.get("candidate_priority_rules"), list) else []
    validation_cases = report.get("what_if_validation_cases") if isinstance(report.get("what_if_validation_cases"), list) else []
    lines = [
        "# Duplicate Governance Packet",
        "",
        f"Report: `{report.get('report_id')}`",
        "",
        "## 一眼结论",
        "",
        f"- 发现重复遮挡风险：`{summary.get('shadow_risk_count', 0)}` 个",
        f"- 需要人工治理：`{summary.get('human_governance_required', 0)}` 个",
        "- 这不是自动修复包。它只把问题摆出来，不合并、不废弃、不改正式 recipe。",
        "",
        "## 不能 claim",
    ]
    for item in report.get("cannot_claim", []):
        lines.append(f"- {item}")
    lines.extend(["", "## 治理选项矩阵"])
    if decision_matrix:
        for option in decision_matrix:
            lines.extend(
                [
                    "",
                    f"### `{option.get('action')}`",
                    "",
                    f"- 大白话：{option.get('plain')}",
                    f"- 适合什么时候用：{option.get('use_when')}",
                    f"- 会改变什么：{option.get('changes')}",
                    f"- 代价：{option.get('tradeoff')}",
                    f"- 需要人工决定：{plain_bool(option.get('requires_human_decision'))}",
                    f"- 能自动执行：{plain_bool(option.get('can_auto_apply'))}",
                    f"- 不能 claim：{option.get('cannot_claim')}",
                    "- 应用后怎么验收：",
                ]
            )
            for validation in option.get("post_decision_validation", []):
                lines.append(f"  - {validation}")
    else:
        lines.append("- 没有生成治理选项矩阵。")
    lines.extend(["", "## 候选优先级规则草案"])
    if candidate_priority_rules:
        for index, rule in enumerate(candidate_priority_rules, start=1):
            trigger_terms = ", ".join(str(item) for item in rule.get("when_query_contains_any", [])) or "无"
            context_terms = ", ".join(str(item) for item in rule.get("context_terms", [])) or "无"
            lines.extend(
                [
                    "",
                    f"### {index}. `{rule.get('preferred_recipe_id')}`",
                    "",
                    f"- 草案动作：`{rule.get('action')}`",
                    f"- 决策前能不能自动生效：{plain_bool(rule.get('can_auto_apply'))}",
                    f"- 需要人工决定：{plain_bool(rule.get('requires_human_decision'))}",
                    f"- 当 query/task 出现这些区分词时优先它：{trigger_terms}",
                    f"- 上下文词：{context_terms}",
                    f"- 被它覆盖的宽/旧 recipe：`{rule.get('fallback_recipe_id')}`",
                    f"- 来源风险：`{rule.get('risk_case_id')}`",
                    "- 不能 claim：",
                ]
            )
            for claim in rule.get("cannot_claim", []):
                lines.append(f"  - {claim}")
    else:
        lines.append("- 没有生成优先级规则草案。")
    lines.extend(["", "## What-if 验收 case 模板"])
    if report.get("what_if_cases_path"):
        lines.append(f"- 模板 JSON：`{report.get('what_if_cases_path')}`")
    if validation_cases:
        for index, template in enumerate(validation_cases, start=1):
            lines.extend(
                [
                    "",
                    f"### {index}. `{template.get('risk_case_id')}`",
                    "",
                    f"- 候选 recipe：`{', '.join(str(item) for item in template.get('candidate_recipe_ids', []))}`",
                    f"- 决策前能不能直接跑：{plain_bool(template.get('can_run_before_decision'))}",
                    f"- 为什么：{template.get('why_not_runnable_before_decision')}",
                    "- lookup-pressure 模板：",
                    f"  - `{json.dumps(template.get('lookup_pressure_case_template', {}), ensure_ascii=False, sort_keys=True)}`",
                    "- lock-pressure 模板：",
                    f"  - `{json.dumps(template.get('lock_pressure_case_template', {}), ensure_ascii=False, sort_keys=True)}`",
                ]
            )
    else:
        lines.append("- 没有生成 what-if 验收模板。")
    lines.extend(["", "## 待审风险"])
    if not risks:
        lines.extend(
            [
                "",
                "- 这次没有发现 shadow 风险。",
                "- 但这不等于以后不会出现重复菜谱，也不等于真实任务质量通过。",
            ]
        )
        return "\n".join(lines) + "\n"

    for index, risk in enumerate(risks, start=1):
        expected_recipe_id = str(risk.get("expected_recipe_id") or "unknown")
        selected_recipe_id = str(risk.get("selected_recipe_id") or "unknown")
        expected_terms = ", ".join(str(term) for term in risk.get("expected_matched_terms", [])) or "无"
        selected_terms = ", ".join(str(term) for term in risk.get("selected_matched_terms", [])) or "无"
        actions = duplicate_governance_candidate_actions_cn(risk.get("candidate_actions", []))
        lines.extend(
            [
                "",
                f"### {index}. `{expected_recipe_id}` 被 `{selected_recipe_id}` 盖住",
                "",
                f"- 压测 case：`{risk.get('case_id')}`",
                f"- 用户问题：{risk.get('query')}",
                f"- 本来想命中：`{expected_recipe_id}`，分数 `{risk.get('expected_score')}`，命中词：{expected_terms}",
                f"- 实际命中：`{selected_recipe_id}`，分数 `{risk.get('selected_score')}`，命中词：{selected_terms}",
                f"- 来源报告：`{risk.get('source_report_id')}`",
                "- 大白话：这说明两条菜谱覆盖了同一块任务，系统现在分不清谁应该优先。",
                "- 为什么不能自动处理：合并、废弃、拆范围、加优先级都会改变正式菜谱关系，必须人工确认。",
                "- 人工可选动作：",
            ]
        )
        for action in actions:
            lines.append(f"  - {action}")
        cannot_claim = risk.get("cannot_claim", [])
        if cannot_claim:
            lines.append("- 这条不能 claim：")
            for claim in cannot_claim:
                lines.append(f"  - {claim}")
    return "\n".join(lines) + "\n"


def duplicate_governance_candidate_actions_cn(actions: Any) -> list[str]:
    labels = {
        "merge_or_supersede": "合并或废弃其中一条，但必须先确认哪条是主菜谱。",
        "mark_narrow_recipe_evidence_only": "把窄菜谱降成证据/来源线索，不再参与直接 lookup。",
        "add_explicit_priority_rule": "保留两条，但加明确优先级规则，让同类问题知道先选谁。",
        "split_broad_recipe_scope": "拆宽菜谱范围，避免它抢走窄菜谱该管的问题。",
    }
    output: list[str] = []
    for action in (actions if isinstance(actions, list) else []):
        label = labels.get(str(action))
        if label:
            output.append(label)
    return output or ["先人工判断两条菜谱的边界，再选择合并、废弃、拆范围或加优先级。"]


def duplicate_governance_candidate_priority_rules(risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for risk in risks:
        expected_recipe_id = str(risk.get("expected_recipe_id") or "")
        selected_recipe_id = str(risk.get("selected_recipe_id") or "")
        if not expected_recipe_id or not selected_recipe_id:
            continue
        expected_terms = [str(term) for term in risk.get("expected_matched_terms", []) if str(term).strip()]
        selected_terms = [str(term) for term in risk.get("selected_matched_terms", []) if str(term).strip()]
        selected_set = {term.casefold() for term in selected_terms}
        expected_set = {term.casefold() for term in expected_terms}
        discriminators = unique_text([term for term in expected_terms if term.casefold() not in selected_set])
        context_terms = unique_text([term for term in expected_terms if term.casefold() in selected_set])
        rules.append(
            {
                "action": "add_explicit_priority_rule",
                "candidate_only": True,
                "risk_case_id": risk.get("case_id"),
                "source_report_id": risk.get("source_report_id"),
                "preferred_recipe_id": expected_recipe_id,
                "fallback_recipe_id": selected_recipe_id,
                "shadowing_recipe_id": selected_recipe_id,
                "when_query_contains_any": discriminators,
                "context_terms": context_terms,
                "selected_only_terms": unique_text([term for term in selected_terms if term.casefold() not in expected_set]),
                "requires_human_decision": True,
                "can_auto_apply": False,
                "why_candidate": "期望 recipe 也在候选里，但同分或近似同分时被另一条 recipe 盖住。",
                "cannot_claim": [
                    "不能说候选优先级规则已经生效。",
                    "不能说系统已经选择 canonical recipe。",
                    "不能说该草案可以替代人工治理决定。",
                ],
            }
        )
    return rules


def latest_official_pressure_report_path(reports_dir: Path, pattern: str) -> Path | None:
    paths = sorted(reports_dir.glob(pattern)) if reports_dir.exists() else []
    if not paths:
        return None
    records: list[tuple[str, str, Path]] = []
    for path in paths:
        data = read_json(path, {})
        if not isinstance(data, dict):
            continue
        if data.get("official_pressure_evidence") is False or data.get("pressure_evidence_scope") == "external_cases":
            continue
        checked_at = str(data.get("checked_at") or "")
        records.append((checked_at, str(path), path))
    if not records:
        return None
    return sorted(records)[-1][2]


def duplicate_governance_validation_case_templates(risks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    templates: list[dict[str, Any]] = []
    for risk in risks:
        case_id = str(risk.get("case_id") or "shadow_case")
        query = str(risk.get("query") or "")
        expected_recipe_id = str(risk.get("expected_recipe_id") or "")
        selected_recipe_id = str(risk.get("selected_recipe_id") or "")
        candidate_recipe_ids = [item for item in [expected_recipe_id, selected_recipe_id] if item]
        lookup_case = {
            "case_id": f"{case_id}__post_governance_lookup",
            "query": query,
            "expect_applicable": True,
            "expected_recipe_id": GOVERNANCE_EXPECTED_RECIPE_PLACEHOLDER,
            "allowed_candidate_recipe_ids": candidate_recipe_ids,
        }
        lock_case = {
            "case_id": f"{case_id}__post_governance_lock",
            "query": query,
            "task": query,
            "expect_lock": True,
            "expected_recipe_id": GOVERNANCE_EXPECTED_RECIPE_PLACEHOLDER,
            "allowed_candidate_recipe_ids": candidate_recipe_ids,
        }
        templates.append(
            {
                "risk_case_id": case_id,
                "source_report_id": risk.get("source_report_id"),
                "candidate_recipe_ids": candidate_recipe_ids,
                "can_run_before_decision": False,
                "why_not_runnable_before_decision": "expected_recipe_id 必须先由人工治理决定，系统不能自动选 canonical recipe。",
                "lookup_pressure_case_template": lookup_case,
                "lock_pressure_case_template": lock_case,
            }
        )
    return templates


def duplicate_governance_what_if_cases_doc(report: dict[str, Any]) -> dict[str, Any]:
    templates = report.get("what_if_validation_cases") if isinstance(report.get("what_if_validation_cases"), list) else []
    lookup_cases = [template.get("lookup_pressure_case_template") for template in templates if isinstance(template, dict)]
    lock_cases = [template.get("lock_pressure_case_template") for template in templates if isinstance(template, dict)]
    lookup_cases = [case for case in lookup_cases if isinstance(case, dict)]
    lock_cases = [case for case in lock_cases if isinstance(case, dict)]
    return {
        "ok": True,
        "action": "duplicate-governance-what-if-validation-cases",
        "candidate_only": True,
        "source_report_id": report.get("report_id"),
        "can_run_before_decision": False,
        "expected_recipe_id_placeholder": GOVERNANCE_EXPECTED_RECIPE_PLACEHOLDER,
        "fill_required": [
            "lookup_pressure_case_templates.cases[].expected_recipe_id",
            "lock_pressure_case_templates.cases[].expected_recipe_id",
        ],
        "lookup_pressure_case_templates": {"cases": lookup_cases},
        "lock_pressure_case_templates": {"cases": lock_cases},
        "cannot_claim": [
            "不能说 what-if case 模板已经应用治理决策。",
            "不能在 CHOOSE_AFTER_HUMAN_GOVERNANCE 未替换前直接作为通过证据。",
            "不能说生成模板等于 duplicate shadow 已修复。",
        ],
    }


def plain_bool(value: Any) -> str:
    return "是" if bool(value) else "否"


def duplicate_governance_decision_matrix() -> list[dict[str, Any]]:
    return [
        {
            "action": "merge_or_supersede",
            "plain": "合并或废弃其中一条，留下一个主菜谱。",
            "use_when": "两条菜谱讲的是同一件事，只是来源或表述不同。",
            "changes": "会改变正式菜谱关系，可能产生 supersede/tombstone/主菜谱选择。",
            "tradeoff": "最干净，但风险最大；选错主菜谱会丢掉窄规则的来源边界。",
            "requires_human_decision": True,
            "can_auto_apply": False,
            "cannot_claim": "不能自动执行，不能说合并后一定更准。",
            "post_decision_validation": [
                "重新跑 lookup-pressure，原 shadow case 必须命中新 canonical recipe。",
                "重新跑 lock-pressure，原 shadow case 必须能创建/复用正确 lock。",
                "重新跑 consumption-coverage，原缺失 recipe 或 canonical recipe 必须有 lookup/lock 覆盖。",
                "重新跑 real-pressure-summary，相关 duplicate_shadow_risk 必须消失。",
            ],
        },
        {
            "action": "mark_narrow_recipe_evidence_only",
            "plain": "把窄菜谱降成证据线索，不再直接参与 lookup 抢结果。",
            "use_when": "窄菜谱主要证明来源，不应该单独指导执行。",
            "changes": "会改变窄菜谱的消费身份，让它更像来源证据而不是执行菜谱。",
            "tradeoff": "能减少遮挡，但可能让细粒度课程证据不容易被直接找到。",
            "requires_human_decision": True,
            "can_auto_apply": False,
            "cannot_claim": "不能自动执行，不能说证据降级后不会影响召回。",
            "post_decision_validation": [
                "重新跑 lookup-pressure，宽菜谱应命中执行类 query，窄证据不应再抢执行结果。",
                "重新跑 lock-pressure，执行类 query 必须锁到可执行 recipe。",
                "重新跑 consumption-coverage，证据-only 条目不能被当成缺执行覆盖。",
                "重新跑 real-pressure-summary，相关 duplicate_shadow_risk 必须消失或降为证据-only 边界。",
            ],
        },
        {
            "action": "add_explicit_priority_rule",
            "plain": "保留两条菜谱，但写清楚遇到同类 query 时谁优先。",
            "use_when": "两条都还有价值，只是需要明确先后顺序。",
            "changes": "会新增优先级规则或 lookup tie-break 规则。",
            "tradeoff": "最保守，但会让系统多一层治理规则，后续还要持续维护。",
            "requires_human_decision": True,
            "can_auto_apply": False,
            "cannot_claim": "不能自动执行，不能说优先级规则已经代表最终知识结构。",
            "post_decision_validation": [
                "重新跑 lookup-pressure，同分 shadow case 必须按优先级命中预期 recipe。",
                "重新跑 lock-pressure，锁定结果必须跟优先级一致。",
                "重新跑 consumption-coverage，被优先保留的 recipe 必须有 lookup/lock 覆盖。",
                "重新跑 real-pressure-summary，相关 duplicate_shadow_risk 必须消失。",
            ],
        },
        {
            "action": "split_broad_recipe_scope",
            "plain": "拆宽菜谱范围，把窄菜谱该管的部分让出来。",
            "use_when": "宽菜谱有用，但它管得太宽，抢走了更准确的窄菜谱。",
            "changes": "会调整宽菜谱边界，可能新增拆分后的子菜谱。",
            "tradeoff": "长期更清楚，但工作量最大，还需要重新跑 lookup/lock/coverage。",
            "requires_human_decision": True,
            "can_auto_apply": False,
            "cannot_claim": "不能自动执行，不能说拆分后所有大池压测都会自动通过。",
            "post_decision_validation": [
                "重新跑 lookup-pressure，宽/窄 query 必须分别命中拆分后的正确 recipe。",
                "重新跑 lock-pressure，拆分后的执行 recipe 必须能正确 lock，负例不能误 lock。",
                "重新跑 consumption-coverage，拆分后的 active recipes 必须都有覆盖。",
                "重新跑 real-pressure-summary，相关 duplicate_shadow_risk 和 coverage 缺口必须消失。",
            ],
        },
    ]


def latest_report_path(reports_dir: Path, pattern: str) -> Path | None:
    paths = sorted(reports_dir.glob(pattern)) if reports_dir.exists() else []
    if not paths:
        return None
    records: list[tuple[str, str, Path]] = []
    for path in paths:
        data = read_json(path, {})
        checked_at = str(data.get("checked_at") or "") if isinstance(data, dict) else ""
        records.append((checked_at, str(path), path))
    return sorted(records)[-1][2]


def candidate_quality_case(project: RecipesProject, raw_case: Any) -> dict[str, Any]:
    if not isinstance(raw_case, dict):
        return {
            "case_id": "invalid_case",
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["candidate-quality case must be an object."],
        }
    case_id = str(raw_case.get("case_id") or raw_case.get("id") or "unnamed_case")
    review_id = str(raw_case.get("review_id") or "").strip()
    target_recipe_id = str(raw_case.get("target_recipe_id") or "").strip()
    if not review_id and not target_recipe_id:
        return {
            "case_id": case_id,
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["case requires review_id or target_recipe_id."],
        }

    review, review_id = candidate_quality_resolve_review(project, review_id=review_id, target_recipe_id=target_recipe_id)
    if not review:
        return {
            "case_id": case_id,
            "review_id": review_id or None,
            "target_recipe_id": target_recipe_id or None,
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["review item 不存在或无法按 target_recipe_id 定位。"],
        }
    patch_id = str(review.get("proposed_patch_id") or "")
    patch_draft_id = str(review.get("source_patch_draft_id") or "")
    candidate_patch = read_json(project.recipes_dir / "candidates" / f"{patch_id}.json", {}) if patch_id else {}
    patch_draft = read_json(project.recipes_dir / "source_refinery" / "patch_drafts" / f"{patch_draft_id}.json", {}) if patch_draft_id else {}
    if not candidate_patch or not patch_draft:
        return {
            "case_id": case_id,
            "review_id": review_id,
            "target_recipe_id": target_recipe_id or candidate_patch.get("target_recipe_id"),
            "status": "blocked",
            "passed": False,
            "missing_evidence": ["candidate patch 或 patch draft 不存在。"],
        }

    target_recipe_id = str(candidate_patch.get("target_recipe_id") or patch_draft.get("target_recipe_id") or target_recipe_id)
    card_ids = [str(card_id) for card_id in review.get("evidence_refs", []) if str(card_id)]
    if not card_ids:
        card_ids = [str(card_id) for card_id in patch_draft.get("source_card_ids", []) if str(card_id)]
    card_map = {str(card.get("card_id")): card for card in read_jsonl(project.recipes_dir / "source_refinery" / "cards" / "cards.jsonl")}
    cards = [card_map[card_id] for card_id in card_ids if card_id in card_map]

    required_terms = [str(term) for term in raw_case.get("required_terms", []) if str(term).strip()]
    required_proposed_terms = [str(term) for term in raw_case.get("required_proposed_terms", []) if str(term).strip()]
    forbidden_terms = [str(term) for term in raw_case.get("forbidden_terms", []) if str(term).strip()]
    forbidden_proposed_terms = [str(term) for term in raw_case.get("forbidden_proposed_terms", []) if str(term).strip()]
    required_source_paths = [str(term) for term in raw_case.get("required_source_paths", []) if str(term).strip()]
    forbidden_source_paths = [str(term) for term in raw_case.get("forbidden_source_paths", []) if str(term).strip()]
    min_card_count = int(raw_case.get("min_card_count") or 1)
    min_proposed_value_count = raw_case.get("min_proposed_value_count")
    min_proposed_value_count_int = int(min_proposed_value_count) if min_proposed_value_count is not None else None
    max_proposed_value_count = raw_case.get("max_proposed_value_count")
    max_proposed_value_count_int = int(max_proposed_value_count) if max_proposed_value_count is not None else None
    expected_review_status = str(raw_case.get("expected_review_status") or "pending")
    allow_formal_recipe_exists = bool(raw_case.get("allow_formal_recipe_exists"))
    allow_missing_plain_summary = bool(raw_case.get("allow_missing_plain_summary"))

    quality_blob_raw = stable_json({"review": review, "candidate_patch": candidate_patch, "patch_draft": patch_draft, "cards": cards})
    quality_blob = quality_blob_raw.casefold()
    normalized_quality_blob = candidate_quality_normalized_text(quality_blob_raw)
    proposed_blob_raw = stable_json(candidate_patch.get("proposed_change", {}))
    proposed_blob = proposed_blob_raw.casefold()
    normalized_proposed_blob = candidate_quality_normalized_text(proposed_blob_raw)
    matched_required = [term for term in required_terms if candidate_quality_text_contains(quality_blob, normalized_quality_blob, term)]
    missing_required = [term for term in required_terms if not candidate_quality_text_contains(quality_blob, normalized_quality_blob, term)]
    matched_required_proposed = [term for term in required_proposed_terms if candidate_quality_text_contains(proposed_blob, normalized_proposed_blob, term)]
    missing_required_proposed = [term for term in required_proposed_terms if not candidate_quality_text_contains(proposed_blob, normalized_proposed_blob, term)]
    matched_forbidden = [term for term in forbidden_terms if candidate_quality_text_contains(quality_blob, normalized_quality_blob, term)]
    matched_forbidden_proposed = [
        term for term in forbidden_proposed_terms if candidate_quality_text_contains(proposed_blob, normalized_proposed_blob, term)
    ]
    source_paths = candidate_quality_source_paths(cards)
    matched_source_paths = [term for term in required_source_paths if any(term in path for path in source_paths)]
    missing_source_paths = [term for term in required_source_paths if term not in matched_source_paths]
    matched_forbidden_source_paths = [term for term in forbidden_source_paths if any(term in path for path in source_paths)]
    review_hints = review.get("review_hints") if isinstance(review.get("review_hints"), dict) else {}
    proposed_hint = review_hints.get("proposed_value_count")
    proposed_value_count = int(proposed_hint) if proposed_hint is not None else candidate_quality_proposed_value_count(candidate_patch)
    plain_language_summary_present = isinstance(review.get("plain_language_summary"), dict) and bool(review.get("plain_language_summary"))
    formal_recipe_exists = recipe_exists(project.recipes_dir, target_recipe_id) if target_recipe_id else False

    all_cards_have_source_trace = bool(cards) and all(bool(card.get("source_trace")) for card in cards)
    invalid_claim_limit_card_ids = [
        str(card.get("card_id") or "")
        for card in cards
        if not candidate_quality_card_has_claim_limits(card)
    ]
    all_cards_have_claim_limits = bool(cards) and not invalid_claim_limit_card_ids
    all_cards_candidate = bool(cards) and all(str(card.get("evidence_strength")) == "candidate" for card in cards)
    failures: list[str] = []
    if review.get("status") != expected_review_status:
        failures.append(f"review status expected {expected_review_status}, got {review.get('status')}")
    if not plain_language_summary_present and not allow_missing_plain_summary:
        failures.append("review missing plain_language_summary")
    if formal_recipe_exists and not allow_formal_recipe_exists:
        failures.append(f"formal recipe unexpectedly exists: {target_recipe_id}")
    if len(cards) < min_card_count:
        failures.append(f"card count below minimum: {len(cards)} / {min_card_count}")
    for term in missing_required:
        failures.append(f"missing required term: {term}")
    for term in missing_required_proposed:
        failures.append(f"missing required proposed term: {term}")
    for term in matched_forbidden:
        failures.append(f"matched forbidden term: {term}")
    for term in matched_forbidden_proposed:
        failures.append(f"matched forbidden proposed term: {term}")
    for term in missing_source_paths:
        failures.append(f"missing required source path: {term}")
    for term in matched_forbidden_source_paths:
        failures.append(f"matched forbidden source path: {term}")
    if max_proposed_value_count_int is not None and proposed_value_count > max_proposed_value_count_int:
        failures.append(f"proposed value count too high: {proposed_value_count} / {max_proposed_value_count_int}")
    if min_proposed_value_count_int is not None and proposed_value_count < min_proposed_value_count_int:
        failures.append(f"proposed value count below minimum: {proposed_value_count} / {min_proposed_value_count_int}")
    if not all_cards_have_source_trace:
        failures.append("not all cards have source_trace")
    if not all_cards_have_claim_limits:
        failures.append("not all cards have useful cannot_claim")
    if not all_cards_candidate:
        failures.append("not all cards keep evidence_strength=candidate")

    passed = not failures
    return {
        "case_id": case_id,
        "review_id": review_id,
        "patch_id": patch_id,
        "patch_draft_id": patch_draft_id,
        "target_recipe_id": target_recipe_id or None,
        "status": "passed" if passed else "failed",
        "passed": passed,
        "review_status": review.get("status"),
        "plain_language_summary_present": plain_language_summary_present,
        "allow_missing_plain_summary": allow_missing_plain_summary,
        "formal_recipe_exists": formal_recipe_exists,
        "allow_formal_recipe_exists": allow_formal_recipe_exists,
        "card_count": len(cards),
        "min_card_count": min_card_count,
        "proposed_value_count": proposed_value_count,
        "min_proposed_value_count": min_proposed_value_count_int,
        "max_proposed_value_count": max_proposed_value_count_int,
        "required_terms": required_terms,
        "matched_required_terms": matched_required,
        "missing_required_terms": missing_required,
        "required_proposed_terms": required_proposed_terms,
        "matched_required_proposed_terms": matched_required_proposed,
        "missing_required_proposed_terms": missing_required_proposed,
        "forbidden_terms": forbidden_terms,
        "matched_forbidden_terms": matched_forbidden,
        "forbidden_proposed_terms": forbidden_proposed_terms,
        "matched_forbidden_proposed_terms": matched_forbidden_proposed,
        "required_source_paths": required_source_paths,
        "matched_source_paths": matched_source_paths,
        "missing_source_paths": missing_source_paths,
        "forbidden_source_paths": forbidden_source_paths,
        "matched_forbidden_source_paths": matched_forbidden_source_paths,
        "all_cards_have_source_trace": all_cards_have_source_trace,
        "all_cards_have_claim_limits": all_cards_have_claim_limits,
        "invalid_claim_limit_card_ids": invalid_claim_limit_card_ids,
        "all_cards_candidate": all_cards_candidate,
        "failure_reasons": failures,
        "cannot_claim": [
            "不能说候选质量压测通过就等于人工 review 通过。",
            "不能说 pending review 已经写入正式 recipe。",
        ],
    }


def candidate_quality_resolve_review(
    project: RecipesProject,
    *,
    review_id: str,
    target_recipe_id: str,
) -> tuple[dict[str, Any], str]:
    if review_id:
        return read_json(project.recipes_dir / "review_queue" / f"{review_id}.json", {}), review_id
    matches: list[tuple[str, dict[str, Any]]] = []
    for review_path in sorted((project.recipes_dir / "review_queue").glob("*.json")):
        review = read_json(review_path, {})
        patch_id = str(review.get("proposed_patch_id") or "")
        candidate_patch = read_json(project.recipes_dir / "candidates" / f"{patch_id}.json", {}) if patch_id else {}
        if candidate_patch.get("target_recipe_id") == target_recipe_id:
            matches.append((str(review.get("review_id") or review_path.stem), review))
    if not matches:
        return {}, ""
    return matches[-1][1], matches[-1][0]


def candidate_quality_source_paths(cards: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for card in cards:
        for trace in card.get("source_trace", []) or []:
            path = str(trace.get("path") or "")
            if path:
                paths.append(path)
    return unique_text(paths)


def candidate_quality_card_has_claim_limits(card: dict[str, Any]) -> bool:
    limits = normalize_string_list(card.get("cannot_claim"))
    return any(claim_limit_is_useful(limit) for limit in limits)


def candidate_quality_proposed_value_count(candidate_patch: dict[str, Any]) -> int:
    proposed = candidate_patch.get("proposed_change", {})
    if not isinstance(proposed, dict):
        return 0
    count = 0
    for value in proposed.values():
        if isinstance(value, list):
            count += len(value)
    return count


def candidate_quality_normalized_text(text: str) -> str:
    return re.sub(r"[^\w\u4e00-\u9fff]+", " ", text.casefold()).strip()


def candidate_quality_text_contains(raw_blob: str, normalized_blob: str, term: str) -> bool:
    raw_term = term.casefold()
    if raw_term in raw_blob:
        return True
    normalized_term = candidate_quality_normalized_text(term)
    return bool(normalized_term and normalized_term in normalized_blob)


def candidate_quality_missing_evidence(cases: list[dict[str, Any]]) -> list[str]:
    missing: list[str] = []
    for case in cases:
        if case.get("status") == "blocked":
            missing.extend(str(item) for item in case.get("missing_evidence", []) if item)
        if case.get("status") == "failed":
            missing.append(f"candidate-quality case failed: {case.get('case_id')}")
            missing.extend(str(item) for item in case.get("failure_reasons", []) if item)
    return unique_text(missing)


def normalize_candidate_fields(fields: list[str]) -> list[str]:
    normalized: list[str] = []
    for field in fields:
        for part in field.split(","):
            key = normalize_field_key(part)
            value = FIELD_ALIASES.get(key, key)
            if value and value not in normalized:
                normalized.append(value)
    return normalized


def infer_candidate_fields(text: str) -> list[str]:
    parsed = parse_field_blocks(text)
    fields: list[str] = []
    for raw_field in parsed:
        if raw_field == "card_type":
            continue
        field = FIELD_ALIASES.get(raw_field, raw_field)
        if field == "stop_line":
            field = "checklist_item"
        if field and field not in fields:
            fields.append(field)
    lowered = normalize_problem(text)
    keyword_fields = {
        "forbidden_path": [
            "forbidden",
            "禁止",
            "不能",
            "不要",
            "避免",
            "不得",
            "不该",
            "failed_path",
            "wrong behavior",
            "reject_if",
            "hard_rejects",
            "motion_rejects",
            "avoid",
            "must not",
            "do not",
            "no new visual",
            "no motion render",
            "no remotion",
            "no provider",
            "no real local_storage",
            "no pass",
        ],
        "failure_signals": [
            "failure_signal",
            "失败信号",
            "失败",
            "看不清",
            "被遮挡",
            "挡脸",
            "挡字幕",
            "抢视线",
            "wrong behavior",
            "reject_if",
            "hard_rejects",
            "motion_rejects",
            "pasted sticker",
            "visible halo",
        ],
        "verified_path": [
            "verified",
            "verification",
            "跑通",
            "outputs",
            "success_means",
            "track_readback",
            "timeline_report",
            "run_receipt",
            "timeline_name",
            "local_artifacts",
            "learning_material_info_cards",
            "learning_material_experience_index",
            "p1_failure_to_experience_map",
            "material_deep_deconstruction_notes",
        ],
        "checklist_item": [
            "checklist",
            "检查",
            "action_change",
            "correct behavior",
            "check:",
            "requirements",
            "review_questions",
            "principle",
            "z_order_bottom_to_top",
        ],
        "visual_check": [
            "visual",
            "截图",
            "小窗",
            "强字卡",
            "重点字卡",
            "标题卡",
            "字卡",
            "大字",
            "首帧",
            "花字",
            "字幕",
            "文字图层",
            "图层",
            "透明度",
            "不透明度",
            "关键帧",
            "阴影",
            "颜色区分",
            "颜色",
            "位置参数",
            "Y轴",
            "y轴",
            "入场动画",
            "动画效果",
            "蒙版",
            "抠像",
            "描边",
            "遮挡",
            "前景",
            "背景",
            "安全区",
            "pip",
            "typography",
            "subtitle",
            "presenter",
            "matte",
            "opacity",
            "font_size",
            "color_relation",
            "placement",
            "layer_id",
            "z_order",
            "keyframe",
            "easing",
            "canvas",
            "visual_strategy",
            "caption_strategy",
            "keyword_card",
            "card_fullscreen",
            "big_subtitle",
            "template_card",
            "template_label",
            "role_label",
            "source_kind",
        ],
        "cannot_claim": ["cannot_claim", "不能说"],
        "pressure_test": ["pressure_test", "验收", "blocked_until_still_frame_review_passes", "future_motion_test"],
    }
    for field, tokens in keyword_fields.items():
        if any(token.casefold() in lowered for token in tokens) and field not in fields:
            fields.append(field)
    return fields


def source_trace_for_record(record: dict[str, Any]) -> list[dict[str, Any]]:
    trace = {
        "record_type": record.get("record_type"),
        "record_id": record.get("record_id"),
        "source_id": record.get("source_id"),
        "path": record.get("path"),
    }
    if record.get("start"):
        trace["timestamp_start"] = record.get("start")
    if record.get("end"):
        trace["timestamp_end"] = record.get("end")
    return [trace]


CARD_TYPES = {
    "correction_card",
    "run_chain_card",
    "failure_card",
    "learning_atom_card",
    "visual_example_card",
}


def infer_card_type(text: str) -> str:
    parsed = parse_field_blocks(text)
    declared = (parsed.get("card_type") or [""])[0].strip()
    if declared in CARD_TYPES:
        return declared
    lowered = normalize_problem(text)
    if parsed.get("wrong_behavior") or parsed.get("correct_behavior") or parsed.get("check"):
        return "correction_card"
    if "before:" in lowered or "correction:" in lowered:
        return "correction_card"
    if "wrong behavior:" in lowered or "correct behavior:" in lowered:
        return "correction_card"
    if "failed_path:" in lowered or "failure_signal:" in lowered:
        return "failure_card"
    if "visual_check:" in lowered or "image_path:" in lowered:
        return "visual_example_card"
    if "steps:" in lowered or "outputs:" in lowered or "verification:" in lowered or looks_like_run_chain_evidence(lowered):
        return "run_chain_card"
    return "learning_atom_card"


def looks_like_run_chain_evidence(lowered_text: str) -> bool:
    run_identity_tokens = [
        '"run_id"',
        "run_id:",
        "run id",
        '"timeline_name"',
        "timeline_name:",
        "timeline name",
    ]
    run_artifact_tokens = [
        '"track_readback"',
        "track_readback:",
        '"success_means"',
        "success_means:",
        '"local_artifacts"',
        "local_artifacts:",
        '"timeline_report"',
        "timeline_report:",
        '"asset_manifest"',
        "asset_manifest:",
        '"run_receipt"',
        "run_receipt:",
    ]
    claim_boundary_tokens = [
        '"does_not_prove"',
        "does_not_prove:",
        '"cannot_claim"',
        "cannot_claim:",
        "no export",
        "pending user review",
        "不能说",
    ]
    has_identity = any(token in lowered_text for token in run_identity_tokens)
    artifact_hits = sum(1 for token in run_artifact_tokens if token in lowered_text)
    has_artifact = artifact_hits > 0
    has_boundary = any(token in lowered_text for token in claim_boundary_tokens)
    return (has_identity and has_artifact) or (has_artifact and has_boundary) or artifact_hits >= 2


def card_dir_for_type(card_type: str) -> str:
    return {
        "correction_card": "correction_cards",
        "run_chain_card": "run_chain_cards",
        "failure_card": "failure_cards",
        "learning_atom_card": "learning_atom_cards",
        "visual_example_card": "visual_example_cards",
    }[card_type]


def merge_cards_by_id(existing_cards: list[dict[str, Any]], new_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for card in existing_cards + new_cards:
        card_id = str(card.get("card_id") or "")
        if not card_id:
            continue
        if card_id not in merged:
            order.append(card_id)
        merged[card_id] = card
    return [merged[card_id] for card_id in order]


def latest_cards_for_target(recipes_dir: Path, target_recipe_id: str, all_target_cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest = read_json(recipes_dir / "source_refinery" / "cards" / "latest.json", {})
    latest_card_ids = {str(card_id) for card_id in latest.get("card_ids", []) if card_id}
    if not latest_card_ids:
        return all_target_cards
    latest_cards = [card for card in all_target_cards if str(card.get("card_id")) in latest_card_ids]
    return latest_cards or all_target_cards


def knowledge_fusion_candidates(cards: list[dict[str, Any]], target_recipe_id: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for card in cards:
        if card_needs_deep_read(card):
            candidates.append(
                knowledge_fusion_candidate(
                    "needs_deep_read",
                    target_recipe_id,
                    [card],
                    reason=f"Card {card.get('card_id')} has incomplete evidence and must not be promoted from shallow context.",
                    details={
                        "missing_evidence": card_payload_values(card, "missing_evidence"),
                        "next_deep_read_target": card_payload_values(card, "next_deep_read_target"),
                    },
                )
            )
        if card_has_explicit_conflict(card):
            candidates.append(
                knowledge_fusion_candidate(
                    "conflict_candidate",
                    target_recipe_id,
                    [card],
                    reason=f"Card {card.get('card_id')} declares or describes a conflict; preserve source traces for review.",
                    details={"conflict": card_payload_values(card, "conflict") or card_payload_values(card, "conflicts_with")},
                )
            )

    checklist_groups: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        for value in card_payload_values(card, "checklist_item"):
            key = normalize_fusion_text(value)
            if key:
                checklist_groups.setdefault(key, []).append(card)
    for checklist, group in checklist_groups.items():
        source_ids = {trace.get("source_id") or trace.get("path") for card in group for trace in card.get("source_trace", [])}
        if len(group) >= 2 and len(source_ids) >= 2:
            candidates.append(
                knowledge_fusion_candidate(
                    "merge_candidate",
                    target_recipe_id,
                    group,
                    reason=f"Multiple sources support the same checklist candidate: {checklist}",
                    details={"shared_checklist": checklist, "source_count": len(source_ids)},
                )
            )

    concept_groups: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        concepts = card_payload_values(card, "concept") or inferred_concepts_from_card(card)
        for concept in concepts:
            key = normalize_fusion_text(concept)
            if key:
                concept_groups.setdefault(key, []).append(card)
    for concept, group in concept_groups.items():
        use_whens = unique_fusion_values([value for card in group for value in card_payload_values(card, "use_when")])
        if len(group) >= 2 and len(use_whens) >= 2:
            candidates.append(
                knowledge_fusion_candidate(
                    "split_candidate",
                    target_recipe_id,
                    group,
                    reason=f"{concept} appears with different use_when branches and should not be merged into one universal recipe.",
                    details={"concept": concept, "use_when": use_whens},
                )
            )
    source_ids = {
        str(trace.get("source_id") or trace.get("path") or "")
        for card in cards
        for trace in card.get("source_trace", [])
        if trace.get("source_id") or trace.get("path")
    }
    has_broad_deep_read = any(
        candidate.get("candidate_type") == "needs_deep_read"
        and isinstance(candidate.get("details"), dict)
        and candidate["details"].get("card_count") == len(cards)
        for candidate in candidates
    )
    if len(cards) >= 8 and len(source_ids) >= 3 and not has_broad_deep_read:
        candidates.append(
            knowledge_fusion_candidate(
                "needs_deep_read",
                target_recipe_id,
                cards,
                reason="Candidate set is too broad across multiple sources; run targeted deep read before merge or split.",
                details={"card_count": len(cards), "source_count": len(source_ids)},
            )
        )
    return dedupe_fusion_candidates(candidates)


def knowledge_fusion_candidate(
    candidate_type: str,
    target_recipe_id: str,
    cards: list[dict[str, Any]],
    *,
    reason: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_trace = combined_source_trace(cards)
    card_ids = [str(card.get("card_id")) for card in cards if card.get("card_id")]
    return {
        "fusion_candidate_id": make_id("fusion_candidate", candidate_type, target_recipe_id, card_ids, reason, details or {}),
        "candidate_type": candidate_type,
        "target_recipe_id": target_recipe_id,
        "source_card_ids": card_ids,
        "source_trace": source_trace,
        "reason": reason,
        "details": details or {},
        "evidence_strength": "candidate",
        "status": "candidate",
        "review_required": True,
        "cannot_claim": [
            "不能说 knowledge_fusion candidate 已经修改正式 recipe。",
            "不能说 knowledge_fusion candidate 已经通过用户审查。",
        ],
    }


def deep_read_tasks_from_candidate(recipes_dir: Path, fusion_id: str, target_recipe_id: str, candidate: dict[str, Any]) -> list[dict[str, Any]]:
    traces = [trace for trace in candidate.get("source_trace", []) if isinstance(trace, dict)]
    paths = unique_text([str(trace.get("path") or "") for trace in traces if str(trace.get("path") or "").strip()])
    if len(paths) < 2:
        return [deep_read_task_from_candidate(recipes_dir, fusion_id, target_recipe_id, candidate)]

    tasks: list[dict[str, Any]] = []
    for path in paths:
        path_traces = [trace for trace in traces if str(trace.get("path") or "") == path]
        card_ids = source_card_ids_for_path(recipes_dir, candidate, path)
        tasks.append(
            deep_read_task_from_candidate(
                recipes_dir,
                fusion_id,
                target_recipe_id,
                candidate,
                traces_override=path_traces,
                source_card_ids_override=card_ids,
            )
        )
    return tasks


def source_card_ids_for_path(recipes_dir: Path, candidate: dict[str, Any], path: str) -> list[str]:
    matched: list[str] = []
    for card_id in [str(card_id) for card_id in candidate.get("source_card_ids", []) if card_id]:
        card_doc = read_card_by_id(recipes_dir, card_id)
        for trace in card_doc.get("source_trace", []) or []:
            if isinstance(trace, dict) and str(trace.get("path") or "") == path:
                matched.append(card_id)
                break
    return matched or [str(card_id) for card_id in candidate.get("source_card_ids", []) if card_id]


def deep_read_task_from_candidate(
    recipes_dir: Path,
    fusion_id: str,
    target_recipe_id: str,
    candidate: dict[str, Any],
    *,
    traces_override: list[dict[str, Any]] | None = None,
    source_card_ids_override: list[str] | None = None,
) -> dict[str, Any]:
    traces = traces_override if traces_override is not None else [trace for trace in candidate.get("source_trace", []) if isinstance(trace, dict)]
    source_path_contains = unique_text(
        [
            Path(str(trace.get("path") or "")).name
            for trace in traces
            if str(trace.get("path") or "").strip()
        ]
    )
    source_card_ids = source_card_ids_override if source_card_ids_override is not None else [str(card_id) for card_id in candidate.get("source_card_ids", []) if card_id]
    details = candidate.get("details", {}) if isinstance(candidate.get("details"), dict) else {}
    next_targets = normalize_string_list(details.get("next_deep_read_target"))
    query_parts = unique_text(
        [
            str(candidate.get("reason") or ""),
            *next_targets,
            *source_path_contains,
            target_recipe_id,
        ]
    )
    query = " ".join(part for part in query_parts if part).strip() or target_recipe_id
    task_id = make_id("deep_read_task", fusion_id, candidate.get("fusion_candidate_id"), source_path_contains, source_card_ids)
    candidate_fields = deep_read_candidate_fields(recipes_dir, source_card_ids)
    knowledge_need_id = f"KN_DEEP_READ_{sha256_text(task_id)[:8]}"
    return {
        "task_id": task_id,
        "fusion_candidate_id": candidate.get("fusion_candidate_id"),
        "target_recipe_id": target_recipe_id,
        "knowledge_need_id": knowledge_need_id,
        "query": query,
        "candidate_fields": candidate_fields,
        "source_path_contains": source_path_contains,
        "source_card_ids": source_card_ids,
        "source_trace": traces,
        "next_command": "self-run-benchmark",
        "command_args": {
            "query": query,
            "knowledge_need_id": knowledge_need_id,
            "target_recipe_id": target_recipe_id,
            "candidate_fields": candidate_fields,
            "source_path_contains": source_path_contains,
            "scan_depth": "medium",
            "kind": "source",
        },
        "cannot_claim": [
            "不能说 deep-read task 已经执行。",
            "不能说 deep-read task 已经写入正式 recipe。",
        ],
    }


def deep_read_candidate_fields(recipes_dir: Path, source_card_ids: list[str]) -> list[str]:
    inherited: list[str] = []
    for card_id in source_card_ids:
        card_doc = read_card_by_id(recipes_dir, card_id)
        inherited.extend(str(field) for field in card_doc.get("target_fields", []) if field)
    fallback = [
        "title",
        "use_when",
        "steps",
        "checklist_item",
        "failure_mode",
        "source_truth_to_read",
        "cannot_claim",
        "missing_evidence",
    ]
    return normalize_candidate_fields(inherited + fallback)


def target_suggestion_groups_from_review(recipes_dir: Path, review: dict[str, Any], patch: dict[str, Any]) -> list[dict[str, Any]]:
    parent_target_id = str(patch.get("target_recipe_id") or "")
    patch_id = str(patch.get("patch_id") or review.get("proposed_patch_id") or "")
    review_id = str(review.get("review_id") or "")
    source_card_ids = [str(card_id) for card_id in patch.get("source_card_ids", []) if card_id]
    card_docs = [read_card_by_id(recipes_dir, card_id) for card_id in source_card_ids]
    card_docs = [card for card in card_docs if card]
    for candidate in patch.get("fusion_candidates", []) or []:
        if not isinstance(candidate, dict):
            continue
        source_card_ids.extend(str(card_id) for card_id in candidate.get("source_card_ids", []) if card_id)
    groups: dict[str, dict[str, Any]] = {}

    def ensure_group(source_path_contains: str) -> dict[str, Any]:
        return groups.setdefault(
            source_path_contains,
            {
                "parent_target_recipe_id": parent_target_id,
                "source_path_contains": source_path_contains,
                "source_paths": [],
                "review_ids": [],
                "patch_ids": [],
                "source_card_ids": [],
                "candidate_fields": [],
                "decision_reasons": [],
                "proposed_value_count": 0,
                "_cards": [],
            },
        )

    for card_doc in card_docs:
        for trace in card_doc.get("source_trace", []) or []:
            if not isinstance(trace, dict):
                continue
            path = str(trace.get("path") or "").strip()
            if not path:
                continue
            source_path_contains = Path(path).name
            group = ensure_group(source_path_contains)
            group["source_paths"].append(path)
            group["source_card_ids"].append(str(card_doc.get("card_id") or ""))
            group["_cards"].append(card_doc)

    if not groups:
        return []

    single_source_group = len(groups) == 1
    decision_reason = str(review.get("decision_reason") or review.get("recommendation") or patch.get("reason") or "").strip()
    for group in groups.values():
        group_cards = [card for card in group.get("_cards", []) if isinstance(card, dict)]
        group["review_ids"].append(review_id)
        group["patch_ids"].append(patch_id)
        group["candidate_fields"].extend(target_suggestion_candidate_fields(group_cards, patch))
        if decision_reason:
            group["decision_reasons"].append(decision_reason)
        if single_source_group:
            group["proposed_value_count"] = target_suggestion_proposed_value_count(review, patch)
        else:
            group["proposed_value_count"] = target_suggestion_card_value_count(group_cards)
        group.pop("_cards", None)
    return list(groups.values())


def target_suggestion_candidate_fields(cards: list[dict[str, Any]], patch: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for card in cards:
        fields.extend(str(field) for field in card.get("target_fields", []) if field)
    proposed = patch.get("proposed_change", {}) if isinstance(patch.get("proposed_change"), dict) else {}
    for key, value in proposed.items():
        if text_values(value):
            fields.append(str(key))
    fallback = [
        "title",
        "use_when",
        "steps",
        "checklist_item",
        "verified_path",
        "failure_signals",
        "source_truth_to_read",
        "cannot_claim",
        "missing_evidence",
    ]
    return normalize_candidate_fields(fields + fallback)


def target_suggestion_proposed_value_count(review: dict[str, Any], patch: dict[str, Any]) -> int:
    hints = review.get("review_hints") if isinstance(review.get("review_hints"), dict) else {}
    proposed_hint = hints.get("proposed_value_count")
    if proposed_hint is not None:
        try:
            return int(proposed_hint)
        except (TypeError, ValueError):
            return 0
    if patch.get("patch_type") == "knowledge_fusion_candidate_set":
        return len([candidate for candidate in patch.get("fusion_candidates", []) if isinstance(candidate, dict)])
    return candidate_quality_proposed_value_count(patch)


def target_suggestion_card_value_count(cards: list[dict[str, Any]]) -> int:
    count = 0
    for card in cards:
        payload = card.get("extracted_payload", {}) if isinstance(card.get("extracted_payload"), dict) else {}
        for value in payload.values():
            count += len(text_values(value))
    if count:
        return count
    return len([card for card in cards if card.get("source_quote")])


def target_suggestion_from_group(group: dict[str, Any]) -> dict[str, Any]:
    parent_target_id = str(group.get("parent_target_recipe_id") or "recipe_candidate_parent")
    source_path_contains = str(group.get("source_path_contains") or "")
    source_slug = ascii_slug(Path(source_path_contains).stem or source_path_contains)
    suggested_target_recipe_id = f"{parent_target_id}__narrow_{source_slug}"
    review_ids = unique_text([str(item) for item in group.get("review_ids", []) if item])
    patch_ids = unique_text([str(item) for item in group.get("patch_ids", []) if item])
    source_paths = unique_text([str(item) for item in group.get("source_paths", []) if item])
    source_card_ids = unique_text([str(item) for item in group.get("source_card_ids", []) if item])
    candidate_fields = normalize_candidate_fields([str(item) for item in group.get("candidate_fields", []) if item])
    decision_reasons = unique_text([str(item) for item in group.get("decision_reasons", []) if item])
    knowledge_need_id = f"KN_NARROW_TARGET_{sha256_text(suggested_target_recipe_id)[:8].upper()}"
    query = target_suggestion_query(parent_target_id, source_path_contains, candidate_fields, decision_reasons)
    suggestion_id = make_id(
        "target_suggestion",
        parent_target_id,
        source_path_contains,
        review_ids,
        patch_ids,
        candidate_fields,
    )
    return {
        "suggestion_id": suggestion_id,
        "candidate_only": True,
        "parent_target_recipe_id": parent_target_id,
        "suggested_target_recipe_id": suggested_target_recipe_id,
        "knowledge_need_id": knowledge_need_id,
        "query": query,
        "source_path_contains": [source_path_contains],
        "source_paths": source_paths,
        "review_ids": review_ids,
        "patch_ids": patch_ids,
        "source_card_ids": source_card_ids,
        "review_count": len(review_ids),
        "proposed_value_count": int(group.get("proposed_value_count") or 0),
        "candidate_fields": candidate_fields,
        "decision_reasons": decision_reasons,
        "reason": "Review history points to a narrower source-scoped target; rerun the chain on this source path before any formal recipe decision.",
        "next_command": "self-run-benchmark",
        "command_args": {
            "query": query,
            "knowledge_need_id": knowledge_need_id,
            "target_recipe_id": suggested_target_recipe_id,
            "candidate_fields": candidate_fields,
            "source_path_contains": [source_path_contains],
            "scan_depth": "medium",
            "kind": "source",
            "min_cards": 1,
        },
        "cannot_claim": [
            "不能说 target suggestion 已经执行。",
            "不能说 target suggestion 已经生成正式 recipe。",
            "不能说 rejected review 里的证据已经被自动判定为正确。",
        ],
    }


def target_suggestion_query(
    parent_target_recipe_id: str,
    source_path_contains: str,
    candidate_fields: list[str],
    decision_reasons: list[str],
) -> str:
    parts = unique_text(
        [
            parent_target_recipe_id,
            source_path_contains,
            *candidate_fields[:8],
        ]
    )
    return " ".join(parts).strip()


def ascii_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", value.casefold()).strip("_")
    if not cleaned:
        cleaned = sha256_text(value)[:8]
    return cleaned[:64].strip("_") or sha256_text(value)[:8]


def review_triage_records(
    recipes_dir: Path,
    events: list[dict[str, Any]],
    *,
    status: str,
    target_recipe_id: str | None,
    target_prefix: str | None,
    latest_per_target: bool,
) -> list[dict[str, Any]]:
    order = review_event_order(events)
    records: list[dict[str, Any]] = []
    for review_path in sorted((recipes_dir / "review_queue").glob("*.json")):
        review = read_json(review_path, {})
        review_id = str(review.get("review_id") or review_path.stem)
        review_status = str(review.get("status") or "")
        if status != "all" and review_status != status:
            continue
        patch_id = str(review.get("proposed_patch_id") or "")
        patch = read_json(recipes_dir / "candidates" / f"{patch_id}.json", {}) if patch_id else {}
        if not patch:
            continue
        target = str(patch.get("target_recipe_id") or "")
        if target_recipe_id and target != target_recipe_id:
            continue
        if target_prefix and not target.startswith(target_prefix):
            continue
        records.append(
            {
                "review_id": review_id,
                "review": review,
                "patch_id": patch_id,
                "patch": patch,
                "target_recipe_id": target,
                "order": order.get(review_id, 0),
                "review_path": str(review_path),
            }
        )
    if not latest_per_target:
        return sorted(records, key=lambda item: (str(item["target_recipe_id"]), int(item["order"]), str(item["review_id"])))
    by_target: dict[str, dict[str, Any]] = {}
    for record in records:
        target = str(record.get("target_recipe_id") or "")
        current = by_target.get(target)
        if current is None or (int(record.get("order") or 0), str(record.get("review_id") or "")) >= (
            int(current.get("order") or 0),
            str(current.get("review_id") or ""),
        ):
            by_target[target] = record
    return sorted(by_target.values(), key=lambda item: str(item.get("target_recipe_id") or ""))


def review_event_order(events: list[dict[str, Any]]) -> dict[str, int]:
    order: dict[str, int] = {}
    for event in events:
        seq = int(event.get("seq") or 0)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        review_id = str(payload.get("review_id") or "")
        if review_id:
            order[review_id] = seq
        for created in payload.get("created", []) if isinstance(payload.get("created"), list) else []:
            if isinstance(created, dict) and created.get("review_id"):
                order[str(created["review_id"])] = seq
    return order


def review_triage_item(record: dict[str, Any], *, min_values: int, max_values: int) -> dict[str, Any]:
    recipes_dir = Path(record["review_path"]).parents[1]
    review = record.get("review", {}) if isinstance(record.get("review"), dict) else {}
    patch = record.get("patch", {}) if isinstance(record.get("patch"), dict) else {}
    patch_draft_id = str(review.get("source_patch_draft_id") or patch.get("source_patch_draft_id") or "")
    patch_draft = read_json(recipes_dir / "source_refinery" / "patch_drafts" / f"{patch_draft_id}.json", {}) if patch_draft_id else {}
    card_ids = unique_text(
        [
            *[str(card_id) for card_id in review.get("evidence_refs", []) if card_id],
            *[str(card_id) for card_id in patch.get("source_card_ids", []) if card_id],
            *[str(card_id) for card_id in patch_draft.get("source_card_ids", []) if card_id],
        ]
    )
    cards = [read_card_by_id(recipes_dir, card_id) for card_id in card_ids]
    cards = [card for card in cards if card]
    source_paths = candidate_quality_source_paths(cards)
    source_basenames = unique_text([Path(path).name for path in source_paths])
    proposed_value_count = review_triage_proposed_value_count(review, patch)
    source_kind = review_triage_source_kind(source_basenames)
    bucket, action, reasons = review_triage_bucket(
        review=review,
        patch=patch,
        proposed_value_count=proposed_value_count,
        source_kind=source_kind,
        min_values=min_values,
        max_values=max_values,
    )
    return {
        "review_id": record.get("review_id"),
        "patch_id": record.get("patch_id"),
        "target_recipe_id": record.get("target_recipe_id"),
        "review_status": review.get("status"),
        "triage_bucket": bucket,
        "recommended_action": action,
        "reasons": reasons,
        "source_kind": source_kind,
        "source_paths": source_paths,
        "source_basenames": source_basenames,
        "card_count": len(cards),
        "proposed_value_count": proposed_value_count,
        "risk": patch.get("risk"),
        "review_recommendation": review.get("recommendation"),
        "candidate_only": True,
        "cannot_claim": [
            "不能说 triage 已经接受或拒绝 review。",
            "不能说 triage bucket 证明内容质量通过。",
        ],
    }


def review_triage_proposed_value_count(review: dict[str, Any], patch: dict[str, Any]) -> int:
    hints = review.get("review_hints") if isinstance(review.get("review_hints"), dict) else {}
    proposed_hint = hints.get("proposed_value_count")
    if proposed_hint is not None:
        try:
            return int(proposed_hint)
        except (TypeError, ValueError):
            return 0
    if patch.get("patch_type") == "knowledge_fusion_candidate_set":
        return len([candidate for candidate in patch.get("fusion_candidates", []) if isinstance(candidate, dict)])
    return candidate_quality_proposed_value_count(patch)


def review_triage_source_kind(source_basenames: list[str]) -> str:
    text = " ".join(source_basenames).casefold()
    if any(token in text for token in ["source_trace", "receipt", "inventory", "priority_queue", "index_sampling", "sampling_map", "transition_receipt"]):
        return "evidence_index"
    if any(token in text for token in ["packet_probe", "execution_blueprint", "feed_ingest"]):
        return "process_evidence"
    if "task_contract" in text:
        return "guardrail_contract"
    if "contract" in text or "review_gate" in text:
        return "rule_contract"
    return "general_candidate"


def review_triage_bucket(
    *,
    review: dict[str, Any],
    patch: dict[str, Any],
    proposed_value_count: int,
    source_kind: str,
    min_values: int,
    max_values: int,
) -> tuple[str, str, list[str]]:
    reasons: list[str] = []
    review_hints = review.get("review_hints") if isinstance(review.get("review_hints"), dict) else {}
    split_recommended = bool(
        review.get("recommendation") == "split_before_accept"
        or review_hints.get("split_recommended")
        or patch.get("risk") == "split_recommended"
    )
    if proposed_value_count < min_values:
        reasons.append(f"proposed value count below triage minimum: {proposed_value_count} / {min_values}")
        return "thin_candidate", "reject_or_archive_until_more_evidence", reasons
    if source_kind in {"evidence_index", "process_evidence", "guardrail_contract"}:
        reasons.append(f"source kind is {source_kind}; keep as evidence unless a later review proves an executable recipe.")
        return "evidence_index_only", "keep_as_evidence_index_or_reject_review", reasons
    if proposed_value_count > max_values or split_recommended:
        if proposed_value_count > max_values:
            reasons.append(f"proposed value count too high for one review: {proposed_value_count} / {max_values}")
        if split_recommended:
            reasons.append("review hints or patch risk recommend split before accept")
        return "too_broad", "split_or_regenerate_narrower", reasons
    if review_triage_duplicate_risk(review, patch):
        reasons.append("review history suggests duplicate or already-covered narrow area")
        return "duplicate_risk", "compare_with_existing_recipe_before_accept", reasons
    reasons.append("candidate has enough values, source_trace, and no automatic reject bucket")
    return "human_review_candidate", "human_review_required", reasons


def review_triage_duplicate_risk(review: dict[str, Any], patch: dict[str, Any]) -> bool:
    text = stable_json({"review": review, "patch": patch}).casefold()
    return any(token in text for token in ["duplicate", "already a narrow recipe", "already covered", "已有窄"])


def review_packet_bucket_rank(bucket: str) -> int:
    return {
        "human_review_candidate": 0,
        "duplicate_risk": 1,
        "too_broad": 2,
        "thin_candidate": 3,
        "evidence_index_only": 4,
    }.get(bucket, 9)


def review_packet_item(record: dict[str, Any], triage_item: dict[str, Any]) -> dict[str, Any]:
    review = record.get("review", {}) if isinstance(record.get("review"), dict) else {}
    patch = record.get("patch", {}) if isinstance(record.get("patch"), dict) else {}
    plain = review.get("plain_language_summary") if isinstance(review.get("plain_language_summary"), dict) else {}
    if not plain and isinstance(patch.get("plain_language_summary"), dict):
        plain = patch.get("plain_language_summary", {})
    bucket = str(triage_item.get("triage_bucket") or "")
    action = str(triage_item.get("recommended_action") or "")
    target_recipe_id = str(triage_item.get("target_recipe_id") or "")
    return {
        "review_id": triage_item.get("review_id"),
        "patch_id": triage_item.get("patch_id"),
        "target_recipe_id": target_recipe_id,
        "display_title": review_packet_display_title(patch, plain, target_recipe_id),
        "review_status": triage_item.get("review_status"),
        "triage_bucket": bucket,
        "plain_bucket": review_packet_bucket_label(bucket),
        "recommended_action": action,
        "plain_action": review_packet_action_label(action),
        "why_this_bucket": review_packet_plain_reasons(triage_item.get("reasons", [])),
        "source_basenames": triage_item.get("source_basenames", []),
        "source_paths": triage_item.get("source_paths", []),
        "candidate_fields": review_packet_candidate_fields(patch, plain),
        "proposed_value_count": triage_item.get("proposed_value_count"),
        "card_count": triage_item.get("card_count"),
        "risk": triage_item.get("risk"),
        "what_this_is": plain.get("what_this_is") or "Agent Recipes 自动生成的候选 patch，仍在 review_queue。",
        "sample_changes": review_packet_sample_changes(patch, plain),
        "why_review": plain.get("why_review") or "只有 review_queue 被接受后，系统才允许生成或修改正式 recipe。",
        "next_step": plain.get("next_step") or review_packet_action_label(action),
        "cannot_claim": unique_text(
            [
                *[str(item) for item in triage_item.get("cannot_claim", [])],
                "不能说这条候选已经是正式菜谱。",
                "不能说这个审核包已经替人完成质量判断。",
            ]
        ),
    }


def review_packet_display_title(patch: dict[str, Any], plain: dict[str, Any], target_recipe_id: str) -> str:
    candidates: list[Any] = [plain.get("title")]
    change = patch.get("proposed_change") if isinstance(patch.get("proposed_change"), dict) else {}
    candidates.append(change.get("title"))
    for candidate in candidates:
        values = candidate if isinstance(candidate, list) else [candidate]
        for value in values:
            if value is None:
                continue
            text = first_line(str(value), max_chars=120).strip()
            if text:
                return text
    return target_recipe_id


def review_packet_bucket_label(bucket: str) -> str:
    labels = {
        "human_review_candidate": "可以拿给人看，但还不能自动收",
        "duplicate_risk": "可能重复，先和已有菜谱比对",
        "too_broad": "太宽了，先拆小再谈收",
        "thin_candidate": "太薄了，证据不够",
        "evidence_index_only": "更像证据索引，不像可执行菜谱",
    }
    return labels.get(bucket, "未知分层，先人工看")


def review_packet_action_label(action: str) -> str:
    labels = {
        "human_review_required": "人工看这条候选，决定收、拆、改或拒绝。",
        "compare_with_existing_recipe_before_accept": "先查已有菜谱，确认不重复再决定。",
        "split_or_regenerate_narrower": "先拆成更小目标，暂时不要收。",
        "reject_or_archive_until_more_evidence": "先拒绝或归档，等更多证据再说。",
        "keep_as_evidence_index_or_reject_review": "当证据索引保留或拒绝，不要当正式菜谱收。",
    }
    return labels.get(action, "先人工判断，不要自动收。")


def review_packet_plain_reasons(reasons: Any) -> list[str]:
    output: list[str] = []
    for reason in reasons if isinstance(reasons, list) else []:
        text = str(reason)
        lowered = text.casefold()
        if "candidate has enough values" in lowered:
            output.append("候选条目数量够，有来源追踪，也没有命中自动拒绝规则。")
        elif "proposed value count below" in lowered:
            output.append("候选条目太少，先别收成菜谱。")
        elif "source kind is" in lowered:
            output.append("来源更像证据索引或流程记录，不适合直接当执行菜谱。")
        elif "proposed value count too high" in lowered:
            output.append("一条候选里塞的内容太多，应该先拆小。")
        elif "recommend split before accept" in lowered:
            output.append("候选自己也提示要先拆再收。")
        elif "duplicate" in lowered or "already-covered" in lowered or "already covered" in lowered:
            output.append("可能和已有菜谱重复，先比对再决定。")
        elif text:
            output.append(text)
    return unique_text(output)


def review_packet_candidate_fields(patch: dict[str, Any], plain: dict[str, Any]) -> list[str]:
    fields = plain.get("fields_to_review") if isinstance(plain.get("fields_to_review"), list) else []
    if fields:
        return unique_text([str(field) for field in fields if field])
    change = patch.get("proposed_change") if isinstance(patch.get("proposed_change"), dict) else {}
    return unique_text([str(field) for field in change.keys() if field and field != "recipe_id"])


def review_packet_sample_changes(patch: dict[str, Any], plain: dict[str, Any], *, limit: int = 5) -> list[str]:
    samples = plain.get("sample_changes") if isinstance(plain.get("sample_changes"), list) else []
    if samples:
        return unique_text([first_line(str(sample)) for sample in samples if str(sample).strip()])[:limit]
    change = patch.get("proposed_change") if isinstance(patch.get("proposed_change"), dict) else {}
    output: list[str] = []
    for field, value in change.items():
        if field == "recipe_id":
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            text = first_line(str(item))
            if text:
                output.append(f"{field}: {text}")
            if len(output) >= limit:
                return unique_text(output)[:limit]
    return unique_text(output)[:limit]


def review_packet_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
    lines = [
        "# Review Packet",
        "",
        f"Report: `{report.get('report_id')}`",
        f"Status: `{report.get('status_filter')}`",
        f"Target: `{report.get('target_recipe_id') or report.get('target_prefix') or 'all'}`",
        "",
        "## 一眼结论",
        "",
        f"- 本包候选数：`{summary.get('review_count', 0)}`",
        f"- 可用候选总数：`{summary.get('available_review_count', 0)}`",
        f"- 分层：`{stable_json(summary.get('bucket_counts', {}))}`",
        f"- 建议动作：`{stable_json(summary.get('action_counts', {}))}`",
        "",
        "## 不能 claim",
    ]
    for item in report.get("cannot_claim", []):
        lines.append(f"- {item}")
    lines.extend(["", "## 候选"])
    for index, item in enumerate(report.get("items", []), start=1):
        target_recipe_id = str(item.get("target_recipe_id") or "")
        display_title = str(item.get("display_title") or target_recipe_id)
        heading = display_title if display_title == target_recipe_id else f"{display_title} (`{target_recipe_id}`)"
        lines.extend(
            [
                "",
                f"### {index}. {heading}",
                "",
                f"- 结论：{item.get('plain_bucket')}",
                f"- 下一步：{item.get('plain_action')}",
                f"- Review ID：`{item.get('review_id')}`",
                f"- Patch ID：`{item.get('patch_id')}`",
                f"- 状态：`{item.get('review_status')}`",
                f"- 候选字段：`{', '.join(item.get('candidate_fields', [])) or 'unknown'}`",
                f"- 候选条目数：`{item.get('proposed_value_count')}`",
                f"- 证据卡数量：`{item.get('card_count')}`",
                f"- 来源文件：`{', '.join(item.get('source_basenames', [])) or 'unknown'}`",
                f"- 为什么这样分：{'; '.join(str(reason) for reason in item.get('why_this_bucket', [])) or '无'}",
                f"- 为什么还要 review：{item.get('why_review')}",
            ]
        )
        samples = item.get("sample_changes", [])
        if samples:
            lines.append("- 样例改动：")
            for sample in samples:
                lines.append(f"  - {sample}")
        cannot_claim = item.get("cannot_claim", [])
        if cannot_claim:
            lines.append("- 这条不能 claim：")
            for claim in cannot_claim:
                lines.append(f"  - {claim}")
    return "\n".join(lines) + "\n"


def card_needs_deep_read(card: dict[str, Any]) -> bool:
    payload = card.get("extracted_payload", {}) if isinstance(card.get("extracted_payload"), dict) else {}
    quote = str(card.get("source_quote") or "")
    evidence_strength = str(card.get("evidence_strength") or "").casefold()
    if evidence_strength in {"partial", "unverified"}:
        return True
    if payload.get("missing_evidence") or payload.get("next_deep_read_target") or payload.get("needs_deep_read"):
        return True
    lowered = quote.casefold()
    return any(marker in lowered for marker in ["信息不全", "缺完整", "shallow", "partial", "小范围"])


def card_has_explicit_conflict(card: dict[str, Any]) -> bool:
    payload = card.get("extracted_payload", {}) if isinstance(card.get("extracted_payload"), dict) else {}
    quote = str(card.get("source_quote") or "").casefold()
    if payload.get("conflict") or payload.get("conflicts_with"):
        return True
    return any(marker in quote for marker in ["冲突", "conflict", "contradict"])


def card_payload_values(card: dict[str, Any], key: str) -> list[str]:
    payload = card.get("extracted_payload", {}) if isinstance(card.get("extracted_payload"), dict) else {}
    return normalize_string_list(payload.get(key))


def inferred_concepts_from_card(card: dict[str, Any]) -> list[str]:
    text = " ".join(
        [
            str(card.get("source_quote") or ""),
            " ".join(str(value) for value in card_payload_values(card, "checklist_item")),
            " ".join(str(value) for value in card_payload_values(card, "visual_check")),
        ]
    )
    concepts: list[str] = []
    if "关键帧" in text or "keyframe" in text.casefold():
        concepts.append("关键帧")
    return concepts


def normalize_fusion_text(value: Any) -> str:
    text = " ".join(str(value).strip().split())
    return text.casefold()


def unique_fusion_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = normalize_fusion_text(value)
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def combined_source_trace(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    seen: set[str] = set()
    for card in cards:
        for trace in card.get("source_trace", []):
            if not isinstance(trace, dict):
                continue
            key = sha256_json(trace)
            if key in seen:
                continue
            seen.add(key)
            traces.append(trace)
    return traces


def dedupe_fusion_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for candidate in candidates:
        candidate_id = str(candidate.get("fusion_candidate_id") or "")
        if candidate_id and candidate_id not in deduped:
            order.append(candidate_id)
        if candidate_id:
            deduped[candidate_id] = candidate
    return [deduped[candidate_id] for candidate_id in order]


def fusion_candidates_of_type(patch: dict[str, Any], candidate_type: str) -> list[dict[str, Any]]:
    return [candidate for candidate in patch.get("fusion_candidates", []) if candidate.get("candidate_type") == candidate_type]


def fusion_candidate_ids(patch: dict[str, Any]) -> list[str]:
    return unique_text([str(candidate.get("fusion_candidate_id") or "") for candidate in patch.get("fusion_candidates", [])])


def fusion_source_trace(patch: dict[str, Any]) -> list[dict[str, Any]]:
    traces: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in patch.get("fusion_candidates", []):
        for trace in candidate.get("source_trace", []):
            if not isinstance(trace, dict):
                continue
            key = sha256_json(trace)
            if key in seen:
                continue
            seen.add(key)
            traces.append(trace)
    return traces


def fusion_source_truth_to_read(patch: dict[str, Any]) -> list[str]:
    refs = text_values(patch.get("source_fusion_id")) + text_values(patch.get("source_card_ids"))
    for candidate in patch.get("fusion_candidates", []):
        details = candidate.get("details", {}) if isinstance(candidate.get("details"), dict) else {}
        refs.extend(text_values(details.get("next_deep_read_target")))
        refs.extend(text_values(details.get("missing_evidence")))
        refs.extend(text_values(details.get("conflict")))
    for trace in fusion_source_trace(patch):
        refs.extend(text_values(trace.get("path")))
        refs.extend(text_values(trace.get("record_id")))
    return unique_text(refs)


def fusion_cannot_claim(decision: str, patch: dict[str, Any]) -> list[str]:
    limits = [
        f"不能说 knowledge_fusion {decision} 已经在真实任务中验证。",
        "不能说 knowledge_fusion candidate 已经自动通过用户审查。",
        "不能说未深读的课程片段已经被完整吸收。",
    ]
    for candidate in patch.get("fusion_candidates", []):
        limits.extend(text_values(candidate.get("cannot_claim")))
        if candidate.get("candidate_type") == "needs_deep_read":
            limits.append("不能说浅读/关键帧小范围读取已经覆盖完整课程上下文。")
        if candidate.get("candidate_type") == "conflict_candidate":
            limits.append("不能说存在冲突的资料已经被自动判定为正确。")
    return unique_text(limits)


def recipe_from_fusion_merge(target_recipe_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    merge_candidates = fusion_candidates_of_type(patch, "merge_candidate")
    if not merge_candidates:
        raise RecipesError(
            "AR420",
            "knowledge_fusion 没有可 merge 的互证候选。",
            f"target_recipe_id={target_recipe_id}",
            "改用 review --split/--reject，或补更多来源后重新运行 knowledge-fusion。",
        )
    checklists: list[str] = []
    for candidate in merge_candidates:
        details = candidate.get("details", {}) if isinstance(candidate.get("details"), dict) else {}
        checklists.extend(text_values(details.get("shared_checklist")))
        checklists.extend(text_values(candidate.get("reason")))
    title = f"{title_from_recipe_id(target_recipe_id)} fusion merge"
    body = "\n".join(unique_text(checklists))
    recipe = recipe_draft(target_recipe_id, title, body, fusion_source_truth_to_read(patch))
    recipe["scope"] = "Knowledge fusion merge promoted after explicit review decision."
    recipe["knowledge_fusion_decision"] = "merge"
    recipe["source_fusion_id"] = patch.get("source_fusion_id")
    recipe["fusion_candidate_ids"] = fusion_candidate_ids(patch)
    recipe["source_trace"] = fusion_source_trace(patch)
    recipe["steps"] = unique_text(checklists)
    recipe["checklist_item"] = unique_text(recipe.get("checklist_item", []) + checklists)
    recipe["source_truth_to_read"] = fusion_source_truth_to_read(patch)
    recipe["evidence_refs"] = unique_text(text_values(patch.get("source_card_ids")) + text_values(patch.get("source_fusion_id")))
    recipe["cannot_claim"] = fusion_cannot_claim("merge", patch)
    recipe["open_questions"] = fusion_open_questions(patch)
    return recipe


def recipes_from_fusion_split(target_recipe_id: str, patch: dict[str, Any]) -> list[dict[str, Any]]:
    recipes: list[dict[str, Any]] = []
    for candidate in fusion_candidates_of_type(patch, "split_candidate"):
        details = candidate.get("details", {}) if isinstance(candidate.get("details"), dict) else {}
        use_whens = text_values(details.get("use_when"))
        concept = " / ".join(text_values(details.get("concept"))) or title_from_recipe_id(target_recipe_id)
        for use_when in use_whens:
            suffix = sha256_text(f"{patch.get('source_fusion_id')}:{candidate.get('fusion_candidate_id')}:{use_when}")[:8]
            recipe_id = f"{target_recipe_id}__split_{suffix}"
            title = first_line(f"{concept}: {use_when}")
            recipe = recipe_draft(recipe_id, title, use_when, fusion_source_truth_to_read(patch))
            recipe["version"] = 1
            recipe["scope"] = "Knowledge fusion split child recipe promoted after explicit review decision."
            recipe["knowledge_fusion_decision"] = "split"
            recipe["parent_recipe_id"] = target_recipe_id
            recipe["source_fusion_id"] = patch.get("source_fusion_id")
            recipe["fusion_candidate_ids"] = [str(candidate.get("fusion_candidate_id"))]
            recipe["source_trace"] = candidate.get("source_trace", [])
            recipe["use_when"] = unique_text([use_when])
            recipe["steps"] = unique_text([use_when, str(candidate.get("reason") or "")])
            recipe["checklist_item"] = unique_text(recipe["steps"])
            recipe["source_truth_to_read"] = fusion_source_truth_to_read(patch)
            recipe["evidence_refs"] = unique_text(text_values(candidate.get("source_card_ids")) + text_values(patch.get("source_fusion_id")))
            recipe["cannot_claim"] = fusion_cannot_claim("split", patch)
            recipe["open_questions"] = fusion_open_questions(patch)
            recipe["recipe_hash"] = recipe_hash(recipe)
            recipes.append(recipe)
    return recipes


def recipe_from_fusion_supersede(target_recipe_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    recipe = recipe_from_fusion_merge(target_recipe_id, patch)
    suffix = sha256_text(str(patch.get("source_fusion_id") or stable_json(patch)))[:8]
    recipe["recipe_id"] = f"{target_recipe_id}__supersede_{suffix}"
    recipe["version"] = 1
    recipe["title"] = first_line(f"{title_from_recipe_id(target_recipe_id)} supersede")
    recipe["scope"] = "Knowledge fusion supersede recipe promoted after explicit locked review decision."
    recipe["knowledge_fusion_decision"] = "supersede"
    recipe["supersedes"] = target_recipe_id
    recipe["cannot_claim"] = fusion_cannot_claim("supersede", patch)
    return recipe


def source_refinery_review_requires_split(review: dict[str, Any], patch: dict[str, Any]) -> bool:
    hints = review.get("review_hints", {}) if isinstance(review.get("review_hints"), dict) else {}
    return bool(
        review.get("recommendation") == "split_before_accept"
        or hints.get("split_recommended")
        or patch.get("risk") == "split_recommended"
    )


SOURCE_REFINERY_SPLIT_FIELDS = [
    "checklist_item",
    "visual_check",
    "forbidden_path",
    "verified_path",
    "failure_signals",
    "pressure_test",
    "good_example",
    "bad_example",
]


def source_refinery_source_truth(patch: dict[str, Any]) -> list[str]:
    proposed = patch.get("proposed_change", {}) if isinstance(patch.get("proposed_change"), dict) else {}
    refs = text_values(proposed.get("source_truth_to_read"))
    refs.extend(text_values(proposed.get("evidence_refs")))
    refs.extend(text_values(patch.get("evidence_refs")))
    refs.extend(text_values(patch.get("source_card_ids")))
    refs.extend(text_values(patch.get("source_patch_draft_id")))
    return unique_text(refs)


def source_refinery_cannot_claim(decision: str, patch: dict[str, Any]) -> list[str]:
    proposed = patch.get("proposed_change", {}) if isinstance(patch.get("proposed_change"), dict) else {}
    limits = text_values(proposed.get("cannot_claim"))
    limits.extend(
        [
            f"不能说 source_refinery {decision} 已经在真实任务中验证。",
            "不能说 source_refinery candidate 已经自动通过用户审查。",
            "不能说未被本次 review 决策覆盖的候选已经自动吸收。",
        ]
    )
    return unique_text(limits)


def source_refinery_split_groups(patch: dict[str, Any]) -> list[tuple[str, list[str]]]:
    proposed = patch.get("proposed_change", {}) if isinstance(patch.get("proposed_change"), dict) else {}
    groups: list[tuple[str, list[str]]] = []
    for field in SOURCE_REFINERY_SPLIT_FIELDS:
        values = text_values(proposed.get(field))
        if values:
            groups.append((field, values))
    if not groups:
        fallback = text_values(proposed.get("steps")) or text_values(proposed.get("checklist_item"))
        if fallback:
            groups.append(("steps", fallback))
    return groups


def recipes_from_source_refinery_split(target_recipe_id: str, patch: dict[str, Any]) -> list[dict[str, Any]]:
    recipes: list[dict[str, Any]] = []
    source_truth = source_refinery_source_truth(patch)
    proposed = patch.get("proposed_change", {}) if isinstance(patch.get("proposed_change"), dict) else {}
    for field, values in source_refinery_split_groups(patch):
        suffix = sha256_text(f"{patch.get('patch_id')}:{field}:{stable_json(values)}")[:8]
        recipe_id = f"{target_recipe_id}__split_{field}_{suffix}"
        title = first_line(f"{title_from_recipe_id(target_recipe_id)} {field} split")
        body = "\n".join(values)
        recipe = recipe_draft(recipe_id, title, body, source_truth)
        recipe["version"] = 1
        recipe["scope"] = "Source refinery split child recipe promoted after explicit review decision."
        recipe["source_refinery_decision"] = "split"
        recipe["parent_recipe_id"] = target_recipe_id
        recipe["source_patch_id"] = patch.get("patch_id")
        recipe["source_patch_draft_id"] = patch.get("source_patch_draft_id")
        recipe["split_field"] = field
        recipe["source_truth_to_read"] = source_truth
        recipe["evidence_refs"] = source_truth
        recipe["steps"] = unique_text(values)
        recipe["checklist_item"] = unique_text(text_values(proposed.get("checklist_item")) if field == "checklist_item" else values)
        recipe[field] = unique_text(values)
        recipe["cannot_claim"] = source_refinery_cannot_claim("split", patch)
        recipe["open_questions"] = unique_text(text_values(proposed.get("open_questions")))
        recipe["recipe_hash"] = recipe_hash(recipe)
        recipes.append(recipe)
    return recipes


def recipe_from_source_refinery_supersede(target_recipe_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    proposed = patch.get("proposed_change", {}) if isinstance(patch.get("proposed_change"), dict) else {}
    recipe = json.loads(json.dumps(proposed, ensure_ascii=False))
    suffix = sha256_text(str(patch.get("patch_id") or stable_json(patch)))[:8]
    recipe["recipe_id"] = f"{target_recipe_id}__supersede_{suffix}"
    recipe["version"] = 1
    recipe["title"] = first_line(f"{title_from_recipe_id(target_recipe_id)} supersede")
    recipe["scope"] = "Source refinery supersede recipe promoted after explicit locked review decision."
    recipe["source_refinery_decision"] = "supersede"
    recipe["supersedes"] = target_recipe_id
    recipe["source_patch_id"] = patch.get("patch_id")
    recipe["source_patch_draft_id"] = patch.get("source_patch_draft_id")
    recipe["source_truth_to_read"] = source_refinery_source_truth(patch)
    recipe["evidence_refs"] = source_refinery_source_truth(patch)
    recipe["cannot_claim"] = source_refinery_cannot_claim("supersede", patch)
    recipe["recipe_hash"] = recipe_hash(recipe)
    return recipe


def fusion_open_questions(patch: dict[str, Any]) -> list[str]:
    questions: list[str] = []
    for candidate in patch.get("fusion_candidates", []):
        if candidate.get("candidate_type") == "needs_deep_read":
            questions.append(f"需要深读：{candidate.get('reason')}")
        if candidate.get("candidate_type") == "conflict_candidate":
            questions.append(f"需要人工判断冲突：{candidate.get('reason')}")
    return unique_text(questions)


def recipe_additions_from_cards(cards: list[dict[str, Any]]) -> dict[str, list[str]]:
    additions: dict[str, list[str]] = {}
    for card in cards:
        payload = card.get("extracted_payload", {})
        raw_fields = list(card.get("target_fields", [])) + markdown_table_recipe_fields(card)
        for field in raw_fields:
            recipe_field = FIELD_ALIASES.get(field, field)
            values = values_for_recipe_field(recipe_field, payload, card)
            if not values:
                continue
            existing = additions.setdefault(recipe_field, [])
            for value in values:
                cleaned = clean_recipe_value(recipe_field, value)
                if cleaned and cleaned not in existing:
                    existing.append(cleaned)
    return additions


def markdown_table_recipe_fields(card: dict[str, Any]) -> list[str]:
    fields: list[str] = []
    for field in sorted(set(MARKDOWN_TABLE_FIELD_ALIASES.values())):
        if markdown_table_values_for_field(card, field):
            fields.append(field)
    return fields


FIELD_VALUE_PREFIXES = [
    "bad_example",
    "cannot_claim",
    "checklist_item",
    "failure_signal",
    "failure_signals",
    "forbidden_path",
    "good_example",
    "pressure_test",
    "source_truth_to_read",
    "verified_path",
    "visual_check",
]


HUMAN_READABLE_VALUE_OVERRIDES = {
    "quality_pass": "不能说视觉或成片质量已经通过。",
    "user_review_pass": "不能说已经通过用户人工 review。",
    "final": "不能说已经是最终版。",
    "prod": "不能说已经进入生产可用状态。",
    "public": "不能说已经可以公开发布。",
    "sample_project_learned": "不能说已经被 SampleProject 正式吸收。",
    "official_skill": "不能说已经成为正式 skill。",
    "不能说 refined chunk 已经验证。": "不能说资料片段已经被人工验证。",
    "不能说 refined chunk 已经进入正式 recipe。": "不能说资料片段已经进入正式菜谱。",
    "cannot say visual quality passed.": "不能说视觉质量已经通过。",
    "cannot say export quality passed without review.": "不能说导出质量已经通过 review。",
    "cannot say summary equals absorption.": "不能说总结等于已经吸收成菜谱。",
    "old coordinates replace screenshot review.": "用旧坐标替代截图 review。",
    "safe margin plus screenshot evidence.": "留出安全边距，并用截图做证据。",
    "summary has no recipe field.": "只有总结，没有菜谱字段。",
    "using a long summary as a learned recipe.": "把长总结直接当成已学会的菜谱。",
    "pip does not block main action.": "小窗不挡主要动作。",
    "partial_master_candidate: V1/A1 spine exists and at least one verified V2/V3 insert exists, but some semantic groups are missing, host-only, or marker-only.": (
        "半成品时间线：V1/A1 主线存在，并且至少有一个已验证的 V2/V3 插入片段；但部分语义组还缺素材、只有主持人、或只是 marker。"
    ),
    "A new isolated run005 local project directory exists.": "已创建新的隔离 run005 本地项目目录。",
    "DaVinci project and timeline were created by script.": "DaVinci 项目和时间线由脚本创建。",
    "The 59.4s presenter video and original audio are present as the timeline spine.": "59.4 秒主持人视频和原始音频已作为时间线主干。",
    "One verified true AI video segment is placed over the real timecode window with a round upper PIP.": (
        "一个已验证的真实 AI 视频片段已放进真实时间码窗口，并使用右上圆形 PIP。"
    ),
    "The run has report, manifest, receipt, and failure ledger.": "这次运行有报告、素材清单、回执和失败记录。",
}


def clean_recipe_value(field: str, value: Any) -> str:
    text = " ".join(str(value).split())
    if not text:
        return ""
    lowered = text.casefold()
    for prefix in FIELD_VALUE_PREFIXES:
        marker = f"{prefix}:"
        if lowered.startswith(marker):
            text = text[len(marker) :].strip()
            lowered = text.casefold()
            break
    text = strip_markdown_list_markers(text)
    lowered = text.casefold()
    return HUMAN_READABLE_VALUE_OVERRIDES.get(text, HUMAN_READABLE_VALUE_OVERRIDES.get(lowered, text))


def strip_markdown_list_markers(text: str) -> str:
    cleaned = text.strip()
    while cleaned.startswith(("- ", "* ")):
        cleaned = cleaned[2:].strip()
    return cleaned


def text_values(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    return [str(value).strip()] if str(value).strip() else []


def timestamped_transcript_cues(text: str) -> list[dict[str, Any]]:
    cues: list[dict[str, Any]] = []
    pattern = re.compile(r"^\[(\d+(?:\.\d+)?)-(\d+(?:\.\d+)?)\]\s*(.+)$")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        start = float(match.group(1))
        end = float(match.group(2))
        cue_text = " ".join(match.group(3).split())
        if end > start and cue_text:
            cues.append({"start": start, "end": end, "text": cue_text})
    return cues


def completeness_audit_report(
    subject: dict[str, Any],
    *,
    subject_type: str,
    requirements: dict[str, Any],
    software_map: dict[str, Any],
    input_path: Path,
    requirements_path: Path | None,
    software_map_path: Path | None,
    execution_evidence_path: Path | None,
    project_root: Path,
) -> dict[str, Any]:
    payload = subject.get("proposed_change") if isinstance(subject.get("proposed_change"), dict) else subject
    dimensions = (
        skill_completeness_dimensions(payload)
        if subject_type == "skill"
        else course_completeness_dimensions(payload)
    )
    structural_score = sum(int(item["score"]) for item in dimensions)
    structural_max = sum(int(item["max_score"]) for item in dimensions)
    structural_hard_gate_failures = [
        item["id"] for item in dimensions if item.get("hard_gate") and int(item["score"]) < 2
    ]

    raw_requirements = requirements.get("requirements", []) if requirements else []
    if not isinstance(raw_requirements, list):
        raw_requirements = []
    domain_checks = [completeness_requirement_check(payload, item) for item in raw_requirements if isinstance(item, dict)]
    required_domain_failures = [
        item["id"] for item in domain_checks if item.get("required", True) and not item.get("passed")
    ]
    execution_readiness = completeness_execution_readiness(
        payload,
        subject_type=subject_type,
        execution_contract=requirements.get("execution_contract", {}),
        software_map=software_map,
        project_root=project_root,
    )
    hard_gate_failures = unique_text(
        structural_hard_gate_failures + execution_readiness["hard_gate_failures"]
    )
    requirements_checked = bool(domain_checks)
    hard_gates_passed = not hard_gate_failures and not required_domain_failures
    score = round(structural_score / structural_max, 3) if structural_max else 0.0
    domain_passed = sum(1 for item in domain_checks if item.get("passed"))
    domain_max = len(domain_checks)
    domain_score = round(domain_passed / domain_max, 3) if domain_max else None
    overall_score = round((score + domain_score) / 2, 3) if domain_score is not None else score

    if execution_readiness["status"] == "fresh_execution_failed":
        status = "execution_failed"
    elif execution_readiness["status"] == "fresh_execution_verified":
        status = "execution_verified"
    elif hard_gates_passed and score >= 0.8:
        status = "complete_for_review"
    elif subject_type == "course" and "source_coverage" in hard_gate_failures:
        status = "needs_deep_read"
    else:
        status = "incomplete"

    missing_evidence = [
        item["plain"] for item in dimensions if int(item["score"]) < int(item["max_score"])
    ]
    missing_evidence.extend(item["plain"] for item in domain_checks if not item.get("passed"))
    if not requirements_checked:
        missing_evidence.append("没有提供技能/课程特有 requirements，只检查了通用结构。")

    return {
        "ok": True,
        "action": "completeness-audit",
        "subject_type": subject_type,
        "input_path": str(input_path),
        "requirements_path": str(requirements_path) if requirements_path else None,
        "software_map_path": str(software_map_path) if software_map_path else None,
        "execution_evidence_path": str(execution_evidence_path) if execution_evidence_path else None,
        "status": status,
        "score": score,
        "score_percent": round(score * 100, 1),
        "domain_score": domain_score,
        "domain_score_percent": round(domain_score * 100, 1) if domain_score is not None else None,
        "overall_score": overall_score,
        "overall_score_percent": round(overall_score * 100, 1),
        "structural_score": structural_score,
        "structural_max_score": structural_max,
        "domain_passed": domain_passed,
        "domain_max": domain_max,
        "hard_gates_passed": hard_gates_passed,
        "hard_gate_failures": hard_gate_failures,
        "requirements_checked": requirements_checked,
        "required_domain_failures": required_domain_failures,
        "dimensions": dimensions,
        "domain_checks": domain_checks,
        "execution_readiness": execution_readiness,
        "missing_evidence": unique_text(missing_evidence + execution_readiness["missing_evidence"]),
        "plain": completeness_plain_summary(
            status=status,
            score_percent=round(score * 100, 1),
            hard_gate_failures=hard_gate_failures,
            required_domain_failures=required_domain_failures,
            requirements_checked=requirements_checked,
        ),
        "cannot_claim": [
            "结构完整不等于内容正确。",
            "课程完整不等于 agent 已掌握技能。",
            "技能完整不等于真实软件执行和最终产物质量通过。",
            "软件功能名出现了，不等于 agent 知道该功能的用途、结果和失败恢复。",
            "没有 fresh agent 从干净环境只按菜谱一次跑通，不能说技能已经学会。",
        ],
    }


def skill_completeness_dimensions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        completeness_group_dimension(
            "applicability",
            "什么时候能用、什么时候不能用",
            payload,
            [("use_when", "scope"), ("do_not_use_when", "forbidden_path", "stop_line")],
            hard_gate=True,
        ),
        completeness_group_dimension(
            "prerequisites",
            "输入、素材和工具前提",
            payload,
            [("inputs_required",), ("source_truth_to_read", "prerequisites", "tools_required")],
        ),
        completeness_list_dimension("procedure", "可按顺序执行的步骤", payload, ("steps",), min_full=3, hard_gate=True),
        completeness_group_dimension(
            "verification",
            "怎么检查做对了",
            payload,
            [("verification", "visual_check"), ("success_means", "outputs_expected", "acceptance_criteria")],
            hard_gate=True,
        ),
        completeness_group_dimension(
            "failure_recovery",
            "失败信号和退路",
            payload,
            [("failure_signals", "failure_means"), ("fallback_allowed", "rollback")],
        ),
        completeness_group_dimension(
            "evidence_trace",
            "来源和真实证据",
            payload,
            [("evidence_refs", "source_trace"), ("verified_path", "source_truth_to_read")],
            hard_gate=True,
        ),
        completeness_group_dimension(
            "claim_boundary",
            "哪些话现在不能说",
            payload,
            [("cannot_claim",), ("status", "version", "recipe_id")],
            hard_gate=True,
        ),
    ]


def course_completeness_dimensions(payload: dict[str, Any]) -> list[dict[str, Any]]:
    coverage_complete = payload.get("coverage_complete") is True or float_or_zero(payload.get("coverage_ratio")) >= 1.0
    coverage_present = any(
        completeness_field_present(payload, field)
        for field in ("source_coverage", "sections_seen", "segments_seen", "coverage_ratio", "read_mode")
    )
    coverage_score = 2 if coverage_complete else (1 if coverage_present else 0)
    dimensions = [
        {
            "id": "source_coverage",
            "label": "课程读到了哪里",
            "score": coverage_score,
            "max_score": 2,
            "hard_gate": True,
            "evidence_fields": completeness_present_fields(
                payload,
                ("coverage_complete", "coverage_ratio", "source_coverage", "sections_seen", "segments_seen", "read_mode"),
            ),
            "plain": "课程覆盖范围没有证明读完整，需要继续深读。",
        },
        completeness_group_dimension(
            "learning_scope",
            "这门课教什么、适用于哪里",
            payload,
            [("learning_objectives", "topic", "scope"), ("use_when", "do_not_use_when")],
        ),
        completeness_list_dimension(
            "procedure",
            "能拆成动作的详细步骤",
            payload,
            ("steps", "executable_steps"),
            min_full=3,
            hard_gate=True,
        ),
        completeness_group_dimension(
            "examples_variants",
            "例子、变体和适用差异",
            payload,
            [("examples", "worked_examples"), ("variants", "exceptions", "different_uses")],
        ),
        completeness_group_dimension(
            "verification",
            "课程里的做对标准",
            payload,
            [("verification", "visual_check"), ("success_means", "acceptance_criteria")],
            hard_gate=True,
        ),
        completeness_group_dimension(
            "evidence_trace",
            "每条结论能回到原课程位置",
            payload,
            [("source_trace", "evidence_refs"), ("timestamps", "page_refs", "section_refs")],
            hard_gate=True,
        ),
        completeness_group_dimension(
            "claim_boundary",
            "不把课程说成已掌握",
            payload,
            [("cannot_claim",), ("status", "evidence_strength")],
            hard_gate=True,
        ),
    ]
    return dimensions


def completeness_execution_readiness(
    payload: dict[str, Any],
    *,
    subject_type: str,
    execution_contract: Any,
    software_map: dict[str, Any],
    project_root: Path,
) -> dict[str, Any]:
    if not isinstance(execution_contract, dict) or execution_contract.get("mode") != "software":
        return {
            "mode": "not_applicable",
            "status": "not_applicable",
            "hard_gate_failures": [],
            "missing_evidence": [],
            "structured_steps_passed": None,
            "software_function_map_passed": None,
            "fresh_execution_required": False,
            "fresh_execution_passed": False,
            "course_skill_inventory_passed": None,
            "unmapped_taught_skills": [],
        }

    required_step_fields = [
        str(field)
        for field in execution_contract.get(
            "required_step_fields",
            ["order", "action", "function_id", "expected_state", "verification", "source_trace"],
        )
        if str(field).strip()
    ]
    min_steps = max(1, int(execution_contract.get("min_steps", 3)))
    steps = payload.get("steps") if isinstance(payload.get("steps"), list) else []
    invalid_steps: list[dict[str, Any]] = []
    for index, step in enumerate(steps, start=1):
        if not isinstance(step, dict):
            invalid_steps.append(
                {
                    "index": index,
                    "reason": "concept_text_is_not_an_executable_step",
                    "missing_fields": required_step_fields,
                }
            )
            continue
        missing_fields = [
            field for field in required_step_fields if not executable_step_field_present(step, field)
        ]
        if step.get("order") != index:
            missing_fields.append("sequential_order")
        if missing_fields:
            invalid_steps.append(
                {
                    "index": index,
                    "reason": "missing_step_contract",
                    "missing_fields": unique_text(missing_fields),
                }
            )
    structured_steps_passed = len(steps) >= min_steps and not invalid_steps

    step_function_ids = unique_text(
        [
            str(step.get("function_id", "")).strip()
            for step in steps
            if isinstance(step, dict) and str(step.get("function_id", "")).strip()
        ]
    )
    required_function_ids = unique_text(
        [str(item).strip() for item in execution_contract.get("required_function_ids", []) if str(item).strip()]
        + step_function_ids
    )
    function_map_check = completeness_software_function_map_check(
        software_map,
        expected_software_id=str(execution_contract.get("software_id", "")).strip(),
        required_function_ids=required_function_ids,
    )

    require_skill_inventory = bool(execution_contract.get("require_skill_inventory"))
    inventory_check = completeness_course_skill_inventory(payload) if require_skill_inventory else {
        "passed": None,
        "taught_skills": [],
        "mapped_skills": [],
        "unmapped_taught_skills": [],
        "invalid_extracted_skills": [],
    }

    fresh_execution_required = bool(execution_contract.get("require_fresh_execution"))
    fresh_check = completeness_fresh_execution_check(
        payload.get("fresh_execution_evidence"),
        project_root=project_root,
    )

    hard_gate_failures: list[str] = []
    missing_evidence: list[str] = []
    if not structured_steps_passed:
        hard_gate_failures.append("structured_steps")
        missing_evidence.append(
            "步骤仍是概念说明，或缺少顺序、软件功能、预期界面状态、逐步验证和课程时间码。"
        )
    if not function_map_check["passed"]:
        hard_gate_failures.append("software_function_map")
        missing_evidence.append(
            "缺少完整的软件功能地图；agent 还不知道这些功能什么时候用、会改变什么、做对后看到什么。"
        )
    if require_skill_inventory and not inventory_check["passed"]:
        hard_gate_failures.append("course_skill_inventory")
        missing_evidence.append("课程里讲到的技能没有全部拆成带来源步骤，不能说整门课程已经拆完。")

    if hard_gate_failures:
        status = "incomplete"
    elif fresh_execution_required and fresh_check["passed"]:
        status = "fresh_execution_verified"
    elif fresh_execution_required and fresh_check["evidence_present"]:
        status = "fresh_execution_failed"
        missing_evidence.append("fresh agent 没有从干净环境只按菜谱一次跑通。")
    elif fresh_execution_required:
        status = "needs_fresh_execution"
        missing_evidence.append("还缺 fresh agent 从干净环境只按菜谱一次跑通的原始证据。")
    else:
        status = "ready_for_execution_review"

    return {
        "mode": "software",
        "software_id": str(execution_contract.get("software_id", "")).strip() or None,
        "status": status,
        "hard_gate_failures": hard_gate_failures,
        "missing_evidence": missing_evidence,
        "structured_steps_passed": structured_steps_passed,
        "step_count": len(steps),
        "minimum_step_count": min_steps,
        "required_step_fields": required_step_fields,
        "invalid_steps": invalid_steps,
        "required_function_ids": required_function_ids,
        "software_function_map_passed": function_map_check["passed"],
        "software_function_map": function_map_check,
        "fresh_execution_required": fresh_execution_required,
        "fresh_execution_passed": fresh_check["passed"],
        "fresh_execution": fresh_check,
        "course_skill_inventory_required": require_skill_inventory,
        "course_skill_inventory_passed": inventory_check["passed"],
        "taught_skills": inventory_check["taught_skills"],
        "mapped_skills": inventory_check["mapped_skills"],
        "unmapped_taught_skills": inventory_check["unmapped_taught_skills"],
        "invalid_extracted_skills": inventory_check["invalid_extracted_skills"],
        "subject_type": subject_type,
    }


def executable_step_field_present(step: dict[str, Any], field: str) -> bool:
    value = step.get(field)
    if field == "order":
        return isinstance(value, int) and value > 0
    if field == "source_trace":
        if not isinstance(value, list) or not value:
            return False
        return all(
            isinstance(trace, dict)
            and bool(str(trace.get("path", "")).strip())
            and any(bool(str(trace.get(locator, "")).strip()) for locator in ("timestamp", "page", "section"))
            for trace in value
        )
    return completeness_field_present(step, field)


def completeness_software_function_map_check(
    software_map: dict[str, Any],
    *,
    expected_software_id: str,
    required_function_ids: list[str],
) -> dict[str, Any]:
    required_fields = (
        "function_id",
        "name",
        "purpose",
        "use_when",
        "ui_action",
        "changes",
        "expected_state",
        "failure_signals",
        "source_trace",
    )
    functions = software_map.get("functions") if isinstance(software_map.get("functions"), list) else []
    by_id = {
        str(item.get("function_id")): item
        for item in functions
        if isinstance(item, dict) and str(item.get("function_id", "")).strip()
    }
    missing_function_ids = [function_id for function_id in required_function_ids if function_id not in by_id]
    incomplete_entries: list[dict[str, Any]] = []
    for function_id in required_function_ids:
        entry = by_id.get(function_id)
        if not entry:
            continue
        missing_fields = [field for field in required_fields if not executable_step_field_present(entry, field)]
        if missing_fields:
            incomplete_entries.append({"function_id": function_id, "missing_fields": missing_fields})
    software_id_matches = bool(expected_software_id) and software_map.get("software_id") == expected_software_id
    version_scope_present = completeness_field_present(software_map, "version_scope")
    passed = (
        software_id_matches
        and version_scope_present
        and bool(required_function_ids)
        and not missing_function_ids
        and not incomplete_entries
    )
    return {
        "passed": passed,
        "expected_software_id": expected_software_id or None,
        "actual_software_id": software_map.get("software_id"),
        "software_id_matches": software_id_matches,
        "version_scope_present": version_scope_present,
        "required_function_ids": required_function_ids,
        "missing_function_ids": missing_function_ids,
        "incomplete_function_entries": incomplete_entries,
    }


def completeness_course_skill_inventory(payload: dict[str, Any]) -> dict[str, Any]:
    taught_skills = unique_text(text_values(payload.get("taught_skills")))
    extracted = payload.get("extracted_skills") if isinstance(payload.get("extracted_skills"), list) else []
    mapped_skills: list[str] = []
    invalid_extracted_skills: list[dict[str, Any]] = []
    for index, item in enumerate(extracted, start=1):
        if not isinstance(item, dict):
            invalid_extracted_skills.append({"index": index, "reason": "not_an_object"})
            continue
        skill_id = str(item.get("skill_id", "")).strip()
        step_orders = item.get("step_orders")
        source_trace = item.get("source_trace")
        trace_valid = executable_step_field_present({"source_trace": source_trace}, "source_trace")
        if not skill_id or not isinstance(step_orders, list) or not step_orders or not trace_valid:
            invalid_extracted_skills.append({
                "index": index,
                "skill_id": skill_id or None,
                "reason": "missing_skill_id_step_orders_or_source_trace",
            })
            continue
        mapped_skills.append(skill_id)
    mapped_skills = unique_text(mapped_skills)
    unmapped = [skill_id for skill_id in taught_skills if skill_id not in mapped_skills]
    return {
        "passed": bool(taught_skills) and not unmapped and not invalid_extracted_skills,
        "taught_skills": taught_skills,
        "mapped_skills": mapped_skills,
        "unmapped_taught_skills": unmapped,
        "invalid_extracted_skills": invalid_extracted_skills,
    }


def completeness_fresh_execution_check(raw: Any, *, project_root: Path) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "evidence_present": False,
            "passed": False,
            "missing_fields": [
                "fresh_agent",
                "clean_start",
                "recipe_only",
                "attempt_count",
                "passed",
                "evidence_paths",
            ],
        }
    raw_evidence_paths = raw.get("evidence_paths") if isinstance(raw.get("evidence_paths"), list) else []
    valid_evidence_paths: list[str] = []
    invalid_evidence_paths: list[dict[str, str]] = []
    resolved_root = project_root.resolve()
    for raw_path in raw_evidence_paths:
        path_text = str(raw_path).strip()
        if not path_text:
            invalid_evidence_paths.append({"path": path_text, "reason": "empty_path"})
            continue
        candidate_path = Path(path_text)
        resolved_path = (candidate_path if candidate_path.is_absolute() else resolved_root / candidate_path).resolve()
        try:
            resolved_path.relative_to(resolved_root)
        except ValueError:
            invalid_evidence_paths.append({"path": path_text, "reason": "outside_project"})
            continue
        if not resolved_path.is_file():
            invalid_evidence_paths.append({"path": path_text, "reason": "file_missing"})
            continue
        valid_evidence_paths.append(str(resolved_path))

    checks = {
        "fresh_agent": raw.get("fresh_agent") is True,
        "clean_start": raw.get("clean_start") is True,
        "recipe_only": raw.get("recipe_only") is True,
        "attempt_count": raw.get("attempt_count") == 1,
        "passed": raw.get("passed") is True,
        "evidence_paths": bool(valid_evidence_paths) and not invalid_evidence_paths,
    }
    return {
        "evidence_present": True,
        "passed": all(checks.values()),
        "checks": checks,
        "missing_fields": [field for field, passed in checks.items() if not passed],
        "evidence_paths": raw_evidence_paths,
        "valid_evidence_paths": valid_evidence_paths,
        "invalid_evidence_paths": invalid_evidence_paths,
    }


def completeness_group_dimension(
    dimension_id: str,
    label: str,
    payload: dict[str, Any],
    groups: list[tuple[str, ...]],
    *,
    hard_gate: bool = False,
) -> dict[str, Any]:
    matched_groups = [
        [field for field in group if completeness_field_present(payload, field)]
        for group in groups
    ]
    matched_count = sum(1 for fields in matched_groups if fields)
    score = 2 if matched_count == len(groups) else (1 if matched_count else 0)
    return {
        "id": dimension_id,
        "label": label,
        "score": score,
        "max_score": 2,
        "hard_gate": hard_gate,
        "evidence_fields": unique_text([field for fields in matched_groups for field in fields]),
        "plain": f"{label}还不完整。",
    }


def completeness_list_dimension(
    dimension_id: str,
    label: str,
    payload: dict[str, Any],
    fields: tuple[str, ...],
    *,
    min_full: int,
    hard_gate: bool,
) -> dict[str, Any]:
    values: list[str] = []
    present_fields: list[str] = []
    for field in fields:
        field_values = text_values(payload.get(field))
        if field_values:
            present_fields.append(field)
            values.extend(field_values)
    score = 2 if len(values) >= min_full else (1 if values else 0)
    return {
        "id": dimension_id,
        "label": label,
        "score": score,
        "max_score": 2,
        "hard_gate": hard_gate,
        "evidence_fields": present_fields,
        "item_count": len(values),
        "plain": f"{label}还不完整。",
    }


def completeness_requirement_check(payload: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]:
    requirement_id = str(raw.get("id") or make_id("requirement", raw))
    label = str(raw.get("label") or requirement_id)
    fields = [str(item) for item in raw.get("fields", []) if str(item).strip()]
    scoped = {field: payload.get(field) for field in fields} if fields else payload
    blob = stable_json(scoped).casefold()
    raw_groups = raw.get("term_groups", [])
    term_groups = [
        [str(term).casefold() for term in group if str(term).strip()]
        for group in raw_groups
        if isinstance(group, list)
    ]
    matched_groups: list[list[str]] = []
    missing_groups: list[list[str]] = []
    for group in term_groups:
        matched = [term for term in group if term in blob]
        if matched:
            matched_groups.append(matched)
        else:
            missing_groups.append(group)
    ordered_groups: list[list[list[str]]] = []
    for raw_group in raw.get("ordered_term_groups", []):
        if not isinstance(raw_group, list):
            continue
        positions: list[list[str]] = []
        for raw_position in raw_group:
            values = raw_position if isinstance(raw_position, list) else [raw_position]
            alternatives = [str(term).casefold() for term in values if str(term).strip()]
            if alternatives:
                positions.append(alternatives)
        if positions:
            ordered_groups.append(positions)
    ordered_matches: list[list[str]] = []
    ordered_missing: list[list[list[str]]] = []
    ordered_values = [value for field in fields for value in text_values(payload.get(field))]
    ordered_text = " ".join(ordered_values).casefold() if fields else blob
    for positions in ordered_groups:
        cursor = 0
        selected_terms: list[str] = []
        missing_positions: list[list[str]] = []
        for alternatives in positions:
            matches = [
                (ordered_text.find(term, cursor), term)
                for term in alternatives
                if ordered_text.find(term, cursor) >= 0
            ]
            if not matches:
                missing_positions.append(alternatives)
                continue
            position, term = min(matches, key=lambda item: item[0])
            selected_terms.append(term)
            cursor = position + len(term)
        if missing_positions:
            ordered_missing.append(missing_positions)
        else:
            ordered_matches.append(selected_terms)
    passed = bool(term_groups or ordered_groups) and not missing_groups and not ordered_missing
    return {
        "id": requirement_id,
        "label": label,
        "required": bool(raw.get("required", True)),
        "passed": passed,
        "fields": fields,
        "matched_term_groups": matched_groups,
        "missing_term_groups": missing_groups,
        "ordered_term_groups": ordered_groups,
        "matched_ordered_term_groups": ordered_matches,
        "missing_ordered_term_groups": ordered_missing,
        "plain": str(raw.get("missing_plain") or f"缺少技能特有关键动作：{label}。"),
    }


def completeness_field_present(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if value is None or value is False:
        return False
    if isinstance(value, (str, list, dict, tuple, set)):
        return bool(value)
    return True


def completeness_present_fields(payload: dict[str, Any], fields: tuple[str, ...]) -> list[str]:
    return [field for field in fields if completeness_field_present(payload, field)]


def float_or_zero(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def completeness_plain_summary(
    *,
    status: str,
    score_percent: float,
    hard_gate_failures: list[str],
    required_domain_failures: list[str],
    requirements_checked: bool,
) -> str:
    if status == "execution_failed":
        return f"结构完整度 {score_percent}%，但 fresh agent 真实执行失败。现在不能说这条技能已学会。"
    if status == "execution_verified":
        return f"结构完整度 {score_percent}%，fresh agent 已从干净环境只按菜谱一次跑通；仍需人工判断成片质量。"
    if status == "complete_for_review":
        suffix = "已检查技能特有关键动作。" if requirements_checked else "只检查了通用结构。"
        return f"结构完整度 {score_percent}%，硬门槛已过；可以进入人工/真实执行复核。{suffix}"
    failures = hard_gate_failures + required_domain_failures
    return f"结构完整度 {score_percent}%，但还有硬缺口：{', '.join(failures) or '未知'}。现在不能说完整。"


def unique_text(values: list[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def source_quote_lines(card: dict[str, Any]) -> list[str]:
    raw_values = source_quote_text_blocks(card)
    lines: list[str] = []
    for value in raw_values:
        lines.extend(extract_json_string_values(value, "text_excerpt"))
        for line in value.splitlines():
            cleaned = " ".join(line.split())
            if cleaned:
                lines.append(cleaned)
    return unique_text(lines)


def source_quote_text_blocks(card: dict[str, Any]) -> list[str]:
    payload = card.get("extracted_payload", {})
    raw_values: list[str] = []
    raw_values.extend(text_values(payload.get("source_quote")))
    if card.get("source_quote"):
        raw_values.extend(text_values(card.get("source_quote")))
    for trace in card.get("source_trace", []) or []:
        if isinstance(trace, dict):
            raw_values.extend(text_values(trace.get("source_quote")))
    return unique_text(raw_values)


def source_quote_field_values(payload: dict[str, list[str]], field: str) -> list[str]:
    values: list[str] = []
    for quote in payload.get("source_quote", []) or []:
        parsed = parse_field_blocks(quote)
        for raw_field, parsed_values in parsed.items():
            parsed_field = FIELD_ALIASES.get(raw_field, raw_field)
            if parsed_field == field:
                values.extend(parsed_values)
    return unique_text(values)


def source_quote_values_for_field(payload: dict[str, list[str]], field: str) -> list[str]:
    parsed_values = source_quote_field_values(payload, field)
    if parsed_values:
        return parsed_values
    loose_values: list[str] = []
    for quote in payload.get("source_quote", []) or []:
        parsed = parse_field_blocks(f"{field}:\n{quote}")
        field_values = parsed.get(field, [])
        if field_values:
            loose_values.extend(field_values)
            continue
        for raw_line in quote.splitlines():
            line = strip_markdown_list_markers(raw_line.strip())
            loose_values.extend(split_inline_field_values(line))
    return unique_text(loose_values) or payload.get("source_quote", []) or []


def markdown_table_values_for_field(card: dict[str, Any], field: str) -> list[str]:
    values: list[str] = []
    for quote in source_quote_text_blocks(card):
        headers: list[str] = []
        for raw_line in quote.splitlines():
            cells = markdown_table_cells(raw_line)
            if not cells:
                headers = []
                continue
            if markdown_table_separator(cells):
                continue
            if not headers:
                headers = cells
                continue
            values.extend(markdown_table_row_values_for_field(headers, cells, field))
    return unique_text(values)


def markdown_table_cells(line: str) -> list[str]:
    stripped = line.strip()
    if not stripped.startswith("|") or "|" not in stripped[1:]:
        return []
    cells = [clean_markdown_table_cell(cell) for cell in stripped.strip("|").split("|")]
    cells = [cell for cell in cells if cell]
    return cells if len(cells) >= 2 else []


def clean_markdown_table_cell(value: str) -> str:
    cleaned = " ".join(value.strip().split())
    return cleaned.strip("`")


def markdown_table_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.replace(" ", "")) for cell in cells)


def markdown_table_row_values_for_field(headers: list[str], cells: list[str], field: str) -> list[str]:
    values: list[str] = []
    row_key = normalize_field_key(cells[0])
    row_field = MARKDOWN_TABLE_FIELD_ALIASES.get(row_key)
    if row_field == field and len(cells) > 1:
        row_value = " | ".join(cells[1:]).strip()
        if row_value:
            values.append(f"{cells[0]}: {row_value}")
    for index, header in enumerate(headers[1:], start=1):
        if index >= len(cells):
            continue
        column_field = MARKDOWN_TABLE_FIELD_ALIASES.get(normalize_field_key(header))
        if column_field != field:
            continue
        value = cells[index].strip()
        if value:
                values.append(f"{cells[0]} - {header}: {value}")
    return values


def markdown_table_plain_candidate_rows(card: dict[str, Any]) -> list[str]:
    rows: list[str] = []
    for quote in source_quote_text_blocks(card):
        for raw_line in quote.splitlines():
            cells = markdown_table_cells(raw_line)
            if not cells or markdown_table_separator(cells) or markdown_table_probable_header_row(cells):
                continue
            if len(cells) < 3:
                continue
            row_key = cells[0].strip()
            row_value = " | ".join(cells[1:]).strip()
            if row_value:
                rows.append(f"{row_key}: {row_value}" if row_key else row_value)
    return unique_text(rows)


def markdown_table_probable_header_row(cells: list[str]) -> bool:
    if not cells:
        return False
    first = cells[0].strip()
    if re.fullmatch(r"\d+", first):
        return False
    normalized = [normalize_field_key(cell) for cell in cells]
    header_tokens = {
        "id",
        "source",
        "status",
        "priority",
        "title",
        "name",
        "type",
        "desc",
        "description",
        "notes",
        "path",
        "owner",
        "use",
        "category",
    }
    headerish = sum(1 for cell in normalized if cell in header_tokens or cell.endswith("_id"))
    return headerish >= 2


def quote_lines_matching(card: dict[str, Any], keywords: list[str]) -> list[str]:
    selected: list[str] = []
    for line in source_quote_lines(card):
        if looks_like_machine_format_line(line):
            continue
        lowered = line.casefold()
        if any(keyword in lowered for keyword in keywords):
            selected.append(line)
    return unique_text(selected)


def looks_like_machine_format_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if stripped.startswith(("|", "{", "}", "[", "]", "```")):
        return True
    if parse_field_header(stripped):
        return True
    if stripped.startswith('"') and re.match(r'^"[A-Za-z0-9_ -]+"\s*:', stripped):
        return True
    return False


def clip_source_quote(text: str, *, max_chars: int = 2000) -> str:
    cleaned = text.strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[:max_chars].rstrip() + "\n..."


def values_for_recipe_field(field: str, payload: dict[str, list[str]], card: dict[str, Any]) -> list[str]:
    if field == "failure_signals":
        return unique_text(
            (payload.get("failure_signals") or [])
            + (payload.get("failure_signal") or [])
            + (payload.get("failed_path") or [])
            + (payload.get("wrong_behavior") or [])
            + (payload.get("current_gap") or [])
            + (payload.get("recurring_failure") or [])
            + (payload.get("why_wrong") or [])
            + json_array_values_from_quote(card, ["reject_if", "hard_rejects", "motion_rejects"], include_key=True)
            + markdown_table_values_for_field(card, field)
            + source_quote_values_for_field(payload, field)
        )
    if field == "verified_path":
        run_steps = run_chain_values_from_quote(card, ["success_means", "steps", "outputs", "verification"])
        return unique_text(
            run_steps
            + (payload.get("steps") or [])
            + (payload.get("verification") or [])
            + (payload.get("outputs") or [])
            + json_array_values_from_quote(
                card,
                ["verification", "contact_sheet", "receipt", "packet_dir", "allowed_next_use"],
                include_key=True,
            )
            + markdown_table_values_for_field(card, field)
            + quote_lines_matching(
                card,
                [
                    "can read",
                    "create",
                    "artifact",
                    "archive",
                    "index",
                    "receipt",
                    "verification",
                    "source_trace",
                    "candidate",
                    "review_queue",
                    "learning_material_info_cards",
                    "learning_material_experience_index",
                    "p1_failure_to_experience_map",
                    "material_deep_deconstruction_notes",
                ],
            )
        )
    if field == "forbidden_path":
        values = unique_text(
            (payload.get("forbidden_path") or [])
            + (payload.get("failed_path") or [])
            + (payload.get("wrong_behavior") or [])
            + (payload.get("why_forbidden") or [])
            + json_array_values_from_quote(card, ["reject_if", "hard_rejects", "motion_rejects", "avoid"], include_key=True)
            + markdown_table_values_for_field(card, field)
            + source_quote_values_for_field(payload, field)
        )
        return values or quote_lines_matching(
            card,
            [
                "must not",
                "do not",
                "cannot",
                "without review",
                "not write",
                "review_queue must",
                "no new visual",
                "no motion render",
                "no remotion",
                "no provider",
                "no real local_storage",
                "no pass",
                "不能",
                "不要",
            ],
        )
    if field == "cannot_claim":
        claim_limits = run_chain_values_from_quote(card, ["does_not_prove", "cannot_claim"])
        quote_limits = [] if claim_limits else quote_lines_matching(card, ["does_not_prove", "does not prove", "cannot_claim", "not prove", "不能说"])
        return proposed_claim_limit_values(unique_text(
            (payload.get("cannot_claim") or [])
            + claim_limits
            + quote_limits
            + markdown_table_values_for_field(card, field)
            + card.get("cannot_claim", [])
        ))
    if field == "visual_check":
        visual_quote_lines = quote_lines_matching(
            card,
            [
                "visual",
                "强字卡",
                "重点字卡",
                "标题卡",
                "字卡",
                "大字",
                "首帧",
                "花字",
                "字幕",
                "文字图层",
                "图层",
                "透明度",
                "不透明度",
                "关键帧",
                "阴影",
                "颜色区分",
                "颜色",
                "位置参数",
                "Y轴",
                "y轴",
                "入场动画",
                "动画效果",
                "蒙版",
                "抠像",
                "描边",
                "遮挡",
                "前景",
                "背景",
                "安全区",
                "pip",
                "typography",
                "subtitle",
                "presenter",
                "matte",
                "opacity",
                "font",
                "color",
                "placement",
                "layer",
                "keyframe",
                "visual_strategy",
                "caption_strategy",
                "keyword_card",
                "card_fullscreen",
                "big_subtitle",
                "template_card",
                "template_label",
                "role_label",
                "source_kind",
            ],
        )
        return unique_text(
            (payload.get("visual_check") or [])
            + (payload.get("good_example") or [])
            + json_array_values_from_quote(
                card,
                ["z_order_bottom_to_top", "requirements", "review_questions", "pass_questions", "color_relation"],
                include_key=True,
            )
            + markdown_table_values_for_field(card, field)
            + filter_visual_quote_noise(visual_quote_lines)
        )
    if field == "checklist_item":
        return unique_text(
            (payload.get("checklist_item") or [])
            + (payload.get("action_change") or [])
            + (payload.get("verification") or [])
            + (payload.get("correct_behavior") or [])
            + (payload.get("check") or [])
            + (payload.get("expected_output") or [])
            + (payload.get("acceptance_check") or [])
            + (payload.get("stop_after") or [])
            + json_array_values_from_quote(
                card,
                ["z_order_bottom_to_top", "requirements", "review_questions", "pass_questions", "color_relation"],
                include_key=True,
            )
            + markdown_table_values_for_field(card, field)
            + source_quote_values_for_field(payload, field)
            + markdown_table_plain_candidate_rows(card)
        )
    if field == "good_example":
        return payload.get("good_example") or payload.get("correct_behavior") or source_quote_values_for_field(payload, field)
    if field == "bad_example":
        return payload.get("bad_example") or payload.get("wrong_behavior") or source_quote_values_for_field(payload, field)
    if field == "fallback_allowed":
        return payload.get("fallback_allowed") or source_quote_values_for_field(payload, field)
    if field == "fallback_forbidden":
        return unique_text(
            (payload.get("fallback_forbidden") or [])
            + (payload.get("failed_path") or [])
            + json_array_values_from_quote(card, ["reject_if", "hard_rejects", "motion_rejects"], include_key=True)
            + source_quote_values_for_field(payload, field)
        )
    if field == "pressure_test":
        return payload.get("pressure_test") or payload.get("verification") or source_quote_values_for_field(payload, field)
    if field == "source_truth_to_read":
        return unique_text(
            (payload.get("source_truth_to_read") or [])
            + (payload.get("must_read_sources") or [])
            + (payload.get("first_patch_target") or [])
        )
    if field == "source_trace":
        return [stable_json(item) for item in card.get("source_trace", [])]
    return unique_text((payload.get(field, []) or []) + markdown_table_values_for_field(card, field))


def json_array_values_from_quote(card: dict[str, Any], keys: list[str], *, include_key: bool = False) -> list[str]:
    values: list[str] = []
    quote = str(card.get("source_quote") or "")
    for key in keys:
        values.extend(extract_json_array_strings(quote, key, include_key=include_key))
        values.extend(extract_json_string_values(quote, key, include_key=include_key))
    return unique_text(values)


def filter_visual_quote_noise(lines: list[str]) -> list[str]:
    noise_terms = [
        "字幕配音",
        "配音用哪个",
        "背景音乐",
        "爆款音效",
        "画面色调",
        "调色",
        "voice_or_avatar_audio",
        "presenter_source",
        "presenter_original",
        "local_avatar",
        "avataradapter",
        "真人音频驱动",
    ]
    return [line for line in lines if not any(term in line for term in noise_terms)]


def run_chain_values_from_quote(card: dict[str, Any], keys: list[str]) -> list[str]:
    values: list[str] = []
    quote = str(card.get("source_quote") or "")
    for key in keys:
        values.extend(extract_json_array_strings(quote, key))
    return unique_text(values)


def extract_json_array_strings(text: str, key: str, *, include_key: bool = False) -> list[str]:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*\[(.*?)(?:\]|\Z)', re.DOTALL)
    values: list[str] = []
    for match in pattern.finditer(text):
        values.extend(re.findall(r'"([^"]+)"', match.group(1)))
    return format_json_key_values(key, values, include_key=include_key)


def extract_json_string_values(text: str, key: str, *, include_key: bool = False) -> list[str]:
    pattern = re.compile(rf'"{re.escape(key)}"\s*:\s*"([^"]+)"')
    values = [match.group(1) for match in pattern.finditer(text)]
    return format_json_key_values(key, values, include_key=include_key)


def format_json_key_values(key: str, values: list[str], *, include_key: bool) -> list[str]:
    formatted: list[str] = []
    for value in values:
        cleaned = " ".join(str(value).split())
        if not cleaned:
            continue
        formatted.append(f"{key}: {cleaned}" if include_key else cleaned)
    return formatted


def recipe_title_from_cards(target_recipe_id: str, cards: list[dict[str, Any]]) -> str:
    for card in cards:
        heading_title = title_from_card_heading(card)
        if heading_title:
            return heading_title
    for card in cards:
        payload = card.get("extracted_payload", {})
        for key in ("correction", "correct_behavior", "action_change", "replacement_path", "steps", "visual_check", "check"):
            values = text_values(payload.get(key))
            if values:
                return first_line(values[0])
    recipe_title = title_from_recipe_id(target_recipe_id)
    if recipe_title:
        return recipe_title
    providers = unique_text([str(card.get("provider", "")).strip() for card in cards])
    if providers:
        labels = {"deepseek": "DeepSeek", "qwen3": "Qwen3", "cognee": "Cognee", "graphiti": "Graphiti"}
        provider = labels.get(providers[0].casefold(), providers[0])
        return f"{provider} source refinement"
    return f"Source refinery patch: {target_recipe_id}"


def title_from_recipe_id(recipe_id: str) -> str:
    text = re.sub(r"^recipe[_-]?", "", recipe_id.strip(), flags=re.IGNORECASE)
    text = re.sub(r"[_-]v\d+$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[_-]+", " ", text).strip()
    if not text:
        return ""
    acronyms = {"ai", "api", "asr", "bgm", "cli", "llm", "mcp", "ocr", "sfx", "ui", "ux", "vfx"}
    words = [
        word.upper()
        if word.casefold() in acronyms or word.isupper() or any(char.isdigit() for char in word)
        else word.capitalize()
        for word in text.split()
    ]
    return " ".join(words)


def title_from_card_heading(card: dict[str, Any]) -> str:
    source_quote = str(card.get("source_quote") or "")
    for raw_line in source_quote.splitlines():
        line = raw_line.strip()
        heading = re.match(r"^#{1,6}\s+(.+)$", line)
        if not heading:
            continue
        title = heading.group(1).strip()
        title = re.sub(r"^card\s+[A-Za-z0-9_.-]+\s*[-:]\s*", "", title, flags=re.IGNORECASE).strip()
        if title:
            return first_line(title)
    return ""


def patch_draft_body(proposed_additions: dict[str, list[str]], cards: list[dict[str, Any]]) -> str:
    fields = [
        "verified_path",
        "checklist_item",
        "forbidden_path",
        "failure_signals",
        "visual_check",
        "good_example",
        "bad_example",
        "cannot_claim",
    ]
    lines: list[str] = []
    for field in fields:
        for value in proposed_additions.get(field, []):
            lines.append(f"{field}: {value}")
    if lines:
        return "\n".join(lines)
    return "\n".join(
        f"source_truth_to_read: {ref}"
        for ref in source_truth_refs_from_cards(cards)
    )


def source_truth_refs_from_cards(cards: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for card in cards:
        card_id = str(card.get("card_id") or "").strip()
        traces = card.get("source_trace") if isinstance(card.get("source_trace"), list) else []
        if not traces:
            if card_id:
                refs.append(card_id)
            continue
        for trace in traces:
            if not isinstance(trace, dict):
                continue
            path = str(trace.get("path") or "").strip()
            source_id = str(trace.get("source_id") or "").strip()
            record_id = str(trace.get("record_id") or "").strip()
            parts = []
            if path:
                parts.append(f"path={path}")
            if source_id:
                parts.append(f"source_id={source_id}")
            if record_id:
                parts.append(f"record_id={record_id}")
            if card_id:
                parts.append(f"card_id={card_id}")
            if parts:
                refs.append("; ".join(parts))
        if card_id and not any(card_id in ref for ref in refs):
            refs.append(card_id)
    return unique_text(refs)


def source_refinery_review_hints(cards: list[dict[str, Any]], proposed_additions: dict[str, list[str]]) -> dict[str, Any]:
    total_values = sum(len(values) for values in proposed_additions.values())
    split_reasons: list[str] = []
    if len(cards) > 40:
        split_reasons.append(f"候选卡片有 {len(cards)} 张，建议拆小后再 accept。")
    if total_values > 60:
        split_reasons.append(f"候选字段值有 {total_values} 条，建议按 knowledge_need 或目标字段拆小。")
    if len(proposed_additions.get("checklist_item", [])) > 30:
        split_reasons.append("checklist_item 太多，建议拆成更小的执行规则。")
    return {
        "split_recommended": bool(split_reasons),
        "split_reasons": split_reasons,
        "card_count": len(cards),
        "proposed_value_count": total_values,
    }


def source_refinery_plain_review_summary(
    target_recipe_id: str,
    cards: list[dict[str, Any]],
    proposed_additions: dict[str, list[str]],
    review_hints: dict[str, Any],
) -> dict[str, Any]:
    field_names = sorted(proposed_additions)
    field_counts = {field: len(proposed_additions[field]) for field in field_names}
    sample_changes: list[str] = []
    for field in field_names:
        for value in proposed_additions[field][:3]:
            sample_changes.append(f"{field}: {first_line(value, max_chars=140)}")
            if len(sample_changes) >= 6:
                break
        if len(sample_changes) >= 6:
            break
    split_recommended = bool(review_hints.get("split_recommended"))
    if split_recommended:
        risk = "这包候选偏大，直接 accept 容易把太多规则混进同一个菜谱。"
        next_step = "先拆小或 supersede 成更窄的 patch，再决定是否 accept。"
    else:
        risk = "这包候选大小正常，但内容仍然只是候选，不能自动进正式菜谱。"
        next_step = "人工看 sample_changes 和 source cards 后，再 accept / reject / supersede。"
    return {
        "title": f"待审：{recipe_title_from_cards(target_recipe_id, cards)}",
        "what_this_is": "source_refinery 从资料卡片生成的候选补丁，还没有写进正式 recipe。",
        "target_recipe_id": target_recipe_id,
        "card_count": len(cards),
        "proposed_value_count": int(review_hints.get("proposed_value_count") or 0),
        "fields_to_review": [f"{field} ({count} 条)" for field, count in field_counts.items()],
        "sample_changes": sample_changes,
        "risk": risk,
        "why_review": "只有 review_queue 被接受后，系统才允许生成或修改正式 recipe。",
        "next_step": next_step,
        "cannot_claim": [
            "不能说这些候选已经通过人工审核。",
            "不能说它已经写进正式 recipe。",
            "不能说真实任务质量已经通过。",
        ],
    }


def steps_from_patch_draft(proposed_additions: dict[str, list[str]], fallback_body: str) -> list[str]:
    steps = []
    steps.extend(proposed_additions.get("verified_path", []))
    steps.extend(proposed_additions.get("checklist_item", []))
    steps.extend(proposed_additions.get("visual_check", []))
    if not steps:
        steps.extend(proposed_additions.get("forbidden_path", []))
    if steps:
        return unique_text(steps)
    return [fallback_body] if fallback_body else []


def recipe_from_patch_draft(target_recipe_id: str, patch_draft: dict[str, Any], cards: list[dict[str, Any]]) -> dict[str, Any]:
    title = recipe_title_from_cards(target_recipe_id, cards)
    evidence_refs = patch_draft["source_card_ids"]
    body = patch_draft_body(patch_draft["proposed_additions"], cards)
    recipe = recipe_draft(target_recipe_id, title, body, evidence_refs)
    recipe["scope"] = "Source refinery generated review-gated recipe."
    recipe["steps"] = steps_from_patch_draft(patch_draft["proposed_additions"], body)
    recipe["verified_path"] = []
    for field, values in patch_draft["proposed_additions"].items():
        current = recipe.get(field, [])
        if not isinstance(current, list):
            current = [str(current)]
        recipe[field] = list(dict.fromkeys(current + values))
    source_truth_refs = source_truth_refs_from_cards(cards)
    recipe["source_truth_to_read"] = list(dict.fromkeys(recipe.get("source_truth_to_read", []) + source_truth_refs + evidence_refs))
    recipe["evidence_refs"] = list(dict.fromkeys(recipe.get("evidence_refs", []) + evidence_refs))
    recipe["cannot_claim"] = list(
        dict.fromkeys(
            recipe.get("cannot_claim", [])
            + patch_draft.get("cannot_claim", [])
            + ["不能说 source_refinery patch draft 已经在真实任务中验证。"]
        )
    )
    return recipe


def merge_recipe_update(current: dict[str, Any], proposed: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in proposed.items():
        if key in {"recipe_id", "version", "recipe_hash"}:
            continue
        if isinstance(value, list):
            existing = merged.get(key, [])
            if not isinstance(existing, list):
                existing = [str(existing)] if existing else []
            merged[key] = list(dict.fromkeys(existing + value))
            continue
        if isinstance(value, dict):
            existing = merged.get(key)
            if isinstance(existing, dict):
                merged[key] = {**existing, **value}
            elif not existing:
                merged[key] = value
            continue
        if key not in merged or merged.get(key) in (None, "", []):
            merged[key] = value
    merged["recipe_id"] = current["recipe_id"]
    return merged


def timestamp_to_seconds(value: str) -> str:
    parts = value.replace(",", ".").split(":")
    if len(parts) == 2:
        hours = 0
        minutes, seconds = parts
    else:
        hours, minutes, seconds = parts
    total = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return f"{total:.3f}"


def extract_keyframes_with_ffmpeg(video_path: Path, output_dir: Path, cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RecipesError(
            "AR265",
            "本机找不到 ffmpeg。",
            "无法抽取本地视频关键帧。",
            "安装或配置 ffmpeg 后再运行，或去掉 --extract-keyframes。",
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    keyframes: list[dict[str, Any]] = []
    for cue in cues:
        out_path = output_dir / f"cue_{cue['index']:04d}.jpg"
        cmd = [
            ffmpeg,
            "-y",
            "-ss",
            timestamp_to_seconds(cue["start"]),
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(out_path),
        ]
        proc = subprocess.run(cmd, text=True, capture_output=True, check=False)
        if proc.returncode != 0 or not out_path.exists():
            raise RecipesError(
                "AR266",
                "ffmpeg 抽关键帧失败。",
                proc.stderr[-1000:] or f"命令失败：{' '.join(cmd)}",
                "检查视频文件是否可读，或去掉 --extract-keyframes。",
            )
        keyframes.append(
            {
                "cue_index": cue["index"],
                "start": cue["start"],
                "path": str(out_path),
                "hash": file_sha256(out_path),
            }
        )
    return keyframes
