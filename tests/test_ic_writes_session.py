"""session-open priming (the ``now:`` line, recent IC writes) and the handoffs retirement."""

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hub import ic, ic_writes, session_open, store
from hub.cli import main

TOKEN = "ict_SYNTHETIC0test0token0value0000"
INSTANT = datetime(2026, 10, 7, 0, 4, 30, tzinfo=UTC)  # 20:04 on Tue 6 Oct in New York (EDT)


def test_now_line_names_the_zone_from_tz():
    line = session_open.local_now_line(INSTANT, {"TZ": "America/New_York"})
    assert line == "now: Tue 2026-10-06 20:04 EDT (America/New_York)"


def test_now_line_is_local_time_not_utc():
    line = session_open.local_now_line(INSTANT, {"TZ": "Pacific/Auckland"})
    assert line.startswith("now: Wed 2026-10-07 13:04 ") and "(Pacific/Auckland)" in line


def test_now_line_uses_etc_localtime_when_tz_unset(tmp_path):
    zoneinfo = Path("/usr/share/zoneinfo/America/New_York")
    if not zoneinfo.exists():
        pytest.skip("no system zoneinfo to link to")
    link = tmp_path / "localtime"
    link.symlink_to(zoneinfo)
    line = session_open.local_now_line(INSTANT, {}, link)
    assert line == "now: Tue 2026-10-06 20:04 EDT (America/New_York)"


def test_now_line_does_not_name_an_unverifiable_zone(tmp_path):
    plain = tmp_path / "localtime"
    plain.write_bytes(b"not a symlink into zoneinfo")
    line = session_open.local_now_line(INSTANT, {}, plain)
    assert line.startswith("now: ") and "(" not in line
    assert "(" not in session_open.local_now_line(INSTANT, {"TZ": "Not/AZone"}, plain)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ic.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ic.BASE_URL_ENV, "http://ic.invalid:8000")
    monkeypatch.setenv("TZ", "America/New_York")
    monkeypatch.setattr(ic, "fetch_contract_docs", lambda *a, **k: [])
    conn = store.connect(tmp_path / "data" / "hub.db")
    store.migrate(conn)
    conn.close()
    return tmp_path


def _log(env, at, action="ADD_ALERT", target="AAA"):
    conn = sqlite3.connect(env / "data" / "hub.db")
    conn.execute(
        "INSERT INTO ic_writes (at, action, target, method, path) VALUES (?, ?, ?, 'POST', '/x')",
        (ic_writes.iso_ms(at), action, target),
    )
    conn.commit()
    conn.close()


def _block(capsys):
    assert main(["session-open"]) == 0
    return capsys.readouterr().out


def test_session_open_prints_now_right_after_the_header(env, capsys):
    lines = _block(capsys).splitlines()
    assert lines[0] == "== hub session-open =="
    assert lines[1].startswith("now: ") and "(America/New_York)" in lines[1]


def test_session_open_counts_ic_writes_in_the_last_24h(env, capsys):
    now = datetime.now(UTC)
    _log(env, now - timedelta(hours=2), "MODIFY_ALERT", "BBB")
    _log(env, now - timedelta(hours=23))
    _log(env, now - timedelta(hours=25), "LOG_TRADE", "OLD")
    out = _block(capsys)
    assert "recent IC writes (last 24h): 2" in out
    assert "MODIFY_ALERT BBB" in out and "OLD" not in out
    assert "handoff" not in out.lower()


def test_session_open_with_no_writes(env, capsys):
    assert "recent IC writes (last 24h): 0" in _block(capsys)


def test_session_open_on_an_unmigrated_store_warns_not_crashes(env, capsys):
    conn = sqlite3.connect(env / "data" / "hub.db")
    conn.execute("DROP TABLE ic_writes")
    conn.commit()
    conn.close()
    out = _block(capsys)
    assert "WARN ic_writes: store query failed" in out


def test_session_open_stays_read_only(env, capsys):
    before = {p: p.read_bytes() for p in (env / "data").rglob("*") if p.is_file()}
    _block(capsys)
    assert {p: p.read_bytes() for p in (env / "data").rglob("*") if p.is_file()} == before


def test_migrations_apply_in_order_and_keep_existing_handoffs(tmp_path):
    conn = store.connect(tmp_path / "hub.db")
    store.migrate(conn, [m for m in store.load_migrations() if m.version <= 5])
    conn.execute("INSERT INTO handoffs (body, status) VALUES ('kept row', 'draft')")
    applied = store.migrate(conn)
    assert applied == ["0006_ic_writes.sql", "0007_handoffs_retired.sql"]
    assert conn.execute("SELECT body FROM handoffs").fetchall()[0]["body"] == "kept row"
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(ic_writes)")]
    assert columns == [
        "id", "at", "action", "target", "method", "path", "request", "before", "after",
        "ic_id", "receipt_id", "receipt_error", "source_ref", "reverted_by", "status", "error",
    ]  # fmt: skip
    assert store.migrate(conn) == []
    conn.close()


def test_only_session_open_reads_the_handoffs_table_and_nothing_writes_it():
    root = Path(__file__).resolve().parent.parent
    for path in (root / "hub").rglob("*.py"):
        text = path.read_text().lower()
        assert not any(
            v in text for v in ("into handoffs", "update handoffs", "delete from handoffs")
        )
        if path.name != "session_open.py":
            assert "from handoffs" not in text


def test_legacy_handoff_rows_warn_once(env, capsys):
    conn = sqlite3.connect(env / "data" / "hub.db")
    for status in ("draft", "approved", "applied"):
        conn.execute("INSERT INTO handoffs (body, status) VALUES ('x', ?)", (status,))
    conn.commit()
    conn.close()
    out = _block(capsys)
    warns = [ln for ln in out.splitlines() if "legacy handoffs" in ln]
    assert warns == ["WARN 2 legacy handoffs unapplied (handoffs retired; apply or ignore)"]


def test_no_legacy_warning_when_none_unapplied(env, capsys):
    assert "legacy handoffs" not in _block(capsys)


def test_failed_writes_are_not_counted_as_recent(env, capsys):
    _log(env, datetime.now(UTC))
    conn = sqlite3.connect(env / "data" / "hub.db")
    conn.execute(
        "INSERT INTO ic_writes (at, action, target, method, path, status)"
        " VALUES (?, 'ADD_ALERT', 'ZZZ', 'POST', '/x', 'failed')",
        (ic_writes.iso_ms(datetime.now(UTC)),),
    )
    conn.execute("UPDATE ic_writes SET status = 'unknown' WHERE id = 1")
    conn.commit()
    conn.close()
    out = _block(capsys)
    assert "recent IC writes (last 24h): 1" in out and "[unknown]" in out and "ZZZ" not in out
