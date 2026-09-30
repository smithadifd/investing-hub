"""Command-line entry point."""

import argparse
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from hub import ic, importer, restore, session_open, store


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


def _restore_check_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("backup", type=Path, help="backup file to verify (e.g. backups/hub-*.db)")
    _db_path_arg(parser)


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
    name = "db restore-check"
    if not args.backup.is_file():
        return _fail(name, f"backup not found: {args.backup}")
    if not args.db.is_file():
        return _fail(name, f"database not found: {args.db}")
    try:
        checks = restore.restore_check(args.backup, args.db)
    except (sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    print(restore.format_checks(checks))
    failed = [c for c in checks if c.status == restore.FAIL]
    if failed:
        print(
            f"FAIL: {len(failed)} of {len(checks)} checks failed; do not restore from this backup"
        )
        return 1
    warned = [c for c in checks if c.status == restore.WARN]
    if warned:
        passed = len(checks) - len(warned)
        print(f"PASS: {passed} passed, {len(warned)} warned, 0 failed")
    else:
        print(f"PASS: all {len(checks)} checks passed")
    return 0


def cmd_import_claude_export(args: argparse.Namespace) -> int:
    name = "import claude-export"
    try:
        candidates, skipped = importer.scan(args.dir)
        if args.apply:
            conn = store.connect(args.db)
            try:
                store.migrate(conn)
                planned = importer.plan(conn, candidates)
                written = importer.write_new(conn, planned)
            finally:
                conn.close()
        else:
            planned = _dry_run_plan(args.db, candidates)
            written = 0
    except (importer.ImportError_, store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    verb = {"new": "add" if args.apply else "would add", "unchanged": "unchanged"}
    for state, cand in planned:
        label = verb.get(state, "changed, left as is")
        print(f"{label}: {cand.source_ref} ({cand.kind})")
    for ref, reason in skipped:
        print(f"skipped: {ref} ({reason})")
    if args.apply:
        print(f"{args.db}: {written} document(s) written")
    else:
        print("dry run: nothing written (use --apply)")
    return 0


def _dry_run_plan(db: Path, candidates):
    """Plan against an existing database read-only; a missing one counts as empty."""
    if db.is_dir():
        raise OSError(f"database path is a directory: {db}")
    if not db.is_file():
        return importer.plan(None, candidates)
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        try:
            return importer.plan(conn, candidates)
        except sqlite3.OperationalError:  # schema not created yet
            return importer.plan(None, candidates)
    finally:
        conn.close()


def _ic_fail(name: str, exc: ic.IcError) -> int:
    print(f"hub {name}: {ic.redact(str(exc), os.environ.get(ic.TOKEN_ENV))}", file=sys.stderr)
    return 1


def cmd_ic_pull(args: argparse.Namespace) -> int:
    try:
        token = ic.read_token()
        meta = ic.pull_pack(ic.load_base_url(), token)
    except ic.IcError as exc:
        return _ic_fail("ic pull", exc)
    print(
        f"hub ic pull: stored {ic.PACK_DIR / ic.PACK_FILE}"
        f" (schema {meta['schema_version']}, advisor-actions {meta['advisor_actions_version']},"
        f" generated {meta['generated_at']}, fetched {meta['fetched_at']})"
    )
    return 0


def cmd_ic_docs(args: argparse.Namespace) -> int:
    try:
        token = ic.read_token()
        docs = ic.fetch_contract_docs(ic.load_base_url(), token)
    except ic.IcError as exc:
        return _ic_fail("ic docs", exc)
    for doc in docs:
        header = (
            f"== {doc.name}: stamp={doc.stamp} expected_stamp={doc.expected_stamp}"
            f" stamp_matches={str(doc.stamp_matches).lower()}"
        )
        print(ic.redact(header, token))
        print(ic.redact(doc.content.rstrip("\n"), token))
        print()
    return 0


def _session_open_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument(
        "--stale-days",
        type=int,
        default=None,
        metavar="N",
        help="list documents whose latest revision is older than N days"
        " (default: session_open.stale_days in config.yaml, else 30)",
    )


def cmd_session_open(args: argparse.Namespace) -> int:
    """Print the session-open status block. Always exits 0: a session must always open."""
    try:
        block = session_open.build_block(args.db, args.stale_days)
    except Exception as exc:  # the block builder guards itself; this is the last resort
        block = f"== hub session-open ==\nWARN session-open failed: {type(exc).__name__}"
    print(block)
    return 0


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


def _import_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument("dir", type=Path, help="export directory to read")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="print what would be written (default)"
    )
    mode.add_argument("--apply", action="store_true", help="write the new documents")


# Full command name -> function adding that command's arguments to its parser.
ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "db init": _db_path_arg,
    "db migrate": _db_path_arg,
    "db backup": _backup_args,
    "db restore-check": _restore_check_args,
    "import claude-export": _import_args,
    "session-open": _session_open_args,
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
