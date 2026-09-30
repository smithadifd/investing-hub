# Investing Hub

The advisor and attention layer that pairs with
[Investing Companion](https://github.com/smithadifd/investing_companion) (IC). IC is the system of
record for live market state; the hub keeps the operator's theses, principles and profile, works
out what new information means for that book, and decides when something deserves attention.

This repo is machinery only. Personal data lives in a local SQLite database and gitignored local
files and is never committed. See [ROADMAP.md](ROADMAP.md) for the architecture and phases.

**Status:** early. The `hub` command line exists and every subcommand is a stub that exits 2.

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
| `hub session-open` | Checks run when an advisor session opens |

## Development

```bash
ruff check .
ruff format --check .
pytest
```

## Local configuration

Copy `CLAUDE.local.md.example` to `CLAUDE.local.md` and fill in your persona, credential
references and IC base URL. That file is gitignored.

## License

MIT. See [LICENSE](LICENSE).
