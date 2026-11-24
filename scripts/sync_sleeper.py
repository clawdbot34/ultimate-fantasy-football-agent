"""Sync public Sleeper NFL market data into the local Fantasy GM database.

Sleeper's API is tokenless and read-only. This collector keeps immutable raw
responses, records provenance in ingest_runs, updates the Sleeper player
directory, and stores trending add/drop counts as timestamped player values.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sqlite3
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".local" / "fantasy_gm.sqlite3"
RAW_ROOT = ROOT / ".local" / "raw" / "sleeper"
BASE_URL = "https://api.sleeper.app/v1"
SOURCE_KEY = "sleeper_api"
USER_AGENT = "fantasy-gm/0.1 (personal non-commercial data collector)"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_json(url: str, timeout: int) -> tuple[Any, bytes]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8")), raw


def save_raw(dataset: str, raw: bytes, observed_at: str) -> Path:
    day = observed_at[:10]
    stamp = observed_at.replace("-", "").replace(":", "").replace(".", "")
    destination = RAW_ROOT / day / f"{dataset}_{stamp}.json.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(destination, "wb") as handle:
        handle.write(raw)
    return destination


def start_run(connection: sqlite3.Connection, dataset: str, url: str) -> tuple[str, str]:
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    connection.execute(
        """
        INSERT INTO ingest_runs (
            run_id, source_key, dataset, started_at, status, source_url
        ) VALUES (?, ?, ?, ?, 'running', ?)
        """,
        (run_id, SOURCE_KEY, dataset, started_at, url),
    )
    connection.commit()
    return run_id, started_at


def complete_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    raw_path: Path | None = None,
    content_sha256: str | None = None,
    row_count: int | None = None,
    season: int | None = None,
    week: int | None = None,
    notes: str | None = None,
) -> None:
    relative_raw_path = raw_path.relative_to(ROOT).as_posix() if raw_path else None
    connection.execute(
        """
        UPDATE ingest_runs
        SET completed_at = ?, status = ?, raw_path = ?, content_sha256 = ?,
            row_count = ?, season = ?, week = ?, notes = ?
        WHERE run_id = ?
        """,
        (
            utc_now(),
            status,
            relative_raw_path,
            content_sha256,
            row_count,
            season,
            week,
            notes,
            run_id,
        ),
    )
    connection.commit()


def execute_dataset(
    connection: sqlite3.Connection,
    *,
    dataset: str,
    url: str,
    timeout: int,
    season: int | None,
    week: int | None,
    normalize: Callable[[Any, str, str], int],
    notes: Callable[[Any], dict[str, Any]] | None = None,
) -> tuple[Any, int, str]:
    run_id, observed_at = start_run(connection, dataset, url)
    raw_path: Path | None = None
    content_sha256: str | None = None
    try:
        payload, raw = fetch_json(url, timeout)
        raw_path = save_raw(dataset, raw, observed_at)
        content_sha256 = hashlib.sha256(raw).hexdigest()
        row_count = normalize(payload, run_id, observed_at)
        note_text = json.dumps(notes(payload), sort_keys=True) if notes else None
        complete_run(
            connection,
            run_id,
            status="success",
            raw_path=raw_path,
            content_sha256=content_sha256,
            row_count=row_count,
            season=season,
            week=week,
            notes=note_text,
        )
        return payload, row_count, run_id
    except Exception as error:
        connection.rollback()
        complete_run(
            connection,
            run_id,
            status="failed",
            raw_path=raw_path,
            content_sha256=content_sha256,
            season=season,
            week=week,
            notes=f"{type(error).__name__}: {error}"[:1000],
        )
        raise


def player_name(player: dict[str, Any], sleeper_id: str) -> str:
    full_name = player.get("full_name")
    if full_name:
        return str(full_name).strip()
    name = " ".join(
        part for part in (player.get("first_name"), player.get("last_name")) if part
    ).strip()
    return name or f"Sleeper player {sleeper_id}"


def player_metadata(player: dict[str, Any], sleeper_id: str) -> dict[str, Any]:
    fields = (
        "age",
        "birth_date",
        "depth_chart_order",
        "depth_chart_position",
        "fantasy_positions",
        "first_name",
        "injury_start_date",
        "injury_status",
        "last_name",
        "news_updated",
        "number",
        "practice_participation",
        "search_rank",
        "sport",
        "status",
        "team",
        "years_exp",
    )
    metadata = {key: player.get(key) for key in fields if player.get(key) is not None}
    metadata["sleeper_id"] = sleeper_id
    external_id_fields = (
        "espn_id",
        "fantasy_data_id",
        "gsis_id",
        "rotowire_id",
        "sportradar_id",
        "stats_id",
        "yahoo_id",
    )
    external_ids = {
        key: str(player[key])
        for key in external_id_fields
        if player.get(key) not in (None, "")
    }
    if external_ids:
        metadata["external_ids"] = external_ids
    return metadata


def upsert_player(
    connection: sqlite3.Connection,
    sleeper_id: str,
    player: dict[str, Any],
    observed_at: str,
) -> str:
    player_key = f"sleeper:{sleeper_id}"
    positions = player.get("fantasy_positions") or []
    position = player.get("position") or (positions[0] if positions else None)
    is_active = bool(player.get("active")) or player.get("status") == "Active"
    connection.execute(
        """
        INSERT INTO players (
            player_key, canonical_name, position, nfl_team, active,
            metadata_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_key) DO UPDATE SET
            canonical_name = excluded.canonical_name,
            position = excluded.position,
            nfl_team = excluded.nfl_team,
            active = excluded.active,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (
            player_key,
            player_name(player, sleeper_id),
            position,
            player.get("team"),
            int(is_active),
            json.dumps(player_metadata(player, sleeper_id), sort_keys=True),
            observed_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO player_ids (source_key, external_id, player_key, observed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_key, external_id) DO UPDATE SET
            player_key = excluded.player_key,
            observed_at = excluded.observed_at
        """,
        (SOURCE_KEY, sleeper_id, player_key, observed_at),
    )
    return player_key


def normalize_players(
    connection: sqlite3.Connection,
) -> Callable[[Any, str, str], int]:
    def normalize(payload: Any, _run_id: str, observed_at: str) -> int:
        if not isinstance(payload, dict):
            raise ValueError("Sleeper player directory was not a JSON object")
        for sleeper_id, player in payload.items():
            if not isinstance(player, dict):
                continue
            upsert_player(connection, str(sleeper_id), player, observed_at)
        return len(payload)

    return normalize


def ensure_placeholder_player(
    connection: sqlite3.Connection, sleeper_id: str, observed_at: str
) -> str:
    row = connection.execute(
        """
        SELECT player_key FROM player_ids
        WHERE source_key = ? AND external_id = ?
        """,
        (SOURCE_KEY, sleeper_id),
    ).fetchone()
    if row:
        return str(row[0])
    return upsert_player(
        connection,
        sleeper_id,
        {"full_name": f"Sleeper player {sleeper_id}", "active": True},
        observed_at,
    )


def normalize_trends(
    connection: sqlite3.Connection,
    *,
    trend_type: str,
    season: int,
    week: int,
    lookback_hours: int,
) -> Callable[[Any, str, str], int]:
    def normalize(payload: Any, run_id: str, observed_at: str) -> int:
        if not isinstance(payload, list):
            raise ValueError("Sleeper trending response was not a JSON list")
        inserted = 0
        for rank, item in enumerate(payload, start=1):
            if not isinstance(item, dict) or item.get("player_id") is None:
                continue
            sleeper_id = str(item["player_id"])
            player_key = ensure_placeholder_player(connection, sleeper_id, observed_at)
            player_row = connection.execute(
                "SELECT position FROM players WHERE player_key = ?", (player_key,)
            ).fetchone()
            connection.execute(
                """
                INSERT INTO player_values (
                    run_id, player_key, season, week, value_type,
                    scoring_profile, position, rank_value, numeric_value,
                    context_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, 'market', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    player_key,
                    season,
                    week,
                    f"sleeper_trending_{trend_type}",
                    player_row[0] if player_row else None,
                    rank,
                    item.get("count"),
                    json.dumps(
                        {
                            "lookback_hours": lookback_hours,
                            "sleeper_player_id": sleeper_id,
                        },
                        sort_keys=True,
                    ),
                    observed_at,
                ),
            )
            inserted += 1
        return inserted

    return normalize


def no_op_count(payload: Any, _run_id: str, _observed_at: str) -> int:
    return 1 if isinstance(payload, dict) else 0


def state_notes(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    keys = (
        "display_week",
        "league_create_season",
        "league_season",
        "leg",
        "previous_season",
        "season",
        "season_start_date",
        "season_type",
        "week",
    )
    return {key: payload.get(key) for key in keys if key in payload}


def players_synced_today(connection: sqlite3.Connection) -> dict[str, Any] | None:
    row = connection.execute(
        """
        SELECT completed_at, raw_path, row_count, run_id
        FROM ingest_runs
        WHERE source_key = ?
          AND dataset = 'players_nfl_active'
          AND status = 'success'
          AND substr(completed_at, 1, 10) = ?
        ORDER BY completed_at DESC
        LIMIT 1
        """,
        (SOURCE_KEY, utc_now()[:10]),
    ).fetchone()
    if not row:
        return None
    return {
        "status": "cached_today",
        "completed_at": row[0],
        "raw_path": row[1],
        "row_count": row[2],
        "run_id": row[3],
    }


def parse_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def sync(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db.resolve()
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database does not exist: {db_path}. Run scripts/init_db.py first."
        )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not connection.execute(
            "SELECT 1 FROM data_sources WHERE source_key = ?", (SOURCE_KEY,)
        ).fetchone():
            raise RuntimeError("Sleeper source is not seeded; run scripts/init_db.py")

        state_url = f"{BASE_URL}/state/nfl"
        state, state_count, state_run_id = execute_dataset(
            connection,
            dataset="state_nfl",
            url=state_url,
            timeout=args.timeout,
            season=None,
            week=None,
            normalize=no_op_count,
            notes=state_notes,
        )
        if not isinstance(state, dict):
            raise ValueError("Sleeper NFL state was not a JSON object")
        current_year = datetime.now(timezone.utc).year
        season = parse_int(state.get("season"), current_year)
        week = max(0, min(25, parse_int(state.get("week"), 0)))

        result: dict[str, Any] = {
            "source": SOURCE_KEY,
            "state": {
                "status": "success",
                "run_id": state_run_id,
                "row_count": state_count,
                **state_notes(state),
            },
        }

        cached_players = players_synced_today(connection)
        if args.skip_players:
            result["players"] = {"status": "skipped_by_request"}
        elif cached_players and not args.force_players:
            result["players"] = cached_players
        else:
            players_url = f"{BASE_URL}/players/nfl?active=true"
            _, player_count, player_run_id = execute_dataset(
                connection,
                dataset="players_nfl_active",
                url=players_url,
                timeout=args.timeout,
                season=season,
                week=week,
                normalize=normalize_players(connection),
            )
            result["players"] = {
                "status": "success",
                "run_id": player_run_id,
                "row_count": player_count,
            }

        trends: dict[str, Any] = {}
        for trend_type in ("add", "drop"):
            query = urllib.parse.urlencode(
                {"lookback_hours": args.lookback_hours, "limit": args.trend_limit}
            )
            trend_url = f"{BASE_URL}/players/nfl/trending/{trend_type}?{query}"
            _, trend_count, trend_run_id = execute_dataset(
                connection,
                dataset=f"trending_{trend_type}",
                url=trend_url,
                timeout=args.timeout,
                season=season,
                week=week,
                normalize=normalize_trends(
                    connection,
                    trend_type=trend_type,
                    season=season,
                    week=week,
                    lookback_hours=args.lookback_hours,
                ),
                notes=lambda _payload: {
                    "lookback_hours": args.lookback_hours,
                    "limit": args.trend_limit,
                },
            )
            trends[trend_type] = {
                "status": "success",
                "run_id": trend_run_id,
                "row_count": trend_count,
            }
        result["trending"] = trends

        result["validation"] = {
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_issues": len(
                connection.execute("PRAGMA foreign_key_check").fetchall()
            ),
            "sleeper_players": connection.execute(
                "SELECT COUNT(*) FROM player_ids WHERE source_key = ?", (SOURCE_KEY,)
            ).fetchone()[0],
            "sleeper_value_observations": connection.execute(
                """
                SELECT COUNT(*)
                FROM player_values AS pv
                JOIN ingest_runs AS ir ON ir.run_id = pv.run_id
                WHERE ir.source_key = ?
                """,
                (SOURCE_KEY,),
            ).fetchone()[0],
        }
        return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--trend-limit", type=int, default=100)
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--skip-players", action="store_true")
    parser.add_argument("--force-players", action="store_true")
    args = parser.parse_args()
    if args.lookback_hours < 1:
        parser.error("--lookback-hours must be at least 1")
    if not 1 <= args.trend_limit <= 1000:
        parser.error("--trend-limit must be between 1 and 1000")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    return args


def main() -> None:
    print(json.dumps(sync(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
