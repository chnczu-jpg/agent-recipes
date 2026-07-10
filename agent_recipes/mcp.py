from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_recipes.core import RecipesError, RecipesProject
from agent_recipes.learning_quality import run_learning_quality_summary


TOOL_NAMES = [
    "doctor",
    "migration_status",
    "migrate",
    "readiness",
    "outcome_status",
    "lookup",
    "lock",
    "recipe_lifecycle",
    "capture",
    "capabilities",
    "search",
    "refine",
    "extract_cards",
    "patch_draft",
    "knowledge_fusion",
    "deep_read_plan",
    "target_suggestions",
    "review_decide",
    "convert_doc",
    "detect_scenes",
    "transcribe",
    "ocr_image",
    "memory_index",
    "memory_search",
    "memory_status",
    "recall_boundary",
    "evidence_quarantine",
    "evidence_pack",
    "memory_native_probe",
    "memory_semantic_probe",
    "memory_semantic_configure",
    "cloud_configure",
    "cloud_status",
    "cloud_refine",
    "embedding_configure",
    "embedding_status",
    "embedding_index",
    "embedding_search",
    "quality_benchmark",
    "recall_quality_benchmark",
    "lookup_pressure",
    "lock_pressure",
    "consumption_coverage",
    "real_pressure_summary",
    "learning_quality_summary",
    "duplicate_governance",
    "candidate_quality_benchmark",
    "completeness_audit",
    "course_skill_draft",
    "review_triage",
    "review_packet",
    "self_run_benchmark",
    "repeat_error_benchmark",
    "output_quality_benchmark",
]

MCP_PROTOCOL_VERSION = "2024-11-05"


def initialize_result(request: dict[str, Any]) -> dict[str, Any]:
    params = request.get("params") or {}
    protocol_version = params.get("protocolVersion") or MCP_PROTOCOL_VERSION
    return {
        "protocolVersion": protocol_version,
        "capabilities": {
            "tools": {
                "listChanged": False,
            }
        },
        "serverInfo": {
            "name": "agent-recipes",
            "version": "0.2.0",
        },
        "instructions": "Use Agent Recipes tools as candidate-only helpers unless doctor claim_status says otherwise.",
    }


def tool_call_result(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, sort_keys=True),
            }
        ],
        "structuredContent": payload,
        "isError": bool(payload.get("code")),
    }


def unwrap_tool_call_result(result: dict[str, Any]) -> dict[str, Any]:
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    return result


MCP_DEFAULT_MIN_SCORE = 2
MCP_PERCENT_LIKE_MIN_SCORE_LIMIT = 20


def mcp_min_score(args: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
    raw_min_score = args.get("min_score", MCP_DEFAULT_MIN_SCORE)
    min_score = int(raw_min_score)
    if MCP_PERCENT_LIKE_MIN_SCORE_LIMIT < min_score <= 100:
        return MCP_DEFAULT_MIN_SCORE, {
            "original": min_score,
            "used": MCP_DEFAULT_MIN_SCORE,
            "reason": "MCP min_score is an internal matched-term count, not a 0-100 percentage. Omit min_score for normal agent use.",
        }
    return min_score, None


def attach_mcp_min_score_normalization(result: dict[str, Any], normalization: dict[str, Any] | None) -> None:
    if normalization is None:
        return
    result["mcp_min_score_normalized"] = normalization
    warnings = result.setdefault("mcp_warnings", [])
    if isinstance(warnings, list):
        warnings.append(normalization["reason"])


def tool_list() -> list[dict[str, Any]]:
    return [
        {
            "name": "agent_recipes_doctor",
            "description": "Check .recipes health and claim_status.",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        },
        {
            "name": "agent_recipes_migration_status",
            "description": "Report the project schema version and whether an explicit migration is required.",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        },
        {
            "name": "agent_recipes_migrate",
            "description": "Explicitly migrate a legacy project without rewriting historical events.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "target": {"type": "string"},
                },
            },
        },
        {
            "name": "agent_recipes_readiness",
            "description": "Report multi-axis governance readiness and a stable recommended next action.",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        },
        {
            "name": "agent_recipes_outcome_status",
            "description": "Report exact recipe-version outcome confidence, maturity, and automatic execution recommendation.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "recipe_id": {"type": "string"},
                },
            },
        },
        {
            "name": "agent_recipes_lookup",
            "description": "Find a recipe for a task.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "strict": {"type": "boolean"},
                    "min_score": {
                        "type": "integer",
                        "description": "Internal matched-term count. Omit for normal use; do not pass 0-100 percentage values like 80.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "agent_recipes_lock",
            "description": "Create an execution lock for a recipe.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "recipe_id": {"type": "string"},
                    "task": {"type": "string"},
                    "query": {"type": "string"},
                    "min_score": {
                        "type": "integer",
                        "description": "Internal matched-term count. Omit for normal use; do not pass 0-100 percentage values like 80.",
                    },
                },
                "required": ["recipe_id"],
            },
        },
        {
            "name": "agent_recipes_capture",
            "description": "Capture correction/success/failure/unknown with exact lock bindings and optional cause-specific feedback.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "capture_type": {"type": "string", "enum": ["correction", "success", "failure", "unknown"]},
                    "text": {"type": "string"},
                    "task": {"type": "string"},
                    "lock_id": {"type": "string"},
                    "idempotency_key": {"type": "string"},
                    "feedback_kind": {"type": "string", "enum": ["verified_success", "partial_success", "generic_failure", "retrieval_mismatch", "execution_error", "recipe_incorrect", "recipe_outdated", "applicability_overreach", "missing_step", "excessive_cost", "recipe_conflict", "user_correction", "external_dependency", "insufficient_evidence", "evaluation_blocked"]},
                },
                "required": ["capture_type", "text"],
            },
        },
        {
            "name": "agent_recipes_recipe_lifecycle",
            "description": "Inspect, tombstone, or revoke a formal recipe lifecycle entry without deleting history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "action": {"type": "string", "enum": ["status", "tombstone", "revoke"]},
                    "recipe_id": {"type": "string"},
                    "tombstone_id": {"type": "string"},
                    "lock_id": {"type": "string"},
                    "reason_kind": {
                        "type": "string",
                        "enum": ["correction", "supersession", "retraction", "contradiction_resolution"],
                    },
                    "reason": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "agent_recipes_capabilities",
            "description": "Report local dependencies, binaries, and adapter runtime receipts.",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        },
        {
            "name": "agent_recipes_search",
            "description": "Search local evidence candidates from source/video indexes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                    "kind": {"type": "string"},
                    "source_path_contains": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query"],
            },
        },
        {
            "name": "agent_recipes_refine",
            "description": "Map evidence candidates to source_refinery candidate fields.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "knowledge_need_id": {"type": "string"},
                    "target_recipe_id": {"type": "string"},
                    "candidate_fields": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                    "kind": {"type": "string"},
                    "source_path_contains": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query", "knowledge_need_id", "target_recipe_id", "candidate_fields"],
            },
        },
        {
            "name": "agent_recipes_extract_cards",
            "description": "Extract five source_refinery card types from refined chunks.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "refinement_id": {"type": "string"}},
            },
        },
        {
            "name": "agent_recipes_patch_draft",
            "description": "Create a RecipePatchDraft and review item from source_refinery cards.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "target_recipe_id": {"type": "string"}},
                "required": ["target_recipe_id"],
            },
        },
        {
            "name": "agent_recipes_knowledge_fusion",
            "description": "Create candidate-only knowledge fusion merge/split/conflict/deep-read review items from source_refinery cards.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "target_recipe_id": {"type": "string"}},
                "required": ["target_recipe_id"],
            },
        },
        {
            "name": "agent_recipes_deep_read_plan",
            "description": "Turn needs_deep_read fusion candidates into candidate-only scoped self-run tasks.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "fusion_id": {"type": "string"}},
                "required": ["fusion_id"],
            },
        },
        {
            "name": "agent_recipes_target_suggestions",
            "description": "Create candidate-only narrower target suggestions from pending/rejected review history without writing formal recipes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "target_recipe_id": {"type": "string"},
                    "status": {"type": "string"},
                    "min_reviews": {"type": "integer"},
                },
            },
        },
        {
            "name": "agent_recipes_review_decide",
            "description": "Decide a review item. Supports fusion merge/split/supersede and ordinary reject/accept routing through core gates.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "review_id": {"type": "string"},
                    "decision": {"type": "string"},
                    "lock_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["review_id", "decision"],
            },
        },
        {
            "name": "agent_recipes_convert_doc",
            "description": "Convert a local document into normalized Markdown candidate evidence.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "input": {"type": "string"}, "adapter": {"type": "string"}},
                "required": ["input"],
            },
        },
        {
            "name": "agent_recipes_detect_scenes",
            "description": "Run local scene detection on a video candidate.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "video": {"type": "string"}, "adapter": {"type": "string"}},
                "required": ["video"],
            },
        },
        {
            "name": "agent_recipes_transcribe",
            "description": "Run local ASR and write a normalized transcript candidate.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "input": {"type": "string"},
                    "adapter": {"type": "string"},
                    "model": {"type": "string"},
                },
                "required": ["input"],
            },
        },
        {
            "name": "agent_recipes_ocr_image",
            "description": "Run local OCR and write normalized OCR candidate evidence.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "input": {"type": "string"}, "adapter": {"type": "string"}},
                "required": ["input"],
            },
        },
        {
            "name": "agent_recipes_memory_index",
            "description": "Index source_refinery candidates into Cognee memory candidates or Graphiti local graph candidates.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "adapter": {"type": "string"}},
            },
        },
        {
            "name": "agent_recipes_memory_search",
            "description": "Search memory candidate evidence from Cognee or Graphiti local graph candidates.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "adapter": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "agent_recipes_memory_status",
            "description": "Report memory adapter candidate-only status for Cognee and Graphiti.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "adapter": {"type": "string"}},
            },
        },
        {
            "name": "agent_recipes_recall_boundary",
            "description": "Verify Cognee, Graphiti, and Qwen recall remain optional candidate-only adapters that cannot mutate core truth.",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        },
        {
            "name": "agent_recipes_evidence_quarantine",
            "description": "Inspect, quarantine, or explicitly release malformed/secret-bearing candidate evidence without deleting history.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "action": {"type": "string", "enum": ["status", "apply", "release"]},
                    "quarantine_id": {"type": "string"},
                },
                "required": ["action"],
            },
        },
        {
            "name": "agent_recipes_evidence_pack",
            "description": "Build a lock-bound execution evidence pack with byte budget, privacy policy, redaction, and explicit omission reasons.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "lock_id": {"type": "string"},
                    "max_bytes": {"type": "integer"},
                    "privacy": {"type": "string", "enum": ["minimal", "project_local"]},
                },
                "required": ["lock_id"],
            },
        },
        {
            "name": "agent_recipes_memory_native_probe",
            "description": "Run a caged native safety probe for Cognee or Graphiti.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "adapter": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
            },
        },
        {
            "name": "agent_recipes_memory_semantic_probe",
            "description": "Run a caged Cognee semantic probe gate with loopback plus explicit DeepSeek allowlist support.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "adapter": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
            },
        },
        {
            "name": "agent_recipes_memory_semantic_configure",
            "description": "Write or detect project-local Cognee semantic runtime config.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "adapter": {"type": "string"},
                    "detect_only": {"type": "boolean"},
                    "llm_provider": {"type": "string"},
                    "llm_model": {"type": "string"},
                    "llm_endpoint": {"type": "string"},
                    "llm_api_key_env": {"type": "string"},
                    "embedding_provider": {"type": "string"},
                    "embedding_model": {"type": "string"},
                    "embedding_endpoint": {"type": "string"},
                    "embedding_dimensions": {"type": "integer"},
                },
            },
        },
        {
            "name": "agent_recipes_cloud_configure",
            "description": "Configure a candidate-only cloud text adapter without storing secrets.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "provider": {"type": "string"},
                    "model": {"type": "string"},
                    "pro_model": {"type": "string"},
                    "base_url": {"type": "string"},
                    "api_key_env": {"type": "string"},
                },
            },
        },
        {
            "name": "agent_recipes_cloud_status",
            "description": "Report cloud adapter config, secret presence, and candidate-only runtime receipts.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "provider": {"type": "string"}},
            },
        },
        {
            "name": "agent_recipes_cloud_refine",
            "description": "Use a cloud text adapter or replay response to create candidate source_refinery cards.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "provider": {"type": "string"},
                    "input": {"type": "string"},
                    "knowledge_need_id": {"type": "string"},
                    "target_recipe_id": {"type": "string"},
                    "candidate_fields": {"type": "array", "items": {"type": "string"}},
                    "response_json": {"type": "string"},
                    "allow_network": {"type": "boolean"},
                    "model": {"type": "string"},
                    "timeout": {"type": "integer"},
                },
                "required": ["input", "knowledge_need_id", "target_recipe_id", "candidate_fields"],
            },
        },
        {
            "name": "agent_recipes_embedding_configure",
            "description": "Configure a project-local Qwen3 embedding provider on loopback only.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "provider": {"type": "string"},
                    "model": {"type": "string"},
                    "endpoint": {"type": "string"},
                    "dimensions": {"type": "integer"},
                },
            },
        },
        {
            "name": "agent_recipes_embedding_status",
            "description": "Report local embedding adapter status and candidate-only receipts.",
            "inputSchema": {
                "type": "object",
                "properties": {"project": {"type": "string"}, "provider": {"type": "string"}},
            },
        },
        {
            "name": "agent_recipes_embedding_index",
            "description": "Index candidate memory records with Qwen3 embeddings or replay vectors.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "provider": {"type": "string"},
                    "response_json": {"type": "string"},
                    "allow_loopback": {"type": "boolean"},
                    "timeout": {"type": "integer"},
                },
            },
        },
        {
            "name": "agent_recipes_embedding_search",
            "description": "Search candidate evidence by embedding similarity.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "provider": {"type": "string"},
                    "response_json": {"type": "string"},
                    "allow_loopback": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "timeout": {"type": "integer"},
                },
                "required": ["query"],
            },
        },
        {
            "name": "agent_recipes_quality_benchmark",
            "description": "Run local candidate-only quality benchmark for search, memory, embedding, and review gate.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "qwen_response_json": {"type": "string"},
                    "allow_loopback": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "timeout": {"type": "integer"},
                },
            },
        },
        {
            "name": "agent_recipes_recall_quality_benchmark",
            "description": "Compare core, Cognee, Graphiti, and Qwen recall on one fixed recipe corpus with no-match and latency evidence.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "cases": {"type": "string"},
                    "backends": {"type": "array", "items": {"type": "string"}},
                    "allow_loopback": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "min_score": {"type": "integer"},
                    "qwen_min_score": {"type": "number"},
                    "timeout": {"type": "integer"},
                },
                "required": ["cases"],
            },
        },
        {
            "name": "agent_recipes_lookup_pressure",
            "description": "Run candidate-only lookup applicability pressure cases and flag overreach.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "cases": {"type": "string"},
                },
                "required": ["cases"],
            },
        },
        {
            "name": "agent_recipes_lock_pressure",
            "description": "Run lookup->lock pressure cases, creating locks only for positive cases and preventing locks for negatives.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "cases": {"type": "string"},
                },
                "required": ["cases"],
            },
        },
        {
            "name": "agent_recipes_consumption_coverage",
            "description": "Report which formal recipes have passed lookup and lock pressure coverage without executing recipes.",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        },
        {
            "name": "agent_recipes_real_pressure_summary",
            "description": "Summarize local real-material pressure test projects and list report/evidence gaps without reading source content.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "projects_root": {"type": "string"},
                    "name_contains": {"type": "string"},
                },
            },
        },
        {
            "name": "agent_recipes_learning_quality_summary",
            "description": "Judge a fixed large-sample learning cohort from existing self-run, candidate-card, quality, and review evidence without reading sources or changing recipes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "projects_root": {"type": "string"},
                    "cohort": {"type": "string"},
                },
                "required": ["cohort"],
            },
        },
        {
            "name": "agent_recipes_duplicate_governance",
            "description": "Create a candidate-only report for duplicate or near-duplicate recipes that shadow expected lookup/lock results.",
            "inputSchema": {"type": "object", "properties": {"project": {"type": "string"}}},
        },
        {
            "name": "agent_recipes_candidate_quality_benchmark",
            "description": "Score pending review/candidate patch quality without accepting or writing formal recipes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "cases": {"type": "string"},
                },
                "required": ["cases"],
            },
        },
        {
            "name": "agent_recipes_completeness_audit",
            "description": "Score structural and domain-specific completeness for a skill recipe or course extraction without claiming correctness or mastery.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "input": {"type": "string"},
                    "subject_type": {"type": "string", "enum": ["skill", "course"]},
                    "requirements": {"type": "string"},
                    "software_map": {"type": "string"},
                    "execution_evidence": {"type": "string"},
                },
                "required": ["input", "subject_type"],
            },
        },
        {
            "name": "agent_recipes_course_skill_draft",
            "description": "Turn timestamped course segments into a source-traced candidate skill draft using a software function map; never writes a formal recipe.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "transcript": {"type": "string"},
                    "spec": {"type": "string"},
                    "software_map": {"type": "string"},
                },
                "required": ["transcript", "spec", "software_map"],
            },
        },
        {
            "name": "agent_recipes_review_triage",
            "description": "Classify pending/rejected review items into candidate-only triage buckets without accepting or writing formal recipes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "target_recipe_id": {"type": "string"},
                    "target_prefix": {"type": "string"},
                    "status": {"type": "string"},
                    "min_values": {"type": "integer"},
                    "max_values": {"type": "integer"},
                    "latest_per_target": {"type": "boolean"},
                },
            },
        },
        {
            "name": "agent_recipes_review_packet",
            "description": "Create a human-readable review packet from review_queue candidates without accepting or writing formal recipes.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "target_recipe_id": {"type": "string"},
                    "target_prefix": {"type": "string"},
                    "status": {"type": "string"},
                    "min_values": {"type": "integer"},
                    "max_values": {"type": "integer"},
                    "max_items": {"type": "integer"},
                    "latest_per_target": {"type": "boolean"},
                },
            },
        },
        {
            "name": "agent_recipes_self_run_benchmark",
            "description": "Run scan/search/refine/extract-cards/patch-draft and verify the result stops at review_queue.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "query": {"type": "string"},
                    "knowledge_need_id": {"type": "string"},
                    "target_recipe_id": {"type": "string"},
                    "candidate_fields": {"type": "array", "items": {"type": "string"}},
                    "min_cards": {"type": "integer"},
                    "limit": {"type": "integer"},
                    "kind": {"type": "string"},
                    "scan_depth": {"type": "string"},
                    "source_path_contains": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["query", "knowledge_need_id", "target_recipe_id", "candidate_fields"],
            },
        },
        {
            "name": "agent_recipes_repeat_error_benchmark",
            "description": "Score provided without-recipe vs with-recipe outputs for repeat-error reduction without running an agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "cases": {"type": "string"},
                    "min_cases": {"type": "integer"},
                    "min_improvements": {"type": "integer"},
                },
                "required": ["cases"],
            },
        },
        {
            "name": "agent_recipes_output_quality_benchmark",
            "description": "Score provided agent outputs for evidence, boundary, lock, and actionable next-step quality without running an agent.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "cases": {"type": "string"},
                    "min_cases": {"type": "integer"},
                    "min_passed": {"type": "integer"},
                },
                "required": ["cases"],
            },
        },
    ]


def normalize_tool_name(name: str) -> str:
    return name.removeprefix("agent_recipes_").replace("-", "_")


def call_tool(name: str, arguments: dict[str, Any] | None = None, *, project: str | Path | None = None) -> dict[str, Any]:
    args = arguments or {}
    project_root = Path(args.get("project") or project or ".")
    recipes = RecipesProject(project_root)
    tool = normalize_tool_name(name)
    try:
        if tool == "doctor":
            result = recipes.doctor()
        elif tool == "migration_status":
            result = recipes.migration_status()
        elif tool == "migrate":
            result = recipes.migrate(target=str(args.get("target", "1.0")))
        elif tool == "readiness":
            result = recipes.readiness()
        elif tool == "outcome_status":
            result = recipes.outcome_status(recipe_id=args.get("recipe_id"))
        elif tool == "lookup":
            min_score, min_score_normalization = mcp_min_score(args)
            result = recipes.lookup(
                str(args["query"]),
                strict=bool(args.get("strict", False)),
                min_score=min_score,
            )
            attach_mcp_min_score_normalization(result, min_score_normalization)
        elif tool == "lock":
            min_score, min_score_normalization = mcp_min_score(args)
            result = recipes.create_lock(
                str(args["recipe_id"]),
                task=str(args.get("task", "")),
                query=str(args["query"]) if args.get("query") is not None else None,
                min_score=min_score,
            )
            attach_mcp_min_score_normalization(result, min_score_normalization)
        elif tool == "recipe_lifecycle":
            action = str(args.get("action", "status")).strip().casefold()
            if action == "status":
                result = recipes.recipe_lifecycle_status(recipe_id=args.get("recipe_id"))
            elif action == "tombstone":
                if not args.get("recipe_id"):
                    raise RecipesError("AR433", "tombstone 缺 recipe id。", "没有 recipe_id。", "传入 recipe_id。")
                result = recipes.tombstone_recipe(
                    str(args["recipe_id"]),
                    lock_id=args.get("lock_id"),
                    reason_kind=str(args.get("reason_kind", "correction")),
                    reason=str(args.get("reason", "")),
                )
            elif action == "revoke":
                if not args.get("tombstone_id"):
                    raise RecipesError("AR434", "revoke 缺 tombstone id。", "没有 tombstone_id。", "先调用 status。")
                result = recipes.revoke_recipe_tombstone(str(args["tombstone_id"]), reason=str(args.get("reason", "")))
            else:
                raise RecipesError("AR433", "recipe lifecycle action 不支持。", f"action={action}", "使用 status/tombstone/revoke。")
        elif tool == "capture":
            result = recipes.capture(
                str(args["capture_type"]),
                str(args["text"]),
                task=str(args.get("task", "")),
                lock_id=args.get("lock_id"),
                idempotency_key=args.get("idempotency_key"),
                feedback_kind=args.get("feedback_kind"),
            )
        elif tool == "capabilities":
            result = recipes.capabilities()
        elif tool == "search":
            source_path_contains = args.get("source_path_contains") or []
            if isinstance(source_path_contains, str):
                source_path_contains = [source_path_contains]
            result = recipes.search(
                str(args["query"]),
                limit=int(args.get("limit", 5)),
                kind=str(args.get("kind", "all")),
                source_path_contains=[str(item) for item in source_path_contains],
            )
        elif tool == "refine":
            fields = args.get("candidate_fields", [])
            if isinstance(fields, str):
                fields = [field.strip() for field in fields.split(",") if field.strip()]
            source_path_contains = args.get("source_path_contains") or []
            if isinstance(source_path_contains, str):
                source_path_contains = [source_path_contains]
            result = recipes.refine(
                query=str(args["query"]),
                knowledge_need_id=str(args["knowledge_need_id"]),
                target_recipe_id=str(args["target_recipe_id"]),
                candidate_fields=list(fields),
                limit=int(args.get("limit", 20)),
                kind=str(args.get("kind", "all")),
                source_path_contains=[str(item) for item in source_path_contains],
            )
        elif tool == "extract_cards":
            result = recipes.extract_cards(refinement_id=args.get("refinement_id"))
        elif tool == "patch_draft":
            result = recipes.patch_draft(target_recipe_id=str(args["target_recipe_id"]))
        elif tool == "knowledge_fusion":
            result = recipes.knowledge_fusion(target_recipe_id=str(args["target_recipe_id"]))
        elif tool == "deep_read_plan":
            result = recipes.deep_read_plan(fusion_id=str(args["fusion_id"]))
        elif tool == "target_suggestions":
            result = recipes.target_suggestions(
                target_recipe_id=args.get("target_recipe_id"),
                status=str(args.get("status", "rejected")),
                min_reviews=int(args.get("min_reviews", 1)),
            )
        elif tool == "review_decide":
            decision = str(args["decision"]).strip().casefold()
            if decision == "accept":
                result = recipes.accept_review(str(args["review_id"]), lock_id=args.get("lock_id"))
            elif decision == "reject":
                result = recipes.reject_review(str(args["review_id"]), reason=str(args.get("reason", "")))
            else:
                result = recipes.decide_fusion_review(str(args["review_id"]), decision=decision, lock_id=args.get("lock_id"))
        elif tool == "convert_doc":
            result = recipes.convert_doc(str(args["input"]), adapter=str(args.get("adapter", "markitdown")))
        elif tool == "detect_scenes":
            result = recipes.detect_scenes(str(args["video"]), adapter=str(args.get("adapter", "pyscenedetect")))
        elif tool == "transcribe":
            result = recipes.transcribe(
                str(args["input"]),
                adapter=str(args.get("adapter", "faster-whisper")),
                model=str(args.get("model", "tiny.en")),
            )
        elif tool == "ocr_image":
            result = recipes.ocr_image(str(args["input"]), adapter=str(args.get("adapter", "paddleocr")))
        elif tool == "memory_index":
            result = recipes.memory_index(adapter=str(args.get("adapter", "cognee")))
        elif tool == "memory_search":
            result = recipes.memory_search(
                str(args["query"]),
                adapter=str(args.get("adapter", "cognee")),
                limit=int(args.get("limit", 5)),
            )
        elif tool == "memory_status":
            result = recipes.memory_status(adapter=str(args.get("adapter", "all")))
        elif tool == "recall_boundary":
            result = recipes.recall_boundary_status()
        elif tool == "evidence_quarantine":
            result = recipes.evidence_quarantine(
                action=str(args.get("action", "status")),
                quarantine_id=args.get("quarantine_id"),
            )
        elif tool == "evidence_pack":
            result = recipes.execution_evidence_pack(
                str(args["lock_id"]),
                max_bytes=int(args.get("max_bytes", 65536)),
                privacy=str(args.get("privacy", "project_local")),
            )
        elif tool == "memory_native_probe":
            result = recipes.memory_native_probe(
                adapter=str(args.get("adapter", "cognee")),
                timeout=int(args.get("timeout", 20)),
            )
        elif tool == "memory_semantic_probe":
            result = recipes.memory_semantic_probe(
                adapter=str(args.get("adapter", "cognee")),
                timeout=int(args.get("timeout", 30)),
            )
        elif tool == "memory_semantic_configure":
            result = recipes.memory_semantic_configure(
                adapter=str(args.get("adapter", "cognee")),
                detect_only=bool(args.get("detect_only", False)),
                llm_provider=args.get("llm_provider"),
                llm_model=args.get("llm_model"),
                llm_endpoint=args.get("llm_endpoint"),
                llm_api_key_env=args.get("llm_api_key_env"),
                embedding_provider=args.get("embedding_provider"),
                embedding_model=args.get("embedding_model"),
                embedding_endpoint=args.get("embedding_endpoint"),
                embedding_dimensions=args.get("embedding_dimensions"),
            )
        elif tool == "cloud_configure":
            result = recipes.cloud_configure(
                provider=str(args.get("provider", "deepseek")),
                model=str(args.get("model", "deepseek-v4-flash")),
                pro_model=str(args.get("pro_model", "deepseek-v4-pro")),
                base_url=str(args.get("base_url", "https://api.deepseek.com")),
                api_key_env=str(args.get("api_key_env", "AGENT_RECIPES_DEEPSEEK_API_KEY")),
            )
        elif tool == "cloud_status":
            result = recipes.cloud_status(provider=str(args.get("provider", "all")))
        elif tool == "cloud_refine":
            fields = args.get("candidate_fields", [])
            if isinstance(fields, str):
                fields = [field.strip() for field in fields.split(",") if field.strip()]
            result = recipes.cloud_refine(
                provider=str(args.get("provider", "deepseek")),
                input_path=str(args["input"]),
                knowledge_need_id=str(args["knowledge_need_id"]),
                target_recipe_id=str(args["target_recipe_id"]),
                candidate_fields=list(fields),
                response_json=args.get("response_json"),
                allow_network=bool(args.get("allow_network", False)),
                model=args.get("model"),
                timeout=int(args.get("timeout", 60)),
            )
        elif tool == "embedding_configure":
            result = recipes.embedding_configure(
                provider=str(args.get("provider", "qwen3")),
                model=str(args.get("model", "qwen3-embedding:0.6b")),
                endpoint=str(args.get("endpoint", "http://127.0.0.1:11434/api/embed")),
                dimensions=int(args.get("dimensions", 1024)),
            )
        elif tool == "embedding_status":
            result = recipes.embedding_status(provider=str(args.get("provider", "all")))
        elif tool == "embedding_index":
            result = recipes.embedding_index(
                provider=str(args.get("provider", "qwen3")),
                response_json=args.get("response_json"),
                allow_loopback=bool(args.get("allow_loopback", False)),
                timeout=int(args.get("timeout", 60)),
            )
        elif tool == "embedding_search":
            result = recipes.embedding_search(
                str(args["query"]),
                provider=str(args.get("provider", "qwen3")),
                response_json=args.get("response_json"),
                allow_loopback=bool(args.get("allow_loopback", False)),
                limit=int(args.get("limit", 5)),
                timeout=int(args.get("timeout", 60)),
            )
        elif tool == "quality_benchmark":
            result = recipes.quality_benchmark(
                qwen_response_json=args.get("qwen_response_json"),
                allow_loopback=bool(args.get("allow_loopback", False)),
                limit=int(args.get("limit", 5)),
                timeout=int(args.get("timeout", 60)),
            )
        elif tool == "recall_quality_benchmark":
            result = recipes.recall_quality_benchmark(
                cases_path=str(args["cases"]),
                backends=[str(item) for item in args.get("backends", ["core", "cognee", "graphiti", "qwen"])],
                allow_loopback=bool(args.get("allow_loopback", False)),
                limit=int(args.get("limit", 5)),
                min_score=int(args.get("min_score", 2)),
                qwen_min_score=float(args.get("qwen_min_score", 0.55)),
                timeout=int(args.get("timeout", 60)),
            )
        elif tool == "lookup_pressure":
            result = recipes.lookup_pressure(cases_path=str(args["cases"]))
        elif tool == "lock_pressure":
            result = recipes.lock_pressure(cases_path=str(args["cases"]))
        elif tool == "consumption_coverage":
            result = recipes.consumption_coverage()
        elif tool == "real_pressure_summary":
            result = recipes.real_pressure_summary(
                projects_root=str(args.get("projects_root", ".recipes_real_tests")),
                name_contains=args.get("name_contains"),
            )
        elif tool == "learning_quality_summary":
            result = run_learning_quality_summary(
                recipes,
                projects_root=str(args.get("projects_root", ".recipes_real_tests")),
                cohort_path=str(args["cohort"]),
            )
        elif tool == "duplicate_governance":
            result = recipes.duplicate_governance()
        elif tool == "candidate_quality_benchmark":
            result = recipes.candidate_quality_benchmark(cases_path=str(args["cases"]))
        elif tool == "completeness_audit":
            result = recipes.completeness_audit(
                input_path=str(args["input"]),
                subject_type=str(args["subject_type"]),
                requirements_path=args.get("requirements"),
                software_map_path=args.get("software_map"),
                execution_evidence_path=args.get("execution_evidence"),
            )
        elif tool == "course_skill_draft":
            result = recipes.course_skill_draft(
                transcript_path=str(args["transcript"]),
                spec_path=str(args["spec"]),
                software_map_path=str(args["software_map"]),
            )
        elif tool == "review_triage":
            result = recipes.review_triage(
                target_recipe_id=args.get("target_recipe_id"),
                target_prefix=args.get("target_prefix"),
                status=str(args.get("status", "pending")),
                min_values=int(args.get("min_values", 2)),
                max_values=int(args.get("max_values", 40)),
                latest_per_target=bool(args.get("latest_per_target", True)),
            )
        elif tool == "review_packet":
            result = recipes.review_packet(
                target_recipe_id=args.get("target_recipe_id"),
                target_prefix=args.get("target_prefix"),
                status=str(args.get("status", "pending")),
                min_values=int(args.get("min_values", 2)),
                max_values=int(args.get("max_values", 40)),
                max_items=int(args.get("max_items", 20)),
                latest_per_target=bool(args.get("latest_per_target", True)),
            )
        elif tool == "self_run_benchmark":
            source_path_contains = args.get("source_path_contains") or []
            if isinstance(source_path_contains, str):
                source_path_contains = [source_path_contains]
            result = recipes.self_run_benchmark(
                query=str(args["query"]),
                knowledge_need_id=str(args["knowledge_need_id"]),
                target_recipe_id=str(args["target_recipe_id"]),
                candidate_fields=[str(field) for field in args.get("candidate_fields", [])],
                min_cards=int(args.get("min_cards", 1)),
                limit=int(args.get("limit", 20)),
                kind=str(args.get("kind", "all")),
                scan_depth=str(args.get("scan_depth", "shallow")),
                source_path_contains=[str(item) for item in source_path_contains],
            )
        elif tool == "repeat_error_benchmark":
            result = recipes.repeat_error_benchmark(
                cases_path=str(args["cases"]),
                min_cases=int(args.get("min_cases", 5)),
                min_improvements=int(args.get("min_improvements", 3)),
            )
        elif tool == "output_quality_benchmark":
            result = recipes.output_quality_benchmark(
                cases_path=str(args["cases"]),
                min_cases=int(args.get("min_cases", 1)),
                min_passed=int(args.get("min_passed", 1)),
            )
        else:
            raise RecipesError("AR600", "未知 MCP tool。", name, "使用 tools/list 查看可用工具。")
    except RecipesError as exc:
        result = exc.to_dict()
    result.setdefault("transport", "mcp")
    result["tool"] = f"agent_recipes_{tool}"
    return result


def handle_request(request: dict[str, Any], *, default_project: Path) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    try:
        if method == "initialize":
            payload = initialize_result(request)
        elif isinstance(method, str) and method.startswith("notifications/"):
            return None
        elif method == "ping":
            payload = {}
        elif method == "tools/list":
            payload = {"tools": tool_list()}
        elif method == "tools/call":
            params = request.get("params") or {}
            payload = tool_call_result(call_tool(params["name"], params.get("arguments") or {}, project=default_project))
        elif method == "resources/list":
            payload = {"resources": []}
        elif method == "prompts/list":
            payload = {"prompts": []}
        else:
            raise RecipesError("AR601", "未知 MCP method。", str(method), "使用 tools/list 或 tools/call。")
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}
    except Exception as exc:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32000, "message": str(exc)},
        }


def debug_log_path() -> Path | None:
    value = os.environ.get("AGENT_RECIPES_MCP_DEBUG_LOG")
    if not value:
        return None
    return Path(value).expanduser()


def debug_log(event: str, **fields: Any) -> None:
    path = debug_log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "pid": os.getpid(),
            "event": event,
        }
        payload.update(fields)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        return


def request_debug_fields(request: dict[str, Any]) -> dict[str, Any]:
    method = request.get("method")
    fields: dict[str, Any] = {
        "method": method,
        "id": request.get("id"),
    }
    params = request.get("params")
    if method == "initialize" and isinstance(params, dict):
        client_info = params.get("clientInfo")
        if isinstance(client_info, dict):
            fields["client_name"] = client_info.get("name")
            fields["client_version"] = client_info.get("version")
    elif method == "tools/call" and isinstance(params, dict):
        fields["tool_name"] = params.get("name")
    return fields


def response_debug_fields(request: dict[str, Any], response: dict[str, Any] | None) -> dict[str, Any]:
    fields = request_debug_fields(request)
    if response is None:
        fields["response"] = "none"
        return fields
    if "error" in response:
        fields["response"] = "error"
        error = response.get("error") or {}
        if isinstance(error, dict):
            fields["error_code"] = error.get("code")
        return fields
    fields["response"] = "result"
    result = response.get("result") or {}
    if request.get("method") == "tools/list" and isinstance(result, dict):
        tools = result.get("tools")
        if isinstance(tools, list):
            fields["tool_count"] = len(tools)
    return fields


def serve_stdio(project: str | Path = ".") -> int:
    default_project = Path(project)
    debug_log("server_start")
    for line in sys.stdin:
        if not line.strip():
            continue
        request: dict[str, Any] = {}
        try:
            decoded = json.loads(line)
            if not isinstance(decoded, dict):
                response = {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "JSON-RPC request must be an object"},
                }
                debug_log("request_invalid", json_type=type(decoded).__name__)
                debug_log("response", response="error", error_code=-32600)
                print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
                continue
            request = decoded
            debug_log("request", **request_debug_fields(request))
            response = handle_request(request, default_project=default_project)
        except json.JSONDecodeError as exc:
            response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": str(exc)}}
            debug_log("request_parse_error", error=str(exc))
        if response is None:
            debug_log("response", response="none")
            continue
        debug_log("response", **response_debug_fields(request, response))
        print(json.dumps(response, ensure_ascii=False, sort_keys=True), flush=True)
    debug_log("server_stop")
    return 0


def descriptor(project: str | Path = ".") -> dict[str, Any]:
    return {
        "ok": True,
        "action": "mcp",
        "mode": "local_v0",
        "tools": tool_list(),
        "stdio_command": ["agent-recipes", "mcp", "--stdio", "--project", str(project)],
        "claim_status": {
            "verified": ["MCP exposes core recipe, source_refinery, external adapter, and memory candidate dispatchers."],
            "inferred": [],
            "missing_evidence": ["尚未注册到真实 Codex/Claude/Hermes 客户端。"],
            "cannot_claim": ["不能说真实 agent 客户端已经连接 MCP。"],
        },
    }
