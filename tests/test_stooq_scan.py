"""Stooq market leg: scoring is pure, fetches are injected, refusals omit the row.

Fixtures under ``tests/fixtures/stooq/`` are synthetic Stooq CSVs. No test
calls the network; ``urlopen`` is patched to fail if production reaches it.
"""

from __future__ import annotations

import urllib.error
from datetime import date, timedelta
from pathlib import Path

import pytest

from hub import scan_score, stooq, store
from hub.cli import main

FIXTURES = Path(__file__).parent / "fixtures" / "stooq"
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
Confirms when: XLF pct52w >= 90
Contradicts when: XLF pct52w <= 0

### Energy trend
Id: energy-trend
Weight: watch
Watching: XLE
Claim: Energy is being watched for a trend break.
Confirms when: XLE rs20 >= 1000
Contradicts when: XLE rs20 <= -1000
"""


def _dated(closes: list[float], *, start: date = date(2020, 1, 1)) -> tuple[tuple[str, float], ...]:
    return tuple(
        ((start + timedelta(days=index)).isoformat(), close) for index, close in enumerate(closes)
    )


def _flat(level: float, n: int = 252, last: float | None = None) -> list[float]:
    closes = [level] * n
    if last is not None:
        closes[-1] = last
    return closes


def _bench(last: float = 104.0) -> tuple[tuple[str, float], ...]:
    return _dated(_flat(100.0, last=last))


def _forbid_network(monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("test hit the network")

    monkeypatch.setattr(stooq.urllib.request, "urlopen", boom)


def _fixture_fetcher(extra: dict[str, str] | None = None):
    files = {
        path.name.removesuffix(".csv"): path.read_text(encoding="utf-8")
        for path in FIXTURES.glob("*.csv")
    }
    if extra:
        files.update(extra)

    def fetch(symbol: str) -> str:
        try:
            return files[symbol]
        except KeyError:
            raise stooq.StooqError("no data") from None

    return fetch


def _seed(db: Path, body: str) -> None:
    conn = store.connect(db)
    store.insert_document_revision(
        conn,
        slug="public-etf-views",
        kind="thesis",
        body=body,
        source_kind="session",
        source_ref="fixture",
    )
    conn.commit()
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
        "--mv-analyst-root",
        str(tmp_path / "no-mv"),
        "--memory-path",
        str(tmp_path / "memory.jsonl"),
        *extra,
    ]


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "hub.db"
    assert main(["db", "init", "--db", str(path)]) == 0
    return path


# ---------------------------------------------------------------------------
# symbols and the daily URL
# ---------------------------------------------------------------------------


def test_symbol_for_maps_a_bare_ticker_to_the_us_listing():
    assert stooq.symbol_for("SPY") == "spy.us"
    assert stooq.symbol_for("spy.us") == "spy.us"
    assert stooq.symbol_for("^SPX") == "^spx"


def test_symbol_for_rejects_a_ticker_that_is_not_a_symbol():
    with pytest.raises(stooq.StooqError, match="not a Stooq symbol"):
        stooq.symbol_for("SPY/../secret")


def test_daily_history_url_is_the_keyless_csv_endpoint():
    assert stooq.daily_history_url("spy.us") == "https://stooq.com/q/d/l/?s=spy.us&i=d"


def test_fetch_csv_requests_that_url_and_returns_the_body(monkeypatch):
    captured = {}

    class _Resp:
        status = 200

        def read(self, _n=-1):
            return b"Date,Close\n2026-10-07,1.5\n"

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_urlopen(request, timeout=0):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(stooq.urllib.request, "urlopen", fake_urlopen)
    assert "2026-10-07" in stooq.fetch_csv("spy.us")
    assert captured["url"] == "https://stooq.com/q/d/l/?s=spy.us&i=d"
    assert captured["timeout"] == stooq.TIMEOUT_SECONDS


def test_fetch_csv_refuses_an_http_error(monkeypatch):
    def fake_urlopen(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 503, "unavailable", hdrs=None, fp=None)

    monkeypatch.setattr(stooq.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(stooq.StooqError, match="HTTP 503"):
        stooq.fetch_csv("spy.us")


def test_fetch_csv_refuses_a_transport_error(monkeypatch):
    def fake_urlopen(_request, timeout=0):
        raise urllib.error.URLError("timed out")

    monkeypatch.setattr(stooq.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(stooq.StooqError, match="HTTP error"):
        stooq.fetch_csv("spy.us")


def test_fetch_csv_refuses_a_truncated_body(monkeypatch):
    class _Resp:
        status = 200

        def read(self, _n=-1):
            return b"x" * 32

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    monkeypatch.setattr(stooq.urllib.request, "urlopen", lambda *_a, **_k: _Resp())
    with pytest.raises(stooq.StooqError, match="too large"):
        stooq.fetch_csv("spy.us", max_bytes=8)


# ---------------------------------------------------------------------------
# CSV parsing refusals
# ---------------------------------------------------------------------------


def test_parse_reads_close_and_sorts_newest_first():
    text = "Date,Open,High,Low,Close,Volume\n2026-10-07,2,2,2,2.5,1\n2026-10-06,1,1,1,1.5,1\n"
    assert stooq.parse_csv(text) == (("2026-10-06", 1.5), ("2026-10-07", 2.5))


def test_parse_refuses_empty_csv():
    with pytest.raises(stooq.StooqError, match="empty csv"):
        stooq.parse_csv("   ")
    with pytest.raises(stooq.StooqError, match="empty csv"):
        stooq.parse_csv("Date,Open,High,Low,Close,Volume\n")


def test_parse_refuses_no_data():
    with pytest.raises(stooq.StooqError, match="no data"):
        stooq.parse_csv("No data\n")
    with pytest.raises(stooq.StooqError, match="no data"):
        stooq.parse_csv("Date,Open,High,Low,Close,Volume\nNo data\n")
    with pytest.raises(stooq.StooqError, match="no data"):
        stooq.parse_csv("Exceeded the daily hits limit\n")


def test_parse_refuses_malformed_csv():
    with pytest.raises(stooq.StooqError, match="malformed csv"):
        stooq.parse_csv("this is not a csv\n")
    with pytest.raises(stooq.StooqError, match="malformed csv"):
        stooq.parse_csv("<html>browser check</html>")
    with pytest.raises(stooq.StooqError, match="malformed csv"):
        stooq.parse_csv("Date,Open\n2026-10-07,1\n")


def test_parse_refuses_a_non_numeric_close():
    text = "Date,Open,High,Low,Close,Volume\n2026-10-07,1,1,1,N/A,1\n"
    with pytest.raises(stooq.StooqError, match="non-numeric close"):
        stooq.parse_csv(text)
    text = "Date,Close\n2026-10-07,nan\n"
    with pytest.raises(stooq.StooqError, match="non-numeric close"):
        stooq.parse_csv(text)


def test_parse_refuses_a_duplicate_session():
    text = "Date,Close\n2026-10-07,1\n2026-10-07,2\n"
    with pytest.raises(stooq.StooqError, match="duplicate session"):
        stooq.parse_csv(text)


# ---------------------------------------------------------------------------
# scoring formulas
# ---------------------------------------------------------------------------


def test_pct52w_is_position_in_the_trailing_range():
    closes = [100.0] * 252
    closes[0] = 80.0
    closes[1] = 120.0
    row = scan_score.score_row("XLK", _dated(closes), _bench())
    assert row.pct52w == pytest.approx(50.0)
    assert row.close == 100.0


def test_pct52w_uses_trailing_252_not_the_whole_file():
    closes = [100.0] * 260
    closes[0] = 1.0  # outside the 252-session window
    closes[8] = 50.0  # inside the window, outside the last 200
    closes[-1] = 80.0
    row = scan_score.score_row("XLK", _dated(closes), _dated(_flat(100.0, 260, last=104.0)))
    assert row.pct52w == pytest.approx(60.0)


def test_rs20_is_excess_return_in_percentage_points():
    ticker = _flat(100.0, last=110.0)  # +10% over 20 sessions
    bench = _flat(100.0, last=104.0)  # +4%
    row = scan_score.score_row("XLK", _dated(ticker), _dated(bench))
    assert row.ret20 == pytest.approx(10.0)
    assert row.rs20 == pytest.approx(6.0)


def test_rs60_is_excess_60_session_return():
    ticker = _flat(100.0, last=110.0)
    bench = _flat(100.0, last=104.0)
    row = scan_score.score_row("XLK", _dated(ticker), _dated(bench))
    assert row.ret60 == pytest.approx(10.0)
    assert row.rs60 == pytest.approx(6.0)


def test_sma200_is_mean_of_last_200_closes():
    closes = _flat(100.0, last=110.0)
    row = scan_score.score_row("XLK", _dated(closes), _bench())
    assert row.sma200 == pytest.approx((199 * 100 + 110) / 200)


def test_trend_above_below_and_level():
    above = scan_score.score_row("XLK", _dated(_flat(100.0, last=110.0)), _bench())
    below = scan_score.score_row("XLK", _dated(_flat(100.0, last=90.0)), _bench())
    level = [90.0] + [100.0] * 251
    flat = scan_score.score_row("XLK", _dated(level), _bench())
    assert above.trend == "above"
    assert below.trend == "below"
    assert flat.trend == "—"
    assert flat.sma200 == pytest.approx(100.0)


def test_crossed_up_from_on_the_average():
    row = scan_score.score_row("XLK", _dated(_flat(100.0, last=110.0)), _bench())
    assert row.crossed == "up"


def test_crossed_down_from_on_the_average():
    row = scan_score.score_row("XLE", _dated(_flat(100.0, last=90.0)), _bench())
    assert row.crossed == "down"


def test_no_cross_when_price_stays_above_the_average():
    closes = [90.0] + [110.0] * 249 + [120.0, 120.0]
    row = scan_score.score_row("XLK", _dated(closes), _bench())
    assert row.trend == "above"
    assert row.crossed is None


def test_score_uses_the_benchmark_date_not_a_later_ticker_close():
    bench = _dated(_flat(100.0, last=104.0))
    later = list(bench) + [("2021-12-31", 999.0)]
    ticker = _dated(_flat(100.0, last=110.0))
    # Same sessions as the benchmark, plus a later print that must not be today's close.
    series = ticker + (("2021-12-31", 999.0),)
    assert later[-1][1] == 999.0
    row = scan_score.score_row("XLK", series, bench)
    assert row.asof == bench[-1][0]
    assert row.close == 110.0


def test_score_refuses_a_ticker_with_no_session_on_the_benchmark_date():
    bench = _dated(_flat(100.0, last=104.0))
    short_end = _dated(_flat(100.0, 252, last=110.0), start=date(2019, 1, 1))
    with pytest.raises(scan_score.ScoreError, match="no session on"):
        scan_score.score_row("XLK", short_end, bench)


# ---------------------------------------------------------------------------
# refusals: no stand-in number
# ---------------------------------------------------------------------------


def test_score_refuses_fewer_than_252_sessions():
    bench = _bench()
    end = date.fromisoformat(bench[-1][0])
    series = _dated(_flat(100.0, 251, last=110.0), start=end - timedelta(days=250))
    assert series[-1][0] == bench[-1][0]
    with pytest.raises(scan_score.ScoreError, match="fewer than 252 sessions"):
        scan_score.score_row("XLK", series, bench)


def test_score_accepts_exactly_252_sessions():
    row = scan_score.score_row("XLK", _dated(_flat(100.0, 252, last=110.0)), _bench())
    assert row.pct52w == pytest.approx(100.0)


def test_score_refuses_constant_closes():
    with pytest.raises(scan_score.ScoreError, match="constant closes"):
        scan_score.score_row("XLK", _dated(_flat(100.0, 252)), _bench())


def test_score_refuses_a_constant_window_even_if_older_closes_move():
    closes = [50.0] * 8 + [100.0] * 252
    with pytest.raises(scan_score.ScoreError, match="constant closes"):
        scan_score.score_row("XLK", _dated(closes), _dated(_flat(100.0, 260, last=104.0)))


def test_score_refuses_a_non_positive_close():
    closes = _flat(100.0, last=110.0)
    closes[10] = 0.0
    with pytest.raises(scan_score.ScoreError, match="non-positive close"):
        scan_score.score_row("XLK", _dated(closes), _bench())


def test_require_benchmark_refuses_a_stubbed_series():
    with pytest.raises(scan_score.ScoreError, match="constant closes"):
        scan_score.require_benchmark(_dated(_flat(100.0, 252)))
    with pytest.raises(scan_score.ScoreError, match="fewer than 252"):
        scan_score.require_benchmark(_dated(_flat(100.0, 10, last=101.0)))


def _csv(closes: list[float], *, end: date = date(2026, 10, 7)) -> str:
    lines = ["Date,Open,High,Low,Close,Volume"]
    start = end - timedelta(days=len(closes) - 1)
    for index, close in enumerate(closes):
        day = (start + timedelta(days=index)).isoformat()
        lines.append(f"{day},1,1,1,{close},1")
    return "\n".join(lines) + "\n"


def test_build_scan_omits_a_bad_ticker_and_keeps_a_good_one():
    good = _csv(_flat(100.0, 252, last=110.0))
    short = _csv(_flat(100.0, 10, last=110.0))
    bench = _csv(_flat(100.0, 252, last=104.0))

    def fetch(symbol: str) -> str:
        return {"spy.us": bench, "xlk.us": good, "qqq.us": short}[symbol]

    scan, notes = stooq.build_scan(["XLK", "QQQ"], "SPY", fetcher=fetch)
    assert [row.ticker for row in scan.rows] == ["XLK"]
    assert scan.rows[0].rs20 == pytest.approx(6.0)
    assert any("QQQ" in note and "fewer than 252" in note for note in notes)
    assert all(row.pct52w is not None for row in scan.rows)


def test_build_scan_refuses_a_stubbed_benchmark():
    constant = _csv(_flat(100.0, 260))

    def fetch(symbol: str) -> str:
        return constant

    with pytest.raises(stooq.StooqError, match="refusing scan: benchmark SPY: constant closes"):
        stooq.build_scan(["XLK"], "SPY", fetcher=fetch)


def test_build_scan_refuses_when_the_benchmark_fetch_fails():
    def fetch(symbol: str) -> str:
        raise stooq.StooqError("HTTP 503")

    with pytest.raises(stooq.StooqError, match="refusing scan: benchmark SPY: HTTP 503"):
        stooq.build_scan(["XLK"], "SPY", fetcher=fetch)


def test_build_scan_does_not_emit_the_benchmark_as_a_row():
    text = _csv(_flat(100.0, 252, last=110.0))

    def fetch(_symbol: str) -> str:
        return text

    scan, notes = stooq.build_scan(["SPY", "XLK"], "SPY", fetcher=fetch)
    assert notes == ()
    assert [row.ticker for row in scan.rows] == ["XLK"]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _closes_from_csv(text: str) -> list[float]:
    closes = []
    for line in text.splitlines()[1:]:
        if not line.strip():
            continue
        closes.append(float(line.split(",")[4]))
    return closes


def test_cli_stooq_fills_rotation_and_52w_in_the_letter(db, tmp_path, monkeypatch, capsys):
    _forbid_network(monkeypatch)
    monkeypatch.setattr(stooq, "fetch_csv", _fixture_fetcher())
    _seed(db, VIEWS)
    rc = main(_cli(db, tmp_path, "--scan-source", "stooq", "--scan-benchmark", "SPY"))
    assert rc == 0
    captured = capsys.readouterr()
    assert "market leg on (stooq benchmark SPY as of 2026-10-07)" in captured.out
    assert "market leg off" not in captured.out
    letter_path = tmp_path / "out" / "2026-10-08-midweek.md"
    text = letter_path.read_text(encoding="utf-8")
    assert "rs20 (pp)" in text
    assert "52w track" in text
    spy = _closes_from_csv((FIXTURES / "spy.us.csv").read_text(encoding="utf-8"))
    xlk = _closes_from_csv((FIXTURES / "xlk.us.csv").read_text(encoding="utf-8"))
    xlf = _closes_from_csv((FIXTURES / "xlf.us.csv").read_text(encoding="utf-8"))
    xle = _closes_from_csv((FIXTURES / "xle.us.csv").read_text(encoding="utf-8"))
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
    assert "rs20" in text.split("**XLK confirms**", 1)[1].split("\n", 1)[0]
    assert "**XLF confirms**" in text
    assert "pct52w" in text.split("**XLF confirms**", 1)[1].split("\n", 1)[0]
    assert "crossed below its 200-day trend" in text
    assert "SPY" not in xlk_line


def test_cli_no_scan_source_market_leg_stays_off(db, tmp_path, capsys):
    _seed(db, VIEWS)
    rc = main(_cli(db, tmp_path))
    assert rc == 0
    captured = capsys.readouterr()
    assert "market leg off: no scan source configured" in captured.out
    assert "stooq" not in captured.out
    letter_path = tmp_path / "out" / "2026-10-08-midweek.md"
    # Corpus is empty and no scan ran, so the week is quiet and no file is written.
    assert not letter_path.exists()
    assert "nothing cleared the merit gate" in captured.out


def test_cli_scan_file_and_stooq_are_mutually_exclusive(db, tmp_path, capsys):
    rc = main(
        _cli(
            db,
            tmp_path,
            "--scan-file",
            str(tmp_path / "scan.json"),
            "--scan-source",
            "stooq",
            "--scan-benchmark",
            "SPY",
        )
    )
    assert rc == 1
    assert "mutually exclusive" in capsys.readouterr().err
    assert not (tmp_path / "out" / "2026-10-08-midweek.delivering").exists()


def test_cli_stooq_needs_a_benchmark(db, tmp_path, capsys):
    rc = main(_cli(db, tmp_path, "--scan-source", "stooq"))
    assert rc == 1
    assert "--scan-benchmark" in capsys.readouterr().err


def test_cli_benchmark_flag_requires_stooq(db, tmp_path, capsys):
    rc = main(_cli(db, tmp_path, "--scan-benchmark", "SPY"))
    assert rc == 1
    assert "only valid with --scan-source stooq" in capsys.readouterr().err


def test_cli_stooq_may_replace_a_scan_backed_letter(db, tmp_path, monkeypatch, capsys):
    _forbid_network(monkeypatch)
    monkeypatch.setattr(stooq, "fetch_csv", _fixture_fetcher())
    _seed(db, VIEWS)
    scan_file = tmp_path / "scan.json"
    scan_file.write_text(
        '{"benchmark": "BAA", "asof": "2026-10-07", "rows": '
        '[{"ticker": "XLK", "close": 1, "rs20": 5, "pct52w": 10}]}',
        encoding="utf-8",
    )
    assert main(_cli(db, tmp_path, "--scan-file", str(scan_file))) == 0
    capsys.readouterr()
    rc = main(_cli(db, tmp_path, "--scan-source", "stooq", "--scan-benchmark", "SPY"))
    assert rc == 0
    text = (tmp_path / "out" / "2026-10-08-midweek.md").read_text(encoding="utf-8")
    assert "crossed below its 200-day trend" in text
    assert "market leg on (stooq benchmark SPY" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("symbol_csv", "match"),
    [
        ("short", "fewer than 252"),
        ("empty", "empty csv"),
        ("nodata", "no data"),
        ("malformed", "malformed csv"),
        ("http", "HTTP 503"),
        ("nan", "non-numeric close"),
        ("constant", "constant closes"),
    ],
)
def test_cli_omits_a_bad_ticker_instead_of_defaulting(
    db, tmp_path, monkeypatch, capsys, symbol_csv, match
):
    _forbid_network(monkeypatch)
    payloads = {
        "short": _csv(_flat(100.0, 12, last=50.0)),
        "empty": "",
        "nodata": "Date,Open,High,Low,Close,Volume\nNo data\n",
        "malformed": "not,a,csv\n",
        "http": None,
        "nan": "Date,Close\n2026-10-07,N/A\n",
        "constant": _csv(_flat(40.0, 260)),
    }

    def fetch(symbol: str) -> str:
        if symbol == "qqq.us":
            if symbol_csv == "http":
                raise stooq.StooqError("HTTP 503")
            return payloads[symbol_csv]
        return (FIXTURES / f"{symbol}.csv").read_text(encoding="utf-8")

    monkeypatch.setattr(stooq, "fetch_csv", fetch)
    body = VIEWS + (
        "\n### Would clear on any invented percentile\n"
        "Id: bad-ticker\n"
        "Weight: standing\n"
        "Watching: QQQ\n"
        "Claim: This view must not clear from a stand-in number.\n"
        "Confirms when: QQQ pct52w <= 100\n"
        "Contradicts when: QQQ pct52w >= 0\n"
    )
    _seed(db, body)
    rc = main(_cli(db, tmp_path, "--scan-source", "stooq", "--scan-benchmark", "SPY"))
    assert rc == 0
    captured = capsys.readouterr()
    assert f"omitted QQQ: {match}" in captured.out
    text = (tmp_path / "out" / "2026-10-08-midweek.md").read_text(encoding="utf-8")
    assert not any(line.startswith("QQQ") for line in text.splitlines())
    assert "**QQQ " not in text
    assert "**XLK confirms**" in text


@pytest.mark.parametrize(
    ("kind", "match"),
    [
        ("short", "fewer than 252"),
        ("empty", "empty csv"),
        ("nodata", "no data"),
        ("malformed", "malformed csv"),
        ("http", "HTTP 503"),
        ("nan", "non-numeric close"),
        ("constant", "constant closes"),
    ],
)
def test_cli_refuses_the_scan_when_the_benchmark_is_unusable(
    db, tmp_path, monkeypatch, capsys, kind, match
):
    _forbid_network(monkeypatch)
    payloads = {
        "short": _csv(_flat(100.0, 12, last=50.0)),
        "empty": "",
        "nodata": "No data\n",
        "malformed": "<html>no csv</html>",
        "nan": "Date,Close\n2026-10-07,abc\n",
        "constant": _csv(_flat(100.0, 260)),
    }

    def fetch(symbol: str) -> str:
        if symbol == "spy.us":
            if kind == "http":
                raise stooq.StooqError("HTTP 503")
            return payloads[kind]
        return (FIXTURES / f"{symbol}.csv").read_text(encoding="utf-8")

    monkeypatch.setattr(stooq, "fetch_csv", fetch)
    _seed(db, VIEWS)
    rc = main(_cli(db, tmp_path, "--scan-source", "stooq", "--scan-benchmark", "SPY"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "refusing scan: benchmark SPY" in err
    assert match in err
    assert not (tmp_path / "out" / "2026-10-08-midweek.md").exists()
    assert not (tmp_path / "out" / "2026-10-08-midweek.delivering").exists()
