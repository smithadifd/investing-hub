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


def _export(tmp_path):
    src = tmp_path / "export"
    (src / "knowledge").mkdir(parents=True)
    (src / "memory.md").write_text("Placeholder memory text.\n", encoding="utf-8")
    return src


@pytest.mark.parametrize("where", ["link.md", "knowledge/link.md"])
def test_symlink_outside_the_export_is_skipped(db, tmp_path, capsys, where):
    src = _export(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-BODY-MARKER\n", encoding="utf-8")
    (src / where).symlink_to(outside)
    assert _run(db, "--apply", src=src) == 0
    captured = capsys.readouterr()
    assert f"skipped: {where} (symlink resolves outside the export directory)" in captured.out
    assert "OUTSIDE-BODY-MARKER" not in captured.out + captured.err
    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT count(*) FROM document_revisions WHERE body LIKE '%OUTSIDE-BODY-MARKER%'"
        ).fetchone() == (0,)
    finally:
        conn.close()
    assert [r[3] for r in _rows(db)] == ["memory.md"]


def test_symlink_inside_the_export_is_still_read(db, tmp_path):
    src = _export(tmp_path)
    (src / "alias.md").symlink_to(src / "memory.md")
    assert _run(db, "--apply", src=src) == 0
    assert {r[3] for r in _rows(db)} == {"memory.md", "alias.md"}


def test_nested_knowledge_entries_are_reported_as_skipped(db, tmp_path, capsys):
    src = _export(tmp_path)
    (src / "knowledge" / "sub").mkdir()
    (src / "knowledge" / "sub" / "x.md").write_text("Placeholder nested.\n", encoding="utf-8")
    (src / "knowledge" / "data.txt").write_text("Placeholder text.\n", encoding="utf-8")
    (src / "knowledge" / "dir.md").mkdir()
    assert _run(db, "--apply", src=src) == 0
    out = capsys.readouterr().out
    assert "skipped: knowledge/sub (not a top-level .md file in knowledge/)" in out
    assert "skipped: knowledge/data.txt (not a top-level .md file in knowledge/)" in out
    assert "skipped: knowledge/dir.md (not a top-level .md file in knowledge/)" in out
    assert [r[3] for r in _rows(db)] == ["memory.md"]


def test_directory_named_like_a_document_is_ignored(db, tmp_path):
    src = _export(tmp_path)
    (src / "x.md").mkdir()
    assert _run(db, "--apply", src=src) == 0
    assert [r[3] for r in _rows(db)] == ["memory.md"]


def test_dry_run_refuses_a_directory_as_db_like_apply(tmp_path, capsys):
    src = _export(tmp_path)
    bad = tmp_path / "dbdir"
    bad.mkdir()
    assert _run(bad, src=src) == 1
    captured = capsys.readouterr()
    assert "would add" not in captured.out
    assert len(captured.err.strip().splitlines()) == 1
    assert _run(bad, "--apply", src=src) == 1


def test_dry_run_reads_a_db_path_with_uri_characters(tmp_path, capsys):
    src = _export(tmp_path)
    odd = tmp_path / "a b#c?d" / "hub.db"
    assert _run(odd, "--apply", src=src) == 0
    capsys.readouterr()
    assert _run(odd, src=src) == 0
    out = capsys.readouterr().out
    assert "unchanged: memory.md (memory)" in out
