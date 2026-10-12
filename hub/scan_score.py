"""Score daily closes into the midweek letter's scan row.

Pure: no HTTP, no clock, no file reads. A series that cannot support a
52-week read is refused. Nothing here substitutes a zero, a midpoint, or
any other stand-in number.

Definitions, all on sessions aligned to the benchmark's latest date:

* ``ret20`` / ``ret60`` — percent change versus the close 20 or 60
  sessions earlier.
* ``rs20`` / ``rs60`` — that return minus the benchmark's, in percentage
  points.
* ``pct52w`` — where today's close sits in the high-low range of the
  trailing 252 closes, 0 at the low and 100 at the high.
* ``sma200`` — arithmetic mean of the last 200 closes.
* ``trend`` — ``above`` or ``below`` that mean, or ``—`` when the close
  equals it.
* ``crossed`` — ``up`` or ``down`` when today's close moved through the
  200-session mean from the other side (or from on the mean); otherwise
  none. Yesterday's mean is the 200 closes ending yesterday.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, datetime
from numbers import Real

from hub.letter import Scan, ScanRow

# A 52-week window in trading sessions. Shorter history cannot place a
# close in that range, so the ticker is left out of the scan.
MIN_SESSIONS = 252
MIN_CALENDAR_DAYS = 330
MAX_CALENDAR_DAYS = 400
RET20 = 20
RET60 = 60
SMA_N = 200

DateInput = date | datetime | str
Series = Sequence[tuple[DateInput, float]]
NormalizedSeries = Sequence[tuple[str, float]]


class ScoreError(Exception):
    """This close series must not become a scan row."""


def score_row(
    ticker: str,
    series: Series,
    benchmark: Series,
    *,
    asof_date: DateInput,
) -> ScanRow:
    """One ticker against the benchmark series. Raises ``ScoreError`` to omit it."""
    if asof_date is None:
        raise TypeError("asof_date is required")
    series_trimmed = _validated_series(series, before=asof_date)
    bench_trimmed = _validated_series(benchmark, before=asof_date)
    asof = require_benchmark(bench_trimmed, asof_date=asof_date)
    trimmed = _aligned(series_trimmed, asof)
    bench = _aligned(bench_trimmed, asof)
    _require_window(trimmed)
    _require_window(bench)
    closes = [close for _, close in trimmed]
    bench_closes = [close for _, close in bench]
    close = closes[-1]
    low, high = min(closes[-MIN_SESSIONS:]), max(closes[-MIN_SESSIONS:])
    # high == low is already refused by _require_window.
    pct52w = _finite_metric("pct52w", (close - low) / (high - low) * 100.0)
    ret20 = _finite_metric("ret20", _return(closes, RET20))
    ret60 = _finite_metric("ret60", _return(closes, RET60))
    rs20 = _finite_metric("rs20", ret20 - _return(bench_closes, RET20))
    rs60 = _finite_metric("rs60", ret60 - _return(bench_closes, RET60))
    sma200 = _finite_metric("sma200", sum(closes[-SMA_N:]) / SMA_N)
    trend = _trend(close, sma200)
    crossed = _crossed(closes)
    return ScanRow(
        ticker=ticker,
        close=close,
        ret20=ret20,
        ret60=ret60,
        rs20=rs20,
        rs60=rs60,
        pct52w=pct52w,
        trend=trend,
        crossed=crossed,
        sma200=sma200,
        asof=asof,
    )


def trim_series(series: Series, asof_date: DateInput) -> tuple[tuple[str, float], ...]:
    """Validate and retain completed sessions dated strictly before the run date."""
    if asof_date is None:
        raise TypeError("asof_date is required")
    return _validated_series(series, before=asof_date)


def require_benchmark(series: Series, asof_date: DateInput) -> str:
    """Refuse a benchmark that cannot anchor relative strength. Returns its as-of date."""
    if asof_date is None:
        raise TypeError("asof_date is required")
    validated = _validated_series(series, before=asof_date)
    if not validated:
        raise ScoreError("no sessions")
    asof = validated[-1][0]
    _require_window(validated)
    target = _calendar_date(asof_date, context="run date")
    bench_d = date.fromisoformat(asof)
    days = (target - bench_d).days
    if days > 7:
        raise ScoreError(
            f"stale history ({asof} is {days} calendar days before {target.isoformat()})"
        )
    return asof


def scan_from_rows(benchmark: str, asof: str, rows: Sequence[ScanRow]) -> Scan:
    """The same ``Scan`` the JSON path produces, rows sorted by ticker."""
    ordered = tuple(sorted(rows, key=lambda row: row.ticker))
    return Scan(benchmark=benchmark, asof=asof, rows=ordered)


def _aligned(series: NormalizedSeries, asof: str) -> list[tuple[str, float]]:
    trimmed = [(day, close) for day, close in series if day <= asof]
    if not trimmed or trimmed[-1][0] != asof:
        raise ScoreError(f"no session on {asof}")
    return trimmed


def _require_window(series: Series) -> None:
    validated = _validated_series(series)
    if len(validated) < MIN_SESSIONS:
        raise ScoreError(f"fewer than {MIN_SESSIONS} sessions")
    window = validated[-MIN_SESSIONS:]
    d_start = date.fromisoformat(window[0][0])
    d_end = date.fromisoformat(window[-1][0])
    span = (d_end - d_start).days
    if span < MIN_CALENDAR_DAYS or span > MAX_CALENDAR_DAYS:
        raise ScoreError(
            f"252-session window spans {span} calendar days "
            f"(expected {MIN_CALENDAR_DAYS}–{MAX_CALENDAR_DAYS})"
        )
    closes = [close for _, close in window]
    if any(close <= 0 for close in closes):
        raise ScoreError("non-positive close")
    if min(closes) == max(closes):
        raise ScoreError("constant closes")


def _validated_series(
    series: Series, *, before: DateInput | None = None
) -> tuple[tuple[str, float], ...]:
    """Normalize dates and enforce one strictly increasing session per calendar day."""
    target = _calendar_date(before, context="run date") if before is not None else None
    validated: list[tuple[str, float]] = []
    previous: date | None = None
    seen: set[date] = set()
    for index, (raw_day, close) in enumerate(series):
        current = _calendar_date(raw_day, context=f"series index {index}")
        current_text = current.isoformat()
        if isinstance(close, bool) or not isinstance(close, Real):
            raise ScoreError(
                f"series index {index} date {current_text} has non-real close {close!r}"
            )
        try:
            close_value = float(close)
        except (OverflowError, TypeError, ValueError):
            raise ScoreError(
                f"series index {index} date {current_text} has non-real close {close!r}"
            ) from None
        if not math.isfinite(close_value):
            raise ScoreError(
                f"series index {index} date {current_text} has non-finite close {close!r}"
            )
        if current in seen:
            raise ScoreError(f"series index {index} duplicates date {current_text}")
        seen.add(current)
        if previous is not None and current < previous:
            raise ScoreError(
                f"series index {index} date {current_text} is not after {previous.isoformat()}"
            )
        previous = current
        if target is None or current < target:
            validated.append((current_text, close_value))
    return tuple(validated)


def _calendar_date(value: DateInput, *, context: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ScoreError(f"{context} has invalid date {value!r}")


def _return(closes: Sequence[float], sessions: int) -> float:
    close = closes[-1]
    earlier = closes[-1 - sessions]
    if earlier <= 0 or close <= 0:
        raise ScoreError("non-positive close")
    return (close / earlier - 1.0) * 100.0


def _finite_metric(name: str, value: float) -> float:
    if not math.isfinite(value):
        raise ScoreError(f"non-finite metric {name}")
    return value


def _trend(close: float, sma200: float) -> str:
    if close > sma200:
        return "above"
    if close < sma200:
        return "below"
    return "—"


def _crossed(closes: Sequence[float]) -> str | None:
    close = closes[-1]
    prev_close = closes[-2]
    sma_today = _finite_metric("sma200", sum(closes[-SMA_N:]) / SMA_N)
    sma_prev = _finite_metric("prior sma200", sum(closes[-SMA_N - 1 : -1]) / SMA_N)
    if prev_close <= sma_prev and close > sma_today:
        return "up"
    if prev_close >= sma_prev and close < sma_today:
        return "down"
    return None
