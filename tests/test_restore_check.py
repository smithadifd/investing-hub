import hashlib
import shutil
import sqlite3
import tempfile
from datetime import UTC, datetime

import pytest

from hub import store
from hub.cli import main

T0 = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)


def _add(conn, n):
    for i in range(n):
        store.insert_document_revision(
            conn,
            slug=f"doc-{i % 3}",
            kind="thesis",
            body=f"Placeholder text {i} " * 40,
            source_kind="import",
            source_ref=f"knowledge/{i}.md",
        )


@pytest.fixture
def live(tmp_path):
    path = tmp_path / "data" / "hub.db"
    conn = store.connect(path)
    store.migrate(conn)
    _add(conn, 60)
    conn.close()
    return path


@pytest.fixture
def good_backup(live, tmp_path):
    return store.backup(live, tmp_path / "backups", now=T0).path


def _run(capsys, backup, live):
    code = main(["db", "restore-check", str(backup), "--db", str(live)])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _insert(live, sql):
    conn = sqlite3.connect(live)
    conn.execute(sql)
    conn.commit()
    conn.close()


def test_good_backup_passes(live, good_backup, capsys):
    code, out, err = _run(capsys, good_backup, live)
    assert code == 0, out
    assert err == ""
    assert "FAIL" not in out
    assert "integrity_check" in out
    assert "rows: document_revisions" in out
    assert "backup 60, live 60" in out
    assert out.strip().splitlines()[-1].startswith("PASS")


def test_corrupt_table_page_fails(live, good_backup, capsys):
    conn = sqlite3.connect(good_backup)
    root = conn.execute(
        "SELECT rootpage FROM sqlite_master WHERE name = 'document_revisions'"
    ).fetchone()[0]
    conn.close()
    data = bytearray(good_backup.read_bytes())
    page = int.from_bytes(data[16:18], "big")
    # Overwrite the table's root page header on disk: real damage, no mocks.
    start = (root - 1) * page
    data[start : start + 16] = b"\xff" * 16
    good_backup.write_bytes(bytes(data))
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 1
    assert "FAIL" in out
    assert "integrity_check" in out


def test_corrupt_index_page_fails_on_integrity_alone(live, tmp_path, capsys):
    conn = sqlite3.connect(live)
    conn.execute("CREATE INDEX idx_revisions_ref ON document_revisions (source_ref)")
    conn.commit()
    conn.close()
    backup = store.backup(live, tmp_path / "backups", now=T0).path
    conn = sqlite3.connect(backup)
    root = conn.execute(
        "SELECT rootpage FROM sqlite_master WHERE name = 'idx_revisions_ref'"
    ).fetchone()[0]
    conn.close()
    data = bytearray(backup.read_bytes())
    page = int.from_bytes(data[16:18], "big")
    start = (root - 1) * page
    # Change key text inside the index page so entries no longer match their table rows, while
    # the page structure, every table and the index itself stay readable.
    leaf = bytes(data[start : start + page])
    assert b"knowledge/" in leaf
    data[start : start + page] = leaf.replace(b"1.md", b"9.md")
    backup.write_bytes(bytes(data))
    code, out, _ = _run(capsys, backup, live)
    assert code == 1
    integrity_line = next(line for line in out.splitlines() if line.startswith("integrity_check"))
    assert "FAIL" in integrity_line
    assert "unreadable" not in integrity_line


def test_non_database_file_fails(live, tmp_path, capsys):
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database " * 500)
    code, out, _ = _run(capsys, junk, live)
    assert code == 1
    assert "FAIL" in out


def test_lagging_backup_is_a_warning_not_a_failure(live, good_backup, capsys):
    conn = store.connect(live)
    _add(conn, 5)
    conn.close()
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 0, out
    assert "WARN" in out
    assert "backup 60, live 65" in out
    assert "lags" in out


def test_backup_with_more_rows_than_live_fails(live, good_backup, capsys):
    _insert(live, "DELETE FROM document_revisions WHERE id > 50")
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 1
    assert "FAIL" in out
    assert "more rows" in out


def test_newer_newest_revision_fails(live, good_backup, capsys):
    _insert(live, "UPDATE document_revisions SET created_at = '2000-01-01T00:00:00.000Z'")
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 1
    assert "backup is newer" in out


def test_older_newest_revision_warns(live, good_backup, capsys):
    _insert(live, "UPDATE document_revisions SET created_at = '2999-01-01T00:00:00.000Z'")
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 0, out
    assert "newest document_revisions.created_at" in out
    assert "backup lags" in out or "lags the live store" in out


def test_table_missing_from_backup_fails(live, good_backup, capsys):
    _insert(live, "CREATE TABLE extra_live (id INTEGER)")
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 1
    assert "missing from backup: extra_live" in out


def test_table_not_in_live_fails(live, good_backup, capsys):
    _insert(live, "DROP TABLE beats_proposals")
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 1
    assert "not in live store: beats_proposals" in out


def test_never_modifies_inputs_and_removes_temp_copy(live, good_backup, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    before = (_digest(live), _digest(good_backup))
    before_files = sorted(p.name for p in live.parent.iterdir())
    assert main(["db", "restore-check", str(good_backup), "--db", str(live)]) == 0
    assert (_digest(live), _digest(good_backup)) == before
    assert sorted(p.name for p in live.parent.iterdir()) == before_files
    assert list(scratch.iterdir()) == []


def test_temp_copy_removed_when_backup_is_corrupt(live, tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"x" * 4096)
    assert main(["db", "restore-check", str(junk), "--db", str(live)]) == 1
    assert list(scratch.iterdir()) == []


def test_missing_backup_exits_1_with_one_line(live, tmp_path, capsys):
    code, out, err = _run(capsys, tmp_path / "nope.db", live)
    assert code == 1
    assert out == ""
    assert err.count("\n") == 1
    assert "backup not found" in err


def test_missing_live_database_exits_1_with_one_line(good_backup, tmp_path, capsys):
    code, out, err = _run(capsys, good_backup, tmp_path / "absent.db")
    assert code == 1
    assert out == ""
    assert err.count("\n") == 1
    assert "database not found" in err
    assert not (tmp_path / "absent.db").exists()


def _line(out, prefix):
    return next(line for line in out.splitlines() if line.startswith(prefix))


def test_row_count_lag_alone_warns(live, good_backup, capsys):
    conn = store.connect(live)
    _add(conn, 5)
    conn.close()
    bconn = sqlite3.connect(good_backup)
    newest = bconn.execute("SELECT MAX(created_at) FROM document_revisions").fetchone()[0]
    bconn.close()
    _insert(live, f"UPDATE document_revisions SET created_at = '{newest}'")
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 0, out
    rows_line = _line(out, "rows: document_revisions")
    assert "WARN" in rows_line
    assert "backup 60, live 65" in rows_line
    assert "PASS" in _line(out, "newest document_revisions.created_at")


def test_backup_with_revisions_against_live_without_fails(live, good_backup, capsys):
    _insert(live, "DELETE FROM document_revisions")
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 1
    newest_line = _line(out, "newest document_revisions.created_at")
    assert "FAIL" in newest_line
    assert "backup is newer" in newest_line
    assert "live None" in newest_line


def test_summary_counts_warnings(live, good_backup, capsys):
    conn = store.connect(live)
    _add(conn, 5)
    conn.close()
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 0, out
    summary = out.strip().splitlines()[-1]
    assert "all" not in summary
    assert summary == "PASS: 12 passed, 2 warned, 0 failed"


def test_summary_without_warnings_says_all_passed(live, good_backup, capsys):
    code, out, _ = _run(capsys, good_backup, live)
    assert code == 0, out
    summary = out.strip().splitlines()[-1]
    assert summary.startswith("PASS: all ")
    assert "warned" not in summary


def test_live_path_with_space_hash_and_question_mark(tmp_path, capsys):
    path = tmp_path / "my data #1 what?" / "hub.db"
    conn = store.connect(path)
    store.migrate(conn)
    _add(conn, 6)
    conn.close()
    backup = tmp_path / "copy.db"
    shutil.copyfile(path, backup)
    code, out, err = _run(capsys, backup, path)
    assert code == 0, out
    assert err == ""
    assert "backup 6, live 6" in out
