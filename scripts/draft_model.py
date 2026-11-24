"""League-specific scoring and replacement-value model for the 2026 draft board."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LEAGUE_SEED = ROOT / ".local" / "league.json"
SCORING_PROFILE = "espn_custom_2026"
HISTORY_SEASON = 2025
SEASON_GAMES = 17
OFFENSE_POSITIONS = {"QB", "RB", "WR", "TE"}
ALL_POSITIONS = ("QB", "RB", "WR", "TE", "K", "DEF")
TEAM_ALIASES = {"AZ": "ARI", "JAC": "JAX", "LA": "LAR", "OAK": "LV", "SD": "LAC", "STL": "LAR", "WSH": "WAS"}


def num(value: Any) -> float:
    if value in (None, "") or isinstance(value, bool):
        return 0.0
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if math.isfinite(parsed) else 0.0


def normalized_team(value: Any) -> str:
    team = str(value or "").strip().upper()
    return TEAM_ALIASES.get(team, team)


def load_league_settings(path: Path = DEFAULT_LEAGUE_SEED) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    settings = payload.get("league", {}).get("settings")
    if not isinstance(settings, dict):
        raise ValueError(f"League seed has no settings object: {path}")
    return settings


def points_allowed_score(points: float, table: dict[str, Any]) -> float:
    if points <= 0:
        return num(table["0"])
    if points <= 6:
        return num(table["1_6"])
    if points <= 13:
        return num(table["7_13"])
    if points <= 17:
        return num(table["14_17"])
    if points <= 21:
        return num(table["18_21"])
    if points <= 27:
        return num(table["22_27"])
    if points <= 34:
        return num(table["28_34"])
    if points <= 45:
        return num(table["35_45"])
    return num(table["46_plus"])


def yards_allowed_score(yards: float, table: dict[str, Any]) -> float:
    if yards < 100:
        return num(table["under_100"])
    if yards < 200:
        return num(table["100_199"])
    if yards < 300:
        return num(table["200_299"])
    if yards < 350:
        return num(table["300_349"])
    if yards < 400:
        return num(table["350_399"])
    if yards < 450:
        return num(table["400_449"])
    if yards < 500:
        return num(table["450_499"])
    if yards < 550:
        return num(table["500_549"])
    return num(table["550_plus"])


def yardage_bonus(yards: float, *, lower: float, upper: float, lower_points: float, upper_points: float) -> float:
    if yards >= upper:
        return upper_points
    if yards >= lower:
        return lower_points
    return 0.0


def blend_rate(
    player_numerator: float,
    player_denominator: float,
    fallback_rate: float,
    full_weight_volume: float,
) -> tuple[float, bool]:
    if player_denominator <= 0:
        return fallback_rate, False
    player_rate = player_numerator / player_denominator
    weight = min(0.65, 0.65 * player_denominator / full_weight_volume)
    return weight * player_rate + (1.0 - weight) * fallback_rate, True


def _metric_rows(connection: sqlite3.Connection, season: int) -> list[sqlite3.Row]:
    metrics = (
        "actual_attempts",
        "actual_sacks_suffered",
        "actual_passing_yards",
        "actual_rushing_yards",
        "actual_receiving_yards",
        "actual_fg_made",
        "actual_fg_att",
        "actual_fg_long",
        "actual_pat_made",
        "actual_pat_att",
        "actual_kickoff_return_yards",
        "actual_punt_return_yards",
    )
    placeholders = ",".join("?" for _ in metrics)
    return connection.execute(
        f"""
        SELECT player_key, position, week, value_type, numeric_value, context_json
        FROM latest_player_values
        WHERE season = ? AND scoring_profile = 'nfl'
          AND value_type IN ({placeholders})
        """,
        (season, *metrics),
    ).fetchall()


def load_history(
    connection: sqlite3.Connection,
    scoring: dict[str, Any],
    season: int = HISTORY_SEASON,
) -> dict[str, Any]:
    player_weeks: dict[str, dict[int, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    player_positions: dict[str, str] = {}
    team_game_yards: dict[tuple[str, str], float] = defaultdict(float)
    team_return_yards: dict[str, float] = defaultdict(float)

    for row in _metric_rows(connection, season):
        context = json.loads(row["context_json"] or "{}")
        metric = str(row["value_type"])
        value = num(row["numeric_value"])
        player_key = str(row["player_key"])
        week = int(row["week"])
        player_weeks[player_key][week][metric] = value
        player_positions[player_key] = str(row["position"] or "")
        team = normalized_team(context.get("team"))
        game_id = str(context.get("game_id") or "")
        if team and game_id and metric in ("actual_passing_yards", "actual_rushing_yards"):
            team_game_yards[(team, game_id)] += value
        if team and metric in ("actual_kickoff_return_yards", "actual_punt_return_yards"):
            team_return_yards[team] += value

    profiles: dict[str, dict[str, float]] = {}
    league_totals: dict[tuple[str, str], list[float]] = defaultdict(lambda: [0.0, 0.0])
    passing = scoring["passing"]
    rushing = scoring["rushing"]
    receiving = scoring["receiving"]

    for player_key, weeks in player_weeks.items():
        position = player_positions.get(player_key, "")
        profile = defaultdict(float)
        for metrics in weeks.values():
            pass_yards = metrics.get("actual_passing_yards", 0.0)
            rush_yards = metrics.get("actual_rushing_yards", 0.0)
            rec_yards = metrics.get("actual_receiving_yards", 0.0)
            profile["pass_yards"] += pass_yards
            profile["pass_bonus"] += yardage_bonus(
                pass_yards,
                lower=300,
                upper=400,
                lower_points=num(passing["yards_300_399_bonus"]),
                upper_points=num(passing["yards_400_plus_bonus"]),
            )
            profile["rush_yards"] += rush_yards
            profile["rush_bonus"] += yardage_bonus(
                rush_yards,
                lower=100,
                upper=200,
                lower_points=num(rushing["yards_100_199_bonus"]),
                upper_points=num(rushing["yards_200_plus_bonus"]),
            )
            profile["rec_yards"] += rec_yards
            profile["rec_bonus"] += yardage_bonus(
                rec_yards,
                lower=100,
                upper=200,
                lower_points=num(receiving["yards_100_199_bonus"]),
                upper_points=num(receiving["yards_200_plus_bonus"]),
            )
            profile["pass_attempts"] += metrics.get("actual_attempts", 0.0)
            profile["sacks"] += metrics.get("actual_sacks_suffered", 0.0)
            fg_made = metrics.get("actual_fg_made", 0.0)
            fg_long = metrics.get("actual_fg_long", 0.0)
            profile["fg_made"] += fg_made
            profile["fg_attempts"] += metrics.get("actual_fg_att", 0.0)
            profile["pat_made"] += metrics.get("actual_pat_made", 0.0)
            profile["pat_attempts"] += metrics.get("actual_pat_att", 0.0)
            if fg_made > 0:
                if fg_long >= 50:
                    profile["fg_distance_bonus"] += 2.0
                elif fg_long >= 40:
                    profile["fg_distance_bonus"] += 1.0
        profiles[player_key] = dict(profile)
        for kind in ("pass", "rush", "rec"):
            league_totals[(position, f"{kind}_bonus")][0] += profile[f"{kind}_bonus"]
            league_totals[(position, f"{kind}_bonus")][1] += profile[f"{kind}_yards"]
        league_totals[(position, "sacks")][0] += profile["sacks"]
        league_totals[(position, "sacks")][1] += profile["pass_attempts"]
        league_totals[(position, "fg_distance")][0] += profile["fg_distance_bonus"]
        league_totals[(position, "fg_distance")][1] += profile["fg_made"]
        league_totals[(position, "pat_miss")][0] += max(profile["pat_attempts"] - profile["pat_made"], 0.0)
        league_totals[(position, "pat_miss")][1] += profile["pat_made"]

    fallback_rates = {
        key: numerator / denominator if denominator > 0 else 0.0
        for key, (numerator, denominator) in league_totals.items()
    }
    dst = load_dst_history(
        connection,
        team_game_yards,
        team_return_yards,
        scoring["dst"],
        season,
    )
    return {"profiles": profiles, "fallback_rates": fallback_rates, "dst": dst}


def _regressed_season_total(team_values: list[float], league_values: list[float]) -> tuple[float, bool]:
    if not league_values:
        return 0.0, False
    league_rate = sum(league_values) / len(league_values)
    if not team_values:
        return league_rate * SEASON_GAMES, False
    team_rate = sum(team_values) / len(team_values)
    weight = min(0.60, 0.60 * len(team_values) / SEASON_GAMES)
    return (weight * team_rate + (1.0 - weight) * league_rate) * SEASON_GAMES, True


def load_dst_history(
    connection: sqlite3.Connection,
    team_game_yards: dict[tuple[str, str], float],
    team_return_yards: dict[str, float],
    dst_scoring: dict[str, Any],
    season: int,
) -> dict[str, dict[str, Any]]:
    points_by_team: dict[str, list[float]] = defaultdict(list)
    yards_by_team: dict[str, list[float]] = defaultdict(list)
    games_by_team: dict[str, int] = defaultdict(int)
    games = connection.execute(
        """
        SELECT game_id, away_team, home_team, away_score, home_score
        FROM latest_nfl_games
        WHERE season = ? AND game_type = 'REG'
          AND away_score IS NOT NULL AND home_score IS NOT NULL
        """,
        (season,),
    ).fetchall()
    for game in games:
        game_id = str(game["game_id"])
        away = normalized_team(game["away_team"])
        home = normalized_team(game["home_team"])
        games_by_team[away] += 1
        games_by_team[home] += 1
        points_by_team[away].append(points_allowed_score(num(game["home_score"]), dst_scoring["points_allowed"]))
        points_by_team[home].append(points_allowed_score(num(game["away_score"]), dst_scoring["points_allowed"]))
        home_offense_yards = team_game_yards.get((home, game_id))
        away_offense_yards = team_game_yards.get((away, game_id))
        if home_offense_yards is not None:
            yards_by_team[away].append(yards_allowed_score(home_offense_yards, dst_scoring["yards_allowed"]))
        if away_offense_yards is not None:
            yards_by_team[home].append(yards_allowed_score(away_offense_yards, dst_scoring["yards_allowed"]))

    league_points = [value for values in points_by_team.values() for value in values]
    league_yards = [value for values in yards_by_team.values() for value in values]
    return_rate_values = [
        yards / games_by_team[team]
        for team, yards in team_return_yards.items()
        if games_by_team.get(team, 0) > 0
    ]
    league_return_rate = sum(return_rate_values) / len(return_rate_values) if return_rate_values else 0.0

    result: dict[str, dict[str, Any]] = {}
    for team in set(games_by_team) | set(team_return_yards):
        point_total, point_history = _regressed_season_total(points_by_team.get(team, []), league_points)
        yard_total, yard_history = _regressed_season_total(yards_by_team.get(team, []), league_yards)
        games_played = games_by_team.get(team, 0)
        if games_played:
            team_return_rate = team_return_yards.get(team, 0.0) / games_played
            weight = min(0.60, 0.60 * games_played / SEASON_GAMES)
            return_yards = (weight * team_return_rate + (1.0 - weight) * league_return_rate) * SEASON_GAMES
            return_history = team in team_return_yards
        else:
            return_yards = league_return_rate * SEASON_GAMES
            return_history = False
        result[team] = {
            "points_allowed_points": point_total,
            "yards_allowed_points": yard_total,
            "return_yards": return_yards,
            "history_available": point_history and yard_history and return_history,
        }
    return result


def _history_rate(
    history: dict[str, Any],
    player_key: str,
    position: str,
    numerator: str,
    denominator: str,
    fallback_metric: str,
    full_weight_volume: float,
) -> tuple[float, bool]:
    profile = history["profiles"].get(player_key, {})
    fallback = history["fallback_rates"].get((position, fallback_metric), 0.0)
    return blend_rate(
        num(profile.get(numerator)),
        num(profile.get(denominator)),
        fallback,
        full_weight_volume,
    )


def score_offense(
    player_key: str,
    position: str,
    stats: dict[str, Any],
    scoring: dict[str, Any],
    history: dict[str, Any],
) -> dict[str, Any]:
    passing = scoring["passing"]
    rushing = scoring["rushing"]
    receiving = scoring["receiving"]
    misc = scoring["miscellaneous"]
    direct = {
        "passing": num(stats.get("pass_yds")) * num(passing["yard"])
        + num(stats.get("pass_tds")) * num(passing["touchdown"])
        + num(stats.get("pass_ints")) * num(passing["interception"]),
        "rushing": num(stats.get("rush_yds")) * num(rushing["yard"])
        + num(stats.get("rush_tds")) * num(rushing["touchdown"]),
        "receiving": num(stats.get("rec_rec")) * num(receiving["reception"])
        + num(stats.get("rec_yds")) * num(receiving["yard"])
        + num(stats.get("rec_tds")) * num(receiving["touchdown"]),
        "miscellaneous": num(stats.get("2pt_tds")) * num(passing["two_point_conversion"])
        + num(stats.get("ret_tds")) * num(misc["kickoff_return_touchdown"])
        + num(stats.get("fumbles")) * num(misc["fumble_lost"]),
    }
    pass_rate, pass_history = _history_rate(history, player_key, position, "pass_bonus", "pass_yards", "pass_bonus", 4000)
    rush_rate, rush_history = _history_rate(history, player_key, position, "rush_bonus", "rush_yards", "rush_bonus", 1000)
    rec_rate, rec_history = _history_rate(history, player_key, position, "rec_bonus", "rec_yards", "rec_bonus", 1200)
    sack_rate, sack_history = _history_rate(history, player_key, position, "sacks", "pass_attempts", "sacks", 500)
    estimated = {
        "passing_yardage_bonus": num(stats.get("pass_yds")) * pass_rate,
        "rushing_yardage_bonus": num(stats.get("rush_yds")) * rush_rate,
        "receiving_yardage_bonus": num(stats.get("rec_yds")) * rec_rate,
        "sacks_taken": num(stats.get("pass_att")) * sack_rate * num(passing["sack_taken"]),
        "long_touchdown_bonus": num(stats.get("pass_tds"))
        * (0.10 * num(passing["touchdown_40_plus_bonus"]) + 0.05 * num(passing["touchdown_50_plus_bonus"]))
        + num(stats.get("rush_tds"))
        * (0.08 * num(rushing["touchdown_40_plus_bonus"]) + 0.04 * num(rushing["touchdown_50_plus_bonus"]))
        + num(stats.get("rec_tds"))
        * (0.10 * num(receiving["touchdown_40_plus_bonus"]) + 0.05 * num(receiving["touchdown_50_plus_bonus"])),
    }
    return scoring_result(
        direct,
        estimated,
        history_available=pass_history or rush_history or rec_history or sack_history,
        notes=["Long-touchdown bonuses use conservative projection-rate estimates."],
    )


def score_kicker(
    player_key: str,
    stats: dict[str, Any],
    scoring: dict[str, Any],
    history: dict[str, Any],
) -> dict[str, Any]:
    kicking = scoring["kicking"]
    made = num(stats.get("fg"))
    attempts = num(stats.get("fga"))
    pats = num(stats.get("xpt"))
    distance_rate, distance_history = _history_rate(history, player_key, "K", "fg_distance_bonus", "fg_made", "fg_distance", 35)
    pat_miss_rate, pat_history = _history_rate(history, player_key, "K", "pat_attempts", "pat_made", "pat_miss", 35)
    profile = history["profiles"].get(player_key, {})
    if profile:
        player_misses = max(num(profile.get("pat_attempts")) - num(profile.get("pat_made")), 0.0)
        fallback = history["fallback_rates"].get(("K", "pat_miss"), 0.0)
        pat_miss_rate, pat_history = blend_rate(player_misses, num(profile.get("pat_made")), fallback, 35)
    direct = {
        "field_goals_base": made * num(kicking["field_goal_0_39"]),
        "field_goal_misses": max(attempts - made, 0.0) * num(kicking["field_goal_missed"]),
        "pat_made": pats * num(kicking["pat_made"]),
    }
    estimated = {
        "field_goal_distance_bonus": made * distance_rate,
        "pat_misses": pats * pat_miss_rate * num(kicking["pat_missed"]),
    }
    return scoring_result(
        direct,
        estimated,
        history_available=distance_history or pat_history,
        notes=["Distance bonus is a conservative lower-bound estimate from 2025 weekly longest kicks."],
    )


def score_defense(
    team: str,
    stats: dict[str, Any],
    scoring: dict[str, Any],
    history: dict[str, Any],
) -> dict[str, Any]:
    dst = scoring["dst"]
    team_history = history["dst"].get(normalized_team(team), {})
    direct = {
        "sacks": num(stats.get("def_sack")) * num(dst["sack"]),
        "interceptions": num(stats.get("def_int")) * num(dst["interception"]),
        "fumble_recoveries": num(stats.get("def_fr")) * num(dst["fumble_recovery"]),
        "safeties": num(stats.get("def_safety")) * num(dst["safety"]),
        "touchdowns": num(stats.get("def_td")) * num(dst["return_touchdown"]),
    }
    estimated = {
        "return_yards": num(team_history.get("return_yards"))
        * (num(dst["kickoff_return_yard"]) + num(dst["punt_return_yard"]))
        / 2.0,
        "points_allowed": num(team_history.get("points_allowed_points")),
        "yards_allowed": num(team_history.get("yards_allowed_points")),
        "tackles_neutral": 1000.0 * num(dst["total_tackle"]),
        "blocked_kicks_neutral": 0.5 * num(dst["blocked_kick"]),
    }
    return scoring_result(
        direct,
        estimated,
        history_available=bool(team_history.get("history_available")),
        notes=[
            "Return, points-allowed, and yards-allowed values regress 2025 team history toward league average.",
            "Tackles and blocked kicks use neutral estimates; rare return conversions are not projected.",
        ],
    )


def scoring_result(
    direct: dict[str, float],
    estimated: dict[str, float],
    *,
    history_available: bool,
    notes: list[str],
) -> dict[str, Any]:
    direct_total = sum(direct.values())
    estimated_total = sum(estimated.values())
    return {
        "direct_components": direct,
        "estimated_components": estimated,
        "direct_points": direct_total,
        "estimated_points": estimated_total,
        "custom_points": direct_total + estimated_total,
        "history_available": history_available,
        "notes": notes,
    }


def load_projection_rows(connection: sqlite3.Connection, season: int) -> list[dict[str, Any]]:
    ecr: dict[str, tuple[float, dict[str, Any]]] = {}
    adp: dict[str, float] = {}
    for row in connection.execute(
        """
        SELECT player_key, value_type, rank_value, context_json
        FROM latest_player_values
        WHERE season = ? AND week = 0 AND scoring_profile = 'ppr'
          AND value_type IN ('draft_ecr', 'draft_adp')
        """,
        (season,),
    ):
        if row["value_type"] == "draft_ecr":
            ecr[str(row["player_key"])] = (num(row["rank_value"]), json.loads(row["context_json"] or "{}"))
        else:
            adp[str(row["player_key"])] = num(row["rank_value"])

    rows: list[dict[str, Any]] = []
    for row in connection.execute(
        """
        SELECT pv.player_key, pv.position, pv.context_json,
               p.canonical_name, p.nfl_team, p.active, p.metadata_json
        FROM latest_player_values AS pv
        JOIN players AS p ON p.player_key = pv.player_key
        WHERE pv.season = ? AND pv.week = 0 AND pv.scoring_profile = 'ppr'
          AND pv.value_type = 'preseason_projection'
        """,
        (season,),
    ):
        player_key = str(row["player_key"])
        projection_context = json.loads(row["context_json"] or "{}")
        player_metadata = json.loads(row["metadata_json"] or "{}")
        ecr_rank, ecr_context = ecr.get(player_key, (0.0, {}))
        adp_rank = adp.get(player_key, 0.0)
        market_values = [value for value in (ecr_rank, adp_rank) if value > 0]
        rows.append(
            {
                "player_key": player_key,
                "name": str(row["canonical_name"]),
                "team": normalized_team(row["nfl_team"]),
                "position": str(row["position"]),
                "active": bool(row["active"]),
                "player_metadata": player_metadata,
                "stats": projection_context.get("stats", {}),
                "ecr": ecr_rank or None,
                "adp": adp_rank or None,
                "market_rank": sum(market_values) / len(market_values) if market_values else 999.0,
                "bye": ecr_context.get("player_bye_week"),
                "consensus_tier": ecr_context.get("tier"),
            }
        )
    return rows


def allocate_draft_pool(rows: list[dict[str, Any]], settings: dict[str, Any]) -> dict[str, Any]:
    team_count = int(settings["team_count"])
    roster = settings["roster"]
    starters = roster["starters"]
    maximums = roster["maximums"]
    minimums = {
        "QB": team_count * int(starters["QB"]),
        "RB": team_count * int(starters["RB"]),
        "WR": team_count * int(starters["WR"]),
        "TE": team_count * int(starters["TE"]),
        "K": team_count * int(starters["K"]),
        "DEF": team_count * int(starters["DST"]),
    }
    caps = {
        position: team_count * int(maximums["DST" if position == "DEF" else position])
        for position in ALL_POSITIONS
    }
    by_market = sorted(rows, key=lambda row: (row["market_rank"], -row["custom_points"], row["name"]))
    selected: set[str] = set()
    counts = defaultdict(int)

    def take_candidates(eligible: set[str], count: int) -> None:
        added = 0
        for row in by_market:
            if added >= count:
                return
            if row["player_key"] in selected or row["position"] not in eligible:
                continue
            if counts[row["position"]] >= caps[row["position"]]:
                continue
            selected.add(row["player_key"])
            counts[row["position"]] += 1
            added += 1
        if added != count:
            raise ValueError(f"Draft pool lacked {count - added} eligible players for {sorted(eligible)}")

    for position, count in minimums.items():
        take_candidates({position}, count)
    take_candidates({"RB", "WR"}, team_count * int(starters["RB_WR"]))
    take_candidates({"RB", "WR", "TE"}, team_count * int(starters["FLEX"]))
    take_candidates(set(ALL_POSITIONS), team_count * int(roster["bench"]))

    expected = team_count * int(roster["size"])
    if len(selected) != expected:
        raise ValueError(f"Draft pool size mismatch: selected={len(selected)}, expected={expected}")

    replacements: dict[str, dict[str, float | int | str]] = {}
    for position in ALL_POSITIONS:
        unselected = [row for row in rows if row["position"] == position and row["player_key"] not in selected]
        if not unselected:
            raise ValueError(f"No replacement player available at {position}")
        replacement = max(unselected, key=lambda row: (row["custom_points"], -row["market_rank"]))
        replacements[position] = {
            "player_key": replacement["player_key"],
            "points": replacement["custom_points"],
            "rank": counts[position] + 1,
        }
    return {"selected": selected, "counts": dict(counts), "replacements": replacements, "size": len(selected)}


def assign_position_tiers(rows: list[dict[str, Any]]) -> None:
    thresholds = {"QB": 12.0, "RB": 12.0, "WR": 12.0, "TE": 10.0, "K": 8.0, "DEF": 12.0}
    for position in ALL_POSITIONS:
        ordered = sorted(
            (row for row in rows if row["position"] == position),
            key=lambda row: (-row["custom_points"], row["market_rank"]),
        )
        tier = 1
        tier_size = 0
        previous: float | None = None
        for row in ordered:
            points = row["custom_points"]
            if previous is not None and (previous - points >= thresholds[position] or tier_size >= 12):
                tier += 1
                tier_size = 0
            row["tier"] = tier
            tier_size += 1
            previous = points


def player_risk(row: dict[str, Any]) -> tuple[str, str]:
    metadata = row.get("player_metadata", {})
    flags: list[str] = []
    injury = metadata.get("injury_status") if isinstance(metadata, dict) else None
    status = metadata.get("status") if isinstance(metadata, dict) else None
    if injury:
        flags.append(str(injury))
    if not row.get("active") or status not in (None, "Active", "ACT"):
        if status:
            flags.append(str(status))
        elif not row.get("active"):
            flags.append("inactive")
    estimate_share = abs(row["estimated_points"]) / max(abs(row["custom_points"]), 1.0)
    if estimate_share >= 0.25:
        flags.append("estimate-heavy")
    if row["market_rank"] >= 999:
        flags.append("market-unranked")
    confidence = "High"
    if row["position"] in {"K", "DEF"} or not row["history_available"] or estimate_share >= 0.15:
        confidence = "Medium"
    if injury or not row.get("active"):
        confidence = "Low"
    return confidence, ", ".join(dict.fromkeys(flags)) or "—"


def build_board_rows(
    connection: sqlite3.Connection,
    settings: dict[str, Any],
    season: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    history = load_history(connection, settings["scoring"])
    rows = load_projection_rows(connection, season)
    if not rows:
        raise ValueError("No current FantasyPros projection rows were found")

    for row in rows:
        if row["position"] in OFFENSE_POSITIONS:
            score = score_offense(row["player_key"], row["position"], row["stats"], settings["scoring"], history)
        elif row["position"] == "K":
            score = score_kicker(row["player_key"], row["stats"], settings["scoring"], history)
        elif row["position"] == "DEF":
            score = score_defense(row["team"], row["stats"], settings["scoring"], history)
        else:
            continue
        row.update(score)

    pool = allocate_draft_pool(rows, settings)
    for row in rows:
        replacement = pool["replacements"][row["position"]]
        row["replacement_points"] = num(replacement["points"])
        row["replacement_rank"] = int(replacement["rank"])
        row["vorp"] = row["custom_points"] - row["replacement_points"]

    assign_position_tiers(rows)
    rows.sort(key=lambda row: (-row["vorp"], row["market_rank"], -row["custom_points"], row["name"]))
    for rank, row in enumerate(rows, start=1):
        row["custom_rank"] = rank
        row["price_gap"] = row["market_rank"] - rank if row["market_rank"] < 999 else None
        if row["market_rank"] < 999:
            center = int(round(row["market_rank"]))
            row["target_range"] = f"{max(1, center - 6)}–{center + 6}"
        else:
            row["target_range"] = "watchlist"
        row["confidence"], row["risk"] = player_risk(row)
        row.pop("player_metadata", None)
        row.pop("stats", None)
        for field in (
            "custom_points",
            "direct_points",
            "estimated_points",
            "replacement_points",
            "vorp",
            "market_rank",
            "price_gap",
        ):
            if row.get(field) is not None:
                row[field] = round(float(row[field]), 2)
        for component_group in ("direct_components", "estimated_components"):
            row[component_group] = {
                key: round(float(value), 2) for key, value in row[component_group].items()
            }

    metadata = {
        "season": season,
        "scoring_profile": SCORING_PROFILE,
        "player_count": len(rows),
        "draft_pool_size": pool["size"],
        "draft_pool_position_counts": pool["counts"],
        "replacement": {
            position: {
                "rank": value["rank"],
                "points": round(num(value["points"]), 2),
            }
            for position, value in pool["replacements"].items()
        },
        "history_season": HISTORY_SEASON,
    }
    return rows, metadata
