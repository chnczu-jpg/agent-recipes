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


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def seed_deepseek_replay(project: Path) -> tuple[Path, Path]:
    fixture_dir = project / "fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    source = fixture_dir / "deepseek_source.md"
    source.write_text(
        "\n\n".join(
            [
                "agent claimed cloud model output was verified truth without review.",
                "The replacement path is candidate card -> patch draft -> review accept.",
                "Every card must keep source_trace, target_fields, evidence_strength, and cannot_claim.",
            ]
        ),
        encoding="utf-8",
    )
    replay = fixture_dir / "deepseek_response.json"
    replay.write_text(
        json.dumps(
            {
                "cards": [
                    {
                        "card_type": "correction_card",
                        "target_fields": ["forbidden_path", "cannot_claim"],
                        "extracted_payload": {
                            "before": ["cloud output treated as verified truth"],
                            "correction": ["cloud output stays candidate until review accept"],
                            "after": ["write candidate card and review queue item first"],
                            "cannot_claim": ["cannot say DeepSeek output modified formal recipe"],
                        },
                        "source_quote": "cloud model output was verified truth without review",
                        "cannot_claim": ["cannot say DeepSeek output modified formal recipe"],
                    },
                    {
                        "card_type": "run_chain_card",
                        "target_fields": ["verified_path", "cannot_claim"],
                        "extracted_payload": {
                            "inputs": ["OCR/ASR/document text"],
                            "steps": ["cloud refine", "patch draft", "review accept"],
                            "outputs": ["candidate cards"],
                            "verification": ["doctor reports cloud adapter candidate-only"],
                            "cannot_claim": ["cannot say replay equals live DeepSeek API"],
                        },
                        "source_quote": "candidate card -> patch draft -> review accept",
                        "cannot_claim": ["cannot say replay equals live DeepSeek API"],
                    },
                    {
                        "card_type": "failure_card",
                        "target_fields": ["failure_signals", "forbidden_path"],
                        "extracted_payload": {
                            "failed_path": ["direct recipe write from cloud output"],
                            "failure_signal": ["formal recipe changed without review"],
                            "replacement_path": ["review_queue before formal recipe"],
                            "cannot_claim": ["cannot say cloud result is verified"],
                        },
                        "source_quote": "without review",
                        "cannot_claim": ["cannot say cloud result is verified"],
                    },
                    {
                        "card_type": "learning_atom_card",
                        "target_fields": ["checklist_item", "cannot_claim"],
                        "extracted_payload": {
                            "action_change": ["keep source_trace on every card"],
                            "checklist_item": ["verify source_trace exists before patch draft"],
                            "good_example": ["card has source file and provider metadata"],
                            "bad_example": ["card has model text only"],
                            "cannot_claim": ["cannot say card has been user accepted"],
                        },
                        "source_quote": "Every card must keep source_trace",
                        "cannot_claim": ["cannot say card has been user accepted"],
                    },
                    {
                        "card_type": "visual_example_card",
                        "target_fields": ["visual_check", "cannot_claim"],
                        "extracted_payload": {
                            "visual_check": ["vision is not handled by DeepSeek text model; OCR/ASR must run first"],
                            "good_example": ["OCR text enters DeepSeek with source_trace"],
                            "bad_example": ["send raw image to text-only API"],
                            "cannot_claim": ["cannot say DeepSeek text adapter saw the original pixels"],
                        },
                        "source_quote": "OCR/ASR/document text",
                        "cannot_claim": ["cannot say DeepSeek text adapter saw the original pixels"],
                    },
                ]
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return source, replay


class Phase8DeepSeekCloudTest(unittest.TestCase):
    def test_deepseek_cloud_config_status_doctor_and_secret_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            rejected = run_cli(
                project,
                "cloud-configure",
                "--provider",
                "deepseek",
                "--api-key-env",
                "rk-test-should-not-be-written",
                expect_ok=False,
            )
            self.assertEqual(rejected["code"], "AR360")

            rejected_host = run_cli(
                project,
                "cloud-configure",
                "--provider",
                "deepseek",
                "--base-url",
                "https://api.deepseek.com.evil.test",
                expect_ok=False,
            )
            self.assertEqual(rejected_host["code"], "AR360")

            configured = run_cli(
                project,
                "cloud-configure",
                "--provider",
                "deepseek",
                "--model",
                "deepseek-v4-flash",
                "--base-url",
                "https://api.deepseek.com",
                "--api-key-env",
                "AGENT_RECIPES_DEEPSEEK_API_KEY",
            )

            self.assertEqual(configured["action"], "cloud-configure")
            self.assertEqual(configured["provider"], "deepseek")
            self.assertEqual(configured["config_status"], "configured")
            self.assertTrue(configured["candidate_only"])
            self.assertFalse(configured["api_key_present"])
            self.assertFalse(configured["vision_supported"])
            self.assertIn("不能说 DeepSeek cloud adapter 已经调用成功。", configured["claim_status"]["cannot_claim"])

            config_path = project / ".recipes" / "cloud" / "deepseek" / "config.json"
            config_text = config_path.read_text(encoding="utf-8")
            self.assertNotIn("sk-", config_text)
            config = json.loads(config_text)
            self.assertEqual(config["api_key_env"], "AGENT_RECIPES_DEEPSEEK_API_KEY")
            self.assertNotIn("api_key", config)

            status = run_cli(project, "cloud-status", "--provider", "deepseek")
            self.assertEqual(status["cloud_adapters"]["deepseek"]["config_status"], "configured")
            self.assertFalse(status["cloud_adapters"]["deepseek"]["api_key_present"])
            self.assertTrue(status["cloud_adapters"]["deepseek"]["candidate_only"])
            self.assertFalse(status["cloud_adapters"]["deepseek"]["vision_supported"])

            capabilities = run_cli(project, "capabilities")
            self.assertEqual(capabilities["cloud_adapters"]["deepseek"]["config_status"], "configured")
            self.assertFalse(capabilities["cloud_adapters"]["deepseek"]["api_key_present"])

            doctor = run_cli(project, "doctor")
            self.assertEqual(doctor["summary"]["cloud_adapters"]["deepseek"]["config_status"], "configured")
            self.assertIn("不能说 DeepSeek cloud adapter 输出已经自动进入正式 recipe。", doctor["claim_status"]["cannot_claim"])

    def test_deepseek_cloud_refine_replay_generates_cards_and_review_gated_patch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            source, replay = seed_deepseek_replay(project)
            run_cli(project, "init")
            run_cli(
                project,
                "cloud-configure",
                "--provider",
                "deepseek",
                "--model",
                "deepseek-v4-flash",
                "--base-url",
                "https://api.deepseek.com",
                "--api-key-env",
                "AGENT_RECIPES_DEEPSEEK_API_KEY",
            )

            refined = run_cli(
                project,
                "cloud-refine",
                "--provider",
                "deepseek",
                "--input",
                str(source.relative_to(project)),
                "--response-json",
                str(replay.relative_to(project)),
                "--knowledge-need",
                "KN_DEEPSEEK_TEXT_BRAIN",
                "--target-recipe",
                "recipe_deepseek_text_brain_v0",
                "--candidate-fields",
                "forbidden_path,failure_signals,verified_path,checklist_item,visual_check,cannot_claim",
            )

            self.assertEqual(refined["action"], "cloud-refine")
            self.assertEqual(refined["provider"], "deepseek")
            self.assertEqual(refined["execution_mode"], "replay")
            self.assertEqual(refined["card_count"], 5)
            self.assertEqual(set(refined["card_counts"]), {"correction_card", "run_chain_card", "failure_card", "learning_atom_card", "visual_example_card"})
            self.assertIn("不能说 replay 响应等于真实 DeepSeek API 已调用。", refined["claim_status"]["cannot_claim"])

            cards = read_jsonl(project / ".recipes" / "source_refinery" / "cards" / "cards.jsonl")
            self.assertEqual(len(cards), 5)
            for card in cards:
                self.assertEqual(card["provider"], "deepseek")
                self.assertEqual(card["evidence_strength"], "candidate")
                self.assertTrue(card["source_trace"])
                self.assertTrue(card["target_fields"])
                self.assertTrue(card["cannot_claim"])

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_deepseek_text_brain_v0")
            self.assertTrue((project / ".recipes" / "source_refinery" / "patch_drafts" / f"{drafted['patch_draft_id']}.json").exists())
            self.assertTrue((project / ".recipes" / "review_queue" / f"{drafted['review_id']}.json").exists())
            self.assertFalse((project / ".recipes" / "recipes" / "recipe_deepseek_text_brain_v0.json").exists())

            accepted = run_cli(project, "review", "--accept", drafted["review_id"])
            self.assertEqual(accepted["recipe_id"], "recipe_deepseek_text_brain_v0")
            recipe = json.loads(
                (project / ".recipes" / "recipes" / "recipe_deepseek_text_brain_v0.json").read_text(encoding="utf-8")
            )
            self.assertIn("cloud output stays candidate", recipe["title"])
            self.assertIn("review accept", " ".join(recipe["steps"]))
            self.assertIn("verify source_trace", " ".join(recipe["checklist_item"]))
            self.assertIn("direct recipe write", " ".join(recipe["forbidden_path"]))
            self.assertFalse(any(item.startswith("correction_card: card_") for item in recipe["steps"]))

            status = run_cli(project, "cloud-status", "--provider", "deepseek")
            self.assertEqual(status["cloud_adapters"]["deepseek"]["runtime_events"], 1)
            self.assertEqual(status["cloud_adapters"]["deepseek"]["last_execution_mode"], "replay")

    def test_deepseek_cloud_refine_replaces_placeholder_cannot_claim(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            source = fixture_dir / "placeholder_claim_source.md"
            source.write_text("Use review_queue before formal recipe changes.", encoding="utf-8")
            replay = fixture_dir / "placeholder_claim_response.json"
            replay.write_text(
                json.dumps(
                    {
                        "cards": [
                            {
                                "card_type": "learning_atom_card",
                                "target_fields": ["checklist_item", "cannot_claim"],
                                "extracted_payload": {
                                    "checklist_item": ["use review_queue before formal recipe changes"],
                                    "cannot_claim": False,
                                },
                                "source_quote": "Use review_queue before formal recipe changes.",
                                "cannot_claim": False,
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            run_cli(project, "init")
            run_cli(
                project,
                "cloud-configure",
                "--provider",
                "deepseek",
                "--model",
                "deepseek-v4-flash",
                "--base-url",
                "https://api.deepseek.com",
                "--api-key-env",
                "AGENT_RECIPES_DEEPSEEK_API_KEY",
            )

            run_cli(
                project,
                "cloud-refine",
                "--provider",
                "deepseek",
                "--input",
                str(source.relative_to(project)),
                "--response-json",
                str(replay.relative_to(project)),
                "--knowledge-need",
                "KN_PLACEHOLDER_CLAIM",
                "--target-recipe",
                "recipe_placeholder_claim_v0",
                "--candidate-fields",
                "checklist_item,cannot_claim",
            )

            cards = read_jsonl(project / ".recipes" / "source_refinery" / "cards" / "cards.jsonl")
            self.assertEqual(len(cards), 1)
            self.assertEqual(cards[0]["cannot_claim"], ["不能说 cloud adapter 输出已经进入正式 recipe。"])
            self.assertNotIn("False", json.dumps(cards[0]["extracted_payload"], ensure_ascii=False))

    def test_patch_draft_source_quote_only_card_still_generates_human_recipe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            fixture_dir = project / "fixtures"
            fixture_dir.mkdir(parents=True, exist_ok=True)
            source = fixture_dir / "deepseek_live_like_source.md"
            source.write_text(
                "DeepSeek cloud text adapter should treat cloud output as candidate evidence only.\n"
                "It must not write formal recipes directly.\n"
                "The review_queue must accept a patch draft before a formal recipe can be created.\n",
                encoding="utf-8",
            )
            replay = fixture_dir / "deepseek_live_like_response.json"
            replay.write_text(
                json.dumps(
                    {
                        "cards": [
                            {
                                "card_type": "learning_atom_card",
                                "target_fields": ["checklist_item"],
                                "extracted_payload": {
                                    "source_quote": [
                                        "checklist_item: BGM 要先看文案、内容和情绪，再选。 - SFX 只给关键词、动作、切点、情绪转折服务。"
                                    ],
                                    "cannot_claim": [
                                        "This card is candidate-only guidance."
                                    ],
                                },
                                "cannot_claim": [
                                    "This card is candidate-only guidance."
                                ],
                            },
                            {
                                "card_type": "learning_atom_card",
                                "target_fields": ["checklist_item"],
                                "extracted_payload": {
                                    "source_quote": [
                                        "- 如果 BGM 已经承担节奏，SFX 要减少。\n- 音效有三个工作：补场景、补情绪、补节奏；没有工作就删。"
                                    ],
                                    "cannot_claim": [
                                        "This card is candidate-only guidance."
                                    ],
                                },
                                "cannot_claim": [
                                    "This card is candidate-only guidance."
                                ],
                            },
                            {
                                "card_type": "failure_card",
                                "target_fields": ["failure_signals"],
                                "extracted_payload": {
                                    "source_quote": [
                                        "failure_signals:\n- 没有 SampleProject-applied output 的人耳听感证据。"
                                    ],
                                    "cannot_claim": [
                                        "This failure signal is candidate-only guidance."
                                    ],
                                },
                                "cannot_claim": [
                                    "This failure signal is candidate-only guidance."
                                ],
                            },
                            {
                                "card_type": "learning_atom_card",
                                "target_fields": ["forbidden_path", "cannot_claim"],
                                "extracted_payload": {
                                    "source_quote": [
                                        "DeepSeek cloud text adapter should treat cloud output as candidate evidence only.\n"
                                        "It must not write formal recipes directly.\n"
                                        "The review_queue must accept a patch draft before a formal recipe can be created."
                                    ],
                                    "cannot_claim": [
                                        "This card does not claim formal recipe changes; it is candidate-only guidance."
                                    ],
                                },
                                "source_quote": "It must not write formal recipes directly.",
                                "cannot_claim": [
                                    "This card does not claim formal recipe changes; it is candidate-only guidance."
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

            run_cli(project, "init")
            run_cli(
                project,
                "cloud-configure",
                "--provider",
                "deepseek",
                "--model",
                "deepseek-v4-flash",
                "--base-url",
                "https://api.deepseek.com",
                "--api-key-env",
                "AGENT_RECIPES_DEEPSEEK_API_KEY",
            )
            run_cli(
                project,
                "cloud-refine",
                "--provider",
                "deepseek",
                "--input",
                str(source.relative_to(project)),
                "--response-json",
                str(replay.relative_to(project)),
                "--knowledge-need",
                "KN_DEEPSEEK_LIVE_LIKE",
                "--target-recipe",
                "recipe_deepseek_live_like_v0",
                "--candidate-fields",
                "forbidden_path,checklist_item,failure_signals,cannot_claim",
            )

            drafted = run_cli(project, "patch-draft", "--target-recipe", "recipe_deepseek_live_like_v0")
            run_cli(project, "review", "--accept", drafted["review_id"])
            recipe = json.loads(
                (project / ".recipes" / "recipes" / "recipe_deepseek_live_like_v0.json").read_text(encoding="utf-8")
            )

            self.assertIn("Deepseek Live Like", recipe["title"])
            self.assertIn("BGM 要先看文案、内容和情绪，再选。", " ".join(recipe["checklist_item"]))
            self.assertIn("SFX 只给关键词、动作、切点、情绪转折服务。", " ".join(recipe["checklist_item"]))
            self.assertIn("如果 BGM 已经承担节奏，SFX 要减少。", " ".join(recipe["checklist_item"]))
            self.assertIn("音效有三个工作：补场景、补情绪、补节奏；没有工作就删。", " ".join(recipe["checklist_item"]))
            self.assertIn("没有 SampleProject-applied output 的人耳听感证据。", " ".join(recipe["failure_signals"]))
            self.assertFalse(any(item.startswith("checklist_item:") for item in recipe["checklist_item"]))
            self.assertFalse(any(item.startswith("failure_signals:") for item in recipe["failure_signals"]))
            self.assertIn("must not write formal recipes directly", " ".join(recipe["forbidden_path"]))
            self.assertIn("review_queue must accept", " ".join(recipe["forbidden_path"]))
            self.assertFalse(any(item.startswith("learning_atom_card: card_") for item in recipe["steps"]))

    def test_mcp_exposes_deepseek_cloud_tools(self) -> None:
        tool_names = [tool["name"] for tool in tool_list()]

        self.assertIn("agent_recipes_cloud_configure", tool_names)
        self.assertIn("agent_recipes_cloud_status", tool_names)
        self.assertIn("agent_recipes_cloud_refine", tool_names)

        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            run_cli(project, "init")

            configured = call_tool(
                "cloud_configure",
                {
                    "project": str(project),
                    "provider": "deepseek",
                    "model": "deepseek-v4-pro",
                    "base_url": "https://api.deepseek.com",
                    "api_key_env": "AGENT_RECIPES_DEEPSEEK_API_KEY",
                },
            )

            self.assertEqual(configured["tool"], "agent_recipes_cloud_configure")
            self.assertEqual(configured["provider"], "deepseek")
            self.assertEqual(configured["config_status"], "configured")


if __name__ == "__main__":
    unittest.main()
