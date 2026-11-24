from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_draft_board import canonical_digest, write_csv, write_html, write_json
from scripts.draft_model import (
    allocate_draft_pool,
    assign_position_tiers,
    points_allowed_score,
    score_defense,
    score_kicker,
    score_offense,
    yards_allowed_score,
)


def scoring_fixture() -> dict:
    return {
        "passing": {
            "yard": 0.04,
            "touchdown": 5,
            "interception": -2,
            "two_point_conversion": 2,
            "sack_taken": -1,
            "touchdown_40_plus_bonus": 1,
            "touchdown_50_plus_bonus": 2,
            "yards_300_399_bonus": 2,
            "yards_400_plus_bonus": 4,
        },
        "rushing": {
            "yard": 0.1,
            "touchdown": 6,
            "two_point_conversion": 2,
            "touchdown_40_plus_bonus": 1,
            "touchdown_50_plus_bonus": 1,
            "yards_100_199_bonus": 2,
            "yards_200_plus_bonus": 4,
        },
        "receiving": {
            "reception": 1,
            "yard": 0.1,
            "touchdown": 6,
            "two_point_conversion": 2,
            "touchdown_40_plus_bonus": 1,
            "touchdown_50_plus_bonus": 2,
            "yards_100_199_bonus": 2,
            "yards_200_plus_bonus": 4,
        },
        "miscellaneous": {
            "fumble_lost": -2,
            "kickoff_return_touchdown": 6,
        },
        "kicking": {
            "field_goal_0_39": 3,
            "field_goal_missed": -1,
            "pat_made": 1,
            "pat_missed": -2,
        },
        "dst": {
            "sack": 1,
            "interception": 2,
            "fumble_recovery": 2,
            "safety": 2,
            "return_touchdown": 6,
            "kickoff_return_yard": 0.1,
            "punt_return_yard": 0.1,
            "total_tackle": 0.01,
            "blocked_kick": 2,
        },
    }


def empty_history() -> dict:
    return {"profiles": {}, "fallback_rates": {}, "dst": {}}


class DraftScoringTests(unittest.TestCase):
    def test_offensive_direct_and_estimated_components_reconcile(self) -> None:
        history = empty_history()
        history["fallback_rates"][("QB", "sacks")] = 0.05
        score = score_offense(
            "player:qb",
            "QB",
            {
                "pass_yds": 100,
                "pass_tds": 2,
                "pass_ints": 1,
                "pass_att": 10,
                "rush_yds": 50,
                "rush_tds": 1,
                "rec_rec": 3,
                "rec_yds": 20,
                "rec_tds": 1,
                "2pt_tds": 1,
                "ret_tds": 1,
                "fumbles": 1,
            },
            scoring_fixture(),
            history,
        )
        self.assertAlmostEqual(40.0, score["direct_points"])
        self.assertAlmostEqual(0.22, score["estimated_points"])
        self.assertAlmostEqual(
            score["direct_points"] + score["estimated_points"],
            score["custom_points"],
        )

    def test_kicker_scoring_uses_distance_and_miss_estimates(self) -> None:
        history = empty_history()
        history["fallback_rates"][("K", "fg_distance")] = 0.5
        history["fallback_rates"][("K", "pat_miss")] = 0.1
        score = score_kicker(
            "player:k",
            {"fg": 10, "fga": 12, "xpt": 20},
            scoring_fixture(),
            history,
        )
        self.assertAlmostEqual(48.0, score["direct_points"])
        self.assertAlmostEqual(1.0, score["estimated_points"])
        self.assertAlmostEqual(49.0, score["custom_points"])

    def test_defense_scoring_separates_projection_and_history(self) -> None:
        history = empty_history()
        history["dst"]["SEA"] = {
            "return_yards": 1000,
            "points_allowed_points": -5,
            "yards_allowed_points": -10,
            "history_available": True,
        }
        score = score_defense(
            "SEA",
            {"def_sack": 10, "def_int": 2, "def_fr": 1, "def_safety": 1, "def_td": 2},
            scoring_fixture(),
            history,
        )
        self.assertAlmostEqual(30.0, score["direct_points"])
        self.assertAlmostEqual(96.0, score["estimated_points"])
        self.assertAlmostEqual(126.0, score["custom_points"])

    def test_allowed_bucket_boundaries(self) -> None:
        points = {
            "0": 7,
            "1_6": 5,
            "7_13": 3,
            "14_17": 1,
            "18_21": 0,
            "22_27": -1,
            "28_34": -2,
            "35_45": -4,
            "46_plus": -7,
        }
        yards = {
            "under_100": 7,
            "100_199": 3,
            "200_299": 2,
            "300_349": 0,
            "350_399": -1,
            "400_449": -3,
            "450_499": -5,
            "500_549": -6,
            "550_plus": -7,
        }
        self.assertEqual(1, points_allowed_score(17, points))
        self.assertEqual(0, points_allowed_score(18, points))
        self.assertEqual(-7, points_allowed_score(46, points))
        self.assertEqual(7, yards_allowed_score(99, yards))
        self.assertEqual(3, yards_allowed_score(100, yards))
        self.assertEqual(-7, yards_allowed_score(550, yards))

    def test_roster_allocation_reconciles_to_168_and_honors_caps(self) -> None:
        rows = []
        market_rank = 1
        for position in ("QB", "RB", "WR", "TE", "K", "DEF"):
            for index in range(100):
                rows.append(
                    {
                        "player_key": f"{position}:{index}",
                        "name": f"{position} {index}",
                        "position": position,
                        "market_rank": float(market_rank),
                        "custom_points": 500.0 - index,
                    }
                )
                market_rank += 1
        settings = {
            "team_count": 12,
            "roster": {
                "size": 14,
                "bench": 4,
                "starters": {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "K": 1, "DST": 1, "RB_WR": 1, "FLEX": 1},
                "maximums": {"QB": 2, "RB": 6, "WR": 6, "TE": 2, "K": 3, "DST": 3},
            },
        }
        pool = allocate_draft_pool(rows, settings)
        self.assertEqual(168, pool["size"])
        self.assertEqual(168, sum(pool["counts"].values()))
        self.assertLessEqual(pool["counts"]["QB"], 24)
        self.assertEqual(pool["counts"]["QB"] + 1, pool["replacements"]["QB"]["rank"])

    def test_position_tier_changes_at_meaningful_gap(self) -> None:
        rows = [
            {"position": "RB", "custom_points": 300.0, "market_rank": 1.0},
            {"position": "RB", "custom_points": 295.0, "market_rank": 2.0},
            {"position": "RB", "custom_points": 280.0, "market_rank": 3.0},
        ]
        assign_position_tiers(rows)
        self.assertEqual([1, 1, 2], [row["tier"] for row in rows])

    def test_artifacts_keep_the_same_order_and_digest(self) -> None:
        rows = [
            {
                "player_key": "fixture:1",
                "custom_rank": 1,
                "tier": 1,
                "name": " =1+1",
                "team": "SEA",
                "position": "RB",
                "bye": "<b>example</b>",
                "custom_points": 250.0,
                "direct_points": 240.0,
                "estimated_points": 10.0,
                "vorp": 80.0,
                "replacement_points": 170.0,
                "replacement_rank": 40,
                "ecr": 5.0,
                "adp": 6.0,
                "market_rank": 5.5,
                "price_gap": -4.5,
                "confidence": "High",
                "risk": "—",
                "target_range": "1–12",
                "direct_components": {"rushing": 200.0, "receiving": 40.0},
                "estimated_components": {"bonuses": 10.0},
                "notes": [],
                "history_available": True,
                "active": True,
                "consensus_tier": 1,
            }
        ]
        digest = canonical_digest(rows)
        metadata = {
            "canonical_digest": digest,
            "player_count": 1,
            "draft_pool_size": 168,
            "history_season": 2025,
            "draft_pool_position_counts": {"RB": 1},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_path, csv_path, html_path = root / "board.json", root / "board.csv", root / "board.html"
            write_json(json_path, rows, metadata)
            write_csv(csv_path, rows)
            write_html(html_path, rows, metadata)
            json_rows = json.loads(json_path.read_text(encoding="utf-8"))["players"]
            with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))
            html_text = html_path.read_text(encoding="utf-8")
        self.assertEqual([1], [row["custom_rank"] for row in json_rows])
        self.assertEqual(["1"], [row["custom_rank"] for row in csv_rows])
        self.assertIn(f'content="{digest}"', html_text)
        self.assertIn('data-position="RB"', html_text)
        self.assertEqual(" =1+1", json_rows[0]["name"])
        self.assertEqual("Text:  =1+1", csv_rows[0]["name"])
        self.assertEqual("-4.5", csv_rows[0]["price_gap"])
        self.assertIn("&lt;b&gt;example&lt;/b&gt;", html_text)
        self.assertNotIn("<b>example</b>", html_text)


if __name__ == "__main__":
    unittest.main()
