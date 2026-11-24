# Fantasy GM Data Architecture

## Decision

Use a local SQLite database as the canonical operational store for the 2026 season.

- SQLite is the source of truth for normalized league state, player identity, rankings, projections, roster snapshots, transactions, events, recommendations, approvals, and verified results.
- Raw source responses remain immutable files under `.local/raw/`, with their path and SHA-256 hash recorded in the database.
- Generated boards and human review surfaces go under `.local/exports/`. A Google Sheet may consume those exports later, but it is not the canonical database.
- Private league identifiers, manager information, credentials, cached source data, and the database itself remain under `.local/` and never enter a public repository.

This matches the current workload: one operator, one local application, low write concurrency, many historical queries, and no need for an always-on server.

## Why not Google Sheets as the database

Sheets is useful for viewing, filtering, annotations, and sharing. It is poor as the system of record for this project because player identity, source snapshots, weekly histories, roster state, and action auditing are relational and time-dependent. Treating every update as a cell mutation would make provenance, joins, constraints, testing, and idempotent imports harder.

If a review sheet becomes useful, generate it from SQLite and import only explicit user annotations back into the database.

## Why not Neon yet

Neon is the preferred promotion target when the project needs Postgres, but adding a network database now would introduce credentials, connectivity, migrations, and service operations without improving the current single-user workflow.

Promote from SQLite to Neon when at least one of these becomes real:

1. A deployed web application must access current data.
2. Scheduled collectors must run while the local computer is off.
3. Multiple devices, users, or workers need concurrent writes.
4. Remote backups and server-side access become an explicit requirement.

The schema uses conventional relational types and keeps JSON at the edges so that a later Postgres migration is mechanical rather than a redesign.

## Data flow

```text
Official league state + forecasts + market data + NFL facts + usage data
                                  |
                                  v
                     immutable raw source snapshots
                                  |
                                  v
                   normalized, timestamped SQLite tables
                                  |
                                  v
                  league-specific values and recommendations
                                  |
                                  v
                   exact approval -> action -> verification
                                  |
                                  v
                       result and learning history
```

## Storage layout

```text
db/schema.sql                  public schema
scripts/init_db.py             idempotent database initializer
scripts/sync_sleeper.py        tokenless Sleeper market-data collector
scripts/sync_nflverse.py       structured NFL facts and usage collector
requirements.txt               pinned Python data dependency
config/sources.json            public source catalog and access requirements
config/league.example.json     public synthetic configuration example
.local/fantasy_gm.sqlite3      private canonical database
.local/league.json             private structured league seed
.local/league-contract.md      private human-readable league audit
.local/raw/                    immutable API/UI/data snapshots
.local/exports/                generated draft boards and weekly reports
.local/backups/                timestamped database backups
```

Initialize or safely re-run the database setup with:

```powershell
python scripts\init_db.py
```

The command applies the versioned schema, refreshes the public source catalog,
loads `.local/league.json` when present, and runs integrity and foreign-key
checks. It does not delete existing observations.

Sync Sleeper's public NFL state, active player directory, and 24-hour trending
adds/drops with:

```powershell
python scripts\sync_sleeper.py
```

No Sleeper account or credential is required. The active player directory is
downloaded at most once per UTC day unless `--force-players` is supplied; NFL
state and market trends are refreshed on every run. Each response is retained
under `.local/raw/sleeper/`, hashed, and linked to its normalized database rows.

Install the pinned nflverse dependency in the project environment and sync the
league season plus the latest nflverse-supported analysis season with:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\python.exe scripts\sync_nflverse.py
```

The collector imports the current league-season schedule and rosters, then
weekly player production, snap usage, and injury/practice reports from the
latest season nflreadpy currently supports. Before the regular-season data feed
flips to a new year, that analysis season is intentionally the prior completed
season. Each dataset succeeds or fails independently and is saved as a private,
hashed Parquet snapshot under `.local/raw/nflverse/`.

## Source policy

Every ingestion should create an `ingest_runs` record containing:

- source and dataset
- start/completion timestamps
- success or failure state
- raw snapshot path and content hash
- source URL or endpoint
- row count and notes

Derived values must retain the ingest run that produced them. Cached proprietary or access-restricted data stays private and is never committed or redistributed.

## Access plan

### Required now

| Source | Purpose | Access |
| --- | --- | --- |
| ESPN authenticated Chrome | Authoritative league settings, availability, roster, draft room, waivers, transactions, and approved actions | Requires an authenticated ESPN browser session. Keep cookies and session data local. |
| nflverse via nflreadpy | Play-by-play, schedules, rosters, stats, snaps, injuries, participation, Next Gen Stats, and fantasy opportunity | No account or API key; attribution required for applicable datasets |
| Sleeper API | Tokenless player metadata, NFL state, trending adds/drops, and cross-platform market context | No account or API key for read-only non-commercial use |
| NFL and team sources | Official injuries, practice status, transactions, schedules, and inactives | No account required |
| National Weather Service API | Kickoff-time outdoor weather checks | No API key required |

### Useful credential to request

| Source | Purpose | Access |
| --- | --- | --- |
| FantasyPros API | ECR, rankings, projections, player IDs, and consensus inputs | Free key can support personal non-commercial prototyping; supply it through the environment or the ignored `.local/.env` file |

### Browser-accessible supporting sources

- ESPN live draft trends and league-adjusted UI context
- FantasyPros public ECR/ADP pages when API coverage is insufficient
- Rotoworld/NBC Sports player news
- Fantasy Life utilization reports
- Official national reporters and team beat reporting, with the original timestamp and source retained

### Optional later

- One paid product only if a demonstrated data gap remains: PFF+, Fantasy Points Data Suite, Establish The Run, 4for4, or FTN.
- Google Sheets as a generated review surface.
- Neon as the deployed Postgres system of record.

Do not purchase or connect multiple overlapping fantasy products by default.

## Backup and migration policy

- Before a schema migration or material import, create a timestamped copy under `.local/backups/`.
- Keep schema changes idempotent and versioned in `schema_migrations`.
- Validate with `PRAGMA integrity_check`, foreign-key checks, row counts, and an idempotent second initialization.
- Do not sync the live SQLite file through a network filesystem while it is open.
