"""Direct IC writes: name resolution, dry-run, the ic_writes log, receipts, --yes gating and
revert. IC is a stateful in-memory fake behind the ``hub.ic`` HTTP seam (``_build_opener``);
every name, id, symbol and number here is synthetic."""

import io
import json
import re
import sqlite3
import urllib.error

import pytest

from hub import ic, ic_writes, store
from hub.cli import main

TOKEN = "ict_SYNTHETIC0test0token0value0000"
EVENT_ID = "11111111-2222-3333-4444-555555555555"


class _Resp:
    def __init__(self, payload=None, status=200):
        self.status = status
        self._raw = b"" if payload is None else json.dumps(payload).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeIc:
    """Just enough of IC's advisor-write surface, with state, for the verbs under test."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.alerts = [
            self._alert(1, "AAA below 10", "below", 10, "AAA"),
            self._alert(2, "AAA above 20", "above", 20, "AAA"),
            self._alert(3, "BBB below 5", "below", 5, "BBB"),
        ]
        self.watchlists = [
            {"id": 1, "name": "Theme One", "items": [self._item(11, 1, "AAA", "old thesis")]},
            {
                "id": 2,
                "name": "Theme Two",
                "items": [self._item(21, 2, "AAA"), self._item(22, 2, "BBB")],
            },
        ]
        self.accounts = [{"id": 7, "name": "Roth Test"}, {"id": 8, "name": "Taxable Test"}]
        self.triggers: list[dict] = []
        self.receipts: list[dict] = []
        self.fail_receipts = False
        self.next_id = 100

    @staticmethod
    def _alert(id_, name, cond, threshold, symbol):
        return {
            "id": id_,
            "name": name,
            "condition_type": cond,
            "threshold_value": f"{threshold}.00",
            "cooldown_minutes": 60,
            "comparison_period": None,
            "confirm_checks": None,
            "notes": None,
            "is_active": True,
            "watchlist_item_id": None,
            "target": {"type": "equity", "id": 5, "symbol": symbol, "name": symbol},
        }

    @staticmethod
    def _trigger(id_, name, rule, action, status="active"):
        # TriggerResponse's real shape: no pack-only fields, alert links as summaries
        return {
            "id": id_,
            "name": name,
            "rule": rule,
            "action": action,
            "tier": None,
            "display_order": 0,
            "status": status,
            "signal": "armed",
            "executed_at": None,
            "execution_note": None,
            "alerts": [],
            "created_at": "2026-10-01T00:00:00Z",
        }

    def _links(self, ids):
        return [
            {"id": a["id"], "name": a["name"], "is_active": a["is_active"]}
            for a in self.alerts
            if a["id"] in ids
        ]

    @staticmethod
    def _item(id_, wid, symbol, thesis=None):
        return {
            "id": id_,
            "watchlist_id": wid,
            "thesis": thesis,
            "notes": None,
            "target_price": None,
            "entry_zones": [],
            "catalyst_tags": [],
            "equity": {"id": id_, "symbol": symbol, "name": symbol},
        }

    # --- HTTP seam ---
    def open(self, request, timeout=None):
        method = request.get_method()
        path = request.full_url.removeprefix("http://ic.invalid:8000")
        body = json.loads(request.data) if request.data else None
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        self.calls.append((method, path, body))
        return self._route(method, path, body)

    def _err(self, code, detail):
        fp = io.BytesIO(json.dumps({"detail": detail}).encode())
        raise urllib.error.HTTPError("http://ic.invalid", code, "err", {}, fp)

    def _route(self, method, path, body):  # noqa: C901 - a flat router reads best
        api = "/api/v1"
        if path == f"{api}/export/handoff-receipts":
            if self.fail_receipts:
                self._err(500, "receipt store down")
            self.receipts.append(body)
            return _Resp({"data": {"id": 900 + len(self.receipts)}}, 201)
        if path == f"{api}/alerts" and method == "GET":
            return _Resp({"data": self.alerts})
        if path == f"{api}/alerts" and method == "POST":
            alert = self._alert(
                self._new(), body["name"], body["condition_type"], 0, body["equity_symbol"]
            )
            alert.update({k: v for k, v in body.items() if k not in ("equity_symbol",)})
            self.alerts.append(alert)
            return _Resp({"data": alert}, 201)
        if m := re.fullmatch(rf"{api}/alerts/(\d+)", path):
            alert = next((a for a in self.alerts if a["id"] == int(m.group(1))), None)
            if alert is None:
                self._err(404, "not found")
            if method == "PUT":
                alert.update(body)
                return _Resp({"data": alert})
            self.alerts.remove(alert)
            return _Resp(None, 204)
        if path == f"{api}/watchlists" and method == "GET":
            return _Resp({"data": [{"id": w["id"], "name": w["name"]} for w in self.watchlists]})
        if m := re.fullmatch(rf"{api}/watchlists/(\d+)", path):
            return _Resp({"data": next(w for w in self.watchlists if w["id"] == int(m.group(1)))})
        if m := re.fullmatch(rf"{api}/watchlists/(\d+)/items/(\d+)", path):
            wl = next(w for w in self.watchlists if w["id"] == int(m.group(1)))
            item = next(i for i in wl["items"] if i["id"] == int(m.group(2)))
            item.update(body)
            return _Resp({"data": item})
        if m := re.fullmatch(rf"{api}/watchlists/(\d+)/items", path):
            wl = next(w for w in self.watchlists if w["id"] == int(m.group(1)))
            item = self._item(self._new(), wl["id"], body["symbol"])
            item.update({k: v for k, v in body.items() if k != "symbol"})
            wl["items"].append(item)
            return _Resp({"data": item}, 201)
        if path == f"{api}/accounts":
            return _Resp({"data": self.accounts})
        if path == f"{api}/trades" and method == "POST":
            return _Resp({"data": {"id": self._new(), **body}}, 201)
        if path == f"{api}/events" and method == "POST":
            return _Resp({"data": {"id": EVENT_ID, **body}}, 201)
        if path == f"{api}/events/{EVENT_ID}":
            if method == "PUT":
                return _Resp({"data": {"id": EVENT_ID, **body}})
            return _Resp(None, 204)
        if path == f"{api}/triggers" and method == "GET":
            return _Resp({"data": self.triggers})
        if path == f"{api}/triggers" and method == "POST":
            row = self._trigger(self._new(), body["name"], body["rule"], body["action"])
            row.update({k: v for k, v in body.items() if k != "alert_ids"})
            row["alerts"] = self._links(body.get("alert_ids", []))
            self.triggers.append(row)
            return _Resp({"data": row}, 201)
        if m := re.fullmatch(rf"{api}/triggers/(\d+)(/retire)?", path):
            row = next((t for t in self.triggers if t["id"] == int(m.group(1))), None)
            if row is None:
                self._err(404, "not found")
            if method == "PUT":
                row.update({k: v for k, v in body.items() if k != "alert_ids"})
                if "alert_ids" in body:
                    row["alerts"] = self._links(body["alert_ids"])
            elif m.group(2):
                row["status"] = "retired"
            return _Resp({"data": row})
        if path in (f"{api}/lessons", f"{api}/ratios", f"{api}/watchlists") and method == "POST":
            return _Resp({"data": {"id": self._new(), **body}}, 201)
        self._err(403, "API tokens cannot access this endpoint")

    def _new(self):
        self.next_id += 1
        return self.next_id

    def writes(self):
        return [c for c in self.calls if c[0] != "GET" and "handoff-receipts" not in c[1]]


@pytest.fixture
def fake(monkeypatch):
    server = FakeIc()
    monkeypatch.setattr(ic, "_build_opener", lambda: server)
    return server


@pytest.fixture
def env(tmp_path, monkeypatch, fake):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ic.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ic.BASE_URL_ENV, "http://ic.invalid:8000")
    conn = store.connect(tmp_path / "data" / "hub.db")
    store.migrate(conn)
    conn.close()
    return tmp_path


def _rows(env):
    conn = sqlite3.connect(env / "data" / "hub.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM ic_writes ORDER BY id").fetchall()
    finally:
        conn.close()


def _hub(capsys, *argv):
    code = main(["ic", *argv])
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err
    return code, captured.out, captured.err


# --- name resolution ------------------------------------------------------------------------


def test_resolve_name_exact_beats_prefix():
    rows = [{"id": 1, "name": "AAA"}, {"id": 2, "name": "AAA long"}]
    assert ic.resolve_name(rows, "aaa", "alert")["id"] == 1


def test_resolve_name_unique_prefix():
    rows = [{"id": 1, "name": "Uranium watch"}, {"id": 2, "name": "Copper"}]
    assert ic.resolve_name(rows, "Uran", "alert")["id"] == 1


def test_resolve_name_zero_matches_lists_known():
    rows = [{"id": 1, "name": "Uranium"}, {"id": 2, "name": "Copper"}]
    with pytest.raises(ic.ResolutionError) as exc:
        ic.resolve_name(rows, "Gold", "alert")
    assert "no alert matches 'Gold'" in str(exc.value)
    assert "'Uranium'" in str(exc.value) and "'Copper'" in str(exc.value)


def test_resolve_name_many_matches_lists_candidates():
    rows = [{"id": 1, "name": "AAA below"}, {"id": 2, "name": "AAA above"}]
    with pytest.raises(ic.ResolutionError) as exc:
        ic.resolve_name(rows, "AAA", "alert")
    message = str(exc.value)
    assert "2 alerts match 'AAA'" in message
    assert "'AAA below' (id 1)" in message and "'AAA above' (id 2)" in message


def test_find_item_by_symbol_and_watchlist(env, fake):
    client = ic.IcClient.from_env()
    watchlist, item = client.find_item("bbb")
    assert (watchlist["name"], item["id"]) == ("Theme Two", 22)
    watchlist, item = client.find_item("AAA", "Theme O")
    assert (watchlist["name"], item["id"]) == ("Theme One", 11)


def test_find_item_on_two_watchlists_is_ambiguous(env, fake):
    with pytest.raises(ic.ResolutionError) as exc:
        ic.IcClient.from_env().find_item("AAA")
    assert "AAA (Theme One), AAA (Theme Two)" in str(exc.value)
    assert "--watchlist" in str(exc.value)


def test_find_item_missing(env, fake):
    with pytest.raises(ic.ResolutionError, match="no watchlist item for ZZZ"):
        ic.IcClient.from_env().find_item("ZZZ")
    with pytest.raises(ic.ResolutionError, match="no watchlist item for BBB on watchlist"):
        ic.IcClient.from_env().find_item("BBB", "Theme One")


def test_find_account_by_name(env, fake):
    assert ic.IcClient.from_env().find_account("roth")["id"] == 7
    with pytest.raises(ic.ResolutionError, match="known accounts"):
        ic.IcClient.from_env().find_account("Nope")


def test_cli_surfaces_ambiguous_alert_name(env, fake, capsys):
    code, _, err = _hub(capsys, "alert", "modify", "AAA", "--notes", "x")
    assert code == 1
    assert "2 alerts match 'AAA'" in err
    assert fake.writes() == []


def test_zone_parsing():
    assert ic_writes.parse_zone("starter:48:50") == {"tier": "starter", "low": 48, "high": 50}
    assert ic_writes.parse_zone("deep::46") == {"tier": "deep", "low": None, "high": 46}
    assert ic_writes.parse_zone("top:100.5:") == {"tier": "top", "low": "100.5", "high": None}
    for bad in ("x", "t::", "t:5:3", ":1:2", "t:a:2"):
        with pytest.raises(ic_writes.WriteError):
            ic_writes.parse_zone(bad)


# --- dry run --------------------------------------------------------------------------------


def test_dry_run_renders_request_and_sends_nothing(env, fake, capsys):
    code, out, _ = _hub(
        capsys, "alert", "add", "--symbol", "ccc", "--condition", "below", "--threshold", "18.5",
        "--notes", "tier one", "--dry-run",
    )  # fmt: skip
    assert code == 0
    assert "dry-run: ADD_ALERT CCC" in out
    assert "POST /api/v1/alerts" in out
    body = json.loads(out[out.index("{") : out.rindex("}") + 1])
    assert body == {
        "condition_type": "below",
        "equity_symbol": "CCC",
        "name": "CCC below 18.5",
        "notes": "tier one",
        "threshold_value": "18.5",
    }
    assert "nothing sent" in out
    assert fake.writes() == []
    assert _rows(env) == []


def test_dry_run_reports_that_yes_is_needed_without_refusing(env, fake, capsys):
    code, out, _ = _hub(capsys, "alert", "remove", "BBB", "--dry-run")
    assert code == 0
    assert "DELETE /api/v1/alerts/3" in out
    assert "requires --yes (removes an alert); --yes NOT given" in out
    assert fake.writes() == []


def test_dry_run_resolves_names_with_reads_only(env, fake, capsys):
    _hub(capsys, "watchlist", "update-item", "BBB", "--thesis", "t", "--dry-run")
    assert {c[0] for c in fake.calls} == {"GET"}


# --- applying, the log, the receipt ---------------------------------------------------------


def test_alert_add_logs_row_and_posts_receipt(env, fake, capsys):
    code, out, err = _hub(
        capsys, "alert", "add", "--symbol", "CCC", "--condition", "above", "--threshold", "30",
        "--source-ref", "session-1", "--summary", "carry tier",
    )  # fmt: skip
    assert code == 0, err
    assert "applied ADD_ALERT CCC" in out and "receipt 901" in out
    (row,) = _rows(env)
    assert row["action"] == "ADD_ALERT" and row["target"] == "CCC"
    assert (row["method"], row["path"]) == ("POST", "/api/v1/alerts")
    assert json.loads(row["request"])["equity_symbol"] == "CCC"
    assert row["before"] is None
    assert json.loads(row["after"])["name"] == "CCC above 30"
    assert row["ic_id"] == "101" and row["receipt_id"] == "901"
    assert row["receipt_error"] is None and row["source_ref"] == "session-1"
    assert row["reverted_by"] is None
    (receipt,) = fake.receipts
    assert receipt["source"] == "investing_hub" and receipt["summary"] == "carry tier"
    (action,) = receipt["actions"]
    assert action["action"] == "ADD_ALERT" and action["target"] == "CCC"
    assert action["result"] == "applied" and "id 101" in action["detail"]


def test_receipt_failure_does_not_undo_the_write_and_is_recorded(env, fake, capsys):
    fake.fail_receipts = True
    code, out, err = _hub(
        capsys, "alert", "add", "--symbol", "CCC", "--condition", "above", "--threshold", "30"
    )
    assert code == 0
    assert "applied ADD_ALERT" in out
    assert "WARN receipt failed" in err and "the write stands" in err
    assert any(a["name"] == "CCC above 30" for a in fake.alerts)
    (row,) = _rows(env)
    assert row["receipt_id"] is None
    assert "receipt store down" in row["receipt_error"]
    _, listing, _ = _hub(capsys, "writes")
    assert "receipt FAILED" in listing


def test_modify_records_before_and_after(env, fake, capsys):
    code, _, err = _hub(
        capsys, "alert", "modify", "BBB", "--threshold", "4.5", "--cooldown", "120", "--notes", "n"
    )
    assert code == 0, err
    (row,) = _rows(env)
    assert json.loads(row["before"])["threshold_value"] == "5.00"
    assert json.loads(row["after"])["threshold_value"] == "4.5"
    assert json.loads(row["request"]) == {
        "cooldown_minutes": 120,
        "notes": "n",
        "threshold_value": "4.5",
    }
    assert row["ic_id"] == "3"


def test_append_thesis_appends_to_the_current_thesis(env, fake, capsys):
    code, _, err = _hub(
        capsys, "watchlist", "update-item", "AAA", "--watchlist", "Theme One",
        "--append-thesis", "new point",
    )  # fmt: skip
    assert code == 0, err
    assert fake.watchlists[0]["items"][0]["thesis"] == "old thesis\n\nnew point"
    (row,) = _rows(env)
    assert row["ic_id"] == "1:11"
    assert json.loads(row["before"])["thesis"] == "old thesis"


def test_entry_zones_and_clear_flags(env, fake, capsys):
    code, _, err = _hub(
        capsys, "watchlist", "update-item", "BBB", "--entry-zone", "starter:48:50",
        "--entry-zone", "deep::46", "--clear", "notes", "--catalyst-tag", "Uranium Restart",
    )  # fmt: skip
    assert code == 0, err
    method, path, body = fake.writes()[0]
    assert (method, path) == ("PUT", "/api/v1/watchlists/2/items/22")
    assert body["entry_zones"] == [
        {"tier": "starter", "low": 48, "high": 50},
        {"tier": "deep", "low": None, "high": 46},
    ]
    assert body["notes"] is None and body["catalyst_tags"] == ["uranium restart"]


def test_thesis_and_append_thesis_conflict(env, fake, capsys):
    code, _, err = _hub(
        capsys, "watchlist", "update-item", "BBB", "--thesis", "a", "--append-thesis", "b"
    )
    assert code == 1 and "not both" in err


def test_watchlist_add_item_create_and_trade_event_trigger_lesson_ratio(env, fake, capsys):
    fake_calls = fake.calls
    steps = [
        ("watchlist", "add-item", "ddd", "--watchlist", "Theme One", "--thesis", "t",
         "--target-price", "12.5", "--no-track-calendar"),
        ("watchlist", "create", "New List", "--description", "d"),
        ("event", "add", "--title", "Test event", "--date", "2026-10-15", "--importance", "high"),
        ("trade", "log", "AAA", "--type", "buy", "--quantity", "10", "--price", "5",
         "--account", "Roth", "--executed-at", "2026-05-12", "--yes"),
        ("trigger", "add", "Test ladder", "--rule", "if x", "--action", "then y",
         "--alert", "BBB", "--tier", "yellow"),
        ("lesson", "add", "AAA", "--outcome", "partial", "--lesson", "sized small", "--tag", "T"),
        ("ratio", "add", "A/B", "--numerator", "aaa", "--denominator", "bbb"),
    ]  # fmt: skip
    for step in steps:
        code, _, err = _hub(capsys, *step)
        assert code == 0, (step, err)
    sent = {(m, p): b for m, p, b in fake_calls if m == "POST" and "receipts" not in p}
    assert sent[("POST", "/api/v1/watchlists/1/items")] == {
        "symbol": "DDD", "thesis": "t", "target_price": "12.5", "track_calendar": False,
    }  # fmt: skip
    assert sent[("POST", "/api/v1/watchlists")] == {"name": "New List", "description": "d"}
    assert sent[("POST", "/api/v1/events")]["event_date"] == "2026-10-15"
    trade = sent[("POST", "/api/v1/trades")]
    assert trade["account_id"] == 7 and trade["executed_at"] == "2026-05-12T12:00:00.000Z"
    assert (trade["symbol"], trade["trade_type"], trade["fees"]) == ("AAA", "buy", 0)
    assert sent[("POST", "/api/v1/triggers")]["alert_ids"] == [3]
    assert sent[("POST", "/api/v1/lessons")] == {
        "symbol": "AAA", "thesis_outcome": "partial", "lesson": "sized small", "tags": ["T"],
    }  # fmt: skip
    assert sent[("POST", "/api/v1/ratios")]["numerator_symbol"] == "AAA"
    assert [r["action"] for r in _rows(env)] == [
        "ADD_TO_WATCHLIST", "CREATE_WATCHLIST", "ADD_CALENDAR_EVENT", "LOG_TRADE",
        "ADD_TRIGGER", "ADD_LESSON", "ADD_RATIO",
    ]  # fmt: skip
    assert len(fake.receipts) == 7


def test_ic_validation_error_detail_is_shown_and_logged_failed(env, fake, capsys, monkeypatch):
    real = fake._route

    def route(method, path, body):
        if method == "POST" and path.endswith("/alerts"):
            fake._err(422, [{"loc": ["body", "threshold_value"], "msg": "must be positive"}])
        return real(method, path, body)

    monkeypatch.setattr(fake, "_route", route)
    code, _, err = _hub(
        capsys, "alert", "add", "--symbol", "CCC", "--condition", "above", "--threshold", "30"
    )
    assert code == 1
    assert "threshold_value: must be positive" in err
    assert [r["status"] for r in _rows(env)] == ["failed"] and fake.receipts == []


def test_write_refused_before_sending_when_the_log_is_unavailable(env, fake, capsys):
    (env / "data" / "hub.db").unlink()
    code, _, err = _hub(capsys, "alert", "remove", "BBB", "--yes")
    assert code == 1 and "database not found" in err
    assert fake.writes() == []
    sqlite3.connect(env / "data" / "hub.db").close()  # a database without the table
    code, _, err = _hub(capsys, "alert", "remove", "BBB", "--yes")
    assert code == 1 and "hub db migrate" in err
    assert fake.writes() == []


# --- confirmation policy --------------------------------------------------------------------


def test_trade_log_requires_yes(env, fake, capsys):
    argv = ("trade", "log", "AAA", "--type", "buy", "--quantity", "1", "--price", "2")
    code, _, err = _hub(capsys, *argv)
    assert code == 1 and "needs --yes" in err and "confirm with the operator" in err
    assert fake.writes() == [] and _rows(env) == []
    code, _, _ = _hub(capsys, *argv, "--yes")
    assert code == 0
    assert [c[1] for c in fake.writes()] == ["/api/v1/trades"]


def test_alert_remove_requires_yes(env, fake, capsys):
    code, _, err = _hub(capsys, "alert", "remove", "BBB")
    assert code == 1 and "needs --yes" in err
    assert len(fake.alerts) == 3
    assert _hub(capsys, "alert", "remove", "BBB", "--yes")[0] == 0
    assert [a["id"] for a in fake.alerts] == [1, 2]


def test_deactivating_an_alert_requires_yes_but_other_edits_do_not(env, fake, capsys):
    code, _, err = _hub(capsys, "alert", "modify", "BBB", "--inactive")
    assert code == 1 and "deactivates an alert" in err
    assert fake.alerts[2]["is_active"] is True
    assert _hub(capsys, "alert", "modify", "BBB", "--threshold", "6")[0] == 0
    assert _hub(capsys, "alert", "modify", "BBB", "--active")[0] == 0
    assert _hub(capsys, "alert", "modify", "BBB", "--inactive", "--yes")[0] == 0
    assert fake.alerts[2]["is_active"] is False


def test_other_writes_apply_without_yes(env, fake, capsys):
    steps = [
        ("event", "add", "--title", "T", "--date", "2026-10-15"),
        ("trigger", "add", "N", "--rule", "r", "--action", "a"),
        ("lesson", "add", "AAA", "--outcome", "wrong", "--lesson", "l"),
    ]
    assert all(_hub(capsys, *s)[0] == 0 for s in steps)


def test_trade_shape_checks(env, fake, capsys):
    base = ("trade", "log", "AAA", "--quantity", "1", "--yes")
    assert "needs --account" in _hub(capsys, *base, "--type", "dividend", "--price", "1")[2]
    assert "split" in _hub(capsys, *base, "--type", "split", "--account", "Roth")[2]
    assert "--price is required" in _hub(capsys, *base, "--type", "buy")[2]
    assert fake.writes() == []


# --- revert ---------------------------------------------------------------------------------


def test_revert_create_deletes_and_is_logged(env, fake, capsys):
    _hub(capsys, "alert", "add", "--symbol", "CCC", "--condition", "above", "--threshold", "30")
    code, _, err = _hub(capsys, "revert", "1")
    assert code == 1 and "needs --yes" in err  # a revert that removes an alert is gated too
    code, out, err = _hub(capsys, "revert", "1", "--yes")
    assert code == 0, err
    assert not any(a["name"] == "CCC above 30" for a in fake.alerts)
    first, second = _rows(env)
    assert first["reverted_by"] == second["id"]
    assert second["action"] == "REVERT_ADD_ALERT" and second["method"] == "DELETE"
    assert "write #1" in second["target"]
    assert (
        len(fake.receipts) == 2 and fake.receipts[1]["actions"][0]["action"] == "REVERT_ADD_ALERT"
    )
    assert _hub(capsys, "revert", "1", "--yes")[0] == 1  # already reverted
    code, _, err = _hub(capsys, "revert", "2", "--yes")
    assert code == 1 and "itself a revert" in err
    _, listing, _ = _hub(capsys, "writes")
    assert "(reverted by #2)" in listing and "REVERT_ADD_ALERT" in listing


def test_revert_modify_puts_the_recorded_before_values(env, fake, capsys):
    _hub(capsys, "alert", "modify", "BBB", "--threshold", "4.5", "--notes", "n")
    code, _, err = _hub(capsys, "revert", "1")
    assert code == 0, err
    assert fake.alerts[2]["threshold_value"] == "5.00" and fake.alerts[2]["notes"] is None
    method, path, body = fake.writes()[-1]
    assert (method, path) == ("PUT", "/api/v1/alerts/3")
    assert body == {"notes": None, "threshold_value": "5.00"}


def test_revert_modify_refuses_when_live_state_changed(env, fake, capsys):
    _hub(capsys, "alert", "modify", "BBB", "--threshold", "4.5")
    fake.alerts[2]["threshold_value"] = "9.00"  # someone edited it in IC meanwhile
    before = len(fake.writes())
    code, _, err = _hub(capsys, "revert", "1")
    assert code == 1
    assert "changed since write #1" in err and "threshold_value" in err
    assert len(fake.writes()) == before
    assert len(_rows(env)) == 1 and _rows(env)[0]["reverted_by"] is None


def test_revert_unchanged_fields_do_not_block_on_numeric_formatting(env, fake, capsys):
    _hub(capsys, "alert", "modify", "BBB", "--threshold", "4.5")
    fake.alerts[2]["threshold_value"] = "4.50"  # same number, different text
    assert _hub(capsys, "revert", "1")[0] == 0


def test_revert_create_refuses_when_live_state_changed(env, fake, capsys):
    _hub(capsys, "alert", "add", "--symbol", "CCC", "--condition", "above", "--threshold", "30")
    fake.alerts[-1]["name"] = "renamed elsewhere"
    code, _, err = _hub(capsys, "revert", "1", "--yes")
    assert code == 1 and "changed since" in err and "name" in err
    assert any(a["name"] == "renamed elsewhere" for a in fake.alerts)


def test_revert_remove_recreates_the_alert_from_before_state(env, fake, capsys):
    _hub(capsys, "alert", "remove", "BBB", "--yes")
    assert [a["name"] for a in fake.alerts] == ["AAA below 10", "AAA above 20"]
    code, out, err = _hub(capsys, "revert", "1")
    assert code == 0, err
    method, path, body = fake.writes()[-1]
    assert (method, path) == ("POST", "/api/v1/alerts")
    assert body["equity_symbol"] == "BBB" and body["name"] == "BBB below 5"
    assert body["condition_type"] == "below" and body["is_active"] is True
    assert fake.alerts[-1]["name"] == "BBB below 5"
    assert _rows(env)[1]["ic_id"] == str(fake.alerts[-1]["id"])


def test_revert_remove_refuses_if_the_alert_is_back(env, fake, capsys):
    _hub(capsys, "alert", "remove", "BBB", "--yes")
    fake.alerts.append(FakeIc._alert(3, "BBB below 5", "below", 5, "BBB"))
    code, _, err = _hub(capsys, "revert", "1")
    assert code == 1 and "exists again" in err


def test_revert_event_add_deletes_unless_changed_by_a_later_write(env, fake, capsys):
    _hub(capsys, "event", "add", "--title", "T", "--date", "2026-10-15")
    _hub(capsys, "event", "update", "--id", EVENT_ID, "--date", "2026-10-16")
    code, _, err = _hub(capsys, "revert", "1")
    assert code == 1 and "later hub write(s) #2" in err
    code, _, err = _hub(capsys, "revert", "2")
    assert code == 1 and "no before-state" in err
    assert fake.writes()[-1][0] == "PUT"


def test_revert_event_add_alone(env, fake, capsys):
    _hub(capsys, "event", "add", "--title", "T", "--date", "2026-10-15")
    assert _hub(capsys, "revert", "1")[0] == 0
    assert fake.writes()[-1][:2] == ("DELETE", f"/api/v1/events/{EVENT_ID}")


@pytest.mark.parametrize(
    ("argv", "why"),
    [
        (("trade", "log", "AAA", "--type", "buy", "--quantity", "1", "--price", "2", "--yes"),
         "trades"),
        (("watchlist", "add-item", "DDD", "--watchlist", "Theme One"), "watchlist items"),
        (("ratio", "add", "A/B", "--numerator", "a", "--denominator", "b"), "ratios"),
        (("lesson", "add", "AAA", "--outcome", "wrong", "--lesson", "l"), "lessons"),
        (("trigger", "add", "N", "--rule", "r", "--action", "a"), "triggers"),
        (("event", "remove", "--id", EVENT_ID), "before-state"),
    ],
)  # fmt: skip
def test_revert_refuses_what_ic_cannot_undo(env, fake, capsys, argv, why):
    assert _hub(capsys, *argv)[0] == 0
    before = len(fake.writes())
    code, _, err = _hub(capsys, "revert", "1", "--yes")
    assert code == 1 and "cannot revert" in err and why in err
    assert len(fake.writes()) == before


def test_revert_item_modify_restores_before_values(env, fake, capsys):
    _hub(capsys, "watchlist", "update-item", "AAA", "--watchlist", "Theme One", "--thesis", "x")
    assert _hub(capsys, "revert", "1")[0] == 0
    assert fake.watchlists[0]["items"][0]["thesis"] == "old thesis"


def test_revert_unknown_id(env, fake, capsys):
    code, _, err = _hub(capsys, "revert", "9")
    assert code == 1 and "no ic_writes row #9" in err


def test_revert_dry_run_sends_nothing(env, fake, capsys):
    _hub(capsys, "alert", "modify", "BBB", "--threshold", "4.5")
    before = len(fake.writes())
    code, out, _ = _hub(capsys, "revert", "1", "--dry-run")
    assert code == 0 and "dry-run: REVERT_MODIFY_ALERT" in out
    assert len(fake.writes()) == before and len(_rows(env)) == 1


def test_trigger_update_resolves_by_name_and_reverts_from_get_state(env, fake, capsys):
    fake.triggers = [
        fake._trigger(41, "Test ladder", "r1", "a1"),
        fake._trigger(42, "Old ladder", "r0", "a0", status="retired"),
    ]
    fake.triggers[0]["alerts"] = fake._links([3])
    code, out, err = _hub(capsys, "trigger", "update", "Test", "--action", "a2", "--alert", "AAA")
    assert code == 1 and "2 alerts match" in err
    code, out, err = _hub(capsys, "trigger", "update", "Test", "--action", "a2", "--alert", "BBB")
    assert code == 0, err
    assert fake.writes()[-1] == ("PUT", "/api/v1/triggers/41", {"action": "a2", "alert_ids": [3]})
    (row,) = _rows(env)
    assert json.loads(row["before"])["action"] == "a1"
    assert json.loads(row["after"])["action"] == "a2" and row["ic_id"] == "41"
    assert _hub(capsys, "revert", "1")[0] == 0
    assert fake.writes()[-1][2] == {"action": "a1", "alert_ids": [3]}
    assert fake.triggers[0]["action"] == "a1"


def test_trigger_revert_refuses_when_changed_since(env, fake, capsys):
    fake.triggers = [fake._trigger(41, "Test ladder", "r1", "a1")]
    _hub(capsys, "trigger", "update", "Test", "--action", "a2")
    fake.triggers[0]["action"] = "edited in the app"
    code, _, err = _hub(capsys, "revert", "1")
    assert code == 1 and "changed since write #1" in err and "action" in err


def test_trigger_retire_and_unknown_name(env, fake, capsys):
    fake.triggers = [fake._trigger(41, "Test ladder", "r1", "a1")]
    assert _hub(capsys, "trigger", "retire", "Test")[0] == 0
    assert fake.writes()[-1][:2] == ("POST", "/api/v1/triggers/41/retire")
    code, _, err = _hub(capsys, "trigger", "retire", "Nope", "--dry-run")
    assert code == 1 and "no trigger matches" in err


def test_trigger_by_id_reads_before_state(env, fake, capsys):
    fake.triggers = [fake._trigger(41, "Test ladder", "r1", "a1")]
    assert _hub(capsys, "trigger", "update", "--id", "41", "--tier", "red")[0] == 0
    assert json.loads(_rows(env)[0]["before"])["tier"] is None
    assert "no trigger with id 9" in _hub(capsys, "trigger", "retire", "--id", "9")[2]


def test_unexpected_payload_shape_is_a_one_line_error(env, fake, capsys, monkeypatch):
    real = fake._route

    def route(method, path, body):
        if path.endswith("/alerts") and method == "GET":
            return _Resp({"data": [{"name": "x"}]})  # a row with no id
        return real(method, path, body)

    monkeypatch.setattr(fake, "_route", route)
    code, _, err = _hub(capsys, "alert", "remove", "x", "--yes")
    assert code == 1 and "unexpected IC response shape: missing 'id'" in err
    assert len(err.strip().splitlines()) == 1


# --- write status: pending, failed, unknown --------------------------------------------------


def test_refused_write_is_logged_failed_and_not_revertible(env, fake, capsys, monkeypatch):
    real = fake._route

    def route(method, path, body):
        if method == "POST" and path.endswith("/alerts"):
            fake._err(422, "bad threshold")
        return real(method, path, body)

    monkeypatch.setattr(fake, "_route", route)
    code, _, err = _hub(capsys, "alert", "add", "--symbol", "CCC", "--condition", "above",
                        "--threshold", "30")  # fmt: skip
    assert code == 1 and "bad threshold" in err
    (row,) = _rows(env)
    assert row["status"] == "failed" and "bad threshold" in row["error"]
    assert row["after"] is None and fake.receipts == []
    _, listing, _ = _hub(capsys, "writes")
    assert "FAILED ADD_ALERT" in listing
    code, _, err = _hub(capsys, "revert", "1", "--yes")
    assert code == 1 and "only applied writes" in err


@pytest.mark.parametrize("failure", ["http500", "timeout"])
def test_ambiguous_failure_is_logged_unknown_with_a_warning(
    env, fake, capsys, monkeypatch, failure
):
    real = fake._route

    def route(method, path, body):
        if method == "POST" and path.endswith("/alerts"):
            if failure == "timeout":
                raise TimeoutError
            fake._err(502, "bad gateway")
        return real(method, path, body)

    monkeypatch.setattr(fake, "_route", route)
    code, _, err = _hub(capsys, "alert", "add", "--symbol", "CCC", "--condition", "above",
                        "--threshold", "30")  # fmt: skip
    assert code == 1 and "check IC before retrying" in err and "unknown" in err
    (row,) = _rows(env)
    assert row["status"] == "unknown" and row["error"]
    assert "only applied writes" in _hub(capsys, "revert", "1", "--yes")[2]


def test_pending_row_exists_before_the_request_is_sent(env, fake, capsys, monkeypatch):
    seen = []
    real = fake._route

    def route(method, path, body):
        if method == "POST" and path.endswith("/alerts"):
            seen.append([(r["status"], r["request"]) for r in _rows(env)])
        return real(method, path, body)

    monkeypatch.setattr(fake, "_route", route)
    _hub(capsys, "alert", "add", "--symbol", "CCC", "--condition", "above", "--threshold", "30")
    assert len(seen[0]) == 1 and seen[0][0][0] == "pending"
    assert json.loads(seen[0][0][1])["equity_symbol"] == "CCC"
    assert _rows(env)[0]["status"] == "applied"


# --- exact comparison and inputs ---------------------------------------------------------------


def test_norm_coerces_only_numeric_keys():
    assert ic_writes._norm("5.00", "threshold_value") == ic_writes._norm(5, "threshold_value")
    assert ic_writes._norm("1.0", "name") != ic_writes._norm("1", "name")
    assert ic_writes._norm("2026", "notes") != ic_writes._norm("2026.0", "notes")
    zones = [{"tier": "1", "low": "48.0", "high": 50}]
    assert ic_writes._norm(zones, "entry_zones") == [{"tier": "1", "low": "48", "high": "50"}]
    assert ic_writes._norm([{"tier": "1.0"}], "z") != ic_writes._norm([{"tier": "1"}], "z")


def test_revert_blocks_on_a_string_that_differs_only_numerically(env, fake, capsys):
    _hub(capsys, "alert", "modify", "BBB", "--notes", "2026")
    fake.alerts[2]["notes"] = "2026.0"
    assert _hub(capsys, "revert", "1")[0] == 1


def test_split_accepts_numeric_zero_forms(env, fake, capsys):
    base = ("trade", "log", "AAA", "--type", "split", "--quantity", "4", "--yes")
    assert _hub(capsys, *base, "--price", "0.0", "--fees", "0.00")[0] == 0
    assert fake.writes()[-1][2]["price"] == 0
    assert "split" in _hub(capsys, *base, "--price", "1")[2]


def test_executed_at_is_always_utc_aware(env, fake, capsys):
    base = ("trade", "log", "AAA", "--type", "buy", "--quantity", "1", "--price", "2", "--yes")
    _hub(capsys, *base, "--executed-at", "2026-05-12T09:30:00")
    _hub(capsys, *base, "--executed-at", "2026-05-12T09:30:00-04:00")
    _hub(capsys, *base)
    stamps = [c[2]["executed_at"] for c in fake.writes()]
    assert stamps[0] == "2026-05-12T09:30:00.000Z"
    assert stamps[1] == "2026-05-12T13:30:00.000Z"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", stamps[2])


def test_prices_travel_as_exact_decimal_strings(env, fake, capsys):
    _hub(capsys, "alert", "add", "--symbol", "CCC", "--condition", "below", "--threshold", "18.1")
    assert fake.writes()[-1][2]["threshold_value"] == "18.1"
    _hub(capsys, "alert", "add", "--symbol", "CCD", "--condition", "below", "--threshold", "20")
    assert fake.writes()[-1][2]["threshold_value"] == 20


def test_event_type_is_checked_against_ic_enum(env, fake, capsys):
    with pytest.raises(SystemExit):
        main(["ic", "event", "add", "--type", "nonsense", "--title", "T", "--date", "2026-10-15"])
    assert (
        _hub(capsys, "event", "add", "--type", "ipo", "--title", "T", "--date", "2026-10-15")[0]
        == 0
    )


# --- listing and the ledger surface ---------------------------------------------------------


def test_writes_lists_newest_first_with_limit(env, fake, capsys):
    for n in ("1", "2", "3"):
        _hub(capsys, "lesson", "add", "AAA", "--outcome", "wrong", "--lesson", n)
    _, out, _ = _hub(capsys, "writes", "--limit", "2")
    lines = out.strip().splitlines()
    assert len(lines) == 2 and lines[0].startswith("#3") and lines[1].startswith("#2")
    assert "ADD_LESSON AAA" in lines[0] and "receipt 903" in lines[0]


def test_writes_on_an_empty_log(env, fake, capsys):
    assert _hub(capsys, "writes")[1].strip() == "no IC writes recorded"


def test_token_never_appears_in_the_log(env, fake, capsys):
    _hub(capsys, "alert", "add", "--symbol", "CCC", "--condition", "above", "--threshold", "30")
    dump = json.dumps([dict(r) for r in _rows(env)])
    assert TOKEN not in dump
