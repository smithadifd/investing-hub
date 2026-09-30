#!/usr/bin/env bash
# Start Claude Code with MASSIVE_API_KEY (massive MCP server) and IC_API_TOKEN (SessionStart hook)
# in its environment. Three credential paths, picked by HUB_SESSION_MODE=connect|op|env or, when
# unset, auto-detected in this order:
#   env      both variables already non-empty in the environment -> claude directly
#   connect  OP_CONNECT_HOST set -> ~/.claude/scripts/op-resolve.py resolves .env.local
#   op       the 1Password CLI is installed -> op run resolves .env.local
# Extra arguments pass through to claude, e.g. scripts/hub-session.sh -p '...'.
set -euo pipefail

mode="${HUB_SESSION_MODE:-}"
if [ -z "$mode" ]; then
  if [ -n "${MASSIVE_API_KEY:-}" ] && [ -n "${IC_API_TOKEN:-}" ]; then
    mode=env
  elif [ -n "${OP_CONNECT_HOST:-}" ]; then
    mode=connect
  elif command -v op >/dev/null 2>&1; then
    mode=op
  else
    echo "hub-session: no credential path found; export MASSIVE_API_KEY and IC_API_TOKEN (env), set OP_CONNECT_HOST (connect), or install the 1Password CLI op (op); see README § Massive" >&2
    exit 2
  fi
fi

case "$mode" in
  env | connect | op) ;;
  *)
    echo "hub-session: HUB_SESSION_MODE must be connect, op or env (see README § Massive)" >&2
    exit 2
    ;;
esac

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

if [ "$mode" != env ] && [ ! -f .env.local ]; then
  echo "hub-session: .env.local is missing; copy .env.example to .env.local (see README § Massive)" >&2
  exit 2
fi

echo "hub-session: using $mode" >&2

case "$mode" in
  env) exec claude "$@" ;;
  connect) exec "$HOME/.claude/scripts/op-resolve.py" --env-file .env.local -- claude "$@" ;;
  op) exec op run --env-file .env.local -- claude "$@" ;;
esac
