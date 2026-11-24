"""Generate private HTML, CSV, and JSON draft boards from one canonical model."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from draft_model import DEFAULT_LEAGUE_SEED, SCORING_PROFILE, build_board_rows, load_league_settings
except ModuleNotFoundError:  # Support import as scripts.build_draft_board in tests.
    from scripts.draft_model import (
        DEFAULT_LEAGUE_SEED,
        SCORING_PROFILE,
        build_board_rows,
        load_league_settings,
    )


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / ".local" / "fantasy_gm.sqlite3"
DEFAULT_OUTPUT_DIR = ROOT / ".local" / "exports"
SOURCE_KEY = "local_model"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_digest(rows: list[dict[str, Any]]) -> str:
    payload = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def display_html(value: Any, *, digits: int = 1) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return html.escape(str(value))


def write_json(path: Path, rows: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    path.write_text(
        json.dumps({"metadata": metadata, "players": rows}, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


CSV_FIELDS = (
    "custom_rank",
    "tier",
    "name",
    "team",
    "position",
    "bye",
    "custom_points",
    "direct_points",
    "estimated_points",
    "vorp",
    "replacement_points",
    "replacement_rank",
    "ecr",
    "adp",
    "market_rank",
    "price_gap",
    "confidence",
    "risk",
    "target_range",
    "direct_components",
    "estimated_components",
)


def spreadsheet_cell(value: Any) -> Any:
    if isinstance(value, str) and (
        value.startswith(("\t", "\r", "\n"))
        or value.lstrip().startswith(("=", "+", "-", "@", "\0", "＝", "＋", "－", "＠"))
    ):
        # A visible prefix also stays in place when a spreadsheet re-saves the CSV.
        return f"Text: {value}"
    return value


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, quoting=csv.QUOTE_NONNUMERIC)
        writer.writeheader()
        for row in rows:
            output = {field: spreadsheet_cell(row.get(field)) for field in CSV_FIELDS}
            output["direct_components"] = json.dumps(row["direct_components"], sort_keys=True)
            output["estimated_components"] = json.dumps(row["estimated_components"], sort_keys=True)
            writer.writerow(output)


def component_details(row: dict[str, Any]) -> str:
    pieces = []
    for label, key in (("Direct", "direct_components"), ("Estimated", "estimated_components")):
        components = ", ".join(
            f"{html.escape(name.replace('_', ' ').title())}: {display_html(value)}"
            for name, value in row[key].items()
            if abs(float(value)) >= 0.05
        )
        pieces.append(f"<strong>{label}</strong>: {components or 'None'}")
    return "<br>".join(pieces)


def board_row_html(row: dict[str, Any]) -> str:
    searchable = " ".join(str(row.get(key) or "") for key in ("name", "team", "position", "risk")).lower()
    risk_class = " risk" if row["risk"] != "—" else ""
    gap = row.get("price_gap")
    gap_class = "good" if gap is not None and gap >= 8 else "bad" if gap is not None and gap <= -8 else ""
    return f"""
        <tr data-search="{html.escape(searchable)}" data-position="{html.escape(row['position'])}" data-tier="{row['tier']}">
          <td class="rank">{row['custom_rank']}</td>
          <td><span class="tier">T{row['tier']}</span></td>
          <td class="player"><strong>{html.escape(row['name'])}</strong><span>{html.escape(row['team'] or 'FA')} · {html.escape(row['position'])}</span></td>
          <td>{display_html(row.get('bye'), digits=0)}</td>
          <td class="number">{display_html(row['custom_points'])}</td>
          <td class="number emphasis">{display_html(row['vorp'])}</td>
          <td class="number">{display_html(row.get('ecr'))}</td>
          <td class="number">{display_html(row.get('adp'))}</td>
          <td class="number {gap_class}">{display_html(gap)}</td>
          <td>{html.escape(row['target_range'])}</td>
          <td>{html.escape(row['confidence'])}</td>
          <td class="{risk_class.strip()}">{html.escape(row['risk'])}</td>
          <td><details><summary>View</summary><div class="details">{component_details(row)}</div></details></td>
        </tr>"""


def write_html(path: Path, rows: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    position_options = "".join(
        f'<option value="{position}">{position}</option>' for position in ("QB", "RB", "WR", "TE", "K", "DEF")
    )
    table_rows = "\n".join(board_row_html(row) for row in rows)
    position_counts = metadata["draft_pool_position_counts"]
    counts_text = " · ".join(f"{position} {count}" for position, count in sorted(position_counts.items()))
    document = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="board-digest" content="{metadata['canonical_digest']}">
  <title>2026 Custom Draft Board</title>
  <style>
    :root {{ color-scheme: dark; --bg:#0b1020; --panel:#121a2e; --line:#26324b; --text:#edf2ff; --muted:#9ca9c6; --accent:#78e6bd; --accent2:#7eb7ff; --warn:#ffd27a; --danger:#ff9a9a; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:linear-gradient(180deg,#101936 0,#0b1020 34rem); color:var(--text); font:15px/1.45 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
    main {{ width:min(1500px,calc(100% - 32px)); margin:0 auto; padding:40px 0 64px; }}
    h1 {{ margin:0; font-size:clamp(2rem,5vw,4rem); letter-spacing:-.055em; line-height:1; }}
    .eyebrow {{ color:var(--accent); font-weight:750; letter-spacing:.12em; text-transform:uppercase; font-size:.75rem; }}
    .lede {{ max-width:760px; color:var(--muted); font-size:1rem; margin:16px 0 26px; }}
    .summary {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; margin-bottom:18px; }}
    .card {{ background:rgba(18,26,46,.86); border:1px solid var(--line); border-radius:14px; padding:16px; }}
    .card strong {{ display:block; font-size:1.35rem; }} .card span {{ color:var(--muted); font-size:.8rem; }}
    .controls {{ display:flex; gap:12px; align-items:end; padding:14px; background:var(--panel); border:1px solid var(--line); border-radius:14px 14px 0 0; position:sticky; top:0; z-index:4; }}
    label {{ display:grid; gap:5px; color:var(--muted); font-size:.75rem; font-weight:700; text-transform:uppercase; letter-spacing:.08em; }}
    input,select {{ min-height:42px; padding:0 12px; border:1px solid #354464; border-radius:9px; background:#0d1427; color:var(--text); font:inherit; }}
    input {{ width:min(460px,55vw); }} input:focus-visible,select:focus-visible,summary:focus-visible {{ outline:3px solid var(--accent2); outline-offset:2px; }}
    #result-count {{ margin-left:auto; color:var(--muted); white-space:nowrap; }}
    .table-wrap {{ overflow:auto; max-height:70vh; border:1px solid var(--line); border-top:0; border-radius:0 0 14px 14px; background:rgba(11,16,32,.9); }}
    table {{ width:100%; border-collapse:collapse; min-width:1260px; }}
    th {{ position:sticky; top:0; z-index:3; background:#16213a; color:#b9c5df; font-size:.7rem; letter-spacing:.07em; text-transform:uppercase; text-align:left; }}
    th,td {{ padding:11px 10px; border-bottom:1px solid rgba(38,50,75,.72); white-space:nowrap; }}
    tbody tr:hover {{ background:#131d34; }} .rank {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
    .player span {{ display:block; color:var(--muted); font-size:.75rem; }} .number {{ text-align:right; font-variant-numeric:tabular-nums; }}
    .emphasis,.good {{ color:var(--accent); font-weight:750; }} .bad,.risk {{ color:var(--warn); }}
    .tier {{ display:inline-grid; place-items:center; min-width:31px; height:25px; border-radius:999px; background:#203252; color:#cfe2ff; font-weight:750; }}
    summary {{ cursor:pointer; color:var(--accent2); }} .details {{ position:absolute; right:20px; width:min(520px,calc(100vw - 40px)); white-space:normal; padding:12px; background:#101a30; border:1px solid #405176; border-radius:10px; box-shadow:0 12px 36px #0009; z-index:8; }}
    .footnote {{ color:var(--muted); font-size:.8rem; margin-top:14px; }}
    [hidden] {{ display:none !important; }}
    @media (max-width:720px) {{ main {{ width:min(100% - 20px,1500px); padding-top:24px; }} .summary {{ grid-template-columns:1fr; }} .controls {{ align-items:stretch; flex-direction:column; position:static; }} input {{ width:100%; }} #result-count {{ margin:0; }} .table-wrap {{ max-height:72vh; }} }}
  </style>
</head>
<body>
<main>
  <p class="eyebrow">Private · League-specific · 2026</p>
  <h1>Draft board</h1>
  <p class="lede">Consensus projections rescored for this league, then compared with the first projected undrafted player at each position. Positive price gap means our value rank is earlier than the market.</p>
  <section class="summary" aria-label="Board summary">
    <div class="card"><strong>{metadata['player_count']}</strong><span>projected players</span></div>
    <div class="card"><strong>{metadata['draft_pool_size']}</strong><span>expected drafted roster spots</span></div>
    <div class="card"><strong>{metadata['history_season']}</strong><span>historical estimate season</span></div>
  </section>
  <div class="controls">
    <label>Search<input id="search" type="search" placeholder="Player, team, position, risk"></label>
    <label>Position<select id="position"><option value="ALL">All</option>{position_options}</select></label>
    <span id="result-count" aria-live="polite">{len(rows)} players</span>
  </div>
  <div class="table-wrap" tabindex="0" aria-label="Scrollable custom draft board">
    <table>
      <thead><tr><th>Rank</th><th>Tier</th><th>Player</th><th>Bye</th><th class="number">Pts</th><th class="number">VORP</th><th class="number">ECR</th><th class="number">ADP</th><th class="number">Gap</th><th>Target picks</th><th>Confidence</th><th>Risk</th><th>Scoring</th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table>
  </div>
  <p class="footnote">Expected draft-pool mix: {html.escape(counts_text)}. Estimates are labeled in each row; current news and final availability remain live checks.</p>
</main>
<script>
  const search = document.querySelector('#search');
  const position = document.querySelector('#position');
  const rows = [...document.querySelectorAll('tbody tr')];
  const count = document.querySelector('#result-count');
  function filterRows() {{
    const query = search.value.trim().toLowerCase();
    const selected = position.value;
    let visible = 0;
    for (const row of rows) {{
      const show = (!query || row.dataset.search.includes(query)) && (selected === 'ALL' || row.dataset.position === selected);
      row.hidden = !show;
      if (show) visible += 1;
    }}
    count.textContent = `${{visible}} player${{visible === 1 ? '' : 's'}}`;
  }}
  search.addEventListener('input', filterRows);
  position.addEventListener('change', filterRows);
</script>
</body>
</html>
"""
    path.write_text(document, encoding="utf-8")


def persist_values(
    connection: sqlite3.Connection,
    rows: list[dict[str, Any]],
    metadata: dict[str, Any],
    json_path: Path,
) -> str:
    run_id = str(uuid.uuid4())
    observed_at = metadata["generated_at"]
    relative_path = json_path.relative_to(ROOT).as_posix()
    content_sha256 = hashlib.sha256(json_path.read_bytes()).hexdigest()
    connection.execute(
        """
        INSERT INTO ingest_runs (
            run_id, source_key, dataset, season, week, started_at, completed_at,
            status, raw_path, content_sha256, row_count, notes
        ) VALUES (?, ?, ?, ?, 0, ?, ?, 'success', ?, ?, ?, ?)
        """,
        (
            run_id,
            SOURCE_KEY,
            f"draft_board_{metadata['season']}",
            metadata["season"],
            observed_at,
            observed_at,
            relative_path,
            content_sha256,
            len(rows),
            json.dumps({key: value for key, value in metadata.items() if key != "generated_at"}, sort_keys=True),
        ),
    )
    confidence_values = {"High": 0.9, "Medium": 0.7, "Low": 0.5}
    for row in rows:
        context = {
            key: row[key]
            for key in (
                "tier",
                "direct_points",
                "estimated_points",
                "vorp",
                "replacement_points",
                "replacement_rank",
                "ecr",
                "adp",
                "market_rank",
                "price_gap",
                "confidence",
                "risk",
                "target_range",
                "direct_components",
                "estimated_components",
                "notes",
            )
        }
        connection.execute(
            """
            INSERT INTO player_values (
                run_id, player_key, season, week, value_type, scoring_profile,
                position, rank_value, numeric_value, confidence, context_json,
                observed_at
            ) VALUES (?, ?, ?, 0, 'custom_draft_value', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                row["player_key"],
                metadata["season"],
                SCORING_PROFILE,
                row["position"],
                row["custom_rank"],
                row["custom_points"],
                confidence_values[row["confidence"]],
                json.dumps(context, sort_keys=True),
                observed_at,
            ),
        )
    connection.commit()
    return run_id


def build(db_path: Path, league_seed: Path, output_dir: Path, season: int) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = load_league_settings(league_seed)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    try:
        if not connection.execute("SELECT 1 FROM data_sources WHERE source_key = ?", (SOURCE_KEY,)).fetchone():
            raise RuntimeError("local_model source is not seeded; run scripts/init_db.py")
        rows, metadata = build_board_rows(connection, settings, season)
        metadata = {**metadata, "generated_at": utc_now()}
        metadata["canonical_digest"] = canonical_digest(rows)
        json_path = output_dir / "draft-board.json"
        csv_path = output_dir / "draft-board.csv"
        html_path = output_dir / "draft-board.html"
        write_json(json_path, rows, metadata)
        write_csv(csv_path, rows)
        write_html(html_path, rows, metadata)
        run_id = persist_values(connection, rows, metadata, json_path)
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = len(connection.execute("PRAGMA foreign_key_check").fetchall())
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return {
        "ok": integrity == "ok" and foreign_keys == 0,
        "season": season,
        "players": len(rows),
        "canonical_digest": metadata["canonical_digest"],
        "run_id": run_id,
        "artifacts": {
            "html": html_path.relative_to(ROOT).as_posix(),
            "csv": csv_path.relative_to(ROOT).as_posix(),
            "json": json_path.relative_to(ROOT).as_posix(),
        },
        "draft_pool_position_counts": metadata["draft_pool_position_counts"],
        "replacement": metadata["replacement"],
        "integrity": integrity,
        "foreign_key_issues": foreign_keys,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--season", type=int, default=2026)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--league-seed", type=Path, default=DEFAULT_LEAGUE_SEED)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build(
            args.db.resolve(),
            args.league_seed.resolve(),
            args.output_dir.resolve(),
            args.season,
        )
    except Exception as error:
        print(json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
