# Fantasy Football GM

A local Python and SQLite toolkit for fantasy football draft prep. It turns league rules and player projections into a ranked draft board, then recommends picks that keep a legal roster within reach.

## Try a mock draft

From the repository root, run the demo with Python 3.12. It uses generated players and runs offline with the Python standard library.

```sh
python -m examples.draft_demo --slot 7
```

The demo runs a 12-team snake draft, prints the recommended first pick and three alternatives, and shows how many teams finish with legal rosters. Try `--slot 12` to see how starting at the turn changes the recommendation.

## How it works

- **Score players against league rules.** The [scoring model](scripts/draft_model.py) handles custom scoring for offense, kickers, and team defenses. It tracks directly projected points and estimated components separately, then uses points above a replacement player to compare positions.
- **Plan the next pick.** The [draft engine](scripts/draft_session.py) weighs player value, positional scarcity, and whether a player might last until the next pick. It checks starter slots and position caps before suggesting a player.
- **Resume from accepted picks.** The [SQLite schema](db/schema.sql) records picks in order, with links back to source data. Replaying an identical pick leaves the ledger unchanged. Conflicting picks are rejected so they can be reconciled.
- **Inspect the inputs.** The [draft board exporter](scripts/build_draft_board.py) writes HTML, CSV, and JSON. The [data architecture](DataArchitecture.md) describes the provider adapters and how the project tracks source provenance.

## Local setup and tests

```sh
python scripts/init_db.py --db .local/demo.sqlite3 --seed config/league.example.json
python -m unittest discover
```

The sample seed shows how league settings are stored. The demo generates its own player board in memory. To use real projections or usage data, see the [source catalog](config/sources.json) for credentials and access requirements. The nflverse adapter uses the dependency pinned in [requirements.txt](requirements.txt). For FantasyPros, set `FANTASYPROS_API_KEY` in your environment or in the ignored `.local/.env` file.

The mock draft tests check roster legality, pick replay, and the scoring math. Recommendation quality against real draft outcomes remains unmeasured.
