"""week-ahead adapter — read-only against ``ledger/calls.md`` and ``beats.md``.

Open call rows in the ledger become candidates. ``beats.md`` is read through
week-ahead's own reference reader (``scripts/read_beats.py --json``; the read
contract is that repo's ``docs/beats-schema.md``) rather than parsed here —
beats.md is hand-written prose with a convention, and two parsers of
hand-written prose disagree eventually, so the hub calls the one shipped
reader across the repo boundary. Each beat the reader returns becomes a
``beats_proposals`` row when a database connection is supplied, and the
reader's warnings and duplicate-name ambiguities surface in the producer
status line rather than being dropped. Week-ahead's own ``beats.md`` is never
written by this adapter — the proposal table is the only place a proposed
beat lives, exactly as the row contract says.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from hub.producers.common import (
    DEFAULT_WEEK_AHEAD_ROOT,
    STATUS_ABSENT,
    STATUS_DEGRADED,
    STATUS_EMPTY,
    STATUS_OK,
    WEEK_AHEAD,
    Candidate,
    ProducerReport,
    as_iso_date,
    file_mtime_iso,
    truncate,
)

# The reader is a subprocess the hub waits on; without a cap a hung read would
# hang every `hub producers list` and every session-open with it.
_READER_TIMEOUT = 30  # seconds

# week-ahead's ``ledger/calls.md`` is the SoT the revise stage appends to; the rows follow
# the ``Made | Call | Resolves by | Status | Resolution`` shape the
# brief's scorecard grades itself against.
_RESOLVED_STATES = {"hit", "miss", "mixed", "not-triggered"}
# We need only to set ``in_table`` once on the header; any later check is on
# the resolved-status vocabulary. The header regex looks for the literal
# ``Made`` header in the first cell — anything else (column rules, data) is
# distinguished by what the cells contain.
_TABLE_HEADERS = re.compile(r"^\|\s*Made\s*\|", re.IGNORECASE)
_HEADER_SEPARATOR = re.compile(r"^\|[\s:|-]+\|\s*$")


def week_ahead_candidates(
    root: Path | str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> ProducerReport:
    """Read week-ahead's open calls as candidates; write beat proposals when ``conn`` given."""
    base = Path(root) if root is not None else DEFAULT_WEEK_AHEAD_ROOT
    if not base.is_dir():
        return ProducerReport(producer=WEEK_AHEAD, candidates=[], status=STATUS_ABSENT)
    ledger = base / "ledger"
    if not ledger.is_dir():
        return ProducerReport(producer=WEEK_AHEAD, candidates=[], status=STATUS_EMPTY)
    candidates: list[Candidate] = []
    notes: list[str] = []
    degraded = False
    calls_path = ledger / "calls.md"
    if calls_path.is_file():
        candidates.extend(_read_calls(calls_path))
    proposals, reader_notes, reader_degraded = _read_beats(base)
    notes.extend(reader_notes)
    degraded = degraded or reader_degraded
    if conn is not None and proposals:
        _write_beat_proposals(conn, proposals)
    if degraded:
        status = STATUS_DEGRADED
    elif not candidates:
        status = STATUS_EMPTY
    else:
        status = STATUS_OK
    return ProducerReport(
        producer=WEEK_AHEAD, candidates=candidates, status=status, notes=tuple(notes)
    )


def _read_calls(path: Path) -> list[Candidate]:
    """Each open call row in ``ledger/calls.md`` becomes one candidate."""
    out: list[Candidate] = []
    in_table = False
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if _TABLE_HEADERS.match(line):
            in_table = True
            continue
        if in_table and _HEADER_SEPARATOR.match(line):
            continue
        if in_table and line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 4:
                continue
            made, call, resolves_by, status_value = cells[0], cells[1], cells[2], cells[3]
            made_iso = as_iso_date(made)
            if made_iso is None and as_iso_date(resolves_by) is None:
                continue
            # Open calls are the ones the ledger says are not yet resolved.
            if status_value.strip().lower() in _RESOLVED_STATES:
                continue
            out.append(
                Candidate(
                    producer=WEEK_AHEAD,
                    kind="open_call",
                    summary=truncate(call, 120),
                    source_path=str(path.resolve()),
                    # The record's own date — the Made date, or the ledger's
                    # mtime when the row carries no date at all. Never the
                    # future ``Resolves by`` deadline.
                    as_of=made_iso or file_mtime_iso(path),
                )
            )
            continue
        # A blank line or non-table line ends the table block.
        if in_table and not line.startswith("|"):
            in_table = False
    return out


def _read_beats(base: Path) -> tuple[list[tuple[str, str | None]], list[str], bool]:
    """Read ``beats.md`` through week-ahead's reference reader.

    Returns ``(proposals, notes, degraded)``: one ``(name, weight)`` proposal
    per beat the reader parsed, its warnings as notes, and whether the read
    failed. The reader's contract defines exits 0 (read), 3 (absent — an
    optional dependency, so a note, not a failure) and 4 (malformed); a missing
    script, a timeout, invalid JSON or any other outcome is a broken read path
    — degraded and loud, no proposals — and none of them may raise: the ledger
    candidates still ship.
    """
    beats_path = base / "beats.md"
    if not beats_path.is_file():
        return [], [f"beats.md absent at {beats_path} — no beat proposals"], False
    reader = base / "scripts" / "read_beats.py"
    if not reader.is_file():
        return [], [f"beats reader missing at {reader} — no beat proposals"], True
    cmd = [sys.executable, str(reader), "--json", "--quiet", "--path", str(beats_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=_READER_TIMEOUT)
    except subprocess.TimeoutExpired:
        return (
            [],
            [f"beats reader timed out after {_READER_TIMEOUT}s — no beat proposals"],
            True,
        )
    except OSError as exc:
        return [], [f"beats reader failed: {exc} — no beat proposals"], True
    if proc.returncode != 0:
        detail = (proc.stderr.strip() or "no detail").splitlines()[0]
        return (
            [],
            [f"beats reader exit {proc.returncode}: {detail} — no beat proposals"],
            proc.returncode != 3,
        )
    try:
        doc = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [], ["beats reader emitted invalid JSON — no beat proposals"], True
    if not isinstance(doc, dict):
        return [], ["beats reader emitted invalid JSON — no beat proposals"], True
    proposals: list[tuple[str, str | None]] = []
    for beat in doc.get("beats", []):
        if not isinstance(beat, dict):
            continue
        name = str(beat.get("name", "")).strip()
        if not name:
            continue
        # ``weight_raw`` is the writer's own Weight line; an out-of-vocabulary
        # or missing weight normalises to None, never to a fabricated default.
        weight = str(beat.get("weight_raw", "")).strip() or None
        proposals.append((name, weight))
    notes = [f"warn: beats: {w}" for w in doc.get("warnings", []) if str(w).strip()]
    return proposals, notes, False


def _write_beat_proposals(
    conn: sqlite3.Connection, proposals: list[tuple[str, str | None]]
) -> None:
    """Insert one ``beats_proposals`` row per (name, weight); duplicates are skipped."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, weight in proposals:
            if not name:
                continue
            # ``IS ?`` compares NULL to NULL as equal; ``= ?`` never matches a
            # NULL rationale, so a beat without a Weight line would insert twice.
            existing = conn.execute(
                "SELECT id FROM beats_proposals WHERE proposal = ? AND rationale IS ?",
                (name, weight),
            ).fetchone()
            if existing is not None:
                continue
            conn.execute(
                "INSERT INTO beats_proposals (proposal, rationale, status)"
                " VALUES (?, ?, 'proposed')",
                (name, weight),
            )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
