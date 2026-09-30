"""SQLite knowledge store: connection, migrations, document revisions and backups."""

import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from importlib import resources
from pathlib import Path

# Defaults are relative to the current working directory, which is the repo root when the
# `hub` command is run from a checkout. Every command accepts an explicit path instead.
DEFAULT_DB_PATH = Path("data/hub.db")
DEFAULT_BACKUP_DIR = Path("backups")
BACKUP_KEEP = 7

SOURCE_KINDS = ("receipt", "digest", "session", "import")

_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_BACKUP_NAME = re.compile(r"^hub-\d{8}T\d{12}Z\.db$")
_BACKUP_TS_FORMAT = "%Y%m%dT%H%M%S%fZ"


class StoreError(Exception):
    """A store operation failed in a way the operator must see."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str


def connect(path: Path | str) -> sqlite3.Connection:
    """Open the database with WAL journaling and foreign keys enforced."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_migrations() -> list[Migration]:
    """Read the packaged `hub/migrations/NNNN_name.sql` files in version order."""
    found: dict[int, Migration] = {}
    for entry in resources.files("hub").joinpath("migrations").iterdir():
        match = _MIGRATION_NAME.match(entry.name)
        if match is None:
            continue
        version = int(match.group(1))
        if version in found:
            raise StoreError(f"duplicate migration version {version:04d}: {entry.name}")
        found[version] = Migration(version, entry.name, entry.read_text(encoding="utf-8"))
    return [found[v] for v in sorted(found)]


def applied_versions(conn: sqlite3.Connection) -> set[int]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version ("
        " version INTEGER PRIMARY KEY,"
        " name TEXT NOT NULL,"
        " applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))"
    )
    return {row[0] for row in conn.execute("SELECT version FROM schema_version")}


def migrate(conn: sqlite3.Connection, migrations: list[Migration] | None = None) -> list[str]:
    """Apply every migration not yet recorded in `schema_version`; return the names applied.

    Each migration and its `schema_version` row commit in one transaction, so a failing
    migration leaves the database at the previous version.
    """
    if migrations is None:
        migrations = load_migrations()
    done = applied_versions(conn)
    applied: list[str] = []
    for migration in migrations:
        if migration.version in done:
            continue
        try:
            conn.executescript(
                "BEGIN;\n"
                f"{migration.sql}\n;\n"
                "INSERT INTO schema_version (version, name) "
                f"VALUES ({migration.version}, '{migration.name}');\n"
                "COMMIT;"
            )
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise StoreError(f"migration {migration.name} failed: {exc}") from exc
        applied.append(migration.name)
    return applied


def insert_document_revision(
    conn: sqlite3.Connection,
    *,
    slug: str,
    kind: str,
    body: str,
    source_kind: str,
    source_ref: str,
    title: str | None = None,
) -> int:
    """Append a revision to the document `slug`, creating the document if needed.

    Returns the new revision number (1 for a new document).
    """
    if source_kind not in SOURCE_KINDS:
        raise StoreError(f"unknown source_kind {source_kind!r}; expected one of {SOURCE_KINDS}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute("SELECT id FROM documents WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            document_id = conn.execute(
                "INSERT INTO documents (slug, kind, title) VALUES (?, ?, ?)",
                (slug, kind, title),
            ).lastrowid
        else:
            document_id = row["id"]
        revision = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM document_revisions WHERE document_id = ?",
            (document_id,),
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO document_revisions"
            " (document_id, revision, body, source_kind, source_ref)"
            " VALUES (?, ?, ?, ?, ?)",
            (document_id, revision, body, source_kind, source_ref),
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return revision


def list_documents(conn: sqlite3.Connection) -> list[dict]:
    """Every document with its latest revision's number, source and timestamp, by slug."""
    rows = conn.execute(
        "SELECT d.slug, d.kind, d.title, r.revision, r.source_kind, r.source_ref,"
        " r.created_at AS revised_at"
        " FROM documents d"
        " JOIN document_revisions r ON r.document_id = d.id"
        " WHERE r.revision = (SELECT MAX(revision) FROM document_revisions"
        "                     WHERE document_id = d.id)"
        " ORDER BY d.slug"
    )
    return [dict(row) for row in rows]


def get_document_revision(conn: sqlite3.Connection, slug: str, revision: int | None = None) -> dict:
    """One revision of the document `slug` (the latest when `revision` is None).

    Raises StoreError for an unknown slug or an unknown revision number.
    """
    doc = conn.execute("SELECT id, kind, title FROM documents WHERE slug = ?", (slug,)).fetchone()
    if doc is None:
        raise StoreError(f"unknown document: {slug}")
    query = (
        "SELECT revision, body, source_kind, source_ref, created_at AS revised_at"
        " FROM document_revisions WHERE document_id = ?"
    )
    if revision is None:
        row = conn.execute(query + " ORDER BY revision DESC LIMIT 1", (doc["id"],)).fetchone()
    else:
        row = conn.execute(query + " AND revision = ?", (doc["id"], revision)).fetchone()
    if row is None:
        raise StoreError(f"unknown revision {revision} of document: {slug}")
    return {"slug": slug, "kind": doc["kind"], "title": doc["title"], **dict(row)}


def integrity_check(path: Path) -> str:
    """Run `PRAGMA integrity_check` on the database file at `path`; return its verdict."""
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
    finally:
        conn.close()
    return "\n".join(str(row[0]) for row in rows)


def _backup_path(backup_dir: Path, now: datetime) -> Path:
    # Microsecond UTC stamps sort lexically; on a collision step forward one microsecond.
    stamp = now.astimezone(UTC)
    while True:
        candidate = backup_dir / f"hub-{stamp.strftime(_BACKUP_TS_FORMAT)}.db"
        partial = candidate.with_name(candidate.name + ".partial")
        if not candidate.exists() and not partial.exists():
            return candidate
        stamp += timedelta(microseconds=1)


def list_backups(backup_dir: Path) -> list[Path]:
    """Backups written by `backup`, oldest first. Other files in the directory are ignored."""
    if not backup_dir.is_dir():
        return []
    return sorted(p for p in backup_dir.iterdir() if _BACKUP_NAME.match(p.name))


def prune_backups(backup_dir: Path, keep: int = BACKUP_KEEP) -> list[Path]:
    """Delete all but the newest `keep` backups; return the paths removed."""
    backups = list_backups(backup_dir)
    doomed = backups[:-keep] if keep > 0 else backups
    for path in doomed:
        path.unlink()
    return doomed


@dataclass(frozen=True)
class BackupResult:
    path: Path
    pruned: list[Path]


def backup(
    db_path: Path | str,
    backup_dir: Path | str = DEFAULT_BACKUP_DIR,
    *,
    keep: int = BACKUP_KEEP,
    now: datetime | None = None,
) -> BackupResult:
    """Copy the live database with the online backup API, verify the copy, then prune.

    The copy is written as `<name>.partial`, checked with `PRAGMA integrity_check`, and only
    renamed to its final `hub-<UTC timestamp>.db` name when the check returns `ok`. If the
    copy or the check fails, the `.partial` file is deleted, the error is raised (a failed check
    as a one-line `StoreError`) and nothing is pruned.
    """
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    if not db_path.is_file():
        raise StoreError(f"database not found: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = _backup_path(backup_dir, now or datetime.now(UTC))
    partial = final.with_name(final.name + ".partial")

    try:
        source = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            dest = sqlite3.connect(partial)
            try:
                source.backup(dest)
                # A standalone file: no -wal/-shm companions next to the copy.
                dest.execute("PRAGMA journal_mode = DELETE")
            finally:
                dest.close()
        finally:
            source.close()

        verdict = integrity_check(partial)
        if verdict != "ok":
            lines = verdict.splitlines() or [verdict]
            more = f" (+{len(lines) - 1} more)" if len(lines) > 1 else ""
            raise StoreError(f"backup failed integrity_check: {lines[0]}{more}")
        partial.rename(final)
    except BaseException:
        # Never leave a failed copy behind: repeated failures would accumulate partials.
        partial.unlink(missing_ok=True)
        raise
    return BackupResult(final, prune_backups(backup_dir, keep))
