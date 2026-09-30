"""Restore check: judge whether a backup file could replace the live store."""

import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

_REVISIONS_TABLE = "document_revisions"
_NEWEST_REVISION_SQL = f"SELECT MAX(created_at) FROM {_REVISIONS_TABLE}"


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _open_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def _table_names(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
    return {row[0] for row in rows}


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    quoted = '"' + table.replace('"', '""') + '"'
    return conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]


def _newest_revision(conn: sqlite3.Connection, tables: set[str]) -> str | None:
    if _REVISIONS_TABLE not in tables:
        return None
    return conn.execute(_NEWEST_REVISION_SQL).fetchone()[0]


def _compare_counts(table: str, backup_rows: int, live_rows: int) -> Check:
    name = f"rows: {table}"
    detail = f"backup {backup_rows}, live {live_rows}"
    if backup_rows > live_rows:
        return Check(name, FAIL, f"{detail} (backup has more rows than the live store)")
    if backup_rows < live_rows:
        return Check(name, WARN, f"{detail} (backup lags the live store)")
    return Check(name, PASS, detail)


def _compare_newest(backup_newest: str | None, live_newest: str | None) -> Check:
    name = f"newest {_REVISIONS_TABLE}.created_at"
    detail = f"backup {backup_newest}, live {live_newest}"
    if backup_newest is not None and (live_newest is None or backup_newest > live_newest):
        return Check(name, FAIL, f"{detail} (backup is newer than the live store)")
    if backup_newest != live_newest:
        return Check(name, WARN, f"{detail} (backup lags the live store)")
    return Check(name, PASS, detail)


def restore_check(backup: Path, live: Path) -> list[Check]:
    """Verify `backup` against `live` without modifying either.

    The backup is copied to a temporary directory and every check runs on the copy, opened
    read-only; the live database is opened read-only too. A backup that lags the live store
    (fewer rows, older newest revision) is WARN, which is not a failure; FAIL is an integrity
    or read error, a missing or extra table, or a backup holding more than the live store.
    """
    with tempfile.TemporaryDirectory(prefix="hub-restore-check-") as tmp:
        copy = Path(tmp) / "backup.db"
        shutil.copyfile(backup, copy)
        checks: list[Check] = []
        try:
            backup_conn = _open_readonly(copy)
            try:
                verdict = [str(r[0]) for r in backup_conn.execute("PRAGMA integrity_check")]
                if verdict == ["ok"]:
                    checks.append(Check("integrity_check", PASS, "ok"))
                else:
                    more = f" (+{len(verdict) - 1} more)" if len(verdict) > 1 else ""
                    checks.append(Check("integrity_check", FAIL, f"{verdict[0]}{more}"))
                backup_tables = _table_names(backup_conn)
                backup_counts = {t: _row_count(backup_conn, t) for t in backup_tables}
                backup_newest = _newest_revision(backup_conn, backup_tables)
            finally:
                backup_conn.close()
        except sqlite3.DatabaseError as exc:
            checks.append(Check("integrity_check", FAIL, f"backup unreadable: {exc}"))
            return checks

        live_conn = _open_readonly(live)
        try:
            live_tables = _table_names(live_conn)
            live_counts = {t: _row_count(live_conn, t) for t in live_tables}
            live_newest = _newest_revision(live_conn, live_tables)
        finally:
            live_conn.close()

    missing = sorted(live_tables - backup_tables)
    extra = sorted(backup_tables - live_tables)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing from backup: {', '.join(missing)}")
        if extra:
            parts.append(f"not in live store: {', '.join(extra)}")
        checks.append(Check("tables", FAIL, "; ".join(parts)))
    else:
        checks.append(Check("tables", PASS, f"{len(live_tables)} tables match"))
    for table in sorted(live_tables & backup_tables):
        checks.append(_compare_counts(table, backup_counts[table], live_counts[table]))
    if _REVISIONS_TABLE in live_tables and _REVISIONS_TABLE in backup_tables:
        checks.append(_compare_newest(backup_newest, live_newest))
    return checks


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{'CHECK':<{width}}  RESULT  DETAIL"]
    lines += [f"{c.name:<{width}}  {c.status:<6}  {c.detail}" for c in checks]
    return "\n".join(lines)
