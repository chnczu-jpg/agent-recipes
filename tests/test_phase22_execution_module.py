from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_recipes.core import lookup_applicability as core_lookup_applicability
from agent_recipes.execution import (
    build_execution_lock,
    lookup_applicability,
    lookup_execution_policy,
    rank_recipes_for_lookup,
    retire_active_execution_locks,
    retire_stale_execution_locks,
    validate_execution_lock,
)
from agent_recipes.persistence import RecipesError, read_json, write_json


REPO_ROOT = Path(__file__).resolve().parents[1]


def recipe(recipe_id: str, *, version: int = 1, recipe_hash: str | None = None) -> dict:
    return {
        "recipe_id": recipe_id,
        "version": version,
        "recipe_hash": recipe_hash or f"hash_{recipe_id}_{version}",
        "title": "big text behind presenter",
        "scope": "three layer compositing",
        "use_when": ["big text behind presenter"],
        "steps": ["background video, middle text, top presenter cutout"],
        "verification": ["presenter occludes part of the text"],
        "cannot_claim": ["rendered quality still needs review"],
        "forbidden_path": ["do not flatten the three layers"],
        "stop_line": "stop when the layer order is unknown",
    }


class Phase22ExecutionModuleTest(unittest.TestCase):
    def test_execution_import_is_independent_and_core_reexports_lookup_policy(self) -> None:
        code = """
import sys
from agent_recipes.execution import lookup_execution_policy, validate_execution_lock
assert 'agent_recipes.core' not in sys.modules
assert lookup_execution_policy.__module__ == 'agent_recipes.execution'
assert validate_execution_lock.__module__ == 'agent_recipes.execution'
"""
        proc = subprocess.run(
            [sys.executable, "-S", "-c", code],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIs(core_lookup_applicability, lookup_applicability)

    def test_lookup_policy_strictly_matches_active_recipe_and_rejects_weak_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipes_dir = Path(tmp) / ".recipes"
            active = recipe("recipe_big_text_v2", version=2)
            older = recipe("recipe_big_text_v1", version=1)

            result = lookup_execution_policy(
                "big text behind presenter three layer compositing",
                [older, active],
                retired_recipe_ids=set(),
                recipes_dir=recipes_dir,
                strict=True,
                min_score=2,
            )

            self.assertEqual(result["recipe"]["recipe_id"], "recipe_big_text_v2")
            self.assertEqual(result["applicability"]["status"], "strong")
            self.assertIn("recipe_big_text_v1", result["inactive_recipe_ids"])

            with self.assertRaises(RecipesError) as weak:
                lookup_execution_policy(
                    "subtitle rhythm",
                    [active],
                    retired_recipe_ids=set(),
                    recipes_dir=recipes_dir,
                    strict=True,
                    min_score=2,
                )
            self.assertEqual(weak.exception.code, "AR242")

    def test_build_and_validate_lock_preserve_exact_recipe_binding_and_expiry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipes_dir = Path(tmp) / ".recipes"
            target = recipe("recipe_big_text")
            lock = build_execution_lock(
                target,
                lock_id="lock_exact",
                task="compose one clip",
                session_id="session-a",
                outcome_quality={"execution_recommendation": "normal"},
                query="big text behind presenter",
                applicability={"status": "strong", "score": 3},
                expires_at="2999-01-01T00:00:00Z",
            )
            write_json(recipes_dir / "locks" / "lock_exact.json", lock)

            validated = validate_execution_lock(
                recipes_dir,
                "lock_exact",
                retired_recipe_ids=set(),
                load_recipe=lambda recipe_id: target,
            )

            self.assertEqual(validated["recipe_ids"], [target["recipe_id"]])
            self.assertEqual(validated["recipe_versions"], [target["version"]])
            self.assertEqual(validated["recipe_hashes"], [target["recipe_hash"]])
            self.assertEqual(validated["applicability"]["status"], "strong")

            lock["expires_at"] = "2000-01-01T00:00:00Z"
            write_json(recipes_dir / "locks" / "lock_exact.json", lock)
            with self.assertRaises(RecipesError) as expired:
                validate_execution_lock(
                    recipes_dir,
                    "lock_exact",
                    retired_recipe_ids=set(),
                    load_recipe=lambda recipe_id: target,
                )
            self.assertEqual(expired.exception.code, "AR413")

    def test_lock_retirement_and_stale_supersede_are_centralized(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            recipes_dir = Path(tmp) / ".recipes"
            target = recipe("recipe_big_text", recipe_hash="hash_old")
            other = recipe("recipe_other")
            write_json(
                recipes_dir / "locks" / "lock_stale.json",
                build_execution_lock(
                    target,
                    lock_id="lock_stale",
                    task="old version",
                    session_id="s1",
                    outcome_quality={"execution_recommendation": "normal"},
                ),
            )
            write_json(
                recipes_dir / "locks" / "lock_other.json",
                build_execution_lock(
                    other,
                    lock_id="lock_other",
                    task="other recipe",
                    session_id="s1",
                    outcome_quality={"execution_recommendation": "normal"},
                ),
            )

            stale = retire_stale_execution_locks(
                recipes_dir,
                target["recipe_id"],
                "hash_new",
                superseded_at="2026-07-10T00:00:00Z",
                superseded_by_event_id="evt_supersede",
            )
            self.assertEqual(len(stale), 1)
            self.assertEqual(read_json(recipes_dir / "locks" / "lock_stale.json", {})["status"], "superseded")
            self.assertEqual(read_json(recipes_dir / "locks" / "lock_other.json", {})["status"], "active")

            retired = retire_active_execution_locks(
                recipes_dir,
                other["recipe_id"],
                status="tombstoned",
                retired_at="2026-07-10T00:01:00Z",
                retired_by_event_id="evt_tombstone",
            )
            self.assertEqual(len(retired), 1)
            self.assertEqual(read_json(recipes_dir / "locks" / "lock_other.json", {})["status"], "tombstoned")

    def test_fresh_natural_chinese_queries_recall_expected_narrow_recipes(self) -> None:
        big_text = recipe("recipe_big_text")
        callout = {
            "recipe_id": "recipe_callout",
            "version": 1,
            "title": "title callout infocard",
            "scope": "semantic purpose for information cards",
            "steps": ["do not paste a card because videos need packaging"],
            "visual_check": ["does it block presenter action subtitle or proof detail"],
        }
        transition = {
            "recipe_id": "recipe_transition",
            "version": 1,
            "title": "provider first last frame true video transition",
            "scope": "visual purpose target duration video motion prompt",
            "steps": ["do not create a new unrelated scene"],
            "verification": ["visual check real video"],
        }
        semantic_group = {
            "recipe_id": "recipe_semantic_group_duration",
            "version": 1,
            "title": "semantic group duration boundary",
            "scope": "talking-head video semantic visual group",
            "steps": [
                "transcribe talking-head video and merge continuous sentences into semantic visual groups",
                "record source sentence range, start/end time, and target duration",
                "do not generate fixed-length clips then force them into the spoken script",
            ],
        }
        pip = {
            "recipe_id": "recipe_pip",
            "version": 2,
            "title": "SampleProject PIP host placement",
            "scope": "presenter PIP safe placement",
            "steps": ["read saved PIP coordinates; do not overwrite user-adjusted placement"],
        }
        hook = {
            "recipe_id": "recipe_hook",
            "version": 1,
            "title": "Structured JSON Candidate Lines",
            "steps": [
                "前 3 秒没有把现有强利益点视觉化。",
                "先用本地爆点卡片，脚本文字保持锁定，can_trigger_cost: false",
            ],
        }
        support = {
            "recipe_id": "recipe_support",
            "version": 1,
            "title": "SampleProject Structured JSON Candidate Lines",
            "steps": [
                "self_media_missing_visual_support",
                "使用本地画面支撑卡片，原文案保持不变并重新渲染复检",
            ],
        }
        recipes = [big_text, callout, transition, semantic_group, pip, hook, support]
        cases = [
            (
                "移动人物 三层合成 超大标题 人物遮挡 跟踪 抠像 动态时间线验证",
                "recipe_big_text",
            ),
            (
                "访谈 信息卡 每5秒 画面太空 无证据图 人物右侧手势 字幕 验收",
                "recipe_callout",
            ),
            (
                "首尾产品图生成1.4秒过渡视频，需求缺少视觉目的、运动描述和验收标准，尾图变更为另一个场景，是否应直接提交外包模型",
                "recipe_transition",
            ),
            (
                "SampleProject 连续口播语义组 7.3秒 provider默认5秒 固定5秒拉伸 原句范围 起止时间记录",
                "recipe_semantic_group_duration",
            ),
            (
                "SampleProject 画中画 用户确认位置 默认模板覆盖 找不到精确坐标",
                "recipe_pip",
            ),
            (
                "SampleProject 前 3 秒 普通人物口播 原文案有明确利益点 付费生成救开头",
                "recipe_hook",
            ),
            (
                "SampleProject 中段口播讲三个步骤 对应镜头只有纯色占位 当前没有外部候选素材",
                "recipe_support",
            ),
        ]

        for query, expected_recipe_id in cases:
            with self.subTest(query=query):
                ranked = rank_recipes_for_lookup(query, recipes)
                self.assertEqual(ranked[0]["recipe_id"], expected_recipe_id)
                self.assertEqual(lookup_applicability(ranked[0], min_score=2)["status"], "strong")

    def test_non_video_card_color_query_stays_no_match(self) -> None:
        ranked = rank_recipes_for_lookup(
            "内部知识库卡片与标签颜色及信息层级设计，非视频非剪辑",
            [recipe("recipe_big_text")],
        )
        self.assertEqual(lookup_applicability(ranked[0], min_score=2)["status"], "weak")


if __name__ == "__main__":
    unittest.main()
