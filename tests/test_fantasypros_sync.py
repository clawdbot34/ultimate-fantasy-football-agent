from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.fantasypros_http import fantasypros_request
from scripts.sync_fantasypros import (
    IdentityResolver,
    finish_run,
    normalize_ecr,
    records_from_payload,
    start_run,
)


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "db" / "schema.sql"


class FantasyProsSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.connection = sqlite3.connect(Path(self.temp_dir.name) / "test.sqlite3")
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.executescript(SCHEMA.read_text(encoding="utf-8"))
        now = "2026-08-27T00:00:00Z"
        for source_key in ("fantasypros_api", "sleeper_api"):
            self.connection.execute(
                """
                INSERT INTO data_sources (
                    source_key, name, category, priority_tier, access_method,
                    enabled, updated_at
                ) VALUES (?, ?, 'test', 1, 'test', 1, ?)
                """,
                (source_key, source_key, now),
            )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp_dir.cleanup()

    def add_player(
        self,
        player_key: str,
        name: str,
        position: str,
        team: str,
        sportsdata_id: str | None = None,
        sportradar_id: str | None = None,
    ) -> None:
        external_ids = {}
        if sportsdata_id:
            external_ids["fantasy_data_id"] = sportsdata_id
        if sportradar_id:
            external_ids["sportradar_id"] = sportradar_id
        metadata = {"external_ids": external_ids} if external_ids else {}
        self.connection.execute(
            """
            INSERT INTO players (
                player_key, canonical_name, position, nfl_team, metadata_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, '2026-08-27T00:00:00Z')
            """,
            (player_key, name, position, team, json.dumps(metadata)),
        )
        self.connection.execute(
            """
            INSERT INTO player_ids (source_key, external_id, player_key, observed_at)
            VALUES ('sleeper_api', ?, ?, '2026-08-27T00:00:00Z')
            """,
            (player_key.split(":", 1)[-1], player_key),
        )
        self.connection.commit()

    def test_api_key_is_sent_only_on_the_original_https_request(self) -> None:
        api_key = "test-value"
        request = fantasypros_request(
            "https://api.fantasypros.com/public/v2/json/nfl/players", api_key, user_agent="test"
        )
        self.assertEqual(api_key, request.get_header("X-api-key"))
        self.assertNotIn("X-api-key", request.headers)
        for url in ("http://api.fantasypros.com/", "https://example.invalid/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                fantasypros_request(url, api_key, user_agent="test")

    def test_stable_id_match_registers_once(self) -> None:
        self.add_player(
            "sleeper:101",
            "Example Runner",
            "RB",
            "SEA",
            sportradar_id="stable-9001",
        )
        resolver = IdentityResolver(self.connection)
        record = {
            "player_id": 501,
            "player_name": "Example Runner",
            "player_position_id": "RB",
            "player_team_id": "SEA",
            "sportsdata_id": "stable-9001",
        }

        resolution = resolver.resolve(record)
        self.assertEqual("sleeper:101", resolution.player_key)
        resolver.register(self.connection, "501", resolution.player_key or "", "2026-08-27T00:00:00Z")
        resolver.register(self.connection, "501", resolution.player_key or "", "2026-08-27T00:00:01Z")

        count = self.connection.execute(
            "SELECT COUNT(*) FROM player_ids WHERE source_key = 'fantasypros_api'"
        ).fetchone()[0]
        self.assertEqual(1, count)

    def test_sample_sized_response_is_rejected_by_count(self) -> None:
        payload = {
            "count": 10,
            "players": [{"player_id": index} for index in range(10)],
            "public_api_limited": True,
        }
        with self.assertRaisesRegex(ValueError, "full-data threshold"):
            records_from_payload(payload, minimum_rows=20)

        full_payload = {
            "count": 20,
            "players": [{"player_id": index} for index in range(20)],
            "public_api_limited": True,
        }
        self.assertEqual(20, len(records_from_payload(full_payload, minimum_rows=20)))

    def test_ambiguous_exact_match_is_not_merged(self) -> None:
        self.add_player("sleeper:201", "Example Receiver", "WR", "NYJ")
        self.add_player("sleeper:202", "Example Receiver", "WR", "NYJ")
        resolver = IdentityResolver(self.connection)

        resolution = resolver.resolve(
            {
                "player_id": 601,
                "player_name": "Example Receiver",
                "player_position_id": "WR",
                "player_team_id": "NYJ",
            }
        )

        self.assertEqual("ambiguous", resolution.status)
        self.assertIsNone(resolution.player_key)
        count = self.connection.execute(
            "SELECT COUNT(*) FROM player_ids WHERE source_key = 'fantasypros_api'"
        ).fetchone()[0]
        self.assertEqual(0, count)

    def test_repeated_fixture_has_one_identity_and_one_latest_ecr(self) -> None:
        self.add_player("sleeper:301", "Example Quarterback", "QB", "LAC", "9101")
        record = {
            "player_id": 701,
            "player_name": "Example Quarterback",
            "player_position_id": "QB",
            "player_team_id": "LAC",
            "sportsdata_id": "9101",
            "rank_ecr": 12,
            "pos_rank": "QB3",
        }
        metadata = {"701": {"player_id": 701, "rank_adp_ppr": 14}}

        for second in (1, 2):
            resolver = IdentityResolver(self.connection)
            run_id, observed_at = start_run(
                self.connection,
                "fixture_ecr",
                2026,
                f"https://example.invalid/{second}",
            )
            stats = normalize_ecr(
                self.connection,
                [record],
                metadata,
                resolver,
                run_id,
                observed_at,
                2026,
                [],
            )
            finish_run(self.connection, run_id, status="success", row_count=1, notes=stats)

        identity_count = self.connection.execute(
            "SELECT COUNT(*) FROM player_ids WHERE source_key = 'fantasypros_api'"
        ).fetchone()[0]
        latest_ecr_count = self.connection.execute(
            "SELECT COUNT(*) FROM latest_player_values WHERE value_type = 'draft_ecr'"
        ).fetchone()[0]
        self.assertEqual(1, identity_count)
        self.assertEqual(1, latest_ecr_count)

    def test_failed_run_does_not_replace_latest_success(self) -> None:
        self.add_player("sleeper:401", "Example Tight End", "TE", "DET", "9201")
        resolver = IdentityResolver(self.connection)
        record = {
            "player_id": 801,
            "player_name": "Example Tight End",
            "player_position_id": "TE",
            "player_team_id": "DET",
            "sportsdata_id": "9201",
            "rank_ecr": 50,
        }
        run_id, observed_at = start_run(
            self.connection, "fixture_success", 2026, "https://example.invalid/success"
        )
        normalize_ecr(
            self.connection,
            [record],
            {},
            resolver,
            run_id,
            observed_at,
            2026,
            [],
        )
        finish_run(self.connection, run_id, status="success", row_count=1)

        failed_id, _ = start_run(
            self.connection, "fixture_failure", 2026, "https://example.invalid/failure"
        )
        self.connection.execute("DELETE FROM player_values WHERE value_type = 'draft_ecr'")
        self.connection.rollback()
        finish_run(self.connection, failed_id, status="failed", notes="synthetic failure")

        latest_rank = self.connection.execute(
            "SELECT rank_value FROM latest_player_values WHERE value_type = 'draft_ecr'"
        ).fetchone()[0]
        self.assertEqual(50.0, latest_rank)


if __name__ == "__main__":
    unittest.main()
