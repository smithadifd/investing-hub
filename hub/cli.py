"""Command-line entry point."""

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Sequence
from datetime import date as _date
from pathlib import Path

from hub import (
    custodian,
    ic,
    ic_cli,
    importer,
    letter,
    preflight,
    producers,
    pulse,
    restore,
    session_open,
    store,
)
from hub.producers.common import truncate


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


def _producers_list_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument(
        "--mv-analyst-root",
        type=Path,
        default=producers.DEFAULT_MV_ANALYST_ROOT,
        help="mv-analyst home directory (default: %(default)s)",
    )
    parser.add_argument(
        "--week-ahead-root",
        type=Path,
        default=producers.DEFAULT_WEEK_AHEAD_ROOT,
        help="week-ahead home directory (default: %(default)s)",
    )
    parser.add_argument(
        "--triage-queue-dir",
        type=Path,
        default=producers.DEFAULT_TRIAGE_QUEUE_DIR,
        help="morning-brief investing-triage-queue directory (default: %(default)s)",
    )


def cmd_producers_list(args: argparse.Namespace) -> int:
    """List the candidates the producers yield, advancing the hub store's read cursor."""
    name = "producers list"
    try:
        conn = _open_existing(name, args.db)
        if isinstance(conn, int):
            return conn
        try:
            reports = producers.status_reports(
                mv_analyst_root=args.mv_analyst_root,
                week_ahead_root=args.week_ahead_root,
                triage_queue_dir=args.triage_queue_dir,
                conn=conn,
            )
        finally:
            conn.close()
    except (store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)
    for report in reports:
        notes = f" — {truncate('; '.join(report.notes))}" if report.notes else ""
        print(f"{report.producer}: {report.status} — {report.count} candidate(s){notes}")
        for candidate in report.candidates:
            print(f"  {candidate.kind} @ {candidate.as_of}")
            print(f"    {candidate.summary}")
            print(f"    {candidate.source_path}")
    return 0


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


def _pulse_write_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="pulse date (default: today UTC)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=pulse.DEFAULT_OUT_DIR,
        help="output directory (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help=f"model the drafter should use (default: {pulse.DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--ask-threshold",
        type=float,
        default=None,
        metavar="F",
        help=f"score above which an item crosses the high Herald ask bar"
        f" (default: {pulse.DEFAULT_ASK_THRESHOLD})",
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=None,
        metavar="F",
        help=f"score below which an item is ignored (default: {pulse.DEFAULT_MIN_SCORE})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the Stage 0 evidence and the deterministic skeleton"
        " instead of writing the file",
    )


def _pulse_provenance(result: pulse.PulseResult, model: str) -> str:
    return (
        f"pulse: model={model}"
        f" date={result.date.isoformat()}"
        f" high_bar={len(result.high_bar)}"
        f" worth_discussing={len(result.worth_discussing)}"
        f" quiet={'yes' if result.quiet else 'no'}"
    )


def _print_dry_run(result: pulse.PulseResult, model: str) -> None:
    print("Stage 0 evidence:")
    if result.high_bar:
        print("  high-bar items:")
        for item in result.high_bar:
            print(f"    - {item.subject} ({item.detail}; score {item.score:.2f})")
    if result.worth_discussing:
        print("  worth-discussing items:")
        for item in result.worth_discussing:
            print(f"    - {item.subject} ({item.detail}; score {item.score:.2f})")
    if not result.high_bar and not result.worth_discussing:
        print("  (no qualifying items)")
    print("Deterministic draft:")
    print(pulse.compose_deterministic(result))


def cmd_pulse_write(args: argparse.Namespace) -> int:
    name = "pulse write"
    if args.date is None:
        on = pulse.today_utc()
    else:
        try:
            on = _date.fromisoformat(args.date)
        except ValueError:
            return _fail(name, f"invalid --date {args.date!r} (expected YYYY-MM-DD)")
    if args.db.is_dir():
        return _fail(name, f"database path is a directory: {args.db}")
    if not args.db.is_file():
        return _fail(name, f"database not found: {args.db} (run `hub db init`)")
    ask_threshold = (
        args.ask_threshold if args.ask_threshold is not None else pulse.DEFAULT_ASK_THRESHOLD
    )
    min_score = args.min_score if args.min_score is not None else pulse.DEFAULT_MIN_SCORE
    model = args.model if args.model is not None else pulse.DEFAULT_MODEL
    try:
        conn = store.connect(args.db)
        try:
            result = pulse.collect_pulse(
                conn, on=on, ask_threshold=ask_threshold, min_score=min_score
            )
        finally:
            conn.close()
    except (pulse.PulseError, store.StoreError, sqlite3.Error, OSError) as exc:
        return _fail(name, exc)

    print(_pulse_provenance(result, model))

    if args.dry_run:
        _print_dry_run(result, model)
        return 0

    try:
        out_path = pulse.write_pulse(result, out_dir=args.out_dir, model=model)
    except (pulse.PulseError, OSError) as exc:
        return _fail(name, exc)
    print(f"pulse written: {out_path}")
    return 0


def _letter_midweek_args(parser: argparse.ArgumentParser) -> None:
    _db_path_arg(parser)
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="letter issue date (default: today UTC)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=letter.DEFAULT_OUT_DIR,
        help="output directory (default: %(default)s)",
    )
    parser.add_argument(
        "--model",
        default=None,
        metavar="MODEL",
        help=(
            "draft a 1-4 word summary paragraph with this model"
            " (default: no prose, mechanical letter only)"
        ),
    )
    parser.add_argument(
        "--memory-path",
        type=Path,
        default=letter.DEFAULT_OUT_DIR / "memory.jsonl",
        help="repeat-suppression memory file (default: %(default)s)",
    )
    parser.add_argument(
        "--mv-analyst-root",
        type=Path,
        default=producers.DEFAULT_MV_ANALYST_ROOT,
        help="mv-analyst home directory (default: %(default)s)",
    )
    parser.add_argument(
        "--scan-file",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "rotation scan source as a JSON file (benchmark, asof, rows;"
            " see README); unset means the market leg is off"
        ),
    )
    parser.add_argument(
        "--deliver",
        action="store_true",
        help="hand the written letter to HUB_LETTER_SEND_CMD",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="allow a no-scan rerun to replace a scan-backed letter",
    )


def cmd_letter_midweek(args: argparse.Namespace) -> int:
    """Run the midweek letter on the mechanical merit gate.

    Quiet days print one combined status line and write no file. Active
    days compose the deterministic visual and (with ``--model``) a short
    drafter summary, then write the file at ``<out-dir>/<date>-midweek.md``.
    The market leg runs only when ``--scan-file`` names a real scan source;
    without it the leg is off and the output says so. A no-scan rerun refuses
    to replace a scan-backed letter unless ``--force`` is set. ``--deliver``
    claims the issue date before rendering, hands the file to
    ``$HUB_LETTER_SEND_CMD``, and records the delivered findings before the
    delivery marker. An interrupted delivery leaves a claim file and needs a
    manual inbox check before retrying; it never resends on its own.
    """
    name = "letter midweek"
    if args.date is None:
        on = pulse.today_utc()
    else:
        try:
            on = _date.fromisoformat(args.date)
        except ValueError:
            return _fail(name, f"invalid --date {args.date!r} (expected YYYY-MM-DD)")
    asof = on.isoformat()
    out_path = args.out_dir / f"{asof}-midweek.md"
    delivery_marker = out_path.with_suffix(".delivered")
    delivery_claim = out_path.with_suffix(".delivering")
    delivered_records_path = out_path.with_suffix(".delivered.jsonl")
    claim_taken = False
    keep_claim = False

    def finish(rc: int) -> int:
        nonlocal claim_taken
        if not claim_taken or keep_claim:
            return rc
        try:
            delivery_claim.unlink()
        except OSError as exc:
            return _fail(name, f"could not release delivery claim {delivery_claim}: {exc}")
        claim_taken = False
        return rc

    if args.deliver:
        try:
            args.out_dir.mkdir(parents=True, exist_ok=True)
            claim_fd = os.open(delivery_claim, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return _fail(
                name,
                f"{delivery_claim}: delivery for {asof} is in progress or was interrupted "
                "(remove the claim after checking the inbox to retry)",
            )
        except OSError as exc:
            return _fail(name, exc)
        os.close(claim_fd)
        claim_taken = True

        if delivery_marker.is_file():
            try:
                if not delivered_records_path.is_file():
                    raise letter.LetterError(
                        f"delivered findings sidecar {delivered_records_path} is missing or unreadable"
                    )
                try:
                    delivered_records = letter.load_memory(delivered_records_path)
                except letter.MemoryMalformed as exc:
                    raise letter.LetterError(
                        f"delivered findings sidecar {delivered_records_path} is missing or unreadable"
                    ) from exc
                if not delivered_records or any(row.date != asof for row in delivered_records):
                    raise letter.LetterError(
                        f"delivered findings sidecar {delivered_records_path} is missing or unreadable"
                    )
                current_memory = letter.load_memory(args.memory_path)
                letter.append_memory(
                    args.memory_path,
                    letter.records_to_append(current_memory, delivered_records, asof=asof),
                )
            except (letter.LetterError, OSError) as exc:
                return finish(_fail(name, exc))
            print(f"letter already delivered: {asof}")
            return finish(0)

    if args.scan_file is None and out_path.is_file() and not args.force:
        try:
            existing = out_path.read_text(encoding="utf-8")
        except OSError as exc:
            return finish(_fail(name, exc))
        if "Market leg off: no scan source configured" not in existing:
            return finish(
                _fail(
                    name,
                    f"refusing to overwrite scan-backed letter for {asof} without"
                    " --scan-file; pass --force to replace it",
                )
            )
    if not args.db.is_file():
        return finish(_fail(name, f"database not found: {args.db} (run `hub db init`)"))

    try:
        conn = store.connect(args.db)
        try:
            docs = store.list_documents(conn)
            doc_bodies: dict[str, str] = {}
            for doc in docs:
                if doc["kind"] != "thesis":
                    continue
                body = store.get_document_revision(conn, doc["slug"])["body"]
                doc_bodies[doc["slug"]] = body
            views = letter.build_views_from_documents(
                conn, body_for=lambda slug: doc_bodies.get(slug, "")
            )
        finally:
            conn.close()
    except (letter.LetterError, store.StoreError, sqlite3.Error, OSError) as exc:
        return finish(_fail(name, exc))

    if args.scan_file is None:
        scan = None
        scan_note = "market leg off: no scan source configured"
    else:
        try:
            scan = letter.load_scan_file(args.scan_file)
        except letter.LetterError as exc:
            return finish(_fail(name, exc))
        scan_note = f"market leg on (scan file: {args.scan_file})"

    mv_candidates = producers.mv_analyst_candidates(args.mv_analyst_root).candidates
    corpus = letter.build_corpus_from_mv_analyst_candidates(mv_candidates)

    try:
        memory = letter.load_memory(args.memory_path)
    except letter.MemoryMalformed as exc:
        return finish(_fail(name, exc))

    result = letter.collect_letter(on=on, views=views, scan=scan, corpus=corpus, memory=memory)
    records = [
        letter.MemoryRecord(
            date=asof,
            view_id=f["view_id"],
            source=f["source"],
            key=f["key"],
            verdict=f["verdict"],
            trigger=f.get("trigger", ""),
            summary=f.get("summary", ""),
        )
        for f in result.findings
    ]

    provenance = _letter_provenance(result, model=args.model, scan_note=scan_note)
    if result.quiet:
        # One combined status line, exit 0, no file.
        print(f"{provenance} — {letter.compose_letter(result, model=None)}")
        return finish(0)
    print(provenance)

    try:
        out_path = letter.write_letter(result, out_dir=args.out_dir, model=args.model)
    except (letter.LetterError, OSError) as exc:
        return finish(_fail(name, exc))
    print(f"letter written: {out_path}")

    if args.deliver:
        subject = f"Investing Hub Midweek Letter — {asof}"
        try:
            receipt = letter.default_sender(out_path, subject=subject)
        except (letter.LetterError, OSError) as exc:
            return finish(_fail(name, exc))
        keep_claim = True
        try:
            letter.write_text_atomic(
                delivered_records_path,
                letter.memory_records_text(records),
            )
            letter.write_text_atomic(delivery_marker, f"{asof}\n")
        except (letter.LetterError, OSError) as exc:
            return finish(_fail(name, exc))
        keep_claim = False
        print(f"letter delivered: {receipt}")

    asks = letter.ask_candidates_for(result)
    if asks:
        sidecar = out_path.with_suffix(".asks.json")
        try:
            letter.write_text_atomic(
                sidecar,
                json.dumps(
                    {
                        "date": asof,
                        "candidates": asks,
                        "note": (
                            "Ask candidates mapped from this letter's"
                            " findings; minting a Herald ask is a separate,"
                            " supervised step."
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
            )
            print(f"ask candidates: {sidecar}")
        except OSError as exc:
            print(f"hub {name}: warn: could not write ask sidecar: {exc}", file=sys.stderr)

    # Memory is recorded only after a successful delivery (and after the
    # letter write when --deliver is not set), so a failed send can be
    # retried without the findings going quiet on their own record.
    try:
        letter.append_memory(args.memory_path, letter.records_to_append(memory, records, asof=asof))
    except (letter.LetterError, OSError) as exc:
        return finish(_fail(name, exc))

    return finish(0)


def _letter_provenance(result: letter.LetterResult, *, model: str | None, scan_note: str) -> str:
    return (
        f"letter: model={model or 'none'}"
        f" date={result.date.isoformat()}"
        f" findings={len(result.findings)}"
        f" suppressed={len(result.suppressed)}"
        f" quiet={'yes' if result.quiet else 'no'}"
        f"; {scan_note}"
    )


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
        **ic_cli.COMMANDS,
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
    "producers": {
        "list": (
            "list stage-0 candidates from mv-analyst, week-ahead and the triage queue",
            cmd_producers_list,
        ),
    },
    "pulse": {
        "write": ("draft today's book section into out/book/<date>.md", cmd_pulse_write),
    },
    "letter": {
        "midweek": (
            "write the midweek letter on the mechanical merit gate",
            cmd_letter_midweek,
        ),
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
    "producers list": _producers_list_args,
    "pulse write": _pulse_write_args,
    "letter midweek": _letter_midweek_args,
    "session-open": _session_open_args,
    **ic_cli.ARGUMENTS,
}


def _command_listing() -> str:
    lines = ["full command surface:"]
    for group, subs in COMMANDS.items():
        for name, (help_text, _) in subs.items():
            full = name if group is None else f"{group} {name}"
            lines.append(f"  hub {full:<26} {help_text}")
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
        nested: dict[str, argparse._SubParsersAction] = {}
        for name, (help_text, handler) in subs.items():
            first, _, verb = name.partition(" ")
            if verb:  # "alert add" -> `hub ic alert add`
                if first not in nested:
                    middle = group_subs.add_parser(first, help=f"{group} {first} commands")
                    nested[first] = middle.add_subparsers(dest="verb", metavar="<verb>")
                    nested[first].required = True
                sub = nested[first].add_parser(verb, help=help_text)
            else:
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
