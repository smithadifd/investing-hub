# AGENTS.md — Investing Hub

Canonical, tool-agnostic and self-contained. `CLAUDE.md` imports this file; don't duplicate it there.

Backup posture: nightly `hub db backup` (the online `sqlite3` `.backup` API, integrity-checked, newest 7 kept, so 7-day retention) of `data/hub.db` into `backups/`, plus the Time Machine layer that picks up `backups/`; GitHub holds only code, never data. The nightly schedule lives on the always-on Mac, outside this repo.
Restore procedure: run `hub db restore-check backups/<file>` (integrity, table list, row counts and newest revision against `data/hub.db`; exit 0 is PASS, a lagging backup is WARN, never a failure). To restore, stop anything using the store, copy the backup over `data/hub.db` and remove `data/hub.db-wal` and `data/hub.db-shm`.

## What Investing Hub is

The advisor and attention layer that pairs with Investing Companion (IC). IC owns live market
state; the hub owns the operator's theses, principles and profile and decides when something is
worth the operator's attention. Public template, private instance: the repo is machinery only.

Non-goals: the hub never executes trades, never writes to IC except through approval-gated
handoff blocks, and never commits personal data.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.12 |
| CLI | `argparse`, one `hub` entry point |
| Store | stdlib `sqlite3`, numbered SQL migrations, no ORM |
| Dependencies | `PyYAML` (reads `config.yaml`); HTTP uses stdlib `urllib` |
| Lint / format | `ruff` |
| Tests | `pytest` |
| CI | GitHub Actions, `ubuntu-latest` |

## Commands

```bash
pip install -e .[dev]      # install with dev tools
hub --help                 # list the command surface
ruff check .               # lint
ruff format --check .      # format check
pytest                     # tests (synthetic data only)
hub db init                # create data/hub.db and apply migrations (safe to re-run)
hub db migrate             # apply pending migrations to an existing database
hub db backup              # online backup into backups/, integrity-checked, keeps newest 7
hub db restore-check FILE  # verify a backup against the live store; non-zero on FAIL
hub import claude-export DIR [--apply]  # claude.ai export as revision-1 documents; dry run by default
hub session-open           # print the session status block; always exits 0, read-only
```

`hub db init|migrate|backup` take `--db PATH` (default `data/hub.db`) and `backup` takes
`--backup-dir PATH` (default `backups/`); defaults are relative to the current directory, so run
them from the repo root. They exit 1 with one line on stderr on failure.
`hub import claude-export` reads top-level `*.md` and `knowledge/*.md` (skipping the two IC
contract docs), keys each document by its relative path, and only adds documents not yet
stored, so re-running changes nothing; a file whose content changed is reported, never
re-imported. Its default is a dry run that writes nothing. `hub ic pull` calls the live IC API.

`hub session-open` (`--db PATH`, `--stale-days N`) prints one status block: pack age and
versions, contract drift against IC's contract docs (or `contract: in sync`), pending briefs,
unapplied handoffs, documents not revised for `--stale-days` (default `session_open.stale_days`
in `config.yaml`, else 30), and one `WARN` line per problem. It never writes anything and always
exits 0. `.claude/settings.json` runs it as the `SessionStart` hook, so its output opens every
session; the hook falls back to `python3 -m hub` and never fails the session if `hub` is absent.

## Repo map

```text
hub/cli.py       argparse entry point; one function per subcommand
hub/importer.py  claude.ai export reader behind `hub import claude-export`
hub/store.py     SQLite connection, migrations, document revisions, backups
hub/restore.py   backup restore check (integrity, tables, row counts)
hub/migrations/  numbered SQL migrations (NNNN_name.sql), shipped as package data
hub/__main__.py  python -m hub
tests/           pytest suite
ROADMAP.md       architecture and phases
.claude/         shared Claude Code settings (hooks only, no secrets)
```

## Architecture in brief

Sources feed a sweep that scores findings deterministically, then drafts a brief only above a
threshold. The operator's answer opens an advisor session seeded with the brief. The hub reads IC
through its API with a read-only token; changes flow back as handoff blocks the operator approves.
See `ROADMAP.md` for the diagram. Do not bypass the handoff step.

## Database / storage

Local SQLite at `data/hub.db`, schema defined by numbered SQL migrations applied in order and
tracked in a `schema_version` table. Connections use WAL mode with foreign keys on. Add a
schema change as the next `hub/migrations/NNNN_name.sql`; never edit an applied migration.
Back up only with `hub db backup` (the online `.backup` API); a plain file copy can capture the
database mid-write. Backups are `backups/hub-<UTC timestamp>.db`; a copy that fails
`PRAGMA integrity_check` is deleted, the command exits 1 and nothing is pruned.

## Conventions

- Stdlib first; `PyYAML` is the only runtime dependency.
- Every subcommand is its own function so it can be replaced independently.
- Tests use synthetic data only.
- Line length 100, `ruff` defaults plus import sorting.
- Conventional commits.

## Critical gotchas

- The repo is public: never commit holdings, values, account names, real packs or import files.
- `import/`, `data/`, `backups/` and `*.db` are gitignored. Keep it that way.
- Never copy a real context pack into a test fixture.
- Never copy the database file by hand; use the backup command.

## Environment

| Variable | Purpose |
|---|---|
| `IC_API_TOKEN` | Read-only IC token, resolved at call time from a credential manager; never stored in the repo |
| `HUB_IC_BASE_URL` | Optional; overrides `ic.base_url` from `config.yaml` (see `config.example.yaml`) |
| `MASSIVE_API_KEY` | Massive market-data key, read by the `massive` MCP server declared in `.mcp.json` via `${MASSIVE_API_KEY}` expansion; resolve it from `.env` at launch, never commit it |

`.mcp.json` is local-only (gitignored); copy it from `.mcp.json.example`.

Per-machine values (IC base URL, credential references) live in the gitignored
`CLAUDE.local.md` and `config.yaml`; see `CLAUDE.local.md.example` for the shape.
