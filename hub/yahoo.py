"""Yahoo daily-history reader for the midweek letter's market leg.

Fetches daily history via Yahoo Finance's chart API, parses the response
into ascending ``(date, close)`` pairs, and delegates scoring to
``hub.scan_score``.

Opt in with ``hub letter midweek --scan-source yahoo --scan-benchmark SPY``.
Watched view tickers are normalized to canonical symbols.
The daily history endpoint is
``https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d``.
"""

from __future__ import annotations

import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import UTC, date, datetime
from urllib.parse import quote

from hub.letter import Scan
from hub.scan_score import (
    ScoreError,
    require_benchmark,
    scan_from_rows,
    score_row,
    trim_series,
)

TIMEOUT_SECONDS = 20.0
MAX_BYTES = 5_000_000
_DAILY_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d"
_SYMBOL = re.compile(r"^[A-Za-z0-9.^][A-Za-z0-9.^-]{0,20}$")


class YahooError(Exception):
    """A Yahoo read failed, or the scan was refused. The message is the note."""


def symbol_for(ticker: str) -> str:
    """Map a view ticker to a canonical Yahoo symbol. Rejects invalid symbols."""
    cleaned = ticker.strip()
    if not _SYMBOL.match(cleaned):
        raise YahooError(f"not a Yahoo symbol: {ticker!r}")
    return cleaned.upper()


def daily_history_url(symbol: str) -> str:
    """The public daily-history chart URL for an already-mapped symbol."""
    return _DAILY_URL.format(symbol=quote(symbol, safe=".^-"))


def fetch_json(
    symbol: str,
    *,
    timeout: float = TIMEOUT_SECONDS,
    max_bytes: int = MAX_BYTES,
) -> str:
    """GET one symbol's daily history JSON. Raises ``YahooError`` on any failure."""
    url = daily_history_url(symbol)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise YahooError(f"HTTP {status}")
            raw = response.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise YahooError(f"body exceeds {max_bytes} bytes")
            return raw.decode("utf-8")
    except YahooError:
        raise
    except urllib.error.HTTPError as exc:
        raise YahooError(f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise YahooError(f"HTTP error: {exc.reason}") from None
    except TimeoutError:
        raise YahooError("HTTP timeout") from None
    except UnicodeDecodeError:
        raise YahooError("malformed json") from None


def parse_json(text: str) -> tuple[tuple[str, float], ...]:
    """Parse Yahoo Finance chart JSON into ascending ``(date, close)`` pairs."""
    trimmed = text.strip()
    if not trimmed:
        raise YahooError("empty json")
    if trimmed.startswith("<"):
        raise YahooError("HTML response: endpoint refused")
    try:
        data = json.loads(text)
    except Exception as exc:
        raise YahooError(f"malformed json: {exc}") from None

    if not isinstance(data, dict):
        raise YahooError("malformed json: root must be object")

    chart = data.get("chart")
    if not isinstance(chart, dict):
        raise YahooError("malformed json: missing chart object")

    err = chart.get("error")
    if err is not None:
        raise YahooError(f"chart error: {err}")

    results = chart.get("result")
    if not results or not isinstance(results, list):
        raise YahooError("missing or empty chart result")

    r0 = results[0]
    if not isinstance(r0, dict):
        raise YahooError("malformed chart result item")

    timestamps = r0.get("timestamp")
    indicators = r0.get("indicators")
    if not isinstance(indicators, dict):
        raise YahooError("missing indicators")

    quotes = indicators.get("quote")
    if not quotes or not isinstance(quotes, list) or not isinstance(quotes[0], dict):
        raise YahooError("missing quote indicators")

    closes = quotes[0].get("close")
    if timestamps is None or closes is None:
        raise YahooError("missing timestamp or close array")

    if not isinstance(timestamps, list) or not isinstance(closes, list):
        raise YahooError("timestamp and close must be lists")

    if len(timestamps) == 0 or len(closes) == 0:
        raise YahooError("empty history")

    if len(timestamps) != len(closes):
        raise YahooError(
            f"mismatched array lengths: {len(timestamps)} timestamps vs {len(closes)} closes"
        )

    parsed: list[tuple[str, float]] = []
    seen_dates: set[str] = set()

    for ts, close in zip(timestamps, closes, strict=True):
        if close is None:
            raise YahooError("null close")
        if not isinstance(close, (int, float)) or isinstance(close, bool):
            raise YahooError("non-numeric close")
        close_float = float(close)
        if math.isnan(close_float) or math.isinf(close_float):
            raise YahooError("non-finite close")
        if close_float <= 0.0:
            raise YahooError("non-positive close")

        if not isinstance(ts, int) or isinstance(ts, bool):
            raise YahooError("invalid timestamp")
        session_date = datetime.fromtimestamp(ts, UTC).date().isoformat()
        if session_date in seen_dates:
            raise YahooError(f"duplicate session date: {session_date}")
        seen_dates.add(session_date)
        parsed.append((session_date, close_float))

    parsed.sort(key=lambda pair: pair[0])
    return tuple(parsed)


def build_scan(
    tickers: Sequence[str],
    benchmark: str,
    *,
    asof_date: date | str,
    fetcher: Callable[[str], str] | None = None,
) -> tuple[Scan, tuple[str, ...]]:
    """Fetch the benchmark and each watched ticker, score what is usable.

    A problem with one ticker omits that ticker and returns a note. A problem
    with the benchmark refuses the whole scan (``YahooError``) so no letter is
    graded against a missing or stubbed relative-strength base. The benchmark
    is not itself a row.
    """
    if asof_date is None:
        raise TypeError("asof_date is required")
    read = fetcher or fetch_json
    bench_name = benchmark.strip()
    if not bench_name:
        raise YahooError("refusing scan: benchmark is missing")
    try:
        bench_symbol = symbol_for(bench_name)
    except YahooError as exc:
        raise YahooError(f"refusing scan: benchmark {bench_name}: {exc}") from None

    try:
        raw_bench = parse_json(_read(bench_symbol, read))
        bench_series = trim_series(raw_bench, asof_date=asof_date)
        asof = require_benchmark(bench_series, asof_date=asof_date)
    except (YahooError, ScoreError) as exc:
        raise YahooError(f"refusing scan: benchmark {bench_name}: {exc}") from None

    notes: list[str] = []
    rows = []
    seen_symbols: set[str] = set()
    canon_bench = bench_symbol

    for ticker in tickers:
        name = ticker.strip()
        if not name:
            continue
        try:
            canon = symbol_for(name)
        except YahooError as exc:
            notes.append(f"{name}: {exc}")
            continue
        if canon == canon_bench or canon in seen_symbols:
            continue
        seen_symbols.add(canon)
        try:
            raw_series = parse_json(_read(canon, read))
            series = trim_series(raw_series, asof_date=asof_date)
            rows.append(score_row(name, series, bench_series, asof_date=asof_date))
        except (YahooError, ScoreError) as exc:
            notes.append(f"{name}: {exc}")

    notes.sort()
    return scan_from_rows(bench_name, asof, rows), tuple(notes)


def _read(symbol: str, fetcher: Callable[[str], str]) -> str:
    try:
        text = fetcher(symbol)
    except YahooError:
        raise
    except Exception as exc:
        raise YahooError(f"HTTP error: {exc}") from None
    return text
