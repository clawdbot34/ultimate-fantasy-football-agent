from __future__ import annotations

import json
import itertools
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from examples.draft_demo import synthetic_board, synthetic_settings
from scripts.draft_session import (
    ALL_POSITIONS,
    DraftConflictError,
    build_pick_window,
    can_finish_roster,
    draft_state_digest,
    is_legal_roster_counts,
    legal_final_shapes,
    load_draft_picks,
    pick_coordinates,
    rank_recommendations,
    recommend_from_connection,
    record_pick,
    simulate_draft,
)


ROOT = Path(__file__).resolve().parents[1]


class DraftSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "draft.sqlite3"
        self.settings = synthetic_settings()
        self.board = synthetic_board()
        connection = sqlite3.connect(self.db_path)
        connection.executescript((ROOT / "db" / "schema.sql").read_text(encoding="utf-8"))
        connection.execute(
            """
            INSERT INTO leagues (
                league_key, platform, platform_league_id, season, name, timezone,
                settings_json, observed_at
            ) VALUES ('league-test', 'synthetic', 'league-1', 2026, 'Test League',
                      'UTC', ?, '2026-08-30T00:00:00Z')
            """,
            (json.dumps(self.settings),),
        )
        for slot in range(1, 13):
            connection.execute(
                """
                INSERT INTO teams (
                    team_key, league_key, platform_team_id, name, is_user_team, observed_at
                ) VALUES (?, 'league-test', ?, ?, ?, '2026-08-30T00:00:00Z')
                """,
                (f"team-{slot}", str(slot), f"Team {slot}", int(slot == 1)),
            )
        connection.executemany(
            """
            INSERT INTO players (player_key, canonical_name, position, nfl_team, active, metadata_json, updated_at)
            VALUES (?, ?, ?, 'TST', 1, '{}', '2026-08-30T00:00:00Z')
            """,
            [
                (player["player_key"], player["name"], player["position"])
                for player in self.board["players"]
            ],
        )
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def test_snake_coordinates_reverse_even_rounds(self) -> None:
        self.assertEqual((1, 1, 1), pick_coordinates(1, 12))
        self.assertEqual((1, 12, 12), pick_coordinates(12, 12))
        self.assertEqual((2, 1, 12), pick_coordinates(13, 12))
        self.assertEqual((2, 12, 1), pick_coordinates(24, 12))

    def test_legal_shapes_obey_starters_flex_and_limits(self) -> None:
        shapes = legal_final_shapes(self.settings)
        self.assertGreater(len(shapes), 1)
        self.assertTrue(all(is_legal_roster_counts(shape, self.settings) for shape in shapes))
        partial = {position: 0 for position in ALL_POSITIONS}
        partial.update({"QB": 2, "RB": 6, "WR": 2, "TE": 2, "K": 1, "DEF": 1})
        self.assertTrue(can_finish_roster(partial, self.settings, shapes))
        partial["QB"] = 3
        self.assertFalse(can_finish_roster(partial, self.settings, shapes))

    def test_duplicate_pick_is_noop_and_conflict_fails_closed(self) -> None:
        connection = self.connect()
        try:
            first = record_pick(
                connection,
                league_key="league-test",
                season=2026,
                overall_pick=1,
                player_key="player-001",
            )
            duplicate = record_pick(
                connection,
                league_key="league-test",
                season=2026,
                overall_pick=1,
                player_key="player-001",
            )
            self.assertEqual("recorded", first["status"])
            self.assertEqual("duplicate", duplicate["status"])
            with self.assertRaises(DraftConflictError):
                record_pick(
                    connection,
                    league_key="league-test",
                    season=2026,
                    overall_pick=1,
                    player_key="player-002",
                )
            self.assertEqual(1, len(load_draft_picks(connection, "league-test", 2026)))
        finally:
            connection.close()

    def test_recommender_returns_primary_plus_three_legal_options(self) -> None:
        cards = rank_recommendations(self.board, [], user_slot=1, settings=self.settings)
        self.assertEqual(4, len(cards))
        self.assertEqual(1, cards[0]["target_overall"])
        self.assertEqual(1, cards[0]["custom_rank"])
        self.assertEqual(set(ALL_POSITIONS), set(cards[0]["resulting_roster"]))
        self.assertTrue(all(card["rationale"].endswith(".") for card in cards))

    def test_on_deck_recommendation_targets_upcoming_user_pick(self) -> None:
        cards = rank_recommendations(
            self.board,
            [],
            user_slot=7,
            settings=self.settings,
            target_overall=7,
        )
        self.assertEqual(4, len(cards))
        self.assertTrue(all(card["target_overall"] == 7 for card in cards))

    def test_persisted_recommendation_resumes_identically(self) -> None:
        connection = self.connect()
        try:
            before = recommend_from_connection(
                connection,
                board=self.board,
                settings=self.settings,
                season=2026,
                user_slot=1,
            )
            state_before = draft_state_digest(load_draft_picks(connection, "league-test", 2026))
        finally:
            connection.close()
        connection = self.connect()
        try:
            after = recommend_from_connection(
                connection,
                board=self.board,
                settings=self.settings,
                season=2026,
                user_slot=1,
            )
            state_after = draft_state_digest(load_draft_picks(connection, "league-test", 2026))
        finally:
            connection.close()
        self.assertEqual(state_before, state_after)
        self.assertEqual(before["recommendation_id"], after["recommendation_id"])
        self.assertEqual(before["primary"], after["primary"])
        self.assertEqual(before["alternatives"], after["alternatives"])

    def test_simulation_is_deterministic_and_all_rosters_are_legal(self) -> None:
        first = simulate_draft(self.board, self.settings, user_slot=6, seed=20260908)
        second = simulate_draft(self.board, self.settings, user_slot=6, seed=20260908)
        self.assertTrue(first["all_legal"])
        self.assertEqual(first["picks"], second["picks"])
        self.assertEqual(168, len(first["picks"]))
        first_six = [
            pick["position"] for pick in first["picks"] if pick["team_slot"] == 6
        ][:6]
        self.assertLessEqual(first_six.count("RB"), 4)
        self.assertLessEqual(first_six.count("WR"), 4)

    def test_every_starting_slot_finishes_legally(self) -> None:
        for slot in range(1, 13):
            with self.subTest(slot=slot):
                result = simulate_draft(self.board, self.settings, user_slot=slot, seed=20260908)
                self.assertTrue(result["all_legal"])
                self.assertTrue(result["legal_rosters"][slot])

    def test_ten_recommendations_each_finish_under_two_seconds(self) -> None:
        timings = []
        for _ in range(10):
            started = time.perf_counter()
            cards = rank_recommendations(self.board, [], user_slot=1, settings=self.settings)
            timings.append(time.perf_counter() - started)
            self.assertEqual(4, len(cards))
        self.assertTrue(all(elapsed < 2.0 for elapsed in timings), timings)

    def test_window_prunes_observed_picks_and_freezes_two_out(self) -> None:
        draft = simulate_draft(self.board, self.settings, user_slot=7, seed=42)
        picks = draft["picks"][:4]
        window = build_pick_window(self.board, picks, user_slot=7, settings=self.settings)
        self.assertEqual("freeze", window["phase"])
        self.assertEqual(2, window["opponents_before"])
        self.assertEqual(10, len(window["ladder"]))
        self.assertEqual([], window["corner_pairs"])
        self.assertTrue(window["covered_until_turn"])
        self.assertTrue({p["player_key"] for p in picks}.isdisjoint(c["player_key"] for c in window["ladder"]))
        changed = build_pick_window(self.board, draft["picks"][:6], user_slot=7, settings=self.settings)
        self.assertEqual("on_clock", changed["phase"])
        self.assertNotEqual(window["state_digest"], changed["state_digest"])

    def test_corner_pairs_survive_any_two_intervening_selections(self) -> None:
        draft = simulate_draft(self.board, self.settings, user_slot=12, seed=42)
        window = build_pick_window(self.board, draft["picks"][:9], user_slot=12, settings=self.settings)
        self.assertEqual(12, window["target_overall"])
        self.assertEqual(13, window["next_user_pick"])
        pairs = window["corner_pairs"]
        self.assertEqual(10, len(pairs))
        self.assertGreaterEqual(window["disjoint_pair_count"], 3)
        self.assertTrue(window["covered_until_turn"])
        self.assertTrue(all(len(set(p["player_keys"])) == 2 and can_finish_roster(p["resulting_roster"], self.settings) for p in pairs))
        universe = {key for pair in pairs for key in pair["player_keys"]}
        for removed in itertools.combinations(universe, 2):
            self.assertTrue(any(set(removed).isdisjoint(pair["player_keys"]) for pair in pairs))

    def test_final_corner_pairs_fill_both_missing_positions(self) -> None:
        # At picks 156/157 our twelve players lack both TE and K. A naive
        # independent top-ten list can contain ten TEs and produce an illegal pair.
        wanted = {"QB": 1, "RB": 5, "WR": 5, "DEF": 1}
        owned = []
        for position, count in wanted.items():
            owned.extend([p for p in self.board["players"] if p["position"] == position][:count])
        owned_keys = {p["player_key"] for p in owned}
        other = iter(p for p in self.board["players"] if p["player_key"] not in owned_keys)
        ours = iter(owned)
        picks = []
        for overall in range(1, 156):
            player = next(ours) if pick_coordinates(overall, 12)[2] == 12 else next(other)
            picks.append({"overall_pick": overall, "player_key": player["player_key"]})
        window = build_pick_window(self.board, picks, user_slot=12, settings=self.settings)
        self.assertTrue(window["corner_pairs"])
        for pair in window["corner_pairs"]:
            self.assertEqual({"TE", "K"}, set(pair["positions"]))
            self.assertTrue(is_legal_roster_counts(pair["resulting_roster"], self.settings))

    def test_short_legal_pool_is_reported_without_fabricated_alternatives(self) -> None:
        small = {**self.board, "players": self.board["players"][:3]}
        window = build_pick_window(small, [], user_slot=7, settings=self.settings)
        self.assertEqual(3, len(window["ladder"]))
        self.assertFalse(window["covered_until_turn"])
        self.assertTrue(window["warnings"])


if __name__ == "__main__":
    unittest.main()
