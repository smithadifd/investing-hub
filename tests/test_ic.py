"""IC client tests against a local HTTP server. Every pack and token here is synthetic."""

import io
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from hub import ic
from hub.cli import main

TOKEN = "ict_SYNTHETIC0test0token0value0000"
# A value with no token shape: only exact-value redaction can hide it.
PLAIN_TOKEN = "plain-synthetic-secret-9f8e7d"
MARKER_SYMBOL = "ZZSYNTH"
MARKER_VALUE = "424242.42"

SYNTHETIC_PACK = {
    "schema_version": "1.7",
    "advisor_actions_version": "1.4",
    "generated_at": "2026-01-02T03:04:05Z",
    "positions": [
        {
            "symbol": MARKER_SYMBOL,
            "account": "Synthetic Account",
            "quantity": "1",
            "avg_cost_basis": "1",
            "current_value": MARKER_VALUE,
        }
    ],
    "portfolio_value": MARKER_VALUE,
    "total_invested": "1",
    "exposures": [],
    "active_alerts": [],
    "recent_triggers": [],
    "watchlist_targets": [],
    "upcoming_events": [],
    "trade_summary": {"total_trades": 0, "total_realized_pnl": "0"},
    "unsupported_features": [],
}


def _doc(filename, stamp, expected, content):
    return {
        "filename": filename,
        "stamp": stamp,
        "expected_stamp": expected,
        "stamp_matches": stamp == expected,
        "content": content,
    }


SYNTHETIC_DOCS = {
    "schema_version": "1.7",
    "advisor_actions_version": "1.4",
    "handoff_schema": _doc("handoff-schema.md", "1.7", "1.7", "# Synthetic handoff schema\n"),
    "advisor_actions": _doc("advisor-actions.md", "1.3", "1.4", "# Synthetic advisor actions\n"),
}


class FakeIc:
    """A tiny IC stand-in: per-path status, body, delay and headers; records requests."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                fake.requests.append((self.path, dict(self.headers)))
                status, body, delay, extra = fake.routes.get(
                    self.path, (404, b'{"detail":"nf"}', 0, {})
                )
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    for name, value in extra.items():
                        self.send_header(name, value)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True)
        self.thread.start()

    def route(self, path, status=200, body=None, delay=0.0, headers=None):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.routes[path] = (status, raw, delay, headers or {})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_ic():
    server = FakeIc()
    yield server
    server.close()


@pytest.fixture
def other_listener():
    """A second local listener standing in for a redirect target or a proxy."""
    server = FakeIc()
    yield server
    server.close()


class FakeOpener:
    """Stands in for the opener ``hub.ic`` builds; ``handler(request, timeout)`` answers."""

    def __init__(self, handler):
        self.handler = handler

    def open(self, request, timeout=None):
        return self.handler(request, timeout)


def _use_opener(monkeypatch, handler):
    monkeypatch.setattr(ic, "_build_opener", lambda: FakeOpener(handler))


@pytest.fixture
def env(tmp_path, monkeypatch, fake_ic):
    """Run from an empty temp dir with a synthetic token and the fake IC as base URL."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ic.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ic.BASE_URL_ENV, fake_ic.base_url)
    return tmp_path


def _all_file_text(root):
    return "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file()
    )


def _assert_one_redacted_line(captured, root, token=TOKEN):
    assert captured.out == ""
    assert captured.err.count("\n") == 1, captured.err
    assert token not in captured.err
    assert token not in _all_file_text(root)


# --- hub ic preflight --------------------------------------------------------------------


@pytest.fixture
def preflight_http(tmp_path, monkeypatch):
    """An in-process HTTP response fixture for the shared GET client."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ic.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ic.BASE_URL_ENV, "http://ic.example.invalid")
    routes = {}
    requests = []

    def route(path, status=200, body=None, delay=0.0):
        routes[path] = (status, body, delay)

    def respond(request, timeout):
        path = urlsplit(request.full_url).path
        requests.append((path, dict(request.headers)))
        status, body, delay = routes.get(path, (404, {}, 0.0))
        if delay > timeout:
            raise urllib.error.URLError(TimeoutError("timed out"))
        if status != 200:
            raise urllib.error.HTTPError(request.full_url, status, "synthetic", {}, None)
        return io.BytesIO(json.dumps(body).encode())

    _use_opener(monkeypatch, respond)
    return SimpleNamespace(root=tmp_path, route=route, requests=requests)


def _ready_docs():
    docs = json.loads(json.dumps(SYNTHETIC_DOCS))
    docs["advisor_actions"] = _doc("advisor-actions.md", "1.4", "1.4", "# Synthetic\n")
    return docs


def _preflight_routes(fake_ic, pack=None, docs=None):
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK if pack is None else pack)
    fake_ic.route(ic.CONTRACT_DOCS_PATH, body=_ready_docs() if docs is None else docs)


def test_preflight_all_pass_is_read_only(preflight_http, capsys):
    _preflight_routes(preflight_http)
    assert main(["ic", "preflight"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("PASS ") == 9
    assert "PASS: 9 passed, 0 failed" in captured.out
    assert f"[GET {ic.CONTEXT_PACK_PATH}#/generated_at]" in captured.out
    assert f"[GET {ic.CONTRACT_DOCS_PATH}#/handoff_schema" in captured.out
    assert MARKER_SYMBOL not in captured.out
    assert MARKER_VALUE not in captured.out
    assert TOKEN not in captured.out
    assert list(preflight_http.root.iterdir()) == [], "preflight created a local file"
    assert [path for path, _ in preflight_http.requests] == [
        ic.CONTEXT_PACK_PATH,
        ic.CONTRACT_DOCS_PATH,
    ]


@pytest.mark.parametrize("key", ["generated_at", "schema_version", "advisor_actions_version"])
def test_preflight_fails_for_each_missing_pack_field(preflight_http, capsys, key):
    pack = dict(SYNTHETIC_PACK)
    del pack[key]
    _preflight_routes(preflight_http, pack=pack)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL {key} present" in out
    assert "PASS pack reachable" in out
    assert list(preflight_http.root.iterdir()) == []


@pytest.mark.parametrize("doc_key", ["handoff_schema", "advisor_actions"])
@pytest.mark.parametrize("field", ["stamp", "expected_stamp"])
def test_preflight_fails_for_each_missing_doc_stamp(preflight_http, capsys, doc_key, field):
    docs = _ready_docs()
    del docs[doc_key][field]
    _preflight_routes(preflight_http, docs=docs)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL {doc_key} stamps present" in out
    assert "PASS contract docs reachable" in out
    assert list(preflight_http.root.iterdir()) == []


@pytest.mark.parametrize("doc_key", ["handoff_schema", "advisor_actions"])
def test_preflight_fails_when_a_contract_doc_is_missing(preflight_http, capsys, doc_key):
    docs = _ready_docs()
    del docs[doc_key]
    _preflight_routes(preflight_http, docs=docs)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL {doc_key} stamps present" in out
    assert f"FAIL {doc_key} stamps match" in out
    assert "PASS contract docs reachable" in out


@pytest.mark.parametrize("doc_key", ["handoff_schema", "advisor_actions"])
@pytest.mark.parametrize("field", ["stamp", "expected_stamp", "stamp_matches"])
def test_preflight_fails_for_each_doc_mismatch(preflight_http, capsys, doc_key, field):
    docs = _ready_docs()
    docs[doc_key][field] = False if field == "stamp_matches" else "different"
    _preflight_routes(preflight_http, docs=docs)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL {doc_key} stamps match" in out
    assert f"PASS {doc_key} stamps present" in out
    assert list(preflight_http.root.iterdir()) == []


@pytest.mark.parametrize(
    ("doc_key", "pack_key"),
    [("handoff_schema", "schema_version"), ("advisor_actions", "advisor_actions_version")],
)
def test_preflight_compares_doc_stamps_to_pack(preflight_http, capsys, doc_key, pack_key):
    pack = dict(SYNTHETIC_PACK)
    pack[pack_key] = "different"
    _preflight_routes(preflight_http, pack=pack)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert f"FAIL {doc_key} stamps match" in out
    assert f"PASS {doc_key} stamps present" in out


@pytest.mark.parametrize("path", [ic.CONTEXT_PACK_PATH, ic.CONTRACT_DOCS_PATH])
def test_preflight_fails_when_either_endpoint_is_unavailable(preflight_http, capsys, path):
    _preflight_routes(preflight_http)
    preflight_http.route(path, status=503, body={})
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert "unavailable (503)" in out
    label = "pack reachable" if path == ic.CONTEXT_PACK_PATH else "contract docs reachable"
    assert f"FAIL {label}" in out
    assert list(preflight_http.root.iterdir()) == []


@pytest.mark.parametrize("path", [ic.CONTEXT_PACK_PATH, ic.CONTRACT_DOCS_PATH])
def test_preflight_rejects_non_object_response(preflight_http, capsys, path):
    _preflight_routes(preflight_http)
    preflight_http.route(path, body=[])
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    label = "pack reachable" if path == ic.CONTEXT_PACK_PATH else "contract docs reachable"
    assert f"FAIL {label}" in out
    assert "response is not a JSON object" in out


def test_preflight_missing_token_fails_closed(preflight_http, capsys, monkeypatch):
    monkeypatch.delenv(ic.TOKEN_ENV)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert "FAIL pack reachable" in out
    assert "FAIL contract docs reachable" in out
    assert preflight_http.requests == []
    assert list(preflight_http.root.iterdir()) == []


def test_preflight_timeout_fails_closed(preflight_http, capsys, monkeypatch):
    _preflight_routes(preflight_http)
    monkeypatch.setattr(ic, "TIMEOUT_SECONDS", 0.2)
    preflight_http.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK, delay=1.0)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert "FAIL pack reachable" in out
    assert "did not respond within 0.2s (timeout)" in out
    assert "PASS contract docs reachable" in out
    assert list(preflight_http.root.iterdir()) == []


def test_preflight_redacts_token_from_stdout_and_explicit_file(preflight_http, capsys, monkeypatch):
    monkeypatch.setenv(ic.TOKEN_ENV, PLAIN_TOKEN)
    pack = dict(SYNTHETIC_PACK)
    pack["schema_version"] = PLAIN_TOKEN
    docs = _ready_docs()
    docs["handoff_schema"]["stamp"] = PLAIN_TOKEN
    _preflight_routes(preflight_http, pack=pack, docs=docs)
    out_file = preflight_http.root / "readiness.txt"
    assert main(["ic", "preflight", "--out", str(out_file)]) == 1
    captured = capsys.readouterr()
    assert PLAIN_TOKEN not in captured.out + captured.err + out_file.read_text()
    assert captured.out == out_file.read_text()
    assert sorted(p.name for p in preflight_http.root.iterdir()) == ["readiness.txt"]


def test_preflight_redacts_an_error_echoing_the_token(preflight_http, capsys, monkeypatch):
    monkeypatch.setenv(ic.TOKEN_ENV, PLAIN_TOKEN)

    def echoed_error(base_url, path, token):
        raise ic.IcError(f"upstream echoed {PLAIN_TOKEN}")

    monkeypatch.setattr(ic, "get_json", echoed_error)
    assert main(["ic", "preflight"]) == 1
    out = capsys.readouterr().out
    assert PLAIN_TOKEN not in out
    assert f"upstream echoed {ic.REDACTED}" in out
    assert list(preflight_http.root.iterdir()) == []


def test_preflight_explicit_output_on_success(preflight_http, capsys):
    _preflight_routes(preflight_http)
    out_file = preflight_http.root / "readiness.txt"
    assert main(["ic", "preflight", "--out", str(out_file)]) == 0
    assert out_file.is_file(), "explicit --out did not create the report"
    assert out_file.read_text() == capsys.readouterr().out
    assert "PASS: 9 passed, 0 failed" in out_file.read_text()
    assert sorted(p.name for p in preflight_http.root.iterdir()) == ["readiness.txt"]


# --- hub ic pull -------------------------------------------------------------------------


def test_pull_stores_pack_and_meta(env, fake_ic, capsys):
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0

    pack = json.loads((env / "data/ic/pack-latest.json").read_text())
    assert pack == SYNTHETIC_PACK
    meta = json.loads((env / "data/ic/pack-meta.json").read_text())
    assert set(meta) == {"fetched_at", "generated_at", "schema_version", "advisor_actions_version"}
    assert meta["schema_version"] == "1.7"
    assert meta["advisor_actions_version"] == "1.4"
    assert meta["generated_at"] == "2026-01-02T03:04:05Z"
    assert meta["fetched_at"].endswith("+00:00")
    # the pack holds portfolio figures: owner-only files
    for name in ("pack-latest.json", "pack-meta.json"):
        assert (env / "data/ic" / name).stat().st_mode & 0o777 == 0o600
    # atomic write leaves no temp files behind
    assert sorted(p.name for p in (env / "data/ic").iterdir()) == [
        "pack-latest.json",
        "pack-meta.json",
    ]

    out = capsys.readouterr()
    assert out.err == ""
    assert out.out.count("\n") == 1
    assert "schema 1.7" in out.out and "advisor-actions 1.4" in out.out
    assert MARKER_SYMBOL not in out.out and MARKER_VALUE not in out.out
    assert TOKEN not in out.out


def test_pull_sends_bearer_token_to_the_pack_route(env, fake_ic):
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0
    path, headers = fake_ic.requests[-1]
    assert path == "/api/v1/export/context-pack"
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_pull_never_writes_the_token(env, fake_ic):
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0
    assert TOKEN not in _all_file_text(env)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "rejected the token (401)"),
        (403, "(403): the token lacks the scope"),
        (503, "unavailable (503)"),
        (500, "HTTP 500"),
    ],
)
def test_pull_http_errors_print_one_line(env, fake_ic, capsys, status, expected):
    fake_ic.route(ic.CONTEXT_PACK_PATH, status=status, body={"detail": TOKEN})
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert expected in captured.err
    _assert_one_redacted_line(captured, env)
    assert not (env / "data").exists()


def test_failed_pull_keeps_the_previous_pack(env, fake_ic, capsys):
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0
    before = (env / "data/ic/pack-meta.json").read_text()
    fake_ic.route(ic.CONTEXT_PACK_PATH, status=503, body={})
    assert main(["ic", "pull"]) == 1
    assert (env / "data/ic/pack-meta.json").read_text() == before


def test_pull_timeout_prints_one_line(env, fake_ic, capsys, monkeypatch):
    monkeypatch.setattr(ic, "TIMEOUT_SECONDS", 0.2)
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK, delay=1.0)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "did not respond within 0.2s (timeout)" in captured.err
    _assert_one_redacted_line(captured, env)


def test_connect_timeout_prints_one_line(env, capsys, monkeypatch):
    def slow_connect(request, timeout):
        raise urllib.error.URLError(TimeoutError("timed out"))

    _use_opener(monkeypatch, slow_connect)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "(timeout)" in captured.err
    _assert_one_redacted_line(captured, env)


def test_opener_gets_an_explicit_15s_timeout(env, fake_ic, monkeypatch):
    seen = {}
    real_build = ic._build_opener

    def spy_build():
        opener = real_build()
        real_open = opener.open

        def spy_open(request, timeout=None, **kwargs):
            seen["timeout"] = timeout
            return real_open(request, timeout=timeout, **kwargs)

        opener.open = spy_open
        return opener

    monkeypatch.setattr(ic, "_build_opener", spy_build)
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0
    assert seen["timeout"] == 15.0, f"timeout passed to the opener: {seen.get('timeout')}"


def test_pull_connection_refused_prints_one_line(env, capsys, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv(ic.BASE_URL_ENV, f"http://127.0.0.1:{port}")
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "connection refused" in captured.err
    _assert_one_redacted_line(captured, env)


def test_pull_non_json_prints_one_line(env, fake_ic, capsys):
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=b"<html>not json</html>")
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "non-JSON" in captured.err
    _assert_one_redacted_line(captured, env)


def test_pull_rejects_a_pack_without_versions(env, fake_ic, capsys):
    pack = {k: v for k, v in SYNTHETIC_PACK.items() if k != "advisor_actions_version"}
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=pack)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "missing advisor_actions_version" in captured.err
    _assert_one_redacted_line(captured, env)
    assert not (env / "data").exists()


def test_missing_token_prints_one_line(env, fake_ic, capsys, monkeypatch):
    monkeypatch.delenv(ic.TOKEN_ENV)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "IC_API_TOKEN is not set" in captured.err
    _assert_one_redacted_line(captured, env)
    assert fake_ic.requests == []


def test_token_with_control_characters_is_refused(env, fake_ic, capsys, monkeypatch):
    monkeypatch.setenv(ic.TOKEN_ENV, "ict_bad\rvalue")
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "control" in captured.err
    assert "bad" not in captured.err
    assert fake_ic.requests == []


def test_token_with_printable_non_ascii_is_refused(env, fake_ic, capsys, monkeypatch):
    monkeypatch.setenv(ic.TOKEN_ENV, "ict_synth\u00e9tic")
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "non-ASCII" in captured.err, captured.err
    assert fake_ic.requests == [], "a non-ASCII token must never be sent"


def test_missing_base_url_prints_one_line(env, capsys, monkeypatch):
    monkeypatch.delenv(ic.BASE_URL_ENV)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "no IC base URL" in captured.err
    _assert_one_redacted_line(captured, env)


def test_base_url_comes_from_config_yaml(env, fake_ic, monkeypatch):
    monkeypatch.delenv(ic.BASE_URL_ENV)
    (env / "config.yaml").write_text(f"ic:\n  base_url: {fake_ic.base_url}/\n")
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0
    assert fake_ic.requests[-1][0] == ic.CONTEXT_PACK_PATH


def test_env_base_url_overrides_config(env, fake_ic):
    (env / "config.yaml").write_text("ic:\n  base_url: http://192.0.2.1:9\n")
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0


# --- redaction -----------------------------------------------------------------------------


@pytest.mark.parametrize("token", [TOKEN, PLAIN_TOKEN])
def test_unexpected_error_carrying_the_token_is_redacted(env, capsys, monkeypatch, token):
    monkeypatch.setenv(ic.TOKEN_ENV, token)

    def boom(request, timeout):
        raise RuntimeError(f"wire trace: {request.get_header('Authorization')}")

    _use_opener(monkeypatch, boom)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert ic.REDACTED in captured.err
    _assert_one_redacted_line(captured, env, token)


def _assert_clean_error(err, secret):
    assert secret not in str(err)
    assert all(secret not in str(arg) for arg in err.args), err.args
    assert err.__cause__ is None
    assert err.__context__ is None, f"reachable original: {err.__context__!r}"
    assert err.__suppress_context__ is True


def test_error_from_get_json_is_redacted_and_unchained(monkeypatch):
    def boom(request, timeout):
        raise RuntimeError(f"echo {PLAIN_TOKEN}")

    _use_opener(monkeypatch, boom)
    with pytest.raises(ic.IcError) as exc:
        ic.get_json("http://127.0.0.1:9", "/x", ic._Token(PLAIN_TOKEN))
    _assert_clean_error(exc.value, PLAIN_TOKEN)


def test_ic_error_from_the_request_is_redacted_and_unchained(monkeypatch):
    def fail(base_url, path, token, timeout):
        raise ic.IcError(f"echo {PLAIN_TOKEN}")

    monkeypatch.setattr(ic, "_get_json", fail)
    with pytest.raises(ic.IcError) as exc:
        ic.get_json("http://127.0.0.1:9", "/x", ic._Token(PLAIN_TOKEN))
    _assert_clean_error(exc.value, PLAIN_TOKEN)


def test_bad_base_url_error_object_is_redacted_and_unchained():
    environ = {ic.BASE_URL_ENV: f"ftp://example.invalid/{PLAIN_TOKEN}", ic.TOKEN_ENV: PLAIN_TOKEN}
    with pytest.raises(ic.IcError) as exc:
        ic.load_base_url(environ=environ)
    _assert_clean_error(exc.value, PLAIN_TOKEN)


def test_config_errors_leave_no_chained_exception(tmp_path):
    with pytest.raises(ic.IcError) as exc:
        ic.load_base_url(config_path=tmp_path / "absent.yaml", environ={})
    _assert_clean_error(exc.value, PLAIN_TOKEN)
    (tmp_path / "bad.yaml").write_text("ic: [unclosed\n")
    with pytest.raises(ic.IcError) as exc:
        ic.load_base_url(config_path=tmp_path / "bad.yaml", environ={})
    _assert_clean_error(exc.value, PLAIN_TOKEN)


def test_doc_missing_field_error_leaves_no_chained_exception(fake_ic):
    body = json.loads(json.dumps(SYNTHETIC_DOCS))
    del body["handoff_schema"]["content"]
    fake_ic.route(ic.CONTRACT_DOCS_PATH, body=body)
    with pytest.raises(ic.IcError) as exc:
        ic.fetch_contract_docs(fake_ic.base_url, ic._Token(TOKEN))
    assert "missing content" in str(exc.value)
    _assert_clean_error(exc.value, TOKEN)


def test_token_repr_and_str_are_redacted():
    token = ic._Token(TOKEN)
    assert TOKEN not in repr(token)
    assert TOKEN not in str(token)
    assert TOKEN not in repr({"token": token})


def test_redact_hides_token_shaped_strings():
    assert ic.redact("a ict_OTHER0value b") == f"a {ic.REDACTED} b"


def test_redact_shape_covers_the_whole_token_alphabet():
    # read_token accepts any printable ASCII without whitespace, so the shape layer must too.
    assert ic.redact("a ict_ab+cd=ef.gh/ij b") == f"a {ic.REDACTED} b"


def test_redact_exact_value_wins_for_a_token_with_symbols():
    token = "ict_synth+value=with.sym/bols"
    assert ic.redact(f"x {token} y", token) == f"x {ic.REDACTED} y"


# --- transport: no redirects, no proxies ---------------------------------------------------


def test_redirect_is_refused_and_the_token_never_follows(env, fake_ic, other_listener, capsys):
    target = f"{other_listener.base_url}{ic.CONTEXT_PACK_PATH}"
    other_listener.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    fake_ic.route(ic.CONTEXT_PACK_PATH, status=302, body={}, headers={"Location": target})
    code = main(["ic", "pull"])
    leaked = [h for _, h in other_listener.requests if "Authorization" in h]
    assert leaked == [], "the redirect target received the Authorization header"
    assert other_listener.requests == []
    assert code == 1
    captured = capsys.readouterr()
    assert "IC redirected (302) — refusing to forward credentials" in captured.err
    _assert_one_redacted_line(captured, env)
    assert not (env / "data").exists()


@pytest.mark.parametrize("status", [301, 303, 307, 308])
def test_every_redirect_status_is_refused(env, fake_ic, other_listener, capsys, status):
    target = f"{other_listener.base_url}{ic.CONTRACT_DOCS_PATH}"
    other_listener.route(ic.CONTRACT_DOCS_PATH, body=SYNTHETIC_DOCS)
    fake_ic.route(ic.CONTRACT_DOCS_PATH, status=status, body={}, headers={"Location": target})
    assert main(["ic", "docs"]) == 1
    captured = capsys.readouterr()
    assert f"IC redirected ({status})" in captured.err
    assert other_listener.requests == []


def test_env_proxy_is_bypassed(env, fake_ic, other_listener, monkeypatch):
    for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, other_listener.base_url)
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0
    assert other_listener.requests == [], "the proxy saw the request"
    assert fake_ic.requests[-1][0] == ic.CONTEXT_PACK_PATH


# --- hub ic docs ---------------------------------------------------------------------------


def test_docs_prints_both_docs_and_keeps_no_copy(env, fake_ic, capsys):
    fake_ic.route(ic.CONTRACT_DOCS_PATH, body=SYNTHETIC_DOCS)
    assert main(["ic", "docs"]) == 0
    out = capsys.readouterr().out
    assert "== handoff-schema.md: stamp=1.7 expected_stamp=1.7 stamp_matches=true" in out
    assert "== advisor-actions.md: stamp=1.3 expected_stamp=1.4 stamp_matches=false" in out
    assert "# Synthetic handoff schema" in out
    assert "# Synthetic advisor actions" in out
    assert TOKEN not in out
    assert list(env.iterdir()) == []
    path, headers = fake_ic.requests[-1]
    assert path == "/api/v1/export/contract-docs"
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_docs_content_echoing_the_token_is_redacted(env, fake_ic, capsys, monkeypatch):
    monkeypatch.setenv(ic.TOKEN_ENV, PLAIN_TOKEN)
    body = json.loads(json.dumps(SYNTHETIC_DOCS))
    body["handoff_schema"]["content"] = f"# Echo\nAuthorization: Bearer {PLAIN_TOKEN}\n"
    body["advisor_actions"]["filename"] = f"echo-{PLAIN_TOKEN}.md"
    fake_ic.route(ic.CONTRACT_DOCS_PATH, body=body)
    assert main(["ic", "docs"]) == 0
    out = capsys.readouterr().out
    assert PLAIN_TOKEN not in out, "doc output carried the token"
    assert f"Authorization: Bearer {ic.REDACTED}" in out


def test_docs_null_stamp_prints_none(env, fake_ic, capsys):
    body = json.loads(json.dumps(SYNTHETIC_DOCS))
    body["handoff_schema"]["stamp"] = None
    body["handoff_schema"]["stamp_matches"] = False
    fake_ic.route(ic.CONTRACT_DOCS_PATH, body=body)
    assert main(["ic", "docs"]) == 0
    assert "stamp=None expected_stamp=1.7 stamp_matches=false" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "(401)"), (403, "(403)"), (503, "(503)"), (404, "HTTP 404")],
)
def test_docs_http_errors_print_one_line(env, fake_ic, capsys, status, expected):
    fake_ic.route(ic.CONTRACT_DOCS_PATH, status=status, body={"detail": TOKEN})
    assert main(["ic", "docs"]) == 1
    captured = capsys.readouterr()
    assert expected in captured.err
    _assert_one_redacted_line(captured, env)


def test_docs_missing_doc_prints_one_line(env, fake_ic, capsys):
    body = {k: v for k, v in SYNTHETIC_DOCS.items() if k != "advisor_actions"}
    fake_ic.route(ic.CONTRACT_DOCS_PATH, body=body)
    assert main(["ic", "docs"]) == 1
    captured = capsys.readouterr()
    assert "missing advisor_actions" in captured.err
    _assert_one_redacted_line(captured, env)


def test_fetch_contract_docs_is_reusable(fake_ic):
    fake_ic.route(ic.CONTRACT_DOCS_PATH, body=SYNTHETIC_DOCS)
    docs = ic.fetch_contract_docs(fake_ic.base_url, ic._Token(TOKEN))
    assert [(d.key, d.stamp, d.stamp_matches) for d in docs] == [
        ("handoff_schema", "1.7", True),
        ("advisor_actions", "1.3", False),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8000\r\nX-Injected: 1",
        "http://127.0.0.1:8000 trailing garbage",
        "http://127.0.0.1:8000/path\twith-tab",
        "http://127.0.0.1:80abc",
        "http://127.0.0.1:8000\x07",
        "http://",
        "http:///no-host",
    ],
)
def test_malformed_base_url_is_refused(env, fake_ic, capsys, monkeypatch, url):
    monkeypatch.setenv(ic.BASE_URL_ENV, url)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "must start with http://" in captured.err, captured.err
    assert captured.err.count("\n") == 1, captured.err
    assert fake_ic.requests == []


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:8000", "https://ic.example.invalid", "http://[::1]:8000/base/"],
)
def test_well_formed_base_url_is_accepted(url):
    assert ic.load_base_url(environ={ic.BASE_URL_ENV: url}) == url.rstrip("/")


def test_bad_base_url_error_is_redacted(env, capsys, monkeypatch):
    monkeypatch.setenv(ic.BASE_URL_ENV, f"ftp://example.invalid/{TOKEN}")
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "must start with http://" in captured.err
    _assert_one_redacted_line(captured, env)


# --- hub ic show -------------------------------------------------------------------------


SYNTHETIC_SHOW_META = {
    "schema_version": "1.7",
    "advisor_actions_version": "1.4",
    "generated_at": "2026-01-02T03:04:05Z",
    "fetched_at": "2026-01-02T03:05:00+00:00",
}

SYNTHETIC_SHOW_PACK = {
    "schema_version": "1.7",
    "advisor_actions_version": "1.4",
    "generated_at": "2026-01-02T03:04:05Z",
    "portfolio_value": "123456.78",
    "total_invested": "98765.43",
    "unknown_scalar_sentinel": "SENTINEL_SCALAR_VALUE",
    "unknown_scalar_number": 777777,
    "positions": [
        {"symbol": "AAA", "account": "Taxable", "quantity": "100", "current_value": "50000"},
        {"symbol": "BBB", "account": "IRA", "quantity": "200", "current_value": "73456.78"},
    ],
    "exposures": [
        {"name": "Tech", "weight": "0.60"},
        {"name": "Energy", "weight": "0.40"},
    ],
    "catalyst_exposures": [
        {"theme": "AI", "weight": "0.70"},
        {"theme": "Cloud", "weight": "0.30"},
    ],
    "active_alerts": [
        {"id": "alt_1", "symbol": "AAA"},
        {"id": "alt_2", "symbol": "BBB"},
    ],
    "recent_triggers": [
        {"id": "trig_1", "type": "trailing_stop"},
        {"id": "trig_2", "type": "take_profit"},
    ],
    "watchlist_targets": [
        {"symbol": "CCC", "target_price": "555"},
        {"symbol": "DDD", "target_price": "777"},
    ],
    "upcoming_events": [
        {"event": "earnings_AAA", "date": "2027-10-15"},
        {"event": "macro_release", "date": "2027-11-01"},
    ],
    "triggers": [
        {"id": "t1", "rule": "rsi_break"},
        {"id": "t2", "rule": "macd_cross"},
    ],
    "recent_handoffs": [
        {"id": "h1", "action": "rebalance_action"},
        {"id": "h2", "action": "trim_action"},
    ],
    "lessons": [
        {"lesson": "patience_note", "context": "chop_regime"},
        {"lesson": "risk_rule", "context": "sizing_limit"},
    ],
    "unsupported_features": [
        {"feature": "options_trading"},
        {"feature": "margin_borrow"},
    ],
    "future_extra_section": [
        {"name": "future_item_1"},
        {"name": "future_item_2"},
        {"name": "future_item_3"},
    ],
    "trade_summary": {"total_trades": 99, "total_realized_pnl": "8888.88"},
}


def _write_cached_pack(root, pack=SYNTHETIC_SHOW_PACK, meta=SYNTHETIC_SHOW_META):
    pack_dir = root / "data" / "ic"
    pack_dir.mkdir(parents=True, exist_ok=True)
    if pack is not None:
        target = pack_dir / "pack-latest.json"
        if isinstance(pack, bytes):
            target.write_bytes(pack)
        else:
            target.write_text(
                json.dumps(pack) if isinstance(pack, (dict, list)) else str(pack),
                encoding="utf-8",
            )
    if meta is not None:
        target = pack_dir / "pack-meta.json"
        if isinstance(meta, bytes):
            target.write_bytes(meta)
        else:
            target.write_text(
                json.dumps(meta) if isinstance(meta, (dict, list)) else str(meta),
                encoding="utf-8",
            )


def _collect_leaf_strings_and_numbers(data):
    items = []
    if isinstance(data, dict):
        for v in data.values():
            items.extend(_collect_leaf_strings_and_numbers(v))
    elif isinstance(data, list):
        for v in data:
            items.extend(_collect_leaf_strings_and_numbers(v))
    elif data is not None:
        items.append(str(data))
    return items


def test_show_cached_pack_counts(env, capsys):
    _write_cached_pack(env)
    assert main(["ic", "show"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    # Assert that no symbol, number or string from any pack item appears in stdout
    # Also assert unknown top-level scalars and portfolio figures are absent from stdout
    assert "SENTINEL_SCALAR_VALUE" not in captured.out
    assert "777777" not in captured.out
    assert "123456.78" not in captured.out
    assert "98765.43" not in captured.out
    lines = captured.out.strip().split("\n")

    assert lines[0] == (
        "schema 1.7, advisor-actions 1.4, generated 2026-01-02T03:04:05Z,"
        " fetched 2026-01-02T03:05:00+00:00"
    )
    expected_sections = [
        "positions: 2",
        "exposures: 2",
        "catalyst_exposures: 2",
        "active_alerts: 2",
        "recent_triggers: 2",
        "watchlist_targets: 2",
        "upcoming_events: 2",
        "triggers: 2",
        "recent_handoffs: 2",
        "lessons: 2",
        "unsupported_features: 2",
        "future_extra_section: 3",
        "trade_summary: present",
    ]
    assert lines[1:] == expected_sections

    for key, value in SYNTHETIC_SHOW_PACK.items():
        if isinstance(value, list):
            for item in value:
                for leaf in _collect_leaf_strings_and_numbers(item):
                    assert leaf not in captured.out, f"Item value {leaf!r} leaked in stdout"
        elif key == "trade_summary":
            for leaf in _collect_leaf_strings_and_numbers(value):
                assert leaf not in captured.out, f"Trade summary value {leaf!r} leaked in stdout"

    # --counts and no flag print identical stdout
    assert main(["ic", "show", "--counts"]) == 0
    counts_captured = capsys.readouterr()
    assert counts_captured.err == ""
    assert counts_captured.out == captured.out


@pytest.mark.parametrize(
    "trade_summary_val",
    [None, "OMIT"],
)
def test_show_trade_summary_absent(env, capsys, trade_summary_val):
    pack = {k: v for k, v in SYNTHETIC_SHOW_PACK.items() if k != "trade_summary"}
    if trade_summary_val != "OMIT":
        pack["trade_summary"] = None
    _write_cached_pack(env, pack=pack)
    assert main(["ic", "show"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "trade_summary: absent" in captured.out


@pytest.mark.parametrize(
    ("pack_val", "meta_val"),
    [
        (None, SYNTHETIC_SHOW_META),
        (SYNTHETIC_SHOW_PACK, None),
    ],
)
def test_show_missing_pack_exits_1(env, capsys, pack_val, meta_val):
    _write_cached_pack(env, pack=pack_val, meta=meta_val)
    assert main(["ic", "show"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "hub ic pull" in captured.err


@pytest.mark.parametrize(
    ("pack_content", "meta_content"),
    [
        ("not json", json.dumps(SYNTHETIC_SHOW_META)),
        (json.dumps(SYNTHETIC_SHOW_PACK), "not json"),
    ],
)
def test_show_invalid_json_exits_1(env, capsys, pack_content, meta_content):
    _write_cached_pack(env, pack=pack_content, meta=meta_content)
    assert main(["ic", "show"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "hub ic pull" in captured.err


@pytest.mark.parametrize(
    ("pack_content", "meta_content"),
    [
        (b"\xff\xfe\x00", SYNTHETIC_SHOW_META),
        (SYNTHETIC_SHOW_PACK, b"\xff\xfe\x00"),
    ],
)
def test_show_undecodable_utf8_exits_1(env, capsys, pack_content, meta_content):
    _write_cached_pack(env, pack=pack_content, meta=meta_content)
    assert main(["ic", "show"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "hub ic pull" in captured.err


def test_show_non_object_pack_exits_1(env, capsys):
    _write_cached_pack(env, pack=[1, 2, 3], meta=SYNTHETIC_SHOW_META)
    assert main(["ic", "show"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "not a JSON object" in captured.err
