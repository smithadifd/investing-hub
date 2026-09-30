# CLAUDE.md — Investing Hub

@AGENTS.md

Everything about the hub lives in the imported AGENTS.md — read it first.
Personal config is in the gitignored CLAUDE.local.md — read it first if it exists.

## Notes for Claude
- Run `ruff check .`, `ruff format --check .` and `pytest` before calling work done.
- Never read, list or copy `import/`, `data/` or `backups/`; they hold personal data.
- Shared hooks go in `.claude/settings.json`; machine overrides go in the gitignored `.claude/settings.local.json`.
