# Yahoo daily history fixtures

Recorded responses from Yahoo Finance's chart API on 2026-10-08.

- URL form: `https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=2y&interval=1d`
- Capture date: 2026-10-08
- Tickers: `SPY`, `XLK`, `XLF`, `XLE`

Each response is trimmed only by removing fields the reader never reads
(`indicators.adjclose`, `indicators.quote[0].open`, `high`, `low`, `volume`,
and `meta`). The `timestamp` and `indicators.quote[0].close` arrays are preserved
verbatim as returned by the live endpoint.

The last bar in each fixture was captured during the 2026-10-08 session and is
not a completed close; the reader excludes run-date sessions and scores only
completed sessions dated strictly before the run date.
