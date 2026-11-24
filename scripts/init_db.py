"""Create or update the local Fantasy GM SQLite database.

This script is intentionally dependency-free and idempotent. Private data is
loaded from .local/league.json when that file exists.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".local" / "fantasy_gm.sqlite3"
DEFAULT_SCHEMA = ROOT / "db" / "schema.sql"
DEFAULT_SOURCES = ROOT / "config" / "sources.json"
DEFAULT_SEED = ROOT / ".local" / "league.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def seed_sources(connection: sqlite3.Connection, path: Path) -> int:
    sources = load_json(path)
    if not isinstance(sources, list):
        raise ValueError(f"Source catalog must be a JSON list: {path}")

    updated_at = utc_now()
    for source in sources:
        connection.execute(
            """
            INSERT INTO data_sources (
                source_key, name, category, priority_tier, access_method,
                credential_env, base_url, license_notes, enabled, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                name = excluded.name,
                category = excluded.category,
                priority_tier = excluded.priority_tier,
                access_method = excluded.access_method,
                credential_env = excluded.credential_env,
                base_url = excluded.base_url,
                license_notes = excluded.license_notes,
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (
                source["source_key"],
                source["name"],
                source["category"],
                source["priority_tier"],
                source["access_method"],
                source.get("credential_env"),
                source.get("base_url"),
                source.get("license_notes"),
                int(bool(source.get("enabled", True))),
                updated_at,
            ),
        )
    return len(sources)


def seed_league(connection: sqlite3.Connection, path: Path) -> tuple[int, int]:
    seed = load_json(path)
    league = seed.get("league")
    teams = seed.get("teams", [])
    if not isinstance(league, dict) or not isinstance(teams, list):
        raise ValueError(f"League seed must contain an object and a team list: {path}")

    observed_at = league.get("observed_at") or utc_now()
    connection.execute(
        """
        INSERT INTO leagues (
            league_key, platform, platform_league_id, season, name, timezone,
            draft_at, settings_json, source_url, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(league_key) DO UPDATE SET
            platform = excluded.platform,
            platform_league_id = excluded.platform_league_id,
            season = excluded.season,
            name = excluded.name,
            timezone = excluded.timezone,
            draft_at = excluded.draft_at,
            settings_json = excluded.settings_json,
            source_url = excluded.source_url,
            observed_at = excluded.observed_at
        """,
        (
            league["league_key"],
            league["platform"],
            str(league["platform_league_id"]),
            league["season"],
            league["name"],
            league["timezone"],
            league.get("draft_at"),
            json.dumps(league.get("settings", {}), sort_keys=True),
            league.get("source_url"),
            observed_at,
        ),
    )

    for team in teams:
        connection.execute(
            """
            INSERT INTO teams (
                team_key, league_key, platform_team_id, name, manager_name,
                division_name, is_user_team, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_key) DO UPDATE SET
                league_key = excluded.league_key,
                platform_team_id = excluded.platform_team_id,
                name = excluded.name,
                manager_name = excluded.manager_name,
                division_name = excluded.division_name,
                is_user_team = excluded.is_user_team,
                observed_at = excluded.observed_at
            """,
            (
                team["team_key"],
                league["league_key"],
                str(team["platform_team_id"]),
                team["name"],
                team.get("manager_name"),
                team.get("division_name"),
                int(bool(team.get("is_user_team", False))),
                team.get("observed_at") or observed_at,
            ),
        )
    return 1, len(teams)


def initialize_database(
    db_path: Path,
    schema_path: Path,
    sources_path: Path,
    seed_path: Path | None,
) -> dict[str, Any]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    for child in ("raw", "exports", "backups"):
        (db_path.parent / child).mkdir(parents=True, exist_ok=True)

    schema = schema_path.read_text(encoding="utf-8")
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.executescript(schema)
        source_count = seed_sources(connection, sources_path)
        league_count = 0
        team_count = 0
        if seed_path is not None and seed_path.exists():
            league_count, team_count = seed_league(connection, seed_path)
        connection.commit()

        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
        table_count = connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0]

    if integrity != "ok" or foreign_key_issues:
        raise RuntimeError(
            f"Database validation failed: integrity={integrity}, "
            f"foreign_key_issues={len(foreign_key_issues)}"
        )

    return {
        "database": str(db_path),
        "tables": table_count,
        "sources_seeded": source_count,
        "leagues_seeded": league_count,
        "teams_seeded": team_count,
        "integrity": integrity,
        "foreign_key_issues": 0,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    parser.add_argument("--no-seed", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    result = initialize_database(
        db_path=args.db.resolve(),
        schema_path=args.schema.resolve(),
        sources_path=args.sources.resolve(),
        seed_path=None if args.no_seed else args.seed.resolve(),
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
