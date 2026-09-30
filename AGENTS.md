# AGENTS.md — Investing Hub

Canonical, tool-agnostic and self-contained. `CLAUDE.md` imports this file; don't duplicate it there.

Backup posture: nightly `sqlite3 .backup` of `data/hub.db` into `backups/` with 7-day retention, plus the Time Machine layer that picks up `backups/`; GitHub holds only code, never data.

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
| Dependencies | none at runtime |
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
```

Every `hub` subcommand is currently a stub: it prints "not implemented" to stderr and exits 2.
Once implemented, `hub ic pull` calls the live IC API and `hub db *` touch the local database.

## Repo map

```text
hub/cli.py       argparse entry point; one function per subcommand
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
tracked in a `schema_version` table. Back up only with the online `.backup` API; a plain file
copy can capture the database mid-write.

## Conventions

- Zero runtime dependencies; stdlib first.
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
| `MASSIVE_API_KEY` | Massive market-data key, read by the `massive` MCP server declared in `.mcp.json` via `${MASSIVE_API_KEY}` expansion; set it in the shell environment, never commit it |

Per-machine values (IC base URL, credential references) live in the gitignored
`CLAUDE.local.md` and `config.yaml`; see `CLAUDE.local.md.example` for the shape.
