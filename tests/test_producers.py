"""Producer adapter tests. Every fixture here is synthetic — no live producer paths.

Fixtures ship in ``tests/fixtures/producers/`` as read-only templates; each test
copies the directory tree into a tmp_path so a destructive edit cannot leak into
the next test or into a committed fixture.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hub import producers, store
from hub.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "producers"
MV_TEMPLATE = FIXTURES / "mv-analyst"
WA_TEMPLATE = FIXTURES / "week-ahead"
TQ_TEMPLATE = FIXTURES / "triage-queue"


@pytest.fixture
def fresh_db(tmp_path):
    """A migrated hub.db in a temp directory the tests own."""
    db_path = tmp_path / "data" / "hub.db"
    conn = store.connect(db_path)
    store.migrate(conn)
    yield conn, db_path
    conn.close()


@pytest.fixture
def copy_fixtures(tmp_path):
    """Copy the three fixture roots into ``tmp_path/...`` so tests can mutate them."""
    roots = {
        "mv_analyst_root": tmp_path / "mv-analyst",
        "week_ahead_root": tmp_path / "week-ahead",
        "triage_queue_dir": tmp_path / "triage-queue",
    }
    shutil.copytree(MV_TEMPLATE, roots["mv_analyst_root"])
    shutil.copytree(WA_TEMPLATE, roots["week_ahead_root"])
    shutil.copytree(TQ_TEMPLATE, roots["triage_queue_dir"])
    return roots


def _write_records(path: Path, records: list[dict]) -> None:
    """Append one JSON line per record to ``path``."""
    with open(path, "a", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=True) + "\n")


# --- shared candidate contract ---------------------------------------------------------------


def test_every_candidate_carries_provenance(copy_fixtures):
    """Provenance: producer + source_path + as_of, plus summary and kind for the sweep."""
    mv_report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    wa_report = producers.week_ahead_candidates(copy_fixtures["week_ahead_root"])
    tq_report = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"])
    for report in (mv_report, wa_report, tq_report):
        for candidate in report.candidates:
            assert candidate.producer == report.producer
            assert candidate.source_path and Path(candidate.source_path).is_file()
            assert candidate.as_of
            assert candidate.summary
            assert candidate.kind


# --- mv-analyst -------------------------------------------------------------------------------


def test_mv_analyst_one_candidate_per_done_marker(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    assert report.producer == producers.MV_ANALYST
    assert report.status == producers.STATUS_OK
    assert len(report.candidates) == 2
    summaries = sorted(c.summary for c in report.candidates)
    assert summaries == ["MV901 analysis ready", "MV902 analysis ready"]
    for candidate in report.candidates:
        assert candidate.kind == "episode_done"
        assert candidate.as_of.endswith("Z")


def test_mv_analyst_uses_analysis_file_as_source_path_when_present(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    paths = sorted(c.source_path for c in report.candidates)
    # mv901 and mv902 each have a matching analysis file in the fixture.
    assert any(p.endswith("2026-01-15-mv901-a-analysis.md") for p in paths)
    assert any(p.endswith("2026-01-22-mv902-b-analysis.md") for p in paths)


def test_mv_analyst_missing_root_is_absent_not_exception(tmp_path):
    report = producers.mv_analyst_candidates(tmp_path / "does-not-exist")
    assert report.producer == producers.MV_ANALYST
    assert report.status == producers.STATUS_ABSENT
    assert report.candidates == []


def test_mv_analyst_empty_done_dir_is_empty(tmp_path):
    empty = tmp_path / "mv-analyst-empty"
    (empty / "state" / "done").mkdir(parents=True)
    report = producers.mv_analyst_candidates(empty)
    assert report.status == producers.STATUS_EMPTY
    assert report.candidates == []


def test_mv_analyst_malformed_marker_is_skipped_not_a_failure(tmp_path):
    broken = tmp_path / "mv-analyst-broken"
    done = broken / "state" / "done"
    done.mkdir(parents=True)
    (done / "mv903").write_text("2026-02-05T10:00:00+0000")  # valid
    (done / "garbage").write_text("not a marker")  # name rejected
    (done / "mv000").write_text("2026-02-05T10:00:00+0000")  # episode 0 rejected
    (done / "mv904").write_text("not a timestamp")  # content rejected
    report = producers.mv_analyst_candidates(broken)
    assert report.status == producers.STATUS_OK
    assert [c.summary for c in report.candidates] == ["MV903 analysis ready"]


# --- week-ahead --------------------------------------------------------------------------------


def test_week_ahead_open_call_rows_become_candidates(copy_fixtures):
    report = producers.week_ahead_candidates(copy_fixtures["week_ahead_root"])
    assert report.producer == producers.WEEK_AHEAD
    assert report.status == producers.STATUS_OK
    # Two of the four fixture rows are 'open'; the others are resolved.
    assert {c.summary for c in report.candidates} == {
        "Synthetic call A — threshold for fixture",
        "Synthetic call C — also open",
    }
    for candidate in report.candidates:
        assert candidate.kind == "open_call"
        assert candidate.source_path.endswith("calls.md")
        assert candidate.as_of.startswith("2026-")


def test_week_ahead_writes_one_beat_proposal_per_beat_when_conn_supplied(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    report = producers.week_ahead_candidates(copy_fixtures["week_ahead_root"], conn=conn)
    assert report.status == producers.STATUS_OK
    rows = list(conn.execute("SELECT proposal, rationale FROM beats_proposals ORDER BY proposal"))
    assert [tuple(r) for r in rows] == [
        ("Energy", "standing"),
        ("Rates and the curve", "lead"),
        ("Volatility", "watch"),
    ]


def test_week_ahead_repeat_write_does_not_duplicate(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    producers.week_ahead_candidates(copy_fixtures["week_ahead_root"], conn=conn)
    producers.week_ahead_candidates(copy_fixtures["week_ahead_root"], conn=conn)
    rows = list(conn.execute("SELECT proposal FROM beats_proposals ORDER BY proposal"))
    assert [r[0] for r in rows] == [
        "Energy",
        "Rates and the curve",
        "Volatility",
    ]


def test_week_ahead_does_not_write_beats_md():
    """The contract says beats proposals go to ``beats_proposals`` and nowhere else."""
    beats_path = WA_TEMPLATE / "beats.md"
    before = beats_path.read_bytes()
    before_mtime = beats_path.stat().st_mtime_ns
    conn = store.connect(":memory:")
    store.migrate(conn)
    try:
        producers.week_ahead_candidates(WA_TEMPLATE, conn=conn)
    finally:
        conn.close()
    assert beats_path.read_bytes() == before
    assert beats_path.stat().st_mtime_ns == before_mtime


def test_week_ahead_missing_root_is_absent(tmp_path):
    report = producers.week_ahead_candidates(tmp_path / "does-not-exist")
    assert report.status == producers.STATUS_ABSENT
    assert report.candidates == []


def test_week_ahead_root_with_no_ledger_is_empty(tmp_path):
    wa_no_ledger = tmp_path / "week-ahead-empty"
    wa_no_ledger.mkdir()
    report = producers.week_ahead_candidates(wa_no_ledger)
    assert report.status == producers.STATUS_EMPTY
    assert report.candidates == []


# --- triage-queue ------------------------------------------------------------------------------


def test_triage_queue_first_read_returns_every_record(copy_fixtures):
    report = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"])
    assert report.producer == producers.TRIAGE_QUEUE
    assert report.status == producers.STATUS_OK
    assert len(report.candidates) == 3
    for candidate in report.candidates:
        assert candidate.kind == "triage_record"
        assert candidate.source_path.endswith("primary.jsonl")
        assert candidate.as_of


def test_triage_queue_second_read_without_new_lines_yields_no_repeats(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    first = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert len(first.candidates) == 3
    second = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert second.candidates == []


def test_triage_queue_advances_after_appending_new_lines(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    path = copy_fixtures["triage_queue_dir"] / "primary.jsonl"
    initial = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert len(initial.candidates) == 3
    _write_records(
        path,
        [
            {
                "queued": "2026-02-04T11:00:00+00:00",
                "account": "primary",
                "message_id": "msg-004",
                "sender": "Fixture Sender D",
                "subject": "Synthetic triage record D",
                "source_rule": "fixture-rule-d",
            }
        ],
    )
    after = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert len(after.candidates) == 1
    assert after.candidates[0].summary == "primary — Synthetic triage record D"


def test_triage_queue_never_modifies_the_jsonl_files(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    path = copy_fixtures["triage_queue_dir"] / "primary.jsonl"
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime


def test_triage_queue_missing_dir_is_absent(tmp_path):
    report = producers.triage_queue_candidates(tmp_path / "does-not-exist")
    assert report.status == producers.STATUS_ABSENT
    assert report.candidates == []


def test_triage_queue_empty_dir_is_empty(tmp_path):
    tq_empty = tmp_path / "triage-queue-empty"
    tq_empty.mkdir()
    report = producers.triage_queue_candidates(tq_empty)
    assert report.status == producers.STATUS_EMPTY
    assert report.candidates == []


def test_triage_queue_reset_when_file_shrinks(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    path = copy_fixtures["triage_queue_dir"] / "primary.jsonl"
    producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    # Truncate the file to one line and rewrite — simulates a rolled-over file.
    new_text = (
        json.dumps(
            {
                "queued": "2026-03-01T00:00:00+00:00",
                "account": "primary",
                "message_id": "msg-rollover",
                "sender": "Fixture Sender Rollover",
                "subject": "Rollover record",
                "source_rule": None,
            }
        )
        + "\n"
    )
    path.write_text(new_text)
    second = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert [c.summary for c in second.candidates] == ["primary — Rollover record"]


# --- session-open integration ------------------------------------------------------------------


def test_status_reports_returns_one_per_producer(copy_fixtures):
    reports = producers.status_reports(
        mv_analyst_root=copy_fixtures["mv_analyst_root"],
        week_ahead_root=copy_fixtures["week_ahead_root"],
        triage_queue_dir=copy_fixtures["triage_queue_dir"],
    )
    assert [r.producer for r in reports] == [
        producers.MV_ANALYST,
        producers.WEEK_AHEAD,
        producers.TRIAGE_QUEUE,
    ]
    assert all(r.status == producers.STATUS_OK for r in reports)


def test_status_reports_handles_all_missing_roots(tmp_path):
    reports = producers.status_reports(
        mv_analyst_root=tmp_path / "nope-mv",
        week_ahead_root=tmp_path / "nope-wa",
        triage_queue_dir=tmp_path / "nope-tq",
    )
    assert [r.status for r in reports] == [
        producers.STATUS_ABSENT,
        producers.STATUS_ABSENT,
        producers.STATUS_ABSENT,
    ]


# --- CLI surface -------------------------------------------------------------------------------


def test_hub_producers_list_prints_every_candidate(copy_fixtures, capsys):
    rc = main(
        [
            "producers",
            "list",
            "--mv-analyst-root",
            str(copy_fixtures["mv_analyst_root"]),
            "--week-ahead-root",
            str(copy_fixtures["week_ahead_root"]),
            "--triage-queue-dir",
            str(copy_fixtures["triage_queue_dir"]),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mv-analyst: ok — 2 candidate(s)" in out
    assert "week-ahead: ok — 2 candidate(s)" in out
    assert "triage-queue: ok — 3 candidate(s)" in out
    assert "MV901 analysis ready" in out
    assert "primary — Synthetic triage record A" in out


def test_hub_producers_list_reports_absent_producers(tmp_path, capsys):
    rc = main(
        [
            "producers",
            "list",
            "--mv-analyst-root",
            str(tmp_path / "nope-mv"),
            "--week-ahead-root",
            str(tmp_path / "nope-wa"),
            "--triage-queue-dir",
            str(tmp_path / "nope-tq"),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "mv-analyst: absent" in out
    assert "week-ahead: absent" in out
    assert "triage-queue: absent" in out


# --- helpers -----------------------------------------------------------------------------------


def test_producers_list_arg_defaults_are_the_home_paths():
    """The default roots are the producer home directories; an explicit override wins."""
    import argparse

    from hub.cli import _producers_list_args

    parser = argparse.ArgumentParser()
    _producers_list_args(parser)
    ns = parser.parse_args([])
    assert ns.mv_analyst_root == producers.DEFAULT_MV_ANALYST_ROOT
    assert ns.week_ahead_root == producers.DEFAULT_WEEK_AHEAD_ROOT
    assert ns.triage_queue_dir == producers.DEFAULT_TRIAGE_QUEUE_DIR
