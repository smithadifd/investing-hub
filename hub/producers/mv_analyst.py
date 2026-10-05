"""mv-analyst adapter — read-only against the producer's own index and analysis files.

Three contracted sources besides the episode watch, each yielding candidates:

* ``index/calls.md`` — the generated open-calls view (``scripts/build_index.py``
  flattens every guest ledger into it). One candidate per open row across its
  Overdue, Open-with-a-date and Open-undated sections; ``as_of`` is the row's
  ``Made`` date, never the future ``Resolves by`` deadline. The Scorecard and
  Resolved sections are history, not candidates.
* ``index/themes.md`` — the CURATED positions board (who stands where on each
  axis, and against whom; stage 3 maintains it, ``build_index.py`` never
  touches it). One candidate per axis block, carrying each guest's stated
  position in its summary plus a short content fingerprint of the whole
  block in its ``identity`` — a curated change to the positions or the
  commentary moves the candidate even when the rows and dates do not.
  ``as_of`` is the newest date on the board's own rows (the file's mtime when
  the block carries none). The ``themes:`` front-matter below stays as
  supporting context; the board is the curated source. A missing board yields
  ``status = degraded`` with a note naming it — never an exception.
* ``analysis/*-analysis.md`` — the ``themes:`` front-matter every episode
  analysis carries. One candidate per analysed episode with its theme tags,
  ``as_of`` the episode's ``date:``.

A missing root yields zero candidates and ``status = absent``; a root with
nothing to read yields ``empty``; a root whose positions board is missing
yields ``degraded`` — never an exception.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from hub.producers.common import (
    DEFAULT_MV_ANALYST_ROOT,
    MV_ANALYST,
    STATUS_ABSENT,
    STATUS_DEGRADED,
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
# An axis block on the curated positions board: `## Title`, a `` `slug` `` line
# sharing the fixed theme vocabulary with the analysis `themes:` front-matter,
# then position rows and commentary. Same shape stage 3 curates and
# mv-analyst's own ``build_beat_view.py`` reads.
_THEMES_BLOCK = re.compile(r"^## (.+?)\s*\n`([a-z0-9-]+)`\s*\n(.*?)(?=^## |\Z)", re.M | re.S)
_THEMES_ROW_SEPARATOR = re.compile(r"^\|[\s:|-]+\|$")
_THEMES_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def mv_analyst_candidates(root: Path | str | None = None) -> ProducerReport:
    """Read mv-analyst's index, analyses and done markers; emit one candidate per finding."""
    base = Path(root) if root is not None else DEFAULT_MV_ANALYST_ROOT
    if not base.is_dir():
        return ProducerReport(producer=MV_ANALYST, candidates=[], status=STATUS_ABSENT)
    candidates: list[Candidate] = []
    notes: list[str] = []
    candidates.extend(_episode_done_candidates(base))
    calls_index = base / "index" / "calls.md"
    if calls_index.is_file():
        candidates.extend(_read_calls_index(calls_index))
    attention_index = base / "index" / "attention.md"
    if attention_index.is_file():
        candidates.extend(_read_attention_index(attention_index))
    themes_board = base / "index" / "themes.md"
    if themes_board.is_file():
        candidates.extend(_read_themes_board(themes_board))
    else:
        notes.append("index/themes.md missing — the curated positions board is the themes source")
    candidates.extend(_read_analysis_themes(base))
    if notes:
        status = STATUS_DEGRADED
    elif not candidates:
        status = STATUS_EMPTY
    else:
        status = STATUS_OK
    return ProducerReport(
        producer=MV_ANALYST, candidates=candidates, status=status, notes=tuple(notes)
    )


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


def _read_themes_board(path: Path) -> list[Candidate]:
    """One candidate per axis block in ``index/themes.md`` — the positions board.

    Each block is one axis: its title, its slug from the shared theme
    vocabulary, and the position rows saying who stands where on it. The
    candidate carries each guest's stated position in its summary and a short
    fingerprint of the whole block in ``identity``, so a curated change to a
    position or the commentary moves the candidate even when the rows, the
    guests and the dates all stay as they were. ``as_of`` is the newest date
    on the block's own rows, the file's mtime when the block carries none.
    """
    fallback = file_mtime_iso(path)
    out: list[Candidate] = []
    for match in _THEMES_BLOCK.finditer(path.read_text(encoding="utf-8", errors="replace")):
        title, slug, body = match.group(1).strip(), match.group(2), match.group(3)
        rows = _board_rows(body)
        if rows:
            stated = "; ".join(
                f"{row['guest']}: {row['position']}"
                for row in rows
                if row["guest"] and row["position"]
            )
            summary = f"{title} ({slug}): {len(rows)} position(s) — {stated}"
        else:
            summary = f"{title} ({slug}): no readable position rows"
        dates = sorted({date for row in rows for date in _THEMES_DATE.findall(row["cells"])})
        as_of = as_iso_date(dates[-1]) if dates else None
        out.append(
            Candidate(
                producer=MV_ANALYST,
                kind="theme_axis",
                summary=truncate(summary),
                source_path=str(path.resolve()),
                as_of=as_of or fallback,
                identity=_block_identity(title, slug, body),
            )
        )
    return out


def _board_rows(body: str) -> list[dict[str, str]]:
    """The position rows of one axis block: ``| Guest | Position | Since | Episode |``.

    Tolerant on purpose, the same way mv-analyst's own board reader is: the
    header and separator rows are skipped, a row short of the four cells the
    board's shape needs is skipped, and nothing is guessed at.
    """
    rows: list[dict[str, str]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line.startswith("|") or _THEMES_ROW_SEPARATOR.match(line):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 4 or cells[0].lower() == "guest":
            continue
        rows.append({"guest": cells[0], "position": cells[1], "cells": " ".join(cells[1:])})
    return rows


def _block_identity(title: str, slug: str, body: str) -> str:
    """A short sha256 of one axis block's normalized content — its stable identity.

    The block is normalized to its non-empty lines, stripped, so a pure
    whitespace edit does not move the fingerprint while any curated change to
    the position rows or the commentary does. That is the point: a candidate
    whose block moved must not be byte-identical to its earlier self.
    """
    lines = [line.strip() for line in (title, slug, *body.splitlines()) if line.strip()]
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()[:12]


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
