from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agent_recipes.core import structured_json_candidate_lines
from agent_recipes.mcp import call_tool, tool_list


REPO_ROOT = Path(__file__).resolve().parents[1]


def run_cli(project: Path, *args: str) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return json.loads(proc.stdout)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Phase12CompletenessTest(unittest.TestCase):
    def test_json_test_contract_maps_claims_evidence_and_layers_without_source_path_leak(self) -> None:
        lines = structured_json_candidate_lines(
            {
                "test_id": "micro_v1",
                "input_source": "/private/source/presenter.mp4",
                "required_layer_stack": ["底层：原片", "中层：大字", "上层：抠像主持人"],
                "required_evidence": ["剪映时间线截图", "本地导出样片"],
                "cannot_claim": ["不能说用户已经验收"],
            }
        )

        text = "\n".join(lines)
        self.assertIn("visual_check: required_layer_stack", text)
        self.assertIn("checklist_item: required_evidence", text)
        self.assertIn("cannot_claim: cannot_claim", text)
        self.assertNotIn("input_source", text)
        self.assertNotIn("/private/source", text)

    def test_skill_requires_structure_and_domain_specific_actions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            recipe_path = project / "skill.json"
            requirements_path = project / "requirements.json"
            write_json(
                recipe_path,
                {
                    "recipe_id": "recipe_big_text",
                    "version": 1,
                    "use_when": ["大字在人后"],
                    "do_not_use_when": ["没有人物素材"],
                    "inputs_required": ["原始口播视频"],
                    "source_truth_to_read": ["human correction"],
                    "steps": ["底层放原片", "中层放大字", "上层放抠像人物"],
                    "verification": ["检查真实剪辑时间线三层结构"],
                    "success_means": ["大字被人物正确遮挡"],
                    "failure_signals": ["文字压在人脸上"],
                    "fallback_allowed": ["抠像失败则停止并返修"],
                    "evidence_refs": ["evt_human_correction"],
                    "verified_path": ["timeline screenshot"],
                    "cannot_claim": ["不能说未导出的时间线已经质量通过"],
                },
            )
            write_json(
                requirements_path,
                {
                    "requirements": [
                        {
                            "id": "three_layer_stack",
                            "label": "底层原片、中层大字、上层抠像人物",
                            "fields": ["steps", "verification"],
                            "term_groups": [
                                ["底层", "bottom"],
                                ["原片", "original footage"],
                                ["中层", "middle"],
                                ["大字", "big text"],
                                ["上层", "top"],
                                ["抠像", "cutout"],
                            ],
                            "ordered_term_groups": [
                                [["底层", "bottom"], ["原片", "original footage"]],
                                [["中层", "中间层", "middle"], ["大字", "big text"]],
                                [["上层", "top"], ["抠像", "cutout"], ["人物", "主持人", "presenter"]],
                            ],
                        }
                    ]
                },
            )

            result = run_cli(
                project,
                "completeness-audit",
                "--input",
                "skill.json",
                "--subject-type",
                "skill",
                "--requirements",
                "requirements.json",
            )

            self.assertEqual(result["status"], "complete_for_review")
            self.assertTrue(result["hard_gates_passed"])
            self.assertTrue(result["domain_checks"][0]["passed"])
            self.assertEqual(result["domain_score"], 1.0)
            self.assertEqual(result["overall_score"], 1.0)
            self.assertIn("不能说完整性分数证明内容正确。", result["claim_status"]["cannot_claim"])

    def test_skill_fails_closed_when_layer_words_are_present_but_order_is_wrong(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(
                project / "skill.json",
                {
                    "use_when": ["大字在人后"],
                    "steps": ["底层放大字", "中层放原片", "上层放抠像人物"],
                    "verification": ["检查遮挡"],
                    "evidence_refs": ["source"],
                    "verified_path": ["timeline"],
                    "cannot_claim": ["未跑真实时间线不能说完成"],
                },
            )
            write_json(
                project / "requirements.json",
                {
                    "requirements": [
                        {
                            "id": "ordered_stack",
                            "label": "三层顺序必须正确",
                            "fields": ["steps"],
                            "ordered_term_groups": [
                                ["底层", "原片"],
                                ["中层", "大字"],
                                ["上层", "抠像", "人物"],
                            ],
                        }
                    ]
                },
            )

            result = run_cli(
                project,
                "completeness-audit",
                "--input",
                "skill.json",
                "--subject-type",
                "skill",
                "--requirements",
                "requirements.json",
            )

            self.assertEqual(result["status"], "incomplete")
            self.assertFalse(result["domain_checks"][0]["passed"])
            self.assertTrue(result["domain_checks"][0]["missing_ordered_term_groups"])

    def test_course_full_coverage_can_enter_review_but_not_claim_mastery(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(
                project / "course.json",
                {
                    "coverage_complete": True,
                    "topic": "关键帧",
                    "scope": "快进快出和影视级慢放分开处理",
                    "steps": ["确认用途", "添加关键帧", "调整参数", "回看并验收"],
                    "examples": ["快进快出", "影视级慢放"],
                    "variants": ["两种用法不能合成一个万能步骤"],
                    "verification": ["看关键帧曲线和最终节奏"],
                    "success_means": ["达到本次目标且没有误套另一种用法"],
                    "source_trace": [{"path": "course.mp4", "timestamp": "00:10"}],
                    "timestamps": ["00:10", "00:20"],
                    "cannot_claim": ["不能说 agent 已经掌握"],
                    "status": "candidate",
                },
            )

            result = run_cli(project, "completeness-audit", "--input", "course.json", "--subject-type", "course")

            self.assertEqual(result["status"], "complete_for_review")
            self.assertEqual(result["domain_score"], None)
            self.assertIn("不能说课程结构完整就证明已经学会或覆盖整套课程。", result["claim_status"]["cannot_claim"])

    def test_skill_fails_closed_when_bottom_original_layer_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(
                project / "skill.json",
                {
                    "recipe_id": "recipe_big_text",
                    "version": 1,
                    "use_when": ["大字在人后"],
                    "do_not_use_when": ["没有人物素材"],
                    "inputs_required": ["视频"],
                    "source_truth_to_read": ["course"],
                    "steps": ["放大字", "抠像人物", "人物放到文字上方"],
                    "verification": ["检查遮挡"],
                    "success_means": ["人物遮住文字"],
                    "failure_signals": ["边缘穿帮"],
                    "fallback_allowed": ["返修"],
                    "evidence_refs": ["source"],
                    "verified_path": ["proof"],
                    "cannot_claim": ["未跑真实时间线不能说完成"],
                },
            )
            write_json(
                project / "requirements.json",
                {
                    "requirements": [
                        {
                            "id": "bottom_original",
                            "label": "底层必须保留原片",
                            "fields": ["steps"],
                            "term_groups": [["底层", "bottom"], ["原片", "original footage"]],
                        }
                    ]
                },
            )

            result = run_cli(
                project,
                "completeness-audit",
                "--input",
                "skill.json",
                "--subject-type",
                "skill",
                "--requirements",
                "requirements.json",
            )

            self.assertEqual(result["status"], "incomplete")
            self.assertFalse(result["hard_gates_passed"])
            self.assertEqual(result["required_domain_failures"], ["bottom_original"])

    def test_course_shallow_read_needs_deep_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(
                project / "course.json",
                {
                    "coverage_complete": False,
                    "coverage_ratio": 0.25,
                    "read_mode": "关键帧浅读",
                    "topic": "关键帧",
                    "scope": "快进快出",
                    "steps": ["加关键帧", "调速度", "检查节奏"],
                    "examples": ["示例 A"],
                    "variants": ["慢放是另一种用法"],
                    "verification": ["看速度曲线"],
                    "success_means": ["节奏符合目标"],
                    "source_trace": [{"path": "course.mp4", "timestamp": "00:10"}],
                    "timestamps": ["00:10"],
                    "cannot_claim": ["不能说已读完整套课程"],
                    "status": "candidate",
                },
            )

            result = run_cli(project, "completeness-audit", "--input", "course.json", "--subject-type", "course")

            self.assertEqual(result["status"], "needs_deep_read")
            self.assertIn("source_coverage", result["hard_gate_failures"])
            self.assertIn("没有提供技能/课程特有 requirements", result["claim_status"]["missing_evidence"][-1])

    def test_software_skill_concept_steps_are_not_executable_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(
                project / "skill.json",
                {
                    "recipe_id": "recipe_big_text",
                    "version": 1,
                    "use_when": ["大字在人后"],
                    "do_not_use_when": ["没有人物素材"],
                    "inputs_required": ["原片"],
                    "source_truth_to_read": ["课程"],
                    "steps": ["底层原片", "中层大字", "上层抠像人物"],
                    "verification": ["看起来像在人后"],
                    "success_means": ["人物遮住文字"],
                    "failure_signals": ["文字盖住人物"],
                    "fallback_allowed": ["失败就停止"],
                    "evidence_refs": ["course"],
                    "verified_path": ["timeline"],
                    "cannot_claim": ["不能说已经跑通"],
                },
            )
            write_json(
                project / "requirements.json",
                {
                    "execution_contract": {
                        "mode": "software",
                        "software_id": "jianying_pro",
                        "min_steps": 3,
                        "required_step_fields": [
                            "order",
                            "action",
                            "function_id",
                            "ui_action",
                            "expected_state",
                            "verification",
                            "source_trace",
                        ],
                        "required_function_ids": [
                            "timeline.layer_stack",
                            "text.basic",
                            "cutout.smart",
                        ],
                        "require_fresh_execution": True,
                    }
                },
            )

            result = run_cli(
                project,
                "completeness-audit",
                "--input",
                "skill.json",
                "--subject-type",
                "skill",
                "--requirements",
                "requirements.json",
            )

            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["execution_readiness"]["status"], "incomplete")
            self.assertIn("structured_steps", result["execution_readiness"]["hard_gate_failures"])
            self.assertIn("software_function_map", result["execution_readiness"]["hard_gate_failures"])

    def test_structured_software_skill_waits_for_fresh_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(
                project / "skill.json",
                {
                    "recipe_id": "recipe_big_text",
                    "version": 1,
                    "use_when": ["大字在人后"],
                    "do_not_use_when": ["没有人物素材"],
                    "inputs_required": ["原片"],
                    "source_truth_to_read": ["课程"],
                    "steps": [
                        {
                            "order": 1,
                            "action": "把原片放到底层轨道",
                            "function_id": "timeline.layer_stack",
                            "ui_action": "把原片置于主轨最底层",
                            "expected_state": "底层出现一条原片视频轨",
                            "verification": "时间线能看到原片轨",
                            "source_trace": [{"path": "course.mp4", "timestamp": "00:10"}],
                        },
                        {
                            "order": 2,
                            "action": "建立文字层并输入大字",
                            "function_id": "text.basic",
                            "ui_action": "打开文本面板并把默认文本放到中层",
                            "expected_state": "文字位于原片上方",
                            "verification": "预览能看到文字",
                            "source_trace": [{"path": "course.mp4", "timestamp": "00:20"}],
                        },
                        {
                            "order": 3,
                            "action": "复制人物层并开启智能抠像",
                            "function_id": "cutout.smart",
                            "ui_action": "选中上层人物片段，在画面面板开启智能抠像",
                            "expected_state": "人物透明背景层位于文字上方",
                            "verification": "文字只在人物轮廓外露出",
                            "source_trace": [{"path": "course.mp4", "timestamp": "00:30"}],
                        },
                    ],
                    "verification": ["逐步检查轨道和人物遮挡"],
                    "success_means": ["文字被人物轮廓遮挡"],
                    "failure_signals": ["文字完整盖住人物"],
                    "fallback_allowed": ["回到上一步检查轨道和抠像"],
                    "evidence_refs": ["course.mp4"],
                    "verified_path": ["timeline screenshot"],
                    "cannot_claim": ["没有 fresh agent 一次跑通证据，不能说技能已经学会"],
                },
            )
            write_json(
                project / "requirements.json",
                {
                    "execution_contract": {
                        "mode": "software",
                        "software_id": "jianying_pro",
                        "min_steps": 3,
                        "required_step_fields": [
                            "order",
                            "action",
                            "function_id",
                            "expected_state",
                            "verification",
                            "source_trace",
                        ],
                        "required_function_ids": [
                            "timeline.layer_stack",
                            "text.basic",
                            "cutout.smart",
                        ],
                        "require_fresh_execution": True,
                    }
                },
            )
            write_json(
                project / "software_map.json",
                {
                    "software_id": "jianying_pro",
                    "version_scope": "tested-current-local-client",
                    "functions": [
                        {
                            "function_id": function_id,
                            "name": name,
                            "purpose": purpose,
                            "use_when": use_when,
                            "ui_action": f"在界面执行 {name}",
                            "changes": changes,
                            "expected_state": expected_state,
                            "failure_signals": failure_signals,
                            "source_trace": [{"path": "course.mp4", "timestamp": timestamp}],
                        }
                        for function_id, name, purpose, use_when, changes, expected_state, failure_signals, timestamp in [
                            (
                                "timeline.layer_stack",
                                "时间线分层",
                                "控制前后遮挡顺序",
                                "需要前中后景时",
                                "改变轨道层级",
                                "时间线出现有顺序的多轨",
                                "轨道顺序与预期相反",
                                "00:10",
                            ),
                            (
                                "text.basic",
                                "基础文字",
                                "建立可编辑文字层",
                                "需要画面文字时",
                                "新增文字素材",
                                "预览和时间线都能看到文字",
                                "输入后退回主时间线文字消失",
                                "00:20",
                            ),
                            (
                                "cutout.smart",
                                "智能抠像",
                                "把人物从背景中分离",
                                "需要人物前景层时",
                                "视频层背景变透明",
                                "下层内容能从人物轮廓外显示",
                                "预览仍显示完整原片背景",
                                "00:30",
                            ),
                        ]
                    ],
                },
            )

            result = run_cli(
                project,
                "completeness-audit",
                "--input",
                "skill.json",
                "--subject-type",
                "skill",
                "--requirements",
                "requirements.json",
                "--software-map",
                "software_map.json",
            )

            self.assertEqual(result["status"], "complete_for_review")
            self.assertEqual(result["execution_readiness"]["status"], "needs_fresh_execution")
            self.assertTrue(result["execution_readiness"]["structured_steps_passed"])
            self.assertTrue(result["execution_readiness"]["software_function_map_passed"])
            self.assertFalse(result["execution_readiness"]["fresh_execution_passed"])

            (project / "timeline.png").write_bytes(b"real-evidence")
            write_json(
                project / "execution_result.json",
                {
                    "fresh_agent": True,
                    "clean_start": True,
                    "recipe_only": True,
                    "attempt_count": 1,
                    "passed": False,
                    "evidence_paths": [str(project / "timeline.png")],
                },
            )
            failed = run_cli(
                project,
                "completeness-audit",
                "--input",
                "skill.json",
                "--subject-type",
                "skill",
                "--requirements",
                "requirements.json",
                "--software-map",
                "software_map.json",
                "--execution-evidence",
                "execution_result.json",
            )
            self.assertEqual(failed["status"], "execution_failed")
            self.assertEqual(failed["execution_readiness"]["status"], "fresh_execution_failed")

            write_json(
                project / "execution_result.json",
                {
                    "fresh_agent": True,
                    "clean_start": True,
                    "recipe_only": True,
                    "attempt_count": 1,
                    "passed": True,
                    "evidence_paths": [str(project / "timeline.png")],
                },
            )
            verified = run_cli(
                project,
                "completeness-audit",
                "--input",
                "skill.json",
                "--subject-type",
                "skill",
                "--requirements",
                "requirements.json",
                "--software-map",
                "software_map.json",
                "--execution-evidence",
                "execution_result.json",
            )
            self.assertEqual(verified["status"], "execution_verified")
            self.assertEqual(verified["execution_readiness"]["status"], "fresh_execution_verified")
            self.assertTrue(verified["execution_readiness"]["fresh_execution_passed"])
            self.assertEqual(
                Path(verified["execution_evidence_path"]).resolve(),
                (project / "execution_result.json").resolve(),
            )

    def test_course_must_map_every_taught_skill_to_traced_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(
                project / "course.json",
                {
                    "coverage_complete": True,
                    "topic": "后期包装课",
                    "scope": "大字在人后与花字",
                    "taught_skills": ["big_text_behind_person", "decorative_text"],
                    "extracted_skills": [
                        {
                            "skill_id": "big_text_behind_person",
                            "step_orders": [1, 2, 3],
                            "source_trace": [{"path": "course.mp4", "timestamp": "00:10-00:40"}],
                        }
                    ],
                    "steps": [
                        {
                            "order": 1,
                            "action": "放原片",
                            "function_id": "timeline.layer_stack",
                            "expected_state": "底层有原片",
                            "verification": "看时间线",
                            "source_trace": [{"path": "course.mp4", "timestamp": "00:10"}],
                        },
                        {
                            "order": 2,
                            "action": "加文字",
                            "function_id": "text.basic",
                            "expected_state": "中层有文字",
                            "verification": "看预览",
                            "source_trace": [{"path": "course.mp4", "timestamp": "00:20"}],
                        },
                        {
                            "order": 3,
                            "action": "抠人物",
                            "function_id": "cutout.smart",
                            "expected_state": "上层有人物",
                            "verification": "看遮挡",
                            "source_trace": [{"path": "course.mp4", "timestamp": "00:30"}],
                        },
                    ],
                    "examples": ["大字在人后"],
                    "variants": ["花字是另一条技能"],
                    "verification": ["逐项复核"],
                    "success_means": ["每个技能都有步骤"],
                    "source_trace": [{"path": "course.mp4", "timestamp": "00:00-10:00"}],
                    "timestamps": ["00:10", "00:20", "00:30"],
                    "cannot_claim": ["不能说未拆出的花字已经学会"],
                    "status": "candidate",
                },
            )
            write_json(
                project / "requirements.json",
                {
                    "execution_contract": {
                        "mode": "software",
                        "software_id": "jianying_pro",
                        "min_steps": 3,
                        "required_step_fields": [
                            "order",
                            "action",
                            "function_id",
                            "expected_state",
                            "verification",
                            "source_trace",
                        ],
                        "require_skill_inventory": True,
                    }
                },
            )

            result = run_cli(
                project,
                "completeness-audit",
                "--input",
                "course.json",
                "--subject-type",
                "course",
                "--requirements",
                "requirements.json",
            )

            self.assertEqual(result["status"], "incomplete")
            self.assertIn("course_skill_inventory", result["execution_readiness"]["hard_gate_failures"])
            self.assertEqual(result["execution_readiness"]["unmapped_taught_skills"], ["decorative_text"])

    def test_mcp_exposes_completeness_audit(self) -> None:
        names = {item["name"] for item in tool_list()}
        self.assertIn("agent_recipes_completeness_audit", names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            write_json(project / "skill.json", {"steps": ["one"]})
            result = call_tool(
                "agent_recipes_completeness_audit",
                {"input": "skill.json", "subject_type": "skill"},
                project=project,
            )
            self.assertEqual(result["transport"], "mcp")
            self.assertEqual(result["status"], "incomplete")


if __name__ == "__main__":
    unittest.main()
