"""Read-only adapters that turn producer files into stage-0 candidates.

Each adapter reads the producer's files in place and returns candidates carrying
at least ``producer``, ``kind``, ``summary``, ``source_path`` and ``as_of``. None
of them write, rename, move or delete anything in the producer's tree: the hub
is a consumer of mv-analyst, week-ahead and the morning brief's
investing-triage queue, and the contract with all three is one-directional.

The triage-queue adapter keeps a read cursor in the hub store (see migration
``0004_producer_cursors.sql``) so a second read sees nothing new; the week-ahead
adapter writes per-beat proposals to ``beats_proposals`` only — never to
``beats.md``, which week-ahead alone owns. mv-analyst's state lives in the
files themselves, so an absent or empty root yields zero candidates with a clear
status line — never an exception.

``status_reports`` is the canonical entry used by ``session-open``; ``cli`` wires
``hub producers list`` against the same function.
"""

from __future__ import annotations

from pathlib import Path

from hub.producers.common import (
    DEFAULT_MV_ANALYST_ROOT,
    DEFAULT_TRIAGE_QUEUE_DIR,
    DEFAULT_WEEK_AHEAD_ROOT,
    MV_ANALYST,
    STATUS_ABSENT,
    STATUS_EMPTY,
    STATUS_OK,
    TRIAGE_QUEUE,
    WEEK_AHEAD,
    Candidate,
    ProducerReport,
)
from hub.producers.mv_analyst import mv_analyst_candidates
from hub.producers.triage_queue import triage_queue_candidates
from hub.producers.week_ahead import week_ahead_candidates

__all__ = [
    "Candidate",
    "ProducerReport",
    "DEFAULT_MV_ANALYST_ROOT",
    "DEFAULT_WEEK_AHEAD_ROOT",
    "DEFAULT_TRIAGE_QUEUE_DIR",
    "MV_ANALYST",
    "WEEK_AHEAD",
    "TRIAGE_QUEUE",
    "STATUS_OK",
    "STATUS_ABSENT",
    "STATUS_EMPTY",
    "mv_analyst_candidates",
    "week_ahead_candidates",
    "triage_queue_candidates",
    "status_reports",
]


def status_reports(
    *,
    mv_analyst_root: Path | str | None = None,
    week_ahead_root: Path | str | None = None,
    triage_queue_dir: Path | str | None = None,
) -> list[ProducerReport]:
    """Run the three adapters against their (overridable) roots.

    The adapters touch the store when ``conn`` is supplied; the default
    ``status_reports`` here does not (session-open must not advance the read
    cursor). Callers that want the cursor advanced and the ``beats_proposals``
    written should call the per-adapter functions with their own connection.
    """
    return [
        mv_analyst_candidates(mv_analyst_root),
        week_ahead_candidates(week_ahead_root),
        triage_queue_candidates(triage_queue_dir),
    ]
