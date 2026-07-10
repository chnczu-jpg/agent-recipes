from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from agent_recipes.client_config import install_client_config
from agent_recipes.core import RecipesError, RecipesProject
from agent_recipes.dependencies import adapter_lock, system_lock
from agent_recipes.install import client_smoke, install_skill
from agent_recipes.learning_quality import run_learning_quality_summary
from agent_recipes.mcp import TOOL_NAMES, call_tool, descriptor, serve_stdio
from agent_recipes.release import EXPORT_DIR_DEFAULT, export_open_source, release_audit


def add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--project", default=".", help="Project root. Defaults to cwd.")
    parser.add_argument("--json", action="store_true", help="Emit JSON output.")
    parser.add_argument("--dry-run", action="store_true", help="Validate command without writing when supported.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-recipes")
    sub = parser.add_subparsers(dest="command", required=True)

    init_p = sub.add_parser("init")
    add_common(init_p)

    migration_status_p = sub.add_parser("migration-status")
    add_common(migration_status_p)

    migrate_p = sub.add_parser("migrate")
    migrate_p.add_argument("--target", default="1.0")
    add_common(migrate_p)

    sources_p = sub.add_parser("sources")
    sources_sub = sources_p.add_subparsers(dest="sources_command", required=True)
    sources_add = sources_sub.add_parser("add")
    sources_add.add_argument("path")
    sources_add.add_argument("--read-only", action="store_true", help="Register source as read-only.")
    add_common(sources_add)

    capture_p = sub.add_parser("capture")
    capture_p.add_argument("--type", required=True, choices=["correction", "success", "failure", "unknown"])
    capture_p.add_argument("--text", required=True)
    capture_p.add_argument("--task", default="")
    capture_p.add_argument("--lock")
    capture_p.add_argument("--idempotency-key")
    add_common(capture_p)

    compile_p = sub.add_parser("compile")
    compile_p.add_argument("--max-candidates", type=int)
    add_common(compile_p)

    scan_p = sub.add_parser("scan")
    scan_p.add_argument("--depth", default="shallow", choices=["shallow", "medium"])
    add_common(scan_p)

    review_p = sub.add_parser("review")
    group = review_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--accept")
    group.add_argument("--reject")
    group.add_argument("--merge")
    group.add_argument("--split")
    group.add_argument("--supersede")
    review_p.add_argument("--reason", default="")
    review_p.add_argument("--lock")
    add_common(review_p)

    lookup_p = sub.add_parser("lookup")
    lookup_p.add_argument("query")
    lookup_p.add_argument("--strict", action="store_true")
    lookup_p.add_argument("--min-score", type=int, default=2)
    add_common(lookup_p)

    lock_p = sub.add_parser("lock")
    lock_p.add_argument("--recipe", required=True)
    lock_p.add_argument("--task", default="")
    lock_p.add_argument("--query")
    lock_p.add_argument("--min-score", type=int, default=2)
    add_common(lock_p)

    lifecycle_p = sub.add_parser("recipe-lifecycle")
    lifecycle_p.add_argument("--action", required=True, choices=["status", "tombstone", "revoke"])
    lifecycle_p.add_argument("--recipe")
    lifecycle_p.add_argument("--tombstone-id")
    lifecycle_p.add_argument(
        "--reason-kind",
        default="correction",
        choices=["correction", "supersession", "retraction", "contradiction_resolution"],
    )
    lifecycle_p.add_argument("--reason", default="")
    lifecycle_p.add_argument("--lock")
    add_common(lifecycle_p)

    search_p = sub.add_parser("search")
    search_p.add_argument("query")
    search_p.add_argument("--limit", type=int, default=5)
    search_p.add_argument("--kind", choices=["all", "source", "video"], default="all")
    search_p.add_argument("--source-path-contains", action="append", default=[])
    add_common(search_p)

    refine_p = sub.add_parser("refine")
    refine_p.add_argument("--query", required=True)
    refine_p.add_argument("--knowledge-need", required=True)
    refine_p.add_argument("--target-recipe", required=True)
    refine_p.add_argument("--candidate-fields", required=True)
    refine_p.add_argument("--limit", type=int, default=20)
    refine_p.add_argument("--kind", choices=["all", "source", "video"], default="all")
    refine_p.add_argument("--source-path-contains", action="append", default=[])
    add_common(refine_p)

    extract_cards_p = sub.add_parser("extract-cards")
    extract_cards_p.add_argument("--refinement")
    add_common(extract_cards_p)

    patch_draft_p = sub.add_parser("patch-draft")
    patch_draft_p.add_argument("--target-recipe", required=True)
    add_common(patch_draft_p)

    fusion_p = sub.add_parser("knowledge-fusion")
    fusion_p.add_argument("--target-recipe", required=True)
    add_common(fusion_p)

    deep_read_plan_p = sub.add_parser("deep-read-plan")
    deep_read_plan_p.add_argument("--fusion", required=True)
    add_common(deep_read_plan_p)

    target_suggestions_p = sub.add_parser("target-suggestions")
    target_suggestions_p.add_argument("--target-recipe")
    target_suggestions_p.add_argument("--status", choices=["rejected", "pending", "all"], default="rejected")
    target_suggestions_p.add_argument("--min-reviews", type=int, default=1)
    add_common(target_suggestions_p)

    recover_p = sub.add_parser("recover")
    recover_p.add_argument("--problem", required=True)
    recover_p.add_argument("--idempotency-key")
    add_common(recover_p)

    ingest_p = sub.add_parser("ingest-video")
    ingest_p.add_argument("--transcript", required=True)
    ingest_p.add_argument("--video")
    ingest_p.add_argument("--extract-keyframes", action="store_true")
    add_common(ingest_p)

    doctor_p = sub.add_parser("doctor")
    add_common(doctor_p)

    readiness_p = sub.add_parser("readiness")
    add_common(readiness_p)

    outcome_status_p = sub.add_parser("outcome-status")
    outcome_status_p.add_argument("--recipe")
    add_common(outcome_status_p)

    capabilities_p = sub.add_parser("capabilities")
    add_common(capabilities_p)

    convert_p = sub.add_parser("convert-doc")
    convert_p.add_argument("--input", required=True)
    convert_p.add_argument("--adapter", choices=["markitdown", "docling"], default="markitdown")
    add_common(convert_p)

    scenes_p = sub.add_parser("detect-scenes")
    scenes_p.add_argument("--video", required=True)
    scenes_p.add_argument("--adapter", choices=["pyscenedetect"], default="pyscenedetect")
    add_common(scenes_p)

    transcribe_p = sub.add_parser("transcribe")
    transcribe_p.add_argument("--input", required=True)
    transcribe_p.add_argument("--adapter", choices=["faster-whisper", "whisperx"], default="faster-whisper")
    transcribe_p.add_argument("--model", default="tiny.en")
    add_common(transcribe_p)

    ocr_p = sub.add_parser("ocr-image")
    ocr_p.add_argument("--input", required=True)
    ocr_p.add_argument("--adapter", choices=["paddleocr", "surya"], default="paddleocr")
    add_common(ocr_p)

    memory_index_p = sub.add_parser("memory-index")
    memory_index_p.add_argument("--adapter", choices=["cognee", "graphiti"], default="cognee")
    add_common(memory_index_p)

    memory_search_p = sub.add_parser("memory-search")
    memory_search_p.add_argument("query")
    memory_search_p.add_argument("--adapter", choices=["cognee", "graphiti"], default="cognee")
    memory_search_p.add_argument("--limit", type=int, default=5)
    add_common(memory_search_p)

    memory_status_p = sub.add_parser("memory-status")
    memory_status_p.add_argument("--adapter", choices=["all", "cognee", "graphiti"], default="all")
    add_common(memory_status_p)

    recall_boundary_p = sub.add_parser("recall-boundary")
    add_common(recall_boundary_p)

    evidence_quarantine_p = sub.add_parser("evidence-quarantine")
    evidence_quarantine_p.add_argument("--action", required=True, choices=["status", "apply", "release"])
    evidence_quarantine_p.add_argument("--quarantine-id")
    add_common(evidence_quarantine_p)

    evidence_pack_p = sub.add_parser("evidence-pack")
    evidence_pack_p.add_argument("--lock", required=True)
    evidence_pack_p.add_argument("--max-bytes", type=int, default=65536)
    evidence_pack_p.add_argument("--privacy", choices=["minimal", "project_local"], default="project_local")
    add_common(evidence_pack_p)

    memory_native_probe_p = sub.add_parser("memory-native-probe")
    memory_native_probe_p.add_argument("--adapter", choices=["cognee", "graphiti"], default="cognee")
    memory_native_probe_p.add_argument("--timeout", type=int, default=20)
    add_common(memory_native_probe_p)

    memory_semantic_probe_p = sub.add_parser("memory-semantic-probe")
    memory_semantic_probe_p.add_argument("--adapter", choices=["cognee"], default="cognee")
    memory_semantic_probe_p.add_argument("--timeout", type=int, default=30)
    add_common(memory_semantic_probe_p)

    memory_semantic_configure_p = sub.add_parser("memory-semantic-configure")
    memory_semantic_configure_p.add_argument("--adapter", choices=["cognee"], default="cognee")
    memory_semantic_configure_p.add_argument("--detect-only", action="store_true")
    memory_semantic_configure_p.add_argument("--llm-provider")
    memory_semantic_configure_p.add_argument("--llm-model")
    memory_semantic_configure_p.add_argument("--llm-endpoint")
    memory_semantic_configure_p.add_argument("--llm-api-key-env", default="AGENT_RECIPES_DEEPSEEK_API_KEY")
    memory_semantic_configure_p.add_argument("--embedding-provider")
    memory_semantic_configure_p.add_argument("--embedding-model")
    memory_semantic_configure_p.add_argument("--embedding-endpoint")
    memory_semantic_configure_p.add_argument("--embedding-dimensions", type=int)
    add_common(memory_semantic_configure_p)

    cloud_configure_p = sub.add_parser("cloud-configure")
    cloud_configure_p.add_argument("--provider", choices=["deepseek"], default="deepseek")
    cloud_configure_p.add_argument("--model", default="deepseek-v4-flash")
    cloud_configure_p.add_argument("--pro-model", default="deepseek-v4-pro")
    cloud_configure_p.add_argument("--base-url", default="https://api.deepseek.com")
    cloud_configure_p.add_argument("--api-key-env", default="AGENT_RECIPES_DEEPSEEK_API_KEY")
    add_common(cloud_configure_p)

    cloud_status_p = sub.add_parser("cloud-status")
    cloud_status_p.add_argument("--provider", choices=["all", "deepseek"], default="all")
    add_common(cloud_status_p)

    cloud_refine_p = sub.add_parser("cloud-refine")
    cloud_refine_p.add_argument("--provider", choices=["deepseek"], default="deepseek")
    cloud_refine_p.add_argument("--input", required=True)
    cloud_refine_p.add_argument("--knowledge-need", required=True)
    cloud_refine_p.add_argument("--target-recipe", required=True)
    cloud_refine_p.add_argument("--candidate-fields", required=True)
    cloud_refine_p.add_argument("--response-json")
    cloud_refine_p.add_argument("--allow-network", action="store_true")
    cloud_refine_p.add_argument("--model")
    cloud_refine_p.add_argument("--timeout", type=int, default=60)
    add_common(cloud_refine_p)

    embedding_configure_p = sub.add_parser("embedding-configure")
    embedding_configure_p.add_argument("--provider", choices=["qwen3"], default="qwen3")
    embedding_configure_p.add_argument("--model", default="qwen3-embedding:0.6b")
    embedding_configure_p.add_argument("--endpoint", default="http://127.0.0.1:11434/api/embed")
    embedding_configure_p.add_argument("--dimensions", type=int, default=1024)
    add_common(embedding_configure_p)

    embedding_status_p = sub.add_parser("embedding-status")
    embedding_status_p.add_argument("--provider", choices=["all", "qwen3"], default="all")
    add_common(embedding_status_p)

    embedding_index_p = sub.add_parser("embedding-index")
    embedding_index_p.add_argument("--provider", choices=["qwen3"], default="qwen3")
    embedding_index_p.add_argument("--response-json")
    embedding_index_p.add_argument("--allow-loopback", action="store_true")
    embedding_index_p.add_argument("--timeout", type=int, default=60)
    add_common(embedding_index_p)

    embedding_search_p = sub.add_parser("embedding-search")
    embedding_search_p.add_argument("query")
    embedding_search_p.add_argument("--provider", choices=["qwen3"], default="qwen3")
    embedding_search_p.add_argument("--response-json")
    embedding_search_p.add_argument("--allow-loopback", action="store_true")
    embedding_search_p.add_argument("--limit", type=int, default=5)
    embedding_search_p.add_argument("--timeout", type=int, default=60)
    add_common(embedding_search_p)

    quality_p = sub.add_parser("quality-benchmark")
    quality_p.add_argument("--qwen-response-json")
    quality_p.add_argument("--allow-loopback", action="store_true")
    quality_p.add_argument("--limit", type=int, default=5)
    quality_p.add_argument("--timeout", type=int, default=60)
    add_common(quality_p)

    recall_quality_p = sub.add_parser("recall-quality-benchmark")
    recall_quality_p.add_argument("--cases", required=True)
    recall_quality_p.add_argument("--backends", default="core,cognee,graphiti,qwen")
    recall_quality_p.add_argument("--allow-loopback", action="store_true")
    recall_quality_p.add_argument("--limit", type=int, default=5)
    recall_quality_p.add_argument("--min-score", type=int, default=2)
    recall_quality_p.add_argument("--qwen-min-score", type=float, default=0.55)
    recall_quality_p.add_argument("--timeout", type=int, default=60)
    add_common(recall_quality_p)

    lookup_pressure_p = sub.add_parser("lookup-pressure")
    lookup_pressure_p.add_argument("--cases", required=True)
    add_common(lookup_pressure_p)

    lock_pressure_p = sub.add_parser("lock-pressure")
    lock_pressure_p.add_argument("--cases", required=True)
    add_common(lock_pressure_p)

    consumption_coverage_p = sub.add_parser("consumption-coverage")
    add_common(consumption_coverage_p)

    real_pressure_summary_p = sub.add_parser("real-pressure-summary")
    real_pressure_summary_p.add_argument("--projects-root", default=".recipes_real_tests")
    real_pressure_summary_p.add_argument("--name-contains")
    add_common(real_pressure_summary_p)

    learning_quality_summary_p = sub.add_parser("learning-quality-summary")
    learning_quality_summary_p.add_argument("--projects-root", default=".recipes_real_tests")
    learning_quality_summary_p.add_argument("--cohort", required=True)
    add_common(learning_quality_summary_p)

    duplicate_governance_p = sub.add_parser("duplicate-governance")
    add_common(duplicate_governance_p)

    candidate_quality_p = sub.add_parser("candidate-quality-benchmark")
    candidate_quality_p.add_argument("--cases", required=True)
    add_common(candidate_quality_p)

    completeness_p = sub.add_parser("completeness-audit")
    completeness_p.add_argument("--input", required=True)
    completeness_p.add_argument("--subject-type", required=True, choices=["skill", "course"])
    completeness_p.add_argument("--requirements")
    completeness_p.add_argument("--software-map")
    completeness_p.add_argument("--execution-evidence")
    add_common(completeness_p)

    course_skill_p = sub.add_parser("course-skill-draft")
    course_skill_p.add_argument("--transcript", required=True)
    course_skill_p.add_argument("--spec", required=True)
    course_skill_p.add_argument("--software-map", required=True)
    add_common(course_skill_p)

    review_triage_p = sub.add_parser("review-triage")
    review_triage_p.add_argument("--target-recipe")
    review_triage_p.add_argument("--target-prefix")
    review_triage_p.add_argument("--status", choices=["pending", "rejected", "all"], default="pending")
    review_triage_p.add_argument("--min-values", type=int, default=2)
    review_triage_p.add_argument("--max-values", type=int, default=40)
    review_triage_p.add_argument("--include-older", action="store_true")
    add_common(review_triage_p)

    review_packet_p = sub.add_parser("review-packet")
    review_packet_p.add_argument("--target-recipe")
    review_packet_p.add_argument("--target-prefix")
    review_packet_p.add_argument("--status", choices=["pending", "rejected", "all"], default="pending")
    review_packet_p.add_argument("--min-values", type=int, default=2)
    review_packet_p.add_argument("--max-values", type=int, default=40)
    review_packet_p.add_argument("--max-items", type=int, default=20)
    review_packet_p.add_argument("--include-older", action="store_true")
    add_common(review_packet_p)

    self_run_p = sub.add_parser("self-run-benchmark")
    self_run_p.add_argument("--query", required=True)
    self_run_p.add_argument("--knowledge-need", required=True)
    self_run_p.add_argument("--target-recipe", required=True)
    self_run_p.add_argument("--candidate-fields", required=True)
    self_run_p.add_argument("--min-cards", type=int, default=1)
    self_run_p.add_argument("--limit", type=int, default=20)
    self_run_p.add_argument("--kind", choices=["all", "source", "video"], default="all")
    self_run_p.add_argument("--scan-depth", choices=["shallow", "medium"], default="shallow")
    self_run_p.add_argument("--source-path-contains", action="append", default=[])
    add_common(self_run_p)

    repeat_error_p = sub.add_parser("repeat-error-benchmark")
    repeat_error_p.add_argument("--cases", required=True)
    repeat_error_p.add_argument("--min-cases", type=int, default=5)
    repeat_error_p.add_argument("--min-improvements", type=int, default=3)
    add_common(repeat_error_p)

    output_quality_p = sub.add_parser("output-quality-benchmark")
    output_quality_p.add_argument("--cases", required=True)
    output_quality_p.add_argument("--min-cases", type=int, default=1)
    output_quality_p.add_argument("--min-passed", type=int, default=1)
    add_common(output_quality_p)

    install_p = sub.add_parser("install-skill")
    install_p.add_argument("--agent", required=True, choices=["codex", "claude", "hermes"])
    install_p.add_argument("--scope", required=True, choices=["project", "user"])
    add_common(install_p)

    smoke_p = sub.add_parser("client-smoke")
    smoke_p.add_argument("--agent", required=True, choices=["codex", "claude", "hermes"])
    smoke_p.add_argument("--scope", required=True, choices=["project", "user"])
    add_common(smoke_p)

    adapter_lock_p = sub.add_parser("adapter-lock")
    add_common(adapter_lock_p)

    system_lock_p = sub.add_parser("system-lock")
    add_common(system_lock_p)

    install_client_p = sub.add_parser("install-client")
    install_client_p.add_argument("--agent", required=True, choices=["codex", "claude", "hermes"])
    install_client_p.add_argument("--config-path")
    add_common(install_client_p)

    audit_p = sub.add_parser("open-source-audit")
    audit_p.add_argument("--output-dir", default=EXPORT_DIR_DEFAULT)
    add_common(audit_p)

    export_p = sub.add_parser("open-source-export")
    export_p.add_argument("--output-dir", default=EXPORT_DIR_DEFAULT)
    add_common(export_p)

    mcp_p = sub.add_parser("mcp")
    mcp_p.add_argument("--stdio", action="store_true", help="Run JSONL stdio MCP skeleton.")
    mcp_p.add_argument("--tool", choices=TOOL_NAMES)
    mcp_p.add_argument("--arguments-json", default="{}")
    add_common(mcp_p)

    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    project = RecipesProject(Path(args.project))
    if args.command == "install-skill":
        return install_skill(Path(args.project), agent=args.agent, scope=args.scope, dry_run=args.dry_run)
    if args.command == "client-smoke":
        return client_smoke(Path(args.project), agent=args.agent, scope=args.scope)
    if args.command == "adapter-lock":
        return adapter_lock(Path(args.project))
    if args.command == "system-lock":
        return system_lock(Path(args.project))
    if args.command == "install-client":
        return install_client_config(Path(args.project), agent=args.agent, config_path=args.config_path)
    if args.command == "open-source-audit":
        return release_audit(Path(args.project), output_dir=args.output_dir)
    if args.command == "open-source-export":
        if args.dry_run:
            result = release_audit(Path(args.project), output_dir=args.output_dir)
            result["action"] = "open-source-export"
            result["dry_run"] = True
            result["claim_status"]["cannot_claim"].append("不能说已经生成导出目录。")
            return result
        return export_open_source(Path(args.project), output_dir=args.output_dir)
    if args.command == "mcp":
        if args.stdio:
            raise RecipesError(
                "AR621",
                "stdio MCP server 不能通过 run() 返回。",
                "请通过 main() 直接进入 serve_stdio。",
                "运行 agent-recipes mcp --stdio。",
            )
        if args.tool:
            try:
                arguments = json.loads(args.arguments_json)
            except json.JSONDecodeError as exc:
                raise RecipesError("AR620", "arguments-json 不是合法 JSON。", str(exc)) from exc
            return call_tool(args.tool, arguments, project=args.project)
        return descriptor(args.project)
    if args.dry_run:
        return {
            "ok": True,
            "action": args.command,
            "dry_run": True,
            "claim_status": {
                "verified": ["命令参数已解析。"],
                "inferred": [],
                "missing_evidence": ["dry-run 未读取或写入项目状态。"],
                "cannot_claim": ["不能说命令已经执行。"],
            },
        }
    if args.command == "init":
        return project.init()
    if args.command == "migration-status":
        return project.migration_status()
    if args.command == "migrate":
        return project.migrate(target=args.target)
    if args.command == "sources" and args.sources_command == "add":
        permission = "read-only" if args.read_only else "read-write"
        return project.add_source(args.path, permission=permission)
    if args.command == "capture":
        return project.capture(
            args.type,
            args.text,
            task=args.task,
            lock_id=args.lock,
            idempotency_key=args.idempotency_key,
        )
    if args.command == "compile":
        return project.compile(max_candidates=args.max_candidates)
    if args.command == "scan":
        return project.scan(depth=args.depth)
    if args.command == "review" and args.accept:
        return project.accept_review(args.accept, lock_id=args.lock)
    if args.command == "review" and args.reject:
        return project.reject_review(args.reject, reason=args.reason)
    if args.command == "review" and args.merge:
        return project.decide_fusion_review(args.merge, decision="merge", lock_id=args.lock)
    if args.command == "review" and args.split:
        return project.decide_fusion_review(args.split, decision="split", lock_id=args.lock)
    if args.command == "review" and args.supersede:
        return project.decide_fusion_review(args.supersede, decision="supersede", lock_id=args.lock)
    if args.command == "lookup":
        return project.lookup(args.query, strict=args.strict, min_score=args.min_score)
    if args.command == "lock":
        return project.create_lock(args.recipe, task=args.task, query=args.query, min_score=args.min_score)
    if args.command == "recipe-lifecycle":
        if args.action == "status":
            return project.recipe_lifecycle_status(recipe_id=args.recipe)
        if args.action == "tombstone":
            if not args.recipe:
                raise RecipesError("AR433", "tombstone 缺 recipe id。", "没有 --recipe。", "传入 --recipe <recipe_id>。")
            return project.tombstone_recipe(
                args.recipe,
                lock_id=args.lock,
                reason_kind=args.reason_kind,
                reason=args.reason,
            )
        if not args.tombstone_id:
            raise RecipesError("AR434", "revoke 缺 tombstone id。", "没有 --tombstone-id。", "先运行 recipe-lifecycle --action status。")
        return project.revoke_recipe_tombstone(args.tombstone_id, reason=args.reason)
    if args.command == "search":
        return project.search(args.query, limit=args.limit, kind=args.kind, source_path_contains=args.source_path_contains)
    if args.command == "refine":
        return project.refine(
            query=args.query,
            knowledge_need_id=args.knowledge_need,
            target_recipe_id=args.target_recipe,
            candidate_fields=args.candidate_fields.split(","),
            limit=args.limit,
            kind=args.kind,
            source_path_contains=args.source_path_contains,
        )
    if args.command == "extract-cards":
        return project.extract_cards(refinement_id=args.refinement)
    if args.command == "patch-draft":
        return project.patch_draft(target_recipe_id=args.target_recipe)
    if args.command == "knowledge-fusion":
        return project.knowledge_fusion(target_recipe_id=args.target_recipe)
    if args.command == "deep-read-plan":
        return project.deep_read_plan(fusion_id=args.fusion)
    if args.command == "target-suggestions":
        return project.target_suggestions(
            target_recipe_id=args.target_recipe,
            status=args.status,
            min_reviews=args.min_reviews,
        )
    if args.command == "recover":
        return project.recover(args.problem, idempotency_key=args.idempotency_key)
    if args.command == "ingest-video":
        return project.ingest_video_transcript(
            args.transcript,
            video_path=args.video,
            extract_keyframes=args.extract_keyframes,
        )
    if args.command == "doctor":
        return project.doctor()
    if args.command == "readiness":
        return project.readiness()
    if args.command == "outcome-status":
        return project.outcome_status(recipe_id=args.recipe)
    if args.command == "capabilities":
        return project.capabilities()
    if args.command == "convert-doc":
        return project.convert_doc(args.input, adapter=args.adapter)
    if args.command == "detect-scenes":
        return project.detect_scenes(args.video, adapter=args.adapter)
    if args.command == "transcribe":
        return project.transcribe(args.input, adapter=args.adapter, model=args.model)
    if args.command == "ocr-image":
        return project.ocr_image(args.input, adapter=args.adapter)
    if args.command == "memory-index":
        return project.memory_index(adapter=args.adapter)
    if args.command == "memory-search":
        return project.memory_search(args.query, adapter=args.adapter, limit=args.limit)
    if args.command == "memory-status":
        return project.memory_status(adapter=args.adapter)
    if args.command == "recall-boundary":
        return project.recall_boundary_status()
    if args.command == "evidence-quarantine":
        return project.evidence_quarantine(action=args.action, quarantine_id=args.quarantine_id)
    if args.command == "evidence-pack":
        return project.execution_evidence_pack(args.lock, max_bytes=args.max_bytes, privacy=args.privacy)
    if args.command == "memory-native-probe":
        return project.memory_native_probe(adapter=args.adapter, timeout=args.timeout)
    if args.command == "memory-semantic-probe":
        return project.memory_semantic_probe(adapter=args.adapter, timeout=args.timeout)
    if args.command == "memory-semantic-configure":
        return project.memory_semantic_configure(
            adapter=args.adapter,
            detect_only=args.detect_only,
            llm_provider=args.llm_provider,
            llm_model=args.llm_model,
            llm_endpoint=args.llm_endpoint,
            llm_api_key_env=args.llm_api_key_env,
            embedding_provider=args.embedding_provider,
            embedding_model=args.embedding_model,
            embedding_endpoint=args.embedding_endpoint,
            embedding_dimensions=args.embedding_dimensions,
        )
    if args.command == "cloud-configure":
        return project.cloud_configure(
            provider=args.provider,
            model=args.model,
            pro_model=args.pro_model,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
        )
    if args.command == "cloud-status":
        return project.cloud_status(provider=args.provider)
    if args.command == "cloud-refine":
        return project.cloud_refine(
            provider=args.provider,
            input_path=args.input,
            knowledge_need_id=args.knowledge_need,
            target_recipe_id=args.target_recipe,
            candidate_fields=args.candidate_fields.split(","),
            response_json=args.response_json,
            allow_network=args.allow_network,
            model=args.model,
            timeout=args.timeout,
        )
    if args.command == "embedding-configure":
        return project.embedding_configure(
            provider=args.provider,
            model=args.model,
            endpoint=args.endpoint,
            dimensions=args.dimensions,
        )
    if args.command == "embedding-status":
        return project.embedding_status(provider=args.provider)
    if args.command == "embedding-index":
        return project.embedding_index(
            provider=args.provider,
            response_json=args.response_json,
            allow_loopback=args.allow_loopback,
            timeout=args.timeout,
        )
    if args.command == "embedding-search":
        return project.embedding_search(
            args.query,
            provider=args.provider,
            response_json=args.response_json,
            allow_loopback=args.allow_loopback,
            limit=args.limit,
            timeout=args.timeout,
        )
    if args.command == "quality-benchmark":
        return project.quality_benchmark(
            qwen_response_json=args.qwen_response_json,
            allow_loopback=args.allow_loopback,
            limit=args.limit,
            timeout=args.timeout,
        )
    if args.command == "recall-quality-benchmark":
        return project.recall_quality_benchmark(
            cases_path=args.cases,
            backends=args.backends.split(","),
            allow_loopback=args.allow_loopback,
            limit=args.limit,
            min_score=args.min_score,
            qwen_min_score=args.qwen_min_score,
            timeout=args.timeout,
        )
    if args.command == "lookup-pressure":
        return project.lookup_pressure(cases_path=args.cases)
    if args.command == "lock-pressure":
        return project.lock_pressure(cases_path=args.cases)
    if args.command == "consumption-coverage":
        return project.consumption_coverage()
    if args.command == "real-pressure-summary":
        return project.real_pressure_summary(projects_root=args.projects_root, name_contains=args.name_contains)
    if args.command == "learning-quality-summary":
        return run_learning_quality_summary(project, projects_root=args.projects_root, cohort_path=args.cohort)
    if args.command == "duplicate-governance":
        return project.duplicate_governance()
    if args.command == "candidate-quality-benchmark":
        return project.candidate_quality_benchmark(cases_path=args.cases)
    if args.command == "completeness-audit":
        return project.completeness_audit(
            input_path=args.input,
            subject_type=args.subject_type,
            requirements_path=args.requirements,
            software_map_path=args.software_map,
            execution_evidence_path=args.execution_evidence,
        )
    if args.command == "course-skill-draft":
        return project.course_skill_draft(
            transcript_path=args.transcript,
            spec_path=args.spec,
            software_map_path=args.software_map,
        )
    if args.command == "review-triage":
        return project.review_triage(
            target_recipe_id=args.target_recipe,
            target_prefix=args.target_prefix,
            status=args.status,
            min_values=args.min_values,
            max_values=args.max_values,
            latest_per_target=not args.include_older,
        )
    if args.command == "review-packet":
        return project.review_packet(
            target_recipe_id=args.target_recipe,
            target_prefix=args.target_prefix,
            status=args.status,
            min_values=args.min_values,
            max_values=args.max_values,
            latest_per_target=not args.include_older,
            max_items=args.max_items,
        )
    if args.command == "self-run-benchmark":
        return project.self_run_benchmark(
            query=args.query,
            knowledge_need_id=args.knowledge_need,
            target_recipe_id=args.target_recipe,
            candidate_fields=args.candidate_fields.split(","),
            min_cards=args.min_cards,
            limit=args.limit,
            kind=args.kind,
            scan_depth=args.scan_depth,
            source_path_contains=args.source_path_contains,
        )
    if args.command == "repeat-error-benchmark":
        return project.repeat_error_benchmark(cases_path=args.cases, min_cases=args.min_cases, min_improvements=args.min_improvements)
    if args.command == "output-quality-benchmark":
        return project.output_quality_benchmark(cases_path=args.cases, min_cases=args.min_cases, min_passed=args.min_passed)
    raise RecipesError("AR999", "未知命令。", args.command)


def emit(result: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
        return
    if result.get("ok"):
        print(f"OK: {result.get('action', 'agent-recipes')}")
        if "idempotency_status" in result:
            print(f"idempotency_status: {result['idempotency_status']}")
        if "claim_status" in result:
            cannot = result["claim_status"].get("cannot_claim", [])
            if cannot:
                print("cannot_claim:")
                for item in cannot:
                    print(f"- {item}")
    else:
        print(f"ERROR {result.get('code')}: {result.get('problem')}", file=sys.stderr)
        print(result.get("cause", ""), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "mcp" and args.stdio:
        return serve_stdio(args.project)
    try:
        result = run(args)
        emit(result, as_json=args.json)
        return 0 if result.get("ok", False) else 1
    except RecipesError as exc:
        result = exc.to_dict()
        emit(result, as_json=getattr(args, "json", False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
