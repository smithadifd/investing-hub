import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from hub import store
from hub.cli import main

ROADMAP_TABLES = {
    "documents",
    "document_revisions",
    "custodian_snapshots",
    "findings",
    "asks",
    "briefs",
    "handoffs",
    "decisions",
    "calls",
    "beats_proposals",
    "producer_cursors",
    "ic_writes",
}
T0 = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "data" / "hub.db"


@pytest.fixture
def conn(db_path):
    connection = store.connect(db_path)
    store.migrate(connection)
    yield connection
    connection.close()


def _tables(connection):
    rows = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    return {row[0] for row in rows}


def _seed(connection):
    store.insert_document_revision(
        connection,
        slug="example-thesis",
        kind="thesis",
        body="Placeholder thesis text.",
        source_kind="import",
        source_ref="knowledge/example.md",
    )


# --- connection and migrations -------------------------------------------------------------


def test_connect_uses_wal_and_foreign_keys(db_path):
    connection = store.connect(db_path)
    try:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        connection.close()


def test_packaged_migrations_are_found_in_order():
    migrations = store.load_migrations()
    assert migrations, "no migrations found in hub/migrations"
    assert migrations[0].name == "0001_initial.sql"
    versions = [m.version for m in migrations]
    assert versions == sorted(versions) == list(range(1, len(versions) + 1))


def test_migrate_creates_the_roadmap_tables(conn):
    assert _tables(conn) == ROADMAP_TABLES | {"schema_version"}
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(document_revisions)")}
    assert {"source_kind", "source_ref"} <= columns
    recorded = [row[0] for row in conn.execute("SELECT name FROM schema_version")]
    assert recorded == [m.name for m in store.load_migrations()]


def test_second_migrate_is_a_no_op(conn):
    before = conn.execute("SELECT version, applied_at FROM schema_version").fetchall()
    assert store.migrate(conn) == []
    after = conn.execute("SELECT version, applied_at FROM schema_version").fetchall()
    assert [tuple(r) for r in after] == [tuple(r) for r in before]


def test_only_pending_migrations_are_applied(conn):
    extra = store.Migration(99, "0099_extra.sql", "CREATE TABLE extra (id INTEGER PRIMARY KEY);")
    migrations = store.load_migrations() + [extra]
    assert store.migrate(conn, migrations) == ["0099_extra.sql"]
    assert "extra" in _tables(conn)
    assert store.migrate(conn, migrations) == []


def test_failing_migration_rolls_back_and_is_not_recorded(conn):
    bad = store.Migration(
        98,
        "0098_bad.sql",
        "CREATE TABLE half_done (id INTEGER);\nINSERT INTO no_such_table VALUES (1);",
    )
    with pytest.raises(store.StoreError, match="0098_bad.sql"):
        store.migrate(conn, [bad])
    assert "half_done" not in _tables(conn)
    assert 98 not in store.applied_versions(conn)


# --- documents -----------------------------------------------------------------------------


def test_revisions_number_from_one_and_list_shows_latest(conn):
    first = store.insert_document_revision(
        conn,
        slug="principles",
        kind="principles",
        title="Principles",
        body="Placeholder v1.",
        source_kind="import",
        source_ref="instructions.md",
    )
    second = store.insert_document_revision(
        conn,
        slug="principles",
        kind="principles",
        body="Placeholder v2.",
        source_kind="session",
        source_ref="session-0001",
    )
    _seed(conn)
    assert (first, second) == (1, 2)
    docs = store.list_documents(conn)
    assert [d["slug"] for d in docs] == ["example-thesis", "principles"]
    latest = docs[1]
    assert latest["revision"] == 2
    assert latest["source_kind"] == "session"
    assert latest["source_ref"] == "session-0001"
    assert latest["title"] == "Principles"
    assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2


def test_unknown_source_kind_is_rejected(conn):
    with pytest.raises(store.StoreError, match="source_kind"):
        store.insert_document_revision(
            conn, slug="x", kind="thesis", body="b", source_kind="guess", source_ref="r"
        )
    assert store.list_documents(conn) == []


def test_schema_rejects_unknown_source_kind_directly(conn):
    conn.execute("INSERT INTO documents (slug, kind) VALUES ('x', 'thesis')")
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        conn.execute(
            "INSERT INTO document_revisions (document_id, revision, body, source_kind, source_ref)"
            " VALUES (1, 1, 'b', 'guess', 'r')"
        )


def test_foreign_keys_are_enforced(conn):
    with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
        conn.execute(
            "INSERT INTO document_revisions (document_id, revision, body, source_kind, source_ref)"
            " VALUES (999, 1, 'b', 'import', 'r')"
        )


# --- backups -------------------------------------------------------------------------------


def test_backup_writes_a_verified_standalone_copy(conn, db_path, tmp_path):
    _seed(conn)
    backups = tmp_path / "backups"
    result = store.backup(db_path, backups, now=T0)
    assert result.path == backups / "hub-20260102T030405678901Z.db"
    assert result.pruned == []
    assert sorted(p.name for p in backups.iterdir()) == [result.path.name]
    assert store.integrity_check(result.path) == "ok"
    copy = sqlite3.connect(result.path)
    try:
        assert copy.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert copy.execute("SELECT slug FROM documents").fetchall() == [("example-thesis",)]
    finally:
        copy.close()


def test_backup_names_sort_and_never_collide(conn, db_path, tmp_path):
    backups = tmp_path / "backups"
    first = store.backup(db_path, backups, now=T0).path
    second = store.backup(db_path, backups, now=T0).path
    third = store.backup(db_path, backups, now=T0 + timedelta(seconds=1)).path
    assert len({first, second, third}) == 3
    assert store.list_backups(backups) == [first, second, third]


def test_backup_retention_keeps_newest_seven(conn, db_path, tmp_path):
    backups = tmp_path / "backups"
    unrelated = backups / "notes.txt"
    backups.mkdir()
    unrelated.write_text("not a backup")
    written = [store.backup(db_path, backups, now=T0 + timedelta(days=day)) for day in range(8)]
    assert [r.pruned for r in written[:7]] == [[]] * 7
    assert written[7].pruned == [written[0].path]
    remaining = store.list_backups(backups)
    assert remaining == [r.path for r in written[1:]]
    assert len(remaining) == 7
    assert unrelated.exists()


def test_backup_retention_counts_days_not_copies(conn, db_path, tmp_path):
    backups = tmp_path / "backups"
    daily = [store.backup(db_path, backups, now=T0 + timedelta(days=day)).path for day in range(6)]
    last_day = T0 + timedelta(days=6)
    same_day = [
        store.backup(db_path, backups, now=last_day + timedelta(hours=h)).path for h in range(4)
    ]
    assert store.list_backups(backups) == daily + same_day


def test_backup_retention_trims_an_older_day_to_its_newest_copy(conn, db_path, tmp_path):
    backups = tmp_path / "backups"
    morning = store.backup(db_path, backups, now=T0).path
    evening = store.backup(db_path, backups, now=T0 + timedelta(hours=10)).path
    result = store.backup(db_path, backups, now=T0 + timedelta(days=1))
    assert result.pruned == [morning]
    assert store.list_backups(backups) == [evening, result.path]


def test_backup_retention_drops_days_beyond_the_window(conn, db_path, tmp_path):
    backups = tmp_path / "backups"
    first_day = [store.backup(db_path, backups, now=T0 + timedelta(hours=h)).path for h in range(2)]
    for day in range(1, 8):
        store.backup(db_path, backups, now=T0 + timedelta(days=day))
    remaining = store.list_backups(backups)
    assert len(remaining) == 7
    assert not set(first_day) & set(remaining)


def test_integrity_check_runs_on_the_copy(conn, db_path, tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    checked = []
    real_check = store.integrity_check

    def spy(path):
        checked.append(path)
        return real_check(path)

    monkeypatch.setattr(store, "integrity_check", spy)
    result = store.backup(db_path, backups, now=T0)
    assert checked == [backups / (result.path.name + ".partial")]
    assert checked[0] != db_path


def test_failed_integrity_check_fails_loudly_and_prunes_nothing(
    conn, db_path, tmp_path, monkeypatch
):
    backups = tmp_path / "backups"
    kept = [store.backup(db_path, backups, now=T0 + timedelta(days=d)).path for d in range(7)]
    monkeypatch.setattr(store, "integrity_check", lambda path: "*** in database main ***")
    with pytest.raises(store.StoreError, match="backup failed integrity_check"):
        store.backup(db_path, backups, now=T0 + timedelta(days=30))
    assert store.list_backups(backups) == kept
    assert list(backups.glob("*.partial")) == []


def _corrupt_index_db(path):
    """A database whose index disagrees with its table: readable, copyable, but not `ok`."""
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE t (k TEXT)")
        connection.execute("CREATE INDEX t_k ON t (k)")
        connection.executemany("INSERT INTO t VALUES (?)", [(f"key-{i:04d}",) for i in range(40)])
        connection.commit()
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        root = connection.execute(
            "SELECT rootpage FROM sqlite_master WHERE name = 't_k'"
        ).fetchone()
    finally:
        connection.close()
    data = bytearray(path.read_bytes())
    start = (root[0] - 1) * page_size
    page = bytes(data[start : start + page_size])
    assert page.count(b"key-0001") == 1
    data[start : start + page_size] = page.replace(b"key-0001", b"key-9999")
    path.write_bytes(bytes(data))


def test_real_corruption_fails_the_check_and_leaves_nothing(tmp_path):
    db = tmp_path / "corrupt.db"
    _corrupt_index_db(db)
    first, *rest = store.integrity_check(db).splitlines()
    assert first != "ok"
    backups = tmp_path / "backups"
    with pytest.raises(store.StoreError) as raised:
        store.backup(db, backups, now=T0)
    message = str(raised.value)
    assert message == f"backup failed integrity_check: {first} (+{len(rest)} more)"
    assert rest
    assert "\n" not in message
    assert list(backups.iterdir()) == []


class _FailingCopy:
    """Wraps a connection so the online-backup copy writes pages and then fails."""

    def __init__(self, connection):
        self._connection = connection

    def backup(self, target):
        self._connection.backup(target._connection)
        raise sqlite3.DatabaseError("database disk image is malformed")

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_failed_copy_removes_the_partial(conn, db_path, tmp_path, monkeypatch):
    _seed(conn)
    backups = tmp_path / "backups"
    kept = store.backup(db_path, backups, now=T0).path
    real_connect = sqlite3.connect
    written = []

    def connect(target, *args, **kwargs):
        if not str(target).startswith("file:"):
            written.append(target)
        return _FailingCopy(real_connect(target, *args, **kwargs))

    monkeypatch.setattr(store.sqlite3, "connect", connect)
    with pytest.raises(sqlite3.DatabaseError, match="malformed"):
        store.backup(db_path, backups, now=T0 + timedelta(days=1))
    monkeypatch.undo()
    assert written == [backups / "hub-20260103T030405678901Z.db.partial"]
    assert sorted(backups.iterdir()) == [kept]


def test_backup_of_missing_database_fails(tmp_path):
    with pytest.raises(store.StoreError, match="database not found"):
        store.backup(tmp_path / "absent.db", tmp_path / "backups")
    assert not (tmp_path / "absent.db").exists()


# --- command line --------------------------------------------------------------------------


def test_cli_init_then_backup_leaves_a_checked_copy(tmp_path, capsys):
    db = tmp_path / "data" / "hub.db"
    backups = tmp_path / "backups"
    assert main(["db", "init", "--db", str(db)]) == 0
    assert "applied 0001_initial.sql" in capsys.readouterr().out
    assert main(["db", "backup", "--db", str(db), "--backup-dir", str(backups)]) == 0
    out = capsys.readouterr().out
    assert "integrity_check ok" in out
    (copy,) = store.list_backups(backups)
    assert str(copy) in out
    connection = sqlite3.connect(copy)
    try:
        assert _tables(connection) == ROADMAP_TABLES | {"schema_version"}
    finally:
        connection.close()


def test_cli_defaults_are_relative_to_the_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["db", "init"]) == 0
    assert main(["db", "backup"]) == 0
    assert (tmp_path / "data" / "hub.db").is_file()
    assert len(store.list_backups(tmp_path / "backups")) == 1


def test_cli_migrate_twice_reports_up_to_date(tmp_path, capsys):
    db = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["db", "migrate", "--db", str(db)]) == 0
    assert "schema up to date" in capsys.readouterr().out


def test_cli_migrate_without_database_fails(tmp_path, capsys):
    db = tmp_path / "hub.db"
    assert main(["db", "migrate", "--db", str(db)]) == 1
    assert "hub db init" in capsys.readouterr().err
    assert not db.exists()


def test_cli_integrity_failure_prints_one_line(tmp_path, capsys):
    db = tmp_path / "corrupt.db"
    _corrupt_index_db(db)
    backups = tmp_path / "b"
    assert main(["db", "backup", "--db", str(db), "--backup-dir", str(backups)]) == 1
    err = capsys.readouterr().err
    assert err.startswith("hub db backup: backup failed integrity_check: ")
    assert err.count("\n") == 1
    assert list(backups.iterdir()) == []


def test_cli_init_filesystem_error_exits_1(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a regular file where the data directory should be")
    assert main(["db", "init", "--db", str(blocker / "hub.db")]) == 1
    err = capsys.readouterr().err
    assert err.startswith("hub db init: ")
    assert err.count("\n") == 1


def test_cli_backup_failure_exits_1(tmp_path, capsys):
    db = tmp_path / "hub.db"
    assert main(["db", "backup", "--db", str(db), "--backup-dir", str(tmp_path / "b")]) == 1
    assert "database not found" in capsys.readouterr().err


ODD_DIR = "odd dir #1 ?x"


def test_backup_and_integrity_check_work_in_a_path_with_space_hash_and_question_mark(
    tmp_path, capsys
):
    db = tmp_path / ODD_DIR / "data" / "hub.db"
    backups = tmp_path / ODD_DIR / "backups"
    assert main(["db", "init", "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["db", "backup", "--db", str(db), "--backup-dir", str(backups)]) == 0
    assert "integrity_check ok" in capsys.readouterr().out
    (copy,) = store.list_backups(backups)
    assert store.integrity_check(copy) == "ok"
    assert [p.name for p in tmp_path.iterdir()] == [ODD_DIR]
    connection = sqlite3.connect(copy)
    try:
        assert _tables(connection) == ROADMAP_TABLES | {"schema_version"}
    finally:
        connection.close()


def test_integrity_check_reads_a_path_with_space_hash_and_question_mark(tmp_path):
    db = tmp_path / ODD_DIR / "hub.db"
    connection = store.connect(db)
    store.migrate(connection)
    connection.close()
    assert store.integrity_check(db) == "ok"
    # A misparsed URI would quietly create an empty file under a truncated name.
    assert [p.name for p in tmp_path.iterdir()] == [ODD_DIR]


def test_backup_reads_its_source_from_a_path_with_space_hash_and_question_mark(tmp_path):
    db = tmp_path / ODD_DIR / "hub.db"
    connection = store.connect(db)
    store.migrate(connection)
    connection.close()
    result = store.backup(db, tmp_path / "backups")
    assert store.integrity_check(result.path) == "ok"
    copy = sqlite3.connect(result.path)
    try:
        assert _tables(copy) == ROADMAP_TABLES | {"schema_version"}
    finally:
        copy.close()
    assert [p.name for p in tmp_path.iterdir() if p.name != "backups"] == [ODD_DIR]
