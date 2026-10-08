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

from collections.abc import Sequence

from hub.letter import Scan, ScanRow

# A 52-week window in trading sessions. Shorter history cannot place a
# close in that range, so the ticker is left out of the scan.
MIN_SESSIONS = 252
RET20 = 20
RET60 = 60
SMA_N = 200

Series = Sequence[tuple[str, float]]


class ScoreError(Exception):
    """This close series must not become a scan row."""


def score_row(ticker: str, series: Series, benchmark: Series) -> ScanRow:
    """One ticker against the benchmark series. Raises ``ScoreError`` to omit it."""
    if not benchmark:
        raise ScoreError("benchmark has no sessions")
    asof = benchmark[-1][0]
    trimmed = _aligned(series, asof)
    bench = _aligned(benchmark, asof)
    _require_window(trimmed)
    _require_window(bench)
    closes = [close for _, close in trimmed]
    bench_closes = [close for _, close in bench]
    close = closes[-1]
    low, high = min(closes[-MIN_SESSIONS:]), max(closes[-MIN_SESSIONS:])
    # high == low is already refused by _require_window.
    pct52w = (close - low) / (high - low) * 100.0
    ret20 = _return(closes, RET20)
    ret60 = _return(closes, RET60)
    rs20 = ret20 - _return(bench_closes, RET20)
    rs60 = ret60 - _return(bench_closes, RET60)
    sma200 = sum(closes[-SMA_N:]) / SMA_N
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


def require_benchmark(series: Series) -> str:
    """Refuse a benchmark that cannot anchor relative strength. Returns its as-of date."""
    if not series:
        raise ScoreError("no sessions")
    asof = series[-1][0]
    trimmed = _aligned(series, asof)
    _require_window(trimmed)
    return asof


def scan_from_rows(benchmark: str, asof: str, rows: Sequence[ScanRow]) -> Scan:
    """The same ``Scan`` the JSON path produces, rows sorted by ticker."""
    ordered = tuple(sorted(rows, key=lambda row: row.ticker))
    return Scan(benchmark=benchmark, asof=asof, rows=ordered)


def _aligned(series: Series, asof: str) -> list[tuple[str, float]]:
    trimmed = [(day, close) for day, close in series if day <= asof]
    if not trimmed or trimmed[-1][0] != asof:
        raise ScoreError(f"no session on {asof}")
    return trimmed


def _require_window(series: Sequence[tuple[str, float]]) -> None:
    if len(series) < MIN_SESSIONS:
        raise ScoreError(f"fewer than {MIN_SESSIONS} sessions")
    closes = [close for _, close in series[-MIN_SESSIONS:]]
    if any(close <= 0 for close in closes):
        raise ScoreError("non-positive close")
    if min(closes) == max(closes):
        raise ScoreError("constant closes")


def _return(closes: Sequence[float], sessions: int) -> float:
    close = closes[-1]
    earlier = closes[-1 - sessions]
    if earlier <= 0 or close <= 0:
        raise ScoreError("non-positive close")
    return (close / earlier - 1.0) * 100.0


def _trend(close: float, sma200: float) -> str:
    if close > sma200:
        return "above"
    if close < sma200:
        return "below"
    return "—"


def _crossed(closes: Sequence[float]) -> str | None:
    close = closes[-1]
    prev_close = closes[-2]
    sma_today = sum(closes[-SMA_N:]) / SMA_N
    sma_prev = sum(closes[-SMA_N - 1 : -1]) / SMA_N
    if prev_close <= sma_prev and close > sma_today:
        return "up"
    if prev_close >= sma_prev and close < sma_today:
        return "down"
    return None
