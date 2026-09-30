"""session-open tests. Every store row, pack and token here is synthetic; IC is mocked at the
``hub.ic`` seam (``fetch_contract_docs``), never over the network."""

import hashlib
import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hub import ic, session_open, store
from hub.cli import main

TOKEN = "ict_SYNTHETIC0test0token0value0000"
ROOT = Path(__file__).resolve().parent.parent


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat(timespec="seconds")


def _docs(handoff_stamp="1.7", actions_stamp="1.4", handoff_ok=None, actions_ok=None):
    def doc(key, name, stamp, expected, ok):
        return ic.ContractDoc(
            key=key,
            name=name,
            stamp=stamp,
            expected_stamp=expected,
            stamp_matches=(stamp == expected) if ok is None else ok,
            content="# synthetic\n",
        )

    return [
        doc("handoff_schema", "handoff-schema.md", handoff_stamp, "1.7", handoff_ok),
        doc("advisor_actions", "advisor-actions.md", actions_stamp, "1.4", actions_ok),
    ]


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ic.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ic.BASE_URL_ENV, "http://ic.invalid:8000")
    conn = store.connect(tmp_path / "data" / "hub.db")
    store.migrate(conn)
    conn.close()
    meta = {
        "fetched_at": _iso(timedelta(hours=5)),
        "generated_at": "2026-01-02T03:04:05Z",
        "schema_version": "1.7",
        "advisor_actions_version": "1.4",
    }
    (tmp_path / "data" / "ic").mkdir()
    (tmp_path / "data" / "ic" / "pack-meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(ic, "fetch_contract_docs", lambda base, token, timeout=None: _docs())
    return tmp_path


def _run(capsys, *argv):
    assert main(["session-open", *argv]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert TOKEN not in captured.out
    return captured.out


def _sql(root, statement, params=()):
    conn = sqlite3.connect(root / "data" / "hub.db")
    conn.execute(statement, params)
    conn.commit()
    conn.close()


def test_matching_stamps_print_in_sync(env, capsys):
    out = _run(capsys)
    assert "contract: in sync" in out
    assert "WARN" not in out
    assert "pack: fetched" in out and "age 5h" in out
    assert "schema 1.7" in out and "advisor-actions 1.4" in out


def test_stamp_differs_from_pack_version_warns_naming_both(env, capsys, monkeypatch):
    monkeypatch.setattr(
        ic,
        "fetch_contract_docs",
        lambda base, token, timeout=None: _docs(handoff_stamp="1.8", handoff_ok=True),
    )
    out = _run(capsys)
    assert "contract: in sync" not in out
    warn = [ln for ln in out.splitlines() if ln.startswith("WARN contract drift")]
    assert len(warn) == 1
    assert "handoff-schema.md" in warn[0] and "1.8" in warn[0] and "1.7" in warn[0]


def test_stamp_matches_false_warns_naming_both(env, capsys, monkeypatch):
    monkeypatch.setattr(
        ic,
        "fetch_contract_docs",
        lambda base, token, timeout=None: _docs(actions_stamp="1.3"),
    )
    out = _run(capsys)
    warn = [ln for ln in out.splitlines() if ln.startswith("WARN contract drift")]
    assert len(warn) == 1
    assert "advisor-actions.md" in warn[0] and "1.3" in warn[0] and "1.4" in warn[0]
    assert "expected_stamp" in warn[0]


def test_two_mismatching_docs_give_two_warn_lines(env, capsys, monkeypatch):
    monkeypatch.setattr(
        ic,
        "fetch_contract_docs",
        lambda base, token, timeout=None: _docs(handoff_stamp="0.1", actions_stamp="0.2"),
    )
    out = _run(capsys)
    assert len([ln for ln in out.splitlines() if ln.startswith("WARN contract drift")]) == 2


def test_missing_pack_meta_warns(env, capsys):
    (env / "data" / "ic" / "pack-meta.json").unlink()
    out = _run(capsys)
    assert "WARN pack:" in out and "not found" in out
    assert "contract: in sync" in out


def test_unreadable_pack_meta_warns(env, capsys):
    (env / "data" / "ic" / "pack-meta.json").write_text("{not json")
    out = _run(capsys)
    assert "WARN pack:" in out and "not valid JSON" in out


def test_missing_token_warns_without_token(env, capsys, monkeypatch):
    monkeypatch.delenv(ic.TOKEN_ENV)
    out = _run(capsys)
    assert "WARN contract: cannot check drift" in out
    assert ic.TOKEN_ENV in out


def test_ic_unreachable_warns_and_redacts_token(env, capsys, monkeypatch):
    def boom(base, token, timeout=None):
        # An error that (wrongly) carries the token must still not reach the output.
        raise ic.IcError(f"cannot reach IC at {base}: refused with {TOKEN}")

    monkeypatch.setattr(ic, "fetch_contract_docs", boom)
    out = _run(capsys)
    assert "WARN contract: cannot check drift: cannot reach IC" in out
    assert TOKEN not in out and ic.REDACTED in out


def test_missing_store_warns(env, capsys):
    (env / "data" / "hub.db").unlink()
    out = _run(capsys)
    assert "WARN store: database not found" in out
    assert "contract: in sync" in out


def test_store_without_tables_warns_per_section(env, capsys):
    (env / "data" / "hub.db").unlink()
    sqlite3.connect(env / "data" / "hub.db").close()
    out = _run(capsys)
    for name in ("briefs", "handoffs", "documents"):
        assert f"WARN {name}: store query failed" in out


def test_pending_briefs_unapplied_handoffs_and_stale_documents_listed(env, capsys):
    _sql(env, "INSERT INTO briefs (body, status) VALUES (?, 'pending')", ("Pending brief A\nx",))
    _sql(env, "INSERT INTO briefs (body, status) VALUES (?, 'consumed')", ("Done brief B",))
    _sql(env, "INSERT INTO handoffs (body, status) VALUES (?, 'draft')", ("Draft handoff C",))
    _sql(env, "INSERT INTO handoffs (body, status) VALUES (?, 'approved')", ("Approved hand D",))
    _sql(env, "INSERT INTO handoffs (body, status) VALUES (?, 'applied')", ("Applied hand E",))
    conn = store.connect(env / "data" / "hub.db")
    for slug in ("old-doc", "fresh-doc"):
        store.insert_document_revision(
            conn, slug=slug, kind="thesis", body="b", source_kind="import", source_ref="r"
        )
    conn.execute(
        "UPDATE document_revisions SET created_at = ? WHERE document_id ="
        " (SELECT id FROM documents WHERE slug = 'old-doc')",
        (_iso(timedelta(days=45)),),
    )
    conn.close()

    out = _run(capsys)
    assert "pending briefs: 1" in out and "Pending brief A" in out and "Done brief B" not in out
    assert "unapplied handoffs: 2" in out
    assert "Draft handoff C" in out and "Approved hand D" in out and "Applied hand E" not in out
    assert "documents older than 30d: 1" in out
    assert "old-doc" in out and "fresh-doc" not in out


def test_stale_days_flag_moves_the_threshold(env, capsys):
    conn = store.connect(env / "data" / "hub.db")
    store.insert_document_revision(
        conn, slug="mid-doc", kind="thesis", body="b", source_kind="import", source_ref="r"
    )
    conn.execute("UPDATE document_revisions SET created_at = ?", (_iso(timedelta(days=10)),))
    conn.close()
    assert "mid-doc" not in _run(capsys)
    out = _run(capsys, "--stale-days", "5")
    assert "documents older than 5d: 1" in out and "mid-doc" in out


def test_stale_days_from_config(env, capsys):
    conn = store.connect(env / "data" / "hub.db")
    store.insert_document_revision(
        conn, slug="mid-doc", kind="thesis", body="b", source_kind="import", source_ref="r"
    )
    conn.execute("UPDATE document_revisions SET created_at = ?", (_iso(timedelta(days=10)),))
    conn.close()
    (env / "config.yaml").write_text("session_open:\n  stale_days: 5\n")
    assert "mid-doc" in _run(capsys)
    (env / "config.yaml").write_text("session_open:\n  stale_days: soon\n")
    out = _run(capsys)
    assert "WARN config:" in out and "documents older than 30d" in out


def test_unexpected_exception_in_a_section_still_exits_0(env, capsys, monkeypatch):
    def explode(base, token, timeout=None):
        raise RuntimeError(f"boom {TOKEN}")

    monkeypatch.setattr(ic, "fetch_contract_docs", explode)
    out = _run(capsys)
    assert "WARN contract: unexpected RuntimeError" in out
    assert "pending briefs: 0" in out


def test_unexpected_exception_in_store_section_still_exits_0(env, capsys, monkeypatch):
    def explode(conn):
        raise ValueError("boom")

    monkeypatch.setattr(store, "list_documents", explode)
    out = _run(capsys)
    assert "WARN documents: unexpected ValueError" in out
    assert "pending briefs: 0" in out


def test_total_failure_of_the_builder_still_exits_0(env, capsys, monkeypatch):
    def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(session_open, "build_block", explode)
    out = _run(capsys)
    assert "WARN session-open failed: RuntimeError" in out


def test_nothing_is_written(env, capsys):
    _sql(env, "INSERT INTO briefs (body) VALUES ('brief')")
    before_tree = _tree(env)
    before_db = (env / "data" / "hub.db").read_bytes()
    _run(capsys)
    assert _tree(env) == before_tree
    assert (env / "data" / "hub.db").read_bytes() == before_db


def test_settings_json_has_one_session_start_hook():
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    entries = settings["hooks"]["SessionStart"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert len(entries) == 1 and len(commands) == 1
    assert "hub session-open" in commands[0]
    assert set(settings["hooks"]) == {"SessionStart"}


def test_hook_command_does_not_fail_without_hub(tmp_path):
    settings = json.loads((ROOT / ".claude" / "settings.json").read_text())
    command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    result = subprocess.run(
        ["/bin/sh", "-c", command],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_live_wal_content_is_seen(env, capsys):
    writer = store.connect(env / "data" / "hub.db")  # stays open: its WAL is live
    writer.execute("INSERT INTO briefs (body) VALUES ('Live WAL brief')")
    try:
        assert (env / "data" / "hub.db-wal").exists()
        out = _run(capsys)
    finally:
        writer.close()
    assert "Live WAL brief" in out
