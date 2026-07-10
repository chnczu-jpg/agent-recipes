from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import real_pressure_manual_governance_readiness, write_json
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


def seed_quality_project(project: Path) -> tuple[Path, Path]:
    if not (project / ".venv").exists():
        (project / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
    fixture_dir = project / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    source = fixture_dir / "quality_source.md"
    source.write_text(
        "\n\n".join(
            [
                "card_type: correction_card\n"
                "before: agent wrote cloud output directly into a recipe\n"
                "correction: review_queue must accept candidate evidence before formal recipe changes\n"
                "after: keep source_trace, target_fields, evidence_strength, and cannot_claim\n"
                "cannot_claim: cannot say candidate evidence is verified truth",
                "card_type: run_chain_card\n"
                "steps: source_refinery cards then patch draft then review accept\n"
                "verification: quality benchmark checks candidate recall without claiming production quality\n"
                "cannot_claim: cannot say one benchmark proves all future searches",
                "card_type: failure_card\n"
                "failed_path: direct formal recipe write from candidate evidence\n"
                "failure_signal: recipe changed before review_queue accept\n"
                "replacement_path: review_queue before formal recipe\n"
                "cannot_claim: cannot say recover fixed every future failure",
            ]
        ),
        encoding="utf-8",
    )
    embedding_replay = fixture_dir / "embedding_replay.json"
    embedding_replay.write_text(
        json.dumps(
            {
                "rules": [
                    {"contains": "review_queue", "embedding": [1.0, 0.0, 0.0]},
                    {"contains": "source_trace", "embedding": [1.0, 0.0, 0.0]},
                ],
                "default_embedding": [0.0, 1.0, 0.0],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    query_replay = fixture_dir / "query_embedding.json"
    query_replay.write_text(json.dumps({"embedding": [1.0, 0.0, 0.0]}, ensure_ascii=False), encoding="utf-8")

    run_cli(project, "init")
    run_cli(project, "sources", "add", "fixtures/quality_source.md", "--read-only")
    run_cli(project, "scan", "--depth", "shallow")
    refined = run_cli(
        project,
        "refine",
        "--query",
        "review_queue source_trace candidate",
        "--knowledge-need",
        "KN_QUALITY_BENCHMARK",
        "--target-recipe",
        "recipe_quality_benchmark_v0",
        "--candidate-fields",
        "forbidden_path,failure_signals,verified_path,checklist_item,cannot_claim",
    )
    run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
    drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_quality_benchmark_v0")
    run_cli(project, "review", "--accept", drafted["review_id"])
    run_cli(project, "memory-index", "--adapter", "cognee")
    run_cli(project, "memory-index", "--adapter", "graphiti")
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
    run_cli(project, "embedding-index", "--provider", "qwen3", "--response-json", "fixtures/embedding_replay.json")
    return embedding_replay, query_replay


def seed_review_triage_item(
    project: Path,
    *,
    review_id: str,
    target_recipe_id: str,
    source_name: str,
    proposed_value_count: int,
    risk: str = "needs_review",
    title: str | None = None,
) -> None:
    recipes_dir = project / ".recipes"
    cards_dir = recipes_dir / "source_refinery" / "cards"
    card_dir = cards_dir / "learning_atom_cards"
    card_dir.mkdir(parents=True, exist_ok=True)
    (recipes_dir / "source_refinery" / "patch_drafts").mkdir(parents=True, exist_ok=True)
    (recipes_dir / "candidates").mkdir(parents=True, exist_ok=True)
    (recipes_dir / "review_queue").mkdir(parents=True, exist_ok=True)
    card_id = f"card_{review_id}"
    source_path = str(project / "fixtures" / source_name)
    card = {
        "card_id": card_id,
        "card_type": "learning_atom_card",
        "source_trace": [{"path": source_path, "record_id": f"chunk_{review_id}", "record_type": "source_chunk"}],
        "target_recipe_id": target_recipe_id,
        "target_fields": ["checklist_item", "verified_path", "cannot_claim"],
        "evidence_strength": "candidate",
        "extracted_payload": {"checklist_item": [f"{source_name} item {index}" for index in range(max(proposed_value_count, 1))]},
        "cannot_claim": ["cannot say candidate is formal"],
        "status": "candidate",
    }
    (card_dir / f"{card_id}.json").write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
    cards_index = cards_dir / "cards.jsonl"
    existing_cards = cards_index.read_text(encoding="utf-8") if cards_index.exists() else ""
    cards_index.write_text(existing_cards + json.dumps(card, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    patch_draft_id = f"patch_draft_{review_id}"
    patch_id = f"patch_{review_id}"
    proposed_values = [f"{source_name} proposed {index}" for index in range(proposed_value_count)]
    patch_draft = {
        "patch_draft_id": patch_draft_id,
        "target_recipe_id": target_recipe_id,
        "source_card_ids": [card_id],
        "target_fields": ["checklist_item"],
        "proposed_additions": {"checklist_item": proposed_values},
        "review_hints": {"proposed_value_count": proposed_value_count},
        "status": "pending_review",
    }
    (recipes_dir / "source_refinery" / "patch_drafts" / f"{patch_draft_id}.json").write_text(
        json.dumps(patch_draft, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    proposed_change = {
        "recipe_id": target_recipe_id,
        "checklist_item": proposed_values,
        "cannot_claim": ["cannot say candidate is formal"],
    }
    if title:
        proposed_change["title"] = title
    patch = {
        "patch_id": patch_id,
        "patch_type": "source_refinery_patch_draft",
        "source_patch_draft_id": patch_draft_id,
        "source_card_ids": [card_id],
        "target_recipe_id": target_recipe_id,
        "proposed_change": proposed_change,
        "risk": risk,
        "status": "pending_review",
    }
    (recipes_dir / "candidates" / f"{patch_id}.json").write_text(json.dumps(patch, ensure_ascii=False, indent=2), encoding="utf-8")
    review = {
        "review_id": review_id,
        "proposed_patch_id": patch_id,
        "source_patch_draft_id": patch_draft_id,
        "evidence_refs": [card_id],
        "review_hints": {"proposed_value_count": proposed_value_count},
        "recommendation": "review",
        "status": "pending",
    }
    (recipes_dir / "review_queue" / f"{review_id}.json").write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")


@unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
class Phase10QualityBenchmarkTest(unittest.TestCase):
    def test_manual_governance_readiness_complete_packet_is_still_not_a_fix(self) -> None:
        item = {
            "project": "type_color_shadow_pool",
            "latest_report_id": "duplicate_governance_ready",
            "latest_markdown_path": "/tmp/duplicate_governance_ready.md",
            "latest_what_if_cases_path": "/tmp/duplicate_governance_ready_what_if_cases.json",
            "candidate_recipe_ids": [
                "recipe_narrow_type_color_v1",
                "recipe_broad_visual_check_v1",
            ],
            "decision_options": [
                {
                    "action": "merge_or_supersede",
                    "can_auto_apply": False,
                    "post_decision_validation": ["重新跑 lookup-pressure。"],
                },
                {
                    "action": "mark_narrow_recipe_evidence_only",
                    "can_auto_apply": False,
                    "post_decision_validation": ["重新跑 lock-pressure。"],
                },
                {
                    "action": "add_explicit_priority_rule",
                    "can_auto_apply": False,
                    "post_decision_validation": ["重新跑 consumption-coverage。"],
                },
                {
                    "action": "split_broad_recipe_scope",
                    "can_auto_apply": False,
                    "post_decision_validation": ["重新跑 real-pressure-summary。"],
                },
            ],
        }

        readiness = real_pressure_manual_governance_readiness([item])

        self.assertEqual(len(readiness), 1)
        self.assertTrue(readiness[0]["ready_for_human_decision"])
        self.assertEqual(readiness[0]["missing_evidence"], [])
        self.assertEqual(
            readiness[0]["decision_actions"],
            [
                "add_explicit_priority_rule",
                "mark_narrow_recipe_evidence_only",
                "merge_or_supersede",
                "split_broad_recipe_scope",
            ],
        )
        self.assertIn("系统仍不能自动选择", readiness[0]["plain"])
        self.assertIn("不能说 readiness=true 就代表 duplicate shadow 已修复。", readiness[0]["cannot_claim"])

    def test_quality_benchmark_scores_candidate_recall_and_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, query_replay = seed_quality_project(project)

            result = run_cli(
                project,
                "quality-benchmark",
                "--qwen-response-json",
                str(query_replay.relative_to(project)),
            )

            self.assertEqual(result["action"], "quality-benchmark")
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["failed"], 0)
            self.assertGreaterEqual(result["quality_score"], 0.8)
            self.assertTrue(Path(result["report_path"]).exists())
            case_status = {case["case_id"]: case["status"] for case in result["cases"]}
            self.assertEqual(case_status["source_search_source_trace"], "passed")
            self.assertEqual(case_status["cognee_memory_review_gate"], "passed")
            self.assertEqual(case_status["graphiti_patch_review_relationship"], "passed")
            self.assertEqual(case_status["qwen_embedding_recall"], "passed")
            self.assertEqual(case_status["review_gate_patch_to_recipe"], "passed")
            self.assertIn("不能说一次本地基准证明生产级质量。", result["claim_status"]["cannot_claim"])

    def test_quality_benchmark_skips_qwen_without_replay_or_loopback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            seed_quality_project(project)

            result = run_cli(project, "quality-benchmark")

            qwen_case = next(case for case in result["cases"] if case["case_id"] == "qwen_embedding_recall")
            self.assertEqual(qwen_case["status"], "skipped")
            self.assertIn("Qwen quality case needs --qwen-response-json or --allow-loopback.", result["claim_status"]["missing_evidence"])
            self.assertEqual(result["summary"]["failed"], 0)

    def test_mcp_exposes_quality_benchmark(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_quality_benchmark", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            _, query_replay = seed_quality_project(project)

            result = call_tool(
                "quality_benchmark",
                {
                    "project": str(project),
                    "qwen_response_json": str(query_replay.relative_to(project)),
                },
            )

            self.assertEqual(result["tool"], "agent_recipes_quality_benchmark")
            self.assertEqual(result["transport"], "mcp")
            self.assertEqual(result["summary"]["failed"], 0)

    def test_lookup_pressure_blocks_narrow_recipe_overreach(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_big_text_gate_v1",
                "version": 1,
                "recipe_hash": "hash_big_text",
                "title": "Big Text Behind Presenter Gate",
                "steps": ["large words should often live behind the presenter using clean matte"],
                "forbidden_path": ["Remotion/HyperFrames may only use reviewed stills or prototype before timeline cutout gate."],
                "cannot_claim": ["cannot claim complete post-production workflow coverage"],
                "verification": ["static still review before 2 second motion"],
            }
            (recipes_dir / "recipe_big_text_gate_v1.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "positive_big_text_gate",
                                "query": "big text behind presenter matte 2 second motion",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_big_text_gate_v1",
                                "required_terms": ["behind the presenter", "matte", "2 second motion"],
                            },
                            {
                                "case_id": "negative_subtitle_rhythm",
                                "query": "subtitle rhythm lower third not blocking subject",
                                "expect_applicable": False,
                                "overreach_recipe_id": "recipe_big_text_gate_v1",
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json")

            self.assertEqual(result["action"], "lookup-pressure")
            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["passed"], 2)
            self.assertEqual(result["summary"]["failed"], 0)
            negative_case = next(case for case in result["cases"] if case["case_id"] == "negative_subtitle_rhythm")
            self.assertEqual(negative_case["status"], "passed")
            self.assertIsNone(negative_case["selected_recipe_id"])
            self.assertEqual(negative_case["no_match_reason"], "没有足够适用的 recipe。")
            self.assertTrue(Path(result["report_path"]).exists())

    def test_lookup_pressure_positive_no_match_returns_failed_case_not_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            unrelated = {
                "recipe_id": "recipe_audio_ducking_v1",
                "version": 1,
                "recipe_hash": "hash_audio_ducking",
                "title": "Audio Ducking Gate",
                "use_when": ["balance narration with background music"],
                "steps": ["lower BGM under speech"],
                "verification": ["listen to voice intelligibility"],
            }
            (recipes_dir / "recipe_audio_ducking_v1.json").write_text(
                json.dumps(unrelated, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "positive_without_recipe",
                                "query": "big text behind presenter matte",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_missing",
                                "required_terms": ["behind presenter"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json", expect_ok=False)

            self.assertEqual(result["action"], "lookup-pressure")
            self.assertFalse(result["ok"])
            self.assertEqual(result["summary"]["failed"], 1)
            case = result["cases"][0]
            self.assertEqual(case["status"], "failed")
            self.assertIn("no sufficient recipe", case["failure_reasons"][0])

    def test_lookup_pressure_reports_shadowed_expected_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            broad = {
                "recipe_id": "recipe_z_layer_type_color_visual_check_v1",
                "version": 1,
                "recipe_hash": "hash_broad_type_color",
                "title": "Layer Type Color Visual Check",
                "use_when": ["type color contract muted green sage"],
                "steps": [
                    "Does the face stay the first read?",
                    "Does the big word sit behind the presenter in layer order?",
                    "Does the palette relate to skin, pink clothing, and warm wall?",
                    "color_relation: muted green/sage only; reject web button feeling",
                ],
            }
            narrow = {
                "recipe_id": "recipe_a_narrow_type_color_contract_v1",
                "version": 1,
                "recipe_hash": "hash_narrow_type_color",
                "title": "Narrow Type Color Contract",
                "use_when": ["type color contract muted green sage"],
                "steps": [
                    "Does the face stay the first read?",
                    "Does the big word sit behind the presenter in layer order?",
                    "Does the palette relate to skin, pink clothing, and warm wall?",
                    "color_relation: muted green/sage only; reject web button feeling",
                ],
            }
            (recipes_dir / "recipe_z_layer_type_color_visual_check_v1.json").write_text(
                json.dumps(broad, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_a_narrow_type_color_contract_v1.json").write_text(
                json.dumps(narrow, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "narrow_contract_shadowed",
                                "query": "type color contract muted green sage face first read palette skin pink clothing warm wall",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_a_narrow_type_color_contract_v1",
                                "required_terms": ["Does the face stay the first read?"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json", expect_ok=False)

            self.assertFalse(result["ok"])
            self.assertEqual(result["summary"]["duplicate_shadow_count"], 1)
            case = result["cases"][0]
            self.assertEqual(case["selected_recipe_id"], "recipe_z_layer_type_color_visual_check_v1")
            shadow = case["shadowed_expected_recipe"]
            self.assertEqual(shadow["expected_recipe_id"], "recipe_a_narrow_type_color_contract_v1")
            self.assertEqual(shadow["selected_recipe_id"], "recipe_z_layer_type_color_visual_check_v1")
            self.assertEqual(shadow["status"], "duplicate_shadow_risk")

    def test_lookup_priority_rule_resolves_shadow_without_changing_broad_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            broad = {
                "recipe_id": "recipe_z_layer_type_color_visual_check_v1",
                "version": 1,
                "recipe_hash": "hash_broad_type_color",
                "title": "Layer Type Color Visual Check",
                "use_when": ["big type color contract muted green sage"],
                "steps": [
                    "Does the face stay the first read?",
                    "Does the big word sit behind the presenter in layer order?",
                    "Does the palette relate to skin, pink clothing, and warm wall?",
                ],
            }
            narrow = {
                "recipe_id": "recipe_a_narrow_type_color_contract_v1",
                "version": 1,
                "recipe_hash": "hash_narrow_type_color",
                "title": "Narrow Type Color Contract",
                "use_when": ["type color contract muted green sage"],
                "steps": [
                    "Does the face stay the first read?",
                    "Does the big word sit behind the presenter in layer order?",
                    "Does the palette relate to skin, pink clothing, and warm wall?",
                ],
            }
            before_after = {
                "recipe_id": "recipe_before_after_review_gate_v1",
                "version": 1,
                "recipe_hash": "hash_before_after",
                "title": "Before After Review Gate",
                "use_when": ["before after review gate presenter sharper one second pass questions"],
                "steps": [
                    "pass_questions",
                    "presenter sharper",
                    "one second",
                ],
            }
            (recipes_dir / "recipe_z_layer_type_color_visual_check_v1.json").write_text(
                json.dumps(broad, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_a_narrow_type_color_contract_v1.json").write_text(
                json.dumps(narrow, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_before_after_review_gate_v1.json").write_text(
                json.dumps(before_after, ensure_ascii=False),
                encoding="utf-8",
            )
            write_json(
                project / ".recipes" / "lookup_priority_rules.json",
                {
                    "version": 1,
                    "rules": [
                        {
                            "rule_id": "priority_type_color_contract_v1",
                            "enabled": True,
                            "preferred_recipe_id": "recipe_a_narrow_type_color_contract_v1",
                            "fallback_recipe_id": "recipe_z_layer_type_color_visual_check_v1",
                            "when_query_contains_all": ["type", "color", "contract"],
                            "bonus": 100,
                            "reason": "Prefer the narrow contract when the query explicitly asks for the contract.",
                        }
                    ],
                },
            )
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "narrow_contract_priority",
                                "query": "big type color contract muted green sage face first read",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_a_narrow_type_color_contract_v1",
                                "required_terms": ["Does the face stay the first read?"],
                            },
                            {
                                "case_id": "broad_visual_still_wins_without_contract",
                                "query": "big type color muted green sage face first read",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_z_layer_type_color_visual_check_v1",
                                "required_terms": ["Does the big word sit behind the presenter"],
                            },
                            {
                                "case_id": "before_after_contract_does_not_trigger_type_color_priority",
                                "query": "before after review gate contract presenter sharper one second pass questions",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_before_after_review_gate_v1",
                                "required_terms": ["presenter sharper"],
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json")
            direct_lookup = run_cli(project, "lookup", "big type color contract muted green sage face first read", "--strict")

            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["duplicate_shadow_count"], 0)
            self.assertEqual(result["cases"][0]["selected_recipe_id"], "recipe_a_narrow_type_color_contract_v1")
            self.assertEqual(result["cases"][1]["selected_recipe_id"], "recipe_z_layer_type_color_visual_check_v1")
            self.assertEqual(result["cases"][2]["selected_recipe_id"], "recipe_before_after_review_gate_v1")
            self.assertEqual(direct_lookup["recipe"]["recipe_id"], "recipe_a_narrow_type_color_contract_v1")
            self.assertEqual(direct_lookup["candidates"][0]["priority_bonus"], 100)
            self.assertEqual(direct_lookup["candidates"][0]["priority_rules_applied"][0]["rule_id"], "priority_type_color_contract_v1")

    def test_lookup_priority_ignores_negated_recipe_mentions_and_handles_chinese_before_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            pip_recipe = {
                "recipe_id": "recipe_pip_safe_margin_v1",
                "version": 1,
                "recipe_hash": "hash_pip_safe_margin",
                "title": "PIP Safe Margin Gate",
                "use_when": ["PIP host placement safe margin"],
                "steps": ["pip_layout_packet has safe margin and obstruction_check."],
            }
            layer_recipe = {
                "recipe_id": "recipe_layer_type_color_v1",
                "version": 1,
                "recipe_hash": "hash_layer_type_color",
                "title": "Layer Type Color Visual Check",
                "use_when": ["big type color visual check"],
                "steps": ["Does the palette relate to skin, clothing, and wall?"],
            }
            before_after_recipe = {
                "recipe_id": "recipe_before_after_review_v1",
                "version": 1,
                "recipe_hash": "hash_before_after_review",
                "title": "Before After Review Gate",
                "use_when": ["before after review gate"],
                "steps": ["pass_questions: presenter sharper", "pass_questions: one second"],
            }
            (recipes_dir / "recipe_pip_safe_margin_v1.json").write_text(json.dumps(pip_recipe, ensure_ascii=False), encoding="utf-8")
            (recipes_dir / "recipe_layer_type_color_v1.json").write_text(json.dumps(layer_recipe, ensure_ascii=False), encoding="utf-8")
            (recipes_dir / "recipe_before_after_review_v1.json").write_text(
                json.dumps(before_after_recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            write_json(
                project / ".recipes" / "lookup_priority_rules.json",
                {
                    "version": 1,
                    "rules": [
                        {
                            "rule_id": "prefer_pip",
                            "enabled": True,
                            "preferred_recipe_id": "recipe_pip_safe_margin_v1",
                            "when_query_contains_any": ["PIP", "小窗", "主持人小窗"],
                            "bonus": 20,
                        },
                        {
                            "rule_id": "prefer_before_after",
                            "enabled": True,
                            "preferred_recipe_id": "recipe_before_after_review_v1",
                            "when_query_contains_any": ["新版", "旧版", "比旧版", "review"],
                            "when_query_contains_all": ["通过"],
                            "bonus": 30,
                        },
                    ],
                },
            )

            no_pip = run_cli(
                project,
                "lookup",
                "用户只问普通字幕断句和底部安全区，不需要主持人小窗，也不涉及 PIP、产品图、大字和声音。你会不会套 PIP safe margin 菜谱？",
                "--strict",
                expect_ok=False,
            )
            before_after = run_cli(
                project,
                "lookup",
                "一个 agent 说新版比旧版高级，因为大字更多、动效更多、颜色更亮。用户问能不能直接说新版通过。你怎么做 before/after review？",
                "--strict",
            )

            self.assertEqual(no_pip["code"], "AR242")
            self.assertEqual(before_after["recipe"]["recipe_id"], "recipe_before_after_review_v1")
            self.assertEqual(before_after["applicability"]["status"], "strong")
            self.assertIn("新版", before_after["applicability"]["matched_terms"])
            self.assertIn("通过", before_after["applicability"]["matched_terms"])

    def test_strict_lookup_rejects_overbroad_full_workflow_quality_claims(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                recipes_dir / "recipe_title_callout_infocard_candidate_v2.json",
                {
                    "recipe_id": "recipe_title_callout_infocard_candidate_v2",
                    "version": 2,
                    "recipe_hash": "hash_title_callout",
                    "title": "Title Callout Infocard Candidate",
                    "use_when": ["SampleProject 后期包装 字幕 专场 转场 声音"],
                    "steps": ["Use only for a narrow title/callout/infocard candidate."],
                },
            )

            result = run_cli(
                project,
                "lookup",
                "SampleProject 完整后期包装流程 大字 花字 字幕 专场 特效 转场 声音 全流程质量通过",
                "--strict",
                expect_ok=False,
            )

            self.assertEqual(result["code"], "AR242")

    def test_duplicate_governance_reports_shadow_risk_without_changing_recipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            broad = {
                "recipe_id": "recipe_z_layer_type_color_visual_check_v1",
                "version": 1,
                "recipe_hash": "hash_broad_type_color",
                "title": "Layer Type Color Visual Check",
                "use_when": ["big type color muted green sage"],
                "steps": ["Does the face stay the first read?"],
            }
            narrow = {
                "recipe_id": "recipe_a_narrow_type_color_contract_v1",
                "version": 1,
                "recipe_hash": "hash_narrow_type_color",
                "title": "Narrow Type Color Contract",
                "use_when": ["type color contract muted green sage"],
                "steps": ["Does the face stay the first read?"],
            }
            (recipes_dir / "recipe_z_layer_type_color_visual_check_v1.json").write_text(
                json.dumps(broad, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_a_narrow_type_color_contract_v1.json").write_text(
                json.dumps(narrow, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "narrow_contract_shadowed",
                                "query": "big type color contract muted green sage face first read",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_a_narrow_type_color_contract_v1",
                                "required_terms": ["Does the face stay the first read?"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json", expect_ok=False)
            before = sorted(path.name for path in recipes_dir.glob("*.json"))

            result = run_cli(project, "duplicate-governance")

            self.assertEqual(result["action"], "duplicate-governance")
            self.assertTrue(result["ok"])
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["shadow_risk_count"], 1)
            risk = result["risks"][0]
            self.assertEqual(risk["risk_type"], "duplicate_shadow_risk")
            self.assertEqual(risk["expected_recipe_id"], "recipe_a_narrow_type_color_contract_v1")
            self.assertEqual(risk["selected_recipe_id"], "recipe_z_layer_type_color_visual_check_v1")
            self.assertEqual(risk["recommended_action"], "human_governance_required")
            self.assertIn("merge_or_supersede", risk["candidate_actions"])
            self.assertIn("cannot auto merge", " ".join(risk["cannot_claim"]))
            matrix = {item["action"]: item for item in result["decision_matrix"]}
            self.assertEqual(set(matrix), {"merge_or_supersede", "mark_narrow_recipe_evidence_only", "add_explicit_priority_rule", "split_broad_recipe_scope"})
            self.assertTrue(matrix["merge_or_supersede"]["requires_human_decision"])
            self.assertFalse(matrix["merge_or_supersede"]["can_auto_apply"])
            self.assertIn("正式菜谱关系", matrix["merge_or_supersede"]["changes"])
            self.assertIn("不能自动执行", matrix["add_explicit_priority_rule"]["cannot_claim"])
            self.assertTrue(any("lookup-pressure" in item for item in matrix["add_explicit_priority_rule"]["post_decision_validation"]))
            self.assertTrue(any("lock-pressure" in item for item in matrix["split_broad_recipe_scope"]["post_decision_validation"]))
            self.assertTrue(any("real-pressure-summary" in item for item in matrix["merge_or_supersede"]["post_decision_validation"]))
            priority_rules = result["candidate_priority_rules"]
            self.assertEqual(priority_rules[0]["action"], "add_explicit_priority_rule")
            self.assertTrue(priority_rules[0]["candidate_only"])
            self.assertFalse(priority_rules[0]["can_auto_apply"])
            self.assertTrue(priority_rules[0]["requires_human_decision"])
            self.assertEqual(priority_rules[0]["preferred_recipe_id"], "recipe_a_narrow_type_color_contract_v1")
            self.assertEqual(priority_rules[0]["fallback_recipe_id"], "recipe_z_layer_type_color_visual_check_v1")
            self.assertIn("contract", priority_rules[0]["when_query_contains_any"])
            self.assertIn("big", priority_rules[0]["selected_only_terms"])
            templates = result["what_if_validation_cases"]
            self.assertEqual(templates[0]["risk_case_id"], "narrow_contract_shadowed")
            self.assertEqual(templates[0]["candidate_recipe_ids"], ["recipe_a_narrow_type_color_contract_v1", "recipe_z_layer_type_color_visual_check_v1"])
            self.assertEqual(templates[0]["lookup_pressure_case_template"]["expected_recipe_id"], "CHOOSE_AFTER_HUMAN_GOVERNANCE")
            self.assertEqual(templates[0]["lock_pressure_case_template"]["expected_recipe_id"], "CHOOSE_AFTER_HUMAN_GOVERNANCE")
            self.assertFalse(templates[0]["can_run_before_decision"])
            after = sorted(path.name for path in recipes_dir.glob("*.json"))
            self.assertEqual(after, before)
            self.assertTrue(Path(result["report_path"]).exists())
            self.assertTrue(Path(result["markdown_path"]).exists())
            self.assertTrue(Path(result["what_if_cases_path"]).exists())
            what_if_cases = json.loads(Path(result["what_if_cases_path"]).read_text(encoding="utf-8"))
            self.assertFalse(what_if_cases["can_run_before_decision"])
            self.assertEqual(
                what_if_cases["lookup_pressure_case_templates"]["cases"][0]["expected_recipe_id"],
                "CHOOSE_AFTER_HUMAN_GOVERNANCE",
            )
            self.assertEqual(
                what_if_cases["lock_pressure_case_templates"]["cases"][0]["expected_recipe_id"],
                "CHOOSE_AFTER_HUMAN_GOVERNANCE",
            )
            markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("需要人工治理", markdown)
            self.assertIn("不是自动修复包", markdown)
            self.assertIn("治理选项矩阵", markdown)
            self.assertIn("加明确优先级", markdown)
            self.assertIn("应用后怎么验收", markdown)
            self.assertIn("What-if 验收 case 模板", markdown)
            self.assertIn("候选优先级规则草案", markdown)
            self.assertIn("contract", markdown)
            self.assertIn("_what_if_cases.json", markdown)
            self.assertIn("CHOOSE_AFTER_HUMAN_GOVERNANCE", markdown)
            self.assertIn("lookup-pressure", markdown)
            self.assertIn("系统现在分不清谁应该优先", markdown)
            self.assertIn("合并或废弃其中一条", markdown)

    def test_pressure_blocks_unfilled_governance_placeholder_cases(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            lookup_cases = project / "lookup_pressure_cases.json"
            lookup_cases.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "post_governance_lookup_template",
                                "query": "type color contract muted green sage",
                                "expect_applicable": True,
                                "expected_recipe_id": "CHOOSE_AFTER_HUMAN_GOVERNANCE",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            lock_cases = project / "lock_pressure_cases.json"
            lock_cases.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "post_governance_lock_template",
                                "query": "type color contract muted green sage",
                                "task": "post governance validation",
                                "expect_lock": True,
                                "expected_recipe_id": "CHOOSE_AFTER_HUMAN_GOVERNANCE",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            lookup = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json", expect_ok=False)
            lock = run_cli(project, "lock-pressure", "--cases", "lock_pressure_cases.json", expect_ok=False)

            self.assertEqual(lookup["cases"][0]["status"], "blocked")
            self.assertIn("CHOOSE_AFTER_HUMAN_GOVERNANCE", " ".join(lookup["cases"][0]["missing_evidence"]))
            self.assertIn("人工治理", " ".join(lookup["cases"][0]["missing_evidence"]))
            self.assertEqual(lock["cases"][0]["status"], "blocked")
            self.assertIn("CHOOSE_AFTER_HUMAN_GOVERNANCE", " ".join(lock["cases"][0]["missing_evidence"]))
            self.assertIn("人工治理", " ".join(lock["cases"][0]["missing_evidence"]))

    def test_duplicate_governance_uses_latest_pressure_report_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            reports_dir = project / ".recipes" / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            old_report = {
                "action": "lookup-pressure",
                "ok": False,
                "report_id": "lookup_pressure_old_shadow",
                "checked_at": "2026-01-01T00:00:00Z",
                "cases": [
                    {
                        "case_id": "old_shadow",
                        "query": "type color",
                        "expected_recipe_id": "recipe_expected",
                        "selected_recipe_id": "recipe_selected",
                        "shadowed_expected_recipe": {
                            "expected_recipe_id": "recipe_expected",
                            "selected_recipe_id": "recipe_selected",
                            "expected_score": 8,
                            "selected_score": 8,
                        },
                    }
                ],
                "summary": {"duplicate_shadow_count": 1},
            }
            latest_report = {
                "action": "lookup-pressure",
                "ok": True,
                "report_id": "lookup_pressure_latest_clean",
                "checked_at": "2026-01-02T00:00:00Z",
                "cases": [],
                "summary": {"duplicate_shadow_count": 0},
            }
            (reports_dir / "lookup_pressure_old_shadow.json").write_text(json.dumps(old_report, ensure_ascii=False), encoding="utf-8")
            (reports_dir / "lookup_pressure_latest_clean.json").write_text(json.dumps(latest_report, ensure_ascii=False), encoding="utf-8")

            result = run_cli(project, "duplicate-governance")

            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["shadow_risk_count"], 0)
            self.assertEqual(result["risks"], [])

    def test_duplicate_governance_ignores_unofficial_simulation_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            reports_dir = project / ".recipes" / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            official_shadow = {
                "action": "lookup-pressure",
                "ok": False,
                "official_pressure_evidence": True,
                "pressure_evidence_scope": "project",
                "report_id": "lookup_pressure_official_shadow",
                "checked_at": "2026-01-01T00:00:00Z",
                "cases": [
                    {
                        "case_id": "official_shadow",
                        "query": "type color contract",
                        "expected_recipe_id": "recipe_expected",
                        "selected_recipe_id": "recipe_selected",
                        "shadowed_expected_recipe": {
                            "expected_recipe_id": "recipe_expected",
                            "selected_recipe_id": "recipe_selected",
                            "expected_score": 8,
                            "selected_score": 8,
                            "expected_matched_terms": ["contract"],
                            "selected_matched_terms": ["type"],
                        },
                    }
                ],
                "summary": {"duplicate_shadow_count": 1},
            }
            unofficial_clean = {
                "action": "lookup-pressure",
                "ok": False,
                "official_pressure_evidence": False,
                "pressure_evidence_scope": "external_cases",
                "report_id": "lookup_pressure_unofficial_clean",
                "checked_at": "2026-01-02T00:00:00Z",
                "cases": [],
                "summary": {"blocked": 1, "duplicate_shadow_count": 0},
            }
            (reports_dir / "lookup_pressure_official_shadow.json").write_text(json.dumps(official_shadow, ensure_ascii=False), encoding="utf-8")
            (reports_dir / "lookup_pressure_unofficial_clean.json").write_text(json.dumps(unofficial_clean, ensure_ascii=False), encoding="utf-8")

            result = run_cli(project, "duplicate-governance")

            self.assertEqual(result["summary"]["shadow_risk_count"], 1)
            self.assertEqual(result["risks"][0]["source_report_id"], "lookup_pressure_official_shadow")
            self.assertEqual(result["candidate_priority_rules"][0]["preferred_recipe_id"], "recipe_expected")
            self.assertIn("contract", result["candidate_priority_rules"][0]["when_query_contains_any"])

    def test_lookup_pressure_negative_fails_if_any_recipe_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            big_text = {
                "recipe_id": "recipe_big_text_gate_v1",
                "version": 1,
                "recipe_hash": "hash_big_text",
                "title": "Big Text Behind Presenter Gate",
                "steps": ["large words behind presenter"],
            }
            provider = {
                "recipe_id": "recipe_provider_generation_gate_v1",
                "version": 1,
                "recipe_hash": "hash_provider",
                "title": "Provider Generation Gate",
                "steps": ["provider route grok cli exact command"],
            }
            (recipes_dir / "recipe_big_text_gate_v1.json").write_text(
                json.dumps(big_text, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_provider_generation_gate_v1.json").write_text(
                json.dumps(provider, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "negative_export_should_not_select_any_recipe",
                                "query": "provider route grok cli exact command",
                                "expect_applicable": False,
                                "overreach_recipe_id": "recipe_big_text_gate_v1",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json", expect_ok=False)

            self.assertFalse(result["ok"])
            self.assertEqual(result["summary"]["failed"], 1)
            case = result["cases"][0]
            self.assertEqual(case["status"], "failed")
            self.assertEqual(case["selected_recipe_id"], "recipe_provider_generation_gate_v1")
            self.assertIn("negative case selected out-of-scope recipe", " ".join(case["failure_reasons"]))

    def test_lookup_pressure_negative_can_allow_other_valid_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            sound_recipe = {
                "recipe_id": "recipe_sound_bgm_gate_v1",
                "version": 1,
                "recipe_hash": "hash_sound",
                "title": "Sound BGM Gate",
                "use_when": ["BGM SFX 音效 声音 人耳听感"],
                "steps": ["BGM 要先看文案"],
            }
            card_recipe = {
                "recipe_id": "recipe_card_visual_gate_v1",
                "version": 1,
                "recipe_hash": "hash_card",
                "title": "Card Visual Gate",
                "use_when": ["card visual support"],
                "steps": ["Do not use card recipe for audio-only work."],
            }
            (recipes_dir / "recipe_sound_bgm_gate_v1.json").write_text(json.dumps(sound_recipe, ensure_ascii=False), encoding="utf-8")
            (recipes_dir / "recipe_card_visual_gate_v1.json").write_text(json.dumps(card_recipe, ensure_ascii=False), encoding="utf-8")
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "audio_may_match_sound_but_not_card",
                                "query": "BGM SFX 音效 声音 人耳听感",
                                "expect_applicable": False,
                                "overreach_recipe_id": "recipe_card_visual_gate_v1",
                                "allow_other_recipe": True,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json")

            self.assertTrue(result["ok"])
            case = result["cases"][0]
            self.assertTrue(case["passed"])
            self.assertEqual(case["selected_recipe_id"], "recipe_sound_bgm_gate_v1")
            self.assertTrue(case["allow_other_recipe"])
            self.assertIn("允许命中非禁止 recipe", case["cannot_claim"])

    def test_lookup_ignores_long_natural_language_filler_and_negated_scope_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_big_text_behind_presenter_v1",
                "version": 1,
                "recipe_hash": "hash_big_text_behind_presenter",
                "title": "SampleProject Postprod Big Text Behind Presenter Gate",
                "use_when": ["large words behind presenter"],
                "steps": ["use presenter cutout / matte before motion"],
                "verification": ["contact sheet before claiming quality"],
                "cannot_claim": ["cannot claim visual quality passed"],
            }
            (recipes_dir / "recipe_big_text_behind_presenter_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            keyframe_recipe = {
                "recipe_id": "recipe_keyframe_motion_reject_v1",
                "version": 1,
                "recipe_hash": "hash_keyframe_motion_reject",
                "title": "SampleProject Keyframe Motion Reject Gate",
                "use_when": ["keyframe motion reject gate"],
                "steps": ["motion_rejects: any layer shifts enough to look like website animation"],
                "verification": ["review still_frame_layer_map before motion"],
                "cannot_claim": ["cannot claim motion quality passed"],
            }
            (recipes_dir / "recipe_keyframe_motion_reject_v1.json").write_text(
                json.dumps(keyframe_recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            natural = run_cli(
                project,
                "lookup",
                "SampleProject 后期包装里有一段口播，用户说“大字要像在主持人后面，不要像贴纸盖在脸上”，但目前只有一张 still 和一句风格描述。你下一步怎么推进？",
                "--strict",
            )

            self.assertEqual(natural["recipe"]["recipe_id"], "recipe_big_text_behind_presenter_v1")
            self.assertEqual(natural["applicability"]["status"], "strong")
            self.assertIn("大字", natural["applicability"]["matched_terms"])
            self.assertLessEqual(len(natural["applicability"]["missing_query_terms"]), 2)

            motion = run_cli(
                project,
                "lookup",
                "有人给了一个 future_motion_test，里面有 2 秒 opacity / x_offset / scale / easeOutCubic，想让你直接做动效预演。",
                "--strict",
            )

            self.assertEqual(motion["recipe"]["recipe_id"], "recipe_keyframe_motion_reject_v1")
            self.assertEqual(motion["applicability"]["status"], "strong")
            self.assertNotIn("opacity", motion["applicability"]["missing_query_terms"])

            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "subtitle_does_not_overreach_to_big_text",
                                "query": "用户只问普通字幕断句和底部安全区，不涉及大字、花字、PIP、声音、转场。你怎么处理？",
                                "expect_applicable": False,
                                "overreach_recipe_id": "recipe_big_text_behind_presenter_v1",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            negative = run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json")

            self.assertTrue(negative["ok"])
            self.assertEqual(negative["cases"][0]["status"], "passed")

    def test_lock_pressure_creates_positive_lock_and_prevents_negative_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_big_text_gate_v1",
                "version": 1,
                "recipe_hash": "hash_big_text_lock",
                "title": "Big Text Behind Presenter Gate",
                "use_when": ["large words behind presenter"],
                "steps": ["use presenter cutout and matte before motion"],
                "verification": ["review still before motion"],
                "forbidden_path": ["do not claim complete subtitle workflow"],
                "cannot_claim": ["cannot claim visual quality passed"],
                "stop_line": "lock before execution",
            }
            (recipes_dir / "recipe_big_text_gate_v1.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
            cases_path = project / "lock_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "positive_lock_big_text",
                                "query": "big text behind presenter cutout matte",
                                "expect_lock": True,
                                "expected_recipe_id": "recipe_big_text_gate_v1",
                                "task": "lock pressure positive",
                            },
                            {
                                "case_id": "negative_no_subtitle_lock",
                                "query": "subtitle rhythm lower third line breaks",
                                "expect_lock": False,
                                "overreach_recipe_id": "recipe_big_text_gate_v1",
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "lock-pressure", "--cases", "lock_pressure_cases.json")

            self.assertEqual(result["action"], "lock-pressure")
            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["passed"], 2)
            self.assertEqual(result["summary"]["failed"], 0)
            self.assertEqual(result["summary"]["locks_created_or_reused"], 1)
            self.assertEqual(result["summary"]["locks_prevented"], 1)
            positive = next(case for case in result["cases"] if case["case_id"] == "positive_lock_big_text")
            negative = next(case for case in result["cases"] if case["case_id"] == "negative_no_subtitle_lock")
            self.assertTrue(positive["lock_id"])
            self.assertEqual(positive["lock_status"], "created")
            self.assertEqual(negative["lock_status"], "prevented")
            self.assertEqual(len(list((project / ".recipes" / "locks").glob("*.json"))), 1)

    def test_consumption_coverage_reports_lookup_and_lock_gaps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            covered_recipe = {
                "recipe_id": "recipe_big_text_gate_v1",
                "version": 1,
                "recipe_hash": "hash_big_text_coverage",
                "title": "Big Text Behind Presenter Gate",
                "use_when": ["large words behind presenter"],
                "steps": ["use presenter cutout and matte before motion"],
                "verification": ["review still before motion"],
                "forbidden_path": ["do not claim complete subtitle workflow"],
                "cannot_claim": ["cannot claim visual quality passed"],
            }
            uncovered_recipe = {
                "recipe_id": "recipe_uncovered_audio_v1",
                "version": 1,
                "recipe_hash": "hash_uncovered_audio",
                "title": "Audio Ducking Gate",
                "use_when": ["balance narration with background music"],
                "steps": ["lower BGM under speech"],
                "verification": ["listen to voice intelligibility"],
                "cannot_claim": ["cannot claim final mix quality passed"],
            }
            (recipes_dir / "recipe_big_text_gate_v1.json").write_text(
                json.dumps(covered_recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_uncovered_audio_v1.json").write_text(
                json.dumps(uncovered_recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "positive_big_text_gate",
                                "query": "big text behind presenter cutout matte",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_big_text_gate_v1",
                                "task": "coverage positive",
                            },
                            {
                                "case_id": "negative_no_subtitle_lock",
                                "query": "subtitle rhythm lower third line breaks",
                                "expect_applicable": False,
                                "overreach_recipe_id": "recipe_big_text_gate_v1",
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json")
            run_cli(project, "lock-pressure", "--cases", "lookup_pressure_cases.json")
            result = run_cli(project, "consumption-coverage", expect_ok=False)

            self.assertEqual(result["action"], "consumption-coverage")
            self.assertFalse(result["ok"])
            self.assertEqual(result["summary"]["recipe_count"], 2)
            self.assertEqual(result["summary"]["lookup_passed_covered"], 1)
            self.assertEqual(result["summary"]["lock_passed_covered"], 1)
            self.assertIn("recipe_uncovered_audio_v1", result["missing_lookup_recipe_ids"])
            self.assertIn("recipe_uncovered_audio_v1", result["missing_lock_recipe_ids"])
            covered_row = next(row for row in result["recipes"] if row["recipe_id"] == "recipe_big_text_gate_v1")
            uncovered_row = next(row for row in result["recipes"] if row["recipe_id"] == "recipe_uncovered_audio_v1")
            self.assertTrue(covered_row["lookup_passed"])
            self.assertTrue(covered_row["lock_passed"])
            self.assertFalse(uncovered_row["lookup_passed"])
            self.assertFalse(uncovered_row["lock_passed"])
            self.assertIn("不能说 consumption coverage 通过就证明 recipe 已执行。", result["claim_status"]["cannot_claim"])

    def test_consumption_coverage_ignores_external_simulation_pressure_reports(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_simulation_sensitive_v1",
                "version": 1,
                "recipe_hash": "hash_simulation_sensitive",
                "title": "Simulation Sensitive Gate",
                "use_when": ["simulation sensitive pressure"],
                "steps": ["require project-local pressure cases"],
                "verification": ["coverage must ignore external temp cases"],
                "cannot_claim": ["cannot claim temp what-if is official pressure"],
            }
            (recipes_dir / "recipe_simulation_sensitive_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_doc = {
                "cases": [
                    {
                        "case_id": "positive_simulation_sensitive",
                        "query": "simulation sensitive pressure project local",
                        "expect_lock": True,
                        "expected_recipe_id": "recipe_simulation_sensitive_v1",
                        "task": "simulation pressure should not count",
                    }
                ]
            }
            (project / "lock_pressure_cases.json").write_text(
                json.dumps(cases_doc, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            external_cases = Path(tmp) / "outside_lock_pressure_cases.json"
            external_cases.write_text(json.dumps(cases_doc, ensure_ascii=False, indent=2), encoding="utf-8")

            simulation = run_cli(project, "lock-pressure", "--cases", str(external_cases))
            result = run_cli(project, "consumption-coverage", expect_ok=False)

            self.assertFalse(simulation["official_pressure_evidence"])
            self.assertEqual(simulation["pressure_evidence_scope"], "external_cases")
            row = next(item for item in result["recipes"] if item["recipe_id"] == "recipe_simulation_sensitive_v1")
            self.assertFalse(row["lookup_passed"])
            self.assertFalse(row["lock_passed"])
            self.assertIn("recipe_simulation_sensitive_v1", result["missing_lock_recipe_ids"])

    def test_consumption_coverage_counts_positive_lock_as_lookup_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_0c_artifact_gate_v1",
                "version": 1,
                "recipe_hash": "hash_0c_artifact_gate",
                "title": "0C Artifact Gate",
                "use_when": ["refactor artifact experience into reusable rule"],
                "steps": ["map artifact signal to narrow reusable gate"],
                "verification": ["lock before execution"],
                "cannot_claim": ["cannot claim artifact review quality passed"],
            }
            (recipes_dir / "recipe_0c_artifact_gate_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_lock_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "positive_0c_artifact_gate",
                                "query": "refactor artifact experience reusable rule gate",
                                "expect_lock": True,
                                "expected_recipe_id": "recipe_0c_artifact_gate_v1",
                                "task": "coverage from lock",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            run_cli(project, "lock-pressure", "--cases", "lookup_lock_cases.json")
            result = run_cli(project, "consumption-coverage")

            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["lookup_passed_covered"], 1)
            self.assertEqual(result["summary"]["lock_passed_covered"], 1)
            row = result["recipes"][0]
            self.assertTrue(row["lookup_passed"])
            self.assertTrue(row["lock_passed"])
            self.assertIn("lock_pressure_", " ".join(row["lookup_passed_reports"]))

    def test_consumption_coverage_can_scope_diagnostic_pool_to_case_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            covered_recipe = {
                "recipe_id": "recipe_type_color_contract_v1",
                "version": 1,
                "recipe_hash": "hash_type_color_contract",
                "title": "Type Color Contract",
                "use_when": ["type color muted green sage face first read"],
                "steps": ["check face first read and palette relation"],
                "verification": ["lock before execution"],
            }
            unrelated_recipe = {
                "recipe_id": "recipe_unrelated_sound_gate_v1",
                "version": 1,
                "recipe_hash": "hash_unrelated_sound",
                "title": "Unrelated Sound Gate",
                "use_when": ["BGM SFX narration rhythm"],
                "steps": ["listen before claiming final mix"],
                "verification": ["human listening review"],
            }
            (recipes_dir / "recipe_type_color_contract_v1.json").write_text(
                json.dumps(covered_recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_unrelated_sound_gate_v1.json").write_text(
                json.dumps(unrelated_recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_lock_cases_diagnostic.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "coverage_scope": {"mode": "case_targets"},
                        "cases": [
                            {
                                "case_id": "positive_type_color_contract",
                                "query": "type color muted green sage face first read palette relation",
                                "expect_applicable": True,
                                "expect_lock": True,
                                "expected_recipe_id": "recipe_type_color_contract_v1",
                                "task": "diagnostic scoped coverage",
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            run_cli(project, "lock-pressure", "--cases", "lookup_lock_cases_diagnostic.json")
            result = run_cli(project, "consumption-coverage")

            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["active_recipe_count"], 2)
            self.assertEqual(result["summary"]["recipe_count"], 1)
            self.assertEqual(result["coverage_scope"]["mode"], "scoped")
            self.assertEqual(result["coverage_recipe_ids"], ["recipe_type_color_contract_v1"])
            self.assertEqual(result["missing_lookup_recipe_ids"], [])
            self.assertEqual(result["missing_lock_recipe_ids"], [])
            self.assertIn("scoped coverage 不能代表项目全部 recipe 已覆盖。", result["claim_status"]["cannot_claim"])

    def test_consumption_coverage_counts_only_latest_suffix_version_in_duplicate_series(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            base_recipe = {
                "version": 1,
                "title": "SampleProject Sound BGM SFX Candidate Gate",
                "use_when": ["judge whether BGM/SFX material is only a candidate"],
                "steps": ["match sound material to script, emotion, rhythm, and cut point"],
                "verification": ["human listening review before claiming final audio quality"],
                "cannot_claim": ["cannot claim final audio mix quality passed"],
            }
            for recipe_id in ("recipe_sample_project_sound_bgm_sfx_candidate_gate_v1", "recipe_sample_project_sound_bgm_sfx_candidate_gate_v2"):
                recipe = {**base_recipe, "recipe_id": recipe_id, "recipe_hash": f"hash_{recipe_id}"}
                (recipes_dir / f"{recipe_id}.json").write_text(
                    json.dumps(recipe, ensure_ascii=False),
                    encoding="utf-8",
                )
            cases_path = project / "lock_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "positive_latest_sound_gate",
                                "query": "SampleProject BGM SFX 声音 候选规则",
                                "expect_lock": True,
                                "expected_recipe_id": "recipe_sample_project_sound_bgm_sfx_candidate_gate_v2",
                                "task": "coverage should require only the latest duplicate-series version",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            run_cli(project, "lock-pressure", "--cases", "lock_pressure_cases.json")
            cmd = [
                sys.executable,
                "-m",
                "agent_recipes.cli",
                "consumption-coverage",
                "--project",
                str(project),
                "--json",
            ]
            proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)

            self.assertEqual(proc.returncode, 0, proc.stdout or proc.stderr)
            result = json.loads(proc.stdout)
            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["recipe_count"], 1)
            self.assertEqual(result["summary"]["inactive_recipe_count"], 1)
            self.assertEqual(result["inactive_recipe_ids"], ["recipe_sample_project_sound_bgm_sfx_candidate_gate_v1"])
            self.assertEqual([row["recipe_id"] for row in result["recipes"]], ["recipe_sample_project_sound_bgm_sfx_candidate_gate_v2"])

    def test_real_pressure_summary_lists_coverage_gaps_without_reading_sources(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "main"
            run_cli(project, "init")
            tests_root = project / ".recipes_real_tests"
            covered = tests_root / "case_covered"
            gap = tests_root / "case_gap"
            run_cli(covered, "init")
            run_cli(gap, "init")
            for child, recipe_id, title in [
                (covered, "recipe_covered_gate_v1", "Covered Gate"),
                (gap, "recipe_gap_gate_v1", "Gap Gate"),
            ]:
                recipes_dir = child / ".recipes" / "recipes"
                recipes_dir.mkdir(parents=True, exist_ok=True)
                recipe = {
                    "recipe_id": recipe_id,
                    "version": 1,
                    "recipe_hash": f"hash_{recipe_id}",
                    "title": title,
                    "use_when": ["real pressure summary"],
                    "steps": ["lock before execution"],
                    "verification": ["coverage report only"],
                    "cannot_claim": ["cannot claim task quality passed"],
                }
                (recipes_dir / f"{recipe_id}.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
            cases_path = covered / "lock_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "positive_covered_gate",
                                "query": "real pressure summary lock before execution",
                                "expect_lock": True,
                                "expected_recipe_id": "recipe_covered_gate_v1",
                                "task": "summary covered",
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_cli(covered, "lock-pressure", "--cases", "lock_pressure_cases.json")
            run_cli(covered, "consumption-coverage")
            reports_dir = covered / ".recipes" / "reports"
            reports_dir.mkdir(parents=True, exist_ok=True)
            (reports_dir / "candidate_quality_passed.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "report_id": "candidate_quality_passed",
                        "checked_at": "2026-01-01T00:00:00Z",
                        "summary": {"passed": 1, "failed": 0},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (reports_dir / "candidate_quality_failed_later.json").write_text(
                json.dumps(
                    {
                        "ok": False,
                        "report_id": "candidate_quality_failed_later",
                        "checked_at": "2026-01-02T00:00:00Z",
                        "summary": {"passed": 0, "failed": 1},
                        "cases": [
                            {
                                "case_id": "candidate_quality_later_failure",
                                "status": "failed",
                                "failure_reasons": ["proposed value count below minimum: 0 / 1"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (reports_dir / "review_packet_latest.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "report_id": "review_packet_latest",
                        "checked_at": "2026-01-03T00:00:00Z",
                        "markdown_path": "/tmp/review_packet_latest.md",
                        "summary": {
                            "review_count": 1,
                            "bucket_counts": {"thin_candidate": 1},
                            "action_counts": {"reject_or_archive_until_more_evidence": 1},
                        },
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (reports_dir / "output_quality_saved.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "report_id": "output_quality_saved",
                        "checked_at": "2026-01-04T00:00:00Z",
                        "evidence_mode": "fresh_agent_saved_output",
                        "outputs_generated_by_benchmark": False,
                        "fresh_generation_in_this_run": False,
                        "summary": {"passed": 2, "failed": 0, "blocked": 0, "case_count": 2},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            gap_reports = gap / ".recipes" / "reports"
            gap_reports.mkdir(parents=True, exist_ok=True)
            (gap_reports / "lookup_pressure_shadow.json").write_text(
                json.dumps(
                    {
                        "ok": False,
                        "report_id": "lookup_pressure_shadow",
                        "checked_at": "2026-01-03T00:00:00Z",
                        "summary": {"passed": 0, "failed": 1, "duplicate_shadow_count": 1},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (gap_reports / "lookup_pressure_tmp_simulation.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "report_id": "lookup_pressure_tmp_simulation",
                        "checked_at": "2026-01-05T00:00:00Z",
                        "cases_path": str(Path(tmp) / "outside_lookup_pressure_cases.json"),
                        "summary": {"passed": 1, "failed": 0, "duplicate_shadow_count": 0},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (gap_reports / "duplicate_governance_shadow.json").write_text(
                json.dumps(
                    {
                        "ok": True,
                        "report_id": "duplicate_governance_shadow",
                        "checked_at": "2026-01-04T00:00:00Z",
                        "markdown_path": "/tmp/duplicate_governance_shadow.md",
                        "what_if_cases_path": "/tmp/duplicate_governance_shadow_what_if_cases.json",
                        "summary": {"human_governance_required": 1, "shadow_risk_count": 1},
                        "decision_matrix": [
                            {
                                "action": "merge_or_supersede",
                                "plain": "合并或废弃其中一条，留下一个主菜谱。",
                                "use_when": "两条菜谱讲的是同一件事。",
                                "tradeoff": "最干净，但风险最大。",
                                "can_auto_apply": False,
                                "requires_human_decision": True,
                                "post_decision_validation": ["重新跑 lookup-pressure。"],
                            },
                            {
                                "action": "add_explicit_priority_rule",
                                "plain": "保留两条菜谱，但写清楚谁优先。",
                                "use_when": "两条都还有价值。",
                                "tradeoff": "更保守，但要维护优先级。",
                                "can_auto_apply": False,
                                "requires_human_decision": True,
                                "post_decision_validation": ["重新跑 lock-pressure。"],
                            },
                        ],
                        "what_if_validation_cases": [
                            {
                                "candidate_recipe_ids": [
                                    "recipe_narrow_gate_v1",
                                    "recipe_broad_gate_v1",
                                ],
                                "can_run_before_decision": False,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "real-pressure-summary", "--projects-root", ".recipes_real_tests")

            self.assertEqual(result["action"], "real-pressure-summary")
            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["project_count"], 2)
            self.assertEqual(result["summary"]["ignored_simulation_report_count"], 1)
            self.assertEqual(result["summary"]["quality_warning_count"], 1)
            self.assertEqual(result["summary"]["manual_governance_required_count"], 1)
            self.assertEqual(result["summary"]["manual_governance_ready_count"], 0)
            self.assertEqual(result["summary"]["manual_governance_not_ready_count"], 1)
            self.assertEqual(result["summary"]["projects_with_consumption_coverage"], 1)
            self.assertEqual(result["summary"]["projects_with_output_quality"], 1)
            self.assertEqual(result["summary"]["projects_with_latest_output_quality_ok"], 1)
            by_name = {item["project_name"]: item for item in result["projects"]}
            self.assertTrue(by_name["case_covered"]["reports"]["consumption_coverage"]["latest_ok"])
            self.assertEqual(by_name["case_covered"]["reports"]["output_quality"]["latest_report_id"], "output_quality_saved")
            self.assertFalse(by_name["case_covered"]["reports"]["output_quality"]["latest_fresh_generation_in_this_run"])
            self.assertEqual(by_name["case_gap"]["reports"]["lookup_pressure"]["latest_report_id"], "lookup_pressure_shadow")
            self.assertEqual(by_name["case_gap"]["reports"]["lookup_pressure"]["ignored_simulation_count"], 1)
            self.assertEqual(by_name["case_gap"]["reports"]["duplicate_governance"]["latest_report_id"], "duplicate_governance_shadow")
            self.assertEqual(
                by_name["case_gap"]["reports"]["duplicate_governance"]["latest_markdown_path"],
                "/tmp/duplicate_governance_shadow.md",
            )
            self.assertEqual(
                by_name["case_gap"]["reports"]["duplicate_governance"]["latest_what_if_cases_path"],
                "/tmp/duplicate_governance_shadow_what_if_cases.json",
            )
            self.assertEqual(by_name["case_gap"]["reports"]["duplicate_governance"]["latest_summary"]["shadow_risk_count"], 1)
            self.assertEqual(result["manual_governance_items"][0]["project"], "case_gap")
            self.assertEqual(result["manual_governance_items"][0]["latest_report_id"], "duplicate_governance_shadow")
            self.assertEqual(
                result["manual_governance_items"][0]["latest_what_if_cases_path"],
                "/tmp/duplicate_governance_shadow_what_if_cases.json",
            )
            self.assertEqual(
                result["manual_governance_items"][0]["candidate_recipe_ids"],
                ["recipe_narrow_gate_v1", "recipe_broad_gate_v1"],
            )
            self.assertEqual(result["manual_governance_items"][0]["decision_options"][0]["action"], "merge_or_supersede")
            self.assertFalse(result["manual_governance_items"][0]["decision_options"][0]["can_auto_apply"])
            self.assertEqual(result["manual_governance_readiness"][0]["project"], "case_gap")
            self.assertFalse(result["manual_governance_readiness"][0]["ready_for_human_decision"])
            self.assertIn("missing governance option: split_broad_recipe_scope", result["manual_governance_readiness"][0]["missing_evidence"])
            self.assertIn("不能说 readiness=true 就代表 duplicate shadow 已修复。", result["manual_governance_readiness"][0]["cannot_claim"])
            gaps = {(gap["project"], gap["gap"]) for gap in result["pressure_gaps"]}
            self.assertIn(("case_gap", "missing_consumption_coverage"), gaps)
            self.assertIn(("case_gap", "duplicate_shadow_risk"), gaps)
            self.assertNotIn(("case_covered", "latest_candidate_quality_failed"), gaps)
            self.assertNotIn(("case_covered", "no_passed_candidate_quality_report"), gaps)
            warnings = {(warning["project"], warning["warning"]) for warning in result["quality_warnings"]}
            self.assertIn(("case_covered", "latest_candidate_quality_failed_but_prior_pass_exists"), warnings)
            warning = result["quality_warnings"][0]
            self.assertEqual(warning["latest_failure_reasons_sample"], ["proposed value count below minimum: 0 / 1"])
            self.assertEqual(warning["latest_review_packet_id"], "review_packet_latest")
            self.assertEqual(warning["latest_review_packet_markdown_path"], "/tmp/review_packet_latest.md")
            self.assertEqual(
                warning["latest_review_packet_summary"],
                {
                    "action_counts": {"reject_or_archive_until_more_evidence": 1},
                    "bucket_counts": {"thin_candidate": 1},
                    "review_count": 1,
                },
            )
            self.assertEqual(
                warning["latest_review_packet_plain"],
                "审核包里有 1 条候选；分层：thin_candidate=1；建议动作：reject_or_archive_until_more_evidence=1",
            )
            self.assertIn(
                "让菜谱重跑更窄的 refine/extract-cards/patch-draft，并用 candidate-quality 的 min_proposed_value_count 复测。",
                warning["next_pressure_actions"],
            )
            self.assertIn(
                "当前 thin_candidate 只能当失败证据；下一轮应缩小 source/path 范围或触发 deep-read-plan 补证据。",
                warning["next_pressure_actions"],
            )
            self.assertIn(
                "补完后必须复跑 candidate-quality-benchmark、review-packet 和 real-pressure-summary。",
                warning["next_pressure_actions"],
            )
            markdown_path = Path(result["markdown_path"])
            self.assertTrue(markdown_path.exists())
            markdown = markdown_path.read_text(encoding="utf-8")
            self.assertIn("真实压测总看板", markdown)
            self.assertIn("根因归并", markdown)
            self.assertIn("有真实输出质量裁判的项目：`1`", markdown)
            self.assertIn("最新真实输出质量裁判通过的项目：`1`", markdown)
            self.assertIn("质量警告", markdown)
            self.assertIn("不能只看曾经通过", markdown)
            self.assertIn("proposed value count below minimum: 0 / 1", markdown)
            self.assertIn("review_packet_latest", markdown)
            self.assertIn("/tmp/review_packet_latest.md", markdown)
            self.assertIn("审核包结论：审核包里有 1 条候选；分层：thin_candidate=1", markdown)
            self.assertIn("下一轮压测建议", markdown)
            self.assertIn("min_proposed_value_count", markdown)
            self.assertIn("thin_candidate 只能当失败证据", markdown)
            self.assertIn("人工治理入口", markdown)
            self.assertIn("治理准备度", markdown)
            self.assertIn("材料还缺", markdown)
            self.assertIn("missing governance option: split_broad_recipe_scope", markdown)
            self.assertIn("readiness=true 只代表材料齐，不代表问题已修", markdown)
            self.assertIn("/tmp/duplicate_governance_shadow_what_if_cases.json", markdown)
            self.assertIn("recipe_narrow_gate_v1", markdown)
            self.assertIn("merge_or_supersede", markdown)
            self.assertIn("add_explicit_priority_rule", markdown)
            self.assertIn("选后验收", markdown)
            self.assertIn("重新跑 lookup-pressure", markdown)
            self.assertIn("重新跑 lock-pressure", markdown)
            self.assertIn("已忽略临时模拟报告：`1`", markdown)
            self.assertIn("case_gap`：`4` 个缺口", markdown)
            self.assertIn("缺消费覆盖报告", markdown)
            self.assertIn("重复/遮挡风险", markdown)
            self.assertIn("duplicate_governance_shadow", markdown)
            self.assertIn("不能说 real-pressure-summary 通过就证明真实任务质量。", result["claim_status"]["cannot_claim"])

    def test_candidate_quality_benchmark_scores_pending_review_without_accepting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            source = fixture_dir / "twoj_quality.md"
            source.write_text(
                "\n\n".join(
                    [
                        "card_type: learning_atom_card\n"
                        "visual_check: accepted AI insert windows use circular real-presenter PIP\n"
                        "forbidden_path: rectangular or full-body PIP is rejected even if export technically succeeds\n"
                        "verified_path: review preview exported for human review only\n"
                        "cannot_claim: cannot say production-ready or quality passed",
                        "card_type: failure_card\n"
                        "failure_signal: agent treated review preview export as final quality pass\n"
                        "replacement_path: keep review preview as candidate until user review\n"
                        "cannot_claim: cannot say review preview proves final quality",
                    ]
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/twoj_quality.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "circular PIP rectangular rejected review preview not production ready",
                "--knowledge-need",
                "KN_TWOJ_PENDING_QUALITY",
                "--target-recipe",
                "recipe_twoj_pending_quality",
                "--candidate-fields",
                "visual_check,forbidden_path,verified_path,cannot_claim,failure_signals",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_twoj_pending_quality")
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_twoj_pending_quality.json").exists())

            cases_path = project / "candidate_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "twoj_pending_review_quality",
                                "review_id": drafted["review_id"],
                                "required_terms": ["circular", "PIP", "rectangular", "review preview", "source_trace"],
                                "required_proposed_terms": ["circular", "rectangular"],
                                "min_proposed_value_count": 4,
                                "forbidden_terms": ["tax filing", "payroll", "production-ready passed"],
                                "required_source_paths": ["twoj_quality.md"],
                                "min_card_count": 2,
                                "max_proposed_value_count": 12,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "candidate-quality-benchmark", "--cases", "candidate_quality_cases.json")

            self.assertEqual(result["action"], "candidate-quality-benchmark")
            self.assertTrue(result["ok"])
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["passed"], 1)
            self.assertEqual(result["summary"]["failed"], 0)
            case = result["cases"][0]
            self.assertEqual(case["review_id"], drafted["review_id"])
            self.assertTrue(case["plain_language_summary_present"])
            self.assertFalse(case["formal_recipe_exists"])
            self.assertEqual(case["missing_required_terms"], [])
            self.assertEqual(case["missing_required_proposed_terms"], [])
            self.assertEqual(case["matched_forbidden_terms"], [])
            self.assertTrue(case["all_cards_have_source_trace"])
            self.assertTrue(Path(result["report_path"]).exists())
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_twoj_pending_quality.json").exists())

    def test_candidate_quality_can_forbid_terms_only_in_proposed_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            seed_review_triage_item(
                project,
                review_id="review_forbidden_proposed_only",
                target_recipe_id="recipe_forbidden_proposed_only",
                source_name="source_noise.md",
                proposed_value_count=2,
            )
            cards_path = project / ".recipes" / "source_refinery" / "cards" / "cards.jsonl"
            cards = [json.loads(line) for line in cards_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            cards[0]["source_quote"] = "source context mentions 字幕配音, but proposed patch does not."
            cards_path.write_text("\n".join(json.dumps(card, ensure_ascii=False, sort_keys=True) for card in cards) + "\n", encoding="utf-8")
            card_file = project / ".recipes" / "source_refinery" / "cards" / "learning_atom_cards" / "card_review_forbidden_proposed_only.json"
            card_file.write_text(json.dumps(cards[0], ensure_ascii=False, indent=2), encoding="utf-8")

            cases_path = project / "candidate_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "source_noise_is_not_proposed_noise",
                                "review_id": "review_forbidden_proposed_only",
                                "required_proposed_terms": ["source_noise.md proposed 0"],
                                "forbidden_proposed_terms": ["字幕配音"],
                                "allow_missing_plain_summary": True,
                                "min_card_count": 1,
                                "min_proposed_value_count": 1,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "candidate-quality-benchmark", "--cases", "candidate_quality_cases.json")

            self.assertTrue(result["ok"])
            case = result["cases"][0]
            self.assertEqual(case["matched_forbidden_proposed_terms"], [])
            self.assertEqual(case["forbidden_proposed_terms"], ["字幕配音"])

    def test_candidate_quality_can_recheck_accepted_review_when_formal_recipe_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes"
            (recipes_dir / "review_queue").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "candidates").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "source_refinery" / "patch_drafts").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "source_refinery" / "cards").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "recipes").mkdir(parents=True, exist_ok=True)

            card = {
                "card_id": "card_accepted_boundary",
                "card_type": "learning_atom_card",
                "evidence_strength": "candidate",
                "source_trace": [{"path": "fixtures/accepted_boundary.md", "line_start": 1, "line_end": 3}],
                "cannot_claim": ["cannot say accepted review proves every future case"],
                "target_fields": ["forbidden_path"],
            }
            (recipes_dir / "source_refinery" / "cards" / "cards.jsonl").write_text(
                json.dumps(card, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )

            patch_draft = {
                "patch_draft_id": "patch_draft_accepted_boundary",
                "target_recipe_id": "recipe_accepted_boundary",
                "source_card_ids": ["card_accepted_boundary"],
            }
            (recipes_dir / "source_refinery" / "patch_drafts" / "patch_draft_accepted_boundary.json").write_text(
                json.dumps(patch_draft, ensure_ascii=False),
                encoding="utf-8",
            )

            candidate_patch = {
                "patch_id": "patch_accepted_boundary",
                "target_recipe_id": "recipe_accepted_boundary",
                "proposed_change": {"forbidden_path": ["do not write candidate rules directly"]},
            }
            (recipes_dir / "candidates" / "patch_accepted_boundary.json").write_text(
                json.dumps(candidate_patch, ensure_ascii=False),
                encoding="utf-8",
            )

            review = {
                "review_id": "review_accepted_boundary",
                "status": "accepted",
                "proposed_patch_id": "patch_accepted_boundary",
                "source_patch_draft_id": "patch_draft_accepted_boundary",
                "evidence_refs": ["card_accepted_boundary"],
                "plain_language_summary": {"what_this_is": "accepted boundary recipe"},
            }
            (recipes_dir / "review_queue" / "review_accepted_boundary.json").write_text(
                json.dumps(review, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipes" / "recipe_accepted_boundary.json").write_text(
                json.dumps({"recipe_id": "recipe_accepted_boundary"}, ensure_ascii=False),
                encoding="utf-8",
            )

            cases_path = project / "candidate_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "accepted_review_can_be_rechecked",
                                "review_id": "review_accepted_boundary",
                                "expected_review_status": "accepted",
                                "allow_formal_recipe_exists": True,
                                "required_terms": ["source_trace", "cannot_claim"],
                                "required_source_paths": ["accepted_boundary.md"],
                                "min_card_count": 1,
                                "min_proposed_value_count": 1,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "candidate-quality-benchmark", "--cases", "candidate_quality_cases.json")

            case = result["cases"][0]
            self.assertTrue(result["ok"])
            self.assertTrue(case["formal_recipe_exists"])
            self.assertTrue(case["allow_formal_recipe_exists"])
            self.assertEqual(case["review_status"], "accepted")

    def test_candidate_quality_can_explicitly_allow_legacy_accepted_review_without_plain_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes"
            (recipes_dir / "review_queue").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "candidates").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "source_refinery" / "patch_drafts").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "source_refinery" / "cards").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "recipes").mkdir(parents=True, exist_ok=True)
            card = {
                "card_id": "card_legacy_summary",
                "card_type": "learning_atom_card",
                "evidence_strength": "candidate",
                "source_trace": [{"path": "fixtures/legacy.md", "line_start": 1}],
                "cannot_claim": ["cannot say legacy accepted review is fully readable"],
                "target_fields": ["checklist_item"],
            }
            (recipes_dir / "source_refinery" / "cards" / "cards.jsonl").write_text(
                json.dumps(card, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            patch_draft = {
                "patch_draft_id": "patch_draft_legacy_summary",
                "target_recipe_id": "recipe_legacy_summary",
                "source_card_ids": ["card_legacy_summary"],
            }
            (recipes_dir / "source_refinery" / "patch_drafts" / "patch_draft_legacy_summary.json").write_text(
                json.dumps(patch_draft, ensure_ascii=False),
                encoding="utf-8",
            )
            candidate_patch = {
                "patch_id": "patch_legacy_summary",
                "target_recipe_id": "recipe_legacy_summary",
                "proposed_change": {"checklist_item": ["reviewable output before claim"]},
            }
            (recipes_dir / "candidates" / "patch_legacy_summary.json").write_text(
                json.dumps(candidate_patch, ensure_ascii=False),
                encoding="utf-8",
            )
            review = {
                "review_id": "review_legacy_summary",
                "status": "accepted",
                "proposed_patch_id": "patch_legacy_summary",
                "source_patch_draft_id": "patch_draft_legacy_summary",
                "evidence_refs": ["card_legacy_summary"],
            }
            (recipes_dir / "review_queue" / "review_legacy_summary.json").write_text(
                json.dumps(review, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipes" / "recipe_legacy_summary.json").write_text(
                json.dumps({"recipe_id": "recipe_legacy_summary"}, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "candidate_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "legacy_accepted_without_plain_summary",
                                "review_id": "review_legacy_summary",
                                "expected_review_status": "accepted",
                                "allow_formal_recipe_exists": True,
                                "allow_missing_plain_summary": True,
                                "required_proposed_terms": ["reviewable output"],
                                "min_proposed_value_count": 1,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "candidate-quality-benchmark", "--cases", "candidate_quality_cases.json")

            case = result["cases"][0]
            self.assertTrue(result["ok"])
            self.assertFalse(case["plain_language_summary_present"])
            self.assertTrue(case["allow_missing_plain_summary"])

    def test_candidate_quality_rejects_placeholder_cannot_claim_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes"
            (recipes_dir / "review_queue").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "candidates").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "source_refinery" / "patch_drafts").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "source_refinery" / "cards").mkdir(parents=True, exist_ok=True)
            card = {
                "card_id": "card_placeholder_limit",
                "card_type": "learning_atom_card",
                "evidence_strength": "candidate",
                "source_trace": [{"path": "fixtures/placeholder.md", "line_start": 1}],
                "cannot_claim": ["False"],
                "target_fields": ["checklist_item"],
            }
            (recipes_dir / "source_refinery" / "cards" / "cards.jsonl").write_text(
                json.dumps(card, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            patch_draft = {
                "patch_draft_id": "patch_draft_placeholder_limit",
                "target_recipe_id": "recipe_placeholder_limit",
                "source_card_ids": ["card_placeholder_limit"],
            }
            (recipes_dir / "source_refinery" / "patch_drafts" / "patch_draft_placeholder_limit.json").write_text(
                json.dumps(patch_draft, ensure_ascii=False),
                encoding="utf-8",
            )
            candidate_patch = {
                "patch_id": "patch_placeholder_limit",
                "target_recipe_id": "recipe_placeholder_limit",
                "proposed_change": {"checklist_item": ["reviewable output before claim"]},
            }
            (recipes_dir / "candidates" / "patch_placeholder_limit.json").write_text(
                json.dumps(candidate_patch, ensure_ascii=False),
                encoding="utf-8",
            )
            review = {
                "review_id": "review_placeholder_limit",
                "status": "pending",
                "proposed_patch_id": "patch_placeholder_limit",
                "source_patch_draft_id": "patch_draft_placeholder_limit",
                "evidence_refs": ["card_placeholder_limit"],
                "plain_language_summary": {"what_this_is": "candidate boundary rule"},
            }
            (recipes_dir / "review_queue" / "review_placeholder_limit.json").write_text(
                json.dumps(review, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "candidate_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "placeholder_cannot_claim_fails",
                                "review_id": "review_placeholder_limit",
                                "required_proposed_terms": ["reviewable output"],
                                "min_proposed_value_count": 1,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "candidate-quality-benchmark", "--cases", "candidate_quality_cases.json", expect_ok=False)

            case = result["cases"][0]
            self.assertFalse(result["ok"])
            self.assertFalse(case["all_cards_have_claim_limits"])
            self.assertEqual(case["invalid_claim_limit_card_ids"], ["card_placeholder_limit"])
            self.assertIn("not all cards have useful cannot_claim", case["failure_reasons"])

    def test_candidate_quality_benchmark_can_fail_on_thin_proposed_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            source = fixture_dir / "artifact_catalog.md"
            source.write_text(
                "\n\n".join(
                    [
                        "card_type: learning_atom_card\n"
                        "verified_path: artifact catalog must mention candidate packets, coverage matrices, experience indexes, and failure-to-experience maps\n"
                        "cannot_claim: cannot say theme pools saturated",
                        "card_type: learning_atom_card\n"
                        "forbidden_path: do not claim artifact catalog is production quality passed\n"
                        "cannot_claim: cannot say official skill ready",
                    ]
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/artifact_catalog.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "artifact catalog candidate packets coverage matrices experience indexes failure-to-experience maps",
                "--knowledge-need",
                "KN_ARTIFACT_CATALOG_THIN_PATCH",
                "--target-recipe",
                "recipe_artifact_catalog_thin_patch",
                "--candidate-fields",
                "verified_path,cannot_claim",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_artifact_catalog_thin_patch")

            cases_path = project / "candidate_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "thin_patch_should_fail",
                                "review_id": drafted["review_id"],
                                "required_terms": ["candidate packets", "source_trace", "cannot_claim"],
                                "required_proposed_terms": ["candidate packets", "coverage matrices", "experience indexes"],
                                "min_card_count": 2,
                                "min_proposed_value_count": 6,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "candidate-quality-benchmark", "--cases", "candidate_quality_cases.json", expect_ok=False)

            self.assertFalse(result["ok"])
            case = result["cases"][0]
            self.assertEqual(case["status"], "failed")
            self.assertIn("candidate packets", case["missing_required_proposed_terms"])
            self.assertTrue(any("proposed value count below minimum" in reason for reason in case["failure_reasons"]))

    def test_candidate_quality_respects_zero_review_hint_count(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes"
            (recipes_dir / "review_queue").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "candidates").mkdir(parents=True, exist_ok=True)
            (recipes_dir / "source_refinery" / "patch_drafts").mkdir(parents=True, exist_ok=True)
            cards_dir = recipes_dir / "source_refinery" / "cards"
            cards_dir.mkdir(parents=True, exist_ok=True)

            card = {
                "card_id": "card_zero_hint",
                "card_type": "learning_atom_card",
                "evidence_strength": "candidate",
                "source_trace": [{"path": "fixtures/course_inventory.md", "line_start": 1, "line_end": 3}],
                "cannot_claim": ["cannot say candidate became formal recipe"],
                "target_fields": ["checklist_item"],
            }
            (cards_dir / "cards.jsonl").write_text(json.dumps(card, ensure_ascii=False) + "\n", encoding="utf-8")

            patch_draft = {
                "patch_draft_id": "patch_draft_zero_hint",
                "target_recipe_id": "recipe_zero_hint",
                "source_card_ids": ["card_zero_hint"],
            }
            (recipes_dir / "source_refinery" / "patch_drafts" / "patch_draft_zero_hint.json").write_text(
                json.dumps(patch_draft, ensure_ascii=False),
                encoding="utf-8",
            )

            candidate_patch = {
                "patch_id": "patch_zero_hint",
                "target_recipe_id": "recipe_zero_hint",
                "proposed_change": {"checklist_item": ["one", "two", "three"]},
            }
            (recipes_dir / "candidates" / "patch_zero_hint.json").write_text(
                json.dumps(candidate_patch, ensure_ascii=False),
                encoding="utf-8",
            )

            review = {
                "review_id": "review_zero_hint",
                "status": "pending",
                "proposed_patch_id": "patch_zero_hint",
                "source_patch_draft_id": "patch_draft_zero_hint",
                "evidence_refs": ["card_zero_hint"],
                "review_hints": {"proposed_value_count": 0},
                "plain_language_summary": {"what_this_is": "candidate patch with an explicit zero count"},
            }
            (recipes_dir / "review_queue" / "review_zero_hint.json").write_text(
                json.dumps(review, ensure_ascii=False),
                encoding="utf-8",
            )

            cases_path = project / "candidate_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "zero_hint_must_stay_zero",
                                "review_id": "review_zero_hint",
                                "required_source_paths": ["course_inventory.md"],
                                "required_terms": ["source_trace", "cannot_claim"],
                                "min_proposed_value_count": 1,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "candidate-quality-benchmark", "--cases", "candidate_quality_cases.json", expect_ok=False)

            case = result["cases"][0]
            self.assertEqual(case["proposed_value_count"], 0)
            self.assertEqual(case["status"], "failed")
            self.assertTrue(any("proposed value count below minimum: 0 / 1" in reason for reason in case["failure_reasons"]))

    def test_strict_lookup_and_query_lock_fail_closed_on_weak_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_big_text_gate_v1",
                "version": 1,
                "recipe_hash": "hash_big_text",
                "title": "Big Text Behind Presenter Gate",
                "steps": ["large words should often live behind the presenter using clean matte"],
                "forbidden_path": ["do not claim caption timing coverage"],
                "cannot_claim": ["cannot claim complete post-production workflow coverage"],
                "verification": ["static still review before 2 second motion"],
            }
            (recipes_dir / "recipe_big_text_gate_v1.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")

            weak_lookup = run_cli(project, "lookup", "subtitle rhythm lower third", "--strict", expect_ok=False)
            self.assertEqual(weak_lookup["code"], "AR242")

            weak_lock = run_cli(
                project,
                "lock",
                "--recipe",
                "recipe_big_text_gate_v1",
                "--query",
                "subtitle rhythm lower third",
                expect_ok=False,
            )
            self.assertEqual(weak_lock["code"], "AR242")

            mcp_lookup = call_tool(
                "lookup",
                {"project": str(project), "query": "subtitle rhythm lower third", "strict": True},
            )
            self.assertEqual(mcp_lookup["tool"], "agent_recipes_lookup")
            self.assertEqual(mcp_lookup["code"], "AR242")

            mcp_lock = call_tool(
                "lock",
                {
                    "project": str(project),
                    "recipe_id": "recipe_big_text_gate_v1",
                    "query": "subtitle rhythm lower third",
                },
            )
            self.assertEqual(mcp_lock["tool"], "agent_recipes_lock")
            self.assertEqual(mcp_lock["code"], "AR242")

            strong_lock = run_cli(
                project,
                "lock",
                "--recipe",
                "recipe_big_text_gate_v1",
                "--query",
                "big text behind presenter matte 2 second motion",
            )
            self.assertEqual(strong_lock["lock"]["applicability"]["status"], "strong")
            self.assertEqual(strong_lock["lock"]["lookup_query"], "big text behind presenter matte 2 second motion")

    def test_mcp_lookup_and_lock_normalize_percent_like_min_score(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_big_text_gate_v1",
                "version": 1,
                "recipe_hash": "hash_big_text",
                "title": "Big Text Behind Presenter Gate",
                "steps": ["large words should often live behind the presenter using clean matte"],
                "forbidden_path": ["do not claim caption timing coverage"],
                "cannot_claim": ["cannot claim complete post-production workflow coverage"],
                "verification": ["static still review before 2 second motion"],
            }
            (recipes_dir / "recipe_big_text_gate_v1.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")

            mcp_lookup = call_tool(
                "lookup",
                {
                    "project": str(project),
                    "query": "big text behind presenter matte 2 second motion",
                    "strict": True,
                    "min_score": 80,
                },
            )

            self.assertTrue(mcp_lookup["ok"])
            self.assertEqual(mcp_lookup["recipe"]["recipe_id"], "recipe_big_text_gate_v1")
            self.assertEqual(mcp_lookup["applicability"]["status"], "strong")
            self.assertEqual(mcp_lookup["applicability"]["min_score"], 2)
            self.assertEqual(mcp_lookup["mcp_min_score_normalized"]["original"], 80)
            self.assertEqual(mcp_lookup["mcp_min_score_normalized"]["used"], 2)

            mcp_lock = call_tool(
                "lock",
                {
                    "project": str(project),
                    "recipe_id": "recipe_big_text_gate_v1",
                    "task": "mcp percent-like min-score smoke",
                    "query": "big text behind presenter matte 2 second motion",
                    "min_score": 80,
                },
            )

            self.assertTrue(mcp_lock["ok"])
            self.assertEqual(mcp_lock["lock"]["recipe_ids"], ["recipe_big_text_gate_v1"])
            self.assertEqual(mcp_lock["lock"]["applicability"]["min_score"], 2)
            self.assertEqual(mcp_lock["mcp_min_score_normalized"]["original"], 80)
            self.assertEqual(mcp_lock["mcp_min_score_normalized"]["used"], 2)

    def test_lookup_does_not_treat_forbidden_path_as_positive_applicability(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_learning_deconstruction_v1",
                "version": 1,
                "recipe_hash": "hash_learning_deconstruction",
                "title": "Learning Material Deep Deconstruction",
                "use_when": ["deeply deconstruct course material into candidate experience cards"],
                "steps": ["create learning_material_info_cards and p1_failure_to_experience_map"],
                "forbidden_path": ["no provider video", "do not make final export or public release"],
                "cannot_claim": ["cannot claim production quality passed"],
                "verification": ["candidate cards stop at review_queue"],
            }
            (recipes_dir / "recipe_learning_deconstruction_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            positive = run_cli(
                project,
                "lookup",
                "deep deconstruct course material learning_material_info_cards p1_failure_to_experience_map",
                "--strict",
            )
            self.assertEqual(positive["recipe"]["recipe_id"], "recipe_learning_deconstruction_v1")

            forbidden_only = run_cli(project, "lookup", "provider video final export public release", "--strict", expect_ok=False)
            self.assertEqual(forbidden_only["code"], "AR242")

    def test_lookup_can_find_explicit_guardrail_by_forbidden_trigger_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            ordinary_recipe = {
                "recipe_id": "recipe_learning_deconstruction_v1",
                "version": 1,
                "recipe_hash": "hash_learning_deconstruction",
                "title": "Learning Material Deep Deconstruction",
                "use_when": ["deeply deconstruct course material into candidate experience cards"],
                "steps": ["create learning_material_info_cards"],
                "forbidden_path": ["no provider video", "do not make final export or public release"],
                "verification": ["candidate cards stop at review_queue"],
            }
            guardrail_recipe = {
                "recipe_id": "recipe_seed003_exact_boundary_guardrail_v1",
                "version": 1,
                "recipe_hash": "hash_seed003_guardrail",
                "title": "SEED003 Exact Boundary Guardrail",
                "use_when": ["check SEED003 timing overclaim before accepting module-label rules"],
                "steps": ["treat sampled windows as bounded evidence only"],
                "forbidden_path": [
                    "do not write a pattern card yet",
                    "exact only means safest sampled range",
                    "cannot claim frame-accurate timing",
                    "exact 只能表示最窄安全范围，不能冒充逐帧剪辑表",
                ],
                "verification": ["lock before claiming exact boundary decisions"],
            }
            (recipes_dir / "recipe_learning_deconstruction_v1.json").write_text(
                json.dumps(ordinary_recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_seed003_exact_boundary_guardrail_v1.json").write_text(
                json.dumps(guardrail_recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            guardrail_lookup = run_cli(
                project,
                "lookup",
                "pattern card frame-accurate",
                "--strict",
            )
            self.assertEqual(
                guardrail_lookup["recipe"]["recipe_id"],
                "recipe_seed003_exact_boundary_guardrail_v1",
            )
            chinese_guardrail_lookup = run_cli(
                project,
                "lookup",
                "不要写 pattern card exact 只能表示 最窄安全范围 不能冒充逐帧剪辑表",
                "--strict",
            )
            self.assertEqual(
                chinese_guardrail_lookup["recipe"]["recipe_id"],
                "recipe_seed003_exact_boundary_guardrail_v1",
            )

            ordinary_forbidden_only = run_cli(
                project,
                "lookup",
                "provider video final export public release",
                "--strict",
                expect_ok=False,
            )
            self.assertEqual(ordinary_forbidden_only["code"], "AR242")

    def test_lookup_rejects_subtitle_ocr_asr_repair_when_recipe_only_mentions_boundary_ref(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                recipes_dir / "recipe_title_callout_infocard_candidate_v2.json",
                {
                    "recipe_id": "recipe_title_callout_infocard_candidate_v2",
                    "version": 2,
                    "recipe_hash": "hash_title_callout",
                    "title": "Title Callout Infocard Candidate",
                    "use_when": ["title callout infocard visual packaging"],
                    "steps": ["Reference: RCP_SampleProject_013_SUBTITLE_OCR_ASR_BOUNDARY"],
                },
            )

            result = run_cli(
                project,
                "lookup",
                "SampleProject 字幕 OCR ASR 识别失败 字幕断句 智能字幕 导入字幕 文本校对",
                "--strict",
                expect_ok=False,
            )

            self.assertEqual(result["code"], "AR242")

    def test_lookup_matches_chinese_layer_type_color_failure_signals(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            write_json(
                recipes_dir / "recipe_keyword_subtitle_animation_v1.json",
                {
                    "recipe_id": "recipe_keyword_subtitle_animation_v1",
                    "version": 1,
                    "recipe_hash": "hash_keyword_subtitle",
                    "title": "Keyword Subtitle Animation",
                    "use_when": ["subtitle keyword animation"],
                    "steps": ["animate subtitle keywords"],
                },
            )
            write_json(
                recipes_dir / "recipe_layer_type_color_failure_gate_v1.json",
                {
                    "recipe_id": "recipe_layer_type_color_failure_gate_v1",
                    "version": 1,
                    "recipe_hash": "hash_layer_failure",
                    "title": "Layer Type Color Failure Gate",
                    "use_when": ["layer type color failure gate"],
                    "steps": [
                        "reject_if: subtitle overlaps hands or face",
                        "hard_rejects: green tag reads as a button",
                        "hard_rejects: white word floats with no shadow/contrast reason",
                        "hard_rejects: type hierarchy is title + title + title with no secondary role",
                    ],
                },
            )

            result = run_cli(
                project,
                "lookup",
                "SampleProject 预览里字幕压到手，绿色小标签像网页按钮，白色字浮着没有阴影关系，还出现 title + title + title。",
                "--strict",
            )

            self.assertEqual(result["recipe"]["recipe_id"], "recipe_layer_type_color_failure_gate_v1")
            self.assertIn("字幕压到手", result["applicability"]["matched_terms"])
            self.assertIn("网页按钮", result["applicability"]["matched_terms"])

    def test_lookup_ignores_generic_output_claim_terms(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_course_evidence_gate_v1",
                "version": 1,
                "recipe_hash": "hash_course_evidence_gate",
                "title": "Course Evidence Gate",
                "use_when": ["course evidence is reference, not proof of skill"],
                "steps": [
                    "Failure ledger must name the exact failed step, not vague provider doubt.",
                    "Only real generated video clips or accepted fallback visuals can be timeline assets.",
                    "Do not treat production ready claims as user-visible quality proof.",
                ],
                "verification": ["stop at review_queue before final quality claims"],
            }
            (recipes_dir / "recipe_course_evidence_gate_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            weak = run_cli(project, "lookup", "provider generate final video production ready", "--strict", expect_ok=False)

            self.assertEqual(weak["code"], "AR242")

    def test_lookup_matches_chinese_task_terms_to_english_recipe_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_sample_project_postprod_big_text_behind_presenter_gate_v1",
                "version": 1,
                "recipe_hash": "hash_big_text_cn",
                "title": "SampleProject Postprod Big Text Behind Presenter Gate",
                "use_when": ["SampleProject Postprod Big Text Behind Presenter Gate"],
                "steps": ["Core repair: large words should often live behind the presenter, using presenter cutout / matte."],
                "verification": ["static still review before 2 second motion"],
                "forbidden_path": ["Remotion/HyperFrames are prototype only before cutout gate"],
                "cannot_claim": ["cannot claim complete post-production workflow coverage"],
            }
            (recipes_dir / "recipe_sample_project_postprod_big_text_behind_presenter_gate_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            positive = run_cli(
                project,
                "lookup",
                "SampleProject 后期包装 大字 主播分层 字在人后 抠像 matte 2秒动效",
                "--strict",
            )
            self.assertEqual(positive["recipe"]["recipe_id"], "recipe_sample_project_postprod_big_text_behind_presenter_gate_v1")
            self.assertEqual(positive["applicability"]["status"], "strong")
            self.assertIn("大字", positive["applicability"]["matched_terms"])
            self.assertIn("字在人后", positive["applicability"]["matched_terms"])

            too_broad = run_cli(
                project,
                "lookup",
                "SampleProject 完整后期包装流程 大字 花字 字幕 专场 特效 转场 声音 全流程质量通过",
                "--strict",
                expect_ok=False,
            )
            self.assertEqual(too_broad["code"], "AR242")

    def test_lookup_prefers_keyframe_reject_gate_over_subtitle_animation_for_failure_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            keyframe_recipe = {
                "recipe_id": "recipe_sample_project_keyframe_motion_reject_gate_v1",
                "version": 1,
                "recipe_hash": "hash_keyframe_motion_reject",
                "title": "SampleProject Keyframe Motion Reject Gate",
                "use_when": ["SampleProject Keyframe Motion Reject Gate"],
                "steps": [
                    "No animation before still-frame review passes.",
                    "motion hides edge/matte defects",
                ],
                "failure_signals": [
                    "motion_rejects: any layer shifts enough to look like website animation",
                    "motion_rejects: back word moves in front of presenter",
                ],
                "forbidden_path": ["motion_rejects means do not treat animation as valid progress"],
                "verification": ["lock before accepting keyframe motion decisions"],
            }
            subtitle_recipe = {
                "recipe_id": "recipe_sample_project_keyword_subtitle_animation_candidate_v4",
                "version": 4,
                "recipe_hash": "hash_keyword_subtitle",
                "title": "SampleProject Keyword Subtitle Animation Candidate",
                "use_when": ["SampleProject Keyword Subtitle Animation Candidate"],
                "steps": [
                    "在关键词出现的位置添加关键帧。",
                    "0.5 秒后再次添加关键帧。",
                    "给关键词字幕添加动画效果。",
                ],
                "verification": ["candidate cards stop at review_queue"],
            }
            (recipes_dir / "recipe_sample_project_keyframe_motion_reject_gate_v1.json").write_text(
                json.dumps(keyframe_recipe, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_sample_project_keyword_subtitle_animation_candidate_v4.json").write_text(
                json.dumps(subtitle_recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            result = run_cli(
                project,
                "lookup",
                "SampleProject 关键帧 动画 2秒 motion_rejects still_frame_layer_map 动效节奏",
                "--strict",
            )

            self.assertEqual(result["recipe"]["recipe_id"], "recipe_sample_project_keyframe_motion_reject_gate_v1")
            self.assertIn("关键帧", result["applicability"]["matched_terms"])
            self.assertIn("motion_rejects", result["applicability"]["matched_terms"])
            self.assertNotIn("2", result["applicability"]["matched_terms"])
            self.assertNotIn("秒", result["applicability"]["matched_terms"])

    def test_lookup_matches_chinese_punctuated_sound_packaging_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_sample_project_sound_bgm_sfx_candidate_gate_v1",
                "version": 1,
                "recipe_hash": "hash_sound_bgm_sfx_cn",
                "title": "SampleProject Sound BGM SFX Candidate Gate",
                "use_when": ["SampleProject sound packaging BGM SFX candidate gate"],
                "steps": [
                    "BGM 要先看文案、内容和情绪，再选。",
                    "SFX 只给关键词、动作、切点、情绪转折服务。",
                    "音效有三个工作：补场景、补情绪、补节奏；没有工作就删。",
                ],
                "verification": ["人耳审核真实 SampleProject 输出。"],
                "forbidden_path": ["不能把候选经验直接写成 learned/pass/official/use-now。"],
                "cannot_claim": ["不能说声音包装质量已通过。"],
            }
            (recipes_dir / "recipe_sample_project_sound_bgm_sfx_candidate_gate_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            result = run_cli(
                project,
                "lookup",
                "SampleProject 声音包装候选规则：BGM 不抢人声，SFX 补场景补情绪补节奏，没有工作就删",
                "--strict",
            )

            self.assertEqual(result["recipe"]["recipe_id"], "recipe_sample_project_sound_bgm_sfx_candidate_gate_v1")
            self.assertEqual(result["applicability"]["status"], "strong")
            self.assertIn("bgm", result["applicability"]["matched_terms"])
            self.assertIn("sfx", result["applicability"]["matched_terms"])
            self.assertIn("补情绪", result["applicability"]["matched_terms"])

    def test_lookup_requires_enough_meaningful_query_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_video_frame_check_v1",
                "version": 1,
                "recipe_hash": "hash_video_frame_check",
                "title": "Video Frame Check",
                "use_when": ["check one video frame for visual layering"],
                "steps": ["inspect frame source trace and layer order"],
                "verification": ["review one frame before accepting candidate"],
            }
            (recipes_dir / "recipe_video_frame_check_v1.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            weak = run_cli(project, "lookup", "make final provider video export publish public release", "--strict", expect_ok=False)
            self.assertEqual(weak["code"], "AR242")

            strong = run_cli(project, "lookup", "video frame visual layering source trace", "--strict")
            self.assertEqual(strong["recipe"]["recipe_id"], "recipe_video_frame_check_v1")

    def test_lookup_ignores_generic_chinese_card_packaging_words(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            recipe = {
                "recipe_id": "recipe_sample_project_hook_card_local_fallback_guardrail_candidate_v3",
                "version": 1,
                "recipe_hash": "hash_hook_card",
                "title": "SampleProject hook card local fallback guardrail",
                "use_when": ["SampleProject 爆点卡片 hook_gate_benefit_not_visualized hook_visual_placeholder"],
                "steps": [
                    "当前没有可直接提升的候选素材，先用本地爆点卡片把现有利益点、痛点或标题关键词顶到第一帧，脚本文字保持锁定。",
                    "这只处理开头画面和文案承接，不代表整体包装好看。"
                ],
                "checklist_item": ["actions[0].primary_action: use_hook_benefit_card"],
                "failure_signals": ["actions[0].issue_id: hook_gate_benefit_not_visualized:shot_01"],
                "verification": ["重新 capture 并检查 claim_status。"],
            }
            (recipes_dir / "recipe_sample_project_hook_card_local_fallback_guardrail_candidate_v3.json").write_text(
                json.dumps(recipe, ensure_ascii=False),
                encoding="utf-8",
            )

            generic = run_cli(project, "lookup", "SampleProject 卡片 包装 文案 画面 好看一点", "--strict", expect_ok=False)
            self.assertEqual(generic["code"], "AR242")

            specific = run_cli(
                project,
                "lookup",
                "SampleProject 爆点卡片 hook_gate_benefit_not_visualized hook_visual_placeholder 本地爆点卡片",
                "--strict",
            )
            self.assertEqual(
                specific["recipe"]["recipe_id"],
                "recipe_sample_project_hook_card_local_fallback_guardrail_candidate_v3",
            )

    def test_mcp_exposes_lookup_pressure(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_lookup_pressure", tool_names)

    def test_mcp_exposes_lock_pressure(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_lock_pressure", tool_names)

    def test_mcp_exposes_consumption_coverage(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_consumption_coverage", tool_names)

    def test_mcp_exposes_real_pressure_summary(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_real_pressure_summary", tool_names)

    def test_mcp_exposes_candidate_quality_benchmark(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_candidate_quality_benchmark", tool_names)

    def test_mcp_exposes_duplicate_governance(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_duplicate_governance", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipes_dir = project / ".recipes" / "recipes"
            recipes_dir.mkdir(parents=True, exist_ok=True)
            broad = {
                "recipe_id": "recipe_z_layer_type_color_visual_check_v1",
                "version": 1,
                "recipe_hash": "hash_broad_type_color",
                "title": "Layer Type Color Visual Check",
                "use_when": ["type color contract muted green sage"],
                "steps": ["Does the face stay the first read?"],
            }
            narrow = {
                "recipe_id": "recipe_a_narrow_type_color_contract_v1",
                "version": 1,
                "recipe_hash": "hash_narrow_type_color",
                "title": "Narrow Type Color Contract",
                "use_when": ["type color contract muted green sage"],
                "steps": ["Does the face stay the first read?"],
            }
            (recipes_dir / "recipe_z_layer_type_color_visual_check_v1.json").write_text(
                json.dumps(broad, ensure_ascii=False),
                encoding="utf-8",
            )
            (recipes_dir / "recipe_a_narrow_type_color_contract_v1.json").write_text(
                json.dumps(narrow, ensure_ascii=False),
                encoding="utf-8",
            )
            cases_path = project / "lookup_pressure_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "narrow_contract_shadowed",
                                "query": "type color contract muted green sage face first read",
                                "expect_applicable": True,
                                "expected_recipe_id": "recipe_a_narrow_type_color_contract_v1",
                                "required_terms": ["Does the face stay the first read?"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_cli(project, "lookup-pressure", "--cases", "lookup_pressure_cases.json", expect_ok=False)

            result = call_tool("agent_recipes_duplicate_governance", {"project": str(project)}, project=project)

            self.assertEqual(result["tool"], "agent_recipes_duplicate_governance")
            self.assertEqual(result["transport"], "mcp")
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["shadow_risk_count"], 1)
            self.assertEqual(result["risks"][0]["recommended_action"], "human_governance_required")

    def test_review_triage_classifies_pending_candidates_without_writing_recipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            seed_review_triage_item(
                project,
                review_id="review_too_broad",
                target_recipe_id="recipe_triage_layer_contract",
                source_name="layer_contract.json",
                proposed_value_count=52,
            )
            seed_review_triage_item(
                project,
                review_id="review_evidence",
                target_recipe_id="recipe_triage_source_trace",
                source_name="source_trace.json",
                proposed_value_count=5,
            )
            seed_review_triage_item(
                project,
                review_id="review_thin",
                target_recipe_id="recipe_triage_before_after",
                source_name="before_after_review_gate.json",
                proposed_value_count=1,
            )
            seed_review_triage_item(
                project,
                review_id="review_human",
                target_recipe_id="recipe_triage_keyframe_contract",
                source_name="keyframe_contract.json",
                proposed_value_count=10,
            )

            result = run_cli(project, "review-triage", "--max-values", "40", "--min-values", "2")

            self.assertEqual(result["action"], "review-triage")
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["review_count"], 4)
            self.assertEqual(result["summary"]["bucket_counts"]["too_broad"], 1)
            self.assertEqual(result["summary"]["bucket_counts"]["evidence_index_only"], 1)
            self.assertEqual(result["summary"]["bucket_counts"]["thin_candidate"], 1)
            self.assertEqual(result["summary"]["bucket_counts"]["human_review_candidate"], 1)
            by_review = {item["review_id"]: item for item in result["items"]}
            self.assertEqual(by_review["review_too_broad"]["recommended_action"], "split_or_regenerate_narrower")
            self.assertEqual(by_review["review_evidence"]["recommended_action"], "keep_as_evidence_index_or_reject_review")
            self.assertEqual(by_review["review_thin"]["recommended_action"], "reject_or_archive_until_more_evidence")
            self.assertEqual(by_review["review_human"]["recommended_action"], "human_review_required")
            self.assertFalse(list((project / ".recipes" / "recipes").glob("*.json")))
            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["summary"]["source_refinery"]["review_triage_reports"], 1)
            self.assertEqual(doctor["summary"]["source_refinery"]["review_triage_items"], 4)

    def test_review_triage_fails_closed_without_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            result = run_cli(project, "review-triage", expect_ok=False)

            self.assertEqual(result["code"], "AR452")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])

    def test_mcp_exposes_review_triage(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_review_triage", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            seed_review_triage_item(
                project,
                review_id="review_mcp_triage",
                target_recipe_id="recipe_mcp_triage",
                source_name="keyframe_contract.json",
                proposed_value_count=5,
            )

            result = call_tool(
                "agent_recipes_review_triage",
                {"project": str(project), "target_recipe_id": "recipe_mcp_triage"},
                project=project,
            )

            self.assertEqual(result["tool"], "agent_recipes_review_triage")
            self.assertEqual(result["transport"], "mcp")
            self.assertEqual(result["summary"]["review_count"], 1)
            self.assertEqual(result["items"][0]["triage_bucket"], "human_review_candidate")

    def test_review_packet_creates_human_readable_packet_without_writing_recipes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            seed_review_triage_item(
                project,
                review_id="review_packet_human",
                target_recipe_id="recipe_packet_keyframe_contract",
                source_name="keyframe_contract.json",
                proposed_value_count=8,
            )
            seed_review_triage_item(
                project,
                review_id="review_packet_thin",
                target_recipe_id="recipe_packet_before_after",
                source_name="before_after_review_gate.json",
                proposed_value_count=1,
            )

            result = run_cli(project, "review-packet", "--target-prefix", "recipe_packet_", "--max-values", "40")

            self.assertEqual(result["action"], "review-packet")
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["review_count"], 2)
            self.assertEqual(result["summary"]["bucket_counts"]["human_review_candidate"], 1)
            self.assertEqual(result["summary"]["bucket_counts"]["thin_candidate"], 1)
            self.assertTrue(result["markdown_path"].endswith(".md"))
            markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("一眼结论", markdown)
            self.assertIn("可以拿给人看", markdown)
            self.assertIn("太薄了", markdown)
            self.assertFalse(list((project / ".recipes" / "recipes").glob("*.json")))
            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["summary"]["source_refinery"]["review_packet_reports"], 1)
            self.assertEqual(doctor["summary"]["source_refinery"]["review_packet_items"], 2)

    def test_review_packet_uses_readable_candidate_title_in_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            seed_review_triage_item(
                project,
                review_id="review_packet_title",
                target_recipe_id="recipe_packet_title",
                source_name="pip_safe_margin.json",
                proposed_value_count=5,
                title="PIP safe margin review candidate",
            )

            result = run_cli(project, "review-packet", "--target-recipe", "recipe_packet_title", "--max-values", "40")

            self.assertEqual(result["items"][0]["display_title"], "PIP safe margin review candidate")
            markdown = Path(result["markdown_path"]).read_text(encoding="utf-8")
            self.assertIn("### 1. PIP safe margin review candidate (`recipe_packet_title`)", markdown)
            self.assertNotIn("### 1. recipe_packet_title\n", markdown)

    def test_review_packet_fails_closed_without_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            result = run_cli(project, "review-packet", expect_ok=False)

            self.assertEqual(result["code"], "AR456")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])

    def test_capabilities_explain_review_packet_and_triage_are_candidate_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            result = run_cli(project, "capabilities")

            tools = result["source_refinery_tools"]
            self.assertTrue(tools["review-packet"]["candidate_only"])
            self.assertFalse(tools["review-packet"]["can_write_formal_recipe"])
            self.assertFalse(tools["review-packet"]["can_accept_review"])
            self.assertIn("人能看的审核包", tools["review-packet"]["plain"])
            self.assertTrue(tools["review-triage"]["candidate_only"])
            self.assertIn("替代人工 review", " ".join(result["claim_status"]["cannot_claim"]))
            consumption = result["consumption_tools"]
            self.assertFalse(consumption["lock-pressure"]["can_execute_recipe"])
            self.assertTrue(consumption["lock-pressure"]["can_create_execution_lock"])
            self.assertIn("lock 不等于任务完成", consumption["lock-pressure"]["plain"])
            self.assertFalse(consumption["consumption-coverage"]["can_execute_recipe"])
            self.assertFalse(consumption["consumption-coverage"]["can_create_execution_lock"])
            self.assertIn("覆盖通过不等于任务执行", consumption["consumption-coverage"]["plain"])
            self.assertFalse(consumption["real-pressure-summary"]["can_execute_recipe"])
            self.assertFalse(consumption["real-pressure-summary"]["can_create_execution_lock"])
            self.assertIn("不能证明任务质量通过", consumption["real-pressure-summary"]["plain"])
            self.assertIn("Markdown", consumption["real-pressure-summary"]["plain"])
            self.assertFalse(consumption["duplicate-governance"]["can_execute_recipe"])
            self.assertFalse(consumption["duplicate-governance"]["can_create_execution_lock"])
            self.assertFalse(consumption["duplicate-governance"]["can_merge_or_supersede"])
            self.assertIn("重复", consumption["duplicate-governance"]["plain"])
            self.assertFalse(consumption["output-quality-benchmark"]["can_execute_recipe"])
            self.assertFalse(consumption["output-quality-benchmark"]["can_create_execution_lock"])
            self.assertFalse(consumption["output-quality-benchmark"]["can_launch_agent"])
            self.assertIn("只评分已经保存的 agent 原始输出", consumption["output-quality-benchmark"]["plain"])
            self.assertIn("真实任务或证明质量通过", " ".join(result["claim_status"]["cannot_claim"]))

    def test_mcp_exposes_review_packet(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_review_packet", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            seed_review_triage_item(
                project,
                review_id="review_mcp_packet",
                target_recipe_id="recipe_mcp_packet",
                source_name="keyframe_contract.json",
                proposed_value_count=5,
            )

            result = call_tool(
                "agent_recipes_review_packet",
                {"project": str(project), "target_recipe_id": "recipe_mcp_packet"},
                project=project,
            )

            self.assertEqual(result["tool"], "agent_recipes_review_packet")
            self.assertEqual(result["transport"], "mcp")
            self.assertEqual(result["summary"]["review_count"], 1)
            self.assertTrue(result["markdown_path"].endswith(".md"))

    def test_self_run_benchmark_requires_system_chain_to_review_queue(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            source = fixture_dir / "self_run_source.md"
            source.write_text(
                "\n\n".join(
                    [
                        "card_type: correction_card\n"
                        "Wrong Behavior: agent manually summarized source material and wrote a formal recipe directly\n"
                        "Correct Behavior: run scan search refine extract-cards patch-draft and stop at review_queue\n"
                        "Check: every card keeps source_trace, evidence_strength, target_fields, cannot_claim\n"
                        "cannot_claim: cannot say self-run benchmark proves production quality",
                        "card_type: learning_atom_card\n"
                        "lesson: archive_index_only chunks stay searchable but do not become recipe fields\n"
                        "checklist_item: review_queue must decide before any formal recipe appears",
                        "Do not write formal recipe directly without review.",
                    ]
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/self_run_source.md", "--read-only")

            result = run_cli(
                project,
                "self-run-benchmark",
                "--query",
                "manual summary review_queue source_trace cannot_claim formal recipe",
                "--knowledge-need",
                "KN_SELF_RUN",
                "--target-recipe",
                "recipe_self_run_gate_v0",
                "--candidate-fields",
                "forbidden_path,checklist_item,cannot_claim",
                "--min-cards",
                "1",
            )

            self.assertEqual(result["action"], "self-run-benchmark")
            self.assertTrue(result["ok"])
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["failed"], 0)
            self.assertTrue(result["review_id"])
            self.assertTrue((project / ".recipes" / "review_queue" / f"{result['review_id']}.json").exists())
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_self_run_gate_v0.json").exists())
            case_status = {case["case_id"]: case["status"] for case in result["cases"]}
            self.assertEqual(case_status["patch_draft_has_candidate_values"], "passed")
            self.assertEqual(case_status["review_queue_pending"], "passed")
            self.assertEqual(case_status["no_direct_formal_recipe_write"], "passed")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{result['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions_text = "\n".join(value for values in patch_draft["proposed_additions"].values() for value in values)
            self.assertIn("Do not write formal recipe directly without review", additions_text)

    def test_repeat_error_benchmark_scores_ab_outputs_without_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cases_path = project / "repeat_error_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "cases": [
                            {
                                "case_id": "manual_summary_old_error",
                                "old_error": "agent bypassed review_queue",
                                "without_recipe_output": "I will manually summarize and write the final recipe now.",
                                "with_recipe_output": "I will run scan refine extract-cards patch-draft and stop at review_queue.",
                                "error_terms": ["manually summarize", "final recipe"],
                                "improvement_terms": ["review_queue", "patch-draft"],
                            },
                            {
                                "case_id": "visual_quality_overclaim",
                                "old_error": "agent claimed visual quality from a keyframe",
                                "without_recipe_output": "The keyframe proves visual quality is passed.",
                                "with_recipe_output": "The keyframe is candidate evidence and cannot_claim visual quality.",
                                "error_terms": ["proves visual quality", "passed"],
                                "improvement_terms": ["candidate", "cannot_claim"],
                            },
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(
                project,
                "repeat-error-benchmark",
                "--cases",
                "repeat_error_cases.json",
                "--min-cases",
                "2",
                "--min-improvements",
                "2",
            )

            self.assertEqual(result["action"], "repeat-error-benchmark")
            self.assertTrue(result["ok"])
            self.assertEqual(result["summary"]["improved"], 2)
            self.assertEqual(result["summary"]["failed"], 0)
            self.assertEqual(result["evidence_mode"], "provided_ab_outputs")
            self.assertFalse(result["ab_outputs_generated_by_benchmark"])
            self.assertFalse(result["fresh_generation_in_this_run"])
            self.assertIn("不能说 repeat-error-benchmark 本轮启动 fresh agent。", result["claim_status"]["cannot_claim"])
            self.assertIn(
                "repeat-error cases 缺 raw_evidence_paths；只能 claim 已评分提供的 A/B 文本，不能 claim fresh-agent 来源。",
                result["claim_status"]["missing_evidence"],
            )
            self.assertTrue(Path(result["report_path"]).exists())
            report = json.loads(Path(result["report_path"]).read_text(encoding="utf-8"))
            self.assertEqual(report["claim_status"], result["claim_status"])
            self.assertFalse(report["ab_outputs_generated_by_benchmark"])

    def test_output_quality_benchmark_scores_saved_agent_outputs_without_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cases_path = project / "output_quality_cases.json"
            cases_path.write_text(
                json.dumps(
                    {
                        "evidence_mode": "fresh_agent_saved_output",
                        "raw_evidence_paths": ["fresh_agent_raw_outputs.json"],
                        "cases": [
                            {
                                "case_id": "locked_failure_gate_output",
                                "output": {
                                    "lookup_recipe_id": "recipe_sample_project_layer_type_color_failure_gate_v1",
                                    "lock_id": "lock_layer_gate_123",
                                    "answer": "先按 locked recipe 判 reject：字幕压到手、绿色标签像网页按钮、白色字浮着，不能 claim final quality。",
                                },
                                "expected_recipe_id": "recipe_sample_project_layer_type_color_failure_gate_v1",
                                "expected_lock": True,
                                "required_terms": ["reject", "字幕压到手", "不能 claim"],
                                "required_any_terms": [["绿色标签", "网页按钮"]],
                                "forbidden_terms": ["可以发布", "质量通过"],
                            },
                            {
                                "case_id": "no_match_output",
                                "output": {
                                    "lookup_recipe_id": "no_applicable_recipe",
                                    "answer": "strict lookup 返回 AR242，no_applicable_recipe；这是全流程发布质量承诺，不能硬套单条窄菜谱。",
                                },
                                "expected_recipe_id": "no_applicable_recipe",
                                "expected_lock": False,
                                "required_terms": ["AR242", "no_applicable_recipe", "不能硬套"],
                                "forbidden_terms": ["lock_"],
                            },
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(
                project,
                "output-quality-benchmark",
                "--cases",
                "output_quality_cases.json",
                "--min-cases",
                "2",
                "--min-passed",
                "2",
            )

            self.assertEqual(result["action"], "output-quality-benchmark")
            self.assertTrue(result["ok"])
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["summary"]["passed"], 2)
            self.assertEqual(result["summary"]["failed"], 0)
            self.assertFalse(result["outputs_generated_by_benchmark"])
            self.assertFalse(result["fresh_generation_in_this_run"])
            self.assertEqual(result["evidence_mode"], "fresh_agent_saved_output")
            self.assertIn("不能说 output-quality-benchmark 本轮启动 fresh agent。", result["claim_status"]["cannot_claim"])
            self.assertTrue(Path(result["report_path"]).exists())
            self.assertFalse(list((project / ".recipes" / "recipes").glob("*.json")))

    def test_output_quality_benchmark_fails_closed_without_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cases_path = project / "output_quality_cases.json"
            cases_path.write_text(
                json.dumps({"cases": [{"case_id": "missing_output", "required_terms": ["review_queue"]}]}, ensure_ascii=False),
                encoding="utf-8",
            )

            result = run_cli(project, "output-quality-benchmark", "--cases", "output_quality_cases.json", expect_ok=False)

            self.assertEqual(result["action"], "output-quality-benchmark")
            self.assertFalse(result["ok"])
            self.assertEqual(result["summary"]["blocked"], 1)
            self.assertIn("benchmark case not passed: missing_output", result["claim_status"]["missing_evidence"])
            self.assertIn(
                "output-quality cases 缺 raw_evidence_paths；只能 claim 已评分提供的输出文本，不能 claim fresh-agent 来源。",
                result["claim_status"]["missing_evidence"],
            )

    def test_mcp_exposes_stage23_benchmarks(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_self_run_benchmark", tool_names)
        self.assertIn("agent_recipes_repeat_error_benchmark", tool_names)
        self.assertIn("agent_recipes_output_quality_benchmark", tool_names)
