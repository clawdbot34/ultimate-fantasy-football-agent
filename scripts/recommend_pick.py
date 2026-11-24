"""Return a compact pick card or export an offline ten-name fallback window."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

try:
    from draft_model import DEFAULT_LEAGUE_SEED, load_league_settings
    from draft_session import (DEFAULT_BOARD, DEFAULT_DB, build_pick_window, load_board,
                               load_draft_picks, recommend_from_db, render_windows_html, resolve_league_key)
except ModuleNotFoundError:  # Support import as scripts.recommend_pick in tests.
    from scripts.draft_model import DEFAULT_LEAGUE_SEED, load_league_settings
    from scripts.draft_session import (DEFAULT_BOARD, DEFAULT_DB, build_pick_window, load_board,
                                       load_draft_picks, recommend_from_db, render_windows_html, resolve_league_key)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--user-slot", type=int, required=True)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--board", type=Path, default=DEFAULT_BOARD)
    parser.add_argument("--league-seed", type=Path, default=DEFAULT_LEAGUE_SEED)
    parser.add_argument("--league-key")
    parser.add_argument("--no-persist", action="store_true")
    parser.add_argument("--window", action="store_true", help="Export a ten-name ladder and corner pairs without writing recommendations to SQLite")
    parser.add_argument("--output", type=Path, default=DEFAULT_BOARD.with_name("draft-fallbacks.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.window:
            board = load_board(args.board.resolve(), args.season)
            settings = load_league_settings(args.league_seed.resolve())
            with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as connection:
                league_key = resolve_league_key(connection, args.season, args.league_key)
                picks = load_draft_picks(connection, league_key, args.season)
            result = build_pick_window(board, picks, user_slot=args.user_slot, settings=settings)
            output = args.output.resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(result, indent=2), encoding="utf-8")
            output.with_suffix(".html").write_text(render_windows_html(
                [result], title="Next pick fallback list",
                note=f"Board: {result['board_generated_at']}. Observed picks: {len(picks)}. Verify current ESPN availability and player news before selecting. Regenerate after new picks.",
            ), encoding="utf-8")
            print(json.dumps({"ok": True, **result, "artifact": str(output.with_suffix('.html'))}, indent=2))
            return 0
        result = recommend_from_db(
            db_path=args.db.resolve(),
            board_path=args.board.resolve(),
            league_seed=args.league_seed.resolve(),
            season=args.season,
            user_slot=args.user_slot,
            league_key=args.league_key,
            persist=not args.no_persist,
        )
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps({"ok": True, **result}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
