"""Daily pulse composer: stage 0 deterministic evaluation + stage 1 draft.

Stage 0 inspects the local SQLite store for items that moved on the date in scope —
`findings` created on that day (or still pending), `calls` whose ``resolves_by`` is
that day and whose ``resolution`` is not set, and ``beats_proposals`` created on
that day with ``status = 'proposed'``.

Stage 0 separates the items by the high Herald ask threshold:
  - ``high_bar`` — items with ``score >= ask_threshold``
  - ``worth_discussing`` — items with ``min_score <= score < ask_threshold``

When nothing qualifies, the date is a "quiet day".

Stage 1 composes the brief section from deterministic templates. ``--model`` is
recorded in an HTML comment so the draft is auditable; no network call is made.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date as _date, datetime
from pathlib import Path

DEFAULT_ASK_THRESHOLD = 0.7
DEFAULT_MIN_SCORE = 0.0
DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_OUT_DIR = Path("out/book")

# Dated calls resolving today and newly-proposed beats count as high-bar evidence
# by construction. Their default score sits above the default ask threshold so the
# operator never has to tune it.
HIGH_BAR_SCORE = 0.85

ITEM_KIND_FINDING = "finding"
ITEM_KIND_CALL = "call"
ITEM_KIND_BEAT = "beat"

DAY_LENGTH = 10  # length of 'YYYY-MM-DD'


class PulseError(Exception):
    """A pulse operation failed in a way the operator must see."""


@dataclass(frozen=True)
class PulseItem:
    """One piece of evidence the stage 0 sweep picked up."""

    kind: str             # ITEM_KIND_*
    subject: str
    detail: str           # deterministic evidence: trigger kind + timestamp
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

    Every input is local; nothing about this function reaches outside the
    database connection. Two calls with the same store and the same date return
    equal results.
    """
    on_str = on.isoformat()
    items: list[PulseItem] = []

    # Findings: created today, or still pending (carried over from earlier days).
    rows = conn.execute(
        "SELECT kind, subject, score, status, created_at FROM findings"
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
        "SELECT subject, call, resolves_by, resolution FROM calls"
    ).fetchall()
    for row in rows:
        if row["resolves_by"] != on_str or row["resolution"] is not None:
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
        "SELECT proposal, status, created_at FROM beats_proposals"
    ).fetchall()
    for row in rows:
        if row["status"] != "proposed" or _date_of(row["created_at"]) != on_str:
            continue
        items.append(
            PulseItem(
                kind=ITEM_KIND_BEAT,
                subject=row["proposal"],
                detail=f"beat @ {row['created_at']}",
                score=HIGH_BAR_SCORE,
            )
        )

    high_bar = tuple(item for item in items if item.score >= ask_threshold)
    worth_discussing = tuple(
        item for item in items if ask_threshold > item.score >= min_score
    )
    return PulseResult(
        date=on,
        quiet=not items,
        high_bar=high_bar,
        worth_discussing=worth_discussing,
    )


def compose(result: PulseResult, *, model: str | None = None) -> str:
    """Stage 1: deterministic draft of the brief section.

    The same ``result`` and ``model`` always return the same string. The model
    name (if given) sits in an HTML comment so the provenance is auditable.
    """
    on_str = result.date.isoformat()
    if result.quiet:
        body = (
            f"{on_str}: Steady day; no book-level thesis invalidations or"
            " rung triggers active."
        )
        if model:
            return f"<!-- model: {model} -->\n{body}"
        return body

    lines = [f"{on_str}: book-level activity:"]
    for item in result.high_bar:
        lines.append(f"- {item.subject} ({item.detail}; score {item.score:.2f})")
    if result.worth_discussing:
        lines.append("Worth discussing:")
        for item in result.worth_discussing:
            lines.append(f"- {item.subject} ({item.detail}; score {item.score:.2f})")
    body = "\n".join(lines)
    if model:
        return f"<!-- model: {model} -->\n{body}"
    return body


def write_pulse(
    result: PulseResult,
    *,
    out_dir: Path,
    model: str | None = None,
) -> Path:
    """Stage 1 + write: ensure ``out_dir`` exists, persist the draft, return the path."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{result.date.isoformat()}.md"
    out_path.write_text(compose(result, model=model) + "\n", encoding="utf-8")
    return out_path