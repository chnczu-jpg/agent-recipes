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


def write_cards(project: Path, cards: list[dict[str, Any]]) -> None:
    cards_dir = project / ".recipes" / "source_refinery" / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    for subdir in [
        "correction_cards",
        "run_chain_cards",
        "failure_cards",
        "learning_atom_cards",
        "visual_example_cards",
    ]:
        (cards_dir / subdir).mkdir(parents=True, exist_ok=True)
    (cards_dir / "cards.jsonl").write_text(
        "\n".join(json.dumps(card, ensure_ascii=False, sort_keys=True) for card in cards) + "\n",
        encoding="utf-8",
    )
    (cards_dir / "latest.json").write_text(
        json.dumps({"refinement_id": "manual_fusion_fixture", "card_ids": [card["card_id"] for card in cards]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def card(
    card_id: str,
    *,
    payload: dict[str, Any],
    quote: str,
    source_id: str,
    evidence_strength: str = "candidate",
    fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "card_id": card_id,
        "card_type": "learning_atom_card",
        "source_chunk_ids": [f"chunk_{card_id}"],
        "source_trace": [{"path": f"fixtures/{source_id}.md", "record_id": f"chunk_{card_id}", "record_type": "source_chunk", "source_id": source_id}],
        "knowledge_need_id": "KN_KEYFRAME_FUSION",
        "target_recipe_id": "recipe_keyframe_control_v0",
        "target_fields": fields or ["checklist_item", "cannot_claim"],
        "evidence_strength": evidence_strength,
        "extracted_payload": payload,
        "source_quote": quote,
        "cannot_claim": ["cannot say this card is a formal recipe"],
        "status": "candidate",
    }


def write_fusion_fixture_cards(project: Path, *, extra_cards: list[dict[str, Any]] | None = None) -> None:
    cards = [
        card(
            "card_partial_keyframe",
            evidence_strength="partial",
            payload={
                "checklist_item": ["关键帧课程疑似讲到速度变化，但只看了关键帧附近小片段"],
                "missing_evidence": ["缺完整课程上下文和前后案例"],
                "next_deep_read_target": ["course-a 00:05:00-00:08:00"],
            },
            quote="只看了关键帧附近小范围，信息不全，缺前后讲解。",
            source_id="course_a_partial",
        ),
        card(
            "card_attention_a",
            payload={"checklist_item": ["关键帧变化必须服务观众注意力"], "concept": ["关键帧"]},
            quote="A 课程：关键帧变化必须服务观众注意力。",
            source_id="course_a_attention",
        ),
        card(
            "card_attention_b",
            payload={"checklist_item": ["关键帧变化必须服务观众注意力"], "concept": ["关键帧"]},
            quote="B 课程：关键帧变化要服务观众注意力，而不是乱动。",
            source_id="course_b_attention",
        ),
        card(
            "card_fast_in_out",
            payload={"concept": ["关键帧"], "use_when": ["快进快出用于强调、冲击、节奏点"], "checklist_item": ["keyframe speed ramp fast in/out"]},
            quote="关键帧快进快出用于强调、冲击、节奏点。",
            source_id="course_fast",
        ),
        card(
            "card_slow_motion",
            payload={"concept": ["关键帧"], "use_when": ["影视级慢放用于情绪、质感、沉浸"], "checklist_item": ["keyframe cinematic slow motion"]},
            quote="关键帧影视级慢放用于情绪、质感、沉浸。",
            source_id="course_slow",
        ),
        card(
            "card_conflict_linear",
            payload={"checklist_item": ["关键帧必须全程线性慢放"], "conflict": ["conflicts with fast in/out keyframe use"]},
            quote="这条说关键帧必须全程线性慢放，和快进快出用法冲突。",
            source_id="course_conflict",
        ),
    ]
    if extra_cards:
        cards.extend(extra_cards)
    write_cards(project, cards)


class Phase11KnowledgeFusionTest(unittest.TestCase):
    def test_knowledge_fusion_creates_candidate_set_and_review_gate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_cards(
                project,
                [
                    card(
                        "card_partial_keyframe",
                        evidence_strength="partial",
                        payload={
                            "checklist_item": ["关键帧课程疑似讲到速度变化，但只看了关键帧附近小片段"],
                            "missing_evidence": ["缺完整课程上下文和前后案例"],
                            "next_deep_read_target": ["course-a 00:05:00-00:08:00"],
                        },
                        quote="只看了关键帧附近小范围，信息不全，缺前后讲解。",
                        source_id="course_a_partial",
                    ),
                    card(
                        "card_attention_a",
                        payload={"checklist_item": ["关键帧变化必须服务观众注意力"], "concept": ["关键帧"]},
                        quote="A 课程：关键帧变化必须服务观众注意力。",
                        source_id="course_a_attention",
                    ),
                    card(
                        "card_attention_b",
                        payload={"checklist_item": ["关键帧变化必须服务观众注意力"], "concept": ["关键帧"]},
                        quote="B 课程：关键帧变化要服务观众注意力，而不是乱动。",
                        source_id="course_b_attention",
                    ),
                    card(
                        "card_fast_in_out",
                        payload={"concept": ["关键帧"], "use_when": ["快进快出用于强调、冲击、节奏点"], "checklist_item": ["keyframe speed ramp fast in/out"]},
                        quote="关键帧快进快出用于强调、冲击、节奏点。",
                        source_id="course_fast",
                    ),
                    card(
                        "card_slow_motion",
                        payload={"concept": ["关键帧"], "use_when": ["影视级慢放用于情绪、质感、沉浸"], "checklist_item": ["keyframe cinematic slow motion"]},
                        quote="关键帧影视级慢放用于情绪、质感、沉浸。",
                        source_id="course_slow",
                    ),
                    card(
                        "card_conflict_linear",
                        payload={"checklist_item": ["关键帧必须全程线性慢放"], "conflict": ["conflicts with fast in/out keyframe use"]},
                        quote="这条说关键帧必须全程线性慢放，和快进快出用法冲突。",
                        source_id="course_conflict",
                    ),
                ],
            )

            result = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")

            self.assertEqual(result["action"], "knowledge-fusion")
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["candidate_counts"].get("needs_deep_read"), 1)
            self.assertGreaterEqual(result["candidate_counts"]["merge_candidate"], 1)
            self.assertGreaterEqual(result["candidate_counts"]["split_candidate"], 1)
            self.assertGreaterEqual(result["candidate_counts"]["conflict_candidate"], 1)
            self.assertTrue((project / ".recipes" / "source_refinery" / "fusion" / f"{result['fusion_id']}.json").exists())
            self.assertTrue((project / ".recipes" / "review_queue" / f"{result['review_id']}.json").exists())
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_keyframe_control_v0.json").exists())
            self.assertIn("不能说 knowledge_fusion candidate 已经修改正式 recipe。", result["claim_status"]["cannot_claim"])

            fusion = json.loads((project / ".recipes" / "source_refinery" / "fusion" / f"{result['fusion_id']}.json").read_text(encoding="utf-8"))
            self.assertTrue(all(item["source_trace"] for item in fusion["candidates"]))
            self.assertTrue(all(item["evidence_strength"] == "candidate" for item in fusion["candidates"]))
            self.assertTrue(any(item["candidate_type"] == "split_candidate" and "关键帧" in item["reason"] for item in fusion["candidates"]))
            self.assertTrue(any(item["candidate_type"] == "needs_deep_read" for item in fusion["candidates"]))

            review = json.loads((project / ".recipes" / "review_queue" / f"{result['review_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(review["status"], "pending")
            self.assertIn("split", review["options"])
            self.assertIn("merge", review["options"])

            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["status"], "ok")
            self.assertEqual(doctor["summary"]["source_refinery"]["fusion_candidates"], len(fusion["candidates"]))

    def test_knowledge_fusion_fails_closed_without_cards(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            result = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_missing_cards", expect_ok=False)

            self.assertEqual(result["code"], "AR410")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])
            self.assertFalse(list((project / ".recipes" / "source_refinery" / "fusion").glob("fusion_*.json")))

    def test_knowledge_fusion_flags_broad_multisource_cards_for_deep_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards = [
                card(
                    f"card_broad_{index}",
                    payload={"verified_path": [f"broad source note {index}"]},
                    quote=f"Broad course note {index}; useful as evidence for future review.",
                    source_id=f"course_{index % 4}",
                    fields=["verified_path", "cannot_claim"],
                )
                for index in range(9)
            ]
            for item in cards:
                item["target_recipe_id"] = "recipe_broad_course_material_v0"
            write_cards(project, cards)

            result = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_broad_course_material_v0")

            self.assertEqual(result["action"], "knowledge-fusion")
            self.assertEqual(result["candidate_counts"].get("needs_deep_read"), 1)
            self.assertNotIn("archive_index_only", result["candidate_counts"])
            fusion = json.loads((project / ".recipes" / "source_refinery" / "fusion" / f"{result['fusion_id']}.json").read_text(encoding="utf-8"))
            deep_read = next(candidate for candidate in fusion["candidates"] if candidate["candidate_type"] == "needs_deep_read")
            self.assertEqual(deep_read["details"]["card_count"], 9)
            self.assertEqual(deep_read["details"]["source_count"], 4)
            self.assertIn("too broad", deep_read["reason"])
            self.assertTrue(deep_read["source_trace"])

    def test_knowledge_fusion_uses_all_target_cards_not_only_latest_batch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards = [
                card(
                    f"card_cross_batch_{index}",
                    payload={"verified_path": [f"cross batch note {index}"]},
                    quote=f"Cross batch note {index}; useful only as candidate evidence.",
                    source_id=f"source_batch_{index % 4}",
                    fields=["verified_path", "cannot_claim"],
                )
                for index in range(9)
            ]
            for item in cards:
                item["target_recipe_id"] = "recipe_cross_batch_compare_v0"
            write_cards(project, cards)
            latest_path = project / ".recipes" / "source_refinery" / "cards" / "latest.json"
            latest_path.write_text(
                json.dumps(
                    {
                        "refinement_id": "latest_only_second_batch",
                        "card_ids": [cards[-1]["card_id"]],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            result = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_cross_batch_compare_v0")

            self.assertEqual(result["candidate_counts"].get("needs_deep_read"), 1)
            fusion = json.loads((project / ".recipes" / "source_refinery" / "fusion" / f"{result['fusion_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(len(fusion["source_card_ids"]), 9)
            self.assertEqual(set(fusion["source_card_ids"]), {item["card_id"] for item in cards})

    def test_knowledge_fusion_keeps_broad_deep_read_when_single_card_also_needs_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards = [
                card(
                    f"card_broad_partial_{index}",
                    payload={"verified_path": [f"broad note {index}"]},
                    quote=f"Broad note {index}; useful only as candidate evidence.",
                    source_id=f"source_broad_{index % 4}",
                    fields=["verified_path", "cannot_claim"],
                )
                for index in range(9)
            ]
            cards[0]["evidence_strength"] = "partial"
            cards[0]["extracted_payload"]["missing_evidence"] = ["needs one more scoped read"]
            for item in cards:
                item["target_recipe_id"] = "recipe_broad_with_partial_v0"
            write_cards(project, cards)

            result = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_broad_with_partial_v0")

            self.assertEqual(result["candidate_counts"].get("needs_deep_read"), 2)
            fusion = json.loads((project / ".recipes" / "source_refinery" / "fusion" / f"{result['fusion_id']}.json").read_text(encoding="utf-8"))
            broad = [
                candidate
                for candidate in fusion["candidates"]
                if candidate["candidate_type"] == "needs_deep_read"
                and candidate.get("details", {}).get("card_count") == 9
            ]
            self.assertEqual(len(broad), 1)
            self.assertEqual(len(broad[0]["source_card_ids"]), 9)

    def test_deep_read_plan_turns_needs_deep_read_into_scoped_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards = [
                card(
                    f"card_broad_{index}",
                    payload={"verified_path": [f"broad source note {index}"]},
                    quote=f"Broad course note {index}; useful as evidence for future review.",
                    source_id=f"course_{index % 4}",
                    fields=["verified_path", "cannot_claim"],
                )
                for index in range(9)
            ]
            for item in cards:
                item["target_recipe_id"] = "recipe_broad_course_material_v0"
            write_cards(project, cards)
            fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_broad_course_material_v0")

            result = run_cli(project, "deep-read-plan", "--fusion", fusion["fusion_id"])

            self.assertEqual(result["action"], "deep-read-plan")
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["task_count"], 4)
            self.assertTrue((project / ".recipes" / "source_refinery" / "deep_read_plans" / f"{result['plan_id']}.json").exists())
            paths = sorted(task["source_path_contains"][0] for task in result["tasks"])
            self.assertEqual(paths, ["course_0.md", "course_1.md", "course_2.md", "course_3.md"])
            for task in result["tasks"]:
                self.assertEqual(task["target_recipe_id"], "recipe_broad_course_material_v0")
                self.assertEqual(task["next_command"], "self-run-benchmark")
                self.assertIn("verified_path", task["candidate_fields"])
                self.assertEqual(len(task["source_path_contains"]), 1)
                self.assertTrue(task["source_trace"])
                self.assertTrue(task["source_card_ids"])
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_broad_course_material_v0.json").exists())
            self.assertIn("不能说 deep-read plan 已经完成深读。", result["claim_status"]["cannot_claim"])
            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["summary"]["source_refinery"]["deep_read_plans"], 1)

    def test_target_suggestions_turns_rejected_review_into_source_scoped_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards = [
                card(
                    "card_keyframe_contract",
                    payload={"checklist_item": ["keyframe contract narrow rule"], "verified_path": ["keyframe contract evidence"]},
                    quote="checklist_item: keyframe contract narrow rule\nverified_path: keyframe contract evidence",
                    source_id="keyframe_contract",
                    fields=["checklist_item", "verified_path", "cannot_claim"],
                ),
                card(
                    "card_layer_contract",
                    payload={"checklist_item": ["layer contract narrow rule"], "verified_path": ["layer contract evidence"]},
                    quote="checklist_item: layer contract narrow rule\nverified_path: layer contract evidence",
                    source_id="layer_contract",
                    fields=["checklist_item", "verified_path", "cannot_claim"],
                ),
            ]
            for item in cards:
                item["target_recipe_id"] = "recipe_broad_cross_compare_v0"
            write_cards(project, cards)
            draft = run_cli(project, "patch-draft", "--target-recipe", "recipe_broad_cross_compare_v0")
            run_cli(project, "review", "--reject", draft["review_id"], "--reason", "candidate is useful but too broad; rerun narrower source-scoped targets")

            result = run_cli(project, "target-suggestions", "--target-recipe", "recipe_broad_cross_compare_v0")

            self.assertEqual(result["action"], "target-suggestions")
            self.assertTrue(result["candidate_only"])
            self.assertEqual(result["suggestion_count"], 2)
            self.assertTrue((project / ".recipes" / "reports" / f"{result['report_id']}.json").exists())
            suggested_ids = {item["suggested_target_recipe_id"] for item in result["suggestions"]}
            self.assertIn("recipe_broad_cross_compare_v0__narrow_keyframe_contract", suggested_ids)
            self.assertIn("recipe_broad_cross_compare_v0__narrow_layer_contract", suggested_ids)
            for suggestion in result["suggestions"]:
                self.assertEqual(suggestion["next_command"], "self-run-benchmark")
                self.assertEqual(suggestion["command_args"]["target_recipe_id"], suggestion["suggested_target_recipe_id"])
                self.assertEqual(suggestion["command_args"]["source_path_contains"], suggestion["source_path_contains"])
                self.assertIn("checklist_item", suggestion["candidate_fields"])
                self.assertIn("不能说 target suggestion 已经生成正式 recipe。", suggestion["cannot_claim"])
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_broad_cross_compare_v0.json").exists())
            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["summary"]["source_refinery"]["target_suggestion_reports"], 1)
            self.assertEqual(doctor["summary"]["source_refinery"]["target_suggestions"], 2)

    def test_target_suggestions_fails_closed_without_review_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            result = run_cli(project, "target-suggestions", expect_ok=False)

            self.assertEqual(result["code"], "AR442")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])

    def test_mcp_exposes_knowledge_fusion(self) -> None:
        tool_names = {tool["name"] for tool in tool_list()}
        self.assertIn("agent_recipes_knowledge_fusion", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_cards(
                project,
                [
                    card(
                        "card_mcp_partial",
                        evidence_strength="partial",
                        payload={"missing_evidence": ["缺完整上下文"], "next_deep_read_target": ["course 00:01:00-00:03:00"]},
                        quote="MCP smoke: 信息不全，需要定向深读。",
                        source_id="mcp_partial",
                    )
                ],
            )

            result = call_tool(
                "agent_recipes_knowledge_fusion",
                {"project": str(project), "target_recipe_id": "recipe_keyframe_control_v0"},
                project=project,
            )

            self.assertEqual(result["tool"], "agent_recipes_knowledge_fusion")
            self.assertEqual(result["candidate_counts"]["needs_deep_read"], 1)
            self.assertEqual(result["transport"], "mcp")

    def test_mcp_exposes_deep_read_plan(self) -> None:
        tool_names = {tool["name"] for tool in tool_list()}
        self.assertIn("agent_recipes_deep_read_plan", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_cards(
                project,
                [
                    card(
                        "card_mcp_partial",
                        evidence_strength="partial",
                        payload={"missing_evidence": ["缺完整上下文"], "next_deep_read_target": ["course 00:01:00-00:03:00"]},
                        quote="MCP smoke: 信息不全，需要定向深读。",
                        source_id="mcp_partial",
                    )
                ],
            )
            fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")

            result = call_tool(
                "agent_recipes_deep_read_plan",
                {"project": str(project), "fusion_id": fusion["fusion_id"]},
            )

            self.assertEqual(result["tool"], "agent_recipes_deep_read_plan")
            self.assertEqual(result["transport"], "mcp")
            self.assertEqual(result["task_count"], 1)
            self.assertEqual(result["tasks"][0]["source_path_contains"], ["mcp_partial.md"])

    def test_mcp_exposes_target_suggestions(self) -> None:
        tool_names = {tool["name"] for tool in tool_list()}
        self.assertIn("agent_recipes_target_suggestions", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            cards = [
                card(
                    "card_mcp_target_suggestion",
                    payload={"checklist_item": ["source scoped candidate"]},
                    quote="checklist_item: source scoped candidate",
                    source_id="target_suggestion_source",
                )
            ]
            for item in cards:
                item["target_recipe_id"] = "recipe_mcp_target_suggestion_v0"
            write_cards(project, cards)
            draft = run_cli(project, "patch-draft", "--target-recipe", "recipe_mcp_target_suggestion_v0")
            run_cli(project, "review", "--reject", draft["review_id"], "--reason", "rerun as a narrower target")

            result = call_tool(
                "agent_recipes_target_suggestions",
                {"project": str(project), "target_recipe_id": "recipe_mcp_target_suggestion_v0"},
                project=project,
            )

            self.assertEqual(result["tool"], "agent_recipes_target_suggestions")
            self.assertEqual(result["transport"], "mcp")
            self.assertEqual(result["suggestion_count"], 1)
            self.assertEqual(result["suggestions"][0]["source_path_contains"], ["target_suggestion_source.md"])

    def test_mcp_self_run_keeps_source_path_scope(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            source_a = project / "source_a.md"
            source_b = project / "source_b.md"
            source_a.write_text("checklist_item: alpha narrow path\ncannot_claim: cannot say alpha is formal\n", encoding="utf-8")
            source_b.write_text("checklist_item: beta should be filtered out\ncannot_claim: cannot say beta is formal\n", encoding="utf-8")
            run_cli(project, "sources", "add", str(source_a), "--read-only")
            run_cli(project, "sources", "add", str(source_b), "--read-only")

            result = call_tool(
                "agent_recipes_self_run_benchmark",
                {
                    "project": str(project),
                    "query": "checklist_item alpha",
                    "knowledge_need_id": "KN_MCP_SCOPED_SELF_RUN",
                    "target_recipe_id": "recipe_mcp_scoped_self_run_v0",
                    "candidate_fields": ["checklist_item", "cannot_claim"],
                    "source_path_contains": ["source_a.md"],
                    "scan_depth": "medium",
                    "kind": "source",
                    "min_cards": 1,
                    "limit": 5,
                },
                project=project,
            )

            self.assertEqual(result["tool"], "agent_recipes_self_run_benchmark")
            self.assertTrue(result["ok"])
            self.assertEqual(result["source_path_contains"], ["source_a.md"])
            review = json.loads((project / ".recipes" / "review_queue" / f"{result['review_id']}.json").read_text(encoding="utf-8"))
            patch = json.loads((project / ".recipes" / "candidates" / f"{review['proposed_patch_id']}.json").read_text(encoding="utf-8"))
            source_paths = []
            for card_id in patch["source_card_ids"]:
                card_doc = json.loads(next((project / ".recipes" / "source_refinery" / "cards").glob(f"*/{card_id}.json")).read_text(encoding="utf-8"))
                source_paths.extend(trace["path"] for trace in card_doc["source_trace"])
            self.assertTrue(source_paths)
            self.assertTrue(all(Path(path).name == "source_a.md" for path in source_paths))

    def test_mcp_exposes_fusion_review_decide(self) -> None:
        tool_names = {tool["name"] for tool in tool_list()}
        self.assertIn("agent_recipes_review_decide", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_fusion_fixture_cards(project)
            fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")

            result = call_tool(
                "agent_recipes_review_decide",
                {"project": str(project), "review_id": fusion["review_id"], "decision": "merge"},
                project=project,
            )

            self.assertEqual(result["tool"], "agent_recipes_review_decide")
            self.assertEqual(result["action"], "review merge")
            self.assertEqual(result["recipe_id"], "recipe_keyframe_control_v0")

    def test_fusion_review_cannot_use_plain_accept(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_fusion_fixture_cards(project)
            fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")

            result = run_cli(project, "review", "--accept", fusion["review_id"], expect_ok=False)

            self.assertEqual(result["code"], "AR416")
            self.assertIn("不能说命令已成功执行。", result["claim_status"]["cannot_claim"])
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_keyframe_control_v0.json").exists())

    def test_review_merge_promotes_fusion_to_formal_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_fusion_fixture_cards(project)
            fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")

            result = run_cli(project, "review", "--merge", fusion["review_id"])

            self.assertEqual(result["action"], "review merge")
            self.assertEqual(result["recipe_id"], "recipe_keyframe_control_v0")
            recipe_path = project / ".recipes" / "recipes" / "recipe_keyframe_control_v0.json"
            self.assertTrue(recipe_path.exists())
            recipe = json.loads(recipe_path.read_text(encoding="utf-8"))
            self.assertEqual(recipe["knowledge_fusion_decision"], "merge")
            self.assertEqual(recipe["source_fusion_id"], fusion["fusion_id"])
            self.assertTrue(recipe["fusion_candidate_ids"])
            self.assertIn("关键帧变化必须服务观众注意力", "\n".join(recipe["steps"] + recipe.get("checklist_item", [])))
            self.assertIn("不能说 knowledge_fusion merge 已经在真实任务中验证。", recipe["cannot_claim"])
            review = json.loads((project / ".recipes" / "review_queue" / f"{fusion['review_id']}.json").read_text(encoding="utf-8"))
            self.assertEqual(review["status"], "merged")

    def test_review_split_creates_child_recipes_without_parent_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_fusion_fixture_cards(project)
            fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")

            result = run_cli(project, "review", "--split", fusion["review_id"])

            self.assertEqual(result["action"], "review split")
            self.assertTrue(result["created_recipe_ids"])
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_keyframe_control_v0.json").exists())
            for recipe_id in result["created_recipe_ids"]:
                self.assertTrue(recipe_id.startswith("recipe_keyframe_control_v0__split_"))
                recipe = json.loads((project / ".recipes" / "recipes" / f"{recipe_id}.json").read_text(encoding="utf-8"))
                self.assertEqual(recipe["knowledge_fusion_decision"], "split")
                self.assertEqual(recipe["parent_recipe_id"], "recipe_keyframe_control_v0")
                self.assertEqual(recipe["source_fusion_id"], fusion["fusion_id"])
                self.assertIn("不能说 knowledge_fusion split 已经在真实任务中验证。", recipe["cannot_claim"])

    def test_review_supersede_requires_lock_and_keeps_old_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_fusion_fixture_cards(project)
            first_fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")
            run_cli(project, "review", "--merge", first_fusion["review_id"])
            old_recipe_path = project / ".recipes" / "recipes" / "recipe_keyframe_control_v0.json"
            old_recipe = json.loads(old_recipe_path.read_text(encoding="utf-8"))

            write_fusion_fixture_cards(
                project,
                extra_cards=[
                    card(
                        "card_attention_c",
                        payload={"checklist_item": ["关键帧变化必须服务观众注意力"], "concept": ["关键帧"]},
                        quote="C 课程：关键帧服务注意力，但要根据段落目的选择动法。",
                        source_id="course_c_attention",
                    )
                ],
            )
            second_fusion = run_cli(project, "knowledge-fusion", "--target-recipe", "recipe_keyframe_control_v0")
            no_lock = run_cli(project, "review", "--supersede", second_fusion["review_id"], expect_ok=False)
            self.assertEqual(no_lock["code"], "AR411")

            lock = run_cli(project, "lock", "--recipe", "recipe_keyframe_control_v0", "--task", "supersede fusion")
            result = run_cli(project, "review", "--supersede", second_fusion["review_id"], "--lock", lock["lock"]["lock_id"])

            self.assertEqual(result["action"], "review supersede")
            self.assertTrue(result["created_recipe_ids"])
            self.assertTrue(old_recipe_path.exists())
            self.assertEqual(json.loads(old_recipe_path.read_text(encoding="utf-8"))["recipe_hash"], old_recipe["recipe_hash"])
            supersede_id = result["created_recipe_ids"][0]
            self.assertTrue(supersede_id.startswith("recipe_keyframe_control_v0__supersede_"))
            supersede_recipe = json.loads((project / ".recipes" / "recipes" / f"{supersede_id}.json").read_text(encoding="utf-8"))
            self.assertEqual(supersede_recipe["knowledge_fusion_decision"], "supersede")
            self.assertEqual(supersede_recipe["supersedes"], "recipe_keyframe_control_v0")
            self.assertIn("不能说 knowledge_fusion supersede 已经在真实任务中验证。", supersede_recipe["cannot_claim"])


if __name__ == "__main__":
    unittest.main()
