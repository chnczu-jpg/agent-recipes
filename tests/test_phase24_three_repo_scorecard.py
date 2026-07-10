import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCORECARD_PATH = ROOT / "THREE_REPO_COMPETITIVE_SCORECARD.json"


class ThreeRepoCompetitiveScorecardTest(unittest.TestCase):
    def load_scorecard(self):
        return json.loads(SCORECARD_PATH.read_text(encoding="utf-8"))

    def test_fixed_upstreams_and_license_boundaries_are_explicit(self):
        scorecard = self.load_scorecard()
        upstreams = {row["id"]: row for row in scorecard["upstreams"]}

        self.assertEqual(
            upstreams["remnic"]["commit"],
            "019518b7ba11e9582484a147420c92f387b863c6",
        )
        self.assertEqual(
            upstreams["cass"]["commit"],
            "5cefdb30ed94a06f6b2eafcad5998f5933e6528b",
        )
        self.assertEqual(
            upstreams["tencentdb_agent_memory"]["commit"],
            "4339e63650920871eb0e8888083a1779d114e3ae",
        )
        self.assertIsNone(upstreams["cass"]["local_checkout"])
        self.assertEqual(
            upstreams["cass"]["local_full_test"],
            "prohibited_by_license_boundary",
        )

    def test_claims_fail_closed_while_any_required_blocker_remains(self):
        scorecard = self.load_scorecard()
        dimensions = {row["id"]: row for row in scorecard["dimensions"]}
        verdict = scorecard["current_verdict"]

        self.assertFalse(verdict["narrowed_superiority_allowed"])
        self.assertFalse(verdict["overall_superiority_allowed"])
        for dimension_id in verdict["narrowed_blockers"]:
            row = dimensions[dimension_id]
            self.assertEqual(row["scope"], "narrowed")
            self.assertTrue(row["claim_blocker"])
            self.assertIn(row["agent_recipes_status"], {"behind", "unproven"})
        for dimension_id in verdict["overall_additional_blockers"]:
            row = dimensions[dimension_id]
            self.assertEqual(row["scope"], "overall_only")
            self.assertTrue(row["claim_blocker"])
        engineering = dimensions["engineering_maturity_and_distribution"]
        self.assertIn("upgrade/rollback", engineering["reason"])
        self.assertIn("second physical-machine", engineering["reason"])
        self.assertIn("second_physical_environment", scorecard["next_order"])

    def test_virtual_best_rows_cannot_be_reduced_to_points(self):
        scorecard = self.load_scorecard()

        self.assertTrue(scorecard["scoring_policy"]["points_are_forbidden"])
        self.assertGreaterEqual(len(scorecard["dimensions"]), 15)
        self.assertTrue(
            any(row["agent_recipes_status"] == "ahead" for row in scorecard["dimensions"])
        )
        self.assertTrue(
            any(row["agent_recipes_status"] == "behind" for row in scorecard["dimensions"])
        )

    def test_human_scorecard_and_release_allowlist_are_present(self):
        self.assertTrue((ROOT / "THREE_REPO_COMPETITIVE_SCORECARD.md").is_file())
        release_text = (ROOT / "agent_recipes" / "release.py").read_text(encoding="utf-8")
        self.assertIn('"THREE_REPO_COMPETITIVE_SCORECARD.json"', release_text)
        self.assertIn('"THREE_REPO_COMPETITIVE_SCORECARD.md"', release_text)

    def test_fresh_agent_gate_keeps_failures_and_passing_v8_visible(self):
        scorecard = self.load_scorecard()
        dimensions = {row["id"]: row for row in scorecard["dimensions"]}
        fresh = dimensions["fresh_agent_production_effect"]

        self.assertEqual(fresh["agent_recipes_status"], "ahead")
        self.assertFalse(fresh["claim_blocker"])
        self.assertNotIn("fresh_agent_production_effect", scorecard["current_verdict"]["narrowed_blockers"])
        self.assertIn("5 recipe wins, 1 tie, 0 baseline wins", fresh["reason"])
        self.assertTrue(any("blind_judgments_v5.json" in path for path in fresh["evidence"]))
        self.assertTrue(any("retest_v6_v7.json" in path for path in fresh["evidence"]))
        self.assertTrue(any("blind_judgments_v8.json" in path for path in fresh["evidence"]))

    def test_recall_quality_gate_uses_deduplicated_same_corpus_evidence(self):
        scorecard = self.load_scorecard()
        dimensions = {row["id"]: row for row in scorecard["dimensions"]}
        recall = dimensions["recall_quality_at_scale"]

        self.assertEqual(recall["agent_recipes_status"], "parity")
        self.assertFalse(recall["claim_blocker"])
        self.assertNotIn("recall_quality_at_scale", scorecard["current_verdict"]["narrowed_blockers"])
        self.assertIn("111 unique expectations", recall["reason"])
        self.assertIn("native Graphiti/Cognee", recall["reason"])
        self.assertTrue(any("recall_quality_d032c112963d.json" in path for path in recall["evidence"]))

if __name__ == "__main__":
    unittest.main()
