"""Producer adapter tests. Every fixture here is synthetic — no live producer paths.

Fixtures ship in ``tests/fixtures/producers/`` as read-only templates; each test
copies the directory tree into a tmp_path so a destructive edit cannot leak into
the next test or into a committed fixture.
"""

from __future__ import annotations

import json
import os
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
    done = [c for c in report.candidates if c.kind == "episode_done"]
    assert len(done) == 2
    summaries = sorted(c.summary for c in done)
    assert summaries == ["MV901 analysis ready", "MV902 analysis ready"]
    for candidate in done:
        assert candidate.as_of.endswith("Z")


def test_mv_analyst_uses_analysis_file_as_source_path_when_present(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    paths = sorted(c.source_path for c in report.candidates if c.kind == "episode_done")
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
    # A present-but-empty positions board: the root is complete, it just has
    # nothing to say. A root WITHOUT the board is degraded, tested below.
    (empty / "index").mkdir()
    (empty / "index" / "themes.md").write_text("# synthetic board\n")
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
    (broken / "index").mkdir()
    (broken / "index" / "themes.md").write_text("# synthetic board, no axis blocks\n")
    report = producers.mv_analyst_candidates(broken)
    assert report.status == producers.STATUS_OK
    assert [c.summary for c in report.candidates] == ["MV903 analysis ready"]


def test_mv_analyst_calls_index_yields_open_call_candidates(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    calls = [c for c in report.candidates if c.kind in ("open_call", "overdue_call")]
    # One overdue, one dated open and one undated open; the Scorecard and
    # Resolved sections are history and yield nothing.
    assert sorted((c.kind, c.summary) for c in calls) == [
        (
            "open_call",
            "Fixture Guest B — Synthetic upcoming call — threshold for fixture"
            " (resolves 2026-11-15)",
        ),
        (
            "open_call",
            "Fixture Guest C — Synthetic undated call — threshold for fixture",
        ),
        (
            "overdue_call",
            "Fixture Guest A — Synthetic overdue call — threshold for fixture (was due 2026-02-10)",
        ),
    ]
    for candidate in calls:
        assert candidate.source_path.endswith("index/calls.md")


def test_mv_analyst_calls_index_as_of_is_the_made_date_not_the_deadline(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    overdue = next(c for c in report.candidates if c.kind == "overdue_call")
    # Made 2026-01-02; the 2026-02-10 due date is a deadline, not provenance.
    assert overdue.as_of == "2026-01-02T00:00:00Z"
    upcoming = next(
        c for c in report.candidates if c.kind == "open_call" and "Fixture Guest B" in c.summary
    )
    assert upcoming.as_of == "2026-01-20T00:00:00Z"


def test_mv_analyst_attention_index_yields_one_candidate_per_beat(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    beats = [c for c in report.candidates if c.kind == "attention_beat"]
    assert sorted(c.summary for c in beats) == [
        "lead: Synthetic lead beat",
        "standing: Synthetic standing beat",
        "watch: Synthetic watch beat",
    ]
    for candidate in beats:
        assert candidate.source_path.endswith("index/attention.md")
        assert candidate.as_of  # the view carries no dates, so the file mtime


def test_mv_analyst_analysis_themes_yield_candidates(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    themes = [c for c in report.candidates if c.kind == "episode_themes"]
    assert sorted((c.summary, c.as_of) for c in themes) == [
        ("MV901 themes: inflation-path, long-end-yields", "2026-01-15T00:00:00Z"),
        ("MV902 themes: oil-price-formation", "2026-01-22T00:00:00Z"),
    ]
    for candidate in themes:
        assert candidate.source_path.endswith("-analysis.md")


def test_mv_analyst_themes_board_yields_one_candidate_per_axis(copy_fixtures):
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    board = [c for c in report.candidates if c.kind == "theme_axis"]
    assert sorted(c.summary for c in board) == [
        # An axis block with no rows is a real board state, still one candidate.
        "Gold as reserve asset (gold-reserve-asset): no readable position rows",
        "The inflation path (inflation-path): 1 position(s) — Fixture Guest B",
        "The long end (long-end-yields): 2 position(s) — Fixture Guest A, Fixture Guest C",
    ]
    by_summary = {c.summary: c.as_of for c in board}
    # as_of is the newest date on the block's own rows, not the file's mtime.
    assert (
        by_summary[
            "The long end (long-end-yields): 2 position(s) — Fixture Guest A, Fixture Guest C"
        ]
        == "2026-01-22T00:00:00Z"
    )
    assert (
        by_summary["The inflation path (inflation-path): 1 position(s) — Fixture Guest B"]
        == "2026-01-22T00:00:00Z"
    )
    # No date on the gold block, so the board file's mtime is the provenance.
    assert by_summary["Gold as reserve asset (gold-reserve-asset): no readable position rows"]
    for candidate in board:
        assert candidate.source_path.endswith("index/themes.md")


def test_mv_analyst_changed_board_position_yields_a_changed_candidate(copy_fixtures):
    """A cross-guest position change on the board moves a candidate, not silence."""
    board_path = copy_fixtures["mv_analyst_root"] / "index" / "themes.md"
    board_path.write_text(
        board_path.read_text(encoding="utf-8").replace(
            "Fixture Guest C | The long end is priced for scarcity, not default",
            "Fixture Guest C | The long end is now a scarcity default, revised stance",
        )
    )
    report = producers.mv_analyst_candidates(copy_fixtures["mv_analyst_root"])
    long_end = next(
        c for c in report.candidates if c.kind == "theme_axis" and "long-end" in c.summary
    )
    assert "Fixture Guest C" in long_end.summary
    # The front-matter tags alone say nothing about who stands where; the
    # board is the source that moved.
    assert any(c.kind == "episode_themes" for c in report.candidates)


def test_mv_analyst_missing_themes_board_is_degraded_not_a_crash(tmp_path):
    root = tmp_path / "mv-analyst-no-board"
    (root / "state" / "done").mkdir(parents=True)
    (root / "index").mkdir()
    (root / "index" / "calls.md").write_text("# synthetic calls, no rows\n")
    report = producers.mv_analyst_candidates(root)
    assert report.status == producers.STATUS_DEGRADED
    assert report.candidates == []
    assert len(report.notes) == 1
    assert "index/themes.md missing" in report.notes[0]


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


def test_week_ahead_open_call_as_of_is_the_made_date(copy_fixtures):
    report = producers.week_ahead_candidates(copy_fixtures["week_ahead_root"])
    by_summary = {c.summary: c.as_of for c in report.candidates}
    # Made 2026-01-12 resolves 2026-01-20: provenance is the record's own date,
    # never the future deadline it resolves by.
    assert by_summary["Synthetic call A — threshold for fixture"] == "2026-01-12T00:00:00Z"
    assert by_summary["Synthetic call C — also open"] == "2026-01-14T00:00:00Z"


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


def test_week_ahead_beat_without_weight_inserts_once_across_two_reads(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    beats_path = copy_fixtures["week_ahead_root"] / "beats.md"
    beats_path.write_text(
        beats_path.read_text(encoding="utf-8") + "\n### Unweighted fixture beat\n"
        "What I care about: a synthetic beat with no Weight line.\n"
    )
    producers.week_ahead_candidates(copy_fixtures["week_ahead_root"], conn=conn)
    producers.week_ahead_candidates(copy_fixtures["week_ahead_root"], conn=conn)
    rows = list(
        conn.execute(
            "SELECT proposal, rationale FROM beats_proposals"
            " WHERE proposal = 'Unweighted fixture beat'"
        )
    )
    # A NULL rationale must still dedup: `= NULL` never matches an existing row.
    assert len(rows) == 1
    assert rows[0]["rationale"] is None


def test_week_ahead_beats_are_read_via_the_reference_reader_not_parsed_locally(
    copy_fixtures, fresh_db
):
    """The proposals come from ``scripts/read_beats.py`` output across the boundary.

    The stub reader emits a beat that exists nowhere in ``beats.md``: only a
    hub that consumes the reader's output can store it, and a hub with its own
    parser cannot.
    """
    conn, _db_path = fresh_db
    root = copy_fixtures["week_ahead_root"]
    reader = root / "scripts" / "read_beats.py"
    reader.write_text(
        "import json\n"
        'print(json.dumps({"beats": [{"name": "Reader-side beat",'
        ' "weight_raw": "watch"}], "warnings": []}))\n',
        encoding="utf-8",
    )
    report = producers.week_ahead_candidates(root, conn=conn)
    assert report.status == producers.STATUS_OK
    rows = list(conn.execute("SELECT proposal, rationale FROM beats_proposals"))
    assert [tuple(r) for r in rows] == [("Reader-side beat", "watch")]


def test_week_ahead_reader_warnings_surface_in_the_status_notes(copy_fixtures):
    report = producers.week_ahead_candidates(copy_fixtures["week_ahead_root"])
    assert report.status == producers.STATUS_OK
    # The fixture beats.md duplicates all three names; the reference reader
    # keeps both and warns, and the hub passes the warning through instead of
    # quietly collapsing the collision into one stored row.
    assert sum("duplicate name" in note for note in report.notes) == 3


def test_week_ahead_missing_reader_script_is_degraded_with_no_proposals(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    root = copy_fixtures["week_ahead_root"]
    (root / "scripts" / "read_beats.py").unlink()
    report = producers.week_ahead_candidates(root, conn=conn)
    assert report.status == producers.STATUS_DEGRADED
    assert report.notes and "reader missing" in report.notes[0]
    # The ledger candidates still ship; only the beats read failed.
    assert {c.summary for c in report.candidates} == {
        "Synthetic call A — threshold for fixture",
        "Synthetic call C — also open",
    }
    rows = list(conn.execute("SELECT proposal FROM beats_proposals"))
    assert rows == []


def test_week_ahead_failing_reader_is_degraded_with_no_proposals(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    root = copy_fixtures["week_ahead_root"]
    (root / "scripts" / "read_beats.py").write_text(
        "import sys\nsys.stderr.write('beats: MALFORMED - synthetic failure\\n')\nsys.exit(4)\n",
        encoding="utf-8",
    )
    report = producers.week_ahead_candidates(root, conn=conn)
    assert report.status == producers.STATUS_DEGRADED
    assert "beats reader exit 4" in report.notes[0]
    assert "synthetic failure" in report.notes[0]
    rows = list(conn.execute("SELECT proposal FROM beats_proposals"))
    assert rows == []


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


def test_triage_queue_partial_final_line_is_read_once_completed(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    path = copy_fixtures["triage_queue_dir"] / "primary.jsonl"
    producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    # Append a record without its newline — the writer is mid-append.
    with open(path, "ab") as fh:
        fh.write(
            json.dumps(
                {
                    "queued": "2026-02-05T09:30:00+00:00",
                    "account": "primary",
                    "message_id": "msg-005",
                    "sender": "Fixture Sender E",
                    "subject": "Synthetic partial record",
                    "source_rule": "fixture-rule-e",
                }
            ).encode()
        )
    mid_append = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert mid_append.candidates == []  # a partial line is never consumed
    # The newline lands; the completed record is read exactly once.
    with open(path, "ab") as fh:
        fh.write(b"\n")
    completed = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert [c.summary for c in completed.candidates] == ["primary — Synthetic partial record"]
    again = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert again.candidates == []


def test_triage_queue_same_size_rotation_yields_new_records(copy_fixtures, fresh_db):
    conn, _db_path = fresh_db
    path = copy_fixtures["triage_queue_dir"] / "primary.jsonl"
    producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    # Rewrite the file in place to the exact same byte size — a rotation that
    # neither grows nor shrinks the file. Only a content identity catches it.
    size = path.stat().st_size
    line = json.dumps(
        {
            "queued": "2026-03-02T00:00:00+00:00",
            "account": "primary",
            "message_id": "msg-rotated",
            "sender": "Fixture Sender Rotation",
            "subject": "Rotated record",
            "source_rule": None,
        }
    ).encode()
    padding = size - len(line) - 1  # json tolerates trailing spaces on the line
    assert padding >= 0
    path.write_bytes(line + b" " * padding + b"\n")
    assert path.stat().st_size == size
    second = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert [c.summary for c in second.candidates] == ["primary — Rotated record"]


def test_triage_queue_same_size_rewrite_keeping_line_1_yields_changed_records(
    copy_fixtures, fresh_db
):
    """An in-place rewrite that keeps line 1 and the size is still a replacement.

    The first-line hash alone cannot see it — line 1 is untouched — so the
    cursor must also treat a same-size mtime change as a rewrite and restart
    from zero rather than resuming past the changed records.
    """
    conn, _db_path = fresh_db
    path = copy_fixtures["triage_queue_dir"] / "primary.jsonl"
    first = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert len(first.candidates) == 3
    lines = path.read_bytes().splitlines(keepends=True)
    replacement = json.dumps(
        {
            "queued": "2026-02-02T09:30:00+00:00",
            "account": "primary",
            "message_id": "msg-002",
            "sender": "Fixture Sender B",
            "subject": "Rewritten record B",
            "source_rule": "fixture-rule-b",
        }
    ).encode()
    padding = len(lines[1]) - len(replacement) - 1  # trailing spaces keep the size
    assert padding >= 0
    path.write_bytes(lines[0] + replacement + b" " * padding + b"\n" + lines[2])
    assert path.stat().st_size == sum(len(line) for line in lines)
    # Same inode, same size, same first line: force the mtime to move the way
    # any real rewrite does, deterministically rather than at ns resolution.
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    second = producers.triage_queue_candidates(copy_fixtures["triage_queue_dir"], conn=conn)
    assert [c.summary for c in second.candidates] == [
        "primary — Synthetic triage record A",
        "primary — Rewritten record B",
        "primary — Synthetic triage record C",
    ]


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


def test_hub_producers_list_prints_every_candidate(copy_fixtures, fresh_db, capsys):
    conn, db_path = fresh_db
    conn.close()
    rc = main(
        [
            "producers",
            "list",
            "--db",
            str(db_path),
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
    assert "mv-analyst: ok — 13 candidate(s)" in out
    assert "week-ahead: ok — 2 candidate(s)" in out
    assert "triage-queue: ok — 3 candidate(s)" in out
    assert "MV901 analysis ready" in out
    assert "primary — Synthetic triage record A" in out


def test_hub_producers_list_is_stateful_across_runs(copy_fixtures, fresh_db, capsys):
    """The CLI path persists: a second run yields no repeat triage candidates."""
    conn, db_path = fresh_db
    conn.close()
    argv = [
        "producers",
        "list",
        "--db",
        str(db_path),
        "--mv-analyst-root",
        str(copy_fixtures["mv_analyst_root"]),
        "--week-ahead-root",
        str(copy_fixtures["week_ahead_root"]),
        "--triage-queue-dir",
        str(copy_fixtures["triage_queue_dir"]),
    ]
    assert main(argv) == 0
    first = capsys.readouterr().out
    assert "triage-queue: ok — 3 candidate(s)" in first
    assert main(argv) == 0
    second = capsys.readouterr().out
    assert "triage-queue: empty — 0 candidate(s)" in second
    assert "triage-queue: ok — 3 candidate(s)" not in second
    # The beat proposals were written once, not re-inserted by the second run.
    conn = store.connect(db_path)
    try:
        proposals = [row[0] for row in conn.execute("SELECT proposal FROM beats_proposals")]
    finally:
        conn.close()
    assert sorted(proposals) == ["Energy", "Rates and the curve", "Volatility"]


def test_hub_producers_list_reports_absent_producers(tmp_path, capsys, fresh_db):
    conn, db_path = fresh_db
    conn.close()
    rc = main(
        [
            "producers",
            "list",
            "--db",
            str(db_path),
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
    # Pin the literal producer data homes, not the module constants: pointing a
    # default back at a ~/code checkout must fail here, not ship.
    assert ns.mv_analyst_root == Path.home() / "mv-analyst"
    assert ns.week_ahead_root == Path.home() / "week-ahead"
    assert ns.triage_queue_dir == Path.home() / "brief" / "investing-triage-queue"
