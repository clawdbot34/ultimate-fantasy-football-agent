PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    description TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

INSERT OR IGNORE INTO schema_migrations (version, description, applied_at)
VALUES (1, 'Initial season-ready schema', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

INSERT OR IGNORE INTO schema_migrations (version, description, applied_at)
VALUES (2, 'Add NFL game snapshots', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

CREATE TABLE IF NOT EXISTS data_sources (
    source_key TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL,
    priority_tier INTEGER NOT NULL CHECK (priority_tier BETWEEN 1 AND 9),
    access_method TEXT NOT NULL,
    credential_env TEXT,
    base_url TEXT,
    license_notes TEXT,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS leagues (
    league_key TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_league_id TEXT NOT NULL,
    season INTEGER NOT NULL CHECK (season >= 2000),
    name TEXT NOT NULL,
    timezone TEXT NOT NULL,
    draft_at TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(settings_json)),
    source_url TEXT,
    observed_at TEXT NOT NULL,
    UNIQUE (platform, platform_league_id, season)
);

CREATE TABLE IF NOT EXISTS teams (
    team_key TEXT PRIMARY KEY,
    league_key TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    platform_team_id TEXT NOT NULL,
    name TEXT NOT NULL,
    manager_name TEXT,
    division_name TEXT,
    is_user_team INTEGER NOT NULL DEFAULT 0 CHECK (is_user_team IN (0, 1)),
    observed_at TEXT NOT NULL,
    UNIQUE (league_key, platform_team_id)
);

CREATE INDEX IF NOT EXISTS idx_teams_league ON teams(league_key);

CREATE TABLE IF NOT EXISTS players (
    player_key TEXT PRIMARY KEY,
    canonical_name TEXT NOT NULL,
    position TEXT,
    nfl_team TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS player_ids (
    source_key TEXT NOT NULL REFERENCES data_sources(source_key),
    external_id TEXT NOT NULL,
    player_key TEXT NOT NULL REFERENCES players(player_key) ON DELETE CASCADE,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (source_key, external_id),
    UNIQUE (source_key, player_key)
);

CREATE INDEX IF NOT EXISTS idx_player_ids_player ON player_ids(player_key);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL REFERENCES data_sources(source_key),
    dataset TEXT NOT NULL,
    league_key TEXT REFERENCES leagues(league_key) ON DELETE SET NULL,
    season INTEGER,
    week INTEGER CHECK (week IS NULL OR week BETWEEN 0 AND 25),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'partial')),
    source_url TEXT,
    raw_path TEXT,
    content_sha256 TEXT,
    row_count INTEGER CHECK (row_count IS NULL OR row_count >= 0),
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_source_time
ON ingest_runs(source_key, dataset, started_at DESC);

CREATE TABLE IF NOT EXISTS player_values (
    value_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id) ON DELETE CASCADE,
    player_key TEXT NOT NULL REFERENCES players(player_key) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL DEFAULT 0 CHECK (week BETWEEN 0 AND 25),
    value_type TEXT NOT NULL,
    scoring_profile TEXT NOT NULL DEFAULT 'unspecified',
    position TEXT,
    rank_value REAL,
    numeric_value REAL,
    floor_value REAL,
    ceiling_value REAL,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    context_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(context_json)),
    observed_at TEXT NOT NULL,
    UNIQUE (run_id, player_key, season, week, value_type, scoring_profile)
);

CREATE INDEX IF NOT EXISTS idx_player_values_lookup
ON player_values(season, week, value_type, scoring_profile, player_key, observed_at DESC);

CREATE TABLE IF NOT EXISTS player_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT REFERENCES ingest_runs(run_id) ON DELETE SET NULL,
    player_key TEXT REFERENCES players(player_key) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    event_at TEXT NOT NULL,
    status TEXT,
    headline TEXT,
    source_url TEXT,
    confidence REAL CHECK (confidence IS NULL OR confidence BETWEEN 0 AND 1),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json)),
    observed_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_player_events_player_time
ON player_events(player_key, event_at DESC);

CREATE TABLE IF NOT EXISTS nfl_game_snapshots (
    run_id TEXT NOT NULL REFERENCES ingest_runs(run_id) ON DELETE CASCADE,
    game_id TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL CHECK (week BETWEEN 0 AND 25),
    game_type TEXT NOT NULL,
    game_at TEXT,
    away_team TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_score REAL,
    home_score REAL,
    stadium TEXT,
    roof TEXT,
    surface TEXT,
    temperature REAL,
    wind REAL,
    context_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(context_json)),
    observed_at TEXT NOT NULL,
    PRIMARY KEY (run_id, game_id)
);

CREATE INDEX IF NOT EXISTS idx_nfl_game_snapshots_lookup
ON nfl_game_snapshots(season, week, game_at, game_id, observed_at DESC);

CREATE TABLE IF NOT EXISTS roster_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    league_key TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    run_id TEXT REFERENCES ingest_runs(run_id) ON DELETE SET NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL DEFAULT 0 CHECK (week BETWEEN 0 AND 25),
    observed_at TEXT NOT NULL,
    source_url TEXT
);

CREATE INDEX IF NOT EXISTS idx_roster_snapshots_latest
ON roster_snapshots(league_key, season, week, observed_at DESC);

CREATE TABLE IF NOT EXISTS roster_entries (
    snapshot_id TEXT NOT NULL REFERENCES roster_snapshots(snapshot_id) ON DELETE CASCADE,
    team_key TEXT NOT NULL REFERENCES teams(team_key) ON DELETE CASCADE,
    player_key TEXT NOT NULL REFERENCES players(player_key) ON DELETE CASCADE,
    slot TEXT NOT NULL,
    lineup_status TEXT,
    acquisition_type TEXT,
    PRIMARY KEY (snapshot_id, team_key, player_key)
);

CREATE INDEX IF NOT EXISTS idx_roster_entries_team ON roster_entries(team_key, snapshot_id);

CREATE TABLE IF NOT EXISTS draft_picks (
    league_key TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    season INTEGER NOT NULL,
    overall_pick INTEGER NOT NULL CHECK (overall_pick > 0),
    round_number INTEGER NOT NULL CHECK (round_number > 0),
    round_pick INTEGER NOT NULL CHECK (round_pick > 0),
    team_key TEXT REFERENCES teams(team_key) ON DELETE SET NULL,
    player_key TEXT REFERENCES players(player_key) ON DELETE SET NULL,
    picked_at TEXT,
    run_id TEXT REFERENCES ingest_runs(run_id) ON DELETE SET NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json)),
    PRIMARY KEY (league_key, season, overall_pick)
);

CREATE TABLE IF NOT EXISTS matchup_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    league_key TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    run_id TEXT REFERENCES ingest_runs(run_id) ON DELETE SET NULL,
    season INTEGER NOT NULL,
    week INTEGER NOT NULL CHECK (week BETWEEN 1 AND 25),
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS matchup_entries (
    snapshot_id TEXT NOT NULL REFERENCES matchup_snapshots(snapshot_id) ON DELETE CASCADE,
    team_key TEXT NOT NULL REFERENCES teams(team_key) ON DELETE CASCADE,
    opponent_team_key TEXT REFERENCES teams(team_key) ON DELETE SET NULL,
    site TEXT CHECK (site IS NULL OR site IN ('home', 'away', 'neutral')),
    points REAL,
    projected_points REAL,
    result TEXT CHECK (result IS NULL OR result IN ('win', 'loss', 'tie', 'pending')),
    PRIMARY KEY (snapshot_id, team_key)
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_key TEXT PRIMARY KEY,
    league_key TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    run_id TEXT REFERENCES ingest_runs(run_id) ON DELETE SET NULL,
    platform_transaction_id TEXT,
    season INTEGER NOT NULL,
    week INTEGER CHECK (week IS NULL OR week BETWEEN 0 AND 25),
    transaction_type TEXT NOT NULL,
    status TEXT,
    occurred_at TEXT,
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json)),
    observed_at TEXT NOT NULL,
    UNIQUE (league_key, platform_transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_transactions_league_time
ON transactions(league_key, season, week, occurred_at DESC);

CREATE TABLE IF NOT EXISTS recommendations (
    recommendation_id TEXT PRIMARY KEY,
    league_key TEXT NOT NULL REFERENCES leagues(league_key) ON DELETE CASCADE,
    recommendation_type TEXT NOT NULL,
    season INTEGER NOT NULL,
    week INTEGER CHECK (week IS NULL OR week BETWEEN 0 AND 25),
    created_at TEXT NOT NULL,
    deadline_at TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('proposed', 'approved', 'rejected', 'expired', 'executed', 'failed', 'verified')
    ),
    action_json TEXT NOT NULL CHECK (json_valid(action_json)),
    alternatives_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(alternatives_json)),
    rationale TEXT NOT NULL,
    expected_value REAL,
    risk_notes TEXT,
    approved_at TEXT,
    executed_at TEXT,
    verification_json TEXT CHECK (verification_json IS NULL OR json_valid(verification_json))
);

CREATE INDEX IF NOT EXISTS idx_recommendations_queue
ON recommendations(league_key, status, deadline_at, created_at DESC);

CREATE VIEW IF NOT EXISTS latest_player_values AS
WITH ranked AS (
    SELECT
        pv.*,
        ROW_NUMBER() OVER (
            PARTITION BY player_key, season, week, value_type, scoring_profile
            ORDER BY observed_at DESC, value_id DESC
        ) AS recency_rank
    FROM player_values AS pv
)
SELECT *
FROM ranked
WHERE recency_rank = 1;

CREATE VIEW IF NOT EXISTS latest_roster_snapshots AS
WITH ranked AS (
    SELECT
        rs.*,
        ROW_NUMBER() OVER (
            PARTITION BY league_key, season, week
            ORDER BY observed_at DESC, snapshot_id DESC
        ) AS recency_rank
    FROM roster_snapshots AS rs
)
SELECT *
FROM ranked
WHERE recency_rank = 1;

CREATE VIEW IF NOT EXISTS latest_nfl_games AS
WITH ranked AS (
    SELECT
        ngs.*,
        ROW_NUMBER() OVER (
            PARTITION BY game_id
            ORDER BY observed_at DESC, run_id DESC
        ) AS recency_rank
    FROM nfl_game_snapshots AS ngs
)
SELECT *
FROM ranked
WHERE recency_rank = 1;
