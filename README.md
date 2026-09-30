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
| `hub db init` / `migrate` / `backup` / `restore-check` | Local database lifecycle and backups |
| `hub import claude-export` | Import a claude.ai export as document revisions |
| `hub ic pull` / `docs` | Read the Investing Companion context pack and contract docs |
| `scripts/hub-session.sh` | Start Claude Code with `MASSIVE_API_KEY` and `IC_API_TOKEN` resolved from `.env.local` (needs `OP_CONNECT_HOST`) |
| `hub session-open` | Checks run when an advisor session opens |

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
uv tool install "mcp_massive @ git+https://github.com/massive-com/mcp_massive@v0.10.0"
cp .mcp.json.example .mcp.json   # local-only, gitignored
cp .env.example .env.local       # local-only, gitignored; holds 1Password references, not keys
```

Export the 1Password Connect URL (placeholder shown) and start Claude Code through the launcher.
It resolves `MASSIVE_API_KEY` and `IC_API_TOKEN` from `.env.local` into the session environment
(the resolver never prints them), so the `massive` MCP server and the SessionStart hook both see them:

```bash
export OP_CONNECT_HOST=http://<connect-host>:8090
scripts/hub-session.sh            # extra args pass through: scripts/hub-session.sh -p '...'
```

The launcher wraps this resolver call:

```bash
OP_CONNECT_HOST=http://<connect-host>:8090 ~/.claude/scripts/op-resolve.py --env-file .env.local -- claude
```

## License

MIT. See [LICENSE](LICENSE).
