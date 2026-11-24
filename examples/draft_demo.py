"""Run a complete draft with synthetic players and league rules."""

from __future__ import annotations

import argparse

from scripts.draft_session import ALL_POSITIONS, simulate_draft


def synthetic_settings() -> dict:
    return {
        "team_count": 12,
        "draft": {"type": "snake", "rounds": 14, "seconds_per_pick": 60},
        "roster": {
            "size": 14,
            "bench": 4,
            "starters": {"QB": 1, "RB": 2, "RB_WR": 1, "WR": 2, "TE": 1, "FLEX": 1, "DST": 1, "K": 1},
            "maximums": {"QB": 2, "RB": 6, "WR": 6, "TE": 2, "DST": 3, "K": 3},
        },
    }


def synthetic_board() -> dict:
    position_cycle = ("RB", "WR", "RB", "WR", "QB", "TE", "RB", "WR", "DEF", "K")
    position_counts = {position: 0 for position in ALL_POSITIONS}
    players = []
    for rank in range(1, 301):
        position = position_cycle[(rank - 1) % len(position_cycle)]
        position_counts[position] += 1
        position_rank = position_counts[position]
        players.append(
            {
                "player_key": f"player-{rank:03d}",
                "name": f"Synthetic Player {rank:03d}",
                "team": "TST",
                "position": position,
                "custom_rank": rank,
                "tier": (position_rank - 1) // 6 + 1,
                "vorp": round(310.0 - rank, 2),
                "market_rank": float(max(1, rank + ((rank % 5) - 2))),
                "target_range": f"{max(1, rank - 6)}–{rank + 6}",
                "confidence": "High",
                "risk": "—",
            }
        )
    metadata = {"season": 2026, "canonical_digest": "synthetic-board"}
    return {"metadata": metadata, "players": players}


def main() -> None:
    settings = synthetic_settings()
    team_count = settings["team_count"]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--slot", type=int, choices=range(1, team_count + 1), default=7)
    args = parser.parse_args()

    result = simulate_draft(synthetic_board(), settings, user_slot=args.slot, seed=42)
    first_turn = result["user_cards"][0]
    legal_count = sum(result["legal_rosters"].values())

    print(f"Synthetic draft: {len(result['picks'])} picks across {team_count} teams")
    print(f"Slot {args.slot}, pick {first_turn['overall_pick']}")
    for label, card in zip(("Top pick", "Fallback 1", "Fallback 2", "Fallback 3"), first_turn["cards"]):
        print(f"  {label}: {card['name']} ({card['position']})")
    print(f"Legal final rosters: {legal_count}/{team_count}")


if __name__ == "__main__":
    main()
