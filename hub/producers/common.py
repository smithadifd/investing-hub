"""Shared types and constants for the producer adapters.

A ``Candidate`` is one stage-0 finding from a producer, ready to score in the
sweep. A ``ProducerReport`` is the full read of one producer — its candidates
plus a status code session-open renders as one line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

MV_ANALYST = "mv-analyst"
WEEK_AHEAD = "week-ahead"
TRIAGE_QUEUE = "triage-queue"

# Default roots are the home paths the three producers actually live at; every
# adapter accepts an override (config or test fixture).
DEFAULT_MV_ANALYST_ROOT = Path.home() / "code" / "mv-analyst"
DEFAULT_WEEK_AHEAD_ROOT = Path.home() / "code" / "week-ahead"
DEFAULT_TRIAGE_QUEUE_DIR = Path.home() / "brief" / "investing-triage-queue"

STATUS_OK = "ok"
STATUS_ABSENT = "absent"
STATUS_EMPTY = "empty"


@dataclass(frozen=True)
class Candidate:
    """One stage-0 finding from a producer, ready to score in the sweep.

    ``source_path`` is the file the adapter read; ``as_of`` is the timestamp the
    producer stamped it with. ``producer`` distinguishes the three adapters in
    logs and downstream scoring.
    """

    producer: str
    kind: str
    summary: str
    source_path: str
    as_of: str


@dataclass(frozen=True)
class ProducerReport:
    """The full read of one producer: its candidates plus a status for session-open."""

    producer: str
    candidates: list[Candidate]
    status: str

    @property
    def count(self) -> int:
        return len(self.candidates)
