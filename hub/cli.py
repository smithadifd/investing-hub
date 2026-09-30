"""Command-line entry point. Commands not yet implemented are stubs that exit 2."""

import argparse
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from hub import store

NOT_IMPLEMENTED_EXIT = 2


def _not_implemented(name: str) -> int:
    print(f"hub {name}: not implemented", file=sys.stderr)
    return NOT_IMPLEMENTED_EXIT


def _db_path_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--db",
        type=Path,
        default=store.DEFAULT_DB_PATH,
        help="database file (default: %(default)s, relative to the current directory)",
    )


def _backup_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=store.DEFAULT_BACKUP_DIR,
        help="backup directory (default: %(default)s, relative to the current directory)",
    )


def _fail(name: str, exc: Exception | str) -> int:
    print(f"hub {name}: {exc}", file=sys.stderr)
    return 1


def _apply_migrations(name: str, db: Path) -> int:
    try:
        conn = store.connect(db)
        try:
            applied = store.migrate(conn)
        finally:
            conn.close()
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    if applied:
        print(f"{db}: applied {', '.join(applied)}")
    else:
        print(f"{db}: schema up to date")
    return 0


# One stub per function: replace a function's body to implement that command.
def cmd_db_init(args: argparse.Namespace) -> int:
    return _apply_migrations("db init", args.db)


def cmd_db_migrate(args: argparse.Namespace) -> int:
    if not args.db.is_file():
        return _fail("db migrate", f"database not found: {args.db} (run `hub db init`)")
    return _apply_migrations("db migrate", args.db)


def cmd_db_backup(args: argparse.Namespace) -> int:
    try:
        result = store.backup(args.db, args.backup_dir)
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail("db backup", exc)
    print(f"backup written: {result.path} (integrity_check ok)")
    for path in result.pruned:
        print(f"pruned: {path}")
    return 0


def cmd_db_restore_check(args: argparse.Namespace) -> int:
    return _not_implemented("db restore-check")


def cmd_import_claude_export(args: argparse.Namespace) -> int:
    return _not_implemented("import claude-export")


def cmd_ic_pull(args: argparse.Namespace) -> int:
    return _not_implemented("ic pull")


def cmd_ic_docs(args: argparse.Namespace) -> int:
    return _not_implemented("ic docs")


def cmd_session_open(args: argparse.Namespace) -> int:
    return _not_implemented("session-open")


# group -> subcommand -> (help, handler). A group of None is a top-level command.
COMMANDS: dict[str | None, dict[str, tuple[str, Callable[[argparse.Namespace], int]]]] = {
    "db": {
        "init": ("create the local database and apply migrations", cmd_db_init),
        "migrate": ("apply pending migrations", cmd_db_migrate),
        "backup": ("write an integrity-checked online backup", cmd_db_backup),
        "restore-check": ("verify the newest backup restores cleanly", cmd_db_restore_check),
    },
    "import": {
        "claude-export": (
            "import a claude.ai export as document revisions",
            cmd_import_claude_export,
        ),
    },
    "ic": {
        "pull": ("fetch the Investing Companion context pack", cmd_ic_pull),
        "docs": ("show Investing Companion's contract docs", cmd_ic_docs),
    },
    None: {
        "session-open": ("run the session-open checks", cmd_session_open),
    },
}


# Full command name -> function adding that command's arguments to its parser.
ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "db init": _db_path_arg,
    "db migrate": _db_path_arg,
    "db backup": _backup_args,
}


def _command_listing() -> str:
    lines = ["full command surface:"]
    for group, subs in COMMANDS.items():
        for name, (help_text, _) in subs.items():
            full = name if group is None else f"{group} {name}"
            lines.append(f"  hub {full:<22} {help_text}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub",
        description="Investing Hub command line.",
        epilog=_command_listing(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    top = parser.add_subparsers(dest="command", metavar="<command>")
    for group, subs in COMMANDS.items():
        if group is None:
            for name, (help_text, handler) in subs.items():
                sub = top.add_parser(name, help=help_text)
                sub.set_defaults(handler=handler)
                if name in ARGUMENTS:
                    ARGUMENTS[name](sub)
            continue
        group_parser = top.add_parser(group, help=f"{group} commands")
        group_subs = group_parser.add_subparsers(dest="subcommand", metavar="<subcommand>")
        group_subs.required = True
        for name, (help_text, handler) in subs.items():
            sub = group_subs.add_parser(name, help=help_text)
            sub.set_defaults(handler=handler)
            if f"{group} {name}" in ARGUMENTS:
                ARGUMENTS[f"{group} {name}"](sub)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0
    return handler(args)
