"""Keyless Stooq daily-history reader for the midweek letter's market leg.

Fetching and CSV parsing stop at dated closes. Scoring lives in
``hub.scan_score`` and is not done here. Tests inject ``fetcher``; nothing
in the test suite calls the network.

A bare ticker such as ``SPY`` is requested as ``spy.us``. A ticker that
already carries a dot, or an index symbol starting with ``^``, is sent
as written (lowercased). The daily CSV endpoint is
``https://stooq.com/q/d/l/?s=<symbol>&i=d``.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date
from urllib.parse import quote

from hub.letter import Scan
from hub.scan_score import ScoreError, require_benchmark, scan_from_rows, score_row

TIMEOUT_SECONDS = 20.0
MAX_BYTES = 5_000_000
_DAILY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
_SYMBOL = re.compile(r"^[A-Za-z0-9.^][A-Za-z0-9.^-]{0,20}$")


class StooqError(Exception):
    """A Stooq read failed, or the scan was refused. The message is the note."""


def symbol_for(ticker: str) -> str:
    """Map a view ticker to a Stooq symbol. Rejects anything that is not a symbol."""
    cleaned = ticker.strip()
    if not _SYMBOL.fullmatch(cleaned):
        raise StooqError(f"not a Stooq symbol: {ticker!r}")
    lowered = cleaned.lower()
    if "." in lowered or lowered.startswith("^"):
        return lowered
    return f"{lowered}.us"


def daily_history_url(symbol: str) -> str:
    """The public daily-history CSV URL for an already-mapped symbol."""
    return _DAILY_URL.format(symbol=quote(symbol, safe=".^"))


def fetch_csv(
    symbol: str,
    *,
    timeout: float = TIMEOUT_SECONDS,
    max_bytes: int = MAX_BYTES,
) -> str:
    """GET one symbol's daily CSV. Raises ``StooqError`` on any HTTP failure."""
    url = daily_history_url(symbol)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "investing-hub/0.0.1", "Accept": "text/csv"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise StooqError(f"HTTP {status}")
            raw = response.read(max_bytes + 1)
    except StooqError:
        raise
    except urllib.error.HTTPError as exc:
        raise StooqError(f"HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise StooqError(f"HTTP error: {exc.reason}") from None
    except TimeoutError:
        raise StooqError("HTTP timeout") from None
    if len(raw) > max_bytes:
        raise StooqError("malformed csv: response too large")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise StooqError("malformed csv") from None


def parse_csv(text: str) -> tuple[tuple[str, float], ...]:
    """Parse a Stooq daily CSV into ascending ``(date, close)`` pairs.

    Empty input, Stooq's ``No data`` line, a missing ``Close`` column, a
    non-numeric close, a bad date, or a duplicate date raises ``StooqError``.
    Rows are not dropped to keep a partial series.
    """
    if not isinstance(text, str) or not text.strip():
        raise StooqError("empty csv")
    body = text.lstrip("\ufeff").strip()
    lowered = body.lower()
    if "<html" in lowered or lowered.startswith("<"):
        raise StooqError("malformed csv")
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if not lines:
        raise StooqError("empty csv")
    if lines[0].lower() in {"no data", "exceeded the daily hits limit"}:
        raise StooqError("no data")
    if len(lines) >= 2 and lines[1].lower() in {"no data", "exceeded the daily hits limit"}:
        raise StooqError("no data")
    header = [part.strip().strip('"').lower() for part in lines[0].split(",")]
    if "date" not in header or "close" not in header:
        raise StooqError("malformed csv")
    date_at = header.index("date")
    close_at = header.index("close")
    width = max(date_at, close_at) + 1
    parsed: list[tuple[str, float]] = []
    for line in lines[1:]:
        fields = [part.strip().strip('"') for part in line.split(",")]
        if len(fields) < width:
            raise StooqError("malformed csv")
        try:
            session = date.fromisoformat(fields[date_at]).isoformat()
        except ValueError:
            raise StooqError("malformed csv") from None
        try:
            close = float(fields[close_at])
        except ValueError:
            raise StooqError("non-numeric close") from None
        if close != close or close in {float("inf"), float("-inf")}:
            raise StooqError("non-numeric close")
        parsed.append((session, close))
    if not parsed:
        raise StooqError("empty csv")
    parsed.sort()
    for index in range(1, len(parsed)):
        if parsed[index][0] == parsed[index - 1][0]:
            raise StooqError("malformed csv: duplicate session date")
    return tuple(parsed)


def build_scan(
    tickers: Sequence[str],
    benchmark: str,
    *,
    fetcher: Callable[[str], str] | None = None,
) -> tuple[Scan, tuple[str, ...]]:
    """Fetch the benchmark and each watched ticker, score what is usable.

    A problem with one ticker omits that ticker and returns a note. A problem
    with the benchmark refuses the whole scan (``StooqError``) so no letter is
    graded against a missing or stubbed relative-strength base. The benchmark
    is not itself a row.
    """
    read = fetcher or fetch_csv
    bench_name = benchmark.strip()
    if not bench_name:
        raise StooqError("refusing scan: benchmark is missing")
    try:
        bench_series = parse_csv(_read(symbol_for(bench_name), read))
        asof = require_benchmark(bench_series)
    except (StooqError, ScoreError) as exc:
        raise StooqError(f"refusing scan: benchmark {bench_name}: {exc}") from None
    notes: list[str] = []
    rows = []
    seen: set[str] = set()
    for ticker in tickers:
        name = ticker.strip()
        if not name or name == bench_name or name in seen:
            continue
        seen.add(name)
        try:
            series = parse_csv(_read(symbol_for(name), read))
            rows.append(score_row(name, series, bench_series))
        except (StooqError, ScoreError) as exc:
            notes.append(f"{name}: {exc}")
    notes.sort()
    return scan_from_rows(bench_name, asof, rows), tuple(notes)


def _read(symbol: str, fetcher: Callable[[str], str]) -> str:
    try:
        text = fetcher(symbol)
    except StooqError:
        raise
    except Exception as exc:
        raise StooqError(f"HTTP error: {exc}") from None
    if not isinstance(text, str):
        raise StooqError("malformed csv")
    return text
