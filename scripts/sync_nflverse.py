"""Sync decision-relevant nflverse datasets into the local Fantasy GM store."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import nflreadpy as nfl
    import polars as pl
    from nflreadpy.config import update_config
except ImportError as error:
    raise SystemExit(
        "nflreadpy is not installed. Create the project virtual environment and "
        "install requirements.txt, then run this script with that environment."
    ) from error


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".local" / "fantasy_gm.sqlite3"
RAW_ROOT = ROOT / ".local" / "raw" / "nflverse"
CACHE_ROOT = ROOT / ".local" / "cache" / "nflreadpy"
SOURCE_KEY = "nflverse"
SOURCE_URL = "https://github.com/nflverse/nflverse-data/releases"
FANTASY_POSITIONS = {"QB", "RB", "FB", "WR", "TE", "K"}

STAT_METRICS = (
    "fantasy_points",
    "fantasy_points_ppr",
    "attempts",
    "completions",
    "passing_yards",
    "passing_tds",
    "passing_interceptions",
    "sacks_suffered",
    "carries",
    "rushing_yards",
    "rushing_tds",
    "targets",
    "receptions",
    "receiving_yards",
    "receiving_tds",
    "receiving_air_yards",
    "receiving_yards_after_catch",
    "target_share",
    "air_yards_share",
    "wopr",
    "fumbles_lost_total",
    "special_teams_tds",
    "punt_return_yards",
    "kickoff_return_yards",
    "fg_made",
    "fg_att",
    "fg_long",
    "pat_made",
    "pat_att",
)

SNAP_METRICS = (
    "offense_snaps",
    "offense_pct",
    "defense_snaps",
    "defense_pct",
    "st_snaps",
    "st_pct",
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def jsonable(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return str(value)


def number(value: Any) -> float | int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return int(result) if result.is_integer() else result


def start_run(
    connection: sqlite3.Connection,
    dataset: str,
    season: int,
) -> tuple[str, str]:
    run_id = str(uuid.uuid4())
    started_at = utc_now()
    connection.execute(
        """
        INSERT INTO ingest_runs (
            run_id, source_key, dataset, season, started_at, status, source_url
        ) VALUES (?, ?, ?, ?, ?, 'running', ?)
        """,
        (run_id, SOURCE_KEY, dataset, season, started_at, SOURCE_URL),
    )
    connection.commit()
    return run_id, started_at


def finish_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    raw_path: Path | None = None,
    content_sha256: str | None = None,
    row_count: int | None = None,
    notes: str | None = None,
) -> None:
    relative = raw_path.relative_to(ROOT).as_posix() if raw_path else None
    connection.execute(
        """
        UPDATE ingest_runs
        SET completed_at = ?, status = ?, raw_path = ?, content_sha256 = ?,
            row_count = ?, notes = ?
        WHERE run_id = ?
        """,
        (utc_now(), status, relative, content_sha256, row_count, notes, run_id),
    )
    connection.commit()


def save_frame(dataset: str, frame: pl.DataFrame, observed_at: str) -> tuple[Path, str]:
    day = observed_at[:10]
    stamp = observed_at.replace("-", "").replace(":", "").replace(".", "")
    destination = RAW_ROOT / day / f"{dataset}_{stamp}.parquet"
    destination.parent.mkdir(parents=True, exist_ok=True)
    frame.write_parquet(destination, compression="zstd")
    digest = hashlib.sha256()
    with destination.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return destination, digest.hexdigest()


def run_dataset(
    connection: sqlite3.Connection,
    *,
    dataset: str,
    season: int,
    loader: Callable[[], pl.DataFrame],
    normalize: Callable[[pl.DataFrame, str, str], int],
) -> dict[str, Any]:
    run_id, observed_at = start_run(connection, dataset, season)
    raw_path: Path | None = None
    content_sha256: str | None = None
    try:
        frame = loader()
        if not isinstance(frame, pl.DataFrame):
            raise TypeError(f"{dataset} did not return a Polars DataFrame")
        raw_path, content_sha256 = save_frame(dataset, frame, observed_at)
        normalized_rows = normalize(frame, run_id, observed_at)
        notes = json.dumps(
            {
                "source_rows": frame.height,
                "source_columns": frame.width,
                "normalized_rows": normalized_rows,
                "nflreadpy_version": getattr(nfl, "__version__", "unknown"),
            },
            sort_keys=True,
        )
        finish_run(
            connection,
            run_id,
            status="success",
            raw_path=raw_path,
            content_sha256=content_sha256,
            row_count=frame.height,
            notes=notes,
        )
        return {
            "status": "success",
            "season": season,
            "source_rows": frame.height,
            "normalized_rows": normalized_rows,
            "run_id": run_id,
        }
    except Exception as error:
        connection.rollback()
        finish_run(
            connection,
            run_id,
            status="failed",
            raw_path=raw_path,
            content_sha256=content_sha256,
            notes=f"{type(error).__name__}: {error}"[:1000],
        )
        return {
            "status": "failed",
            "season": season,
            "run_id": run_id,
            "error": f"{type(error).__name__}: {error}"[:500],
        }


def merge_metadata(existing_json: str | None, namespace: str, values: dict[str, Any]) -> str:
    try:
        metadata = json.loads(existing_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    metadata[namespace] = jsonable(values)
    return json.dumps(metadata, sort_keys=True)


def lookup_player(
    connection: sqlite3.Connection,
    *,
    gsis_id: str | None = None,
    sleeper_id: str | None = None,
    pfr_id: str | None = None,
    name: str | None = None,
    team: str | None = None,
    position: str | None = None,
) -> str | None:
    if gsis_id:
        row = connection.execute(
            "SELECT player_key FROM player_ids WHERE source_key = ? AND external_id = ?",
            (SOURCE_KEY, gsis_id),
        ).fetchone()
        if row:
            return str(row[0])
    if sleeper_id:
        row = connection.execute(
            "SELECT player_key FROM player_ids WHERE source_key = 'sleeper_api' AND external_id = ?",
            (sleeper_id,),
        ).fetchone()
        if row:
            return str(row[0])
    if gsis_id:
        row = connection.execute(
            """
            SELECT player_key FROM players
            WHERE json_extract(metadata_json, '$.external_ids.gsis_id') = ?
            LIMIT 1
            """,
            (gsis_id,),
        ).fetchone()
        if row:
            return str(row[0])
    if pfr_id:
        row = connection.execute(
            """
            SELECT player_key FROM players
            WHERE json_extract(metadata_json, '$.nflverse.pfr_id') = ?
            LIMIT 1
            """,
            (pfr_id,),
        ).fetchone()
        if row:
            return str(row[0])
    if name:
        rows = connection.execute(
            """
            SELECT player_key FROM players
            WHERE canonical_name = ?
              AND (? IS NULL OR nfl_team = ?)
              AND (? IS NULL OR position = ?)
            LIMIT 2
            """,
            (name, team, team, position, position),
        ).fetchall()
        if len(rows) == 1:
            return str(rows[0][0])
    return None


def register_nflverse_id(
    connection: sqlite3.Connection,
    player_key: str,
    external_id: str | None,
    observed_at: str,
) -> None:
    if not external_id:
        return
    existing = connection.execute(
        "SELECT external_id FROM player_ids WHERE source_key = ? AND player_key = ?",
        (SOURCE_KEY, player_key),
    ).fetchone()
    if existing:
        return
    connection.execute(
        """
        INSERT INTO player_ids (source_key, external_id, player_key, observed_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(source_key, external_id) DO UPDATE SET
            player_key = excluded.player_key,
            observed_at = excluded.observed_at
        """,
        (SOURCE_KEY, external_id, player_key, observed_at),
    )


def upsert_roster_player(
    connection: sqlite3.Connection,
    row: dict[str, Any],
    observed_at: str,
) -> str:
    gsis_id = str(row["gsis_id"]) if row.get("gsis_id") else None
    sleeper_id = str(row["sleeper_id"]) if row.get("sleeper_id") else None
    pfr_id = str(row["pfr_id"]) if row.get("pfr_id") else None
    name = str(row.get("full_name") or row.get("football_name") or gsis_id or pfr_id)
    team = str(row["team"]) if row.get("team") else None
    position = str(row["position"]) if row.get("position") else None
    player_key = lookup_player(
        connection,
        gsis_id=gsis_id,
        sleeper_id=sleeper_id,
        pfr_id=pfr_id,
        name=name,
        team=team,
        position=position,
    ) or f"nflverse:{gsis_id or pfr_id or hashlib.sha256(name.encode()).hexdigest()[:16]}"

    existing = connection.execute(
        "SELECT metadata_json FROM players WHERE player_key = ?", (player_key,)
    ).fetchone()
    roster_fields = (
        "birth_date",
        "college",
        "depth_chart_position",
        "draft_club",
        "draft_number",
        "entry_year",
        "espn_id",
        "fantasy_data_id",
        "gsis_id",
        "headshot_url",
        "height",
        "jersey_number",
        "pff_id",
        "pfr_id",
        "rookie_year",
        "rotowire_id",
        "sleeper_id",
        "sportradar_id",
        "status",
        "status_description_abbr",
        "weight",
        "yahoo_id",
        "years_exp",
    )
    source_metadata = {
        key: row.get(key) for key in roster_fields if row.get(key) is not None
    }
    metadata_json = merge_metadata(
        existing[0] if existing else None, "nflverse", source_metadata
    )
    active = str(row.get("status") or "").upper() not in {"CUT", "RET"}
    connection.execute(
        """
        INSERT INTO players (
            player_key, canonical_name, position, nfl_team, active,
            metadata_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(player_key) DO UPDATE SET
            canonical_name = excluded.canonical_name,
            position = COALESCE(excluded.position, players.position),
            nfl_team = COALESCE(excluded.nfl_team, players.nfl_team),
            active = excluded.active,
            metadata_json = excluded.metadata_json,
            updated_at = excluded.updated_at
        """,
        (player_key, name, position, team, int(active), metadata_json, observed_at),
    )
    register_nflverse_id(connection, player_key, gsis_id or pfr_id, observed_at)
    return player_key


def normalize_rosters(connection: sqlite3.Connection) -> Callable[[pl.DataFrame, str, str], int]:
    def normalize(frame: pl.DataFrame, _run_id: str, observed_at: str) -> int:
        ordered = frame.sort("week") if "week" in frame.columns else frame
        processed = 0
        for row in ordered.iter_rows(named=True):
            if not row.get("full_name") and not row.get("gsis_id"):
                continue
            upsert_roster_player(connection, row, observed_at)
            processed += 1
        return processed

    return normalize


def normalize_schedules(connection: sqlite3.Connection) -> Callable[[pl.DataFrame, str, str], int]:
    def normalize(frame: pl.DataFrame, run_id: str, observed_at: str) -> int:
        inserted = 0
        context_fields = (
            "away_coach",
            "away_moneyline",
            "away_qb_id",
            "away_qb_name",
            "away_rest",
            "away_spread_odds",
            "div_game",
            "espn",
            "gametime",
            "gameday",
            "home_coach",
            "home_moneyline",
            "home_qb_id",
            "home_qb_name",
            "home_rest",
            "home_spread_odds",
            "location",
            "overtime",
            "result",
            "spread_line",
            "stadium_id",
            "total",
            "total_line",
            "weekday",
        )
        for row in frame.iter_rows(named=True):
            game_id = row.get("game_id")
            if not game_id:
                continue
            gameday = jsonable(row.get("gameday"))
            gametime = row.get("gametime")
            game_at = f"{gameday}T{gametime}" if gameday and gametime else gameday
            context = {
                key: jsonable(row.get(key))
                for key in context_fields
                if row.get(key) is not None
            }
            connection.execute(
                """
                INSERT INTO nfl_game_snapshots (
                    run_id, game_id, season, week, game_type, game_at,
                    away_team, home_team, away_score, home_score, stadium,
                    roof, surface, temperature, wind, context_json, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    str(game_id),
                    int(row["season"]),
                    max(0, min(25, int(row.get("week") or 0))),
                    str(row.get("game_type") or "unknown"),
                    game_at,
                    str(row.get("away_team") or "unknown"),
                    str(row.get("home_team") or "unknown"),
                    number(row.get("away_score")),
                    number(row.get("home_score")),
                    row.get("stadium"),
                    row.get("roof"),
                    row.get("surface"),
                    number(row.get("temp")),
                    number(row.get("wind")),
                    json.dumps(context, sort_keys=True),
                    observed_at,
                ),
            )
            inserted += 1
        return inserted

    return normalize


def ensure_fact_player(
    connection: sqlite3.Connection,
    *,
    external_id: str | None,
    name: str,
    team: str | None,
    position: str | None,
    observed_at: str,
    pfr_id: str | None = None,
) -> str:
    player_key = lookup_player(
        connection,
        gsis_id=external_id,
        pfr_id=pfr_id,
        name=name,
        team=team,
        position=position,
    )
    if player_key:
        register_nflverse_id(connection, player_key, external_id or pfr_id, observed_at)
        return player_key
    identity = external_id or pfr_id or hashlib.sha256(name.encode()).hexdigest()[:16]
    return upsert_roster_player(
        connection,
        {
            "gsis_id": external_id,
            "pfr_id": pfr_id,
            "full_name": name,
            "team": team,
            "position": position,
            "status": "ACT",
        },
        observed_at,
    )


def insert_player_value(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    player_key: str,
    season: int,
    week: int,
    value_type: str,
    scoring_profile: str,
    position: str | None,
    numeric_value: float | int,
    context: dict[str, Any],
    observed_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO player_values (
            run_id, player_key, season, week, value_type, scoring_profile,
            position, numeric_value, context_json, observed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(
            run_id, player_key, season, week, value_type, scoring_profile
        ) DO UPDATE SET
            numeric_value = excluded.numeric_value,
            context_json = excluded.context_json,
            observed_at = excluded.observed_at
        """,
        (
            run_id,
            player_key,
            season,
            week,
            value_type,
            scoring_profile,
            position,
            numeric_value,
            json.dumps(jsonable(context), sort_keys=True),
            observed_at,
        ),
    )


def normalize_stats(connection: sqlite3.Connection) -> Callable[[pl.DataFrame, str, str], int]:
    def normalize(frame: pl.DataFrame, run_id: str, observed_at: str) -> int:
        inserted = 0
        for row in frame.iter_rows(named=True):
            position = str(row.get("position") or "")
            if position not in FANTASY_POSITIONS or not row.get("player_id"):
                continue
            name = str(row.get("player_display_name") or row.get("player_name") or row["player_id"])
            team = str(row["team"]) if row.get("team") else None
            player_key = ensure_fact_player(
                connection,
                external_id=str(row["player_id"]),
                name=name,
                team=team,
                position=position,
                observed_at=observed_at,
            )
            context = {
                "game_id": row.get("game_id"),
                "opponent_team": row.get("opponent_team"),
                "season_type": row.get("season_type"),
                "team": team,
            }
            for metric in STAT_METRICS:
                metric_value = number(row.get(metric))
                if metric_value in (None, 0):
                    continue
                insert_player_value(
                    connection,
                    run_id=run_id,
                    player_key=player_key,
                    season=int(row["season"]),
                    week=max(0, min(25, int(row.get("week") or 0))),
                    value_type=f"actual_{metric}",
                    scoring_profile="nfl",
                    position=position,
                    numeric_value=metric_value,
                    context=context,
                    observed_at=observed_at,
                )
                inserted += 1
        return inserted

    return normalize


def normalize_snaps(connection: sqlite3.Connection) -> Callable[[pl.DataFrame, str, str], int]:
    def normalize(frame: pl.DataFrame, run_id: str, observed_at: str) -> int:
        inserted = 0
        for row in frame.iter_rows(named=True):
            position = str(row.get("position") or "")
            if position not in FANTASY_POSITIONS:
                continue
            pfr_id = str(row["pfr_player_id"]) if row.get("pfr_player_id") else None
            name = str(row.get("player") or pfr_id or "Unknown player")
            team = str(row["team"]) if row.get("team") else None
            player_key = ensure_fact_player(
                connection,
                external_id=None,
                pfr_id=pfr_id,
                name=name,
                team=team,
                position=position,
                observed_at=observed_at,
            )
            context = {
                "game_id": row.get("game_id"),
                "opponent_team": row.get("opponent"),
                "game_type": row.get("game_type"),
                "team": team,
            }
            for metric in SNAP_METRICS:
                metric_value = number(row.get(metric))
                if metric_value in (None, 0):
                    continue
                insert_player_value(
                    connection,
                    run_id=run_id,
                    player_key=player_key,
                    season=int(row["season"]),
                    week=max(0, min(25, int(row.get("week") or 0))),
                    value_type=f"snap_{metric}",
                    scoring_profile="usage",
                    position=position,
                    numeric_value=metric_value,
                    context=context,
                    observed_at=observed_at,
                )
                inserted += 1
        return inserted

    return normalize


def normalize_injuries(connection: sqlite3.Connection) -> Callable[[pl.DataFrame, str, str], int]:
    def normalize(frame: pl.DataFrame, run_id: str, observed_at: str) -> int:
        inserted = 0
        for row in frame.iter_rows(named=True):
            position = str(row.get("position") or "")
            if position not in FANTASY_POSITIONS:
                continue
            gsis_id = str(row["gsis_id"]) if row.get("gsis_id") else None
            name = str(row.get("full_name") or gsis_id or "Unknown player")
            team = str(row["team"]) if row.get("team") else None
            player_key = ensure_fact_player(
                connection,
                external_id=gsis_id,
                name=name,
                team=team,
                position=position,
                observed_at=observed_at,
            )
            status = row.get("report_status") or row.get("practice_status")
            injury = row.get("report_primary_injury") or row.get("practice_primary_injury")
            week = max(0, min(25, int(row.get("week") or 0)))
            identity = "|".join(
                str(value)
                for value in (run_id, gsis_id, name, week, status, injury)
            )
            event_id = f"nflverse:{hashlib.sha256(identity.encode()).hexdigest()}"
            details = {key: jsonable(value) for key, value in row.items()}
            headline = " - ".join(str(value) for value in (name, status, injury) if value)
            connection.execute(
                """
                INSERT INTO player_events (
                    event_id, run_id, player_key, event_type, event_at, status,
                    headline, source_url, confidence, details_json, observed_at
                ) VALUES (?, ?, ?, 'injury_report', ?, ?, ?, ?, 0.9, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    player_key,
                    observed_at,
                    status,
                    headline,
                    SOURCE_URL,
                    json.dumps(details, sort_keys=True),
                    observed_at,
                ),
            )
            inserted += 1
        return inserted

    return normalize


def league_season(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT season FROM leagues ORDER BY observed_at DESC LIMIT 1"
    ).fetchone()
    return int(row[0]) if row else datetime.now().year


def sync(args: argparse.Namespace) -> dict[str, Any]:
    db_path = args.db.resolve()
    if not db_path.exists():
        raise FileNotFoundError(
            f"Database does not exist: {db_path}. Run scripts/init_db.py first."
        )
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    update_config(
        cache_mode="filesystem",
        cache_dir=CACHE_ROOT,
        cache_duration=args.cache_hours * 3600,
        verbose=False,
        timeout=args.timeout,
        user_agent="fantasy-gm/0.1 nflreadpy/0.1.5",
    )

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if not connection.execute(
            "SELECT 1 FROM data_sources WHERE source_key = ?", (SOURCE_KEY,)
        ).fetchone():
            raise RuntimeError("nflverse source is not seeded; run scripts/init_db.py")
        season = args.season or league_season(connection)
        available_season = int(nfl.get_current_season())
        analysis_season = args.analysis_season or min(season, available_season)
        schedule_seasons = sorted({season, analysis_season})

        results: dict[str, Any] = {
            "source": SOURCE_KEY,
            "league_season": season,
            "analysis_season": analysis_season,
            "datasets": {},
        }
        results["datasets"]["schedules"] = run_dataset(
            connection,
            dataset="schedules_" + "_".join(str(value) for value in schedule_seasons),
            season=season,
            loader=lambda: nfl.load_schedules(schedule_seasons),
            normalize=normalize_schedules(connection),
        )
        results["datasets"]["rosters"] = run_dataset(
            connection,
            dataset=f"rosters_{season}",
            season=season,
            loader=lambda: nfl.load_rosters([season]),
            normalize=normalize_rosters(connection),
        )
        results["datasets"]["player_stats"] = run_dataset(
            connection,
            dataset=f"player_stats_week_{analysis_season}",
            season=analysis_season,
            loader=lambda: nfl.load_player_stats([analysis_season], summary_level="week"),
            normalize=normalize_stats(connection),
        )
        results["datasets"]["snap_counts"] = run_dataset(
            connection,
            dataset=f"snap_counts_{analysis_season}",
            season=analysis_season,
            loader=lambda: nfl.load_snap_counts([analysis_season]),
            normalize=normalize_snaps(connection),
        )
        results["datasets"]["injuries"] = run_dataset(
            connection,
            dataset=f"injuries_{analysis_season}",
            season=analysis_season,
            loader=lambda: nfl.load_injuries([analysis_season]),
            normalize=normalize_injuries(connection),
        )

        failed = [
            name
            for name, result in results["datasets"].items()
            if result["status"] == "failed"
        ]
        results["validation"] = {
            "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
            "foreign_key_issues": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
            "failed_datasets": failed,
            "nflverse_player_ids": connection.execute(
                "SELECT COUNT(*) FROM player_ids WHERE source_key = ?", (SOURCE_KEY,)
            ).fetchone()[0],
            "latest_games": connection.execute(
                "SELECT COUNT(*) FROM latest_nfl_games WHERE season = ?", (season,)
            ).fetchone()[0],
            "nflverse_values": connection.execute(
                """
                SELECT COUNT(*) FROM player_values AS pv
                JOIN ingest_runs AS ir ON ir.run_id = pv.run_id
                WHERE ir.source_key = ?
                """,
                (SOURCE_KEY,),
            ).fetchone()[0],
            "nflverse_injury_events": connection.execute(
                """
                SELECT COUNT(*) FROM player_events AS pe
                JOIN ingest_runs AS ir ON ir.run_id = pe.run_id
                WHERE ir.source_key = ? AND pe.event_type = 'injury_report'
                """,
                (SOURCE_KEY,),
            ).fetchone()[0],
        }
        results["status"] = "partial" if failed else "success"
        return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--season", type=int)
    parser.add_argument("--analysis-season", type=int)
    parser.add_argument("--cache-hours", type=int, default=6)
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    if args.cache_hours < 0:
        parser.error("--cache-hours cannot be negative")
    if args.timeout < 1:
        parser.error("--timeout must be at least 1")
    return args


def main() -> None:
    result = sync(parse_args())
    print(json.dumps(result, indent=2, sort_keys=True))
    if result["validation"]["integrity"] != "ok" or result["validation"]["foreign_key_issues"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
