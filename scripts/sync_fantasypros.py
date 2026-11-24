"""Sync FantasyPros draft rankings and projections into the local GM database.

The collector is intentionally small: one request-paced importer, the existing
SQLite schema, private compressed raw snapshots, and conservative identity
matching to canonical Sleeper players. It never prints the API key or raw data.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

if __package__:
    from .fantasypros_http import fantasypros_request
else:
    from fantasypros_http import fantasypros_request


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".local" / "fantasy_gm.sqlite3"
LOCAL_ENV = ROOT / ".local" / ".env"
RAW_ROOT = ROOT / ".local" / "raw" / "fantasypros"
REPORT_PATH = ROOT / ".local" / "exports" / "fantasypros-unresolved.json"
SOURCE_KEY = "fantasypros_api"
BASE_URL = "https://api.fantasypros.com/public/v2/json/nfl"
USER_AGENT = "fantasy-gm-personal/0.1"
POSITIONS = ("QB", "RB", "WR", "TE", "K", "DST")
MINIMUM_ROWS = {
    "players": 500,
    "draft_ecr_ppr": 300,
    "projections_qb": 50,
    "projections_rb": 75,
    "projections_wr": 100,
    "projections_te": 50,
    "projections_k": 20,
    "projections_dst": 25,
}
OFFENSIVE_POSITIONS = {"QB", "RB", "WR", "TE"}
POSITION_ALIASES = {"D/ST": "DEF", "DST": "DEF", "PK": "K"}
TEAM_ALIASES = {
    "AZ": "ARI",
    "JAC": "JAX",
    "LA": "LAR",
    "OAK": "LV",
    "SD": "LAC",
    "STL": "LAR",
    "WSH": "WAS",
}
SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def load_local_env(path: Path = LOCAL_ENV) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip())


def number(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    parsed = number(value)
    return int(parsed) if parsed is not None and parsed.is_integer() else None


def normalize_position(value: Any) -> str:
    position = str(value or "").strip().upper()
    return POSITION_ALIASES.get(position, position)


def normalize_team(value: Any) -> str:
    team = str(value or "").strip().upper()
    return TEAM_ALIASES.get(team, team)


def normalize_name(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    ascii_text = "".join(character for character in text if not unicodedata.combining(character))
    words = re.findall(r"[a-z0-9]+", ascii_text.lower())
    while words and words[-1] in SUFFIXES:
        words.pop()
    return "".join(words)


def fantasypros_id(record: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    value = record.get("player_id") or record.get("fpid")
    if value in (None, "") and metadata:
        value = metadata.get("player_id")
    return str(value) if value not in (None, "") else ""


def record_name(record: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    value = record.get("player_name") or record.get("name")
    if value in (None, "") and metadata:
        value = metadata.get("player_name")
    return str(value or "").strip()


def record_team(record: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    value = record.get("player_team_id") or record.get("team_id")
    if value in (None, "") and metadata:
        value = metadata.get("team_id")
    return normalize_team(value)


def record_position(record: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    value = record.get("player_position_id") or record.get("position_id")
    if value in (None, "") and metadata:
        value = metadata.get("position_id")
    return normalize_position(value)


def sportsdata_id(record: dict[str, Any], metadata: dict[str, Any] | None = None) -> str:
    value = record.get("sportsdata_id") or record.get("sportsdata_player_id")
    if value in (None, "") and metadata:
        value = metadata.get("sportsdata_player_id")
    return str(value) if value not in (None, "", "0") else ""


@dataclass(frozen=True)
class Resolution:
    status: str
    player_key: str | None
    reason: str


class IdentityResolver:
    """Resolve FantasyPros records without silently merging ambiguous players."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.fp_to_player: dict[str, str] = {}
        self.sportsdata_to_players: dict[str, set[str]] = defaultdict(set)
        self.exact_to_players: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        self.defense_by_team: dict[str, set[str]] = defaultdict(set)

        for external_id, player_key in connection.execute(
            "SELECT external_id, player_key FROM player_ids WHERE source_key = ?",
            (SOURCE_KEY,),
        ):
            self.fp_to_player[str(external_id)] = str(player_key)

        for player_key, name, position, team, metadata_json in connection.execute(
            """
            SELECT player_key, canonical_name, position, nfl_team, metadata_json
            FROM players
            """
        ):
            key = str(player_key)
            position_key = normalize_position(position)
            team_key = normalize_team(team)
            name_key = normalize_name(name)
            if name_key and position_key and team_key:
                self.exact_to_players[(name_key, position_key, team_key)].add(key)
            if position_key == "DEF" and team_key:
                self.defense_by_team[team_key].add(key)
            try:
                metadata = json.loads(metadata_json or "{}")
            except json.JSONDecodeError:
                metadata = {}
            external_ids = metadata.get("external_ids", {}) if isinstance(metadata, dict) else {}
            if isinstance(external_ids, dict):
                for external_key in ("fantasy_data_id", "sportradar_id"):
                    stable_id = external_ids.get(external_key)
                    if stable_id not in (None, "", "0"):
                        self.sportsdata_to_players[str(stable_id)].add(key)

    def resolve(
        self,
        record: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> Resolution:
        fp_id = fantasypros_id(record, metadata)
        if fp_id and fp_id in self.fp_to_player:
            return Resolution("matched", self.fp_to_player[fp_id], "fantasypros_id")

        stable_id = sportsdata_id(record, metadata)
        if stable_id:
            candidates = self.sportsdata_to_players.get(stable_id, set())
            if len(candidates) == 1:
                return Resolution("matched", next(iter(candidates)), "sportsdata_id")
            if len(candidates) > 1:
                return Resolution("ambiguous", None, "sportsdata_id")

        position = record_position(record, metadata)
        team = record_team(record, metadata)
        if position == "DEF" and team:
            candidates = self.defense_by_team.get(team, set())
            if len(candidates) == 1:
                return Resolution("matched", next(iter(candidates)), "defense_team")
            if len(candidates) > 1:
                return Resolution("ambiguous", None, "defense_team")

        name = normalize_name(record_name(record, metadata))
        if name and position and team:
            candidates = self.exact_to_players.get((name, position, team), set())
            if len(candidates) == 1:
                return Resolution("matched", next(iter(candidates)), "name_team_position")
            if len(candidates) > 1:
                return Resolution("ambiguous", None, "name_team_position")
        return Resolution("unresolved", None, "no_exact_match")

    def register(
        self,
        connection: sqlite3.Connection,
        fp_id: str,
        player_key: str,
        observed_at: str,
    ) -> Resolution:
        if not fp_id:
            return Resolution("unresolved", None, "missing_fantasypros_id")
        existing_player = self.fp_to_player.get(fp_id)
        if existing_player and existing_player != player_key:
            return Resolution("ambiguous", None, "fantasypros_id_conflict")
        existing_id = connection.execute(
            "SELECT external_id FROM player_ids WHERE source_key = ? AND player_key = ?",
            (SOURCE_KEY, player_key),
        ).fetchone()
        if existing_id and str(existing_id[0]) != fp_id:
            return Resolution("ambiguous", None, "canonical_player_already_linked")
        connection.execute(
            """
            INSERT INTO player_ids (source_key, external_id, player_key, observed_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_key, external_id) DO UPDATE SET
                observed_at = excluded.observed_at
            """,
            (SOURCE_KEY, fp_id, player_key, observed_at),
        )
        self.fp_to_player[fp_id] = player_key
        return Resolution("matched", player_key, "registered")


class RequestPacer:
    def __init__(self, interval_seconds: float = 1.1) -> None:
        self.interval_seconds = interval_seconds
        self.last_request_at: float | None = None

    def wait(self) -> None:
        if self.last_request_at is not None:
            remaining = self.interval_seconds - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()


def endpoint_url(path: str, **params: Any) -> str:
    return f"{BASE_URL}/{path}?{urlencode(params)}"


def dataset_endpoints(season: int) -> list[tuple[str, str]]:
    endpoints = [
        (
            "players",
            endpoint_url("players", sport="NFL", year=season, position="ALL"),
        ),
        (
            "draft_ecr_ppr",
            endpoint_url(
                f"{season}/consensus-rankings",
                experts="combined",
                position="ALL",
                type="draft",
                scoring="PPR",
                week=0,
            ),
        ),
    ]
    endpoints.extend(
        (
            f"projections_{position.lower()}",
            endpoint_url(
                f"{season}/projections",
                position=position,
                scoring="PPR",
                week=0,
            ),
        )
        for position in POSITIONS
    )
    return endpoints


def fetch_json(url: str, api_key: str, timeout: int) -> tuple[dict[str, Any], bytes]:
    request = fantasypros_request(url, api_key, user_agent=USER_AGENT)
    with urlopen(request, timeout=timeout) as response:
        raw = response.read()
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("FantasyPros response was not a JSON object")
    return payload, raw


def records_from_payload(
    payload: dict[str, Any], *, minimum_rows: int = 0
) -> list[dict[str, Any]]:
    records = payload.get("players")
    if not isinstance(records, list):
        raise ValueError("FantasyPros response did not contain a players list")
    if not all(isinstance(record, dict) for record in records):
        raise ValueError("FantasyPros players list contained a non-object record")
    declared_count = integer(payload.get("count"))
    if declared_count is None or declared_count != len(records):
        raise ValueError(
            f"FantasyPros count mismatch: declared={declared_count}, returned={len(records)}"
        )
    if len(records) < minimum_rows:
        raise ValueError(
            f"FantasyPros response was below the full-data threshold: "
            f"returned={len(records)}, minimum={minimum_rows}"
        )
    return records


def save_raw(dataset: str, raw: bytes, observed_at: str) -> tuple[Path, str]:
    day = observed_at[:10]
    stamp = observed_at.replace("-", "").replace(":", "").replace(".", "")
    destination = RAW_ROOT / day / f"{dataset}_{stamp}.json.gz"
    destination.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(destination, "wb") as handle:
        handle.write(raw)
    return destination, hashlib.sha256(raw).hexdigest()


def start_run(connection: sqlite3.Connection, dataset: str, season: int, url: str) -> tuple[str, str]:
    run_id = str(uuid.uuid4())
    observed_at = utc_now()
    connection.execute(
        """
        INSERT INTO ingest_runs (
            run_id, source_key, dataset, season, week, started_at, status, source_url
        ) VALUES (?, ?, ?, ?, 0, ?, 'running', ?)
        """,
        (run_id, SOURCE_KEY, dataset, season, observed_at, url),
    )
    connection.commit()
    return run_id, observed_at


def finish_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    status: str,
    raw_path: Path | None = None,
    content_sha256: str | None = None,
    row_count: int | None = None,
    notes: dict[str, Any] | str | None = None,
) -> None:
    relative_path = raw_path.relative_to(ROOT).as_posix() if raw_path else None
    note_text = json.dumps(notes, sort_keys=True) if isinstance(notes, dict) else notes
    connection.execute(
        """
        UPDATE ingest_runs
        SET completed_at = ?, status = ?, raw_path = ?, content_sha256 = ?,
            row_count = ?, notes = ?
        WHERE run_id = ?
        """,
        (utc_now(), status, relative_path, content_sha256, row_count, note_text, run_id),
    )
    connection.commit()


def selected_context(record: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {key: record[key] for key in keys if record.get(key) not in (None, "")}


def insert_value(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    player_key: str,
    season: int,
    value_type: str,
    position: str,
    observed_at: str,
    rank_value: float | None = None,
    numeric_value: float | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO player_values (
            run_id, player_key, season, week, value_type, scoring_profile,
            position, rank_value, numeric_value, context_json, observed_at
        ) VALUES (?, ?, ?, 0, ?, 'ppr', ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            player_key,
            season,
            value_type,
            position,
            rank_value,
            numeric_value,
            json.dumps(context or {}, sort_keys=True),
            observed_at,
        ),
    )


def position_rank(record: dict[str, Any]) -> int | None:
    match = re.search(r"(\d+)", str(record.get("pos_rank") or ""))
    return int(match.group(1)) if match else None


def is_draft_relevant(
    record: dict[str, Any],
    metadata: dict[str, Any] | None,
    dataset: str,
    position_order: int,
) -> bool:
    position = record_position(record, metadata)
    if position == "DEF":
        return True
    if position == "K":
        rank = position_rank(record)
        return (rank is not None and rank <= 24) or (
            dataset.startswith("projections_") and position_order <= 24
        )
    overall_rank = integer(record.get("rank_ecr"))
    return position in OFFENSIVE_POSITIONS and overall_rank is not None and overall_rank <= 200


def unresolved_entry(
    record: dict[str, Any],
    metadata: dict[str, Any] | None,
    *,
    dataset: str,
    position_order: int,
    resolution: Resolution,
) -> dict[str, Any]:
    return {
        "dataset": dataset,
        "fantasypros_player_id": fantasypros_id(record, metadata),
        "name": record_name(record, metadata),
        "team": record_team(record, metadata),
        "position": record_position(record, metadata),
        "rank_ecr": integer(record.get("rank_ecr")),
        "position_rank": position_rank(record),
        "status": resolution.status,
        "reason": resolution.reason,
        "draft_relevant": is_draft_relevant(record, metadata, dataset, position_order),
    }


def normalize_ecr(
    connection: sqlite3.Connection,
    records: list[dict[str, Any]],
    metadata_by_id: dict[str, dict[str, Any]],
    resolver: IdentityResolver,
    run_id: str,
    observed_at: str,
    season: int,
    unresolved: list[dict[str, Any]],
) -> dict[str, int]:
    matched = 0
    values_inserted = 0
    for index, record in enumerate(records, start=1):
        fp_id = fantasypros_id(record)
        metadata = metadata_by_id.get(fp_id)
        resolution = resolver.resolve(record, metadata)
        if resolution.player_key:
            resolution = resolver.register(connection, fp_id, resolution.player_key, observed_at)
        if not resolution.player_key:
            unresolved.append(
                unresolved_entry(
                    record,
                    metadata,
                    dataset="draft_ecr_ppr",
                    position_order=index,
                    resolution=resolution,
                )
            )
            continue

        matched += 1
        position = record_position(record, metadata)
        ecr_context = selected_context(
            record,
            (
                "player_bye_week",
                "pos_rank",
                "rank_ave",
                "rank_min",
                "rank_max",
                "rank_std",
                "tier",
                "player_owned_avg",
                "player_owned_espn",
                "player_owned_yahoo",
            ),
        )
        ecr_context["fantasypros_player_id"] = fp_id
        insert_value(
            connection,
            run_id=run_id,
            player_key=resolution.player_key,
            season=season,
            value_type="draft_ecr",
            position=position,
            rank_value=number(record.get("rank_ecr")),
            observed_at=observed_at,
            context=ecr_context,
        )
        values_inserted += 1

        adp = number(metadata.get("rank_adp_ppr")) if metadata else None
        if adp is not None and adp > 0:
            insert_value(
                connection,
                run_id=run_id,
                player_key=resolution.player_key,
                season=season,
                value_type="draft_adp",
                position=position,
                rank_value=adp,
                observed_at=observed_at,
                context={"fantasypros_player_id": fp_id},
            )
            values_inserted += 1
    return {"matched": matched, "values_inserted": values_inserted}


def normalized_stats(record: dict[str, Any]) -> dict[str, float | int | str | None]:
    stats = record.get("stats")
    if not isinstance(stats, dict):
        return {}
    normalized: dict[str, float | int | str | None] = {}
    for key, value in stats.items():
        parsed = number(value)
        normalized[str(key)] = parsed if parsed is not None else value
    return normalized


def projected_points(stats: dict[str, Any]) -> float | None:
    normalized_keys = {str(key).lower(): value for key, value in stats.items()}
    for key in ("fpts", "fantasy_points", "points"):
        parsed = number(normalized_keys.get(key))
        if parsed is not None:
            return parsed
    return None


def normalize_projections(
    connection: sqlite3.Connection,
    records: list[dict[str, Any]],
    metadata_by_id: dict[str, dict[str, Any]],
    resolver: IdentityResolver,
    run_id: str,
    observed_at: str,
    season: int,
    dataset: str,
    unresolved: list[dict[str, Any]],
) -> dict[str, int]:
    matched = 0
    values_inserted = 0
    for index, record in enumerate(records, start=1):
        fp_id = fantasypros_id(record)
        metadata = metadata_by_id.get(fp_id)
        resolution = resolver.resolve(record, metadata)
        if resolution.player_key:
            resolution = resolver.register(connection, fp_id, resolution.player_key, observed_at)
        if not resolution.player_key:
            unresolved.append(
                unresolved_entry(
                    record,
                    metadata,
                    dataset=dataset,
                    position_order=index,
                    resolution=resolution,
                )
            )
            continue

        stats = normalized_stats(record)
        position = record_position(record, metadata)
        insert_value(
            connection,
            run_id=run_id,
            player_key=resolution.player_key,
            season=season,
            value_type="preseason_projection",
            position=position,
            numeric_value=projected_points(stats),
            observed_at=observed_at,
            context={"fantasypros_player_id": fp_id, "stats": stats},
        )
        matched += 1
        values_inserted += 1
    return {"matched": matched, "values_inserted": values_inserted}


Normalizer = Callable[[list[dict[str, Any]], str, str], dict[str, int]]


def ingest_endpoint(
    connection: sqlite3.Connection,
    *,
    dataset: str,
    url: str,
    season: int,
    api_key: str,
    timeout: int,
    pacer: RequestPacer,
    normalizer: Normalizer | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    run_id, observed_at = start_run(connection, dataset, season, url)
    raw_path: Path | None = None
    content_sha256: str | None = None
    started = time.monotonic()
    try:
        pacer.wait()
        payload, raw = fetch_json(url, api_key, timeout)
        records = records_from_payload(payload, minimum_rows=MINIMUM_ROWS[dataset])
        raw_path, content_sha256 = save_raw(dataset, raw, observed_at)
        normalized = normalizer(records, run_id, observed_at) if normalizer else {}
        notes = {
            "declared_count": integer(payload.get("count")),
            "source_rows": len(records),
            "public_api_limited_flag": bool(payload.get("public_api_limited")),
            **normalized,
        }
        finish_run(
            connection,
            run_id,
            status="success",
            raw_path=raw_path,
            content_sha256=content_sha256,
            row_count=len(records),
            notes=notes,
        )
        return (
            {
                "dataset": dataset,
                "status": "success",
                "declared_count": integer(payload.get("count")),
                "source_rows": len(records),
                "public_api_limited_flag": bool(payload.get("public_api_limited")),
                "matched": normalized.get("matched", 0),
                "values_inserted": normalized.get("values_inserted", 0),
                "run_id": run_id,
                "raw_path": raw_path.relative_to(ROOT).as_posix(),
                "content_sha256": content_sha256,
                "elapsed_seconds": round(time.monotonic() - started, 3),
            },
            payload,
        )
    except HTTPError as error:
        connection.rollback()
        error_text = f"HTTPError:{error.code}"
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        connection.rollback()
        error_text = type(error).__name__
    except Exception as error:
        connection.rollback()
        error_text = f"{type(error).__name__}:{str(error)[:300]}"

    finish_run(
        connection,
        run_id,
        status="failed",
        raw_path=raw_path,
        content_sha256=content_sha256,
        notes=error_text,
    )
    return (
        {
            "dataset": dataset,
            "status": "failed",
            "run_id": run_id,
            "error": error_text,
            "elapsed_seconds": round(time.monotonic() - started, 3),
        },
        None,
    )


def metadata_index(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    if payload is None:
        return {}
    return {
        fantasypros_id(record): record
        for record in records_from_payload(payload)
        if fantasypros_id(record)
    }


def reconcile_unresolved(
    unresolved: list[dict[str, Any]], resolver: IdentityResolver
) -> list[dict[str, Any]]:
    return [
        item
        for item in unresolved
        if not item.get("fantasypros_player_id")
        or str(item["fantasypros_player_id"]) not in resolver.fp_to_player
    ]


def relevant_player_count(unresolved: list[dict[str, Any]]) -> int:
    keys = {
        item.get("fantasypros_player_id")
        or "|".join(str(item.get(key) or "") for key in ("name", "team", "position"))
        for item in unresolved
        if item.get("draft_relevant")
    }
    return len(keys)


def database_checks(connection: sqlite3.Connection, season: int) -> dict[str, Any]:
    latest_counts = {
        str(value_type): int(count)
        for value_type, count in connection.execute(
            """
            SELECT value_type, COUNT(*)
            FROM latest_player_values
            WHERE season = ? AND week = 0 AND scoring_profile = 'ppr'
              AND value_type IN ('draft_ecr', 'draft_adp', 'preseason_projection')
            GROUP BY value_type
            ORDER BY value_type
            """,
            (season,),
        )
    }
    return {
        "fantasypros_player_ids": connection.execute(
            "SELECT COUNT(*) FROM player_ids WHERE source_key = ?", (SOURCE_KEY,)
        ).fetchone()[0],
        "latest_value_counts": latest_counts,
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_key_issues": len(connection.execute("PRAGMA foreign_key_check").fetchall()),
    }


def sync(season: int, db_path: Path, timeout: int) -> dict[str, Any]:
    load_local_env()
    api_key = os.environ.get("FANTASYPROS_API_KEY")
    if not api_key:
        raise RuntimeError("FANTASYPROS_API_KEY is not configured")

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    pacer = RequestPacer()
    unresolved: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    try:
        resolver = IdentityResolver(connection)
        endpoints = dataset_endpoints(season)

        players_dataset, players_url = endpoints[0]
        players_result, players_payload = ingest_endpoint(
            connection,
            dataset=players_dataset,
            url=players_url,
            season=season,
            api_key=api_key,
            timeout=timeout,
            pacer=pacer,
        )
        results.append(players_result)
        metadata_by_id = metadata_index(players_payload)

        ecr_dataset, ecr_url = endpoints[1]
        ecr_result, _ = ingest_endpoint(
            connection,
            dataset=ecr_dataset,
            url=ecr_url,
            season=season,
            api_key=api_key,
            timeout=timeout,
            pacer=pacer,
            normalizer=lambda records, run_id, observed_at: normalize_ecr(
                connection,
                records,
                metadata_by_id,
                resolver,
                run_id,
                observed_at,
                season,
                unresolved,
            ),
        )
        results.append(ecr_result)

        for dataset, url in endpoints[2:]:
            projection_result, _ = ingest_endpoint(
                connection,
                dataset=dataset,
                url=url,
                season=season,
                api_key=api_key,
                timeout=timeout,
                pacer=pacer,
                normalizer=lambda records, run_id, observed_at, dataset=dataset: normalize_projections(
                    connection,
                    records,
                    metadata_by_id,
                    resolver,
                    run_id,
                    observed_at,
                    season,
                    dataset,
                    unresolved,
                ),
            )
            results.append(projection_result)

        unresolved = reconcile_unresolved(unresolved, resolver)
        report = {
            "generated_at": utc_now(),
            "season": season,
            "unresolved_records": len(unresolved),
            "draft_relevant_players": relevant_player_count(unresolved),
            "records": unresolved,
        }
        REPORT_PATH.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        checks = database_checks(connection, season)
    finally:
        connection.close()

    failed = [result["dataset"] for result in results if result["status"] != "success"]
    return {
        "ok": not failed
        and report["draft_relevant_players"] == 0
        and checks["integrity"] == "ok"
        and checks["foreign_key_issues"] == 0,
        "season": season,
        "datasets": results,
        "identity": {
            "unresolved_records": report["unresolved_records"],
            "draft_relevant_players": report["draft_relevant_players"],
            "private_report": REPORT_PATH.relative_to(ROOT).as_posix(),
        },
        "database": checks,
        "failed_datasets": failed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--timeout", type=int, default=30)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = sync(args.season, args.db.resolve(), args.timeout)
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
