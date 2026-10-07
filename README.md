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
| `hub ic pull` / `docs` / `show` | Read the Investing Companion context pack and contract docs |
| `scripts/hub-session.sh` | Start Claude Code with `MASSIVE_API_KEY` and `IC_API_TOKEN` resolved from `.env.local` (needs `OP_CONNECT_HOST`) |
| `hub doc list` / `show` / `revise` | List documents, print any revision of one, and append a session revision (`revise` needs `--source-ref`; a new slug needs `--kind`) |
| `hub custodian import` / `list` | Import a custodian positions or transactions CSV as one snapshot (dry run by default; `--apply` writes) and list the snapshots |
| `hub letter midweek` | The weekly letter on the mechanical merit gate (silent unless something clears; `--deliver` hands it to `HUB_LETTER_SEND_CMD`) |
| `hub session-open` | Checks run when an advisor session opens |

### Custodian exports

`hub custodian import` reads CSV exports only; PDF statements are not parsed. Keep the files
under `import/` in your instance directory (the one you run `hub` from), one folder per custodian, date and account:

```text
import/custodians/<custodian>/<YYYY-MM-DD>/<account-label>/positions.csv
import/custodians/<custodian>/<YYYY-MM-DD>/<account-label>/transactions.csv
import/custodians/<custodian>/<YYYY-MM-DD>/<account-label>/statement.pdf
```

The `<account-label>` folder becomes the snapshot's account (pass `--account` to set it by hand),
so `hub custodian list` tells several accounts at one custodian apart.

Run it without `--apply` first to see how the headers map and how many rows were found; pass
`--map "Header As Written=field"` for any column it does not recognise. Overrides are persisted
with the snapshot so `hub custodian list` reuses them when counting data rows.

### Midweek letter

`hub letter midweek` grades the thesis documents in the store against two legs of evidence: the
beats corpus from mv-analyst, and a rotation scan supplied with `--scan-file` (views are parsed
from thesis document bodies; see `hub/letter.py` for the views shape). The scan is a JSON file:

```json
{
  "benchmark": "BAA",
  "asof": "2026-02-09",
  "rows": [
    {"ticker": "BOT", "close": 103.0, "ret20": 3.0, "rs20": -1.0, "pct52w": 40.0}
  ]
}
```

`ticker` and `close` are required in each row; `ret20`, `ret60`, `rs20`, `rs60`, `pct52w`,
`sma200`, `trend`, `crossed` and the row's own `asof` are optional. Without `--scan-file` the
market leg is off — the output says "market leg off: no scan source configured" and only the
corpus leg can clear the gate. A no-scan rerun refuses to replace that date's scan-backed letter
unless `--force` is given. A quiet week prints one status line and writes no file. `--deliver`
hands the written letter to the command in `HUB_LETTER_SEND_CMD` (unset means a clear refusal).
A successful delivery is marked per issue date, so rerunning `--deliver` does not send twice; the
repo itself schedules nothing and sends nothing.

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

In your instance directory (the one you run `hub` from, not the code checkout), copy
`CLAUDE.local.md.example` to `CLAUDE.local.md` and fill in your persona, credential references
and IC base URL. Keeping it there keeps personal data out of the code tree.

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
# run from the instance directory (the one holding .env.local); extra args pass through
/path/to/hub/scripts/hub-session.sh -p '...'
HUB_INSTANCE_DIR=/path/to/instance /path/to/hub/scripts/hub-session.sh   # or name it explicitly
```

The launcher picks the path itself, in this order: `env` when both variables are already set, then
`connect` when `OP_CONNECT_HOST` is set, then `op` when the `op` CLI is installed. Set
`HUB_SESSION_MODE=connect|op|env` to force one. The launcher works in the current directory (or
`HUB_INSTANCE_DIR`), not the code checkout, and looks for `.env.local` there. It prints `hub-session: using <mode>` to stderr
before it starts `claude`, and exits 2 with a one-line message if no path is available or (for
`connect` and `op`) `.env.local` is missing. Secrets are never printed.

The `connect` path wraps this resolver call:

```bash
OP_CONNECT_HOST=http://<connect-host>:8090 ~/.claude/scripts/op-resolve.py --env-file .env.local -- claude
```

#### Resolving the key per server (no launcher needed)

The default `.mcp.json` passes `${MASSIVE_API_KEY}` through from the shell that started `claude`, so
a plain `claude` in the repo starts the server with an empty key and every call fails with
`Unknown API Key`. To make the server independent of how the session starts, have it resolve its
own key. Put only the Massive reference in `.env.massive` (gitignored by `.env.*`):

```bash
MASSIVE_API_KEY=op://<vault>/<item>/MASSIVE_API_KEY
```

and wrap the server command in your local `.mcp.json`. Use absolute paths, since the file is local:

```json
{
  "mcpServers": {
    "massive": {
      "command": "/Users/<you>/.claude/scripts/op-resolve.py",
      "args": ["--env-file", "/path/to/investing-hub/.env.massive",
               "--", "/Users/<you>/.local/bin/mcp_massive"],
      "env": { "OP_CONNECT_HOST": "http://<connect-host>:8090" }
    }
  }
}
```

For the `op` CLI, use `"command": "op"` with `"args": ["run", "--env-file", "/path/to/investing-hub/.env.massive", "--", "/Users/<you>/.local/bin/mcp_massive"]`
and drop the `env` block. Use a separate env file rather than `.env.local` so the server only receives
its own key, not `IC_API_TOKEN`. If the resolver fails, it exits non-zero before starting the
server, and `/mcp` shows `massive` as failed.
`IC_API_TOKEN` still has to come from the launcher (or the environment); without it,
`hub session-open` prints a `WARN contract:` line.

## License

MIT. See [LICENSE](LICENSE).
