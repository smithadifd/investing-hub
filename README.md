# Investing Hub

The advisor and attention layer that pairs with
[Investing Companion](https://github.com/smithadifd/investing_companion) (IC). IC is the system of
record for live market state; the hub keeps the operator's theses, principles and profile, works
out what new information means for that book, and decides when something deserves attention.

This repo is machinery only. Personal data lives in a local SQLite database and gitignored local
files and is never committed. See [ROADMAP.md](ROADMAP.md) for the architecture and phases.

**Status:** early. Every `hub` subcommand in the first milestone is implemented: the store and its backups, the claude.ai import, the IC pack client and the session-open status block.

## Quick start

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e .[dev]
hub --help
```

## Commands

| Command | Purpose |
|---|---|
| `hub db init` / `migrate` / `backup` / `restore-check` | Local database lifecycle and backups (`restore-check` defaults to the newest backup) |
| `hub import claude-export` | Import a claude.ai export as document revisions |
| `hub ic pull` / `docs` | Read the Investing Companion context pack and contract docs |
| `scripts/hub-session.sh` | Start Claude Code with `MASSIVE_API_KEY` and `IC_API_TOKEN` resolved from `.env.local` (needs `OP_CONNECT_HOST`) |
| `hub doc list` / `show` / `revise` | List documents, print any revision of one, and append a session revision (`revise` needs `--source-ref`; a new slug needs `--kind`) |
| `hub custodian import` / `list` | Import a custodian positions or transactions CSV as one snapshot (dry run by default; `--apply` writes) and list the snapshots |
| `hub session-open` | Checks run when an advisor session opens |

### Custodian exports

`hub custodian import` reads CSV exports only; PDF statements are not parsed. Keep the files
under the git-ignored `import/` directory, one folder per custodian, date and account:

```text
import/custodians/<custodian>/<YYYY-MM-DD>/<account-label>/positions.csv
import/custodians/<custodian>/<YYYY-MM-DD>/<account-label>/transactions.csv
import/custodians/<custodian>/<YYYY-MM-DD>/<account-label>/statement.pdf
```

Run it without `--apply` first to see how the headers map and how many rows were found; pass
`--map "Header As Written=field"` for any column it does not recognise. Overrides are persisted
with the snapshot so `hub custodian list` reuses them when counting data rows.
## Onboarding

The [onboarding kit](docs/onboarding/README.md) is a fill-in-the-blanks scaffold for standing up your
own advisor: an interview script, operating instructions and portfolio-state templates.

## Development

```bash
ruff check .
ruff format --check .
pytest
```

## Local configuration

Copy `CLAUDE.local.md.example` to `CLAUDE.local.md` and fill in your persona, credential
references and IC base URL. That file is gitignored.

### Massive market-data MCP server

One-time setup:

```bash
brew install uv                  # macOS; the dotfiles Brewfile lists it
uv tool install --with 'mcp<2' "mcp_massive @ git+https://github.com/massive-com/mcp_massive@v0.10.0"
cp .mcp.json.example .mcp.json   # local-only, gitignored
cp .env.example .env.local       # local-only, gitignored; holds 1Password references, not keys
```

The `--with 'mcp<2'` pin is required because mcp_massive 0.10.0 imports `mcp.server.fastmcp`, which mcp 2.x renamed, so an unpinned install dies at start and Claude Code reports `CONNECTION_CLOSED`. If already installed unpinned, re-install with:

```bash
uv tool install --force --with 'mcp<2' "mcp_massive @ git+https://github.com/massive-com/mcp_massive@v0.10.0"
```

Start Claude Code through the launcher. It needs `MASSIVE_API_KEY` (for the `massive` MCP server) and
`IC_API_TOKEN` (for the SessionStart hook) in the session environment, and supports three ways to get them there:

| Path | For | What you do |
|---|---|---|
| `connect` | 1Password Connect users | `export OP_CONNECT_HOST=http://<connect-host>:8090`; `.env.local` holds the `op://` references and `~/.claude/scripts/op-resolve.py` resolves them |
| `op` | 1Password CLI users | Sign in to `op`; `.env.local` holds the same `op://` references and `op run --env-file .env.local` resolves them |
| `env` | Everyone else | Export `MASSIVE_API_KEY` and `IC_API_TOKEN` yourself (by hand or from another secrets manager); no `.env.local` needed |

```bash
scripts/hub-session.sh            # extra args pass through: scripts/hub-session.sh -p '...'
```

The launcher picks the path itself, in this order: `env` when both variables are already set, then
`connect` when `OP_CONNECT_HOST` is set, then `op` when the `op` CLI is installed. Set
`HUB_SESSION_MODE=connect|op|env` to force one. It prints `hub-session: using <mode>` to stderr
before it starts `claude`, and exits 2 with a one-line message if no path is available or (for
`connect` and `op`) `.env.local` is missing. Secrets are never printed.

The `connect` path wraps this resolver call:

```bash
OP_CONNECT_HOST=http://<connect-host>:8090 ~/.claude/scripts/op-resolve.py --env-file .env.local -- claude
```

## License

MIT. See [LICENSE](LICENSE).
