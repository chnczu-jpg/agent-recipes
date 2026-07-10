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


def run_cli(project: Path, *args: str) -> dict[str, Any]:
    cmd = [sys.executable, "-m", "agent_recipes.cli", *args, "--project", str(project), "--json"]
    proc = subprocess.run(cmd, cwd=REPO_ROOT, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"command failed: {' '.join(cmd)}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return json.loads(proc.stdout)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


class Phase13CourseSkillDraftTest(unittest.TestCase):
    def test_timestamped_course_segment_becomes_traced_candidate_steps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            transcript = project / "lesson.txt"
            transcript.write_text(
                "\n".join(
                    [
                        "[10.00-12.00] 把原片拖到时间线上",
                        "[12.00-14.00] 复制一份放到上方并和原片对齐",
                        "[14.00-16.00] 选中上层素材开启智能抠像",
                        "[16.00-18.00] 暂时停用底层检查抠像边缘",
                        "[18.00-20.00] 添加文字并放在人物层和背景层之间",
                    ]
                ),
                encoding="utf-8",
            )
            write_json(
                project / "software_map.json",
                {
                    "software_id": "jianying_pro",
                    "version_scope": "course-evidence-only",
                    "functions": [
                        {
                            "function_id": "timeline.duplicate_overlay_align",
                            "name": "复制并对齐上层素材",
                            "purpose": "建立独立人物前景层",
                            "use_when": "需要前后景分层",
                            "ui_action": "选中底层片段，复制后粘贴到上方副轨并对齐起点",
                            "changes": "新增并对齐一条上层视频轨",
                            "expected_state": "上下两层素材时间范围一致",
                            "verification": "时间线能看到对齐的双层素材",
                            "failure_signals": "上下层不同步",
                            "fallback": "撤回并重新对齐",
                            "source_trace": [{"path": "lesson.txt", "timestamp": "12.00-14.00"}],
                        },
                        {
                            "function_id": "cutout.smart",
                            "name": "智能抠像",
                            "purpose": "把人物从背景中分离",
                            "use_when": "需要人物前景层",
                            "ui_action": "选中上层片段，在画面面板打开抠像并选智能抠像",
                            "changes": "上层视频背景变透明",
                            "expected_state": "人物成为透明背景前景层",
                            "verification": "停用底层后检查边缘",
                            "failure_signals": "背景残留或人物缺失",
                            "fallback": "改用自定义抠像或停止",
                            "source_trace": [{"path": "lesson.txt", "timestamp": "14.00-18.00"}],
                        },
                        {
                            "function_id": "timeline.layer_stack",
                            "name": "时间线分层",
                            "purpose": "控制人物、文字和背景的遮挡顺序",
                            "use_when": "需要文字在人后",
                            "ui_action": "把文字片段放在人物轨下方、背景轨上方",
                            "changes": "文字层位于人物层和背景层之间",
                            "expected_state": "人物最上、文字中间、背景最下",
                            "verification": "预览中文字被人物轮廓遮住",
                            "failure_signals": "文字完整盖住人物",
                            "fallback": "停止并重新排列轨道",
                            "source_trace": [{"path": "lesson.txt", "timestamp": "18.00-20.00"}],
                        },
                    ],
                },
            )
            write_json(
                project / "skill_spec.json",
                {
                    "skill_id": "big_text_behind_person",
                    "title": "大字在人后",
                    "software_id": "jianying_pro",
                    "use_when": ["需要人物遮住大字形成空间层次"],
                    "do_not_use_when": ["抠像边缘无法通过检查"],
                    "steps": [
                        {
                            "order": 1,
                            "function_id": "timeline.duplicate_overlay_align",
                            "start": 12.0,
                            "end": 14.0,
                        },
                        {"order": 2, "function_id": "cutout.smart", "start": 14.0, "end": 18.0},
                        {"order": 3, "function_id": "timeline.layer_stack", "start": 18.0, "end": 20.0},
                    ],
                },
            )

            result = run_cli(
                project,
                "course-skill-draft",
                "--transcript",
                "lesson.txt",
                "--spec",
                "skill_spec.json",
                "--software-map",
                "software_map.json",
            )

            self.assertEqual(result["status"], "candidate")
            self.assertEqual(result["step_count"], 3)
            self.assertEqual(result["candidate"]["steps"][1]["function_id"], "cutout.smart")
            self.assertIn("画面面板", result["candidate"]["steps"][1]["ui_action"])
            self.assertIn("智能抠像", result["candidate"]["steps"][1]["action"])
            self.assertEqual(
                result["candidate"]["steps"][2]["source_trace"][0]["timestamp"],
                "18.00-20.00",
            )
            self.assertFalse(result["formal_recipe_written"])
            self.assertFalse((project / ".recipes" / "recipes" / "big_text_behind_person.json").exists())
            self.assertIn("不能说课程候选步骤已经在真实软件中跑通。", result["claim_status"]["cannot_claim"])

    def test_mcp_exposes_course_skill_draft(self) -> None:
        names = {item["name"] for item in tool_list()}
        self.assertIn("agent_recipes_course_skill_draft", names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")
            (project / "lesson.txt").write_text("[0.00-1.00] 加文字", encoding="utf-8")
            write_json(
                project / "software_map.json",
                {
                    "software_id": "jianying_pro",
                    "version_scope": "test",
                    "functions": [
                        {
                            "function_id": "text.basic",
                            "name": "文字",
                            "purpose": "加字",
                            "use_when": "需要文字",
                            "ui_action": "打开文本面板并添加默认文本",
                            "changes": "新增文字",
                            "expected_state": "看到文字",
                            "verification": "检查预览",
                            "failure_signals": "文字没出现",
                            "fallback": "停止",
                            "source_trace": [{"path": "lesson.txt", "timestamp": "0.00-1.00"}],
                        }
                    ],
                },
            )
            write_json(
                project / "spec.json",
                {
                    "skill_id": "add_text",
                    "title": "加文字",
                    "software_id": "jianying_pro",
                    "steps": [{"order": 1, "function_id": "text.basic", "start": 0.0, "end": 1.0}],
                },
            )
            result = call_tool(
                "agent_recipes_course_skill_draft",
                {
                    "transcript": "lesson.txt",
                    "spec": "spec.json",
                    "software_map": "software_map.json",
                },
                project=project,
            )
            self.assertEqual(result["transport"], "mcp")
            self.assertEqual(result["status"], "candidate")

            capabilities = run_cli(project, "capabilities")
            course_tool = capabilities["source_refinery_tools"]["course-skill-draft"]
            self.assertTrue(course_tool["candidate_only"])
            self.assertFalse(course_tool["can_write_formal_recipe"])
            self.assertFalse(course_tool["can_claim_skill_learned"])
            completeness_tool = capabilities["source_refinery_tools"]["completeness-audit"]
            self.assertIn("fresh-agent", completeness_tool["plain"])


if __name__ == "__main__":
    unittest.main()
