import io

import pytest

from hub import store
from hub.cli import main


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(path)]) == 0
    return path


def _seed(db, slug="alpha-note", bodies=("first body\n", "second body\n")):
    conn = store.connect(db)
    try:
        for body in bodies:
            store.insert_document_revision(
                conn,
                slug=slug,
                kind="note",
                body=body,
                source_kind="import",
                source_ref="seed",
                title="Alpha Title",
            )
    finally:
        conn.close()


def _revise(db, slug, body_file, *extra):
    argv = ["doc", "revise", slug, "--source-ref", "sess-1", "--db", str(db)]
    if body_file is not None:
        argv += ["--body-file", str(body_file)]
    return main(argv + list(extra))


def test_get_document_revision_latest_and_specific(db):
    _seed(db)
    conn = store.connect(db)
    try:
        assert store.get_document_revision(conn, "alpha-note")["body"] == "second body\n"
        assert store.get_document_revision(conn, "alpha-note", 1)["body"] == "first body\n"
        with pytest.raises(store.StoreError, match="unknown document"):
            store.get_document_revision(conn, "missing")
        with pytest.raises(store.StoreError, match="unknown revision 9"):
            store.get_document_revision(conn, "alpha-note", 9)
    finally:
        conn.close()


def test_list_show_round_trip(db, capsys):
    _seed(db)
    capsys.readouterr()
    assert main(["doc", "list", "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].split() == [
        "slug",
        "kind",
        "revision",
        "source_kind",
        "revised_at",
    ]
    row = out.splitlines()[1].split()
    assert row[:4] == ["alpha-note", "note", "2", "import"]
    assert "Alpha Title" not in out
    assert main(["doc", "list", "--titles", "--db", str(db)]) == 0
    titled = capsys.readouterr().out
    assert titled.splitlines()[0].split()[-1] == "title"
    assert "Alpha Title" in titled


def test_list_empty_store_prints_header_only(db, capsys):
    capsys.readouterr()
    assert main(["doc", "list", "--db", str(db)]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 1
    assert lines[0].split() == [
        "slug",
        "kind",
        "revision",
        "source_kind",
        "revised_at",
    ]
    assert main(["doc", "list", "--titles", "--db", str(db)]) == 0
    titled_lines = capsys.readouterr().out.splitlines()
    assert len(titled_lines) == 1
    assert titled_lines[0].split() == [
        "slug",
        "kind",
        "revision",
        "source_kind",
        "revised_at",
        "title",
    ]


def test_show_latest_vs_revision(db, capsys):
    _seed(db)
    capsys.readouterr()
    assert main(["doc", "show", "alpha-note", "--db", str(db)]) == 0
    assert capsys.readouterr().out == "second body\n"
    assert main(["doc", "show", "alpha-note", "--revision", "1", "--db", str(db)]) == 0
    assert capsys.readouterr().out == "first body\n"


def test_show_unknown_slug_and_revision_exit_1(db, capsys):
    _seed(db)
    capsys.readouterr()
    assert main(["doc", "show", "missing", "--db", str(db)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "unknown document" in captured.err
    assert main(["doc", "show", "alpha-note", "--revision", "7", "--db", str(db)]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "unknown revision" in captured.err


def test_revise_bumps_revision_and_list_shows_session(db, tmp_path, capsys):
    _seed(db)
    body = tmp_path / "new.md"
    body.write_text("third body\n")
    capsys.readouterr()
    assert _revise(db, "alpha-note", body) == 0
    assert capsys.readouterr().out == "alpha-note revision 3 (session)\n"
    assert main(["doc", "list", "--db", str(db)]) == 0
    row = capsys.readouterr().out.splitlines()[1].split()
    assert row[:4] == ["alpha-note", "note", "3", "session"]
    conn = store.connect(db)
    try:
        latest = store.get_document_revision(conn, "alpha-note")
    finally:
        conn.close()
    assert (latest["body"], latest["source_ref"]) == ("third body\n", "sess-1")


def test_revise_new_slug_with_kind_creates_revision_1(db, tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("hello\n")
    capsys.readouterr()
    assert _revise(db, "beta-note", body, "--kind", "note", "--title", "Beta") == 0
    assert capsys.readouterr().out == "beta-note revision 1 (session)\n"


def test_revise_new_slug_without_kind_refused(db, tmp_path, capsys):
    body = tmp_path / "b.md"
    body.write_text("hello\n")
    capsys.readouterr()
    assert _revise(db, "beta-note", body) == 1
    captured = capsys.readouterr()
    assert len(captured.err.splitlines()) == 1
    assert "--kind" in captured.err
    conn = store.connect(db)
    try:
        assert store.list_documents(conn) == []
    finally:
        conn.close()


@pytest.mark.parametrize("flag", [("--kind", "other"), ("--title", "Other Title")])
def test_revise_existing_slug_refuses_kind_and_title(db, tmp_path, capsys, flag):
    _seed(db)
    body = tmp_path / "new.md"
    body.write_text("third body\n")
    capsys.readouterr()
    assert _revise(db, "alpha-note", body, *flag) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "hub doc revise: alpha-note exists; --kind/--title apply to a new document only"
    ]
    conn = store.connect(db)
    try:
        assert store.get_document_revision(conn, "alpha-note")["revision"] == 2
        row = conn.execute("SELECT kind, title FROM documents").fetchone()
    finally:
        conn.close()
    assert (row["kind"], row["title"]) == ("note", "Alpha Title")


def test_revise_missing_database_beats_empty_body(tmp_path, capsys):
    missing = tmp_path / "none.db"
    body = tmp_path / "empty.md"
    body.write_text("")
    assert _revise(missing, "alpha-note", body) == 1
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1
    assert "database not found" in err
    assert "empty body" not in err
    assert not missing.exists()


def test_revise_unknown_body_file_exit_1(db, tmp_path, capsys):
    _seed(db)
    capsys.readouterr()
    assert _revise(db, "alpha-note", tmp_path / "nope.md") == 1
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_revise_reads_body_from_stdin(db, monkeypatch, capsys):
    _seed(db)
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin\n"))
    capsys.readouterr()
    assert _revise(db, "alpha-note", None) == 0
    assert capsys.readouterr().out == "alpha-note revision 3 (session)\n"
    assert main(["doc", "show", "alpha-note", "--db", str(db)]) == 0
    assert capsys.readouterr().out == "from stdin\n"


@pytest.mark.parametrize("empty", ["", "  \n\n"])
def test_revise_empty_body_refused(db, tmp_path, capsys, empty):
    _seed(db)
    body = tmp_path / "empty.md"
    body.write_text(empty)
    capsys.readouterr()
    assert _revise(db, "alpha-note", body) == 1
    assert "empty body" in capsys.readouterr().err
    conn = store.connect(db)
    try:
        assert store.get_document_revision(conn, "alpha-note")["revision"] == 2
    finally:
        conn.close()


def test_missing_database_exit_1(tmp_path, capsys):
    missing = tmp_path / "none.db"
    assert main(["doc", "list", "--db", str(missing)]) == 1
    assert "database not found" in capsys.readouterr().err
    assert not missing.exists()
