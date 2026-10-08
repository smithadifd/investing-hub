"""Tests for the Yahoo daily-history reader and the midweek letter's market leg.

Fixtures under ``tests/fixtures/yahoo/`` are recorded Yahoo chart JSON files.
No test calls the network; ``urlopen`` is patched to fail if production reaches it.
"""

from __future__ import annotations

import json
import urllib.error
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from hub import scan_score, store, yahoo
from hub.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "yahoo"
VIEWS = """
### Sector leadership
Id: sector-rs
Weight: lead
Watching: XLK
Claim: Technology is leading the broad tape.
Confirms when: XLK rs20 >= 0
Contradicts when: XLK rs20 <= -100

### Range position
Id: financials-range
Weight: standing
Watching: XLF
Claim: Financials are high in their own trailing range.
Confirms when: XLF pct52w >= 50
Contradicts when: XLF pct52w <= 0

### Energy trend
Id: energy-trend
Weight: watch
Watching: XLE
Claim: Energy is being watched for a trend break.
"""


def _weekdays(n: int, end: date = date(2026, 10, 8)) -> list[date]:
    """Generate ``n`` weekdays ending on ``end`` in ascending order."""
    dates = []
    current = end
    while len(dates) < n:
        if current.weekday() < 5:
            dates.append(current)
        current -= timedelta(days=1)
    return list(reversed(dates))


def _dated(closes: list[float], *, end: date = date(2026, 10, 8)) -> tuple[tuple[str, float], ...]:
    days = _weekdays(len(closes), end=end)
    return tuple((d.isoformat(), close) for d, close in zip(days, closes, strict=True))


def _flat(level: float, n: int = 252, last: float | None = None) -> list[float]:
    closes = [level] * n
    if last is not None:
        closes[-1] = last
    return closes


def _bench(last: float = 104.0) -> tuple[tuple[str, float], ...]:
    return _dated(_flat(100.0, last=last))


def _json(closes: list[float], *, end: date = date(2026, 10, 8)) -> str:
    days = _weekdays(len(closes), end=end)
    timestamps = [
        int(datetime(d.year, d.month, d.day, 13, 30, tzinfo=UTC).timestamp()) for d in days
    ]
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "close": closes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    return json.dumps(payload)


def _json_from_dated(series: Sequence[tuple[str, float]]) -> str:
    timestamps = [int(datetime.fromisoformat(f"{d}T13:30:00+00:00").timestamp()) for d, _ in series]
    closes = [close for _, close in series]
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "close": closes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    return json.dumps(payload)


def _forbid_network(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("test hit the network")

    monkeypatch.setattr(yahoo.urllib.request, "urlopen", boom)


def _fixture_fetcher(extra: dict[str, str] | None = None):
    files = {
        path.name.removesuffix(".json"): path.read_text(encoding="utf-8")
        for path in FIXTURES.glob("*.json")
    }
    if extra:
        files.update(extra)

    def fetch(symbol: str) -> str:
        key = symbol.lower()
        try:
            return files[key]
        except KeyError:
            raise yahoo.YahooError(f"no data for {symbol}") from None

    return fetch


def _seed(db: Path, body: str) -> None:
    conn = store.connect(db)
    store.insert_document_revision(
        conn,
        slug="theses/sector-rs",
        kind="thesis",
        title="Sector leadership",
        body=body,
        source_kind="session",
        source_ref="test",
    )
    conn.close()


def _cli(db: Path, tmp_path: Path, *extra: str) -> list[str]:
    return [
        "letter",
        "midweek",
        "--date",
        "2026-10-08",
        "--db",
        str(db),
        "--out-dir",
        str(tmp_path / "out"),
        *extra,
    ]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(path)]) == 0
    return path


def test_symbol_for_maps_a_bare_ticker():
    assert yahoo.symbol_for("SPY") == "SPY"
    assert yahoo.symbol_for("spy") == "SPY"
    assert yahoo.symbol_for("^GSPC") == "^GSPC"
    assert yahoo.symbol_for("BRK-B") == "BRK-B"
    assert yahoo.symbol_for("brk-b") == "BRK-B"


def test_symbol_for_rejects_a_ticker_that_is_not_a_symbol():
    with pytest.raises(yahoo.YahooError, match="not a Yahoo symbol"):
        yahoo.symbol_for("SPY/../secret")


def test_daily_history_url_is_the_chart_endpoint():
    assert (
        yahoo.daily_history_url("SPY")
        == "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=2y&interval=1d"
    )


def test_fetch_json_requests_that_url_and_returns_the_body(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def read(self, _n=-1):
            return b'{"chart": {"result": [], "error": null}}'

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(request.headers)
        return _Resp()

    monkeypatch.setattr(yahoo.urllib.request, "urlopen", fake_urlopen)
    body = yahoo.fetch_json("SPY")
    assert "chart" in body
    assert (
        captured["url"]
        == "https://query1.finance.yahoo.com/v8/finance/chart/SPY?range=2y&interval=1d"
    )
    assert captured["timeout"] == yahoo.TIMEOUT_SECONDS
    assert captured["headers"].get("Accept") == "application/json"
    assert captured["headers"].get("User-agent") == "Mozilla/5.0"


def test_fetch_json_refuses_an_http_error(monkeypatch):
    def fake_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 503, "unavailable", hdrs=None, fp=None)

    monkeypatch.setattr(yahoo.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(yahoo.YahooError, match="HTTP 503"):
        yahoo.fetch_json("SPY")


def test_fetch_json_refuses_a_transport_error(monkeypatch):
    def fake_urlopen(_request, timeout=0):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(yahoo.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(yahoo.YahooError, match="HTTP error"):
        yahoo.fetch_json("SPY")


def test_fetch_json_refuses_a_truncated_body(monkeypatch):
    class _Resp:
        status = 200

        def read(self, _n=-1):
            return b"x" * 32

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(yahoo.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(yahoo.YahooError, match="body exceeds 10 bytes"):
        yahoo.fetch_json("SPY", max_bytes=10)


def test_parse_reads_close_and_sorts_chronologically():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791379800, 1791293400],
                    "indicators": {
                        "quote": [
                            {
                                "close": [2.5, 1.5],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }
    parsed = yahoo.parse_json(json.dumps(payload))
    assert len(parsed) == 2
    assert parsed[0][1] == 1.5
    assert parsed[1][1] == 2.5
    assert parsed[0][0] < parsed[1][0]


def test_parse_refuses_empty_json():
    with pytest.raises(yahoo.YahooError, match="empty json"):
        yahoo.parse_json("   ")


def test_parse_refuses_html_response():
    with pytest.raises(yahoo.YahooError, match="HTML response"):
        yahoo.parse_json("<!DOCTYPE html><html><body>Browser check</body></html>")


def test_parse_refuses_malformed_json():
    with pytest.raises(yahoo.YahooError, match="malformed json"):
        yahoo.parse_json("not valid json at all")
    with pytest.raises(yahoo.YahooError, match="root must be object"):
        yahoo.parse_json("[1, 2, 3]")
    with pytest.raises(yahoo.YahooError, match="missing chart object"):
        yahoo.parse_json('{"foo": "bar"}')


def test_parse_refuses_chart_error():
    payload = {
        "chart": {
            "result": None,
            "error": {"code": "Not Found", "description": "No data found for symbol"},
        }
    }
    with pytest.raises(yahoo.YahooError, match="chart error"):
        yahoo.parse_json(json.dumps(payload))


def test_parse_refuses_missing_or_empty_arrays():
    with pytest.raises(yahoo.YahooError, match="missing or empty chart result"):
        yahoo.parse_json('{"chart": {"result": [], "error": null}}')
    with pytest.raises(yahoo.YahooError, match="missing indicators"):
        yahoo.parse_json('{"chart": {"result": [{"timestamp": [1]}], "error": null}}')
    with pytest.raises(yahoo.YahooError, match="missing timestamp or close array"):
        yahoo.parse_json('{"chart": {"result": [{"indicators": {"quote": [{}]}}], "error": null}}')
    with pytest.raises(yahoo.YahooError, match="empty history"):
        empty_payload = {
            "chart": {
                "result": [{"timestamp": [], "indicators": {"quote": [{"close": []}]}}],
                "error": None,
            }
        }
        yahoo.parse_json(json.dumps(empty_payload))


def test_parse_refuses_mismatched_array_lengths():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791293400, 1791379800],
                    "indicators": {"quote": [{"close": [1.5]}]},
                }
            ],
            "error": None,
        }
    }
    with pytest.raises(yahoo.YahooError, match="mismatched array lengths"):
        yahoo.parse_json(json.dumps(payload))


def test_parse_refuses_null_close():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791293400, 1791379800],
                    "indicators": {"quote": [{"close": [1.5, None]}]},
                }
            ],
            "error": None,
        }
    }
    with pytest.raises(yahoo.YahooError, match="null close"):
        yahoo.parse_json(json.dumps(payload))


def test_parse_refuses_a_non_numeric_close():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791293400],
                    "indicators": {"quote": [{"close": ["1.5"]}]},
                }
            ],
            "error": None,
        }
    }
    with pytest.raises(yahoo.YahooError, match="non-numeric close"):
        yahoo.parse_json(json.dumps(payload))


def test_parse_refuses_non_finite_close():
    payload_nan = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791293400],
                    "indicators": {"quote": [{"close": [float("nan")]}]},
                }
            ],
            "error": None,
        }
    }
    with pytest.raises(yahoo.YahooError, match="non-finite close"):
        yahoo.parse_json(json.dumps(payload_nan))


def test_parse_refuses_non_positive_close():
    payload_zero = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791293400],
                    "indicators": {"quote": [{"close": [0.0]}]},
                }
            ],
            "error": None,
        }
    }
    with pytest.raises(yahoo.YahooError, match="non-positive close"):
        yahoo.parse_json(json.dumps(payload_zero))

    payload_neg = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791293400],
                    "indicators": {"quote": [{"close": [-10.0]}]},
                }
            ],
            "error": None,
        }
    }
    with pytest.raises(yahoo.YahooError, match="non-positive close"):
        yahoo.parse_json(json.dumps(payload_neg))


def test_parse_refuses_a_duplicate_session():
    payload = {
        "chart": {
            "result": [
                {
                    "timestamp": [1791293400, 1791293400],
                    "indicators": {"quote": [{"close": [1.5, 2.0]}]},
                }
            ],
            "error": None,
        }
    }
    with pytest.raises(yahoo.YahooError, match="duplicate session"):
        yahoo.parse_json(json.dumps(payload))


def test_pct52w_is_position_in_the_trailing_range():
    closes = [100.0] * 252
    closes[0] = 80.0
    closes[1] = 120.0
    row = scan_score.score_row("XLK", _dated(closes), _bench(), asof_date=date(2026, 10, 9))
    assert row.pct52w == pytest.approx(50.0)


def test_pct52w_uses_trailing_252_not_the_whole_file():
    closes = [100.0] * 260
    closes[0] = 1.0  # outside the 252-session window
    closes[8] = 50.0  # inside the window, outside the last 200
    closes[-1] = 80.0
    row = scan_score.score_row(
        "XLK", _dated(closes), _dated(_flat(100.0, 260, last=104.0)), asof_date=date(2026, 10, 9)
    )
    assert row.pct52w == pytest.approx(60.0)


def test_rs20_is_excess_return_in_percentage_points():
    ticker = _flat(100.0, last=110.0)  # +10% over 20 sessions
    bench = _flat(100.0, last=104.0)  # +4%
    row = scan_score.score_row("XLK", _dated(ticker), _dated(bench), asof_date=date(2026, 10, 9))
    assert row.ret20 == pytest.approx(10.0)
    assert row.rs20 == pytest.approx(6.0)


def test_rs60_is_excess_60_session_return():
    ticker = _flat(100.0, last=110.0)
    bench = _flat(100.0, last=104.0)
    row = scan_score.score_row("XLK", _dated(ticker), _dated(bench), asof_date=date(2026, 10, 9))
    assert row.ret60 == pytest.approx(10.0)
    assert row.rs60 == pytest.approx(6.0)


def test_sma200_is_mean_of_last_200_closes():
    closes = _flat(100.0, last=110.0)
    row = scan_score.score_row("XLK", _dated(closes), _bench(), asof_date=date(2026, 10, 9))
    assert row.sma200 == pytest.approx((199 * 100 + 110) / 200)


def test_trend_above_below_and_level():
    above = scan_score.score_row(
        "XLK", _dated(_flat(100.0, last=110.0)), _bench(), asof_date=date(2026, 10, 9)
    )
    below = scan_score.score_row(
        "XLK", _dated(_flat(100.0, last=90.0)), _bench(), asof_date=date(2026, 10, 9)
    )
    level = [90.0] + [100.0] * 251
    equal = scan_score.score_row("XLK", _dated(level), _bench(), asof_date=date(2026, 10, 9))
    assert above.trend == "above"
    assert below.trend == "below"
    assert equal.trend == "—"


def test_crossed_up_from_on_the_average():
    row = scan_score.score_row(
        "XLK", _dated(_flat(100.0, last=110.0)), _bench(), asof_date=date(2026, 10, 9)
    )
    assert row.crossed == "up"


def test_crossed_down_from_on_the_average():
    row = scan_score.score_row(
        "XLE", _dated(_flat(100.0, last=90.0)), _bench(), asof_date=date(2026, 10, 9)
    )
    assert row.crossed == "down"


def test_no_cross_when_price_stays_above_the_average():
    closes = [90.0] + [110.0] * 249 + [120.0, 120.0]
    row = scan_score.score_row("XLK", _dated(closes), _bench(), asof_date=date(2026, 10, 9))
    assert row.trend == "above"
    assert row.crossed is None


def test_crossed_fails_if_sma_prev_equals_sma_today():
    """Yesterday's mean uses the 200 closes ending yesterday, not today's mean.

    When a high close drops out of the 200-session window, sma_prev > sma_today.
    If yesterday's close was at or below sma_prev but above sma_today, the upward
    cross condition holds only when sma_prev is correctly computed.
    """
    # 201 closes: Day -201 is 300.0, Days -200..-3 are 100.0, Day -2 is 100.5, Day -1 is 100.2
    # sma_prev = (300 + 198*100 + 100.5)/200 = 101.0025
    # sma_today = (198*100 + 100.5 + 100.2)/200 = 100.0035
    # prev_close (100.5) <= sma_prev (101.0025) is True
    # prev_close (100.5) <= sma_today (100.0035) is False
    # close (100.2) > sma_today (100.0035) is True
    closes = [300.0] + [100.0] * 198 + [100.5, 100.2]
    assert scan_score._crossed(closes) == "up"


def test_score_uses_the_benchmark_date_not_a_later_ticker_close():
    bench = _dated(_flat(100.0, last=104.0), end=date(2026, 10, 7))
    ticker = _dated(_flat(100.0, last=110.0), end=date(2026, 10, 7))
    series = ticker + (("2026-10-08", 999.0),)
    row = scan_score.score_row("XLK", series, bench, asof_date=date(2026, 10, 9))
    assert row.asof == bench[-1][0]
    assert row.close == 110.0


def test_score_refuses_a_ticker_with_no_session_on_the_benchmark_date():
    bench = _dated(_flat(100.0, last=104.0))
    short_end = _dated(_flat(100.0, 252, last=110.0), end=date(2026, 10, 1))
    with pytest.raises(scan_score.ScoreError, match="no session on"):
        scan_score.score_row("XLK", short_end, bench, asof_date=date(2026, 10, 9))


def test_score_refuses_fewer_than_252_sessions():
    bench = _bench()
    short_series = _dated(_flat(100.0, 251, last=110.0))
    with pytest.raises(scan_score.ScoreError, match="fewer than 252"):
        scan_score.score_row("XLK", short_series, bench, asof_date=date(2026, 10, 9))


def test_score_accepts_exactly_252_sessions():
    row = scan_score.score_row(
        "XLK", _dated(_flat(100.0, 252, last=110.0)), _bench(), asof_date=date(2026, 10, 9)
    )
    assert row.pct52w == pytest.approx(100.0)


def test_score_refuses_constant_closes():
    with pytest.raises(scan_score.ScoreError, match="constant closes"):
        scan_score.score_row(
            "XLK", _dated(_flat(100.0, 252)), _bench(), asof_date=date(2026, 10, 9)
        )


def test_score_refuses_a_constant_window_even_if_older_closes_move():
    closes = [50.0] * 8 + [100.0] * 252
    with pytest.raises(scan_score.ScoreError, match="constant closes"):
        scan_score.score_row(
            "XLK",
            _dated(closes),
            _dated(_flat(100.0, 260, last=104.0)),
            asof_date=date(2026, 10, 9),
        )


def test_score_refuses_a_non_positive_close():
    closes = _flat(100.0, last=110.0)
    closes[10] = 0.0
    with pytest.raises(scan_score.ScoreError, match="non-positive close"):
        scan_score.score_row("XLK", _dated(closes), _bench(), asof_date=date(2026, 10, 9))


def test_score_refuses_252_sessions_spanning_fewer_than_330_calendar_days():
    # 252 consecutive calendar days ending on benchmark date span only 251 calendar days
    end = date(2026, 10, 8)
    start = end - timedelta(days=251)
    consecutive = [((start + timedelta(days=i)).isoformat(), 100.0) for i in range(251)] + [
        (end.isoformat(), 110.0)
    ]
    with pytest.raises(scan_score.ScoreError, match="spans 251 calendar days"):
        scan_score.score_row("XLK", consecutive, _bench(), asof_date=date(2026, 10, 9))


def test_score_refuses_252_sessions_spanning_more_than_400_calendar_days():
    # 252 weekly sessions ending on benchmark date span 251 * 7 = 1757 calendar days
    end = date(2026, 10, 8)
    weekly = [((end - timedelta(weeks=251 - i)).isoformat(), 100.0) for i in range(251)] + [
        (end.isoformat(), 110.0)
    ]
    with pytest.raises(scan_score.ScoreError, match="spans 1757 calendar days"):
        scan_score.score_row("XLK", weekly, _bench(), asof_date=date(2026, 10, 9))


def test_require_benchmark_refuses_a_stubbed_series():
    with pytest.raises(scan_score.ScoreError, match="constant closes"):
        scan_score.require_benchmark(
            _dated(_flat(100.0, 252), end=date(2026, 10, 7)), asof_date=date(2026, 10, 8)
        )
    with pytest.raises(scan_score.ScoreError, match="fewer than 252"):
        scan_score.require_benchmark(
            _dated(_flat(100.0, 10), end=date(2026, 10, 7)), asof_date=date(2026, 10, 8)
        )


def test_require_benchmark_refuses_stale_history_more_than_7_days_before_date():
    bench = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 9, 25))
    with pytest.raises(scan_score.ScoreError, match="stale history"):
        scan_score.require_benchmark(bench, asof_date=date(2026, 10, 8))


def test_require_benchmark_accepts_session_within_7_days_before_date():
    bench = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 1))
    assert scan_score.require_benchmark(bench, asof_date=date(2026, 10, 8)) == "2026-10-01"


def test_require_benchmark_defends_against_untrimmed_run_date_session():
    bench = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 8))
    with pytest.raises(
        scan_score.ScoreError,
        match="benchmark session 2026-10-08 is not strictly before 2026-10-08",
    ):
        scan_score.require_benchmark(bench, asof_date=date(2026, 10, 8))


def test_require_benchmark_refuses_session_after_run_date():
    bench = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 9))
    with pytest.raises(
        scan_score.ScoreError,
        match="benchmark session 2026-10-09 is not strictly before 2026-10-08",
    ):
        scan_score.require_benchmark(bench, asof_date=date(2026, 10, 8))


def test_require_benchmark_requires_asof_date():
    bench = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 7))
    with pytest.raises(TypeError):
        scan_score.require_benchmark(bench)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        scan_score.require_benchmark(bench, asof_date=None)  # type: ignore[arg-type]


def test_build_scan_without_asof_date_raises_type_error():
    with pytest.raises(TypeError):
        yahoo.build_scan(["XLK"], "SPY")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        yahoo.build_scan(["XLK"], "SPY", asof_date=None)  # type: ignore[arg-type]


def test_score_row_requires_asof_date():
    bench = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 7))
    ticker = _dated(_flat(100.0, 252, last=110.0), end=date(2026, 10, 7))
    with pytest.raises(TypeError):
        scan_score.score_row("XLK", ticker, bench)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        scan_score.score_row("XLK", ticker, bench, asof_date=None)  # type: ignore[arg-type]


def test_build_scan_omits_a_bad_ticker_and_keeps_a_good_one():
    good = _json(_flat(100.0, 252, last=110.0))
    short = _json(_flat(100.0, 10, last=110.0))
    bench = _json(_flat(100.0, 252, last=104.0))

    def fetch(symbol: str) -> str:
        return {"SPY": bench, "XLK": good, "QQQ": short}[symbol]

    scan, notes = yahoo.build_scan(
        ["XLK", "QQQ"], "SPY", fetcher=fetch, asof_date=date(2026, 10, 9)
    )
    assert [row.ticker for row in scan.rows] == ["XLK"]
    assert any("QQQ" in note for note in notes)


def test_build_scan_refuses_a_stubbed_benchmark():
    constant = _json(_flat(100.0, 260))

    def fetch(_symbol: str) -> str:
        return constant

    with pytest.raises(yahoo.YahooError, match="refusing scan: benchmark SPY: constant closes"):
        yahoo.build_scan(["XLK"], "SPY", fetcher=fetch, asof_date=date(2026, 10, 9))


def test_build_scan_refuses_when_the_benchmark_fetch_fails():
    def fetch(_symbol: str) -> str:
        raise yahoo.YahooError("HTTP 503")

    with pytest.raises(yahoo.YahooError, match="refusing scan: benchmark SPY: HTTP 503"):
        yahoo.build_scan(["XLK"], "SPY", fetcher=fetch, asof_date=date(2026, 10, 9))


def test_build_scan_refuses_stale_benchmark():
    stale_bench = _json(_flat(100.0, 252, last=104.0), end=date(2026, 9, 20))

    def fetch(_symbol: str) -> str:
        return stale_bench

    with pytest.raises(yahoo.YahooError, match="refusing scan: benchmark SPY: stale history"):
        yahoo.build_scan(["XLK"], "SPY", fetcher=fetch, asof_date=date(2026, 10, 8))


def test_build_scan_case_insensitive_benchmark_exclusion():
    text = _json(_flat(100.0, 252, last=110.0))
    fetched_symbols = []

    def fetch(symbol: str) -> str:
        fetched_symbols.append(symbol)
        return text

    # View watching "SPY" with --scan-benchmark "spy"
    scan, notes = yahoo.build_scan(
        ["SPY", "XLK"], "spy", fetcher=fetch, asof_date=date(2026, 10, 9)
    )
    # Neither fetches SPY twice nor emits SPY as a row
    assert fetched_symbols == ["SPY", "XLK"]
    assert [row.ticker for row in scan.rows] == ["XLK"]

    # Also deduplicates tickers and preserves display spelling
    fetched_symbols.clear()
    scan2, _ = yahoo.build_scan(["XLK", "xlk"], "SPY", fetcher=fetch, asof_date=date(2026, 10, 9))
    assert fetched_symbols == ["SPY", "XLK"]
    assert [row.ticker for row in scan2.rows] == ["XLK"]


def test_trim_series_excludes_sessions_on_and_after_asof_date():
    series = (
        ("2026-10-06", 100.0),
        ("2026-10-07", 101.0),
        ("2026-10-08", 102.0),
        ("2026-10-09", 103.0),
    )
    trimmed = scan_score.trim_series(series, asof_date=date(2026, 10, 8))
    assert trimmed == (("2026-10-06", 100.0), ("2026-10-07", 101.0))
    with pytest.raises(TypeError):
        scan_score.trim_series(series, asof_date=None)  # type: ignore[arg-type]


def test_trim_series_requires_asof_date():
    series = (("2026-10-07", 100.0),)
    with pytest.raises(TypeError):
        scan_score.trim_series(series)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        scan_score.trim_series(series, asof_date=None)  # type: ignore[arg-type]


def test_unit_run_date_session_close_never_reaches_row():
    run_date = date(2026, 10, 8)
    bench_completed = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 7))
    ticker_completed = _dated(_flat(100.0, 252, last=110.0), end=date(2026, 10, 7))
    bench_payload = _json_from_dated(bench_completed + (("2026-10-08", 99999.0),))
    ticker_payload = _json_from_dated(ticker_completed + (("2026-10-08", 88888.0),))

    def fetch(symbol: str) -> str:
        return ticker_payload if symbol == "XLK" else bench_payload

    scan, notes = yahoo.build_scan(["XLK"], "SPY", fetcher=fetch, asof_date=run_date)
    assert notes == ()
    assert scan.asof == "2026-10-07"
    assert len(scan.rows) == 1
    row = scan.rows[0]
    assert row.asof == "2026-10-07"
    assert row.close == pytest.approx(110.0)
    assert row.close != 88888.0
    assert row.ret20 == pytest.approx(10.0)
    assert row.rs20 == pytest.approx(6.0)
    assert row.trend == "above"


def test_score_row_with_run_date_session_never_uses_that_close():
    run_date = date(2026, 10, 8)
    bench_completed = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 7))
    ticker_completed = _dated(_flat(100.0, 252, last=110.0), end=date(2026, 10, 7))
    bench = bench_completed + (("2026-10-08", 99999.0),)
    ticker = ticker_completed + (("2026-10-08", 88888.0),)
    row = scan_score.score_row("XLK", ticker, bench, asof_date=run_date)
    assert row.asof == "2026-10-07"
    assert row.close == pytest.approx(110.0)
    assert row.close != 88888.0
    assert row.ret20 == pytest.approx(10.0)
    assert row.rs20 == pytest.approx(6.0)


def test_score_row_probe_run_date_bar_trimmed():
    """XLK/SPY fixtures on 2026-10-08 must trim the 2026-10-08 bar."""
    run_date = date(2026, 10, 8)
    fixtures = _fixture_fetcher()
    spy_series = yahoo.parse_json(fixtures("SPY"))
    xlk_series = yahoo.parse_json(fixtures("XLK"))
    row = scan_score.score_row("XLK", xlk_series, spy_series, asof_date=run_date)
    assert row.asof == "2026-10-07"
    assert row.close != pytest.approx(197.32000732421875)


def test_unit_earlier_asof_date_ignores_later_fixture_sessions():
    fixtures = _fixture_fetcher()
    scan_clean, _ = yahoo.build_scan(
        ["XLK", "XLF", "XLE"], "SPY", fetcher=fixtures, asof_date=date(2026, 10, 1)
    )
    assert scan_clean.asof <= "2026-09-30"
    assert scan_clean.asof == "2026-09-30"
    for row in scan_clean.rows:
        assert row.asof == "2026-09-30"

    def corrupted_fetcher(symbol: str) -> str:
        raw = fixtures(symbol)
        d = json.loads(raw)
        timestamps = d["chart"]["result"][0]["timestamp"]
        closes = d["chart"]["result"][0]["indicators"]["quote"][0]["close"]
        new_closes = []
        for ts, close in zip(timestamps, closes, strict=True):
            dt = datetime.fromtimestamp(ts, UTC).date()
            if dt >= date(2026, 10, 1):
                new_closes.append(999999.0)
            else:
                new_closes.append(close)
        d["chart"]["result"][0]["indicators"]["quote"][0]["close"] = new_closes
        return json.dumps(d)

    scan_corrupted, _ = yahoo.build_scan(
        ["XLK", "XLF", "XLE"], "SPY", fetcher=corrupted_fetcher, asof_date=date(2026, 10, 1)
    )
    assert scan_corrupted.asof == scan_clean.asof
    assert len(scan_corrupted.rows) == len(scan_clean.rows)
    for row_corr, row_clean in zip(scan_corrupted.rows, scan_clean.rows, strict=True):
        assert row_corr.ticker == row_clean.ticker
        assert row_corr.asof == row_clean.asof
        assert row_corr.close == row_clean.close
        assert row_corr.ret20 == row_clean.ret20
        assert row_corr.rs20 == row_clean.rs20
        assert row_corr.pct52w == row_clean.pct52w
        assert row_corr.trend == row_clean.trend


def test_build_scan_trims_ticker_series_before_scoring(monkeypatch):
    run_date = date(2026, 10, 8)
    bench_completed = _dated(_flat(100.0, 252, last=104.0), end=date(2026, 10, 7))
    ticker_completed = _dated(_flat(100.0, 252, last=110.0), end=date(2026, 10, 7))
    bench_payload = _json_from_dated(bench_completed + (("2026-10-08", 99999.0),))
    ticker_payload = _json_from_dated(ticker_completed + (("2026-10-08", 88888.0),))

    received_series: dict[str, list[tuple[str, float]]] = {}
    orig_score = yahoo.score_row

    def spy_score_row(ticker, series, bench, *, asof_date):
        received_series[ticker] = list(series)
        return orig_score(ticker, series, bench, asof_date=asof_date)

    monkeypatch.setattr(yahoo, "score_row", spy_score_row)

    def fetch(symbol: str) -> str:
        return ticker_payload if symbol == "XLK" else bench_payload

    yahoo.build_scan(["XLK"], "SPY", fetcher=fetch, asof_date=run_date)

    assert "XLK" in received_series
    assert all(day < run_date.isoformat() for day, _ in received_series["XLK"])
    assert not any(day == "2026-10-08" for day, _ in received_series["XLK"])


def _closes_from_json(text: str) -> list[float]:
    d = json.loads(text)
    return d["chart"]["result"][0]["indicators"]["quote"][0]["close"]


def test_cli_yahoo_fills_rotation_and_52w_in_the_letter(db, tmp_path, monkeypatch, capsys):
    _forbid_network(monkeypatch)
    monkeypatch.setattr(yahoo, "fetch_json", _fixture_fetcher())
    _seed(db, VIEWS)

    rc = main(
        _cli(
            db,
            tmp_path,
            "--scan-source",
            "yahoo",
            "--scan-benchmark",
            "SPY",
        )
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "market leg on (yahoo benchmark SPY as of 2026-10-07)" in out
    assert "findings=2" in out

    out_file = tmp_path / "out" / "2026-10-08-midweek.md"
    assert out_file.is_file()
    text = out_file.read_text(encoding="utf-8")
    assert "rs20 (pp)" in text
    assert "52w track" in text

    spy = _closes_from_json((FIXTURES / "spy.json").read_text(encoding="utf-8"))[:-1]
    xlk = _closes_from_json((FIXTURES / "xlk.json").read_text(encoding="utf-8"))[:-1]
    xlf = _closes_from_json((FIXTURES / "xlf.json").read_text(encoding="utf-8"))[:-1]
    xle = _closes_from_json((FIXTURES / "xle.json").read_text(encoding="utf-8"))[:-1]

    xlk_rs20 = (xlk[-1] / xlk[-21] - 1.0) * 100.0 - (spy[-1] / spy[-21] - 1.0) * 100.0
    xlf_pct = (xlf[-1] - min(xlf[-252:])) / (max(xlf[-252:]) - min(xlf[-252:])) * 100.0
    xle_pct = (xle[-1] - min(xle[-252:])) / (max(xle[-252:]) - min(xle[-252:])) * 100.0

    xlk_line = next(line for line in text.splitlines() if line.startswith("XLK"))
    xlf_line = next(line for line in text.splitlines() if line.startswith("XLF"))
    xle_line = next(line for line in text.splitlines() if line.startswith("XLE"))
    assert f"{xlk_rs20:+.2f}" in xlk_line
    assert f"{xlf_pct:5.0f}" in xlf_line
    assert f"{xle_pct:5.0f}" in xle_line
    assert "**XLK confirms**" in text
    assert "**XLF confirms**" in text
    assert "**XLE extends**" not in text
    assert "SPY" not in xlk_line
    assert "2026-10-07" in text


def test_cli_no_scan_source_market_leg_stays_off(db, tmp_path, capsys):
    _seed(db, VIEWS)
    rc = main(_cli(db, tmp_path))
    assert rc == 0
    out = capsys.readouterr().out
    assert "market leg off: no scan source configured" in out
    out_file = tmp_path / "out" / "2026-10-08-midweek.md"
    assert not out_file.exists()


def test_cli_scan_file_and_yahoo_are_mutually_exclusive(db, tmp_path, capsys):
    rc = main(
        _cli(
            db,
            tmp_path,
            "--scan-file",
            str(tmp_path / "scan.json"),
            "--scan-source",
            "yahoo",
            "--scan-benchmark",
            "SPY",
        )
    )
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err


def test_cli_yahoo_needs_a_benchmark(db, tmp_path, capsys):
    rc = main(_cli(db, tmp_path, "--scan-source", "yahoo"))
    assert rc == 1
    assert "--scan-benchmark" in capsys.readouterr().err


def test_cli_benchmark_flag_requires_yahoo(db, tmp_path, capsys):
    rc = main(_cli(db, tmp_path, "--scan-benchmark", "SPY"))
    assert rc == 1
    assert "only valid with --scan-source yahoo" in capsys.readouterr().err


def test_cli_yahoo_may_replace_a_scan_backed_letter(db, tmp_path, monkeypatch, capsys):
    _forbid_network(monkeypatch)
    monkeypatch.setattr(yahoo, "fetch_json", _fixture_fetcher())
    _seed(db, VIEWS)
    first = main(_cli(db, tmp_path, "--scan-source", "yahoo", "--scan-benchmark", "SPY"))
    assert first == 0
    capsys.readouterr()
    second = main(_cli(db, tmp_path, "--scan-source", "yahoo", "--scan-benchmark", "SPY"))
    assert second == 0


@pytest.mark.parametrize(
    ("symbol_json", "match"),
    [
        ("http", "HTTP 503"),
        ("empty", "empty json"),
        ("html", "HTML response"),
        ("flat", "constant closes"),
        ("bad_symbol", "not a Yahoo symbol"),
    ],
)
def test_cli_omits_a_bad_ticker_instead_of_defaulting(
    db, tmp_path, monkeypatch, capsys, symbol_json, match
):
    _forbid_network(monkeypatch)
    fixtures = _fixture_fetcher()
    payloads = {
        "empty": "   ",
        "html": "<html><body>blocked</body></html>",
        "flat": _json(_flat(100.0, 260)),
    }

    def fetch(symbol: str) -> str:
        if symbol == "QQQ":
            if symbol_json == "http":
                raise yahoo.YahooError("HTTP 503")
            return payloads[symbol_json]
        return fixtures(symbol)

    monkeypatch.setattr(yahoo, "fetch_json", fetch)
    body = VIEWS + (
        "\n### Bad ticker\n"
        "Id: bad-ticker\n"
        "Weight: standing\n"
        "Watching: QQQ\n"
        "Claim: A bad ticker is omitted, not filled with a default.\n"
        "Confirms when: QQQ pct52w >= 0\n"
        if symbol_json != "bad_symbol"
        else "\n### Bad ticker\n"
        "Id: bad-ticker\n"
        "Weight: standing\n"
        "Watching: QQQ/../bad\n"
        "Claim: A bad symbol is rejected.\n"
        "Confirms when: QQQ/../bad pct52w >= 0\n"
    )
    _seed(db, body)
    rc = main(_cli(db, tmp_path, "--scan-source", "yahoo", "--scan-benchmark", "SPY"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "omitted" in out
    assert match in out


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("http", "HTTP 503"),
        ("flat", "constant closes"),
        ("html", "HTML response"),
        ("stale", "stale history"),
    ],
)
def test_cli_refuses_the_scan_when_the_benchmark_is_unusable(
    db, tmp_path, monkeypatch, capsys, kind, match
):
    _forbid_network(monkeypatch)
    payloads = {
        "flat": _json(_flat(100.0, 260)),
        "html": "<html><body>check</body></html>",
        "stale": _json(_flat(100.0, 260, last=104.0), end=date(2026, 9, 20)),
    }

    def fetch(symbol: str) -> str:
        if symbol == "SPY":
            if kind == "http":
                raise yahoo.YahooError("HTTP 503")
            return payloads[kind]
        return (FIXTURES / f"{symbol.lower()}.json").read_text(encoding="utf-8")

    monkeypatch.setattr(yahoo, "fetch_json", fetch)
    _seed(db, VIEWS)
    rc = main(_cli(db, tmp_path, "--scan-source", "yahoo", "--scan-benchmark", "SPY"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing scan: benchmark SPY" in err
    assert match in err
    assert not (tmp_path / "out" / "2026-10-08-midweek.md").exists()
