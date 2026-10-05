"""mv-analyst adapter — read-only against the producer's own index and analysis files.

Three contracted sources besides the episode watch, each yielding candidates:

* ``index/calls.md`` — the generated open-calls view (``scripts/build_index.py``
  flattens every guest ledger into it). One candidate per open row across its
  Overdue, Open-with-a-date and Open-undated sections; ``as_of`` is the row's
  ``Made`` date, never the future ``Resolves by`` deadline. The Scorecard and
  Resolved sections are history, not candidates.
* ``index/attention.md`` — the generated attention view. One candidate per beat
  under its weight heading (``lead``/``standing``/``watch``/``skip``); the view
  carries no dates, so ``as_of`` is the file's mtime.
* ``analysis/*-analysis.md`` — the ``themes:`` front-matter every episode
  analysis carries. One candidate per analysed episode with its theme tags,
  ``as_of`` the episode's ``date:``.

``state/done/mv<ep>`` markers stay as the episode watch: one candidate per
completed episode, ``as_of`` the ISO timestamp inside the marker. A missing root
yields zero candidates and ``status = absent``; a root with nothing to read
yields ``empty`` — never an exception.
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
    as_iso_date,
    file_mtime_iso,
    truncate,
)

# An mv<ep> done marker is `state/done/mv<episode>` containing the ISO timestamp
# the pipeline stamped on the final analysis.
_MV_DONE = re.compile(r"^mv(\d+)$")
# A beat line in index/attention.md: `**Beat name** — the axes that can speak to it`.
_BEAT_LINE = re.compile(r"^\*\*(.+?)\*\*")
_WEIGHT_HEADINGS = {"lead", "standing", "watch", "skip"}
# index/calls.md sections that hold open calls, and the candidate kind each yields.
_CALL_SECTIONS = {
    "overdue": "overdue_call",
    "open, with a resolution date": "open_call",
    "open, with no resolution date": "open_call",
}
_TABLE_SEPARATOR = re.compile(r"^[\s:|-]*$")


def mv_analyst_candidates(root: Path | str | None = None) -> ProducerReport:
    """Read mv-analyst's index, analyses and done markers; emit one candidate per finding."""
    base = Path(root) if root is not None else DEFAULT_MV_ANALYST_ROOT
    if not base.is_dir():
        return ProducerReport(producer=MV_ANALYST, candidates=[], status=STATUS_ABSENT)
    candidates: list[Candidate] = []
    candidates.extend(_episode_done_candidates(base))
    calls_index = base / "index" / "calls.md"
    if calls_index.is_file():
        candidates.extend(_read_calls_index(calls_index))
    attention_index = base / "index" / "attention.md"
    if attention_index.is_file():
        candidates.extend(_read_attention_index(attention_index))
    candidates.extend(_read_analysis_themes(base))
    status = STATUS_EMPTY if not candidates else STATUS_OK
    return ProducerReport(producer=MV_ANALYST, candidates=candidates, status=status)


def _episode_done_candidates(base: Path) -> list[Candidate]:
    """One candidate per ``state/done/mv<ep>`` marker — the episode watch."""
    done_dir = base / "state" / "done"
    if not done_dir.is_dir():
        return []
    analysis_files_by_episode: dict[int, list[Path]] = {}
    analysis_dir = base / "analysis"
    if analysis_dir.is_dir():
        for path in analysis_dir.glob("*-analysis.md"):
            episode = _episode_from_analysis(path.name)
            if episode is None:
                continue
            analysis_files_by_episode.setdefault(episode, []).append(path)
    out: list[Candidate] = []
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
        source_path = str(sorted(matches)[-1].resolve()) if matches else str(marker.resolve())
        out.append(
            Candidate(
                producer=MV_ANALYST,
                kind="episode_done",
                summary=f"MV{episode} analysis ready",
                source_path=source_path,
                as_of=as_of,
            )
        )
    return out


def _read_calls_index(path: Path) -> list[Candidate]:
    """One candidate per open call row in ``index/calls.md``, ``as_of`` the Made date."""
    fallback = file_mtime_iso(path)
    out: list[Candidate] = []
    section: str | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("## ") and not line.startswith("### "):
            heading = line[3:].strip().lower()
            section = heading if heading in _CALL_SECTIONS else None
            continue
        if section is None or not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells or all(_TABLE_SEPARATOR.fullmatch(c) for c in cells):
            continue  # header separator row
        if any(c.lower() == "made" for c in cells):
            continue  # the table's own header row
        if section == "open, with no resolution date":
            if len(cells) < 3:
                continue
            guest, call, made = cells[0], cells[1], cells[2]
            extra = ""
        else:
            if len(cells) < 4:
                continue
            deadline, guest, call, made = cells[0], cells[1], cells[2], cells[3]
            extra = f" (was due {deadline})" if section == "overdue" else f" (resolves {deadline})"
        out.append(
            Candidate(
                producer=MV_ANALYST,
                kind=_CALL_SECTIONS[section],
                summary=truncate(f"{guest} — {call}{extra}"),
                source_path=str(path.resolve()),
                # The record's own date, not the future deadline it resolves by.
                as_of=as_iso_date(made) or fallback,
            )
        )
    return out


def _read_attention_index(path: Path) -> list[Candidate]:
    """One candidate per beat under its weight heading in ``index/attention.md``."""
    as_of = file_mtime_iso(path)
    out: list[Candidate] = []
    weight: str | None = None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line.startswith("## ") and not line.startswith("### "):
            heading = line[3:].strip().lower()
            weight = heading if heading in _WEIGHT_HEADINGS else None
            continue
        if weight is None:
            continue
        match = _BEAT_LINE.match(line)
        if match is None:
            continue
        name = match.group(1).strip()
        out.append(
            Candidate(
                producer=MV_ANALYST,
                kind="attention_beat",
                summary=truncate(f"{weight}: {name}"),
                source_path=str(path.resolve()),
                as_of=as_of,
            )
        )
    return out


def _read_analysis_themes(base: Path) -> list[Candidate]:
    """One candidate per episode analysis carrying ``themes:`` front-matter."""
    analysis_dir = base / "analysis"
    if not analysis_dir.is_dir():
        return []
    out: list[Candidate] = []
    for path in sorted(analysis_dir.glob("*-analysis.md")):
        fm = _front_matter(path.read_text(encoding="utf-8", errors="replace"))
        episode = fm.get("episode", "").strip()
        themes = _theme_list(fm.get("themes", ""))
        if not episode.isdigit() or not themes:
            continue
        out.append(
            Candidate(
                producer=MV_ANALYST,
                kind="episode_themes",
                summary=truncate(f"MV{int(episode)} themes: " + ", ".join(themes)),
                source_path=str(path.resolve()),
                as_of=as_iso_date(fm.get("date", "")) or file_mtime_iso(path),
            )
        )
    return out


def _front_matter(text: str) -> dict[str, str]:
    """The ``---``-delimited key/value block at the top of an analysis file.

    Same shape mv-analyst's own ``build_index.py`` reads: ``key: value`` lines,
    a list value written as ``[a, b]`` or a JSON-style ``["a", "b"]``.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    fm: dict[str, str] = {}
    for line in text[3:end].split("\n"):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fm[key.strip()] = value.strip()
    return fm


def _theme_list(raw: str) -> list[str]:
    """Parse a front-matter ``themes:`` value into its slugs."""
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        return [slug.strip().strip('"') for slug in raw[1:-1].split(",") if slug.strip()]
    return [raw] if raw else []


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
