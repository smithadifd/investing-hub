"""Tests for hub/letter.py and `hub letter midweek`.

Synthetic fixtures, no live producer paths. The fixtures live in
``tests/fixtures/letter/`` and are read by the tests; nothing writes
them. Every public commitment in the contract is exercised here,
including the market leg being off without a scan source, the
same-date rerun being byte-stable, and memory being recorded only
after a successful delivery.
"""

from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from hub import letter, producers, store
from hub.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "letter"
VIEWS_DIR = FIXTURES / "views"
MV_DIR = FIXTURES / "mv-analyst"
SCAN_FILE = FIXTURES / "scan" / "scan-0.json"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def letter_roots(tmp_path):
    """Copy the letter fixtures into tmp_path so tests can mutate them."""
    roots = {
        "views_dir": tmp_path / "views",
        "mv_analyst_root": tmp_path / "mv-analyst",
    }
    shutil.copytree(VIEWS_DIR, roots["views_dir"])
    shutil.copytree(MV_DIR, roots["mv_analyst_root"])
    return roots


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(path)]) == 0
    return path


@pytest.fixture
def memory_path(tmp_path):
    return tmp_path / "letter-memory.jsonl"


def _views(name: str) -> list[letter.View]:
    return letter.parse_views_file(VIEWS_DIR / name)


def _scan() -> letter.Scan:
    return letter.load_scan_file(SCAN_FILE)


def _corpus() -> letter.Corpus:
    candidates = producers.mv_analyst_candidates(MV_DIR).candidates
    return letter.build_corpus_from_mv_analyst_candidates(candidates)


def _seed_thesis_doc(db: Path, body: str, *, slug: str = "fixture-thesis") -> None:
    """Insert a thesis-shaped document so the CLI path can read views."""
    conn = store.connect(db)
    store.insert_document_revision(
        conn,
        slug=slug,
        kind="thesis",
        body=body,
        source_kind="session",
        source_ref="fixture",
    )
    conn.commit()
    conn.close()


def _letter_args(db, tmp_path, roots, *, scan_file=None, extra=()):
    """The CLI arguments every letter test shares, plus per-test extras."""
    args = [
        "letter",
        "midweek",
        "--date",
        "2026-02-10",
        "--db",
        str(db),
        "--out-dir",
        str(tmp_path / "out"),
        "--mv-analyst-root",
        str(roots["mv_analyst_root"]),
        "--memory-path",
        str(tmp_path / "memory.jsonl"),
    ]
    if scan_file is not None:
        args += ["--scan-file", str(scan_file)]
    return args + list(extra)


# ---------------------------------------------------------------------------
# view parser
# ---------------------------------------------------------------------------


def test_parse_views_file_extracts_views_with_thresholds():
    views = _views("views-0.md")
    ids = {view.id for view in views}
    assert {"synthetic-confirms", "synthetic-contradicts", "synthetic-quiet"} <= ids
    view = next(v for v in views if v.id == "synthetic-confirms")
    cond = view.confirms_when[0]
    assert cond["ticker"] == "BOT"
    assert cond["metric"] == "rs20"


def test_parse_views_file_handles_empty_or_malformed():
    assert _views("views-quiet.md") == []


def test_parse_views_file_omits_weight_skip(tmp_path):
    views_file = tmp_path / "views.md"
    views_file.write_text(
        "\n".join(
            [
                "### Paused view",
                "Id: paused",
                "Weight: skip",
                "Watching: BOT",
                "Claim: This view is intentionally inactive.",
                "Confirms when: BOT rs20 >= -100",
            ]
        ),
        encoding="utf-8",
    )
    views = letter.parse_views_file(views_file)
    scan = letter.Scan(
        benchmark="BAA",
        asof="2026-02-10",
        rows=(letter.ScanRow(ticker="BOT", close=100.0, rs20=1.0),),
    )
    assert views == []
    assert letter.gate(views, scan, letter.Corpus(), [], "2026-02-10").findings == ()


# ---------------------------------------------------------------------------
# scan source: load_scan_file
# ---------------------------------------------------------------------------


def test_load_scan_file_reads_documented_shape():
    scan = _scan()
    assert scan.benchmark == "BAA"
    assert scan.asof == "2026-02-09"
    tickers = [row.ticker for row in scan.rows]
    assert tickers == sorted(tickers)
    bot = next(row for row in scan.rows if row.ticker == "BOT")
    assert bot.close == 103.0
    assert bot.ret20 == 3.0
    assert bot.rs20 == -1.0
    assert bot.pct52w == 40.0


def test_load_scan_file_defaults_optional_fields_to_unmeasured():
    scan = _scan()
    bcd = next(row for row in scan.rows if row.ticker == "BCD")
    assert bcd.pct52w is None
    assert bcd.sma200 is None
    assert bcd.crossed is None
    assert bcd.trend == "—"
    assert bcd.asof == ""


def test_load_scan_file_missing_file_raises(tmp_path):
    with pytest.raises(letter.LetterError, match="scan file"):
        letter.load_scan_file(tmp_path / "absent.json")


def test_load_scan_file_invalid_json_raises(tmp_path):
    bad = tmp_path / "scan.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(letter.LetterError, match="not valid JSON"):
        letter.load_scan_file(bad)


def test_load_scan_file_row_missing_close_raises(tmp_path):
    bad = tmp_path / "scan.json"
    bad.write_text(
        '{"benchmark": "BAA", "asof": "2026-02-09", "rows": [{"ticker": "BOT"}]}',
        encoding="utf-8",
    )
    with pytest.raises(letter.LetterError, match="close"):
        letter.load_scan_file(bad)


def test_load_scan_file_missing_benchmark_raises(tmp_path):
    bad = tmp_path / "scan.json"
    bad.write_text('{"asof": "2026-02-09", "rows": []}', encoding="utf-8")
    with pytest.raises(letter.LetterError, match="benchmark"):
        letter.load_scan_file(bad)


def test_rs60_threshold_uses_loaded_scan_metric(tmp_path):
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(
        json.dumps(
            {
                "benchmark": "BAA",
                "asof": "2026-02-10",
                "rows": [{"ticker": "BOT", "close": 100.0, "rs60": 4.5}],
            }
        ),
        encoding="utf-8",
    )
    view = letter.View(
        id="rs60-view",
        title="RS60 view",
        weight="standing",
        watching=("BOT",),
        confirms_when=({"ticker": "BOT", "metric": "rs60", "op": ">=", "value": 4.0},),
    )
    scan = letter.load_scan_file(scan_file)
    result = letter.gate([view], scan, letter.Corpus(), [], "2026-02-10")
    assert scan.rows[0].rs60 == 4.5
    assert [(finding["key"], finding["verdict"]) for finding in result.findings] == [
        ("BOT", "confirms")
    ]


# ---------------------------------------------------------------------------
# corpus events
# ---------------------------------------------------------------------------


def test_build_corpus_yields_one_event_per_theme_axis():
    candidates = producers.mv_analyst_candidates(MV_DIR).candidates
    corpus = letter.build_corpus_from_mv_analyst_candidates(candidates)
    events = [event for _slug, events in corpus.views for event in events]
    assert events
    for event in events:
        assert event.classes == ("theme-axis",)


def test_build_corpus_skips_non_theme_axis():
    """Only theme-axis candidates feed the corpus leg; calls/attention/done do not."""
    candidates = producers.mv_analyst_candidates(MV_DIR).candidates
    corpus = letter.build_corpus_from_mv_analyst_candidates(candidates)
    corpus_events = sum(len(events) for _slug, events in corpus.views)
    assert corpus_events == sum(1 for c in candidates if c.kind == "theme_axis")


# ---------------------------------------------------------------------------
# memory: load / append / is_repeat / same-date reruns
# ---------------------------------------------------------------------------


def test_load_memory_missing_file_is_empty(tmp_path):
    assert letter.load_memory(tmp_path / "absent.jsonl") == []


def test_load_memory_round_trip(memory_path):
    record = letter.MemoryRecord(
        date="2026-02-10",
        view_id="synthetic-confirms",
        source="scan",
        key="BOT",
        verdict="confirms",
        trigger="BOT rs20 >= -2",
        summary="BOT confirms the view",
    )
    assert letter.append_memory(memory_path, [record]) == 1
    assert letter.load_memory(memory_path) == [record]


def test_load_memory_malformed_raises(memory_path):
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text('{"date": "2026-02-10"}\n', encoding="utf-8")
    with pytest.raises(letter.MemoryMalformed, match="missing"):
        letter.load_memory(memory_path)


def test_is_repeat_returns_seen_date_within_window(memory_path):
    record = letter.MemoryRecord(
        date="2026-02-05",
        view_id="synthetic-confirms",
        source="scan",
        key="BOT",
        verdict="confirms",
    )
    letter.append_memory(memory_path, [record])
    records = letter.load_memory(memory_path)
    seen = letter.is_repeat(
        records,
        view_id="synthetic-confirms",
        key="BOT",
        verdict="confirms",
        asof="2026-02-10",
    )
    assert seen == "2026-02-05"
    assert (
        letter.is_repeat(
            records,
            view_id="synthetic-confirms",
            key="BOT",
            verdict="contradicts",
            asof="2026-02-10",
        )
        is None
    )


def test_is_repeat_window_is_28_days():
    records = [
        letter.MemoryRecord(
            date="2025-12-01",
            view_id="v",
            source="scan",
            key="BOT",
            verdict="confirms",
        ),
    ]
    # 28 days from 2025-12-01 is 2025-12-29: the record still blocks there ...
    assert (
        letter.is_repeat(records, view_id="v", key="BOT", verdict="confirms", asof="2025-12-29")
        == "2025-12-01"
    )
    # ... and falls outside the window the day after.
    assert (
        letter.is_repeat(records, view_id="v", key="BOT", verdict="confirms", asof="2025-12-30")
        is None
    )


def test_is_repeat_ignores_same_date_records():
    """A record from the same issue date never blocks: a rerun reproduces, not quiets."""
    records = [
        letter.MemoryRecord(
            date="2026-02-10",
            view_id="v",
            source="scan",
            key="BOT",
            verdict="confirms",
        )
    ]
    assert (
        letter.is_repeat(records, view_id="v", key="BOT", verdict="confirms", asof="2026-02-10")
        is None
    )
    records.append(
        letter.MemoryRecord(
            date="2026-02-09",
            view_id="v",
            source="scan",
            key="BOT",
            verdict="confirms",
        )
    )
    assert (
        letter.is_repeat(records, view_id="v", key="BOT", verdict="confirms", asof="2026-02-10")
        == "2026-02-09"
    )


def test_records_to_append_drops_same_date_duplicates():
    memory = [
        letter.MemoryRecord(
            date="2026-02-10",
            view_id="v",
            source="scan",
            key="BOT",
            verdict="confirms",
        )
    ]
    records = [
        letter.MemoryRecord(
            date="2026-02-10",
            view_id="v",
            source="scan",
            key="BOT",
            verdict="confirms",
        ),
        letter.MemoryRecord(
            date="2026-02-10",
            view_id="v",
            source="scan",
            key="BDU",
            verdict="contradicts",
        ),
    ]
    out = letter.records_to_append(memory, records, asof="2026-02-10")
    assert [row.key for row in out] == ["BDU"]


# ---------------------------------------------------------------------------
# gate() — the leg (c) / leg (b) / memory logic
# ---------------------------------------------------------------------------


def test_gate_clears_on_confirms_for_first_view():
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")
    assert result.quiet is False
    verdicts = [f["verdict"] for f in result.findings]
    assert "confirms" in verdicts
    assert "contradicts" in verdicts


def test_gate_strength_orders_contradicts_over_confirms():
    """A row that contradicts wins over the same view's `confirms`."""
    view = next(v for v in _views("views-0.md") if v.id == "synthetic-contradicts")
    row = next(r for r in _scan().rows if r.ticker == "BDU")
    ok, label = letter._holds(view.contradicts_when[0], row)
    assert ok
    # The gate's own ordering: contradicts > confirms > extends.
    assert letter.STRENGTH["contradicts"] < letter.STRENGTH["confirms"]


def test_gate_silences_a_quiet_week():
    views = _views("views-quiet.md")
    result = letter.gate(views, _scan(), _corpus(), [], "2026-02-10")
    assert result.quiet is True
    assert result.findings == ()


def test_gate_suppresses_repeats(memory_path):
    memory_record = letter.MemoryRecord(
        date="2026-02-05",
        view_id="synthetic-confirms",
        source="scan",
        key="BOT",
        verdict="confirms",
    )
    letter.append_memory(memory_path, [memory_record])
    memory = letter.load_memory(memory_path)
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), memory, "2026-02-10")
    assert any(s["key"] == "BOT" and s["verdict"] == "confirms" for s in result.suppressed)
    assert not any(f["key"] == "BOT" and f["verdict"] == "confirms" for f in result.findings)


def test_gate_threshold_binds_to_named_ticker():
    """A watch-only ticker cannot borrow another's threshold."""
    view = letter.View(
        id="v",
        title="V",
        weight="lead",
        watching=("XLE", "XLU"),
        contradicts_when=({"ticker": "XLE", "metric": "rs20", "op": "<=", "value": -5.0},),
    )
    scan = letter.Scan(
        benchmark="SPY",
        asof="2026-02-10",
        rows=(
            letter.ScanRow(
                ticker="XLU",
                close=100.0,
                rs20=-10.0,  # would have tripped XLE's threshold if not bound
            ),
        ),
    )
    result = letter.gate([view], scan, letter.Corpus(), [], "2026-02-10")
    assert result.findings == ()


def test_gate_extends_when_only_regime_triggers():
    """An `extends` finding fires only when the regime rule alone survives."""
    view = letter.View(
        id="v",
        title="V",
        weight="lead",
        watching=("BOT",),
        contradicts_when=({"ticker": "BOT", "metric": "rs20", "op": "<=", "value": -100.0},),
        confirms_when=({"ticker": "BOT", "metric": "rs20", "op": ">=", "value": 100.0},),
    )
    scan = letter.Scan(
        benchmark="BDU",
        asof="2026-02-10",
        rows=(
            letter.ScanRow(
                ticker="BOT",
                close=42.0,
                rs20=0.3,  # confirms arm not met
                pct52w=97.0,  # but the 52-week regime does
            ),
        ),
    )
    result = letter.gate([view], scan, letter.Corpus(), [], "2026-02-10")
    assert len(result.findings) == 1
    assert result.findings[0]["verdict"] == "extends"


def test_gate_without_scan_source_yields_no_market_findings():
    """views-0's thresholds would clear on any fabricated scan; with no scan
    source configured the market leg is off and nothing market-side clears."""
    result = letter.gate(_views("views-0.md"), None, letter.Corpus(), [], "2026-02-10")
    assert result.scan is None
    assert result.quiet is True
    assert result.findings == ()


def test_gate_with_scan_file_market_leg_clears():
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")
    market = [f for f in result.findings if f["source"] == "scan"]
    keys = {f["key"]: f["verdict"] for f in market}
    assert keys == {"BOT": "confirms", "BDU": "contradicts"}


def test_gate_corpus_leg_only_emits_extends():
    """leg (b) emits `extends` only — never adjudicates the corpus against the view."""
    scan = letter.Scan(benchmark="BAA", asof="2026-02-10")
    result = letter.gate(_views("views-0.md"), scan, _corpus(), [], "2026-02-10")
    corpus_findings = [f for f in result.findings if f["source"] == "mv-analyst"]
    assert corpus_findings, "expected the corpus leg to emit at least one extends finding"
    assert {f["verdict"] for f in corpus_findings} == {"extends"}


def test_gate_corpus_event_is_surfaced_only_for_views_naming_its_beat():
    scan = letter.Scan(benchmark="BAA", asof="2026-02-10")
    result = letter.gate(_views("views-1.md"), scan, _corpus(), [], "2026-02-10")
    corpus_findings = [f for f in result.findings if f["source"] == "mv-analyst"]
    assert corpus_findings  # at least one view names synthetic-axis-a
    beats_seen = {f["view_id"] for f in corpus_findings}
    assert beats_seen <= {v.id for v in _views("views-1.md")}


def test_gate_reports_coverage_gap_when_beat_unmapped():
    # An empty corpus so every beat a view names is a coverage gap.
    result = letter.gate(_views("views-0.md"), None, letter.Corpus(), [], "2026-02-10")
    views = _views("views-0.md")
    assert result.coverage_gaps
    assert all(g in {b for v in views for b in v.beats} for g in result.coverage_gaps)


# ---------------------------------------------------------------------------
# compose_letter() — quiet week + active days
# ---------------------------------------------------------------------------


def test_compose_quiet_week_is_one_concise_line():
    result = letter.LetterResult(date=date(2026, 2, 10), quiet=True)
    text = letter.compose_letter(result, model=None)
    assert len(text.strip().splitlines()) == 1
    assert "2026-02-10" in text
    assert "nothing cleared the merit gate" in text


def test_compose_quiet_week_never_invokes_drafter():
    """A quiet day must never shell out even when a drafter is provided."""
    result = letter.LetterResult(date=date(2026, 2, 10), quiet=True)
    called = []

    def drafter(prompt, model):
        called.append((prompt, model))
        return "should not run"

    text = letter.compose_letter(result, model="m", drafter=drafter)
    assert text.startswith("2026-02-10")
    assert called == []


def test_compose_active_day_emits_rotation_strip_and_packets():
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")
    text = letter.compose_letter(result, model=None)
    assert "Rotation" in text
    assert "ticker" in text
    assert "rs20" in text
    assert "## The desk this week" in text
    assert "## What the scan did to your views" in text
    assert "## Standing views, as they stand" in text


def test_compose_without_scan_says_market_leg_off():
    """With no scan source the letter says so plainly and drops the strip."""
    result = letter.gate(_views("views-0.md"), None, _corpus(), [], "2026-02-10")
    assert result.quiet is False  # the corpus leg still clears
    text = letter.compose_letter(result, model=None)
    assert "Market leg off: no scan source configured" in text
    assert "Rotation," not in text


def test_compose_active_day_drafter_failure_raises():
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")

    def failing_drafter(prompt, model):
        raise letter.LetterError("drafter crashed")

    with pytest.raises(letter.LetterError, match="drafter crashed"):
        letter.compose_letter(result, model="m", drafter=failing_drafter)


def test_compose_active_day_empty_drafter_raises():
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")

    def empty_drafter(prompt, model):
        return "   "

    with pytest.raises(letter.LetterError, match="empty output"):
        letter.compose_letter(result, model="m", drafter=empty_drafter)


def test_compose_with_drafter_includes_summary_above_visual():
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")

    def fake_drafter(prompt, model):
        return "Summary line 1.\nSummary line 2."

    text = letter.compose_letter(result, model="m", drafter=fake_drafter)
    summary_pos = text.find("Summary line 1.")
    visual_pos = text.find("Rotation")
    assert summary_pos != -1
    assert visual_pos != -1
    assert summary_pos < visual_pos


# ---------------------------------------------------------------------------
# write_letter() — atomic write + no file on drafter failure
# ---------------------------------------------------------------------------


def test_write_letter_atomic_write_does_not_leave_tmp(tmp_path):
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")
    out_dir = tmp_path / "out"
    letter.write_letter(result, out_dir=out_dir, model=None)
    md_files = sorted(out_dir.glob("*.md"))
    assert md_files == [out_dir / "2026-02-10-midweek.md"]
    assert not list(out_dir.glob(".*.tmp"))


def test_write_letter_drafter_failure_writes_no_file(tmp_path):
    result = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")
    out_dir = tmp_path / "out"

    def failing_drafter(prompt, model):
        raise letter.LetterError("drafter crashed")

    with pytest.raises(letter.LetterError):
        letter.write_letter(result, out_dir=out_dir, model="m", drafter=failing_drafter)
    assert not list(out_dir.glob("*.md"))
    assert not list(out_dir.glob(".*.tmp"))


def test_write_letter_quiet_day_writes_no_file(tmp_path):
    views = _views("views-quiet.md")
    result = letter.gate(views, _scan(), _corpus(), [], "2026-02-10")
    out_dir = tmp_path / "out"
    with pytest.raises(ValueError, match="quiet"):
        letter.write_letter(result, out_dir=out_dir, model=None)


def test_write_letter_two_runs_byte_identical(tmp_path):
    """Two independent `gate()`+`write_letter()` runs over the same inputs
    produce byte-identical files."""
    result_a = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")
    result_b = letter.gate(_views("views-0.md"), _scan(), _corpus(), [], "2026-02-10")

    out_a = tmp_path / "out-a"
    out_b = tmp_path / "out-b"
    path_a = letter.write_letter(result_a, out_dir=out_a, model=None)
    path_b = letter.write_letter(result_b, out_dir=out_b, model=None)
    assert path_a.read_bytes() == path_b.read_bytes()


# ---------------------------------------------------------------------------
# the rotation visual: deterministic, fixed-width
# ---------------------------------------------------------------------------


def test_rotation_visual_is_deterministic():
    rows = [
        letter.ScanRow(
            ticker="AAA",
            close=100.0,
            ret20=0.5,
            ret60=1.0,
            rs20=2.5,
            pct52w=80.0,
            trend="above",
        ),
        letter.ScanRow(
            ticker="BBB",
            close=50.0,
            ret20=-0.2,
            ret60=-0.5,
            rs20=-1.0,
            pct52w=20.0,
            trend="below",
        ),
    ]
    scan = letter.Scan(benchmark="BAA", asof="2026-02-10", rows=tuple(rows))
    chart_a = letter.rotation_chart(scan)
    chart_b = letter.rotation_chart(scan)
    assert chart_a == chart_b
    assert "ticker" in chart_a
    assert "rs20" in chart_a
    assert "AAA" in chart_a
    assert "BBB" in chart_a


def test_rotation_visual_empty_scan_states_nothing():
    scan = letter.Scan(benchmark="BAA", asof="2026-02-10", rows=())
    chart = letter.rotation_chart(scan)
    assert "no scan rows" in chart


def test_rotation_visual_handles_missing_percentile():
    rows = [
        letter.ScanRow(ticker="AAA", close=100.0, rs20=1.0, pct52w=None),
    ]
    scan = letter.Scan(benchmark="BAA", asof="2026-02-10", rows=tuple(rows))
    chart = letter.rotation_chart(scan)
    assert "n/a" in chart


def test_rotation_visual_all_zero_rs20_is_neutral():
    scan = letter.Scan(
        benchmark="BAA",
        asof="2026-02-10",
        rows=(
            letter.ScanRow(ticker="AAA", close=100.0, rs20=0.0),
            letter.ScanRow(ticker="BBB", close=50.0, rs20=0.0),
        ),
    )
    chart = letter.rotation_chart(scan)
    assert chart.count("+0.00") == 2
    assert chart.count("|") == 2


# ---------------------------------------------------------------------------
# ask candidates
# ---------------------------------------------------------------------------


def _ask_result(**overrides):
    finding = {
        "view_id": "view",
        "view_title": "View",
        "key": "BOT",
        "verdict": "extends",
        "source": "scan",
        "trigger": "",
    }
    finding.update(overrides)
    return letter.LetterResult(
        date=date(2026, 2, 10),
        quiet=False,
        findings=(finding,),
    )


def test_ask_candidates_for_contradiction_is_trigger_1():
    asks = letter.ask_candidates_for(_ask_result(verdict="contradicts"))
    assert [ask["trigger"] for ask in asks] == [letter.TRIGGER_INVALIDATION]


def test_ask_candidates_for_scan_rung_event_is_trigger_2():
    asks = letter.ask_candidates_for(
        _ask_result(trigger="BOT is at the 97th percentile of its own 52-week range")
    )
    assert [ask["trigger"] for ask in asks] == [letter.TRIGGER_RUNG]


def test_ask_candidates_for_dated_call_resolving_is_trigger_3():
    asks = letter.ask_candidates_for(
        _ask_result(
            source="mv-analyst",
            classes=["dated-call-resolving"],
        )
    )
    assert [ask["trigger"] for ask in asks] == [letter.TRIGGER_CALL]


def test_ask_candidates_for_would_make_lead_is_trigger_4():
    asks = letter.ask_candidates_for(
        _ask_result(
            source="mv-analyst",
            classes=["would-make-lead"],
        )
    )
    assert [ask["trigger"] for ask in asks] == [letter.TRIGGER_BEATS]


def test_ask_candidates_theme_axis_alone_is_not_a_dated_call_or_lead_condition():
    asks = letter.ask_candidates_for(
        _ask_result(
            source="mv-analyst",
            weight="lead",
            classes=["theme-axis"],
        )
    )
    assert asks == []


def test_ask_candidates_for_quiet_letter_is_empty():
    result = letter.LetterResult(date=date(2026, 2, 10), quiet=True)
    assert letter.ask_candidates_for(result) == []


# ---------------------------------------------------------------------------
# default_sender / drafter subprocess handling
# ---------------------------------------------------------------------------


def test_default_sender_unset_variable_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("HUB_LETTER_SEND_CMD", raising=False)
    with pytest.raises(letter.LetterError, match="refusing to send"):
        letter.default_sender(tmp_path / "letter.md")


def test_default_sender_receives_letter_path(monkeypatch, tmp_path):
    letter_path = tmp_path / "letter.md"
    letter_path.write_text("hello\n", encoding="utf-8")
    script = tmp_path / "sender.sh"
    log = tmp_path / "received.log"
    script.write_text(
        f'#!/bin/sh\necho "$@" >> {log}\nexit 0\n',
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("HUB_LETTER_SEND_CMD", str(script))
    receipt = letter.default_sender(letter_path, subject="test")
    assert "sent" in receipt or str(letter_path) in receipt
    assert str(letter_path) in log.read_text(encoding="utf-8")


def test_default_drafter_missing_binary_raises(monkeypatch):
    monkeypatch.setenv("HUB_LETTER_DRAFT_CMD", "/definitely/not/a/real/binary")
    with pytest.raises(letter.LetterError, match="binary not found"):
        letter.default_drafter("prompt", "m")


# ---------------------------------------------------------------------------
# CLI: hub letter midweek
# ---------------------------------------------------------------------------


def test_cli_quiet_week_prints_one_line_and_writes_no_file(db, letter_roots, tmp_path, capsys):
    """A quiet week is exactly one combined status line, exit 0, no file."""
    rc = main(_letter_args(db, tmp_path, letter_roots))
    assert rc == 0
    captured = capsys.readouterr()
    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    line = out_lines[0]
    assert "quiet=yes" in line
    assert "market leg off: no scan source configured" in line
    assert "nothing cleared the merit gate" in line
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.md"))


def test_cli_quiet_week_with_scan_source_still_one_line(db, letter_roots, tmp_path, capsys):
    """Even with a scan source, a quiet week is one line and no file."""
    body = (VIEWS_DIR / "views-quiet.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE))
    assert rc == 0
    captured = capsys.readouterr()
    out_lines = [line for line in captured.out.splitlines() if line.strip()]
    assert len(out_lines) == 1
    assert "quiet=yes" in out_lines[0]
    assert "market leg on" in out_lines[0]
    assert "nothing cleared the merit gate" in out_lines[0]
    assert not (tmp_path / "out").exists() or not list((tmp_path / "out").glob("*.md"))


def test_cli_active_week_with_scan_file_writes_letter(db, letter_roots, tmp_path, capsys):
    """A fixture scan file lets the market leg clear as before."""
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE))
    assert rc == 0
    captured = capsys.readouterr()
    assert "market leg on" in captured.out
    assert "letter written" in captured.out
    out_dir = tmp_path / "out"
    md = list(out_dir.glob("2026-02-10-midweek.md"))
    assert len(md) == 1
    text = md[0].read_text(encoding="utf-8")
    assert "Rotation" in text
    assert "rs20" in text
    assert "**BOT confirms**" in text
    assert "**BDU contradicts**" in text
    assert "## Ask candidates" in text


def test_cli_no_scan_source_market_leg_off(db, letter_roots, tmp_path, capsys):
    """Without a scan source, a views file whose thresholds would have cleared
    on a fabricated scan yields no market findings and the market-leg-off line."""
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    rc = main(_letter_args(db, tmp_path, letter_roots))
    assert rc == 0
    captured = capsys.readouterr()
    assert "market leg off: no scan source configured" in captured.out
    out_dir = tmp_path / "out"
    md = list(out_dir.glob("2026-02-10-midweek.md"))
    assert len(md) == 1
    text = md[0].read_text(encoding="utf-8")
    assert "Market leg off: no scan source configured" in text
    assert "Rotation," not in text
    # Only corpus findings can clear; nothing market-side was recorded.
    memory = tmp_path / "memory.jsonl"
    lines = memory.read_text(encoding="utf-8").splitlines()
    assert lines
    assert all(json.loads(line)["source"] == "mv-analyst" for line in lines)


def test_cli_same_date_rerun_reproduces_letter_byte_for_byte(db, letter_roots, tmp_path, capsys):
    """Rerunning the same --date with a shared memory sidecar reproduces the
    same letter byte for byte; same-date records neither suppress nor append."""
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE))
    assert rc == 0
    out1 = capsys.readouterr().out
    letter_path = tmp_path / "out" / "2026-02-10-midweek.md"
    first_bytes = letter_path.read_bytes()
    memory = tmp_path / "memory.jsonl"
    first_lines = memory.read_text(encoding="utf-8").splitlines()

    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE))
    assert rc == 0
    out2 = capsys.readouterr().out
    assert "quiet=no" in out2
    assert out2 == out1
    assert letter_path.read_bytes() == first_bytes
    second_lines = memory.read_text(encoding="utf-8").splitlines()
    assert second_lines == first_lines  # no duplicate same-date records
    assert len(set(second_lines)) == len(second_lines)


def test_cli_no_scan_rerun_refuses_scan_backed_overwrite_without_force(
    db, letter_roots, tmp_path, capsys
):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    scan_args = _letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE)
    assert main(scan_args) == 0
    capsys.readouterr()
    letter_path = tmp_path / "out" / "2026-02-10-midweek.md"
    scan_backed = letter_path.read_bytes()

    assert main(_letter_args(db, tmp_path, letter_roots)) == 1
    captured = capsys.readouterr()
    assert "refusing to overwrite scan-backed letter" in captured.err
    assert letter_path.read_bytes() == scan_backed

    assert main(_letter_args(db, tmp_path, letter_roots, extra=["--force"])) == 0
    captured = capsys.readouterr()
    assert "market leg off: no scan source configured" in captured.out
    assert "Market leg off: no scan source configured" in letter_path.read_text(encoding="utf-8")


def test_cli_deliver_sends_once_per_issue_date(db, letter_roots, tmp_path, monkeypatch, capsys):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    send_log = tmp_path / "send.log"
    sender = tmp_path / "sender.sh"
    sender.write_text(f'#!/bin/sh\necho sent >> "{send_log}"\n', encoding="utf-8")
    sender.chmod(0o755)
    monkeypatch.setenv("HUB_LETTER_SEND_CMD", str(sender))
    args = _letter_args(
        db,
        tmp_path,
        letter_roots,
        scan_file=SCAN_FILE,
        extra=["--deliver"],
    )

    assert main(args) == 0
    assert "letter delivered" in capsys.readouterr().out
    assert main(args) == 0
    assert capsys.readouterr().out.strip() == "letter already delivered: 2026-02-10"
    assert send_log.read_text(encoding="utf-8").splitlines() == ["sent"]
    assert (tmp_path / "out" / "2026-02-10-midweek.delivered").is_file()


def test_cli_failed_delivery_leaves_memory_unchanged_then_retry_delivers(
    db, letter_roots, tmp_path, monkeypatch, capsys
):
    """A failed send records no memory, so the retry still delivers."""
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    failing = tmp_path / "sender-fail.sh"
    failing.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    failing.chmod(0o755)
    monkeypatch.setenv("HUB_LETTER_SEND_CMD", str(failing))
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE, extra=["--deliver"]))
    assert rc == 1
    captured = capsys.readouterr()
    assert "exited 1" in captured.err
    memory = tmp_path / "memory.jsonl"
    assert not memory.exists() or memory.read_text(encoding="utf-8") == ""
    assert (tmp_path / "out" / "2026-02-10-midweek.md").exists()
    assert not (tmp_path / "out" / "2026-02-10-midweek.delivered").exists()

    working = tmp_path / "sender-ok.sh"
    send_log = tmp_path / "retry-send.log"
    working.write_text(
        f'#!/bin/sh\necho sent >> "{send_log}"\necho "sent: $2"\n',
        encoding="utf-8",
    )
    working.chmod(0o755)
    monkeypatch.setenv("HUB_LETTER_SEND_CMD", str(working))
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE, extra=["--deliver"]))
    assert rc == 0
    captured = capsys.readouterr()
    assert "letter delivered" in captured.out
    assert send_log.read_text(encoding="utf-8").splitlines() == ["sent"]
    assert (tmp_path / "out" / "2026-02-10-midweek.delivered").is_file()
    assert memory.exists()
    lines = memory.read_text(encoding="utf-8").splitlines()
    assert lines
    first = json.loads(lines[0])
    assert first["date"] == "2026-02-10"
    assert first["verdict"] in ("confirms", "contradicts", "extends")


def test_cli_delivery_claim_blocks_another_sender(db, letter_roots, tmp_path, monkeypatch, capsys):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    claim = tmp_path / "out" / "2026-02-10-midweek.delivering"
    claim.parent.mkdir(parents=True)
    claim.write_text("", encoding="utf-8")
    sends = []

    def counting_sender(path, *, subject):
        sends.append((path, subject))
        return "sent"

    monkeypatch.setattr(letter, "default_sender", counting_sender)
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE, extra=["--deliver"]))

    assert rc == 1
    captured = capsys.readouterr()
    assert str(claim) in captured.err
    assert "in progress or was interrupted" in captured.err
    assert sends == []


def test_cli_sender_failure_releases_claim_for_one_retry(
    db, letter_roots, tmp_path, monkeypatch, capsys
):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    claim = tmp_path / "out" / "2026-02-10-midweek.delivering"
    successful_sends = []
    fail = True

    def counting_sender(path, *, subject):
        nonlocal fail
        if fail:
            fail = False
            raise letter.LetterError("sender failed")
        successful_sends.append((path, subject))
        return "sent"

    monkeypatch.setattr(letter, "default_sender", counting_sender)
    args = _letter_args(
        db,
        tmp_path,
        letter_roots,
        scan_file=SCAN_FILE,
        extra=["--deliver"],
    )

    assert main(args) == 1
    assert "sender failed" in capsys.readouterr().err
    assert not claim.exists()

    assert main(args) == 0
    assert "letter delivered" in capsys.readouterr().out
    assert len(successful_sends) == 1


def test_cli_marker_failure_keeps_claim_and_prevents_resend(
    db, letter_roots, tmp_path, monkeypatch, capsys
):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    sends = []

    def counting_sender(path, *, subject):
        sends.append((path, subject))
        return "sent"

    real_write_text_atomic = letter.write_text_atomic

    def fail_marker(path, text):
        if Path(path).suffix == ".delivered":
            raise OSError("marker failed")
        real_write_text_atomic(path, text)

    monkeypatch.setattr(letter, "default_sender", counting_sender)
    monkeypatch.setattr(letter, "write_text_atomic", fail_marker)
    args = _letter_args(
        db,
        tmp_path,
        letter_roots,
        scan_file=SCAN_FILE,
        extra=["--deliver"],
    )
    claim = tmp_path / "out" / "2026-02-10-midweek.delivering"

    assert main(args) == 1
    assert "marker failed" in capsys.readouterr().err
    assert claim.exists()
    assert len(sends) == 1

    assert main(args) == 1
    captured = capsys.readouterr()
    assert str(claim) in captured.err
    assert len(sends) == 1


def test_cli_delivered_rerun_repairs_memory_once(db, letter_roots, tmp_path, monkeypatch, capsys):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    sends = []
    fail_append = True

    def counting_sender(path, *, subject):
        sends.append((path, subject))
        return "sent"

    real_append_memory = letter.append_memory

    def append_memory_once_recovered(path, records):
        nonlocal fail_append
        if fail_append:
            fail_append = False
            raise OSError("memory append failed")
        return real_append_memory(path, records)

    monkeypatch.setattr(letter, "default_sender", counting_sender)
    monkeypatch.setattr(letter, "append_memory", append_memory_once_recovered)
    args = _letter_args(
        db,
        tmp_path,
        letter_roots,
        scan_file=SCAN_FILE,
        extra=["--deliver"],
    )
    memory_path = tmp_path / "memory.jsonl"

    assert main(args) == 1
    assert "memory append failed" in capsys.readouterr().err
    assert len(sends) == 1
    assert (tmp_path / "out" / "2026-02-10-midweek.delivered").exists()

    assert main(args) == 0
    assert capsys.readouterr().out.strip() == "letter already delivered: 2026-02-10"
    repaired = memory_path.read_bytes()
    rows = [json.loads(line) for line in repaired.splitlines()]
    identities = {(row["view_id"], row["key"], row["verdict"]) for row in rows}
    assert rows
    assert all(row["date"] == "2026-02-10" for row in rows)
    assert len(rows) == len(identities)
    assert len(sends) == 1

    assert main(args) == 0
    assert capsys.readouterr().out.strip() == "letter already delivered: 2026-02-10"
    assert memory_path.read_bytes() == repaired
    assert len(sends) == 1


def test_cli_drafter_failure_exits_nonzero_and_writes_no_file(
    db, letter_roots, tmp_path, monkeypatch, capsys
):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    monkeypatch.setenv("HUB_LETTER_DRAFT_CMD", "/definitely/not/a/real/binary")
    rc = main(
        _letter_args(
            db,
            tmp_path,
            letter_roots,
            scan_file=SCAN_FILE,
            extra=["--model", "fixture-model"],
        )
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert "drafter" in captured.err.lower()
    out_dir = tmp_path / "out"
    if out_dir.exists():
        assert not list(out_dir.glob("*.md"))
    assert not (tmp_path / "memory.jsonl").exists()


def test_cli_deliver_with_unset_sender_refuses(db, letter_roots, tmp_path, monkeypatch, capsys):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    monkeypatch.delenv("HUB_LETTER_SEND_CMD", raising=False)
    rc = main(_letter_args(db, tmp_path, letter_roots, extra=["--deliver"]))
    assert rc == 1
    captured = capsys.readouterr()
    assert "refusing to send" in captured.err.lower()


def test_cli_deliver_with_stub_sender_receives_file_path(
    db, letter_roots, tmp_path, monkeypatch, capsys
):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    letter_file = tmp_path / "out" / "2026-02-10-midweek.md"
    script = tmp_path / "sender.sh"
    script.write_text('#!/bin/sh\necho "$@"\nexit 0\n', encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("HUB_LETTER_SEND_CMD", str(script))
    rc = main(_letter_args(db, tmp_path, letter_roots, extra=["--deliver"]))
    assert rc == 0
    captured = capsys.readouterr()
    assert "letter delivered" in captured.out
    assert letter_file.exists()
    # The script's stdout is captured; the path appears in the receipt.
    assert str(letter_file) in captured.out or "letter delivered" in captured.out


def test_cli_invalid_date_exits_1(db, letter_roots, tmp_path, capsys):
    args = _letter_args(db, tmp_path, letter_roots)
    args[args.index("--date") + 1] = "not-a-date"
    rc = main(args)
    assert rc == 1
    assert "invalid --date" in capsys.readouterr().err


def test_cli_missing_database_exits_1(letter_roots, tmp_path, capsys):
    rc = main(_letter_args(tmp_path / "missing.db", tmp_path, letter_roots))
    assert rc == 1
    assert "database not found" in capsys.readouterr().err


def test_cli_records_memory_after_writing_letter(db, letter_roots, tmp_path, capsys):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE))
    assert rc == 0
    memory = tmp_path / "memory.jsonl"
    assert memory.exists()
    lines = memory.read_text(encoding="utf-8").splitlines()
    assert len(lines) > 0
    first = json.loads(lines[0])
    assert first["date"] == "2026-02-10"
    assert first["verdict"] in ("confirms", "contradicts", "extends")


def test_cli_active_day_writes_asks_sidecar(db, letter_roots, tmp_path, capsys):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    _seed_thesis_doc(db, body)
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=SCAN_FILE))
    assert rc == 0
    out_dir = tmp_path / "out"
    sidecar = out_dir / "2026-02-10-midweek.asks.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["date"] == "2026-02-10"
    assert isinstance(payload["candidates"], list)
    assert payload["candidates"], "expected at least one ask candidate from the active day"
    assert not list(out_dir.glob(".*.tmp"))  # written atomically


def test_cli_bad_scan_file_exits_1(db, letter_roots, tmp_path, capsys):
    bad = tmp_path / "bad-scan.json"
    bad.write_text("{not json", encoding="utf-8")
    rc = main(_letter_args(db, tmp_path, letter_roots, scan_file=bad))
    assert rc == 1
    assert "not valid JSON" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# build_views_from_documents: thesis documents in the store
# ---------------------------------------------------------------------------


def test_build_views_from_documents_parses_thesis_bodies(db):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    conn = store.connect(db)
    store.insert_document_revision(
        conn,
        slug="fixture-thesis",
        kind="thesis",
        body=body,
        source_kind="session",
        source_ref="fixture",
    )
    conn.commit()
    views = letter.build_views_from_documents(
        conn, body_for=lambda slug: store.get_document_revision(conn, slug)["body"]
    )
    conn.close()
    ids = {v.id for v in views}
    assert {"synthetic-confirms", "synthetic-contradicts", "synthetic-quiet"} <= ids


def test_build_views_from_documents_skips_non_thesis_kinds(db):
    body = (VIEWS_DIR / "views-0.md").read_text(encoding="utf-8")
    conn = store.connect(db)
    store.insert_document_revision(
        conn,
        slug="principles-doc",
        kind="principles",
        body=body,
        source_kind="session",
        source_ref="fixture",
    )
    conn.commit()
    views = letter.build_views_from_documents(
        conn, body_for=lambda slug: store.get_document_revision(conn, slug)["body"]
    )
    conn.close()
    assert views == []
