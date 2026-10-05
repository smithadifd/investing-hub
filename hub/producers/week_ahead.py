"""week-ahead adapter — read-only against ``ledger/calls.md`` and ``beats.md``.

Open call rows in the ledger become candidates. The ``## Beats`` section in
``beats.md`` is parsed for its headings; each beat becomes a ``beats_proposals``
row when a database connection is supplied. Week-ahead's own ``beats.md`` is
never written by this adapter — the proposal table is the only place a proposed
beat lives, exactly as the row contract says.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from hub.producers.common import (
    DEFAULT_WEEK_AHEAD_ROOT,
    STATUS_ABSENT,
    STATUS_EMPTY,
    STATUS_OK,
    WEEK_AHEAD,
    Candidate,
    ProducerReport,
)

# week-ahead's ``ledger/calls.md`` is the SoT the revise stage appends to; the
# rows follow the ``Made | Call | Resolves by | Status | Resolution`` shape the
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
    proposals: list[tuple[str, str | None]] = []
    calls_path = ledger / "calls.md"
    if calls_path.is_file():
        candidates.extend(_read_calls(calls_path))
    beats_path = base / "beats.md"
    if beats_path.is_file():
        proposals.extend(_read_beats(beats_path))
    if conn is not None and proposals:
        _write_beat_proposals(conn, proposals)
    status = STATUS_EMPTY if not candidates else STATUS_OK
    return ProducerReport(producer=WEEK_AHEAD, candidates=candidates, status=status)


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
            made_iso = _as_iso_date(made)
            res_iso = _as_iso_date(resolves_by)
            if made_iso is None:
                continue
            # Open calls are the ones the ledger says are not yet resolved.
            if status_value.strip().lower() in _RESOLVED_STATES:
                continue
            text = call[:120] + ("…" if len(call) > 120 else "")
            out.append(
                Candidate(
                    producer=WEEK_AHEAD,
                    kind="open_call",
                    summary=text,
                    source_path=str(path.resolve()),
                    as_of=res_iso or made_iso,
                )
            )
            continue
        # A blank line or non-table line ends the table block.
        if in_table and not line.startswith("|"):
            in_table = False
    return out


def _as_iso_date(value: str) -> str | None:
    """Return ``YYYY-MM-DDT00:00:00Z`` for an ISO date, else None."""
    value = value.strip()
    if not value:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value)
    if m is None:
        return None
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00Z"


def _read_beats(path: Path) -> list[tuple[str, str | None]]:
    """Each ``### <name>`` heading under ``## Beats`` becomes one proposed beat.

    The brief schema is prose-with-a-convention (see week-ahead's
    ``docs/beats-schema.md``); the only structured field we touch here is the
    heading, and the rationale is whatever ``Weight:`` line we can find under it.
    """
    out: list[tuple[str, str | None]] = []
    in_beats = False
    current_name: str | None = None
    current_weight: str | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        if line.startswith("## "):
            in_beats = line.strip().lower() == "## beats"
            current_name = None
            current_weight = None
            continue
        if not in_beats:
            continue
        if line.startswith("### ") and not line.startswith("####"):
            if current_name is not None:
                out.append((current_name, current_weight))
            current_name = line[4:].strip()
            current_weight = None
            continue
        if current_name is None:
            continue
        m = re.match(r"^Weight:\s*(.+)$", line.strip(), re.IGNORECASE)
        if m is not None:
            current_weight = m.group(1).strip()
    if current_name is not None:
        out.append((current_name, current_weight))
    return out


def _write_beat_proposals(
    conn: sqlite3.Connection, proposals: list[tuple[str, str | None]]
) -> None:
    """Insert one ``beats_proposals`` row per (name, weight); duplicates are skipped."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for name, weight in proposals:
            if not name:
                continue
            existing = conn.execute(
                "SELECT id FROM beats_proposals WHERE proposal = ? AND rationale = ?",
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
