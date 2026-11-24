"""Deterministic draft simulation, resumable state, and pick recommendations."""

from __future__ import annotations

import argparse
import hashlib
import html
import itertools
import json
import math
import random
import sqlite3
import sys
import tempfile
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from draft_model import ALL_POSITIONS, DEFAULT_LEAGUE_SEED, load_league_settings
except ModuleNotFoundError:  # Support import as scripts.draft_session in tests.
    from scripts.draft_model import ALL_POSITIONS, DEFAULT_LEAGUE_SEED, load_league_settings


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".local" / "fantasy_gm.sqlite3"
DEFAULT_BOARD = ROOT / ".local" / "exports" / "draft-board.json"
DEFAULT_REHEARSAL_REPORT = ROOT / ".local" / "exports" / "draft-session-rehearsal.json"
ROOM_SCENARIOS = ("league_history", "high_rb", "high_wr", "early_qb", "early_dst")
SCENARIO_SEED_OFFSETS = {
    "league_history": 0,
    "high_rb": 101,
    "high_wr": 211,
    "early_qb": 307,
    "early_dst": 401,
}


class DraftConflictError(RuntimeError):
    """Raised when an observation conflicts with accepted draft state."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def load_board(path: Path = DEFAULT_BOARD, season: int | None = None) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    metadata = payload.get("metadata")
    players = payload.get("players")
    if not isinstance(metadata, dict) or not isinstance(players, list) or not players:
        raise ValueError(f"Draft board is missing metadata or players: {path}")
    if season is not None and int(metadata.get("season", 0)) != season:
        raise ValueError(f"Draft board season {metadata.get('season')} does not match {season}")
    keys = [str(player.get("player_key") or "") for player in players]
    if not all(keys) or len(keys) != len(set(keys)):
        raise ValueError("Draft board player keys must be present and unique")
    normalized_players = []
    for player in players:
        row = dict(player)
        row["position"] = "DEF" if row.get("position") == "DST" else str(row.get("position"))
        if row["position"] not in ALL_POSITIONS:
            continue
        normalized_players.append(row)
    return {"metadata": metadata, "players": normalized_players}


def _roster_rules(settings: dict[str, Any]) -> tuple[dict[str, int], dict[str, int], int]:
    roster = settings["roster"]
    starters = roster["starters"]
    minimums = {
        "QB": int(starters["QB"]),
        "RB": int(starters["RB"]),
        "WR": int(starters["WR"]),
        "TE": int(starters["TE"]),
        "K": int(starters["K"]),
        "DEF": int(starters["DST"]),
    }
    maximums = {
        position: int(roster["maximums"]["DST" if position == "DEF" else position])
        for position in ALL_POSITIONS
    }
    return minimums, maximums, int(roster["size"])


def is_legal_roster_counts(counts: dict[str, int], settings: dict[str, Any]) -> bool:
    minimums, maximums, roster_size = _roster_rules(settings)
    if sum(int(counts.get(position, 0)) for position in ALL_POSITIONS) != roster_size:
        return False
    if any(int(counts.get(position, 0)) < minimums[position] for position in ALL_POSITIONS):
        return False
    if any(int(counts.get(position, 0)) > maximums[position] for position in ALL_POSITIONS):
        return False
    starters = settings["roster"]["starters"]
    rb_wr_required = minimums["RB"] + minimums["WR"] + int(starters["RB_WR"])
    flex_required = rb_wr_required + minimums["TE"] + int(starters["FLEX"])
    if int(counts.get("RB", 0)) + int(counts.get("WR", 0)) < rb_wr_required:
        return False
    if sum(int(counts.get(position, 0)) for position in ("RB", "WR", "TE")) < flex_required:
        return False
    return True


def legal_final_shapes(settings: dict[str, Any]) -> tuple[dict[str, int], ...]:
    minimums, maximums, roster_size = _roster_rules(settings)
    ranges = [range(minimums[position], maximums[position] + 1) for position in ALL_POSITIONS]
    shapes: list[dict[str, int]] = []
    for values in itertools.product(*ranges):
        if sum(values) != roster_size:
            continue
        counts = dict(zip(ALL_POSITIONS, values))
        if is_legal_roster_counts(counts, settings):
            shapes.append(counts)
    if not shapes:
        raise ValueError("League settings produce no legal final roster shape")
    return tuple(shapes)


def can_finish_roster(
    counts: dict[str, int],
    settings: dict[str, Any],
    shapes: tuple[dict[str, int], ...] | None = None,
) -> bool:
    _, maximums, roster_size = _roster_rules(settings)
    normalized = {position: int(counts.get(position, 0)) for position in ALL_POSITIONS}
    if sum(normalized.values()) > roster_size:
        return False
    if any(normalized[position] > maximums[position] for position in ALL_POSITIONS):
        return False
    candidates = shapes or legal_final_shapes(settings)
    return any(all(shape[position] >= normalized[position] for position in ALL_POSITIONS) for shape in candidates)


def pick_coordinates(overall_pick: int, team_count: int) -> tuple[int, int, int]:
    if overall_pick < 1:
        raise ValueError("overall_pick must be positive")
    round_number = (overall_pick - 1) // team_count + 1
    round_pick = (overall_pick - 1) % team_count + 1
    team_slot = round_pick if round_number % 2 else team_count - round_pick + 1
    return round_number, round_pick, team_slot


def next_pick_for_slot(after_overall: int, user_slot: int, team_count: int, rounds: int) -> int | None:
    for overall_pick in range(after_overall + 1, team_count * rounds + 1):
        if pick_coordinates(overall_pick, team_count)[2] == user_slot:
            return overall_pick
    return None


def _pick_value(pick: Any, key: str) -> Any:
    if isinstance(pick, sqlite3.Row):
        return pick[key]
    return pick.get(key)


def draft_state_digest(picks: Iterable[Any]) -> str:
    stable = [
        {
            "overall_pick": int(_pick_value(pick, "overall_pick")),
            "player_key": _pick_value(pick, "player_key"),
            "team_key": _pick_value(pick, "team_key"),
        }
        for pick in sorted(picks, key=lambda item: int(_pick_value(item, "overall_pick")))
    ]
    return canonical_digest(stable)


def resolve_league_key(connection: sqlite3.Connection, season: int, league_key: str | None = None) -> str:
    if league_key:
        row = connection.execute(
            "SELECT league_key FROM leagues WHERE league_key = ? AND season = ?", (league_key, season)
        ).fetchone()
        if not row:
            raise ValueError("Requested league was not found for the season")
        return str(row[0])
    rows = connection.execute("SELECT league_key FROM leagues WHERE season = ?", (season,)).fetchall()
    if len(rows) != 1:
        raise ValueError(f"Expected one league for {season}; found {len(rows)}")
    return str(rows[0][0])


def load_draft_picks(connection: sqlite3.Connection, league_key: str, season: int) -> list[sqlite3.Row]:
    prior_factory = connection.row_factory
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(
            """
            SELECT league_key, season, overall_pick, round_number, round_pick,
                   team_key, player_key, picked_at, metadata_json
            FROM draft_picks
            WHERE league_key = ? AND season = ?
            ORDER BY overall_pick
            """,
            (league_key, season),
        ).fetchall()
    finally:
        connection.row_factory = prior_factory


def _assert_contiguous(picks: list[Any]) -> None:
    actual = [int(_pick_value(pick, "overall_pick")) for pick in picks]
    expected = list(range(1, len(actual) + 1))
    if actual != expected:
        raise DraftConflictError(f"Draft observations are not contiguous: expected {expected[-1:] or [0]}, got {actual[-1:] or [0]}")


def record_pick(
    connection: sqlite3.Connection,
    *,
    league_key: str,
    season: int,
    overall_pick: int,
    player_key: str,
    team_key: str | None = None,
    metadata: dict[str, Any] | None = None,
    picked_at: str | None = None,
    team_count: int = 12,
    rounds: int = 14,
) -> dict[str, Any]:
    if overall_pick > team_count * rounds:
        raise ValueError(f"overall_pick exceeds the {rounds}-round draft")
    round_number, round_pick, _ = pick_coordinates(overall_pick, team_count)
    if not connection.execute("SELECT 1 FROM players WHERE player_key = ?", (player_key,)).fetchone():
        raise ValueError("Observed player is not mapped to a canonical player")
    if team_key and not connection.execute(
        "SELECT 1 FROM teams WHERE team_key = ? AND league_key = ?", (team_key, league_key)
    ).fetchone():
        raise ValueError("Observed team is not mapped to this league")

    with connection:
        existing = connection.execute(
            """
            SELECT player_key, team_key FROM draft_picks
            WHERE league_key = ? AND season = ? AND overall_pick = ?
            """,
            (league_key, season, overall_pick),
        ).fetchone()
        if existing:
            existing_player, existing_team = existing
            compatible_team = existing_team == team_key or existing_team is None or team_key is None
            if str(existing_player) == player_key and compatible_team:
                return {"status": "duplicate", "overall_pick": overall_pick, "player_key": player_key}
            raise DraftConflictError(f"Overall pick {overall_pick} conflicts with accepted draft state")
        elsewhere = connection.execute(
            """
            SELECT overall_pick FROM draft_picks
            WHERE league_key = ? AND season = ? AND player_key = ?
            """,
            (league_key, season, player_key),
        ).fetchone()
        if elsewhere:
            raise DraftConflictError(f"Player is already recorded at overall pick {elsewhere[0]}")
        connection.execute(
            """
            INSERT INTO draft_picks (
                league_key, season, overall_pick, round_number, round_pick,
                team_key, player_key, picked_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                league_key,
                season,
                overall_pick,
                round_number,
                round_pick,
                team_key,
                player_key,
                picked_at or utc_now(),
                json.dumps(metadata or {}, sort_keys=True),
            ),
        )
    return {"status": "recorded", "overall_pick": overall_pick, "player_key": player_key}


def _roster_for_slot(
    picks: Iterable[Any], user_slot: int, team_count: int, board_by_key: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    roster = []
    for pick in picks:
        overall_pick = int(_pick_value(pick, "overall_pick"))
        if pick_coordinates(overall_pick, team_count)[2] != user_slot:
            continue
        player = board_by_key.get(str(_pick_value(pick, "player_key")))
        if not player:
            raise DraftConflictError(f"Drafted player at overall pick {overall_pick} is absent from the current board")
        roster.append(player)
    return roster


def _counts(players: Iterable[dict[str, Any]]) -> dict[str, int]:
    counter = Counter(str(player["position"]) for player in players)
    return {position: int(counter.get(position, 0)) for position in ALL_POSITIONS}


def _survival_probability(market_rank: float, next_user_pick: int | None) -> float:
    if next_user_pick is None:
        return 0.0
    if market_rank >= 900:
        return 0.98
    exponent = max(-50.0, min(50.0, -(market_rank - next_user_pick) / 8.0))
    return max(0.02, min(0.98, 1.0 / (1.0 + math.exp(exponent))))


def _tier_cliff(player: dict[str, Any], available: list[dict[str, Any]]) -> float:
    same_position = sorted(
        (row for row in available if row["position"] == player["position"]),
        key=lambda row: (int(row["custom_rank"]), float(row.get("market_rank") or 999)),
    )
    for index, row in enumerate(same_position):
        if row["player_key"] != player["player_key"]:
            continue
        # A tier-cliff bonus represents the cost of passing the best remaining
        # player at a position. It must not let a dominated same-position option
        # jump ahead of the better player that is still available.
        if index != 0:
            return 0.0
        if index + 1 >= len(same_position):
            return 0.0
        return max(0.0, float(row.get("vorp") or 0.0) - float(same_position[index + 1].get("vorp") or 0.0))
    return 0.0


def _candidate_score(
    player: dict[str, Any],
    *,
    counts: dict[str, int],
    available: list[dict[str, Any]],
    settings: dict[str, Any],
    target_overall: int,
    next_user_pick: int | None,
) -> tuple[float, float, float]:
    position = str(player["position"])
    round_number = pick_coordinates(target_overall, int(settings["team_count"]))[0]
    minimums, _, _ = _roster_rules(settings)
    market_rank = float(player.get("market_rank") or 999.0)
    survival = _survival_probability(market_rank, next_user_pick)
    cliff = _tier_cliff(player, available)
    score = float(player["custom_rank"])

    if counts[position] < minimums[position]:
        score -= 8.0 + max(0, round_number - 7) * 2.0
    starters = settings["roster"]["starters"]
    rb_wr_required = minimums["RB"] + minimums["WR"] + int(starters["RB_WR"])
    flex_required = rb_wr_required + minimums["TE"] + int(starters["FLEX"])
    if position in {"RB", "WR"} and counts["RB"] + counts["WR"] < rb_wr_required:
        score -= 5.0
    if position in {"RB", "WR", "TE"} and sum(counts[item] for item in ("RB", "WR", "TE")) < flex_required:
        score -= 4.0
    if position in {"RB", "WR"}:
        other = "WR" if position == "RB" else "RB"
        if counts[position] >= minimums[position] and counts[other] < minimums[other]:
            score += 12.0 * (counts[position] - minimums[position] + 1)
        imbalance = counts[position] - counts[other]
        if imbalance >= 2:
            score += 8.0 * (imbalance - 1)
        if counts[position] >= 4 and round_number <= 8:
            score += 8.0
    if position in {"QB", "TE", "K", "DEF"} and counts[position] >= minimums[position]:
        score += 45.0
    if position == "K" and round_number <= 10:
        score += 34.0
    if position == "DEF":
        score += 18.0 if round_number <= 6 else 7.0 if round_number <= 8 else 0.0

    score -= (1.0 - survival) * 8.0
    if market_rank < 900 and market_rank - target_overall > 18:
        score += min(18.0, (market_rank - target_overall - 18.0) * 0.25)
    score -= min(10.0, cliff / 6.0)
    if player.get("confidence") == "Low":
        score += 6.0
    elif player.get("confidence") == "Medium":
        score += 1.5
    return score, survival, cliff


def rank_recommendations(
    board: dict[str, Any],
    picks: list[Any],
    *,
    user_slot: int,
    settings: dict[str, Any],
    target_overall: int | None = None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    team_count = int(settings["team_count"])
    rounds = int(settings["draft"]["rounds"])
    if not 1 <= user_slot <= team_count:
        raise ValueError(f"user_slot must be between 1 and {team_count}")
    _assert_contiguous(picks)
    next_observation = len(picks) + 1
    target = target_overall or next_pick_for_slot(next_observation - 1, user_slot, team_count, rounds)
    if target is None:
        raise ValueError("The user roster has no remaining draft pick")
    if target < next_observation or pick_coordinates(target, team_count)[2] != user_slot:
        raise ValueError("target_overall is not an upcoming pick for the requested slot")

    players = board["players"]
    board_by_key = {str(player["player_key"]): player for player in players}
    drafted = {str(_pick_value(pick, "player_key")) for pick in picks}
    available = [player for player in players if str(player["player_key"]) not in drafted]
    roster = _roster_for_slot(picks, user_slot, team_count, board_by_key)
    counts = _counts(roster)
    shapes = legal_final_shapes(settings)
    next_user_pick = next_pick_for_slot(target, user_slot, team_count, rounds)
    ranked: list[tuple[float, dict[str, Any], float, float, dict[str, int]]] = []
    for player in available:
        resulting = dict(counts)
        resulting[str(player["position"])] += 1
        if not can_finish_roster(resulting, settings, shapes):
            continue
        score, survival, cliff = _candidate_score(
            player,
            counts=counts,
            available=available,
            settings=settings,
            target_overall=target,
            next_user_pick=next_user_pick,
        )
        ranked.append((score, player, survival, cliff, resulting))
    ranked.sort(key=lambda item: (item[0], int(item[1]["custom_rank"]), str(item[1]["name"])))
    if limit < 1:
        raise ValueError("limit must be positive")
    if not ranked:
        raise ValueError("No legal candidates remain")

    cards = []
    for score, player, survival, cliff, resulting in ranked[:limit]:
        next_text = "final pick" if next_user_pick is None else f"{survival:.0%} modeled chance to reach pick {next_user_pick}"
        rationale = (
            f"Tier {player['tier']} {player['position']} with {float(player.get('vorp') or 0):.1f} VORP; "
            f"{next_text}; roster becomes "
            + ", ".join(f"{position} {resulting[position]}" for position in ALL_POSITIONS if resulting[position])
            + "."
        )
        cards.append(
            {
                "player_key": player["player_key"],
                "name": player["name"],
                "team": player.get("team"),
                "position": player["position"],
                "custom_rank": int(player["custom_rank"]),
                "tier": int(player["tier"]),
                "vorp": float(player.get("vorp") or 0.0),
                "market_rank": float(player.get("market_rank") or 999.0),
                "target_range": player.get("target_range"),
                "confidence": player.get("confidence"),
                "risk": player.get("risk"),
                "target_overall": target,
                "next_user_pick": next_user_pick,
                "expected_next_pick_survival": round(survival, 4),
                "tier_cliff_points": round(cliff, 2),
                "resulting_roster": resulting,
                "decision_score": round(score, 4),
                "rationale": rationale,
            }
        )
    return cards


def build_pick_window(
    board: dict[str, Any], picks: list[Any], *, user_slot: int,
    settings: dict[str, Any],
) -> dict[str, Any]:
    """Prepare an advisory ladder for the next actual turn, never invent picks."""
    cards = rank_recommendations(board, picks, user_slot=user_slot, settings=settings, limit=10)
    target = cards[0]["target_overall"]
    team_count = int(settings["team_count"])
    rounds = int(settings["draft"]["rounds"])
    next_target = next_pick_for_slot(target, user_slot, team_count, rounds)
    opponents_before = target - len(picks) - 1
    drafted = {str(_pick_value(pick, "player_key")) for pick in picks}
    by_key = {player["player_key"]: player for player in board["players"]}
    available = [player for player in board["players"] if player["player_key"] not in drafted]
    counts = _counts(_roster_for_slot(picks, user_slot, team_count, by_key))
    shapes = legal_final_shapes(settings)
    pairs: list[dict[str, Any]] = []
    disjoint_count = 0
    if next_target == target + 1:
        after_pair = next_pick_for_slot(next_target, user_slot, team_count, rounds)
        combinations: dict[tuple[str, str], dict[str, Any]] = {}
        for first in cards:
            first_player = by_key[first["player_key"]]
            first_score = _candidate_score(
                first_player, counts=counts, available=available, settings=settings,
                target_overall=target, next_user_pick=after_pair,
            )[0]
            remaining = [player for player in available if player["player_key"] != first["player_key"]]
            second_choices = []
            for second in remaining:
                resulting = dict(first["resulting_roster"])
                resulting[second["position"]] += 1
                if not can_finish_roster(resulting, settings, shapes):
                    continue
                second_score = _candidate_score(
                    second, counts=first["resulting_roster"], available=remaining,
                    settings=settings, target_overall=next_target, next_user_pick=after_pair,
                )[0]
                second_choices.append((second_score, second["custom_rank"], second["name"], second, resulting))
            for second_score, _, _, second, resulting in sorted(second_choices, key=lambda item: item[:3])[:10]:
                key = tuple(sorted((first["player_key"], second["player_key"])))
                pair = {
                    "player_keys": [first["player_key"], second["player_key"]],
                    "names": [first["name"], second["name"]],
                    "positions": [first["position"], second["position"]],
                    "decision_score": round(first_score + second_score, 4),
                    "resulting_roster": resulting,
                }
                if key not in combinations or pair["decision_score"] < combinations[key]["decision_score"]:
                    combinations[key] = pair
        ranked_pairs = sorted(combinations.values(), key=lambda pair: (pair["decision_score"], pair["names"]))
        # Reserve five disjoint pairs before filling the ten best alternatives.
        # This provides a conservative, explicit fallback guarantee: removing
        # k players cannot destroy k+1 disjoint pairs.
        used: set[str] = set()
        for pair in ranked_pairs:
            if used.isdisjoint(pair["player_keys"]):
                pairs.append(pair)
                used.update(pair["player_keys"])
                disjoint_count += 1
                if disjoint_count == 5:
                    break
        for pair in ranked_pairs:
            if len(pairs) == 10:
                break
            if pair not in pairs:
                pairs.append(pair)
        pairs.sort(key=lambda pair: (pair["decision_score"], pair["names"]))
    coverage = disjoint_count - 1 if next_target == target + 1 else len(cards) - 1
    warnings = []
    if len(cards) < 10:
        warnings.append(f"Only {len(cards)} legal candidates remain; inspect availability before using this list.")
    if coverage < opponents_before:
        warnings.append("More opponent picks remain than this frozen list can cover; refresh closer to the turn.")
    phase = "on_clock" if opponents_before == 0 else "freeze" if opponents_before <= 2 else "prune" if opponents_before <= 4 else "prepare"
    return {
        "user_slot": user_slot, "round": pick_coordinates(target, team_count)[0],
        "target_overall": target, "next_user_pick": next_target,
        "observed_pick_count": len(picks), "opponents_before": opponents_before,
        "phase": phase, "state_digest": draft_state_digest(picks),
        "board_digest": board["metadata"].get("canonical_digest"),
        "board_generated_at": board["metadata"].get("generated_at"),
        "ladder": cards, "corner_pairs": pairs, "disjoint_pair_count": disjoint_count,
        "guaranteed_opponent_pick_coverage": coverage,
        "covered_until_turn": coverage >= opponents_before, "warnings": warnings,
        "instruction": "Use the first pair with both players available; follow its order." if pairs else "Use the highest available name in the ladder.",
        "external_action_executed": False,
    }


def render_windows_html(windows: list[dict[str, Any]], *, title: str, note: str) -> str:
    """Small standalone fallback sheet; works offline without a service."""
    escape = lambda value: html.escape(str(value))
    sections = []
    for window in windows:
        rows = "".join(
            f"<tr><td>{index}</td><td>{escape(card['name'])}</td><td>{escape(card['position'])}</td>"
            f"<td>{card['tier']}</td><td>{escape(card.get('risk') or '—')}</td></tr>"
            for index, card in enumerate(window["ladder"], 1)
        )
        pairs = "".join(f"<li>{escape(' + '.join(pair['names']))}</li>" for pair in window["corner_pairs"])
        warnings = " ".join(window["warnings"])
        sections.append(
            f"<section id='slot-{window['user_slot']}-round-{window['round']}'>"
            f"<h2>Slot {window['user_slot']} · Round {window['round']} · Pick {window['target_overall']}</h2>"
            f"<p>{escape(window['instruction'])} Opponent picks before turn: {window['opponents_before']}.</p>"
            f"<p class='warning'>{escape(warnings)}</p>"
            + (f"<h3>Corner pairs, in priority order</h3><ol>{pairs}</ol>" if pairs else "")
            + f"<table><thead><tr><th>#</th><th>Player</th><th>Pos</th><th>Tier</th><th>Risk</th></tr></thead><tbody>{rows}</tbody></table></section>"
        )
    slots = sorted({window["user_slot"] for window in windows})
    links = " · ".join(f"<a href='#slot-{slot}-round-1'>Slot {slot}</a>" for slot in slots) if len(slots) > 1 else ""
    return (
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{escape(title)}</title><style>body{{font:17px system-ui;max-width:1000px;margin:32px auto;padding:0 20px;background:#f8fafc;color:#152235}}"
        "section{background:white;padding:20px;margin:24px 0;border:1px solid #ccd4de;border-radius:8px}table{width:100%;border-collapse:collapse}"
        "th,td{text-align:left;padding:9px;border-bottom:1px solid #ddd}th{background:#e9eef5}.warning{color:#8c3c00}li{padding:5px}a{color:#0759a5}"
        "@media print{section{break-inside:avoid}body{font-size:12px}}</style>"
        f"<h1>{escape(title)}</h1><p>{escape(note)}</p><nav>{links}</nav>"
        + "".join(sections) + "</html>"
    )


def _recommendation_signature(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "recommendation_id": result["recommendation_id"],
        "state_digest": result["state_digest"],
        "target_overall": result["target_overall"],
        "primary": result["primary"],
        "alternatives": result["alternatives"],
    }


def recommend_from_connection(
    connection: sqlite3.Connection,
    *,
    board: dict[str, Any],
    settings: dict[str, Any],
    season: int,
    user_slot: int,
    league_key: str | None = None,
    persist: bool = True,
    target_overall: int | None = None,
) -> dict[str, Any]:
    resolved_league = resolve_league_key(connection, season, league_key)
    picks = load_draft_picks(connection, resolved_league, season)
    cards = rank_recommendations(
        board,
        picks,
        user_slot=user_slot,
        settings=settings,
        target_overall=target_overall,
    )
    state = draft_state_digest(picks)
    board_digest = str(board["metadata"].get("canonical_digest") or canonical_digest(board["players"]))
    target = int(cards[0]["target_overall"])
    identity = canonical_digest(
        {
            "league_key": resolved_league,
            "season": season,
            "user_slot": user_slot,
            "target_overall": target,
            "state_digest": state,
            "board_digest": board_digest,
        }
    )
    recommendation_id = f"draft-{identity[:32]}"
    primary = cards[0]
    alternatives = cards[1:]
    created_at = utc_now()
    if persist:
        with connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO recommendations (
                    recommendation_id, league_key, recommendation_type, season,
                    week, created_at, status, action_json, alternatives_json,
                    rationale, expected_value, risk_notes
                ) VALUES (?, ?, 'draft_pick', ?, 0, ?, 'proposed', ?, ?, ?, ?, ?)
                """,
                (
                    recommendation_id,
                    resolved_league,
                    season,
                    created_at,
                    json.dumps(
                        {
                            "action": "draft_player",
                            "external_action": "none",
                            "user_slot": user_slot,
                            "target_overall": target,
                            "state_digest": state,
                            "board_digest": board_digest,
                            "primary": primary,
                        },
                        sort_keys=True,
                    ),
                    json.dumps(alternatives, sort_keys=True),
                    primary["rationale"],
                    primary["vorp"],
                    f"{primary.get('risk') or '—'}; proposal only, no ESPN action executed",
                ),
            )
            stored = connection.execute(
                "SELECT created_at, status FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            created_at, status = str(stored[0]), str(stored[1])
    else:
        status = "proposed"
    return {
        "recommendation_id": recommendation_id,
        "status": status,
        "created_at": created_at,
        "season": season,
        "user_slot": user_slot,
        "target_overall": target,
        "state_digest": state,
        "board_digest": board_digest,
        "primary": primary,
        "alternatives": alternatives,
        "external_action_executed": False,
    }


def recommend_from_db(
    *,
    db_path: Path = DEFAULT_DB,
    board_path: Path = DEFAULT_BOARD,
    league_seed: Path = DEFAULT_LEAGUE_SEED,
    season: int = 2026,
    user_slot: int,
    league_key: str | None = None,
    persist: bool = True,
) -> dict[str, Any]:
    board = load_board(board_path, season)
    settings = load_league_settings(league_seed)
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        return recommend_from_connection(
            connection,
            board=board,
            settings=settings,
            season=season,
            user_slot=user_slot,
            league_key=league_key,
            persist=persist,
        )
    finally:
        connection.close()


def _scenario_bias(scenario: str, position: str, round_number: int) -> float:
    bias = 0.0
    if scenario == "league_history":
        if position == "QB" and 3 <= round_number <= 5:
            bias -= 4.0
        if position == "DEF" and 7 <= round_number <= 9:
            bias -= 5.0
    elif scenario == "high_rb" and position == "RB" and round_number <= 5:
        bias -= 10.0
    elif scenario == "high_wr" and position == "WR" and round_number <= 5:
        bias -= 10.0
    elif scenario == "early_qb" and position == "QB" and round_number <= 5:
        bias -= 16.0
    elif scenario == "early_dst" and position == "DEF" and 6 <= round_number <= 8:
        bias -= 20.0
    return bias


def _opponent_pick(
    *,
    available: list[dict[str, Any]],
    roster: list[dict[str, Any]],
    settings: dict[str, Any],
    overall_pick: int,
    scenario: str,
    rng: random.Random,
    shapes: tuple[dict[str, int], ...],
) -> dict[str, Any]:
    counts = _counts(roster)
    minimums, _, _ = _roster_rules(settings)
    round_number = pick_coordinates(overall_pick, int(settings["team_count"]))[0]
    scored = []
    for player in sorted(available, key=lambda row: (float(row.get("market_rank") or 999), int(row["custom_rank"])))[:100]:
        position = str(player["position"])
        resulting = dict(counts)
        resulting[position] += 1
        if not can_finish_roster(resulting, settings, shapes):
            continue
        market = float(player.get("market_rank") or 999.0)
        score = 0.85 * market + 0.15 * float(player["custom_rank"]) + rng.uniform(-5.0, 5.0)
        score += _scenario_bias(scenario, position, round_number)
        if counts[position] < minimums[position]:
            score -= 3.0 + max(0, round_number - 8) * 4.0
        if position in {"QB", "TE"} and counts[position] >= minimums[position] and round_number <= 9:
            score += 12.0
        if position in {"K", "DEF"} and counts[position] >= minimums[position]:
            score += 45.0
        if position == "K" and round_number <= 10:
            score += 40.0
        if position == "DEF":
            score += 24.0 if round_number <= 5 else 10.0 if round_number <= 7 else 0.0
        if player.get("confidence") == "Low":
            score += 6.0
        scored.append((score, int(player["custom_rank"]), str(player["name"]), player))
    if not scored:
        raise ValueError(f"No legal player available at overall pick {overall_pick}")
    scored.sort(key=lambda item: item[:3])
    return scored[0][3]


def simulate_draft(
    board: dict[str, Any],
    settings: dict[str, Any],
    *,
    user_slot: int,
    seed: int,
    scenario: str = "league_history",
    include_windows: bool = False,
) -> dict[str, Any]:
    if scenario not in ROOM_SCENARIOS:
        raise ValueError(f"Unknown room scenario: {scenario}")
    team_count = int(settings["team_count"])
    rounds = int(settings["draft"]["rounds"])
    rng = random.Random(seed + user_slot * 1009 + SCENARIO_SEED_OFFSETS[scenario])
    shapes = legal_final_shapes(settings)
    available = list(board["players"])
    picks: list[dict[str, Any]] = []
    rosters: dict[int, list[dict[str, Any]]] = {slot: [] for slot in range(1, team_count + 1)}
    user_cards: list[dict[str, Any]] = []

    for overall_pick in range(1, team_count * rounds + 1):
        round_number, round_pick, team_slot = pick_coordinates(overall_pick, team_count)
        if team_slot == user_slot:
            cards = rank_recommendations(
                board,
                picks,
                user_slot=user_slot,
                settings=settings,
                target_overall=overall_pick,
                limit=10 if include_windows else 4,
            )
            selected = next(player for player in available if player["player_key"] == cards[0]["player_key"])
            user_card = {"round": round_number, "overall_pick": overall_pick, "cards": cards}
            if include_windows:
                user_card["window"] = build_pick_window(board, picks, user_slot=user_slot, settings=settings)
            user_cards.append(user_card)
        else:
            selected = _opponent_pick(
                available=available,
                roster=rosters[team_slot],
                settings=settings,
                overall_pick=overall_pick,
                scenario=scenario,
                rng=rng,
                shapes=shapes,
            )
        pick = {
            "overall_pick": overall_pick,
            "round_number": round_number,
            "round_pick": round_pick,
            "team_slot": team_slot,
            "team_key": None,
            "player_key": selected["player_key"],
            "name": selected["name"],
            "position": selected["position"],
        }
        picks.append(pick)
        rosters[team_slot].append(selected)
        available.remove(selected)

    roster_counts = {slot: _counts(players) for slot, players in rosters.items()}
    legal = {slot: is_legal_roster_counts(counts, settings) for slot, counts in roster_counts.items()}
    return {
        "user_slot": user_slot,
        "scenario": scenario,
        "seed": seed,
        "picks": picks,
        "user_cards": user_cards,
        "roster_counts": roster_counts,
        "legal_rosters": legal,
        "all_legal": all(legal.values()),
    }


def _slot_plan(simulations: dict[str, dict[str, Any]], user_slot: int) -> dict[str, Any]:
    primary = simulations["league_history"]
    round_targets = []
    tier_cliffs = []
    for pick in primary["user_cards"]:
        cards = pick["cards"]
        primary_card = cards[0]
        target = {
            "round": pick["round"],
            "overall_pick": pick["overall_pick"],
            "primary": {
                key: primary_card[key]
                for key in (
                    "name",
                    "position",
                    "tier",
                    "market_rank",
                    "target_range",
                    "tier_cliff_points",
                    "expected_next_pick_survival",
                )
            },
            "fallbacks": [
                {key: card[key] for key in ("name", "position", "tier", "market_rank", "target_range")}
                for card in cards[1:]
            ],
        }
        if "window" in pick:
            target["window"] = pick["window"]
        round_targets.append(target)
        if float(primary_card["tier_cliff_points"]) >= 6.0:
            tier_cliffs.append(
                {
                    "round": pick["round"],
                    "overall_pick": pick["overall_pick"],
                    "position": primary_card["position"],
                    "cliff_points": primary_card["tier_cliff_points"],
                }
            )
    return {
        "slot": user_slot,
        "primary_scenario": "league_history",
        "legal": bool(primary["legal_rosters"][user_slot]),
        "final_roster_counts": primary["roster_counts"][user_slot],
        "round_targets": round_targets,
        "tier_cliffs": tier_cliffs,
        "scenario_results": {
            scenario: {
                "legal": bool(result["legal_rosters"][user_slot]),
                "roster_counts": result["roster_counts"][user_slot],
                "position_sequence": [
                    pick["position"] for pick in result["picks"] if pick["team_slot"] == user_slot
                ],
            }
            for scenario, result in simulations.items()
        },
    }


def simulate_slot_plans(
    board: dict[str, Any],
    settings: dict[str, Any],
    *,
    seed: int,
    slots: Iterable[int],
) -> dict[str, Any]:
    plans = []
    legal_rosters = 0
    simulations_run = 0
    for user_slot in slots:
        simulations = {
            scenario: simulate_draft(board, settings, user_slot=user_slot, seed=seed, scenario=scenario,
                                     include_windows=scenario == "league_history")
            for scenario in ROOM_SCENARIOS
        }
        simulations_run += len(simulations)
        legal_rosters += sum(
            int(result["legal_rosters"][user_slot]) for result in simulations.values()
        )
        plans.append(_slot_plan(simulations, user_slot))
    stable = {
        "season": int(board["metadata"]["season"]),
        "seed": seed,
        "scenarios_tested": list(ROOM_SCENARIOS),
        "slot_plans": plans,
    }
    return {
        **stable,
        "simulation_count": simulations_run,
        "legal_user_rosters": legal_rosters,
        "all_legal": legal_rosters == simulations_run,
        "reproducible_digest": canonical_digest(stable),
    }


def _open_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def rehearse_draft(
    *,
    db_path: Path = DEFAULT_DB,
    board_path: Path = DEFAULT_BOARD,
    league_seed: Path = DEFAULT_LEAGUE_SEED,
    output_path: Path = DEFAULT_REHEARSAL_REPORT,
    season: int = 2026,
    seed: int = 20260908,
    user_slot: int = 7,
    interrupt_after: int = 73,
) -> dict[str, Any]:
    board = load_board(board_path, season)
    settings = load_league_settings(league_seed)
    total_picks = int(settings["team_count"]) * int(settings["draft"]["rounds"])
    if not 1 <= interrupt_after < total_picks:
        raise ValueError(f"interrupt_after must be between 1 and {total_picks - 1}")
    planned = simulate_draft(board, settings, user_slot=user_slot, seed=seed, scenario="league_history")
    if not planned["all_legal"]:
        raise RuntimeError("Synthetic plan produced an illegal roster")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="draft-rehearsal-", dir=ROOT / ".local") as temp_dir:
        temp_db = Path(temp_dir) / "rehearsal.sqlite3"
        source = sqlite3.connect(db_path)
        destination = sqlite3.connect(temp_db)
        try:
            source.backup(destination)
        finally:
            source.close()
            destination.close()

        connection = _open_connection(temp_db)
        league_key = resolve_league_key(connection, season)
        with connection:
            connection.execute("DELETE FROM draft_picks WHERE league_key = ? AND season = ?", (league_key, season))
            connection.execute(
                "DELETE FROM recommendations WHERE league_key = ? AND season = ? AND recommendation_type = 'draft_pick'",
                (league_key, season),
            )

        duplicate_status = None
        conflict_failed_closed = False
        digest_before_resume = None
        digest_after_resume = None
        recommendation_before = None
        recommendation_after = None
        timing_samples: list[float] = []
        on_deck_cards = 0
        on_clock_cards = 0
        all_pick_cards_have_three_fallbacks = True
        player_keys = [str(player["player_key"]) for player in board["players"]]

        for planned_pick in planned["picks"]:
            overall_pick = int(planned_pick["overall_pick"])
            upcoming_user_pick = next_pick_for_slot(
                overall_pick - 1,
                user_slot,
                int(settings["team_count"]),
                int(settings["draft"]["rounds"]),
            )
            if upcoming_user_pick is not None and upcoming_user_pick - overall_pick == 2:
                on_deck = recommend_from_connection(
                    connection,
                    board=board,
                    settings=settings,
                    season=season,
                    user_slot=user_slot,
                    league_key=league_key,
                    target_overall=upcoming_user_pick,
                )
                on_deck_cards += 1
                all_pick_cards_have_three_fallbacks &= len(on_deck["alternatives"]) == 3
            if int(planned_pick["team_slot"]) == user_slot:
                live = recommend_from_connection(
                    connection,
                    board=board,
                    settings=settings,
                    season=season,
                    user_slot=user_slot,
                    league_key=league_key,
                    target_overall=overall_pick,
                )
                if live["primary"]["player_key"] != planned_pick["player_key"]:
                    raise RuntimeError(f"Rehearsal recommendation diverged at overall pick {overall_pick}")
                on_clock_cards += 1
                all_pick_cards_have_three_fallbacks &= len(live["alternatives"]) == 3
            recorded = record_pick(
                connection,
                league_key=league_key,
                season=season,
                overall_pick=overall_pick,
                player_key=str(planned_pick["player_key"]),
                team_count=int(settings["team_count"]),
                rounds=int(settings["draft"]["rounds"]),
                metadata={"fixture": "synthetic-12x14", "team_slot": planned_pick["team_slot"]},
            )
            if recorded["status"] != "recorded":
                raise RuntimeError(f"Unexpected replay status at overall pick {overall_pick}")

            if overall_pick == interrupt_after:
                duplicate_status = record_pick(
                    connection,
                    league_key=league_key,
                    season=season,
                    overall_pick=overall_pick,
                    player_key=str(planned_pick["player_key"]),
                    team_count=int(settings["team_count"]),
                    rounds=int(settings["draft"]["rounds"]),
                )["status"]
                conflicting_player = next(key for key in player_keys if key != planned_pick["player_key"])
                try:
                    record_pick(
                        connection,
                        league_key=league_key,
                        season=season,
                        overall_pick=overall_pick,
                        player_key=conflicting_player,
                        team_count=int(settings["team_count"]),
                        rounds=int(settings["draft"]["rounds"]),
                    )
                except DraftConflictError:
                    conflict_failed_closed = True

                for _ in range(10):
                    started = time.perf_counter()
                    recommendation_before = recommend_from_connection(
                        connection,
                        board=board,
                        settings=settings,
                        season=season,
                        user_slot=user_slot,
                        league_key=league_key,
                    )
                    timing_samples.append(time.perf_counter() - started)
                digest_before_resume = draft_state_digest(load_draft_picks(connection, league_key, season))
                connection.close()
                connection = _open_connection(temp_db)
                digest_after_resume = draft_state_digest(load_draft_picks(connection, league_key, season))
                recommendation_after = recommend_from_connection(
                    connection,
                    board=board,
                    settings=settings,
                    season=season,
                    user_slot=user_slot,
                    league_key=league_key,
                )

        final_picks = load_draft_picks(connection, league_key, season)
        board_by_key = {str(player["player_key"]): player for player in board["players"]}
        final_legal = {}
        for slot in range(1, int(settings["team_count"]) + 1):
            roster = _roster_for_slot(final_picks, slot, int(settings["team_count"]), board_by_key)
            final_legal[slot] = is_legal_roster_counts(_counts(roster), settings)
        proposed_records = connection.execute(
            """
            SELECT COUNT(*) FROM recommendations
            WHERE league_key = ? AND season = ? AND recommendation_type = 'draft_pick' AND status = 'proposed'
            """,
            (league_key, season),
        ).fetchone()[0]
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_key_issues = len(connection.execute("PRAGMA foreign_key_check").fetchall())
        connection.close()

    recommendation_equal = bool(
        recommendation_before
        and recommendation_after
        and _recommendation_signature(recommendation_before) == _recommendation_signature(recommendation_after)
    )
    max_timing = max(timing_samples) if timing_samples else math.inf
    report = {
        "generated_at": utc_now(),
        "fixture": "synthetic-12x14",
        "season": season,
        "seed": seed,
        "user_slot": user_slot,
        "interrupt_after": interrupt_after,
        "planned_pick_count": len(planned["picks"]),
        "planned_draft_digest": canonical_digest(planned["picks"]),
        "final_state_digest": draft_state_digest(final_picks),
        "interrupted_state_digest_before_close": digest_before_resume,
        "interrupted_state_digest_after_reopen": digest_after_resume,
        "resume_state_equal": digest_before_resume == digest_after_resume,
        "resume_recommendation_equal": recommendation_equal,
        "on_deck_pick_cards": on_deck_cards,
        "on_clock_pick_cards": on_clock_cards,
        "all_pick_cards_have_three_fallbacks": all_pick_cards_have_three_fallbacks,
        "duplicate_pick_result": duplicate_status,
        "conflict_failed_closed": conflict_failed_closed,
        "forced_data_interruption": True,
        "fallback_recommendation_available_after_resume": bool(
            recommendation_after and len(recommendation_after["alternatives"]) == 3
        ),
        "legal_rosters": sum(int(value) for value in final_legal.values()),
        "all_rosters_legal": all(final_legal.values()),
        "recommendation_timings_seconds": [round(value, 6) for value in timing_samples],
        "max_recommendation_seconds": round(max_timing, 6),
        "recommendations_under_two_seconds": bool(timing_samples) and all(value < 2.0 for value in timing_samples),
        "proposed_recommendation_records": int(proposed_records),
        "executed_external_actions": 0,
        "integrity": integrity,
        "foreign_key_issues": foreign_key_issues,
    }
    report["ok"] = all(
        (
            report["planned_pick_count"] == total_picks,
            report["resume_state_equal"],
            report["resume_recommendation_equal"],
            report["on_deck_pick_cards"] == int(settings["draft"]["rounds"]),
            report["on_clock_pick_cards"] == int(settings["draft"]["rounds"]),
            report["all_pick_cards_have_three_fallbacks"],
            report["duplicate_pick_result"] == "duplicate",
            report["conflict_failed_closed"],
            report["forced_data_interruption"],
            report["fallback_recommendation_available_after_resume"],
            report["all_rosters_legal"],
            report["recommendations_under_two_seconds"],
            report["proposed_recommendation_records"] > 0,
            report["integrity"] == "ok",
            report["foreign_key_issues"] == 0,
        )
    )
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    report["artifact"] = output_path.relative_to(ROOT).as_posix()
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--league-seed", type=Path, default=DEFAULT_LEAGUE_SEED)
    parser.add_argument("--season", type=int, default=2026)
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Record one observed pick idempotently")
    record.add_argument("--overall-pick", type=int, required=True)
    record.add_argument("--player-key", required=True)
    record.add_argument("--team-key")
    record.add_argument("--league-key")

    status = subparsers.add_parser("status", help="Show compact resumable state")
    status.add_argument("--league-key")

    rehearse = subparsers.add_parser("rehearse", help="Run the offline synthetic interruption rehearsal")
    rehearse.add_argument("--fixture", choices=("synthetic-12x14",), required=True)
    rehearse.add_argument("--interrupt-after", type=int, default=73)
    rehearse.add_argument("--seed", type=int, default=20260908)
    rehearse.add_argument("--user-slot", type=int, default=7)
    rehearse.add_argument("--board", type=Path, default=DEFAULT_BOARD)
    rehearse.add_argument("--output", type=Path, default=DEFAULT_REHEARSAL_REPORT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "rehearse":
            result = rehearse_draft(
                db_path=args.db.resolve(),
                board_path=args.board.resolve(),
                league_seed=args.league_seed.resolve(),
                output_path=args.output.resolve(),
                season=args.season,
                seed=args.seed,
                user_slot=args.user_slot,
                interrupt_after=args.interrupt_after,
            )
        else:
            settings = load_league_settings(args.league_seed.resolve())
            connection = _open_connection(args.db.resolve())
            try:
                league_key = resolve_league_key(connection, args.season, args.league_key)
                if args.command == "record":
                    result = record_pick(
                        connection,
                        league_key=league_key,
                        season=args.season,
                        overall_pick=args.overall_pick,
                        player_key=args.player_key,
                        team_key=args.team_key,
                        team_count=int(settings["team_count"]),
                        rounds=int(settings["draft"]["rounds"]),
                    )
                else:
                    picks = load_draft_picks(connection, league_key, args.season)
                    _assert_contiguous(picks)
                    result = {
                        "season": args.season,
                        "recorded_picks": len(picks),
                        "next_overall_pick": len(picks) + 1,
                        "state_digest": draft_state_digest(picks),
                    }
            finally:
                connection.close()
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
