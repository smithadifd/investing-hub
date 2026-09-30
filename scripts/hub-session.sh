#!/usr/bin/env bash
# Start Claude Code with both credential references in .env.local resolved into its environment
# (MASSIVE_API_KEY for the massive MCP server, IC_API_TOKEN for the SessionStart hook).
# Extra arguments pass through to claude, e.g. scripts/hub-session.sh -p '...'.
set -euo pipefail

if [ -z "${OP_CONNECT_HOST:-}" ]; then
  echo "hub-session: OP_CONNECT_HOST is not set; export the 1Password Connect URL first (see README § Massive)" >&2
  exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [ ! -f .env.local ]; then
  echo "hub-session: .env.local is missing; copy .env.example to .env.local (see README § Massive)" >&2
  exit 2
fi

exec "$HOME/.claude/scripts/op-resolve.py" --env-file .env.local -- claude "$@"
