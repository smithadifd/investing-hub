"""Daily pulse composer: stage 0 deterministic evaluation + stage 1 draft.

Stage 0 inspects the local SQLite store for items that moved on the date in scope —
``findings`` created on that day (or still pending), ``calls`` whose ``resolves_by``
is that day and whose ``resolution`` is not set, and ``beats_proposals`` created on
that day with ``status = 'proposed'``.

Stage 0 separates the items by the high Herald ask threshold:
  - ``high_bar`` — items with ``score >= ask_threshold``
  - ``worth_discussing`` — items with ``min_score <= score < ask_threshold``

When nothing qualifies, the date is a "quiet day".

Stage 1 composes the brief section:
  - Quiet day: one concise deterministic line; no drafter is invoked.
  - Active day: a drafter is invoked with the explicit ``--model`` and a prompt
    built from the Stage 0 evidence; the high-bar bullets and ``Worth discussing:``
    section are then appended deterministically so a model cannot drop them.

Ordering is canonical: every Stage 0 query is ``ORDER BY id`` and the merged
``high_bar`` / ``worth_discussing`` tuples are sorted by ``(-score, detail,
kind, subject)`` — so the same store seeded in two different insertion orders
produces identical results.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import date as _date
from pathlib import Path

DEFAULT_ASK_THRESHOLD = 0.7
DEFAULT_MIN_SCORE = 0.0
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_OUT_DIR = Path("out/book")
DEFAULT_DRAFTER_CMD = "claude"
DEFAULT_DRAFTER_TIMEOUT_S = 30.0

# Dated calls resolving today and newly-proposed beats count as high-bar evidence
# by construction. Their default score sits above the default ask threshold so the
# operator never has to tune it.
HIGH_BAR_SCORE = 0.85

ITEM_KIND_FINDING = "finding"
ITEM_KIND_CALL = "call"
ITEM_KIND_BEAT = "beat"

DAY_LENGTH = 10  # length of 'YYYY-MM-DD'

# A drafter takes the prompt and the explicit model name and returns the drafted
# text. The default implementation shells out to ``$HUB_PULSE_DRAFT_CMD -p
# --model <model>``; tests inject a fake.
DrafterFn = Callable[[str, str], str]


class PulseError(Exception):
    """A pulse operation failed in a way the operator must see."""


@dataclass(frozen=True)
class PulseItem:
    """One piece of evidence the stage 0 sweep picked up."""

    kind: str  # ITEM_KIND_*
    subject: str
    detail: str  # deterministic evidence: trigger kind + timestamp
    score: float


@dataclass(frozen=True)
class PulseResult:
    """What stage 0 found for one date."""

    date: _date
    quiet: bool
    high_bar: tuple[PulseItem, ...] = ()
    worth_discussing: tuple[PulseItem, ...] = ()


def today_utc() -> _date:
    """Today's date in UTC (the writer runs once a day, so timezone matters)."""
    return datetime.now(UTC).date()


def _date_of(text: str | None) -> str | None:
    """Return the leading YYYY-MM-DD of an ISO-8601 UTC timestamp, or None."""
    if not text or len(text) < DAY_LENGTH:
        return None
    return text[:DAY_LENGTH]


def collect_pulse(
    conn,
    *,
    on: _date,
    ask_threshold: float = DEFAULT_ASK_THRESHOLD,
    min_score: float = DEFAULT_MIN_SCORE,
) -> PulseResult:
    """Stage 0: deterministic scan of the store for ``on`` (UTC date).

    Every query is ``ORDER BY id``; the merged ``high_bar`` and
    ``worth_discussing`` tuples are then sorted by ``(-score, detail, kind,
    subject)`` so two stores seeded with the same rows in different insertion
    orders return equal ``PulseResult``s.
    """
    on_str = on.isoformat()
    items: list[PulseItem] = []

    # Findings: created today, or still pending (carried over from earlier days).
    rows = conn.execute(
        "SELECT kind, subject, score, status, created_at FROM findings ORDER BY id"
    ).fetchall()
    for row in rows:
        if _date_of(row["created_at"]) != on_str and row["status"] != "pending":
            continue
        try:
            score = float(row["score"])
        except (TypeError, ValueError):
            continue
        if score < min_score:
            continue
        items.append(
            PulseItem(
                kind=ITEM_KIND_FINDING,
                subject=row["subject"],
                detail=f"{row['kind']} @ {row['created_at']}",
                score=score,
            )
        )

    # Dated calls: resolves_by == on, resolution not yet set.
    rows = conn.execute(
        "SELECT subject, call, resolves_by, resolution FROM calls ORDER BY id"
    ).fetchall()
    for row in rows:
        if row["resolves_by"] != on_str or row["resolution"] is not None:
            continue
        if HIGH_BAR_SCORE < min_score:
            continue
        items.append(
            PulseItem(
                kind=ITEM_KIND_CALL,
                subject=row["subject"],
                detail=f"call resolves {on_str}: {row['call']}",
                score=HIGH_BAR_SCORE,
            )
        )

    # Beats proposals: created today, status='proposed'.
    rows = conn.execute(
        "SELECT proposal, status, created_at FROM beats_proposals ORDER BY id"
    ).fetchall()
    for row in rows:
        if row["status"] != "proposed" or _date_of(row["created_at"]) != on_str:
            continue
        if HIGH_BAR_SCORE < min_score:
            continue
        items.append(
            PulseItem(
                kind=ITEM_KIND_BEAT,
                subject=row["proposal"],
                detail=f"beat @ {row['created_at']}",
                score=HIGH_BAR_SCORE,
            )
        )

    items.sort(key=lambda item: (-item.score, item.detail, item.kind, item.subject))
    high_bar = tuple(item for item in items if item.score >= ask_threshold)
    worth_discussing = tuple(item for item in items if ask_threshold > item.score >= min_score)
    return PulseResult(
        date=on,
        quiet=not (high_bar or worth_discussing),
        high_bar=high_bar,
        worth_discussing=worth_discussing,
    )


def _quiet_line(result: PulseResult) -> str:
    on_str = result.date.isoformat()
    return f"{on_str}: Steady day; no book-level thesis invalidations or rung triggers active."


def _worth_discussing_block(result: PulseResult) -> str:
    """The deterministic 'Worth discussing:' section built from Stage 0."""
    if not result.worth_discussing:
        return ""
    lines = ["Worth discussing:"]
    for item in result.worth_discussing:
        lines.append(f"- {item.subject} ({item.detail}; score {item.score:.2f})")
    return "\n".join(lines)


def _high_bar_bullets(result: PulseResult) -> list[str]:
    """The deterministic high-bar bullet lines built from Stage 0."""
    return [f"- {item.subject} ({item.detail}; score {item.score:.2f})" for item in result.high_bar]


def compose_deterministic(result: PulseResult) -> str:
    """Stage 1 skeleton without a drafter call: the deterministic active-day frame.

    Quiet days return the single concise line; active days return the date
    header, high-bar bullets and ``Worth discussing:`` section. Used by
    ``--dry-run`` so the operator can see the deterministic frame without
    invoking any model.
    """
    if result.quiet:
        return _quiet_line(result)
    on_str = result.date.isoformat()
    lines = [f"{on_str}: book-level activity:"]
    lines.extend(_high_bar_bullets(result))
    worth = _worth_discussing_block(result)
    if worth:
        lines.append(worth)
    return "\n".join(lines)


def default_drafter(
    prompt: str,
    model: str,
    *,
    timeout: float = DEFAULT_DRAFTER_TIMEOUT_S,
) -> str:
    """Default drafter: invoke ``$HUB_PULSE_DRAFT_CMD -p --model <model>`` with the prompt on stdin.

    ``HUB_PULSE_DRAFT_CMD`` defaults to ``claude``. A non-zero exit code, a
    timeout or empty stdout all raise ``PulseError`` so the CLI can fail
    clearly without falling back to a silent template.
    """
    binary = os.environ.get("HUB_PULSE_DRAFT_CMD", DEFAULT_DRAFTER_CMD)
    try:
        proc = subprocess.run(
            [binary, "-p", "--model", model],
            input=prompt.encode("utf-8"),
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise PulseError(f"drafter {binary!r} timed out after {timeout:.0f}s") from exc
    except FileNotFoundError as exc:
        raise PulseError(f"drafter binary not found: {binary!r}") from exc
    if proc.returncode != 0:
        stderr_text = proc.stderr.decode("utf-8", errors="replace").strip()
        raise PulseError(
            f"drafter {binary!r} exited {proc.returncode}: {stderr_text or '<no stderr>'}"
        )
    out = proc.stdout.decode("utf-8").strip()
    if not out:
        raise PulseError(f"drafter {binary!r} returned empty output")
    return out


def _prompt_for(result: PulseResult) -> str:
    """The prompt passed to the drafter on an active day."""
    on_str = result.date.isoformat()
    lines = [f"Date: {on_str}", ""]
    lines.append("High-bar items (score >= ask threshold):")
    for item in result.high_bar:
        lines.append(f"- {item.subject} | {item.detail} | score={item.score:.2f}")
    if result.worth_discussing:
        lines.append("")
        lines.append("Worth-discussing items (score < ask threshold):")
        for item in result.worth_discussing:
            lines.append(f"- {item.subject} | {item.detail} | score={item.score:.2f}")
    lines.append("")
    lines.append(
        "Write a 1-4 sentence book-level summary for the date above. "
        "The deterministic stage will follow with high-bar bullets and a "
        "'Worth discussing:' section, so output ONLY the summary prose "
        "(you may lead with the date). No bullets, no 'Worth discussing:'."
    )
    return "\n".join(lines)


def compose(
    result: PulseResult,
    *,
    model: str,
    drafter: DrafterFn | None = None,
) -> str:
    """Stage 1: compose the brief section.

    A quiet day writes one concise line and never invokes a drafter. An active
    day invokes ``drafter(prompt, model)`` (default: a subprocess drafter
    configured by ``HUB_PULSE_DRAFT_CMD``), then appends the deterministic
    high-bar bullets and ``Worth discussing:`` section so a model cannot drop
    them. An empty drafter return value raises ``PulseError`` — there is no
    silent template fallback.
    """
    if result.quiet:
        return _quiet_line(result)
    fn = drafter or default_drafter
    summary = fn(_prompt_for(result), model).rstrip("\n")
    if not summary:
        raise PulseError("drafter returned empty output; refusing silent fallback")
    lines = [summary, *_high_bar_bullets(result)]
    worth = _worth_discussing_block(result)
    if worth:
        lines.append(worth)
    return "\n".join(lines)


def write_pulse(
    result: PulseResult,
    *,
    out_dir: Path,
    model: str,
    drafter: DrafterFn | None = None,
) -> Path:
    """Stage 1 + write: ensure ``out_dir`` exists, persist the draft, return the path.

    If the drafter fails, times out or returns empty output, raises
    ``PulseError`` and does NOT create the artifact file — a previous
    successful ``<date>.md`` for the same date is left in place untouched.
    The final file is written via a temp file in the same directory plus
    ``os.replace`` so a crash mid-write never leaves a partial file.
    """
    text = compose(result, model=model, drafter=drafter)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{result.date.isoformat()}.md"
    tmp_fd, tmp_name = tempfile.mkstemp(dir=out_dir, prefix=f".{out_path.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp_name, out_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise
    return out_path
