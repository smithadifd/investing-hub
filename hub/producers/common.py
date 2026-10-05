"""Shared types and constants for the producer adapters.

A ``Candidate`` is one stage-0 finding from a producer, ready to score in the
sweep. A ``ProducerReport`` is the full read of one producer — its candidates
plus a status code session-open renders as one line.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

MV_ANALYST = "mv-analyst"
WEEK_AHEAD = "week-ahead"
TRIAGE_QUEUE = "triage-queue"

# Default roots are the data homes the three producers actually write to, not the
# development checkouts; every adapter accepts an override (config or test fixture).
DEFAULT_MV_ANALYST_ROOT = Path.home() / "mv-analyst"
DEFAULT_WEEK_AHEAD_ROOT = Path.home() / "week-ahead"
DEFAULT_TRIAGE_QUEUE_DIR = Path.home() / "brief" / "investing-triage-queue"

STATUS_OK = "ok"
STATUS_ABSENT = "absent"
STATUS_EMPTY = "empty"


def as_iso_date(value: str) -> str | None:
    """Return ``YYYY-MM-DDT00:00:00Z`` for an ISO date cell, else None."""
    value = value.strip()
    if not value:
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return None
    return f"{value}T00:00:00Z"


def file_mtime_iso(path: Path) -> str:
    """The file's mtime as a UTC ISO timestamp — provenance for a generated file with no date."""
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def truncate(text: str, width: int = 200) -> str:
    """Clip to ``width`` with a plain ``...`` tail."""
    return text if len(text) <= width else text[: width - 3] + "..."


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
