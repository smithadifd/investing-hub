import shutil
import sqlite3
from pathlib import Path

import pytest

from hub import store
from hub.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "claude-export"
EXPECTED = {
    "conversations.md": "conversations",
    "extra-notes.md": "document",
    "instructions.md": "instructions",
    "knowledge/notes-a.md": "knowledge",
    "knowledge/notes-b.md": "knowledge",
    "memory.md": "memory",
    "PROJECT.md": "project",
}


@pytest.fixture
def db(tmp_path):
    return tmp_path / "hub.db"


def _rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT d.kind, r.revision, r.source_kind, r.source_ref, d.slug"
            " FROM documents d JOIN document_revisions r ON r.document_id = d.id"
            " ORDER BY r.source_ref"
        ).fetchall()
    finally:
        conn.close()


def _run(db, *extra, src=FIXTURE):
    return main(["import", "claude-export", str(src), "--db", str(db), *extra])


def test_dry_run_is_default_and_writes_nothing(db, capsys):
    assert _run(db) == 0
    out = capsys.readouterr().out
    assert "would add: memory.md (memory)" in out
    assert "dry run" in out
    assert not db.exists()


def test_explicit_dry_run_does_not_touch_existing_store(db):
    assert main(["db", "init", "--db", str(db)]) == 0
    assert _run(db, "--dry-run") == 0
    assert _rows(db) == []


def test_apply_writes_one_revision_one_document_per_file(db):
    assert _run(db, "--apply") == 0
    rows = _rows(db)
    assert {r[3]: r[0] for r in rows} == EXPECTED
    assert all(r[1] == 1 and r[2] == "import" for r in rows)
    assert {r[4] for r in rows} == {ref.removesuffix(".md") for ref in EXPECTED}


def test_contract_docs_are_skipped_and_reported(db, capsys):
    assert _run(db, "--apply") == 0
    out = capsys.readouterr().out
    assert "skipped: knowledge/advisor-actions.md" in out
    assert "skipped: knowledge/handoff-schema.md" in out
    refs = {r[3] for r in _rows(db)}
    assert "knowledge/advisor-actions.md" not in refs
    assert "knowledge/handoff-schema.md" not in refs


def test_second_apply_adds_nothing(db, capsys):
    assert _run(db, "--apply") == 0
    before = _rows(db)
    capsys.readouterr()
    assert _run(db, "--apply") == 0
    assert _rows(db) == before
    out = capsys.readouterr().out
    assert "unchanged: memory.md" in out
    assert "0 document(s) written" in out


def test_changed_content_is_reported_not_reimported(db, tmp_path, capsys):
    src = tmp_path / "export"
    shutil.copytree(FIXTURE, src)
    assert _run(db, "--apply", src=src) == 0
    (src / "memory.md").write_text("Edited placeholder.\n", encoding="utf-8")
    capsys.readouterr()
    assert _run(db, "--apply", src=src) == 0
    assert "changed, left as is: memory.md" in capsys.readouterr().out
    conn = store.connect(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM document_revisions").fetchone()[0] == len(
            EXPECTED
        )
    finally:
        conn.close()


def test_missing_dir_exits_1_with_one_line(db, tmp_path, capsys):
    assert _run(db, "--apply", src=tmp_path / "nope") == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.strip().splitlines()) == 1
    assert not db.exists()
