"""mv-analyst adapter — read-only against ``state/done/mv<ep>``.

Each marker is an ISO timestamp written by ``scripts/run-analysis.sh`` when the
final analysis lands; the candidate summary names the episode and the source
path is the analysis file when one is on this checkout, the marker otherwise.
A missing root yields zero candidates and ``status = absent``; an empty marker
directory yields ``empty``.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from hub.producers.common import (
    DEFAULT_MV_ANALYST_ROOT,
    MV_ANALYST,
    STATUS_ABSENT,
    STATUS_EMPTY,
    STATUS_OK,
    Candidate,
    ProducerReport,
)

# An mv<ep> done marker is `state/done/mv<episode>` containing the ISO timestamp
# the pipeline stamped on the final analysis.
_MV_DONE = re.compile(r"^mv(\d+)$")


def _parse_iso(value: str) -> str | None:
    """Return the ISO timestamp inside an mv-analyst done marker, or None.

    The script writes ``date +%Y-%m-%dT%H:%M:%S%z`` which carries a numeric offset
    (``+0000``-ish); normalise that to a ``Z``-suffixed UTC string so consumers
    have one shape to parse.
    """
    value = value.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def mv_analyst_candidates(root: Path | str | None = None) -> ProducerReport:
    """Read mv-analyst's ``state/done/mv<ep>`` markers and emit one candidate per episode."""
    base = Path(root) if root is not None else DEFAULT_MV_ANALYST_ROOT
    if not base.is_dir():
        return ProducerReport(producer=MV_ANALYST, candidates=[], status=STATUS_ABSENT)
    done_dir = base / "state" / "done"
    if not done_dir.is_dir():
        return ProducerReport(producer=MV_ANALYST, candidates=[], status=STATUS_EMPTY)
    analysis_dir = base / "analysis"
    analysis_files_by_episode: dict[int, list[Path]] = {}
    if analysis_dir.is_dir():
        for path in analysis_dir.glob("*-analysis.md"):
            episode = _episode_from_analysis(path.name)
            if episode is None:
                continue
            analysis_files_by_episode.setdefault(episode, []).append(path)
    candidates: list[Candidate] = []
    for marker in sorted(done_dir.iterdir()):
        if not marker.is_file():
            continue
        match = _MV_DONE.match(marker.name)
        if match is None:
            continue
        try:
            episode = int(match.group(1))
        except ValueError:
            continue
        if episode < 1:
            continue
        as_of = _parse_iso(marker.read_text(encoding="utf-8", errors="replace"))
        if as_of is None:
            continue
        matches = analysis_files_by_episode.get(episode, [])
        if matches:
            source_path = str(sorted(matches)[-1].resolve())
        else:
            source_path = str(marker.resolve())
        candidates.append(
            Candidate(
                producer=MV_ANALYST,
                kind="episode_done",
                summary=f"MV{episode} analysis ready",
                source_path=source_path,
                as_of=as_of,
            )
        )
    status = STATUS_EMPTY if not candidates else STATUS_OK
    return ProducerReport(producer=MV_ANALYST, candidates=candidates, status=status)


def _episode_from_analysis(filename: str) -> int | None:
    """Extract the episode number from a ``*-mv{NN}-*.md`` analysis filename.

    mv-analyst's analysis files are named ``$base-analysis.md`` where ``$base``
    is the transcript basename (e.g. ``2026-01-15-mv901-a``); the ``mvNNN``
    token is the only stable identifier across renames, and the trailing letter
    is the guest letter, which varies. Return the integer episode or None.
    """
    m = re.search(r"mv(\d+)", filename)
    if m is None:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None
