"""Tests for hub/pulse.py and `hub pulse write`."""

import textwrap
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


def _seed_beat(
    conn, *, proposal: str, status: str = "proposed", created_at: str | None = None
) -> None:
    created_at = created_at or _today_timestamp()
    conn.execute(
        "INSERT INTO beats_proposals (proposal, rationale, status, created_at) VALUES (?, ?, ?, ?)",
        (proposal, "synthetic", status, created_at),
    )
    conn.commit()


def _stub_drafter_script(tmp_path, *, body: str, exit_code: int = 0) -> str:
    """Write an executable shell script that records its args+stdin and exits."""
    record = tmp_path / "drafter.log"
    body_quoted = body.replace("'", "'\\''")
    script = tmp_path / "stub_drafter.sh"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/bin/sh
            printf '%s\\n' "$*" >> {record}
            cat >> {record}
            printf '\\n---END---\\n' >> {record}
            """
        )
        + f"printf '%s' '{body_quoted}'\n"
        + f"exit {exit_code}\n"
    )
    script.chmod(0o755)
    return str(script)


# --- compose() unit tests (deterministic skeleton + drafter injection) ----------------


def test_compose_quiet_day_is_one_concise_line():
    result = pulse.PulseResult(date=date(2026, 10, 4), quiet=True)
    text = pulse.compose(result, model="claude-sonnet-5-5")
    assert text == (
        "2026-10-04: Steady day; no book-level thesis invalidations or rung triggers active."
    )


def test_compose_quiet_day_never_invokes_drafter():
    """Quiet days must not shell out to a drafter even when one is provided."""
    called = []

    def trap(_prompt, _model):
        called.append(_model)
        return "should not be used"

    result = pulse.PulseResult(date=date(2026, 10, 4), quiet=True)
    text = pulse.compose(result, model="claude-sonnet-5-5", drafter=trap)
    assert called == []
    assert text == (
        "2026-10-04: Steady day; no book-level thesis invalidations or rung triggers active."
    )


def test_compose_active_day_invokes_drafter_with_explicit_model():
    """Two ``--model`` values must reach the drafter as distinct invocations."""
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
    captured: list[tuple[str, str]] = []

    def fake_drafter(prompt, model):
        captured.append((prompt, model))
        return f"2026-10-04: model {model} summary line"

    text_a = pulse.compose(result, model="model-a", drafter=fake_drafter)
    text_b = pulse.compose(result, model="model-b", drafter=fake_drafter)

    assert [c[1] for c in captured] == ["model-a", "model-b"]
    assert "model-a" in text_a and "model-a" not in text_b
    assert "model-b" in text_b and "model-b" not in text_a
    assert text_a != text_b


def test_compose_active_day_drafter_failure_raises_and_writes_no_file():
    """A drafter that raises must surface as PulseError; the caller writes nothing."""
    result = pulse.PulseResult(
        date=date(2026, 10, 4),
        quiet=False,
        high_bar=(
            pulse.PulseItem(
                kind=pulse.ITEM_KIND_FINDING,
                subject="thesis-x",
                detail="synthetic_trigger @ 2026-10-04T07:00:00.000000Z",
                score=0.95,
            ),
        ),
    )

    def failing_drafter(_prompt, _model):
        raise pulse.PulseError("drafter boom")

    with pytest.raises(pulse.PulseError, match="drafter boom"):
        pulse.compose(result, model="model-x", drafter=failing_drafter)


def test_compose_active_day_appends_deterministic_worth_discussing_block():
    """A model cannot drop the Worth discussing section: it is appended deterministically."""
    result = pulse.PulseResult(
        date=date(2026, 10, 4),
        quiet=False,
        high_bar=(
            pulse.PulseItem(
                kind=pulse.ITEM_KIND_CALL,
                subject="thesis-x",
                detail="call resolves 2026-10-04: x",
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
    text = pulse.compose(result, model="m", drafter=lambda _p, _m: "model prose summary")
    lines = text.splitlines()
    assert lines[0] == "model prose summary"
    assert "Worth discussing:" in lines
    assert any(line.startswith("- rung-y near") for line in lines)


def test_compose_active_day_empty_drafter_output_raises():
    """An empty drafter return value is treated as a failure, not silent fallback."""
    result = pulse.PulseResult(
        date=date(2026, 10, 4),
        quiet=False,
        high_bar=(
            pulse.PulseItem(
                kind=pulse.ITEM_KIND_FINDING,
                subject="thesis-x",
                detail="synthetic_trigger @ 2026-10-04T07:00:00.000000Z",
                score=0.95,
            ),
        ),
    )

    def empty_drafter(_prompt, _model):
        return ""

    with pytest.raises(pulse.PulseError, match="empty output"):
        pulse.compose(result, model="m", drafter=empty_drafter)


def test_compose_deterministic_includes_high_bar_and_worth_discussing():
    """``compose_deterministic`` shows the active-day skeleton without invoking a drafter."""
    result = pulse.PulseResult(
        date=date(2026, 10, 4),
        quiet=False,
        high_bar=(
            pulse.PulseItem(
                kind=pulse.ITEM_KIND_CALL,
                subject="thesis-x",
                detail="call resolves 2026-10-04: x",
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
    text = pulse.compose_deterministic(result)
    assert text.startswith("2026-10-04: book-level activity:")
    assert "- thesis-x" in text
    assert "Worth discussing:" in text
    assert "- rung-y near" in text


def test_compose_deterministic_quiet_day_is_one_line():
    result = pulse.PulseResult(date=date(2026, 10, 4), quiet=True)
    assert pulse.compose_deterministic(result) == (
        "2026-10-04: Steady day; no book-level thesis invalidations or rung triggers active."
    )


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


def test_collect_pulse_order_is_canonical_across_insertion_orders(tmp_path):
    """Two stores seeded with the same rows in opposite orders must emit equal results.

    Multiple findings share the same score so insertion order would otherwise leak
    through into ``high_bar``; the canonical sort must mask it.
    """
    today = date.fromisoformat(_today_iso())
    # Shared timestamps so the two databases end up byte-equal after seeding.
    ts_a = f"{_today_iso()}T07:00:00.000000Z"
    ts_b = f"{_today_iso()}T07:01:00.000000Z"
    ts_c = f"{_today_iso()}T07:02:00.000000Z"
    ts_d = f"{_today_iso()}T07:03:00.000000Z"

    def make_db_and_collect(insert_fn) -> pulse.PulseResult:
        path = tmp_path / f"db-{insert_fn.__name__}.db"
        assert main(["db", "init", "--db", str(path)]) == 0
        conn = store.connect(path)
        try:
            insert_fn(conn)
            return pulse.collect_pulse(conn, on=today)
        finally:
            conn.close()

    def forward(conn):
        _seed_finding(conn, subject="alpha finding", score=0.95, created_at=ts_a)
        _seed_finding(conn, subject="beta finding", score=0.85, created_at=ts_b)
        _seed_finding(conn, subject="gamma finding", score=0.85, created_at=ts_c)
        _seed_finding(conn, subject="delta finding", score=0.4, created_at=ts_d)

    def reverse(conn):
        _seed_finding(conn, subject="delta finding", score=0.4, created_at=ts_d)
        _seed_finding(conn, subject="gamma finding", score=0.85, created_at=ts_c)
        _seed_finding(conn, subject="beta finding", score=0.85, created_at=ts_b)
        _seed_finding(conn, subject="alpha finding", score=0.95, created_at=ts_a)

    forward_result = make_db_and_collect(forward)
    reverse_result = make_db_and_collect(reverse)

    assert forward_result == reverse_result
    # Canonical order: score desc → detail (timestamp) → kind → subject.
    # Same-score findings order by timestamp: ts_a < ts_b < ts_c.
    subjects_high = [item.subject for item in forward_result.high_bar]
    subjects_worth = [item.subject for item in forward_result.worth_discussing]
    assert subjects_high == ["alpha finding", "beta finding", "gamma finding"]
    assert subjects_worth == ["delta finding"]


# --- `hub pulse write` end-to-end -----------------------------------------------------


def test_quiet_day_writes_exactly_one_line(db, tmp_path, capsys):
    out_dir = tmp_path / "out"
    rc = main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)])
    assert rc == 0
    captured = capsys.readouterr()
    out_path = out_dir / (_today_iso() + ".md")
    assert f"pulse written: {out_path}" in captured.out
    body = out_path.read_text(encoding="utf-8")
    # Exactly one substantive line; no HTML-comment header, no blank lines.
    assert body.strip().splitlines() == [
        f"{_today_iso()}: Steady day; no book-level thesis invalidations or rung triggers active."
    ]
    # Provenance goes to stdout, not the artifact.
    assert "<!-- model:" not in body
    assert "model=" in captured.out


def test_active_day_writes_drafted_summary_and_worth_discussing(db, tmp_path, monkeypatch):
    """An active day writes the drafter's summary above the deterministic worth-discussing."""
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
        _seed_finding(conn, subject="rung-y near", score=0.4)
    finally:
        conn.close()

    monkeypatch.setenv(
        "HUB_PULSE_DRAFT_CMD", _stub_drafter_script(tmp_path, body="drafted prose summary\n")
    )
    out_dir = tmp_path / "out"
    rc = main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)])
    assert rc == 0
    out_path = out_dir / (_today_iso() + ".md")
    body = out_path.read_text(encoding="utf-8")
    assert "drafted prose summary" in body
    assert "- thesis-x invalidation" in body
    assert "Worth discussing:" in body
    assert "- rung-y near" in body
    assert "<!-- model:" not in body


def test_custom_date_and_out_dir_writes_to_target(db, tmp_path, monkeypatch):
    monkeypatch.setenv(
        "HUB_PULSE_DRAFT_CMD", _stub_drafter_script(tmp_path, body="custom-date prose\n")
    )
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
    assert "custom-date prose" in body
    assert "thesis-x invalidation" in body
    default_path = tmp_path / "default-book"
    assert not (default_path / "2026-09-15.md").exists()


def test_dry_run_does_not_write_file_and_skips_drafter(db, tmp_path, capsys, monkeypatch):
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
        _seed_finding(conn, subject="rung-y near", score=0.4)
    finally:
        conn.close()

    script_path = _stub_drafter_script(tmp_path, body="would not be called\n")
    monkeypatch.setenv("HUB_PULSE_DRAFT_CMD", script_path)

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
    assert not (tmp_path / "drafter.log").exists()

    captured = capsys.readouterr()
    assert "Stage 0 evidence:" in captured.out
    assert "Deterministic draft:" in captured.out
    assert "thesis-x invalidation" in captured.out
    assert "rung-y near" in captured.out
    assert "Worth discussing:" in captured.out


def test_model_option_is_passed_to_drafter(db, tmp_path, monkeypatch, capsys):
    """Two ``--model`` values produce two drafter invocations with distinct ``--model`` args."""
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
    finally:
        conn.close()

    script_path = _stub_drafter_script(tmp_path, body="summary\n")
    monkeypatch.setenv("HUB_PULSE_DRAFT_CMD", script_path)

    out_dir_a = tmp_path / "out-a"
    out_dir_b = tmp_path / "out-b"
    rc_a = main(
        [
            "pulse",
            "write",
            "--db",
            str(db),
            "--out-dir",
            str(out_dir_a),
            "--model",
            "model-a",
        ]
    )
    rc_b = main(
        [
            "pulse",
            "write",
            "--db",
            str(db),
            "--out-dir",
            str(out_dir_b),
            "--model",
            "model-b",
        ]
    )
    assert rc_a == 0 and rc_b == 0

    log = (tmp_path / "drafter.log").read_text()
    assert "--model" in log and "model-a" in log
    assert "model-b" in log
    # Two distinct invocations, each carrying its own --model value.
    assert log.count("--model") == 2


def test_quiet_day_never_invokes_drafter(db, tmp_path, capsys, monkeypatch):
    """A quiet day must not shell out, even when HUB_PULSE_DRAFT_CMD points at a stub."""
    script_path = _stub_drafter_script(tmp_path, body="unused\n")
    monkeypatch.setenv("HUB_PULSE_DRAFT_CMD", script_path)

    out_dir = tmp_path / "out"
    rc = main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)])
    assert rc == 0
    assert not (tmp_path / "drafter.log").exists()


def test_drafter_failure_exits_nonzero_and_writes_no_file(db, tmp_path, capsys, monkeypatch):
    """A failing drafter must surface as a nonzero exit and must not create the file."""
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
    finally:
        conn.close()

    script_path = _stub_drafter_script(
        tmp_path, body="", exit_code=7
    )  # stub wrote empty body and exits 7
    monkeypatch.setenv("HUB_PULSE_DRAFT_CMD", script_path)

    out_dir = tmp_path / "out"
    rc = main(
        [
            "pulse",
            "write",
            "--db",
            str(db),
            "--out-dir",
            str(out_dir),
            "--model",
            "model-x",
        ]
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "drafter" in err.lower()
    assert not list(out_dir.glob("*.md"))


def test_drafter_empty_output_exits_nonzero_and_writes_no_file(db, tmp_path, capsys, monkeypatch):
    """An empty drafter return value must surface as a nonzero exit and no file."""
    conn = store.connect(db)
    try:
        _seed_finding(conn, subject="thesis-x invalidation", score=0.95)
    finally:
        conn.close()

    script_path = tmp_path / "stub_empty.sh"
    script_path.write_text("#!/bin/sh\nexit 0\n")
    script_path.chmod(0o755)
    monkeypatch.setenv("HUB_PULSE_DRAFT_CMD", str(script_path))

    out_dir = tmp_path / "out"
    rc = main(["pulse", "write", "--db", str(db), "--out-dir", str(out_dir)])
    assert rc != 0
    err = capsys.readouterr().err
    assert "empty" in err.lower()
    assert not list(out_dir.glob("*.md"))


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


def test_default_drafter_missing_binary_raises_pulse_error(monkeypatch):
    monkeypatch.setenv("HUB_PULSE_DRAFT_CMD", "/definitely/not/a/real/binary")
    with pytest.raises(pulse.PulseError, match="binary not found"):
        pulse.default_drafter("prompt text", "model-a")
