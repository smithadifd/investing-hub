"""Command-line entry point."""

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from hub import custodian, ic, importer, preflight, restore, session_open, store


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
    parser.add_argument(
        "backup",
        type=Path,
        nargs="?",
        help="backup file to verify (default: the newest hub-*.db in --backup-dir)",
    )
    _backup_args(parser)


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
    if args.backup is None:
        backups = store.list_backups(args.backup_dir)
        if not backups:
            return _fail(name, f"no backups found in {args.backup_dir}")
        args.backup = backups[-1]
        print(f"hub {name}: using {args.backup}")
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


def cmd_ic_preflight(args: argparse.Namespace) -> int:
    report, ready = preflight.build_report()
    if args.out is not None:
        try:
            args.out.write_text(report, encoding="utf-8")
        except OSError as exc:
            return _ic_fail("ic preflight", ic.IcError(f"cannot write report: {exc.strerror}"))
    print(report, end="")
    return 0 if ready else 1


def cmd_ic_show(args: argparse.Namespace) -> int:
    name = "ic show"
    meta_path = ic.PACK_DIR / ic.META_FILE
    pack_path = ic.PACK_DIR / ic.PACK_FILE
    if not meta_path.is_file():
        return _fail(name, f"pack metadata not found: {meta_path} (run `hub ic pull`)")
    if not pack_path.is_file():
        return _fail(name, f"pack not found: {pack_path} (run `hub ic pull`)")
    try:
        meta_text = meta_path.read_text(encoding="utf-8")
        pack_text = pack_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        reason = getattr(exc, "strerror", None) or str(exc)
        return _fail(name, f"cannot read pack: {reason} (run `hub ic pull`)")

    try:
        meta = json.loads(meta_text)
        pack = json.loads(pack_text)
    except ValueError:
        return _fail(name, "invalid JSON in cached pack (run `hub ic pull`)")

    if not isinstance(meta, dict):
        return _fail(name, "pack metadata is not a JSON object (run `hub ic pull`)")
    if not isinstance(pack, dict):
        return _fail(name, "pack is not a JSON object")

    try:
        header = (
            f"schema {meta['schema_version']}, advisor-actions {meta['advisor_actions_version']},"
            f" generated {meta['generated_at']}, fetched {meta['fetched_at']}"
        )
    except KeyError as exc:
        return _fail(name, f"pack metadata missing {exc} (run `hub ic pull`)")

    print(header)
    for key, value in pack.items():
        if key != "trade_summary" and isinstance(value, list):
            print(f"{key}: {len(value)}")

    trade_summary = "present" if pack.get("trade_summary") is not None else "absent"
    print(f"trade_summary: {trade_summary}")
    return 0


def _non_negative_int(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if value < 0:
        raise argparse.ArgumentTypeError("must be a non-negative integer")
    return value


def _session_open_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument(
        "--stale-days",
        type=_non_negative_int,
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


def _doc_list_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument(
        "--titles", action="store_true", help="also show each document's title (hidden by default)"
    )


def _doc_show_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument("slug", help="document slug")
    parser.add_argument(
        "--revision", type=int, default=None, metavar="N", help="revision to show (default: latest)"
    )


def _doc_revise_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument("slug", help="document slug")
    parser.add_argument("--source-ref", required=True, help="handle of the session making the edit")
    parser.add_argument(
        "--body-file",
        type=Path,
        default=None,
        help="file holding the new body (default: read the body from stdin)",
    )
    parser.add_argument("--kind", default=None, help="document kind (required for a new slug)")
    parser.add_argument("--title", default=None, help="document title (new slug only)")


def _open_existing(name: str, db: Path) -> sqlite3.Connection | int:
    if not db.is_file():
        return _fail(name, f"database not found: {db} (run `hub db init`)")
    return store.connect(db)


def cmd_doc_list(args: argparse.Namespace) -> int:
    name = "doc list"
    try:
        conn = _open_existing(name, args.db)
        if isinstance(conn, int):
            return conn
        try:
            docs = store.list_documents(conn)
        finally:
            conn.close()
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    headers = ["slug", "kind", "revision", "source_kind", "revised_at"]
    if args.titles:
        headers.append("title")
    rows = [[str(d["title"] or "") if h == "title" else str(d[h]) for h in headers] for d in docs]
    widths = [max([len(h), *(len(r[i]) for r in rows)]) for i, h in enumerate(headers)]
    for line in [headers, *rows]:
        print("  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip())
    return 0


def cmd_doc_show(args: argparse.Namespace) -> int:
    name = "doc show"
    try:
        conn = _open_existing(name, args.db)
        if isinstance(conn, int):
            return conn
        try:
            doc = store.get_document_revision(conn, args.slug, args.revision)
        finally:
            conn.close()
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    sys.stdout.write(doc["body"])
    return 0


def cmd_doc_revise(args: argparse.Namespace) -> int:
    name = "doc revise"
    try:
        body = args.body_file.read_text() if args.body_file else sys.stdin.read()
    except (OSError, UnicodeDecodeError) as exc:
        return _fail(name, exc)
    try:
        conn = _open_existing(name, args.db)
        if isinstance(conn, int):
            return conn
        try:
            if not body.strip():
                return _fail(name, "empty body refused")
            exists = conn.execute("SELECT 1 FROM documents WHERE slug = ?", (args.slug,)).fetchone()
            if exists is None and args.kind is None:
                return _fail(name, f"new document {args.slug}: --kind is required")
            if exists is not None and (args.kind is not None or args.title is not None):
                return _fail(
                    name, f"{args.slug} exists; --kind/--title apply to a new document only"
                )
            revision = store.insert_document_revision(
                conn,
                slug=args.slug,
                kind=args.kind or "",
                body=body,
                source_kind="session",
                source_ref=args.source_ref,
                title=args.title,
            )
        finally:
            conn.close()
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    print(f"{args.slug} revision {revision} (session)")
    return 0


def _custodian_import_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument("file", type=Path, help="CSV file to read")
    parser.add_argument("--custodian", required=True, help="custodian name, e.g. a broker label")
    parser.add_argument(
        "--account",
        default=None,
        metavar="LABEL",
        help="account label (default: the <account-label> folder of the drop layout, if any)",
    )
    parser.add_argument("--kind", required=True, choices=custodian.KINDS, help="file contents")
    parser.add_argument(
        "--as-of",
        default=None,
        metavar="YYYY-MM-DD",
        help="snapshot date (default: the newest date in the file's date column)",
    )
    parser.add_argument(
        "--map",
        action="append",
        default=[],
        metavar="HEADER=FIELD",
        help="map a header to a field by hand (repeatable)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="write the snapshot (default: dry run)"
    )


def cmd_custodian_import(args: argparse.Namespace) -> int:
    name = "custodian import"
    if args.file.suffix.lower() != ".csv":
        return _fail(
            name, f"{args.file}: only .csv files are imported (PDF statements are not parsed)"
        )
    try:
        overrides = custodian.normalize_overrides(args.map, kind=args.kind)
        as_of_arg = custodian.parse_as_of(args.as_of) if args.as_of else None
        raw = args.file.read_bytes().decode("utf-8-sig")
        parsed = custodian.parse(raw, args.kind, overrides)
    except (custodian.CustodianError, OSError, UnicodeDecodeError) as exc:
        return _fail(name, exc)
    if parsed.missing:
        form = " ".join(f"--map '<header>={f}'" for f in parsed.missing)
        headers_str = ", ".join(parsed.headers)
        return _fail(
            name,
            f"missing required {args.kind} column(s): {', '.join(parsed.missing)};"
            f" headers found: {headers_str};"
            f" name them with {form}",
        )
    as_of = as_of_arg or parsed.newest_date
    if args.apply:
        if as_of is None:
            return _fail(name, "no date found in the file; pass --as-of YYYY-MM-DD")
        conn = _open_existing(name, args.db)
        if isinstance(conn, int):
            return conn
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(custodian_snapshots)")}
        if not {"mapping", "account"} <= columns:
            conn.close()
            return _fail(name, "database schema is out of date (run `hub db migrate`)")
    report = [f"{args.kind} file: {args.file} (header on row {parsed.header_row + 1})"]
    report.extend(f"  {header} -> {field_name}" for header, field_name in parsed.mapping_report())
    report.append(f"rows: {len(parsed.rows)}")
    ref = custodian.source_ref(args.file)
    account = args.account.strip() if args.account else custodian.account_from_ref(ref)
    account = account or None
    report.append(f"account: {account or 'none (pass --account to label it)'}")
    if not args.apply:
        report.append(f"as_of: {as_of or 'not found (pass --as-of to apply)'}")
        report.append("dry run: nothing written (use --apply)")
        print("\n".join(report))
        return 0
    mapping_json = json.dumps(overrides, sort_keys=True) if overrides else None
    try:
        try:
            key = (args.custodian, args.kind, as_of, ref)
            snapshot_id, created = store.insert_custodian_snapshot(
                conn,
                custodian=key[0],
                kind=key[1],
                as_of=key[2],
                source_ref=key[3],
                raw=raw,
                mapping=mapping_json,
                account=account,
            )
            stored = None if created else store.get_custodian_snapshot_raw(conn, *key)
        finally:
            conn.close()
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    print("\n".join(report))
    if created:
        print(f"snapshot {snapshot_id} written: {args.custodian} {args.kind} as of {as_of}")
    else:
        note = "" if stored == raw else "; file content differs, left as is"
        print(
            f"no-op: snapshot {snapshot_id} already holds {args.custodian} {args.kind}"
            f" as of {as_of} from {ref}{note}"
        )
    return 0


def cmd_custodian_list(args: argparse.Namespace) -> int:
    name = "custodian list"
    try:
        conn = _open_existing(name, args.db)
        if isinstance(conn, int):
            return conn
        try:
            snaps = store.list_custodian_snapshots(conn)
        finally:
            conn.close()
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    headers = ["custodian", "account", "kind", "as_of", "imported_at", "rows"]
    rows = []
    for s in snaps:
        mapping_text = s.get("mapping")
        stored_overrides = None
        if mapping_text:
            try:
                stored_overrides = json.loads(mapping_text)
            except (json.JSONDecodeError, TypeError):
                stored_overrides = None
        row_count = custodian.count_rows(s["raw"], s["kind"], stored_overrides)
        account = s.get("account") or custodian.account_from_ref(s["source_ref"]) or "-"
        rows.append(
            [s["custodian"], account, s["kind"], s["as_of"], s["imported_at"], str(row_count)]
        )
    widths = [max([len(h), *(len(r[i]) for r in rows)]) for i, h in enumerate(headers)]
    for line in [headers, *rows]:
        print("  ".join(cell.ljust(w) for cell, w in zip(line, widths, strict=True)).rstrip())
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
        "preflight": (
            "check Investing Companion readiness without changing local state",
            cmd_ic_preflight,
        ),
        "show": ("show counts from the cached context pack", cmd_ic_show),
    },
    "doc": {
        "list": ("list documents with their latest revision", cmd_doc_list),
        "show": ("print one revision of a document", cmd_doc_show),
        "revise": ("append a session revision to a document", cmd_doc_revise),
    },
    "custodian": {
        "import": ("import a custodian positions or transactions CSV", cmd_custodian_import),
        "list": ("list imported custodian snapshots", cmd_custodian_list),
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


def _ic_show_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--counts",
        action="store_true",
        help="print section counts (default)",
    )


def _ic_preflight_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--out", type=Path, help="write the redacted report to this file")


# Full command name -> function adding that command's arguments to its parser.
ARGUMENTS: dict[str, Callable[[argparse.ArgumentParser], None]] = {
    "db init": _db_path_arg,
    "db migrate": _db_path_arg,
    "db backup": _backup_args,
    "db restore-check": _restore_check_args,
    "import claude-export": _import_args,
    "ic preflight": _ic_preflight_args,
    "ic show": _ic_show_args,
    "doc list": _doc_list_args,
    "doc show": _doc_show_args,
    "doc revise": _doc_revise_args,
    "custodian import": _custodian_import_args,
    "custodian list": _db_path_arg,
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
