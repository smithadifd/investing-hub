# AGENTS.md — Investing Hub

Canonical, tool-agnostic and self-contained. `CLAUDE.md` imports this file; don't duplicate it there.

Backup posture: nightly `hub db backup` (the online `sqlite3` `.backup` API, integrity-checked, 7-day retention: the newest copy of each of the last 7 backup days, plus every copy from the newest day) of `data/hub.db` into `backups/`, plus the Time Machine layer that picks up `backups/`; GitHub holds only code, never data. The nightly schedule lives on the always-on Mac, outside this repo.
Restore procedure: run `hub db restore-check [backups/<file>]` (defaults to the newest backup; integrity, table list, row counts and newest revision against `data/hub.db`; exit 0 is PASS, a lagging backup is WARN, never a failure). To restore, stop anything using the store, copy the backup over `data/hub.db` and remove `data/hub.db-wal` and `data/hub.db-shm`.

## What Investing Hub is

The advisor and attention layer that pairs with Investing Companion (IC). IC owns live market
state; the hub owns the operator's theses, principles and profile and decides when something is
worth the operator's attention. Public template, private instance: the repo is machinery only.

The room session changes IC itself, directly: `hub ic ...` verbs apply each advisor action with
an `advisor:write` token, log it in `ic_writes` and post an IC receipt. There is no handoff step
(decided 2026-10-06).

Non-goals: the hub never executes trades, never writes to IC except through the logged `hub ic`
verbs, and never commits personal data.

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
hub db backup              # online backup into backups/, integrity-checked, keeps the last 7 backup days
hub db restore-check [FILE]  # verify a backup (default: newest) against the live store; non-zero on FAIL
hub import claude-export DIR [--apply]  # claude.ai export as revision-1 documents; dry run by default
hub doc list [--titles]    # documents with latest revision, source and time; titles hidden unless asked
hub doc show SLUG [--revision N]  # print one revision's body (default latest)
hub doc revise SLUG --source-ref HANDLE [--body-file F] [--kind K] [--title T]  # append a session revision; body from F or stdin
hub custodian import FILE --custodian NAME --kind positions|transactions [--account L] [--as-of D] [--map H=F ...] [--apply]  # one CSV -> one custodian_snapshots row; dry run by default
hub custodian list         # snapshots: custodian, account, kind, as_of, imported_at, row count
hub producers list [--db PATH] [--mv-analyst-root R] [--week-ahead-root R] [--triage-queue-dir D]  # stage-0 candidates; stateful: advances the triage cursor and writes beat proposals
hub ic show [--counts]     # counts-only view of the cached context pack
hub session-open           # print the session status block; always exits 0, read-only
hub ic alert add|modify|remove ...            # IC alerts (thresholds, --inactive and remove need --yes)
hub ic watchlist add-item|update-item|create ...  # watchlists and items
hub ic event add|update|remove ...            # calendar events
hub ic trade log SYMBOL --type T --quantity N --price P --yes  # log a trade (needs --yes)
hub ic trigger add|update|retire ...          # trigger-playbook standing orders
hub ic lesson add ... / hub ic ratio add ...  # lessons and ratios
hub ic writes [--limit N]  # the ic_writes log, newest first
hub ic revert ID [--yes]   # undo one logged write (refuses if IC changed since)
```

`hub db init|migrate|backup` take `--db PATH` (default `data/hub.db`) and `backup` takes
`--backup-dir PATH` (default `backups/`); defaults are relative to the current directory, so run
them from the instance directory (the working directory that holds data/, backups/ and import/). They exit 1 with one line on stderr on failure.
`hub import claude-export` reads top-level `*.md` and `knowledge/*.md` (skipping the two IC
contract docs), keys each document by its relative path, and only adds documents not yet
stored, so re-running changes nothing; a file whose content changed is reported, never
re-imported. Its default is a dry run that writes nothing. `hub ic pull` calls the live IC API.

`hub custodian import` takes `--db PATH`, reads `.csv` only (anything else exits 1; PDFs are not
parsed), skips lines above the header row, and matches headers case- and punctuation-insensitively
against synonym sets in `hub/custodian.py`. Required: transactions `date, action, symbol, quantity`;
positions `symbol, quantity`. A missing one exits 1 naming it, the file's headers and the
`--map 'header=field'` form. `--apply` needs an existing database and writes one snapshot
(`raw` = the file text, `mapping` = stored `--map` overrides, `source_ref` = the path relative
to the current working directory, else as given, `account` from `--account` or the `<account-label>` folder of the drop
layout, else none, `as_of` from `--as-of` or the newest date in a date column); a repeated
`--apply` import that produces the same `(custodian, kind, as_of, source_ref)` is a no-op. `hub custodian list` takes `--db PATH`
and reuses stored mappings when counting rows (ignoring trailing summary, total and disclaimer lines);
for a snapshot stored without an account it shows the label derived from `source_ref`, else `-`.
Drop layout: README "Custodian exports".

`hub doc list|show|revise` take `--db PATH`. `revise` appends a revision with `source_kind = session`
and prints `<slug> revision N (session)`; creating a new slug needs `--kind` (and `--kind`/`--title` on an existing slug are refused), and an empty body
is refused. Unknown slugs or revisions and other failures exit 1 with one line on stderr.

`hub session-open` (`--db PATH`, `--stale-days N`) prints one status block: a `now:` line (local
weekday, date, time and zone), pack age and versions, contract drift against IC's contract docs
(or `contract: in sync`), pending briefs, `recent IC writes (last 24h): N` with one line each,
documents not revised for `--stale-days` (default `session_open.stale_days`
in `config.yaml`, else 30), and one `WARN` line per problem. It never writes anything and always
exits 0. `.claude/settings.json` runs it as the `SessionStart` hook, so its output opens every
session; the hook falls back to `python3 -m hub` and never fails the session if `hub` is absent.

`hub producers list` takes `--db PATH` (an existing store, so run `hub db init` first) plus
root overrides for the three producer data homes (`~/mv-analyst`, `~/week-ahead`,
`~/brief/investing-triage-queue` by default). It is the stateful producer path: the triage
read cursor persists and week-ahead beat proposals are written; `hub session-open` prints
the same producer lines but reads without persisting anything. Producer files are never
written, moved or truncated.

## Repo map

```text
hub/cli.py       argparse entry point; one function per subcommand
hub/importer.py  claude.ai export reader behind `hub import claude-export`
hub/custodian.py custodian CSV reader behind `hub custodian import` (synonym maps, preamble skip)
hub/producers/   read-only producer adapters (mv-analyst index/analyses, week-ahead ledger/beats, triage queue)
hub/ic.py        IC adapter: pack read path plus the write client (`IcClient.call`, name resolution)
hub/ic_writes.py the advisor-action planners, the `ic_writes` log, receipts and revert
hub/ic_cli.py    argparse wiring for the `hub ic` write verbs
hub/store.py     SQLite connection, migrations, document revisions, custodian snapshots, backups
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
through its API; the session writes changes back itself with `hub ic` verbs.
See `ROADMAP.md` for the diagram. Make every IC change through a `hub ic` verb, never by calling
IC's write API by hand, so it is logged, receipted and revertible.

## Writing to IC

Run `hub ic ...` from the instance directory with `IC_API_TOKEN` in the environment. Each verb
resolves names (never ids) and reads the before-state. It then records a `pending` `ic_writes`
row (`id, at, action, target, method, path, request, before, after, ic_id, receipt_id,
receipt_error, source_ref, reverted_by, status, error`) BEFORE sending, sends the request, reads
the after-state and marks the row `applied`. If IC refuses the request (HTTP below 500) the row
becomes `failed` and nothing changed. If there is no answer (timeout, dropped connection, 5xx) the
row becomes `unknown` with a `WARN`: IC may have applied it, so check IC before retrying. Last it
posts a receipt to IC (`POST /export/handoff-receipts`, `source: investing_hub`). A receipt
failure never undoes the write; it is recorded in `receipt_error` and printed as a `WARN`.
`hub ic writes` shows each row's status.

This depends on IC PR #383 being deployed (with #381): it adds `GET /triggers` and
`GET /triggers/{id}` to the advisor token and lets `PUT`/`DELETE /events/{uuid}` through. `--dry-run` prints the resolved
request and sends nothing (it still reads to resolve names). `--source-ref` records provenance.

Name resolution: an alert is matched by exact name, else a unique prefix; a watchlist item by
symbol, plus `--watchlist NAME` when the symbol is on several; an account by name. Zero or several
matches stop with an error listing the candidates. Triggers are found by name through
`GET /triggers` (or by `--id`), with before and after state from `GET /triggers/{id}`. Events are
addressed by `--id` (printed by `event add`): IC's advisor token can read no single event, so event
updates and removals capture no before-state. Fields: `--thesis` replaces, `--append-thesis` appends,
`--clear FIELD` sets a field to null, `--entry-zone tier:low:high` repeats (empty bound = null).

Confirmation policy (enforced in `hub/ic_writes.py`): these need `--yes`. Confirm with Andrew in
chat first, then pass `--yes`.
- `trade log`;
- any write that deactivates or removes an alert (`alert modify --inactive`, `alert remove`, or a
  revert that does either);
- any write that sets, changes or clears a level: every `alert add`, an alert's threshold or
  condition (`alert modify --threshold|--condition`), a watchlist item's `--target-price` or
  `--entry-zone` (or `--clear` of either), or a revert that restores one. Levels and sizes are
  always proposed, never applied silently.

Everything else (notes, theses, names, cooldowns, events, triggers, lessons) applies directly.

`hub ic revert ID` undoes one write and logs the revert. A create is removed where IC has a
delete route (alerts, events); a modify is restored from the recorded before-state (changed
fields only); a removed alert is re-created from its before-state (new id; trigger links are not
restored); a trigger update is restored like any modify. Everything else (trades, watchlist
items and watchlists, ratios, lessons, trigger adds and retires, event edits and removals) is
refused with the reason. Only `applied` writes can be reverted. It also refuses when IC no longer
matches the recorded after-state, or, for events (which the token cannot read), when a later hub
write touched the same event.

The handoff flow is retired. Migration 0007 leaves the old `handoffs` table and its rows alone;
nothing uses it. IC's contract docs still describe handoff blocks as an optional legacy path;
ignore that path.

## Session start and follow-ups

- At session start, state the date and time from the `now:` line of `hub session-open` before
  anything else. Do not guess the date or the weekday.
- When Andrew agrees to a dated action (for example "CCJ GTCs expire Oct 15: check fill, log
  trade"), record it in kitchen-table with the `table` CLI: `table commit add "ACTION" --cue-type
  date --cue-value YYYY-MM-DD --source investing-hub`; use `table followup add` and `table thread
  touch` for follow-ups and the conversation thread. The binary is `$TABLE_BIN` (default
  `table`); if it is absent, say so in one line and carry on.
- The flow is one way. Put the action text and date in kitchen-table, but never let kitchen-table
  read hub or IC data, and do not paste holdings, values or account names into it.

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
| `IC_API_TOKEN` | IC API token carrying both `pack:read` and `advisor:write` (the pack reads and the `hub ic` write verbs), resolved at call time from a credential manager; never stored in the repo |
| `TABLE_BIN` | Optional; the kitchen-table `table` binary used to record follow-ups (default `table`). Absent is fine: skip with a note |
| `HUB_IC_BASE_URL` | Optional; overrides `ic.base_url` from `config.yaml` (see `config.example.yaml`) |
| `MASSIVE_API_KEY` | Massive market-data key, read by the `massive` MCP server declared in `.mcp.json`, either via `${MASSIVE_API_KEY}` expansion (resolved from `.env.local` at launch) or resolved by the server itself from a gitignored `.env.massive` (README "Resolving the key per server"); never commit it |
| `OP_CONNECT_HOST` | 1Password Connect URL (`http://<connect-host>:8090`); selects the `connect` path of `scripts/hub-session.sh`, which resolves `.env.local` via `~/.claude/scripts/op-resolve.py` and starts `claude` so `MASSIVE_API_KEY` and `IC_API_TOKEN` reach the session |
| `HUB_INSTANCE_DIR` | Optional; the directory `scripts/hub-session.sh` works in and reads `.env.local` from. Unset, it is the current directory, so run the script from the instance directory |
| `HUB_SESSION_MODE` | Optional; `connect`, `op` or `env` forces one credential path in `scripts/hub-session.sh`. Unset, it auto-detects: `env` (both variables already set), then `connect` (`OP_CONNECT_HOST` set), then `op` (the 1Password CLI is installed, resolving `.env.local` with `op run`). `.env.local` is required except in `env` mode |

`.mcp.json` is local-only (gitignored); copy it from `.mcp.json.example`.

Per-machine values (IC base URL, credential references) live in `CLAUDE.local.md` and
`config.yaml` in the instance directory (the one you run `hub` from), not the code checkout, so
personal data stays out of the code tree; see `CLAUDE.local.md.example` for the shape.
