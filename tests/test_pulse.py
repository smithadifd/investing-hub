"""Tests for hub/pulse.py and `hub pulse write`."""

import urllib.request
from datetime import UTC, date, datetime

import pytest

from hub import pulse, store
from hub.cli import main


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(path)]) == 0
    return path


def _today_iso() -> str:
    """Today's date as YYYY-MM-DD (UTC)."""
    return datetime.now(UTC).strftime("%Y-%m-%d")


def _today_timestamp() -> str:
    """A today-stamped timestamp matching the store's strftime('%Y-%m-%dT%H:%M:%fZ') shape."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%fZ")


def _seed_finding(
    conn, *, subject: str, score: float, status: str = "new", created_at: str | None = None
) -> None:
    created_at = created_at or _today_timestamp()
    conn.execute(
        "INSERT INTO findings (kind, subject, score, evidence, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("synthetic_trigger", subject, score, "{}", status, created_at),
    )
    conn.commit()


def _seed_call(
    conn,
    *,
    subject: str,
    call: str,
    resolves_by: str,
    resolution: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO calls (subject, call, called_at, resolves_by, resolution)"
        " VALUES (?, ?, ?, ?, ?)",
        (subject, call, _today_timestamp(), resolves_by, resolution),
    )
    conn.commit()


def _seed_beat(conn, *, proposal: str, status: str = "proposed", created_at: str | None = None) -> None:
    created_at = created_at or _today_timestamp()
    conn.execute(
        "INSERT INTO beats_proposals (proposal, rationale, status, created_at)"
        " VALUES (?, ?, ?, ?)",
        (proposal, "synthetic", status, created_at),
    )
    conn.commit()


# --- compose() unit tests (deterministic, no I/O) ----------------------------------------


def test_compose_quiet_day_is_one_concise_line():
    result = pulse.PulseResult(date=date(2026, 10, 4), quiet=True)
    text = pulse.compose(result)
    assert text == (
        "2026-10-04: Steady day; no book-level thesis invalidations or"
        " rung triggers active."
    )


def test_compose_records_model_in_comment_when_supplied():
    result = pulse.PulseResult(date=date(2026, 10, 4), quiet=True)
    text = pulse.compose(result, model="claude-sonnet-5-5")
    assert text.startswith("<!-- model: claude-sonnet-5-5 -->\n")


def test_compose_emits_high_bar_lines_and_separates_worth_discussing():
    result = pulse.PulseResult(
        date=date(2026, 10, 4),
        quiet=False,
        high_bar=(
            pulse.PulseItem(
                kind=pulse.ITEM_KIND_CALL,
                subject="thesis-x invalidation",
                detail="call resolves 2026-10-04: thesis-x broken",
                score=0.85,
            ),
        ),
        worth_discussing=(
            pulse.PulseItem(
                kind=pulse.ITEM_KIND_FINDING,
                subject="rung-y near",
                detail="rung_trigger @ 2026-10-04T07:00:00.000000Z",
                score=0.4,
            ),
        ),
    )
    text = pulse.compose(result, model="m")
    assert "book-level activity:" in text
    assert "- thesis-x invalidation" in text
    assert "Worth discussing:" in text
    assert "- rung-y near" in text


# --- collect_pulse() against a fixture store --------------------------------------------


def test_collect_pulse_empty_store_is_quiet(db):
    conn = store.connect(db)
    try:
        result = pulse.collect_pulse(conn, on=date(2026, 10, 4))
    finally:
        conn.close()
    assert result.quiet is True
    assert result.high_bar == ()
    assert result.worth_discussing == ()


def test_collect_pulse_splits_by_threshold(db):
    conn = store.connect(db)
    try:
        # High-bar finding (above 0.7).
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
        # Worth-discussing finding (between 0.0 and 0.7).
        _seed_finding(conn, subject="rung-y near", score=0.4)
        # Dated call resolving today → high-bar by construction.
        _seed_call(
            conn,
            subject="call-z resolves",
            call="call-z note",
            resolves_by=_today_iso(),
        )
        result = pulse.collect_pulse(conn, on=date.fromisoformat(_today_iso()))
    finally:
        conn.close()
    subjects = {item.subject for item in result.high_bar}
    assert "thesis-x invalidation" in subjects
    assert "call-z resolves" in subjects
    discussing_subjects = {item.subject for item in result.worth_discussing}
    assert discussing_subjects == {"rung-y near"}
    assert result.quiet is False


# --- `hub pulse write` end-to-end -----------------------------------------------------


def test_quiet_day_writes_one_concise_line(db, tmp_path, capsys):
    out_dir = tmp_path / "out"
    rc = main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)])
    assert rc == 0
    captured = capsys.readouterr()
    out_path = out_dir / (datetime.now(UTC).strftime("%Y-%m-%d") + ".md")
    assert f"pulse written: {out_path}" in captured.out
    body = out_path.read_text(encoding="utf-8")
    lines = [line for line in body.splitlines() if not line.startswith("<!--")]
    assert len(lines) == 1  # exactly one concise substantive line
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    assert lines[0] == (
        f"{today}: Steady day; no book-level thesis invalidations or"
        " rung triggers active."
    )


def test_active_day_lists_high_bar_items(db, tmp_path, capsys):
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
    finally:
        conn.close()
    out_dir = tmp_path / "out"
    assert main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)]) == 0
    out_path = out_dir / (_today_iso() + ".md")
    body = out_path.read_text(encoding="utf-8")
    assert "book-level activity:" in body
    assert "thesis-x invalidation" in body


def test_active_day_lists_worth_discussing_items(db, tmp_path):
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="rung-y near", score=0.4)
    finally:
        conn.close()
    out_dir = tmp_path / "out"
    assert main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)]) == 0
    out_path = out_dir / (_today_iso() + ".md")
    body = out_path.read_text(encoding="utf-8")
    assert "Worth discussing:" in body
    assert "rung-y near" in body


def test_custom_date_and_out_dir_writes_to_target(db, tmp_path):
    conn = store.connect(db)
    try:
        _seed_finding(
            conn,
            subject="thesis-x invalidation",
            score=0.95,
            created_at="2026-09-15T07:00:00.000000Z",
        )
    finally:
        conn.close()
    out_dir = tmp_path / "custom"
    rc = main(
        [
            "pulse",
            "write",
            "--db",
            str(db),
            "--out-dir",
            str(out_dir),
            "--date",
            "2026-09-15",
        ]
    )
    assert rc == 0
    out_path = out_dir / "2026-09-15.md"
    assert out_path.is_file()
    body = out_path.read_text(encoding="utf-8")
    assert "2026-09-15: book-level activity:" in body
    assert "thesis-x invalidation" in body
    default_path = tmp_path / "default-book"
    assert not (default_path / "2026-09-15.md").exists()


def test_dry_run_does_not_write_file(db, tmp_path, capsys):
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
    finally:
        conn.close()
    out_dir = tmp_path / "out"
    rc = main(
        [
            "pulse",
            "write",
            "--db",
            str(db),
            "--out-dir",
            str(out_dir),
            "--dry-run",
        ]
    )
    assert rc == 0
    assert not list(out_dir.glob("*.md"))
    captured = capsys.readouterr()
    assert "book-level activity:" in captured.out
    assert "thesis-x invalidation" in captured.out


def test_deterministic_path_makes_no_network_calls(db, tmp_path, monkeypatch):
    """The deterministic fallback must never reach the network."""
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
    finally:
        conn.close()
    network_calls = []

    def trap(*args, **kwargs):
        network_calls.append((args, kwargs))
        raise AssertionError("network call attempted during deterministic pulse")

    monkeypatch.setattr(urllib.request, "urlopen", trap)
    out_dir = tmp_path / "out"
    assert main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)]) == 0
    assert network_calls == []


def test_model_option_records_model_in_header(db, tmp_path):
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
    finally:
        conn.close()
    out_dir = tmp_path / "out"
    assert main(
        [
            "pulse",
            "write",
            "--db",
            str(db),
            "--out-dir",
            str(out_dir),
            "--model",
            "claude-sonnet-5-5",
        ]
    ) == 0
    out_path = out_dir / (_today_iso() + ".md")
    body = out_path.read_text(encoding="utf-8")
    assert body.startswith("<!-- model: claude-sonnet-5-5 -->\n")


def test_invalid_date_exits_1(db, tmp_path, capsys):
    rc = main(
        [
            "pulse",
            "write",
            "--db",
            str(db),
            "--out-dir",
            str(tmp_path / "out"),
            "--date",
            "not-a-date",
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "invalid --date" in err


def test_missing_database_exits_1(tmp_path, capsys):
    missing = tmp_path / "none.db"
    rc = main(
        [
            "pulse",
            "write",
            "--db",
            str(missing),
            "--out-dir",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "database not found" in err
    assert not missing.exists()


def test_collect_pulse_ignores_resolved_calls(db):
    conn = store.connect(db)
    try:
        _seed_call(
            conn,
            subject="call-resolved",
            call="done",
            resolves_by=_today_iso(),
            resolution="resolved today",
        )
        result = pulse.collect_pulse(conn, on=date.fromisoformat(_today_iso()))
    finally:
        conn.close()
    assert result.quiet is True


def test_collect_pulse_includes_pending_findings_from_prior_days(db):
    conn = store.connect(db)
    try:
        _seed_finding(
            conn,
            subject="pending carry-over",
            score=0.9,
            status="pending",
            created_at="2026-09-01T07:00:00.000000Z",
        )
        result = pulse.collect_pulse(conn, on=date.fromisoformat(_today_iso()))
    finally:
        conn.close()
    assert result.quiet is False
    assert any(item.subject == "pending carry-over" for item in result.high_bar)