from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import (
    RecipesError,
    extract_json_array_strings,
    infer_candidate_fields,
    run_adapter_json,
    source_text_for_index,
    write_json,
)


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


def source_refinery_recipe(recipe_id: str, *, title: str = "Review Pressure Recipe", marker: str = "base") -> dict[str, Any]:
    return {
        "recipe_id": recipe_id,
        "title": title,
        "version": 0,
        "scope": "Review decision pressure fixture.",
        "use_when": [f"use when {marker}"],
        "do_not_use_when": ["when the review has not been accepted"],
        "inputs_required": ["task", "source_truth"],
        "steps": [f"{marker} step"],
        "checklist_item": [f"{marker} checklist"],
        "forbidden_path": [f"{marker} forbidden path"],
        "visual_check": [f"{marker} visual check"],
        "verification": ["doctor must report ok"],
        "source_truth_to_read": [f"{marker}_source"],
        "cannot_claim": [f"cannot say {marker} was field tested"],
    }


def seed_source_refinery_review(
    project: Path,
    *,
    review_id: str,
    patch_id: str,
    target_recipe_id: str,
    proposed_change: dict[str, Any],
    recommendation: str = "review",
    split_recommended: bool = False,
) -> None:
    candidates_dir = project / ".recipes" / "candidates"
    review_dir = project / ".recipes" / "review_queue"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    review_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        candidates_dir / f"{patch_id}.json",
        {
            "patch_id": patch_id,
            "patch_type": "source_refinery_patch_draft",
            "source_patch_draft_id": f"draft_{patch_id}",
            "source_card_ids": [f"card_{patch_id}"],
            "target_recipe_id": target_recipe_id,
            "proposed_change": proposed_change,
            "reason": "review decision pressure fixture",
            "evidence_refs": [f"card_{patch_id}"],
            "risk": "split_recommended" if split_recommended else "needs_review",
            "status": "pending_review",
        },
    )
    write_json(
        review_dir / f"{review_id}.json",
        {
            "review_id": review_id,
            "blocking_level": "P0",
            "question": f"是否接受 source_refinery patch draft：{target_recipe_id}",
            "why_user_must_decide": "接受后才会生成或修改正式 recipe version。",
            "options": ["accept", "reject", "supersede", "split"],
            "recommendation": recommendation,
            "review_hints": {
                "split_recommended": split_recommended,
                "split_reasons": ["fixture is too broad"] if split_recommended else [],
                "card_count": 45 if split_recommended else 3,
                "proposed_value_count": 70 if split_recommended else 5,
            },
            "evidence_refs": [f"card_{patch_id}"],
            "proposed_patch_id": patch_id,
            "source_patch_draft_id": f"draft_{patch_id}",
            "status": "pending",
            "decided_by": None,
            "decided_at": None,
        },
    )


class Phase2Test(unittest.TestCase):
    def test_capabilities_reports_optional_dependency_status_without_installing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli(Path(tmp), "capabilities")

            self.assertEqual(result["action"], "capabilities")
            self.assertIn("markitdown", result["optional_python_modules"])
            self.assertIn("whisperx", result["optional_python_modules"])
            self.assertIn("project_python_modules", result)
            self.assertIn("adapter_runtime", result)
            self.assertTrue(result["adapter_runtime"]["markitdown"]["candidate_only"])
            self.assertTrue(result["adapter_runtime"]["cognee"]["candidate_only"])
            self.assertIn("ffmpeg", result["local_binaries"])
            self.assertIn("不能说依赖可用就等于 adapter 已完成真实任务验收。", result["claim_status"]["cannot_claim"])

    @unittest.skipUnless((REPO_ROOT / ".venv" / "bin" / "python").exists(), "project .venv not available")
    def test_external_adapter_convert_doc_uses_project_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            (project / ".venv").symlink_to(REPO_ROOT / ".venv", target_is_directory=True)
            source = project / "fixtures" / "doc.md"
            source.parent.mkdir()
            source.write_text("# Adapter Fixture\n\nThis document should become Markdown.\n", encoding="utf-8")
            run_cli(project, "init")

            result = run_cli(project, "convert-doc", "--input", "fixtures/doc.md", "--adapter", "markitdown")

            self.assertEqual(result["action"], "convert-doc")
            self.assertEqual(result["adapter"], "markitdown")
            self.assertTrue(Path(result["markdown_path"]).exists())
            self.assertIn("不能说文档内容已被吸收进正式菜谱。", result["claim_status"]["cannot_claim"])

    def test_ocr_image_missing_input_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = run_cli(Path(tmp), "ocr-image", "--input", "fixtures/missing.png", expect_ok=False)

            self.assertEqual(result["code"], "AR303")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])
            self.assertFalse((Path(tmp) / ".recipes").exists())

    def test_adapter_timeout_returns_structured_error(self) -> None:
        with self.assertRaises(RecipesError) as raised:
            run_adapter_json(
                Path(sys.executable),
                "import time; time.sleep(2)",
                {},
                error_code="AR399",
                problem="adapter probe timeout",
                timeout=1,
            )

        self.assertEqual(raised.exception.code, "AR399")
        self.assertIn("timeout", raised.exception.cause)

    def test_search_returns_local_evidence_candidates_from_source_and_video_indexes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "fixtures" / "source.md"
            transcript = project / "fixtures" / "lesson.srt"
            source.parent.mkdir()
            source.write_text("PIP 小窗不能挡主动作，必须保留视觉证据。\n", encoding="utf-8")
            transcript.write_text(
                "1\n00:00:00,100 --> 00:00:00,500\nPIP 小窗要放在安全边距内。\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/source.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            run_cli(project, "ingest-video", "--transcript", "fixtures/lesson.srt")

            result = run_cli(project, "search", "PIP 小窗", "--limit", "5")

            self.assertEqual(result["action"], "search")
            self.assertGreaterEqual(len(result["results"]), 2)
            self.assertTrue(all(item["evidence_status"] == "candidate" for item in result["results"]))
            self.assertTrue({item["record_type"] for item in result["results"]} & {"source_chunk"})
            self.assertTrue({item["record_type"] for item in result["results"]} & {"video_chunk"})
            self.assertIn("不能说检索结果已经验证。", result["claim_status"]["cannot_claim"])

    def test_search_boosts_relevant_source_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "fixtures" / "RULES_PIP.md"
            source.parent.mkdir()
            source.write_text("圆形左上，不能矩形全身。必须保留真实主播小窗。\n", encoding="utf-8")
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/RULES_PIP.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")

            result = run_cli(project, "search", "PIP presenter window", "--limit", "3")

            self.assertEqual(result["action"], "search")
            self.assertTrue(any(item["path"].endswith("RULES_PIP.md") for item in result["results"]))
            self.assertTrue(all(item["evidence_status"] == "candidate" for item in result["results"]))

    def test_chinese_editing_course_terms_map_to_visual_candidate_fields(self) -> None:
        text = (
            "复制一个文字图层，删掉关键词以外内容；关键词出现时添加位置关键帧，"
            "0.5 秒后再次添加关键帧，再加阴影、调整不透明度和颜色区分。"
        )

        self.assertIn("visual_check", infer_candidate_fields(text))

    def test_refine_maps_chinese_keyword_subtitle_course_chunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "fixtures" / "chenchen16_video_014_script_segments.json"
            source.parent.mkdir()
            source.write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "start_s": 70,
                                "end_s": 148,
                                "text_excerpt": (
                                    "口播必备的网感字幕动画效果关键词半透明动画。复制文字图层，"
                                    "入场对齐口播，透明度降到50%，位置参数里的Y轴改成150和-150，"
                                    "添加位置关键帧、阴影和颜色区分。"
                                ),
                            },
                            {
                                "start_s": 148,
                                "end_s": 153,
                                "text_excerpt": "角色十个三字幕配音用哪个剪映的字幕配音你真的会用吗？",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/chenchen16_video_014_script_segments.json", "--read-only")
            run_cli(project, "scan", "--depth", "medium")

            result = run_cli(
                project,
                "refine",
                "--query",
                "关键词字幕 半透明动画 文字图层 关键帧 阴影 颜色区分",
                "--knowledge-need",
                "KN_CHINESE_KEYWORD_SUBTITLE",
                "--target-recipe",
                "recipe_chinese_keyword_subtitle",
                "--candidate-fields",
                "visual_check,checklist_item,forbidden_path,cannot_claim",
                "--kind",
                "source",
                "--source-path-contains",
                "chenchen16_video_014",
            )

            self.assertGreater(result["mapped_count"], 0)
            self.assertEqual(result["archive_index_only_count"], 0)

            extracted = run_cli(project, "extract-cards", "--refinement", result["refinement_id"])
            self.assertGreater(sum(extracted["card_counts"].values()), 0)

            draft = run_cli(project, "patch-draft", "--target-recipe", "recipe_chinese_keyword_subtitle")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{draft['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions = patch_draft["proposed_additions"]
            visual_values = "\n".join(additions["visual_check"])
            self.assertIn("关键词半透明动画", visual_values)
            self.assertIn("文字图层", visual_values)
            self.assertIn("位置关键帧", visual_values)
            self.assertIn("Y轴改成150和-150", visual_values)
            self.assertNotIn("字幕配音", visual_values)

    def test_structured_json_artifact_values_become_visual_candidate_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            director_plan = fixture_dir / "director_plan.json"
            timeline = fixture_dir / "timeline.json"
            director_plan.write_text(
                json.dumps(
                    {
                        "segments": [
                            {
                                "id": "seg_01",
                                "visual_strategy": "card_fullscreen",
                                "caption_strategy": {
                                    "mode": "big_subtitle",
                                    "emphasis_words": ["别"],
                                },
                                "material_request": {
                                    "kind": "card",
                                    "fallback_kind": "template_card",
                                },
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            timeline.write_text(
                json.dumps(
                    {
                        "shots": [
                            {
                                "role": "keyword_card",
                                "title": "强字卡开场",
                            }
                        ],
                        "tracks": [
                            {
                                "label": "主画面",
                                "layer": 10,
                                "clips": [
                                    {
                                        "role": "keyword_card",
                                        "role_label": "重点字卡",
                                        "source_kind": "template_card",
                                        "source_path": "assets/templates/shot_01-jjip_keyword_card_v1.png",
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/director_plan.json", "--read-only")
            run_cli(project, "sources", "add", "fixtures/timeline.json", "--read-only")
            run_cli(project, "scan", "--depth", "medium")

            refined = run_cli(
                project,
                "refine",
                "--query",
                "强字卡 首帧 card_fullscreen big_subtitle keyword_card 重点字卡 template_card",
                "--knowledge-need",
                "KN_STRUCTURED_VISUAL_ARTIFACT",
                "--target-recipe",
                "recipe_structured_visual_artifact",
                "--candidate-fields",
                "visual_check,checklist_item,verified_path",
                "--kind",
                "source",
                "--limit",
                "10",
            )

            self.assertGreaterEqual(refined["mapped_count"], 2)
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_structured_visual_artifact")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            visual_values = "\n".join(patch_draft["proposed_additions"].get("visual_check", []))
            self.assertIn("visual_strategy: card_fullscreen", visual_values)
            self.assertIn("caption_strategy.mode: big_subtitle", visual_values)
            self.assertIn("role: keyword_card", visual_values)
            self.assertIn("role_label: 重点字卡", visual_values)
            self.assertIn("title: 强字卡开场", visual_values)

    def test_structured_json_artifact_ignores_machine_metadata_candidate_lines(self) -> None:
        indexed = source_text_for_index(
            Path("qa_repair_execution.json"),
            json.dumps(
                {
                    "schema_version": "jjip_qa_repair_execution_v1",
                    "source_qa_status": "review",
                    "summary": {
                        "requested_actions": 1,
                        "total_actions": 1,
                        "manual_actions": 0,
                    },
                    "execution_id": "qa_repair_execution:123",
                    "actions": [
                        {
                            "plan_id": "qa_repair_plan:abc",
                            "action_label": "画面支撑卡片",
                            "status": "blocked",
                            "blocked_reason": "没有需要切换的画面支撑素材。",
                            "issue_id": "self_media_missing_visual_support:shot_01",
                            "issue_key": "qa_report:self_media_missing_visual_support:shot_01",
                            "expected_gate_to_rerun": "qa_report_current, issue_registry_current",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        )

        candidate_lines = indexed.split("# Raw JSON", 1)[0]
        self.assertIn("actions[0].action_label: 画面支撑卡片", candidate_lines)
        self.assertIn("actions[0].issue_id: self_media_missing_visual_support:shot_01", candidate_lines)
        self.assertIn("actions[0].blocked_reason: 没有需要切换的画面支撑素材。", candidate_lines)
        self.assertNotIn("schema_version", candidate_lines)
        self.assertNotIn("source_qa_status", candidate_lines)
        self.assertNotIn("summary.requested_actions", candidate_lines)
        self.assertNotIn("summary.total_actions", candidate_lines)
        self.assertNotIn("summary.manual_actions", candidate_lines)
        self.assertNotIn("execution_id", candidate_lines)
        self.assertNotIn("plan_id", candidate_lines)
        self.assertNotIn("issue_key", candidate_lines)
        self.assertNotIn("expected_gate_to_rerun", candidate_lines)

    def test_refine_extract_cards_and_patch_draft_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source = project / "fixtures" / "refinery.md"
            source.parent.mkdir()
            source.write_text(
                "\n\n".join(
                    [
                        "card_type: correction_card\n"
                        "before: agent claimed export quality passed without evidence\n"
                        "correction: export success is not quality pass\n"
                        "after: require claim_status before final claim\n"
                        "cannot_claim: cannot say export quality passed without review",
                        "card_type: run_chain_card\n"
                        "inputs: transcript fixture\n"
                        "steps: scan then refine then extract cards\n"
                        "outputs: patch draft\n"
                        "verification: doctor ok\n"
                        "cannot_claim: cannot say all future runs are covered",
                        "card_type: failure_card\n"
                        "failed_path: using a summary as learned recipe\n"
                        "failure_signal: long summary with no recipe field\n"
                        "replacement_path: archive_index_only unless fieldized\n"
                        "cannot_claim: cannot say summary equals absorption",
                        "card_type: learning_atom_card\n"
                        "action_change: require source_trace on every card\n"
                        "checklist_item: verify source_trace exists\n"
                        "good_example: card has source id and line range\n"
                        "bad_example: card has no source\n"
                        "cannot_claim: cannot say card is verified recipe",
                        "card_type: visual_example_card\n"
                        "visual_check: PIP does not block main action\n"
                        "bad_example: old coordinates replace screenshot review\n"
                        "good_example: safe margin plus screenshot evidence\n"
                        "cannot_claim: cannot say visual quality passed",
                        "This chunk is useful background but has no target recipe field.",
                    ]
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/refinery.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")

            refined = run_cli(
                project,
                "refine",
                "--query",
                "card_type cannot_claim visual_check source_trace",
                "--knowledge-need",
                "KN_LOCAL_REFINERY",
                "--target-recipe",
                "recipe_refinery_fixture",
                "--candidate-fields",
                "verified_path,forbidden_path,failure_signal,checklist_item,visual_check,cannot_claim,pressure_test",
                "--limit",
                "10",
            )
            self.assertEqual(refined["action"], "refine")
            self.assertEqual(refined["archive_index_only_count"], 1)
            self.assertGreaterEqual(refined["mapped_count"], 5)
            self.assertIn("不能说 refine 输出已经进入正式 recipe。", refined["claim_status"]["cannot_claim"])

            extracted = run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            self.assertEqual(extracted["action"], "extract-cards")
            self.assertEqual(
                set(extracted["card_counts"]),
                {
                    "correction_card",
                    "run_chain_card",
                    "failure_card",
                    "learning_atom_card",
                    "visual_example_card",
                },
            )
            self.assertTrue(all(count >= 1 for count in extracted["card_counts"].values()))

            cards_dir = project / ".recipes" / "source_refinery" / "cards"
            card_paths = sorted(cards_dir.glob("*_cards/*.json"))
            self.assertGreaterEqual(len(card_paths), 5)
            for path in card_paths:
                card = json.loads(path.read_text(encoding="utf-8"))
                self.assertTrue(card["source_trace"])
                self.assertTrue(card["target_fields"])
                self.assertEqual(card["evidence_strength"], "candidate")
                self.assertTrue(card["cannot_claim"])

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_refinery_fixture")
            self.assertEqual(drafted["action"], "patch-draft")
            self.assertTrue((project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").exists())
            review_path = project / ".recipes" / "review_queue" / f"{drafted['review_id']}.json"
            self.assertTrue(review_path.exists())
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_refinery_fixture.json").exists())
            self.assertIn("不能说 patch draft 已经修改正式 recipe。", drafted["claim_status"]["cannot_claim"])
            review = json.loads(review_path.read_text(encoding="utf-8"))
            plain = review["plain_language_summary"]
            self.assertIn("source_refinery", plain["what_this_is"])
            self.assertEqual("recipe_refinery_fixture", plain["target_recipe_id"])
            self.assertGreaterEqual(plain["card_count"], 5)
            self.assertTrue(plain["fields_to_review"])
            self.assertTrue(plain["sample_changes"])
            self.assertIn("正式 recipe", plain["why_review"])
            self.assertIn("候选", plain["risk"])
            candidate_patch = json.loads(
                (project / ".recipes" / "candidates" / f"{review['proposed_patch_id']}.json").read_text(encoding="utf-8")
            )
            source_truth = "\n".join(candidate_patch["proposed_change"].get("source_truth_to_read", []))
            self.assertIn("fixtures/refinery.md", source_truth)
            self.assertIn("source_id=", source_truth)
            self.assertIn("card_id=", source_truth)

            accepted = run_cli(project, "review", "--accept", drafted["review_id"])
            self.assertEqual(accepted["recipe_id"], "recipe_refinery_fixture")
            self.assertTrue((project / ".recipes" / "recipes" / "recipe_refinery_fixture.json").exists())
            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["status"], "ok")
            self.assertGreaterEqual(doctor["summary"]["source_refinery"]["cards"], 5)
            self.assertIn(
                "不能说 source_refinery 候选卡片或 patch draft 已全部验证或吸收。",
                doctor["claim_status"]["cannot_claim"],
            )

    def test_real_run_receipt_json_extracts_run_chain_card(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "run_receipt.json"
            source.write_text(
                json.dumps(
                    {
                        "run_id": "run_001",
                        "status": "reviewable_timeline_created_no_export_pending_user_review",
                        "run": {
                            "timeline_name": "RUN001_REVIEWABLE_TIMELINE",
                            "export_attempted": False,
                        },
                        "local_artifacts": {
                            "timeline_report": {"path": "timeline_report.json"},
                            "asset_manifest": {"path": "asset_manifest.json"},
                            "run_receipt": {"path": "RUN_RECEIPT.md"},
                        },
                        "track_readback": {
                            "video_track_count": 3,
                            "audio_track_count": 1,
                            "V1": "presenter spine",
                            "A1": "original audio spine",
                        },
                        "success_means": [
                            "Timeline was created by script.",
                            "Presenter video and original audio are present as the spine.",
                        ],
                        "does_not_prove": [
                            "It does not prove user quality pass.",
                            "It does not prove final release readiness.",
                        ],
                        "cannot_claim": ["quality_pass", "final", "public"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/run_receipt.json", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")

            refined = run_cli(
                project,
                "refine",
                "--query",
                "run_id timeline track_readback success_means does_not_prove presenter original audio",
                "--knowledge-need",
                "KN_REAL_RUN_CHAIN",
                "--target-recipe",
                "recipe_real_run_chain_v0",
                "--candidate-fields",
                "verified_path,forbidden_path,failure_signal,checklist_item,visual_check,cannot_claim,pressure_test,source_trace",
                "--limit",
                "20",
            )
            self.assertGreaterEqual(refined["mapped_count"], 1)

            extracted = run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])

            self.assertGreaterEqual(extracted["card_counts"].get("run_chain_card", 0), 1)
            run_chain_cards = sorted((project / ".recipes" / "source_refinery" / "cards" / "run_chain_cards").glob("*.json"))
            self.assertTrue(run_chain_cards)
            card = json.loads(run_chain_cards[0].read_text(encoding="utf-8"))
            self.assertEqual(card["card_type"], "run_chain_card")
            self.assertIn("verified_path", card["target_fields"])
            self.assertTrue(card["source_trace"])
            self.assertIn("success_means", card["source_quote"])
            self.assertTrue(any("正式 recipe" in item for item in card["cannot_claim"]))

            cards_index = project / ".recipes" / "source_refinery" / "cards" / "cards.jsonl"
            stale_card = {
                "card_id": "card_stale_old_candidate",
                "card_type": "visual_example_card",
                "source_chunk_ids": ["old_refined_chunk"],
                "source_trace": [{"path": "old-source.md", "record_id": "old", "record_type": "source_chunk", "source_id": "old"}],
                "knowledge_need_id": "KN_OLD",
                "target_recipe_id": "recipe_real_run_chain_v0",
                "target_fields": ["visual_check"],
                "evidence_strength": "candidate",
                "extracted_payload": {"visual_check": ["stale old visual rule that should not leak into this draft"]},
                "source_quote": "stale old source",
                "cannot_claim": ["cannot claim stale old card is current"],
                "status": "candidate",
            }
            existing_cards = read_jsonl(cards_index)
            cards_index.write_text(
                "\n".join(json.dumps(item, ensure_ascii=False, sort_keys=True) for item in existing_cards + [stale_card]) + "\n",
                encoding="utf-8",
            )

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_real_run_chain_v0")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(encoding="utf-8")
            )
            additions = patch_draft["proposed_additions"]
            joined_steps = "\n".join(additions.get("verified_path", []) + additions.get("checklist_item", []))
            joined_additions = "\n".join(str(item) for values in additions.values() for item in values)
            self.assertIn("Timeline was created by script", joined_steps)
            self.assertIn("Presenter video and original audio", joined_steps)
            self.assertNotIn('"success_means"', joined_steps)
            self.assertNotIn("card_type:", joined_additions)
            self.assertNotIn("cannot_claim:", joined_additions)
            self.assertIn("does not prove user quality pass", "\n".join(additions.get("cannot_claim", [])))
            self.assertNotIn("stale old visual rule", "\n".join(additions.get("visual_check", [])))

            rejected = run_cli(project, "review", "--reject", drafted["review_id"], "--reason", "rules exist but the recipe is not clear enough")
            self.assertEqual(rejected["action"], "review reject")
            self.assertEqual(rejected["review"]["status"], "rejected")
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_real_run_chain_v0.json").exists())

    def test_markdown_method_outputs_become_candidate_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "method_outputs.md"
            source.write_text(
                "# Method Outputs\n\n"
                "1. `learning_material_info_cards.json`\n"
                "2. `learning_material_experience_index.json`\n"
                "3. `p1_failure_to_experience_map.json`\n"
                "4. `material_deep_deconstruction_notes.md`\n\n"
                "- no new visual\n"
                "- no motion render\n"
                "- no provider / XYQ / Grok / GPT Web/API call\n"
                "Do not mechanically deep-deconstruct every course.\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/method_outputs.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")

            result = run_cli(
                project,
                "self-run-benchmark",
                "--query",
                "learning_material_info_cards learning_material_experience_index p1_failure_to_experience_map no motion render provider",
                "--knowledge-need",
                "KN_METHOD_OUTPUTS",
                "--target-recipe",
                "recipe_method_outputs_v0",
                "--candidate-fields",
                "verified_path,forbidden_path,cannot_claim",
                "--min-cards",
                "2",
            )

            self.assertEqual(result["summary"]["failed"], 0)
            case_status = {case["case_id"]: case["status"] for case in result["cases"]}
            self.assertEqual(case_status["patch_draft_has_candidate_values"], "passed")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{result['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions_text = "\n".join(value for values in patch_draft["proposed_additions"].values() for value in values)
            self.assertIn("learning_material_info_cards.json", additions_text)
            self.assertIn("learning_material_experience_index.json", additions_text)
            self.assertIn("p1_failure_to_experience_map.json", additions_text)
            self.assertIn("no motion render", additions_text)

    def test_markdown_table_rows_become_candidate_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "seed003_table.md"
            source.write_text(
                "# A0-HD-SEED-003 Orange Module Sequence Map\n\n"
                "| field | value |\n"
                "| --- | --- |\n"
                "| seed id | A0-HD-SEED-003 |\n"
                "| observed phenomenon | The sequence uses numbered module markers to make a long tutorial feel chunked and trackable. |\n"
                "| possible project use | For multi-step explanation, number each major section so the viewer understands where they are in the route. |\n"
                "| concrete candidate action | Use a small recurring number marker plus short module label for each main step. |\n"
                "| strongest evidence | Modules 1-5 have sampled evidence; Module 2 transition is bounded between 140.5s and 143.0s. |\n"
                "| blocked use | Do not claim the report's bumper, animation timing, or production quality. |\n"
                "| evidence gap | No animation timing readback, no full sequence map, no SampleProject sample test. |\n"
                "| hard blockers | Exact starts/ends still incomplete; most transition timing unmapped. |\n"
                "| status | candidate seed only |\n\n"
                "| seed | current candidate value | strongest evidence | hard blockers | decision |\n"
                "| --- | --- | --- | --- | --- |\n"
                "| SEED-003 orange numbered module system | Medium-high value for long tutorial orientation and section navigation. | Modules 1-5 have sampled evidence; Module 2 transition is bounded between 140.5s and 143.0s. | Exact starts/ends still incomplete; most transition timing unmapped. | keep_candidate_pause_before_pattern_card. |\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/seed003_table.md", "--read-only")
            run_cli(project, "scan", "--depth", "medium")

            result = run_cli(
                project,
                "self-run-benchmark",
                "--query",
                "seed003 orange numbered module system Module 2 transition candidate seed only",
                "--knowledge-need",
                "KN_SEED003_TABLE",
                "--target-recipe",
                "recipe_seed003_table",
                "--candidate-fields",
                "steps,forbidden_path,cannot_claim,failure_signal,verified_path,visual_check",
                "--min-cards",
                "1",
                "--limit",
                "10",
            )

            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{result['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions_text = "\n".join(value for values in patch_draft["proposed_additions"].values() for value in values)
            self.assertIn("numbered module markers", additions_text)
            self.assertIn("number each major section", additions_text)
            self.assertIn("Use a small recurring number marker", additions_text)
            self.assertIn("Do not claim the report's bumper", additions_text)
            self.assertIn("No animation timing readback", additions_text)
            self.assertIn("candidate seed only", additions_text)
            self.assertIn("Modules 1-5 have sampled evidence", additions_text)
            self.assertIn("Module 2 transition", additions_text)
            self.assertIn("Exact starts/ends still incomplete", additions_text)
            self.assertNotIn("| field |", additions_text)

    def test_refinery_extracts_human_wrong_correct_check_correction_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "human_correction.md"
            source.write_text(
                "# User Corrections\n\n"
                "## Card 001 - Reference Images Are Not Timeline Assets\n\n"
                "Wrong Behavior:\n"
                "- - Put first-frame, last-frame, or keyframe images directly on the timeline and call them AI video.\n\n"
                "Correct Behavior:\n"
                "- Treat first-frame and last-frame images as production intermediates.\n"
                "- Only real generated video clips, real footage, or accepted fallback visuals can be timeline assets.\n\n"
                "Applies To:\n"
                "- Provider checks.\n"
                "- Timeline assembly.\n\n"
                "Check:\n"
                "- Before timeline import, each clip must state whether it is real video, real footage, accepted fallback, or rejected intermediate.\n\n"
                "Status:\n"
                "- ACTIVE.\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/human_correction.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")

            refined = run_cli(
                project,
                "refine",
                "--query",
                "Wrong Behavior Correct Behavior Check timeline assets generated video",
                "--knowledge-need",
                "KN_HUMAN_CORRECTION_CARD",
                "--target-recipe",
                "recipe_human_correction_card",
                "--candidate-fields",
                "forbidden_path,checklist_item,good_example,bad_example,cannot_claim",
                "--limit",
                "10",
            )
            self.assertGreaterEqual(refined["mapped_count"], 1)

            extracted = run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            self.assertGreaterEqual(extracted["card_counts"].get("correction_card", 0), 1)

            correction_cards = sorted((project / ".recipes" / "source_refinery" / "cards" / "correction_cards").glob("*.json"))
            self.assertTrue(correction_cards)
            card = json.loads(correction_cards[0].read_text(encoding="utf-8"))
            self.assertEqual(card["card_type"], "correction_card")
            self.assertIn("wrong_behavior", card["extracted_payload"])
            self.assertIn("correct_behavior", card["extracted_payload"])
            self.assertIn("check", card["extracted_payload"])
            self.assertIn("Wrong Behavior", card["source_quote"])
            self.assertIn("Correct Behavior", card["source_quote"])
            self.assertIn("Check", card["source_quote"])

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_human_correction_card")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            proposed_recipe = json.loads(
                (project / ".recipes" / "candidates" / f"{drafted['patch_id']}.json").read_text(encoding="utf-8")
            )["proposed_change"]
            self.assertEqual(proposed_recipe["title"], "Reference Images Are Not Timeline Assets")
            additions = patch_draft["proposed_additions"]
            self.assertIn("Put first-frame", "\n".join(additions.get("forbidden_path", [])))
            self.assertTrue(all(not item.startswith("- ") for item in additions.get("forbidden_path", [])))
            checklist = "\n".join(additions.get("checklist_item", []) + additions.get("good_example", []))
            self.assertIn("Treat first-frame", checklist)
            self.assertIn("Before timeline import", checklist)
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_human_correction_card.json").exists())

    def test_refinery_maps_knowledge_map_sections_without_machine_format_leakage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "knowledge_map_and_receipt.md"
            source.write_text(
                "### K003 DaVinci timeline landing\n\n"
                "current_gap:\n"
                "- DaVinci track, marker, timeline report, and screenshot readback are not stable enough.\n\n"
                "recurring_failure:\n"
                "- Claiming a complete timeline after only importing media.\n"
                "- Losing the V1 presenter or A1 original audio spine.\n\n"
                "must_read_sources:\n"
                "- `RCP_SampleProject_005_DAVINCI_MASTER_ROUGHCUT_TIMELINE.md`.\n"
                "- `USER_CORRECTIONS_LATEST.md` Card 004.\n\n"
                "expected_output:\n"
                "- timeline report schema.\n"
                "- screenshot/readback checklist.\n\n"
                "acceptance_check:\n"
                "- A future agent can distinguish skeleton, roughcut, reviewable timeline, and user quality pass.\n\n"
                "cannot_claim:\n"
                "- Cannot claim DaVinci automation is stable.\n\n"
                "| Source | Use | Cannot claim |\n"
                "| --- | --- | --- |\n"
                "| run005 | narrow evidence | quality pass |\n\n"
                "{\n"
                '  "viewer_should_understand": "This is raw machine metadata, not a recipe instruction.",\n'
                '  "success_means": ["Timeline was created by script."],\n'
                '  "does_not_prove": ["It does not prove user quality pass."]\n'
                "}\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/knowledge_map_and_receipt.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "DaVinci timeline report recurring failure must read success means does not prove",
                "--knowledge-need",
                "KN_KNOWLEDGE_MAP",
                "--target-recipe",
                "recipe_knowledge_map_fixture",
                "--candidate-fields",
                "verified_path,forbidden_path,failure_signal,checklist_item,cannot_claim,source_truth_to_read",
                "--limit",
                "10",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_knowledge_map_fixture")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions = patch_draft["proposed_additions"]
            self.assertIn("Claiming a complete timeline", "\n".join(additions.get("failure_signals", [])))
            self.assertIn("timeline report schema", "\n".join(additions.get("checklist_item", [])))
            self.assertIn("RCP_SampleProject_005", "\n".join(additions.get("source_truth_to_read", [])))
            joined = "\n".join(str(item) for values in additions.values() for item in values)
            self.assertNotIn('"viewer_should_understand"', joined)
            self.assertNotIn("| Source |", joined)
            self.assertNotIn('"success_means"', joined)

    def test_refinery_does_not_turn_metadata_labels_into_recipe_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "metadata_labels.md"
            source.write_text(
                "### Candidate run metadata\n\n"
                "Create the reviewable roughcut candidate only after timeline_report.json exists.\n\n"
                "timeline_report:\n"
                "- timeline_report.json exists.\n\n"
                "candidate_essence:\n"
                "- This label describes raw extraction metadata, not an executable recipe step.\n"
                "evidence_strength: proven / partial / candidate / unverified\n"
                "privacy_class: public_candidate / project_local / private_only\n\n"
                "A future agent can say which packaging work belongs to the reviewable roughcut candidate.\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/metadata_labels.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "timeline report candidate roughcut metadata",
                "--knowledge-need",
                "KN_METADATA_LABELS",
                "--target-recipe",
                "recipe_metadata_labels",
                "--candidate-fields",
                "verified_path,checklist_item,cannot_claim",
                "--limit",
                "10",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_metadata_labels")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            joined = "\n".join(str(item) for values in patch_draft["proposed_additions"].values() for item in values)
            self.assertIn("Create the reviewable roughcut candidate", joined)
            self.assertNotIn("candidate_essence:", joined)
            self.assertNotIn("evidence_strength:", joined)
            self.assertNotIn("privacy_class:", joined)

    def test_patch_draft_without_verified_path_does_not_promote_body_to_verified_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "checklist_only.md"
            source.write_text(
                "### Semantic group rules\n\n"
                "checklist_item:\n"
                "- Build a sentence table first.\n"
                "- Keep host_only and marker_for_human as valid decisions.\n\n"
                "forbidden_path:\n"
                "- Do not generate arbitrary fixed-length clips and force them into the spoken script.\n\n"
                "cannot_claim:\n"
                "- Cannot claim visual quality from a checklist-only recipe.\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/checklist_only.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "semantic group host_only marker_for_human checklist cannot claim",
                "--knowledge-need",
                "KN_CHECKLIST_ONLY",
                "--target-recipe",
                "recipe_checklist_only",
                "--candidate-fields",
                "checklist_item,forbidden_path,cannot_claim",
                "--limit",
                "10",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_checklist_only")
            accepted = run_cli(project, "review", "--accept", drafted["review_id"])
            recipe = json.loads(
                (project / ".recipes" / "recipes" / f"{accepted['recipe_id']}.json").read_text(encoding="utf-8")
            )
            self.assertEqual([], recipe.get("verified_path"))
            self.assertIn("Build a sentence table first", "\n".join(recipe.get("steps", [])))
            self.assertNotIn("checklist_item:", "\n".join(recipe.get("verified_path", [])))

    def test_verified_path_candidate_can_use_artifact_receipt_quote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "artifact_receipt.md"
            source.write_text(
                "## Artifact receipt\n\n"
                "- docs/verification/s2b_artifact_catalog/3A3B_ARTIFACT_TYPE_CATALOG.md\n"
                "- archive index and sync verification receipt are available for review.\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/artifact_receipt.md", "--read-only")
            run_cli(project, "scan", "--depth", "medium")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "artifact receipt archive index verification",
                "--knowledge-need",
                "KN_ARTIFACT_RECEIPT",
                "--target-recipe",
                "recipe_artifact_receipt",
                "--candidate-fields",
                "verified_path,cannot_claim",
                "--limit",
                "10",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_artifact_receipt")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions = "\n".join(patch_draft["proposed_additions"].get("verified_path", []))
            self.assertIn("3A3B_ARTIFACT_TYPE_CATALOG.md", additions)
            self.assertIn("sync verification receipt", additions)

    def test_verified_path_candidate_extracts_source_trace_json_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "source_trace.json"
            source.write_text(
                "{\n"
                "  \"schema_version\": \"source_trace.v1\",\n"
                "  \"verification\": \"/tmp/verification.json\",\n"
                "  \"contact_sheet\": \"/tmp/contact_sheet.jpg\",\n"
                "  \"receipt\": \"/tmp/promotion_readiness_receipt.json\",\n"
                "  \"allowed_next_use\": [\n"
                "    \"source/timecode/keyframe/hash-or-limitation backlinks must be preserved\"\n"
                "  ]\n"
                "}\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/source_trace.json", "--read-only")
            run_cli(project, "scan", "--depth", "medium")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "source trace verification contact sheet receipt",
                "--knowledge-need",
                "KN_SOURCE_TRACE",
                "--target-recipe",
                "recipe_source_trace",
                "--candidate-fields",
                "verified_path,cannot_claim",
                "--limit",
                "10",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_source_trace")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions = "\n".join(patch_draft["proposed_additions"].get("verified_path", []))
            self.assertIn("verification: /tmp/verification.json", additions)
            self.assertIn("contact_sheet: /tmp/contact_sheet.jpg", additions)
            self.assertIn("receipt: /tmp/promotion_readiness_receipt.json", additions)

    def test_recipe_title_prefers_descriptive_target_id_over_opaque_knowledge_heading(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "knowledge_heading.md"
            source.write_text(
                "### K001 口播理解和时间码\n\n"
                "checklist_item:\n"
                "- Build a sentence table first.\n"
                "- Merge sentences into semantic visual groups.\n",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/knowledge_heading.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "semantic visual group sentence table",
                "--knowledge-need",
                "KN_K001_TITLE",
                "--target-recipe",
                "recipe_semantic_visual_group_rules_v0",
                "--candidate-fields",
                "checklist_item",
                "--limit",
                "10",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_semantic_visual_group_rules_v0")
            proposed_recipe = json.loads(
                (project / ".recipes" / "candidates" / f"{drafted['patch_id']}.json").read_text(encoding="utf-8")
            )["proposed_change"]
            self.assertEqual("Semantic Visual Group Rules", proposed_recipe["title"])

    def test_refinery_maps_structured_json_contract_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            source = fixture_dir / "layer_contract.json"
            source.write_text(
                json.dumps(
                    {
                        "schema_version": "test.layer_contract.v1",
                        "z_order_bottom_to_top": [
                            "L00_background_plate",
                            "L10_back_typography",
                            "L20_presenter_clean_matte",
                        ],
                        "color_relation": "muted green/sage only; reject web button feeling",
                        "layers": [
                            {
                                "layer_id": "L10_back_typography",
                                "role": "large words behind presenter",
                                "requirements": [
                                    "behind presenter matte",
                                    "opacity 0.16 to 0.28 before user review",
                                ],
                                "reject_if": [
                                    "word sits in front of presenter by accident",
                                    "word looks like pasted sticker text",
                                ],
                            }
                        ],
                        "review_questions": [
                            "Does the face stay the first read?",
                            "Does the big word sit behind the presenter in layer order?",
                        ],
                        "pass_questions": [
                            "Is the after frame visibly stronger than the baseline?",
                        ],
                        "motion_rejects": [
                            "text pops as sticker decoration",
                            "back word moves in front of presenter",
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/layer_contract.json", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "back_typography presenter_clean_matte z_order_bottom_to_top color_relation review_questions pass_questions motion_rejects reject_if",
                "--knowledge-need",
                "KN_JSON_CONTRACT",
                "--target-recipe",
                "recipe_json_contract_gate",
                "--candidate-fields",
                "visual_check,checklist_item,forbidden_path,failure_signal",
                "--limit",
                "10",
            )
            self.assertGreaterEqual(refined["mapped_count"], 1)
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_json_contract_gate")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions_text = "\n".join(value for values in patch_draft["proposed_additions"].values() for value in values)
            self.assertIn("behind presenter matte", additions_text)
            self.assertIn("word looks like pasted sticker text", additions_text)
            self.assertIn("z_order_bottom_to_top: L10_back_typography", additions_text)
            self.assertIn("color_relation: muted green/sage only; reject web button feeling", additions_text)
            self.assertIn("review_questions: Does the face stay the first read?", additions_text)
            self.assertIn("pass_questions: Is the after frame visibly stronger than the baseline?", additions_text)
            self.assertIn("motion_rejects: text pops as sticker decoration", additions_text)

    def test_json_array_extraction_handles_clipped_contract_quote(self) -> None:
        clipped_quote = (
            '{"motion_rejects": ['
            '"any layer shifts enough to look like website animation", '
            '"text pops as sticker decoration", '
            '"back word moves in front of presenter"'
        )

        values = extract_json_array_strings(clipped_quote, "motion_rejects", include_key=True)

        self.assertEqual(
            values,
            [
                "motion_rejects: any layer shifts enough to look like website animation",
                "motion_rejects: text pops as sticker decoration",
                "motion_rejects: back word moves in front of presenter",
            ],
        )

    def test_self_run_can_scope_refinery_to_source_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            allowed = fixture_dir / "allowed_source.md"
            blocked = fixture_dir / "blocked_source.md"
            allowed.write_text(
                "visual_check: allowed layer rule\n"
                "checklist_item: allowed source must be used\n"
                "cannot_claim: allowed candidate only",
                encoding="utf-8",
            )
            blocked.write_text(
                "visual_check: blocked layer rule\n"
                "checklist_item: blocked source must not be used\n"
                "cannot_claim: blocked candidate only",
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(project, "sources", "add", "fixtures/allowed_source.md", "--read-only")
            run_cli(project, "sources", "add", "fixtures/blocked_source.md", "--read-only")

            result = run_cli(
                project,
                "self-run-benchmark",
                "--query",
                "layer rule source",
                "--knowledge-need",
                "KN_SCOPED_REFINERY",
                "--target-recipe",
                "recipe_scoped_refinery_v0",
                "--candidate-fields",
                "visual_check,checklist_item,cannot_claim",
                "--source-path-contains",
                "allowed_source.md",
                "--limit",
                "10",
            )

            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{result['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            additions_text = "\n".join(value for values in patch_draft["proposed_additions"].values() for value in values)
            self.assertIn("allowed layer rule", additions_text)
            self.assertNotIn("blocked layer rule", additions_text)
            cards = []
            for card_id in patch_draft["source_card_ids"]:
                matches = list((project / ".recipes" / "source_refinery" / "cards").glob(f"*/{card_id}.json"))
                self.assertEqual(len(matches), 1)
                cards.append(json.loads(matches[0].read_text(encoding="utf-8")))
            self.assertTrue(cards)
            self.assertTrue(
                all("allowed_source.md" in trace.get("path", "") for card in cards for trace in card.get("source_trace", []))
            )

    def test_large_source_refinery_patch_draft_recommends_splitting(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards_dir = project / ".recipes" / "source_refinery" / "cards"
            cards = []
            for index in range(45):
                card = {
                    "card_id": f"card_large_{index:02d}",
                    "card_type": "learning_atom_card",
                    "source_chunk_ids": [f"refined_large_{index:02d}"],
                    "source_trace": [
                        {
                            "path": "large-source.md",
                            "record_id": f"chunk_{index:02d}",
                            "record_type": "source_chunk",
                            "source_id": "src_large",
                        }
                    ],
                    "knowledge_need_id": "KN_LARGE_PATCH",
                    "target_recipe_id": "recipe_large_patch",
                    "target_fields": ["checklist_item"],
                    "evidence_strength": "candidate",
                    "extracted_payload": {"checklist_item": [f"Do narrow action {index:02d}."]},
                    "source_quote": f"checklist_item: Do narrow action {index:02d}.",
                    "cannot_claim": ["cannot claim large card is reviewed"],
                    "status": "candidate",
                }
                cards.append(card)
            (cards_dir / "cards.jsonl").write_text(
                "\n".join(json.dumps(card, ensure_ascii=False, sort_keys=True) for card in cards) + "\n",
                encoding="utf-8",
            )
            write_json(
                cards_dir / "latest.json",
                {
                    "refinement_id": "refinement_large",
                    "card_ids": [card["card_id"] for card in cards],
                    "card_counts": {"learning_atom_card": len(cards)},
                },
            )

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_large_patch")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            candidate_patch = json.loads(
                (project / ".recipes" / "candidates" / f"{drafted['patch_id']}.json").read_text(encoding="utf-8")
            )
            review = json.loads((project / ".recipes" / "review_queue" / f"{drafted['review_id']}.json").read_text(encoding="utf-8"))

            self.assertTrue(patch_draft["review_hints"]["split_recommended"])
            self.assertIn("拆小", "\n".join(patch_draft["review_hints"]["split_reasons"]))
            self.assertIn("拆小", patch_draft["plain_language_summary"]["next_step"])
            self.assertEqual("split_recommended", candidate_patch["risk"])
            self.assertIn("拆小", candidate_patch["plain_language_summary"]["next_step"])
            self.assertEqual("split_before_accept", review["recommendation"])
            self.assertIn("split", review["options"])
            self.assertIn("拆小", review["plain_language_summary"]["next_step"])

    def test_patch_draft_preserves_source_quote_only_fallback_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards_dir = project / ".recipes" / "source_refinery" / "cards"
            cards_dir.mkdir(parents=True, exist_ok=True)
            cards = [
                {
                    "card_id": "card_fallback_allowed",
                    "card_type": "run_chain_card",
                    "source_chunk_ids": ["refined_fallback_allowed"],
                    "source_trace": [{"path": "fallback.md", "record_id": "chunk_a", "record_type": "source_chunk"}],
                    "knowledge_need_id": "KN_FALLBACK",
                    "target_recipe_id": "recipe_fallback_quote",
                    "target_fields": ["fallback_allowed"],
                    "evidence_strength": "candidate",
                    "extracted_payload": {"source_quote": ["Use smart cutout only after the real timeline gate opens."]},
                    "source_quote": "",
                    "cannot_claim": ["candidate only"],
                    "status": "candidate",
                },
                {
                    "card_id": "card_forbidden_path",
                    "card_type": "correction_card",
                    "source_chunk_ids": ["refined_forbidden_path"],
                    "source_trace": [{"path": "fallback.md", "record_id": "chunk_b", "record_type": "source_chunk"}],
                    "knowledge_need_id": "KN_FALLBACK",
                    "target_recipe_id": "recipe_fallback_quote",
                    "target_fields": ["forbidden_path"],
                    "evidence_strength": "candidate",
                    "extracted_payload": {"source_quote": ["Remotion may only use safe no-cutout layouts before that gate."]},
                    "source_quote": "",
                    "cannot_claim": ["candidate only"],
                    "status": "candidate",
                },
            ]
            (cards_dir / "cards.jsonl").write_text(
                "\n".join(json.dumps(card, ensure_ascii=False, sort_keys=True) for card in cards) + "\n",
                encoding="utf-8",
            )

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_fallback_quote")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("Use smart cutout only after the real timeline gate opens.", patch_draft["proposed_additions"]["fallback_allowed"])
            self.assertIn("Remotion may only use safe no-cutout layouts before that gate.", patch_draft["proposed_additions"]["forbidden_path"])

    def test_cannot_claim_patch_values_filter_status_readable_path_noise(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards_dir = project / ".recipes" / "source_refinery" / "cards"
            cards_dir.mkdir(parents=True, exist_ok=True)
            card = {
                "card_id": "card_claim_noise",
                "card_type": "learning_atom_card",
                "source_chunk_ids": ["refined_claim_noise"],
                "source_trace": [{"path": "s0c_receipt.md", "record_id": "chunk_a", "record_type": "source_chunk"}],
                "knowledge_need_id": "KN_CLAIM_NOISE",
                "target_recipe_id": "recipe_claim_noise",
                "target_fields": ["cannot_claim"],
                "evidence_strength": "candidate",
                "extracted_payload": {
                    "cannot_claim": [
                        "docs/archive/clean_total_scheme_history_20260703.md - Status: readable",
                        "Status: readable",
                        "不能说资料片段已经通过人工验证。",
                    ]
                },
                "source_quote": "cannot_claim:\n- 不能说资料片段已经通过人工验证。",
                "cannot_claim": ["不能说卡片已经进入正式 recipe。"],
                "status": "candidate",
            }
            (cards_dir / "cards.jsonl").write_text(json.dumps(card, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_claim_noise")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            claim_values = "\n".join(patch_draft["proposed_additions"].get("cannot_claim", []))
            self.assertIn("不能说资料片段已经通过人工验证", claim_values)
            self.assertNotIn("Status: readable", claim_values)
            self.assertNotIn("docs/archive/clean_total_scheme_history_20260703.md", claim_values)

    def test_patch_draft_can_use_priority_queue_table_rows_as_checklist_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards_dir = project / ".recipes" / "source_refinery" / "cards"
            cards_dir.mkdir(parents=True, exist_ok=True)
            card = {
                "card_id": "card_priority_table",
                "card_type": "learning_atom_card",
                "source_chunk_ids": ["refined_priority_table"],
                "source_trace": [{"path": "COURSE_PRIORITY_QUEUE.md", "record_id": "chunk_a", "record_type": "source_chunk"}],
                "knowledge_need_id": "KN_PRIORITY_TABLE",
                "target_recipe_id": "recipe_priority_table",
                "target_fields": ["checklist_item"],
                "evidence_strength": "candidate",
                "extracted_payload": {},
                "source_quote": (
                    "| 13 | JIANYING_GPT_DEEP | P1 | method teardown for captions, effects, keyframes and material matching | "
                    "wave17 preflight done: 85 files / 46 text entries / media+zip presence recorded |\n"
                    "| 14 | GPTPRO_GATE | P1 control | helps convert supporting work vs real progress into gates | convert to checklist later under a new gate |"
                ),
                "cannot_claim": ["不能说表格行已经通过人工审核。"],
                "status": "candidate",
            }
            (cards_dir / "cards.jsonl").write_text(json.dumps(card, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_priority_table")
            patch_draft = json.loads(
                (project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").read_text(
                    encoding="utf-8"
                )
            )
            checklist = "\n".join(patch_draft["proposed_additions"].get("checklist_item", []))
            self.assertIn("JIANYING_GPT_DEEP", checklist)
            self.assertIn("GPTPRO_GATE", checklist)

    def test_source_refinery_update_existing_recipe_requires_matching_lock_and_preserves_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            run_cli(project, "init")
            run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                "Original recipe\nforbidden_path: keep original forbidden path\nvisual_check: keep original visual check",
            )
            compiled = run_cli(project, "compile", "--max-candidates", "1")
            recipe_id = run_cli(project, "review", "--accept", compiled["created"][0]["review_id"])["recipe_id"]
            original_recipe = json.loads((project / ".recipes" / "recipes" / f"{recipe_id}.json").read_text(encoding="utf-8"))
            self.assertIn("keep original forbidden path", original_recipe["forbidden_path"])

            run_cli(
                project,
                "capture",
                "--type",
                "correction",
                "--text",
                "Other recipe used only for wrong-lock test.",
            )
            other_compiled = run_cli(project, "compile", "--max-candidates", "1")
            other_recipe_id = run_cli(project, "review", "--accept", other_compiled["created"][0]["review_id"])["recipe_id"]

            source = fixture_dir / "update.md"
            source.write_text(
                "card_type: learning_atom_card\n"
                "checklist_item: new checklist from source refinery\n"
                "cannot_claim: cannot say source refinery update was field tested\n",
                encoding="utf-8",
            )
            run_cli(project, "sources", "add", "fixtures/update.md", "--read-only")
            run_cli(project, "scan", "--depth", "shallow")
            refined = run_cli(
                project,
                "refine",
                "--query",
                "new checklist source refinery",
                "--knowledge-need",
                "KN_UPDATE_EXISTING",
                "--target-recipe",
                recipe_id,
                "--candidate-fields",
                "checklist_item,cannot_claim",
                "--limit",
                "5",
            )
            run_cli(project, "extract-cards", "--refinement", refined["refinement_id"])
            drafted = run_cli(project, "patch-draft", "--target-recipe", recipe_id)

            no_lock = run_cli(project, "review", "--accept", drafted["review_id"], expect_ok=False)
            self.assertEqual(no_lock["code"], "AR411")

            wrong_lock = run_cli(project, "lock", "--recipe", other_recipe_id, "--task", "wrong lock")
            wrong_lock_result = run_cli(
                project,
                "review",
                "--accept",
                drafted["review_id"],
                "--lock",
                wrong_lock["lock"]["lock_id"],
                expect_ok=False,
            )
            self.assertEqual(wrong_lock_result["code"], "AR415")

            correct_lock = run_cli(project, "lock", "--recipe", recipe_id, "--task", "correct lock")
            accepted = run_cli(project, "review", "--accept", drafted["review_id"], "--lock", correct_lock["lock"]["lock_id"])
            self.assertEqual(accepted["recipe_id"], recipe_id)

            updated_recipe = json.loads((project / ".recipes" / "recipes" / f"{recipe_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(updated_recipe["version"], 2)
            self.assertIn("keep original forbidden path", updated_recipe["forbidden_path"])
            self.assertIn("keep original visual check", updated_recipe["visual_check"])
            self.assertIn("new checklist from source refinery", updated_recipe["checklist_item"])
            self.assertIn("cannot say source refinery update was field tested", updated_recipe["cannot_claim"])
            retired_lock = json.loads(
                (
                    project
                    / ".recipes"
                    / "locks"
                    / f"{correct_lock['lock']['lock_id']}.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(retired_lock["status"], "superseded")
            self.assertEqual(retired_lock["superseded_recipe_id"], recipe_id)
            self.assertEqual(retired_lock["superseded_recipe_hash"], updated_recipe["recipe_hash"])
            self.assertEqual(run_cli(project, "doctor")["status"], "ok")

    def test_source_refinery_split_recommended_cannot_plain_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipe_id = "recipe_review_decision_pressure_v0"
            seed_source_refinery_review(
                project,
                review_id="review_split_gate",
                patch_id="patch_split_gate",
                target_recipe_id=recipe_id,
                proposed_change=source_refinery_recipe(recipe_id, marker="too broad"),
                recommendation="split_before_accept",
                split_recommended=True,
            )

            result = run_cli(project, "review", "--accept", "review_split_gate", expect_ok=False)

            self.assertEqual(result["code"], "AR419")
            self.assertFalse((project / ".recipes" / "recipes" / f"{recipe_id}.json").exists())

    def test_source_refinery_rejected_review_cannot_later_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipe_id = "recipe_review_decision_pressure_v0"
            seed_source_refinery_review(
                project,
                review_id="review_reject_then_accept",
                patch_id="patch_reject_then_accept",
                target_recipe_id=recipe_id,
                proposed_change=source_refinery_recipe(recipe_id, marker="reject"),
            )
            run_cli(project, "review", "--reject", "review_reject_then_accept", "--reason", "too broad")

            result = run_cli(project, "review", "--accept", "review_reject_then_accept", expect_ok=False)

            self.assertEqual(result["code"], "AR417")
            self.assertFalse((project / ".recipes" / "recipes" / f"{recipe_id}.json").exists())

    def test_source_refinery_review_split_creates_child_recipes_without_parent_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipe_id = "recipe_review_decision_pressure_v0"
            proposed = source_refinery_recipe(recipe_id, marker="split")
            proposed["checklist_item"] = ["split checklist A", "split checklist B"]
            proposed["visual_check"] = ["split visual A"]
            proposed["forbidden_path"] = ["split forbidden A"]
            seed_source_refinery_review(
                project,
                review_id="review_split_ordinary",
                patch_id="patch_split_ordinary",
                target_recipe_id=recipe_id,
                proposed_change=proposed,
                recommendation="split_before_accept",
                split_recommended=True,
            )

            result = run_cli(project, "review", "--split", "review_split_ordinary")

            self.assertEqual(result["action"], "review split")
            self.assertTrue(result["created_recipe_ids"])
            self.assertFalse((project / ".recipes" / "recipes" / f"{recipe_id}.json").exists())
            for child_id in result["created_recipe_ids"]:
                self.assertTrue(child_id.startswith(f"{recipe_id}__split_"))
                child = json.loads((project / ".recipes" / "recipes" / f"{child_id}.json").read_text(encoding="utf-8"))
                self.assertEqual(child["source_refinery_decision"], "split")
                self.assertEqual(child["parent_recipe_id"], recipe_id)
                self.assertIn("不能说 source_refinery split 已经在真实任务中验证。", child["cannot_claim"])
            review = json.loads((project / ".recipes" / "review_queue" / "review_split_ordinary.json").read_text(encoding="utf-8"))
            self.assertEqual(review["status"], "split")

    def test_source_refinery_review_supersede_requires_lock_and_keeps_old_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipe_id = "recipe_review_decision_pressure_v0"
            seed_source_refinery_review(
                project,
                review_id="review_base_accept",
                patch_id="patch_base_accept",
                target_recipe_id=recipe_id,
                proposed_change=source_refinery_recipe(recipe_id, marker="base"),
            )
            run_cli(project, "review", "--accept", "review_base_accept")
            old_recipe_path = project / ".recipes" / "recipes" / f"{recipe_id}.json"
            old_hash = json.loads(old_recipe_path.read_text(encoding="utf-8"))["recipe_hash"]

            seed_source_refinery_review(
                project,
                review_id="review_supersede_ordinary",
                patch_id="patch_supersede_ordinary",
                target_recipe_id=recipe_id,
                proposed_change=source_refinery_recipe(recipe_id, title="Replacement Recipe", marker="replacement"),
            )

            no_lock = run_cli(project, "review", "--supersede", "review_supersede_ordinary", expect_ok=False)
            self.assertEqual(no_lock["code"], "AR411")

            lock = run_cli(project, "lock", "--recipe", recipe_id, "--task", "ordinary supersede pressure")
            result = run_cli(project, "review", "--supersede", "review_supersede_ordinary", "--lock", lock["lock"]["lock_id"])

            self.assertEqual(result["action"], "review supersede")
            self.assertTrue(result["created_recipe_ids"])
            self.assertEqual(json.loads(old_recipe_path.read_text(encoding="utf-8"))["recipe_hash"], old_hash)
            supersede_id = result["created_recipe_ids"][0]
            self.assertTrue(supersede_id.startswith(f"{recipe_id}__supersede_"))
            supersede = json.loads((project / ".recipes" / "recipes" / f"{supersede_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(supersede["source_refinery_decision"], "supersede")
            self.assertEqual(supersede["supersedes"], recipe_id)
            self.assertIn("不能说 source_refinery supersede 已经在真实任务中验证。", supersede["cannot_claim"])
            review = json.loads((project / ".recipes" / "review_queue" / "review_supersede_ordinary.json").read_text(encoding="utf-8"))
            self.assertEqual(review["status"], "superseded")

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg not available")
    def test_ingest_video_can_extract_local_keyframes_without_asr_or_cloud(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir()
            transcript = fixture_dir / "lesson.srt"
            video = fixture_dir / "lesson.mp4"
            transcript.write_text(
                "1\n00:00:00,100 --> 00:00:00,400\n第一帧测试。\n\n"
                "2\n00:00:00,500 --> 00:00:00,800\n第二帧测试。\n",
                encoding="utf-8",
            )
            ffmpeg_cmd = [
                shutil.which("ffmpeg") or "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=160x90:d=1",
                "-pix_fmt",
                "yuv420p",
                str(video),
            ]
            proc = subprocess.run(ffmpeg_cmd, text=True, capture_output=True, check=False)
            if proc.returncode != 0:
                self.skipTest(f"ffmpeg could not create fixture video: {proc.stderr[-500:]}")

            run_cli(project, "init")
            result = run_cli(
                project,
                "ingest-video",
                "--transcript",
                "fixtures/lesson.srt",
                "--video",
                "fixtures/lesson.mp4",
                "--extract-keyframes",
            )

            video_dir = project / ".recipes" / "video_index" / result["course_id"]
            chunks = read_jsonl(video_dir / "chunks.jsonl")
            keyframes = sorted((video_dir / "keyframes").glob("*.jpg"))

            self.assertEqual(result["keyframe_count"], 2)
            self.assertEqual(len(keyframes), 2)
            self.assertTrue(all(chunk["keyframe_path"] for chunk in chunks))
            self.assertIn("不能说已完成 ASR 或云端转写。", result["claim_status"]["cannot_claim"])
            self.assertIn("不能说关键帧视觉质量已经通过。", result["claim_status"]["cannot_claim"])


if __name__ == "__main__":
    unittest.main()
