"""Midweek letter: a port of living-desk's mechanical merit gate, re-pointed at
the hub's sources.

Stage 0 evaluates three legs of evidence against the operator's standing views
and emits only what clears the gate:

* **leg (c) — the rotation scan.** Per (view, ticker the view watches), the
  strongest verdict wins. ``contradicts`` outranks ``confirms`` outranks
  ``extends``. A threshold binds to the ticker it names, and to no other:
  a watch-only ticker cannot reach the letter by borrowing another
  ticker's threshold.
* **leg (b) — the beats × MacroVoices corpus.** Per (view, foregrounded
  corpus event on a beat the view names): ``extends``, always. The ratified
  beats×MacroVoices brief is explicit that tensions are surfaced, never
  adjudicated, so this leg assembles the packet and stops.
* **The memory.** Same view + same key + same verdict inside the repeat
  window is a ``repeat`` and is dropped. A **changed** verdict is never a
  repeat — a window that swallowed it would be a bug in the definition of
  merit, not a tuning problem.

Stage 1 composes the letter:

* Quiet days (no findings) print one concise status line and write NO file.
* Active days compose the deterministic visual (rotation strip + 52-week
  track) from the scan, append the mechanical packets for each finding, and
  optionally invoke a drafter for a short summary paragraph (the prose is
  drafter output; the data under it is not).

This is the **port** of living-desk's gate: the same mechanical rule over the
hub's producers (``mv-analyst``) and the local knowledge store
(``documents``). The hub does not have a market-data rotation scan of its
own today — a future market-data source will supply one — so the scan is an
explicit input: ``--scan-file`` on the CLI, a documented JSON shape read by
``load_scan_file``. With no scan source configured the market leg is **off**:
no market finding can clear, the provenance line and the letter say so, and
only the corpus leg can clear the gate.

Key facts:

* Every Stage 0 query is ``ORDER BY id`` and the merged ``findings`` tuple is
  sorted by a canonical key, so two runs of the same data produce identical
  output.
* The drafter is injectable (env ``HUB_LETTER_DRAFT_CMD``, else
  ``claude -p --model <M>``); a failure raises ``LetterError`` and writes
  nothing.
* The sender is injectable (env ``HUB_LETTER_SEND_CMD``); unset means a
  clear refusal — no default that sends anything.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date as _date
from pathlib import Path

# Same threshold as living-desk's merit.py. The numbers are guesses with
# reasons (merit.py defs); the hub ports them because the rule, not the
# tuning, is the load-bearing thing.
EXTREME_HIGH, EXTREME_LOW = 95.0, 5.0

# leg (c) threshold vocabulary — ported from living-desk.
METRICS = {"ret20", "ret60", "rs20", "rs60", "pct52w"}
OPS = {
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}

# Verdict strength: contradicts > confirms > extends. A view being broken is
# worth more than the same view being reaffirmed, and both are worth more
# than a regime note about the same instrument.
STRENGTH = {"contradicts": 0, "confirms": 1, "extends": 2}

# 28-day repeat suppression. Same window as living-desk's memory.py: a
# changed verdict is never a repeat.
REPEAT_DAYS = 28
WEEK_LENGTH = 10  # length of 'YYYY-MM-DD'
DAY_LENGTH = 10

VERDICTS = ("confirms", "contradicts", "extends")
SOURCES = ("scan", "mv-analyst")

SCHEMA = "investing-hub.letter.v1"

DEFAULT_OUT_DIR = Path("out/letters")
DEFAULT_DRAFTER_CMD = "claude"
DEFAULT_DRAFTER_TIMEOUT_S = 30.0
DEFAULT_SEND_TIMEOUT_S = 60.0

# A drafter takes the prompt and the explicit model name and returns the
# drafted text. Tests inject a fake.
DrafterFn = Callable[[str, str], str]
# A sender takes the letter path and a context dict, returns a one-line
# receipt. The CLI can substitute a stub for tests.
SenderFn = Callable[[Path, dict], str]


class LetterError(Exception):
    """A letter operation failed in a way the operator must see."""


# ---------------------------------------------------------------------------
# The memory: a finding reaches the operator once inside the window.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MemoryRecord:
    """One finding the letter has already produced."""

    date: str
    view_id: str
    source: str
    key: str
    verdict: str
    trigger: str = ""
    summary: str = ""


class MemoryMalformed(LetterError):
    """A memory line could not be read; refusing to silently skip is part of the contract."""


def _date_of(value: str, where: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise MemoryMalformed(f"{where}: {value!r} is not an ISO date") from exc


def load_memory(path: Path | str | None) -> list[MemoryRecord]:
    """Every record in file order. An absent memory is an empty one."""
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        text = p.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise MemoryMalformed(f"{p}: {exc}") from exc
    out: list[MemoryRecord] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        where = f"{p}:{lineno}"
        try:
            row = json.loads(line)
        except ValueError as exc:
            raise MemoryMalformed(f"{where}: {exc}") from exc
        if not isinstance(row, dict):
            raise MemoryMalformed(f"{where}: not an object")
        for field_name in ("date", "view_id", "source", "key", "verdict"):
            if field_name not in row:
                raise MemoryMalformed(f"{where}: missing {field_name!r}")
        if row["verdict"] not in VERDICTS:
            raise MemoryMalformed(f"{where}: verdict {row['verdict']!r}")
        if row["source"] not in SOURCES:
            raise MemoryMalformed(f"{where}: source {row['source']!r}")
        _date_of(row["date"], f"{where}: date")
        out.append(
            MemoryRecord(
                date=row["date"],
                view_id=row["view_id"],
                source=row["source"],
                key=row["key"],
                verdict=row["verdict"],
                trigger=row.get("trigger", ""),
                summary=row.get("summary", ""),
            )
        )
    return out


def append_memory(path: Path | str, records: list[MemoryRecord]) -> int:
    """Append whole JSON lines; an empty list writes nothing."""
    if not records:
        return 0
    p = Path(path)
    parent = p.parent
    if str(parent):
        parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as fh:
        for row in records:
            payload = {
                "date": row.date,
                "view_id": row.view_id,
                "source": row.source,
                "key": row.key,
                "verdict": row.verdict,
                "trigger": row.trigger,
                "summary": row.summary,
            }
            fh.write(json.dumps(payload, sort_keys=True) + "\n")
    return len(records)


def is_repeat(
    records: list[MemoryRecord],
    *,
    view_id: str,
    key: str,
    verdict: str,
    asof: str,
    within_days: int = REPEAT_DAYS,
) -> str | None:
    """The date of the most recent record that blocks this finding, or None.

    A record dated the same day as the issue (or later) never blocks: an
    identical rerun of the same date must reproduce the letter, not go
    quiet on its own record.
    """
    on = _date_of(asof, "asof")
    cutoff = on - dt.timedelta(days=within_days)
    best: dt.date | None = None
    for row in records:
        if row.view_id != view_id or row.key != key or row.verdict != verdict:
            continue
        when = _date_of(row.date, "memory")
        if when < cutoff or when >= on:
            continue
        if best is None or when > best:
            best = when
    return best.isoformat() if best else None


def records_to_append(
    memory: list[MemoryRecord],
    records: list[MemoryRecord],
    *,
    asof: str,
) -> list[MemoryRecord]:
    """Drop records already stored for this issue date.

    Same-date records never suppress a rerun, so a second run of the same
    date must not append them twice; the memory stays byte-stable too.
    """
    seen = {(row.view_id, row.key, row.verdict) for row in memory if row.date == asof}
    out: list[MemoryRecord] = []
    for row in records:
        ident = (row.view_id, row.key, row.verdict)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# The data shapes the gate reads.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class View:
    """One standing view the gate grades evidence against."""

    id: str
    title: str
    weight: str  # "lead" | "standing" | "watch"
    watching: tuple[str, ...] = ()  # tickers the view scans
    beats: tuple[str, ...] = ()  # beat slugs the corpus leg reads
    claim: str = ""
    confirms_when: tuple[dict, ...] = ()
    contradicts_when: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ScanRow:
    """One ticker's rotation numbers for a given moment."""

    ticker: str
    close: float
    ret20: float | None = None
    ret60: float | None = None
    rs20: float | None = None
    pct52w: float | None = None
    trend: str = "—"
    crossed: str | None = None  # "up" | "down" | None
    sma200: float | None = None
    asof: str = ""


@dataclass(frozen=True)
class Scan:
    """The full rotation scan: rows + benchmark + as-of date."""

    benchmark: str
    asof: str
    rows: tuple[ScanRow, ...] = ()


@dataclass(frozen=True)
class CorpusEvent:
    """One foregrounded event on a beat the corpus leg reads."""

    beat_slug: str
    beat_name: str
    key: str  # stable across runs: ``beat:date:guest:claim-digest``
    date: str
    classes: tuple[str, ...]
    guest: str
    claim: str
    citation: str


@dataclass(frozen=True)
class Corpus:
    """The beats × MacroVoices corpus: events grouped under beat slugs."""

    views: tuple[tuple[str, tuple[CorpusEvent, ...]], ...] = ()


# ---------------------------------------------------------------------------
# Stage 0: the gate itself (leg c, leg b, memory). Pure — no clock.
# ---------------------------------------------------------------------------


def _holds(condition: dict | None, row: ScanRow) -> tuple[bool, str]:
    """Does this condition hold — for the ticker it NAMES, and no other."""
    if not condition:
        return False, ""
    if condition["ticker"] != row.ticker:
        return False, ""
    value = getattr(row, condition["metric"], None)
    if value is None:
        return False, ""
    ok = OPS[condition["op"]](float(value), float(condition["value"]))
    label = (
        f"{condition['ticker']} {condition['metric']} {condition['op']}"
        f" {condition['value']} ({condition['metric']} = {_fmt(value)})"
    )
    return ok, label


def _fmt(value, unit: str = "") -> str:
    if value is None:
        return "n/a"
    return f"{value:+.2f}{unit}"


def _regime(row: ScanRow) -> list[str]:
    """The extends triggers for one scan row, in words."""
    notes: list[str] = []
    if row.crossed == "down" and row.sma200 is not None:
        notes.append(
            f"{row.ticker} crossed below its 200-day trend"
            f" (close {row.close:.2f} vs SMA200 {row.sma200:.2f})"
        )
    elif row.crossed == "up" and row.sma200 is not None:
        notes.append(
            f"{row.ticker} crossed above its 200-day trend"
            f" (close {row.close:.2f} vs SMA200 {row.sma200:.2f})"
        )
    if row.pct52w is not None and row.pct52w >= EXTREME_HIGH:
        notes.append(
            f"{row.ticker} is at the {row.pct52w:.0f}th percentile of its own 52-week range"
        )
    elif row.pct52w is not None and row.pct52w <= EXTREME_LOW:
        notes.append(
            f"{row.ticker} is at the {row.pct52w:.0f}th percentile of its own 52-week range"
        )
    return notes


def _market_findings(views: list[View], scan: Scan | None) -> list[dict]:
    """Per (view, watched ticker) verdicts from the scan, if there is one.

    ``scan is None`` means no scan source is configured: the market leg is
    off and yields nothing. An empty scan (``rows == ()``) is a configured
    source over an empty universe: also no findings, but reported as on.
    """
    if scan is None:
        return []
    rows_by_ticker = {row.ticker: row for row in scan.rows}
    out: list[dict] = []
    for index, view in enumerate(views):
        for ticker in view.watching:
            if ticker == scan.benchmark:
                continue
            row = rows_by_ticker.get(ticker)
            if row is None:
                continue
            best_verdict: str | None = None
            best_trigger = ""
            for cond in view.contradicts_when:
                ok, label = _holds(cond, row)
                if ok:
                    best_verdict = "contradicts"
                    best_trigger = label
                    break
            if best_verdict is None:
                for cond in view.confirms_when:
                    ok, label = _holds(cond, row)
                    if ok:
                        best_verdict = "confirms"
                        best_trigger = label
                        break
            regime = _regime(row)
            if best_verdict is None:
                if regime:
                    best_verdict = "extends"
                    best_trigger = regime[0]
            if best_verdict is None:
                continue
            evidence = list(regime)
            for cond in view.contradicts_when:
                _, label = _holds(cond, row)
                if label and best_verdict != "contradicts":
                    evidence.append(f"threshold not met: {label}")
            for cond in view.confirms_when:
                _, label = _holds(cond, row)
                if label and best_verdict != "confirms":
                    evidence.append(f"threshold not met: {label}")
            evidence.append(
                f"{ticker}: close {row.close:.2f} "
                f"· 20d {_fmt(row.ret20)}% "
                f"· 60d {_fmt(row.ret60)}% "
                f"· vs {scan.benchmark} {_fmt(row.rs20)} pp "
                f"· 52w percentile {_fmt(row.pct52w, '')} "
                f"· {row.trend} its 200-day trend "
                f"· as of {row.asof or scan.asof}"
            )
            out.append(
                {
                    "view_index": index,
                    "view_id": view.id,
                    "view_title": view.title,
                    "weight": view.weight,
                    "claim": view.claim,
                    "source": "scan",
                    "key": ticker,
                    "verdict": best_verdict,
                    "speed": _speed(view, best_verdict, row),
                    "trigger": best_trigger,
                    "summary": f"{ticker} {best_verdict} the view: {best_trigger}",
                    "evidence": evidence,
                    "citation": f"rotation scan as of {scan.asof}",
                }
            )
    return out


def _speed(view: View, verdict: str, row: ScanRow) -> str:
    if view.weight != "lead":
        return "weekly"
    if verdict == "contradicts" or (row and row.crossed):
        return "same-day"
    return "weekly"


def _corpus_findings(views: list[View], corpus: Corpus) -> list[dict]:
    by_slug = dict(corpus.views)
    out: list[dict] = []
    seen_beats: set[str] = set()
    for index, view in enumerate(views):
        for slug in view.beats:
            beat_events = by_slug.get(slug)
            if beat_events is None:
                continue
            if slug not in seen_beats:
                seen_beats.add(slug)
            beat_name = beat_events[0].beat_name if beat_events else slug
            for event in beat_events:
                out.append(
                    {
                        "view_index": index,
                        "view_id": view.id,
                        "view_title": view.title,
                        "weight": view.weight,
                        "claim": view.claim,
                        "source": "mv-analyst",
                        "key": event.key,
                        "verdict": "extends",
                        "speed": "weekly",
                        "trigger": (
                            f"{' \u00b7 '.join(event.classes) or 'foregrounded event'}"
                            f" on the {beat_name} beat ({slug}), {event.date}"
                        ),
                        "summary": event.claim or "(the corpus recorded no claim text)",
                        "evidence": [
                            f"the mv-analyst corpus foregrounded this as:"
                            f" {', '.join(event.classes) or 'foregrounded'}",
                            f"beat {beat_name}, slug {slug}",
                        ],
                        "citation": event.citation,
                        "guest": event.guest,
                        "classes": list(event.classes),
                        "date": event.date,
                        "beat_slug": slug,
                        "beat_name": beat_name,
                    }
                )
    return out


@dataclass(frozen=True)
class LetterResult:
    """What stage 0 found for one date."""

    date: _date
    quiet: bool
    findings: tuple[dict, ...] = ()
    suppressed: tuple[dict, ...] = ()
    scan: Scan | None = None
    views: tuple[View, ...] = ()
    coverage_gaps: tuple[str, ...] = ()


def gate(
    views: list[View],
    scan: Scan | None,
    corpus: Corpus,
    memory_records: list[MemoryRecord],
    asof: str,
) -> LetterResult:
    """views × scan × corpus × memory -> what earned the letter.

    ``scan is None`` means no scan source is configured: the market leg is
    off, no market finding can clear, and only the corpus leg can earn the
    letter.
    """
    market = _market_findings(views, scan)
    beats = _corpus_findings(views, corpus)

    candidates = market + beats
    candidates.sort(
        key=lambda f: (
            0 if f["speed"] == "same-day" else 1,
            f["view_index"],
            0 if f["source"] == "scan" else 1,
            f["key"],
        )
    )

    findings: list[dict] = []
    suppressed: list[dict] = []
    for f in candidates:
        seen = is_repeat(
            memory_records,
            view_id=f["view_id"],
            key=f["key"],
            verdict=f["verdict"],
            asof=asof,
        )
        if seen:
            suppressed.append(
                {
                    "view_id": f["view_id"],
                    "key": f["key"],
                    "verdict": f["verdict"],
                    "seen": seen,
                    "source": f["source"],
                }
            )
            continue
        findings.append(f)

    beats_without_view: list[str] = []
    named: set[str] = set()
    for view in views:
        for slug in view.beats:
            if slug not in dict(corpus.views) and slug not in named:
                named.add(slug)
                beats_without_view.append(slug)

    return LetterResult(
        date=_date.fromisoformat(asof),
        quiet=not findings,
        findings=tuple(findings),
        suppressed=tuple(suppressed),
        scan=scan,
        views=tuple(views),
        coverage_gaps=tuple(beats_without_view),
    )


# ---------------------------------------------------------------------------
# Hub adapters: build views, scan and corpus from local producers and the store.
# ---------------------------------------------------------------------------


_THRESHOLD_LINE = re.compile(
    r"^(Confirms when|Contradicts when):\s+"
    r"(?P<ticker>[A-Z][A-Z0-9.\-=]{0,15})\s+"
    r"(?P<metric>ret20|ret60|rs20|rs60|pct52w)\s+"
    r"(?P<op>>=|<=|>|<)\s+"
    r"(?P<value>-?\d+(?:\.\d+)?)\s*$"
)
_VIEW_HEADING_RE = re.compile(r"^###\s+(?P<title>.+?)\s*$")
_VIEW_ID_RE = re.compile(r"^Id:\s*(?P<id>[a-z0-9][a-z0-9\-_]*)\s*$", re.IGNORECASE)
_VIEW_WEIGHT_RE = re.compile(r"^Weight:\s*(?P<weight>lead|standing|watch|skip)\s*$", re.IGNORECASE)
_VIEW_WATCHING_RE = re.compile(r"^Watching:\s*(?P<list>.+?)\s*$", re.IGNORECASE)
_VIEW_BEATS_RE = re.compile(r"^Beats:\s*(?P<list>.+?)\s*$", re.IGNORECASE)
_VIEW_CLAIM_RE = re.compile(r"^Claim:\s*(?P<claim>.+?)\s*$", re.IGNORECASE)


def parse_views_file(path: Path | str) -> list[View]:
    """Parse a views file in the living-desk shape — public-safe inputs only.

    Used by tests and by adapters that want to keep the view file outside the
    store. The CLI path instead calls ``build_views_from_documents``, which
    reads thesis documents the store holds.
    """
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return _parse_views_text(text)


def _parse_views_text(text: str) -> list[View]:
    out: list[View] = []
    current: dict | None = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        m = _VIEW_HEADING_RE.match(line)
        if m:
            if current is not None:
                _close_view(current, out)
            current = {"title": m.group("title").strip()}
            continue
        if current is None:
            continue
        for pattern, key in (
            (_VIEW_ID_RE, "id"),
            (_VIEW_WEIGHT_RE, "weight"),
        ):
            mm = pattern.match(line)
            if mm:
                current[key] = mm.group(key)
                break
        else:
            mm = _VIEW_WATCHING_RE.match(line)
            if mm:
                current["watching"] = _split_csv(mm.group("list"))
                continue
            mm = _VIEW_BEATS_RE.match(line)
            if mm:
                current["beats"] = _split_csv(mm.group("list"))
                continue
            mm = _VIEW_CLAIM_RE.match(line)
            if mm:
                current["claim"] = mm.group("claim").strip()
                current["claim_lines"] = [current["claim"]]
                current["collecting_claim"] = True
                continue
            mm = _THRESHOLD_LINE.match(line)
            if mm:
                cond = {
                    "ticker": mm.group("ticker"),
                    "metric": mm.group("metric"),
                    "op": mm.group("op"),
                    "value": float(mm.group("value")),
                }
                kind = "contradicts_when" if line.startswith("Contradicts") else "confirms_when"
                current.setdefault(kind, []).append(cond)
                # Threshold lines end the claim collection.
                current["collecting_claim"] = False
                continue
            if current.get("collecting_claim"):
                current.setdefault("claim_lines", []).append(line.strip())
    if current is not None:
        _close_view(current, out)
    return out


def _split_csv(value: str) -> tuple[str, ...]:
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _close_view(current: dict, out: list[View]) -> None:
    if current.get("claim_lines"):
        current["claim"] = " ".join(current.pop("claim_lines")).strip()
    elif "claim" in current:
        current.pop("claim", None)
    if not current.get("id") or not current.get("weight"):
        return
    out.append(
        View(
            id=current["id"],
            title=current.get("title", current["id"]),
            weight=current["weight"],
            watching=tuple(current.get("watching") or ()),
            beats=tuple(current.get("beats") or ()),
            claim=current.get("claim", ""),
            confirms_when=tuple(current.get("confirms_when") or ()),
            contradicts_when=tuple(current.get("contradicts_when") or ()),
        )
    )


# ---------------------------------------------------------------------------
# Build the scan and corpus from explicit inputs.
# ---------------------------------------------------------------------------


def load_scan_file(path: Path | str) -> Scan:
    """Read a rotation scan from a JSON file: the market leg's only source.

    Shape (see README "Midweek letter")::

        {
          "benchmark": "BAA",
          "asof": "2026-02-09",
          "rows": [
            {"ticker": "BOT", "close": 103.0, "ret20": 3.0, "rs20": -1.0,
             "pct52w": 40.0, "trend": "below", "crossed": "down",
             "sma200": 110.0, "asof": "2026-02-09"}
          ]
        }

    ``benchmark`` and ``asof`` are required at the top level, ``ticker`` and
    ``close`` in every row; ``ret20``, ``ret60``, ``rs20``, ``pct52w``,
    ``sma200``, ``trend``, ``crossed`` and the row's own ``asof`` are
    optional and default to "not measured". Rows are sorted by ticker so
    two reads of the same file yield the same scan. Anything malformed
    raises ``LetterError`` naming the file, so the CLI can fail loudly
    instead of grading views against half a scan.
    """
    p = Path(path)
    try:
        text = p.read_bytes().decode("utf-8")
    except (UnicodeDecodeError, OSError) as exc:
        raise LetterError(f"scan file {p}: {exc}") from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise LetterError(f"scan file {p}: not valid JSON") from exc
    if not isinstance(payload, dict):
        raise LetterError(f"scan file {p}: expected a JSON object")
    benchmark = payload.get("benchmark")
    if not isinstance(benchmark, str) or not benchmark.strip():
        raise LetterError(f"scan file {p}: missing 'benchmark'")
    asof = payload.get("asof")
    if not isinstance(asof, str) or not asof.strip():
        raise LetterError(f"scan file {p}: missing 'asof'")
    _date_of(asof.strip(), f"scan file {p}: asof")
    raw_rows = payload.get("rows", [])
    if not isinstance(raw_rows, list):
        raise LetterError(f"scan file {p}: 'rows' must be a list")
    rows: list[ScanRow] = []
    for index, raw in enumerate(raw_rows):
        where = f"scan file {p}: row {index}"
        if not isinstance(raw, dict):
            raise LetterError(f"{where}: expected an object")
        ticker = raw.get("ticker")
        if not isinstance(ticker, str) or not ticker.strip():
            raise LetterError(f"{where}: missing 'ticker'")
        close = raw.get("close")
        if isinstance(close, bool) or not isinstance(close, (int, float)):
            raise LetterError(f"{where}: missing numeric 'close'")
        metrics: dict[str, float | None] = {}
        for metric in ("ret20", "ret60", "rs20", "pct52w", "sma200"):
            value = raw.get(metric)
            if value is None:
                metrics[metric] = None
            elif isinstance(value, bool) or not isinstance(value, (int, float)):
                raise LetterError(f"{where}: '{metric}' must be a number")
            else:
                metrics[metric] = float(value)
        trend = raw.get("trend", "—")
        if not isinstance(trend, str):
            raise LetterError(f"{where}: 'trend' must be a string")
        crossed = raw.get("crossed")
        if crossed is not None and crossed not in ("up", "down"):
            raise LetterError(f'{where}: \'crossed\' must be "up", "down" or null')
        row_asof = raw.get("asof", "")
        if not isinstance(row_asof, str):
            raise LetterError(f"{where}: 'asof' must be a string")
        rows.append(
            ScanRow(
                ticker=ticker.strip(),
                close=float(close),
                ret20=metrics["ret20"],
                ret60=metrics["ret60"],
                rs20=metrics["rs20"],
                pct52w=metrics["pct52w"],
                trend=trend,
                crossed=crossed,
                sma200=metrics["sma200"],
                asof=row_asof,
            )
        )
    rows.sort(key=lambda r: r.ticker)
    return Scan(benchmark=benchmark.strip(), asof=asof.strip(), rows=tuple(rows))


def build_corpus_from_mv_analyst_candidates(
    candidates: list,
) -> Corpus:
    """Read mv-analyst theme-axis candidates and emit one CorpusEvent per axis.

    The mv-analyst adapter's ``cand.summary`` carries ``Title (slug): N
    position(s) — guest: claim``. We project it back into the beat/slug/claim
    shape the gate's corpus leg expects, and use the candidate's
    ``identity`` as the stable ``key`` so a curated change to the positions
    board moves the event, not silence it.
    """
    grouped: dict[str, list[CorpusEvent]] = {}
    for cand in candidates:
        if getattr(cand, "kind", "") != "theme_axis":
            continue
        text = cand.summary
        title = ""
        slug = ""
        claim = ""
        if "(" in text and ")" in text:
            title = text.split("(")[0].strip()
            slug_part = text.split("(", 1)[1].split(")", 1)[0]
            slug = slug_part.split(":", 1)[0].strip()
            after = text.split(")", 1)[1].strip()
            # Drop the ``N position(s) — `` prefix; keep the guest claim.
            if " \u2014 " in after:
                claim = after.split(" \u2014 ", 1)[1].strip()
            elif " — " in after:
                claim = after.split(" — ", 1)[1].strip()
            else:
                claim = after
        beat_name = title or slug or "unknown"
        date = cand.as_of[:WEEK_LENGTH] if len(cand.as_of) >= WEEK_LENGTH else cand.as_of
        key = f"{slug}:{date}:{getattr(cand, 'identity', '') or beat_name}"
        grouped.setdefault(slug, []).append(
            CorpusEvent(
                beat_slug=slug,
                beat_name=beat_name,
                key=key,
                date=date,
                classes=("theme-axis",),
                guest="the corpus",
                claim=claim or beat_name,
                citation=str(cand.source_path),
            )
        )
    return Corpus(views=tuple((slug, tuple(events)) for slug, events in sorted(grouped.items())))


def build_views_from_documents(
    conn,
    *,
    body_for: callable,
    # callable(slug, kind, body) -> View | None; lets the caller decide which
    # documents are views (kind=='thesis', or any other rule).
) -> list[View]:
    """Read thesis-shaped documents from the store.

    ``body_for(slug)`` returns the body of one revision. A view's ``claim``
    must be parseable from the body in living-desk's shape; if it is not, the
    document is skipped. The CLI passes ``store.get_document_revision``.
    """
    out: list[View] = []
    rows = conn.execute("SELECT slug, kind, title FROM documents ORDER BY id").fetchall()
    for row in rows:
        slug = row["slug"]
        kind = row["kind"]
        if kind != "thesis":
            continue
        body = body_for(slug)
        if not body:
            continue
        views = _parse_views_text(body)
        if views:
            out.extend(views)
    return out


# ---------------------------------------------------------------------------
# Stage 1: deterministic composer + optional drafter.
# ---------------------------------------------------------------------------


def _quiet_line(result: LetterResult) -> str:
    on = result.date.isoformat()
    return f"{on}: nothing cleared the merit gate — no letter this week."


def rotation_chart(scan: Scan, *, width: int = 24) -> str:
    """Deterministic text visual of the rotation scan.

    A fixed-width table with three columns: ticker, a diverging bar around
    the centre, and the raw pp. An undefined percentile prints ``n/a``.
    """
    if not scan.rows:
        return "no scan rows for this week\n"
    ranked = sorted(
        scan.rows,
        key=lambda r: (r.rs20 is None, -(r.rs20 or 0.0)),
    )
    scale = max([abs(r.rs20 or 0.0) for r in ranked] or [1.0])
    out = [
        f"Rotation, {scan.asof} (relative to {scan.benchmark})",
        "",
        f"{'ticker':<8} {'rs20 (pp)':>10}  {'52w track':>10}  visual",
    ]
    for row in ranked:
        if row.rs20 is None:
            pp = "n/a"
            bar = ""
        else:
            pp = f"{row.rs20:+.2f}"
            half = round(width / 2 * min(abs(row.rs20) / scale, 1.0))
            if row.rs20 < 0:
                bar = " " * (width // 2 - half) + "█" * half + "|" + " " * (width // 2)
            else:
                bar = " " * (width // 2) + "|" + "█" * half + " " * (width // 2 - half)
        if row.pct52w is None:
            track = "n/a"
        else:
            track = f"{row.pct52w:5.0f}"
        out.append(f"{row.ticker:<8} {pp:>10}  {track:>10}  {bar}")
    out.append("")
    out.append(
        f"rs20 is each row's relative strength vs {scan.benchmark} in"
        f" percentage points; bars share a scale across the table. 52w"
        f" track is the percentile within the trailing range. Numbers come"
        f" from the scan; nothing here is composed."
    )
    return "\n".join(out)


def _lede(result: LetterResult, *, max_items: int = 5) -> list[str]:
    findings = list(result.findings)
    out: list[str] = []
    for f in findings[:max_items]:
        tag = " [same-day]" if f["speed"] == "same-day" else ""
        subject = f["key"]
        if f["source"] == "mv-analyst":
            subject = f"{f.get('guest') or 'the corpus'} on {f.get('key')}"
        out.append(
            f"{len(out) + 1}. **{subject} {f['verdict']}**"
            f" *{f['view_title']}* — {f['trigger']}{tag}"
        )
    extra = len(findings) - max_items
    if extra > 0:
        out.append("")
        out.append(f"...and {extra} more below, in full.")
    return out


def _packet(finding: dict) -> list[str]:
    head = f"### {finding['key']} — {finding['verdict']}"
    if finding["source"] == "mv-analyst":
        head = (
            f"### {finding.get('guest') or 'the corpus'},"
            f" {finding.get('date', '')} — {finding['verdict']}"
        )
    out = [head, ""]
    out.append(
        f"**The view it bears on.** *{finding['view_title']}*"
        f" (`{finding['view_id']}`, weight {finding['weight']})."
    )
    if finding["claim"]:
        out.extend(["", f"> {finding['claim']}", ""])
    out.append(f"**What happened.** {finding['summary']}")
    out.append("")
    out.append(f"**Why it cleared the gate.** {finding['trigger']}")
    if finding.get("evidence"):
        out.extend(["", "**Evidence.**", ""])
        out.extend([f"- {e}" for e in finding["evidence"]])
    if finding.get("citation"):
        out.extend(["", f"**Source.** {finding['citation']}"])
    out.append("")
    return out


def compose_letter(
    result: LetterResult,
    *,
    model: str | None,
    drafter: DrafterFn | None = None,
) -> str:
    """Stage 1: compose the letter body.

    Quiet days return a single concise line and never invoke a drafter.
    Active days emit the front matter, a lede, the deterministic visual,
    the finding packets, a "since the last issue" section and the
    appendix; if ``model`` is given, an optional drafter writes the lead
    summary paragraph. The data sections are deterministic — a model
    cannot drop them.
    """
    on = result.date.isoformat()
    if result.quiet:
        return _quiet_line(result)
    scan = result.scan
    if drafter is None and model is not None:

        def drafter(prompt: str, model_name: str) -> str:
            return default_drafter(prompt, model_name)

    summary = ""
    if drafter is not None and model is not None:
        summary = drafter(_prompt_for(result), model).strip()
        if not summary:
            raise LetterError("drafter returned empty output; refusing silent fallback")
    lines: list[str] = []
    if summary:
        lines.extend([summary, ""])
    if scan is None:
        intro = (
            "What cleared the mechanical merit gate this week, against"
            " the standing views in the hub's knowledge store. The market"
            " leg was off — no scan source was configured — so only the"
            " corpus leg ran; nothing here is composed."
        )
    else:
        intro = (
            "What cleared the mechanical merit gate this week, against"
            " the standing views in the hub's knowledge store. The"
            " rotation strip and 52-week tracks are drawn from the scan;"
            " nothing here is composed."
        )
    lines.extend(
        [
            "---",
            "product: Investing Hub Midweek Letter",
            f"issue: {on}",
            f"asof: {on}",
            f"findings: {len(result.findings)}",
            f"same_day: {sum(1 for f in result.findings if f['speed'] == 'same-day')}",
            f"views: {len(result.views)}",
            "---",
            "",
            f"# Midweek Letter — {on}",
            "",
            intro,
            "",
        ]
    )
    if scan is None:
        lines.extend(
            [
                "Market leg off: no scan source configured — the rotation"
                " strip and the 52-week tracks are absent this week, and no"
                " market finding can clear the gate.",
                "",
            ]
        )
    else:
        lines.extend([rotation_chart(scan), ""])
    lines.extend(["## The desk this week", ""])
    lines.extend(_lede(result))
    lines.extend(
        [
            "",
            (
                f"{len(result.findings)} finding(s) cleared the gate this"
                f" week, against {len(result.views)} view(s)."
                f" {len(result.suppressed)} repeat(s) inside the"
                f" {REPEAT_DAYS}-day window held back."
            ),
            "",
        ]
    )
    market = [f for f in result.findings if f["source"] == "scan"]
    corpus = [f for f in result.findings if f["source"] == "mv-analyst"]
    if scan is None:
        lines.extend(
            [
                "## What the scan did to your views",
                "",
                "Market leg off: no scan source configured — no view was"
                " graded against market data this week.",
                "",
            ]
        )
    elif market:
        lines.extend(
            [
                "## What the scan did to your views",
                "",
                "The rotation leg, scanned against every ticker your views"
                " watch. The rotation strip above ranks the universe; the"
                " packets below are only the rows that cleared the gate.",
                "",
            ]
        )
        for finding in market:
            lines.extend(_packet(finding))
    else:
        lines.extend(
            [
                "## What the scan did to your views",
                "",
                "Nothing in the scan reached a view this week.",
                "",
            ]
        )
    if corpus:
        lines.extend(
            [
                "## People you follow, and your themes",
                "",
                "The mv-analyst corpus, read through the theme axes the"
                " hub's views name. This section assembles the packet"
                " — the view in its own words, the corpus claim, the"
                " class the corpus assigned it — and stops. ``extends``"
                " is the honest label for *here is something that bears"
                " on this, you decide*.",
                "",
            ]
        )
        by_beat: dict[str, list[dict]] = {}
        for finding in corpus:
            by_beat.setdefault(finding.get("beat_name", "the corpus"), []).append(finding)
        for beat_name, group in by_beat.items():
            guests = []
            for finding in group:
                guest = finding.get("guest")
                if guest and guest not in guests:
                    guests.append(guest)
            lines.append(f"Beat **{beat_name}** — {', '.join(guests) or 'no guest named'}.")
            lines.append("")
            for finding in group:
                lines.extend(_packet(finding))
    else:
        lines.extend(
            [
                "## People you follow, and your themes",
                "",
                "Nothing in the corpus reached a view this week.",
                "",
            ]
        )
    lines.extend(
        [
            "## Standing views, as they stand",
            "",
            "Every view in the store, including the quiet ones. A view"
            " that disappears when it is quiet cannot be seen to be quiet.",
            "",
            "| View | Weight | Beats | Watching | This week |",
            "|---|---|---|---|---|",
        ]
    )
    for view in result.views:
        mine = [f for f in result.findings if f["view_id"] == view.id]
        if mine:
            verdicts = sorted({f["verdict"] for f in mine})
            note = f"{len(mine)} finding(s) — {', '.join(verdicts)}"
        else:
            note = "quiet"
        lines.append(
            f"| {view.title} `{view.id}` | {view.weight}"
            f" | {', '.join(view.beats) or '—'}"
            f" | {', '.join(view.watching) or '—'} | {note} |"
        )
    lines.append("")
    lines.extend(
        [
            "## Since the last issue",
            "",
        ]
    )
    if result.suppressed:
        lines.append("Held back as already said, inside the memory window:")
        lines.append("")
        for row in result.suppressed:
            lines.append(
                f"- `{row['key']}` on `{row['view_id']}`"
                f" — {row['verdict']}, last said {row['seen']}"
            )
        lines.append("")
    else:
        lines.append("Nothing was held back as a repeat this week.")
        lines.append("")
    if result.coverage_gaps:
        lines.append(
            "Your views name"
            f" {len(result.coverage_gaps)} beat(s) the corpus has not"
            " generated a view for yet —"
            f" {', '.join(f'`{slug}`' for slug in result.coverage_gaps)}."
            " That is a coverage gap, reported here and arbitrated"
            " nowhere."
        )
        lines.append("")
    asks = ask_candidates_for(result)
    if asks:
        lines.append("## Ask candidates")
        lines.append("")
        lines.append(
            "Findings that meet one of the four ask triggers. Recorded"
            " here as candidates; minting a Herald ask is a separate,"
            " supervised step."
        )
        lines.append("")
        for ask in asks:
            lines.append(
                f"- **trigger {ask['trigger']}** — *{ask['view_title']}*"
                f" ({ask['key']}, {ask['verdict']}): {ask['reason']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _prompt_for(result: LetterResult) -> str:
    on = result.date.isoformat()
    lines = [f"Date: {on}", ""]
    lines.append("Findings that cleared the merit gate:")
    for f in result.findings:
        lines.append(f"- {f['key']} | {f['verdict']} | {f['view_title']} | {f['trigger']}")
    lines.append("")
    lines.append(
        "Write a 1-4 sentence summary for the date above. The"
        " deterministic stage will follow with the rotation strip,"
        " lede and finding packets, so output ONLY the summary prose"
        " (you may lead with the date). No bullets, no lede, no"
        " rotation strip."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Ask triggers: a qualifying item may also raise a Herald ask.
# ---------------------------------------------------------------------------


# The four ask triggers. The numbers name them; they map to findings the
# gate emits: a thesis or position invalidation risk, a rung trigger
# firing or near, a dated call resolving that bears on the book, and a
# beats "would make it lead" condition met.
TRIGGER_INVALIDATION = 1  # thesis or position invalidation risk
TRIGGER_RUNG = 2  # a rung trigger firing or near
TRIGGER_CALL = 3  # a dated call resolving that bears on the book
TRIGGER_BEATS = 4  # a beats "would make it lead" condition met


def ask_candidates_for(result: LetterResult) -> list[dict]:
    """Map findings to ask candidates. Pure: no DB writes, no Herald mint."""
    out: list[dict] = []
    for f in result.findings:
        trigger: int | None = None
        reason: str = ""
        if f["verdict"] == "contradicts":
            trigger = TRIGGER_INVALIDATION
            reason = "contradicts the standing view outright — invalidation risk"
        elif f["verdict"] == "extends" and f["source"] == "triage-queue":
            if "200-day trend" in f["trigger"]:
                trigger = TRIGGER_RUNG
                reason = "instrument crossed its 200-day trend — rung trigger near"
            elif "52-week" in f["trigger"]:
                trigger = TRIGGER_RUNG
                reason = "instrument at a 52-week extreme — rung trigger near"
        elif f["source"] == "mv-analyst" and f["verdict"] == "extends":
            classes = set(f.get("classes") or ())
            if "lead" in (f.get("weight") or "") and classes:
                trigger = TRIGGER_BEATS
                reason = "mv-analyst foregrounded an event on a lead-weight view's beat"
            else:
                trigger = TRIGGER_CALL
                reason = "corpus event on a beat the view names — dated call resolving"
        if trigger is None:
            continue
        out.append(
            {
                "trigger": trigger,
                "reason": reason,
                "view_id": f["view_id"],
                "view_title": f["view_title"],
                "key": f["key"],
                "verdict": f["verdict"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# The CLI and CLI seams: drafter, sender, atomic write.
# ---------------------------------------------------------------------------


def default_drafter(
    prompt: str,
    model: str,
    *,
    timeout: float = DEFAULT_DRAFTER_TIMEOUT_S,
) -> str:
    """Default drafter: invoke ``$HUB_LETTER_DRAFT_CMD -p --model <model>``.

    ``HUB_LETTER_DRAFT_CMD`` defaults to ``claude``. A non-zero exit code,
    a timeout, an empty stdout or a missing binary all raise
    ``LetterError`` so the CLI can fail clearly without a silent template.
    """
    binary = os.environ.get("HUB_LETTER_DRAFT_CMD", DEFAULT_DRAFTER_CMD)
    try:
        proc = subprocess.run(
            [binary, "-p", "--model", model],
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise LetterError(f"drafter {binary!r} timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise LetterError(f"drafter binary not found: {binary!r}") from exc
    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        raise LetterError(
            f"drafter {binary!r} exited {proc.returncode}: {stderr_text or '<no stderr>'}"
        )
    out = proc.stdout.decode("utf-8").strip()
    if not out:
        raise LetterError(f"drafter {binary!r} returned empty output")
    return out


def write_text_atomic(path: Path | str, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file in the same directory plus
    ``os.replace``, so a crash mid-write never leaves a partial file."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, p)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def write_letter(
    result: LetterResult,
    *,
    out_dir: Path | str,
    model: str | None,
    drafter: DrafterFn | None = None,
) -> Path:
    """Stage 1 + write: ensure ``out_dir`` exists, persist the letter, return it.

    Quiet days do not write any file; ``write_letter`` raises ``ValueError``
    for them so a caller that mistakenly composes one cannot leak a stub
    document onto disk. An active-day drafter failure raises ``LetterError``
    and writes no file (a previous successful letter for the same date is
    left in place untouched). The final file is written via
    ``write_text_atomic`` so a crash mid-write never leaves a partial file.
    """
    if result.quiet:
        raise ValueError(
            f"write_letter: result for {result.date.isoformat()} is quiet; no letter to write"
        )
    text = compose_letter(result, model=model, drafter=drafter)
    out = Path(out_dir)
    out_path = out / f"{result.date.isoformat()}-midweek.md"
    write_text_atomic(out_path, text + "\n")
    return out_path


def default_sender(
    letter_path: Path,
    *,
    subject: str | None = None,
    timeout_s: float = DEFAULT_SEND_TIMEOUT_S,
) -> str:
    """Default sender: hand the letter path to ``$HUB_LETTER_SEND_CMD``.

    Mirrors the brief's email sender seam: the variable is the path the
    operator chooses to use (``mail-send.py``, ``ntfy``, anything else).
    ``HUB_LETTER_SEND_CMD`` unset is a **clear refusal** with a nonzero
    exit — there is no default that sends anything. The command receives
    ``--file <path>`` on its argv and ``HUB_LETTER_SUBJECT`` /
    ``HUB_LETTER_RECIPIENT`` in its environment when set.
    """
    binary = os.environ.get("HUB_LETTER_SEND_CMD", "").strip()
    if not binary:
        raise LetterError(
            "HUB_LETTER_SEND_CMD is unset; refusing to send. Set it to a"
            " sender command, e.g. a script that sends mail, to deliver the"
            " letter."
        )
    env = os.environ.copy()
    env.setdefault("HUB_LETTER_SUBJECT", subject or "Midweek Letter")
    try:
        proc = subprocess.run(
            [binary, "--file", str(letter_path)],
            capture_output=True,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise LetterError(f"sender {binary!r} timed out after {timeout_s:.0f}s") from exc
    except FileNotFoundError as exc:
        raise LetterError(f"sender binary not found: {binary!r}") from exc
    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        raise LetterError(
            f"sender {binary!r} exited {proc.returncode}: {stderr_text or '<no stderr>'}"
        )
    return proc.stdout.decode("utf-8", errors="replace").strip() or f"sent: {letter_path}"


# ---------------------------------------------------------------------------
# The CLI's stage 0 wrapper: collect views + scan + corpus + memory.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LetterContext:
    """The bundle stage 0 reads."""

    views: list[View] = field(default_factory=list)
    scan: Scan | None = None
    corpus: Corpus = field(default_factory=Corpus)
    memory: list[MemoryRecord] = field(default_factory=list)


def collect_letter(
    *,
    on: _date,
    views: list[View],
    scan: Scan | None,
    corpus: Corpus,
    memory: list[MemoryRecord],
) -> LetterResult:
    """Stage 0 wrapper: invoke the gate and return the result.

    Same canonical ordering as the underlying ``gate()``; ``scan is None``
    means no scan source is configured and the market leg is off. The
    wrapper exists so the CLI and tests can build their inputs
    independently and call a single deterministic entry point.
    """
    return gate(views, scan, corpus, memory, on.isoformat())


def compute_letter_id(asof: str) -> str:
    """Stable id for one issue date."""
    return asof
