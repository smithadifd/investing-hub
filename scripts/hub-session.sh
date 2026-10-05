#!/usr/bin/env bash
# Start Claude Code with MASSIVE_API_KEY (massive MCP server) and IC_API_TOKEN (SessionStart hook)
# in its environment. Three credential paths, picked by HUB_SESSION_MODE=connect|op|env or, when
# unset, auto-detected in this order:
#   env      both variables already non-empty in the environment -> claude directly
#   connect  OP_CONNECT_HOST set -> ~/.claude/scripts/op-resolve.py resolves .env.local
#   op       the 1Password CLI is installed -> op run resolves .env.local
# Run it from the instance directory (the one holding .env.local), or set HUB_INSTANCE_DIR to it.
# Extra arguments pass through to claude, e.g. hub-session.sh -p '...'.
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

# The instance directory (where .env.local lives) is the current directory unless
# HUB_INSTANCE_DIR names another one.
cd "${HUB_INSTANCE_DIR:-.}" 2>/dev/null || {
  echo "hub-session: cannot enter HUB_INSTANCE_DIR=${HUB_INSTANCE_DIR:-.}" >&2
  exit 2
}

if [ "$mode" != env ] && [ ! -f .env.local ]; then
  echo "hub-session: .env.local is missing (looked in $(pwd)); copy .env.example to .env.local there, run from the instance directory, or set HUB_INSTANCE_DIR (see README § Massive)" >&2
  exit 2
fi

if [ -n "${HUB_SESSION_MODE:-}" ]; then
  case "$mode" in
    op)
      if ! command -v op >/dev/null 2>&1; then
        echo "hub-session: HUB_SESSION_MODE=op but the 1Password CLI op is not on PATH" >&2
        exit 2
      fi
      ;;
    connect)
      if [ ! -e "$HOME/.claude/scripts/op-resolve.py" ]; then
        echo "hub-session: HUB_SESSION_MODE=connect but $HOME/.claude/scripts/op-resolve.py is missing" >&2
        exit 2
      fi
      ;;
    env)
      if [ -z "${MASSIVE_API_KEY:-}" ] || [ -z "${IC_API_TOKEN:-}" ]; then
        echo "hub-session: HUB_SESSION_MODE=env needs MASSIVE_API_KEY and IC_API_TOKEN both non-empty" >&2
        exit 2
      fi
      ;;
  esac
fi

echo "hub-session: using $mode" >&2

case "$mode" in
  env) exec claude "$@" ;;
  connect) exec "$HOME/.claude/scripts/op-resolve.py" --env-file .env.local -- claude "$@" ;;
  op) exec op run --env-file .env.local -- claude "$@" ;;
esac
