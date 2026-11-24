"""Generate deterministic, league-specific plans for one or all snake-draft slots."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from draft_model import DEFAULT_LEAGUE_SEED, load_league_settings
    from draft_session import DEFAULT_BOARD, ROOT, load_board, render_windows_html, simulate_slot_plans
except ModuleNotFoundError:  # Support import as scripts.simulate_draft in tests.
    from scripts.draft_model import DEFAULT_LEAGUE_SEED, load_league_settings
    from scripts.draft_session import DEFAULT_BOARD, ROOT, load_board, render_windows_html, simulate_slot_plans


DEFAULT_OUTPUT = ROOT / ".local" / "exports" / "draft-slot-plans.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--all-slots", action="store_true")
    group.add_argument("--slot", type=int)
    parser.add_argument("--seed", type=int, default=20260908)
    parser.add_argument("--board", type=Path, default=DEFAULT_BOARD)
    parser.add_argument("--league-seed", type=Path, default=DEFAULT_LEAGUE_SEED)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        board = load_board(args.board.resolve(), args.season)
        settings = load_league_settings(args.league_seed.resolve())
        team_count = int(settings["team_count"])
        slots = range(1, team_count + 1) if args.all_slots else (args.slot,)
        if any(slot is None or not 1 <= int(slot) <= team_count for slot in slots):
            raise ValueError(f"slot must be between 1 and {team_count}")
        result = simulate_slot_plans(board, settings, seed=args.seed, slots=(int(slot) for slot in slots))
        result["generated_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        windows = [target["window"] for plan in result["slot_plans"] for target in plan["round_targets"]]
        output.with_suffix(".html").write_text(render_windows_html(
            windows, title="Draft fallback plans by slot",
            note=f"Planning scenarios, not live availability. Later rounds assume the simulated earlier picks. Board: {board['metadata'].get('generated_at')}. Regenerate your next-pick list from actual ESPN observations during the draft.",
        ), encoding="utf-8")
        summary = {
            "ok": result["all_legal"],
            "season": args.season,
            "seed": args.seed,
            "slots": len(result["slot_plans"]),
            "scenarios": result["scenarios_tested"],
            "simulations": result["simulation_count"],
            "legal_user_rosters": result["legal_user_rosters"],
            "reproducible_digest": result["reproducible_digest"],
            "artifact": output.relative_to(ROOT).as_posix(),
        }
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
