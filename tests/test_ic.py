"""IC client tests against a local HTTP server. Every pack and token here is synthetic."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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
    """A tiny IC stand-in: per-path status, body and delay; records request headers."""

    def __init__(self):
        self.routes = {}
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                fake.requests.append((self.path, dict(self.headers)))
                status, body, delay = fake.routes.get(self.path, (404, b'{"detail":"nf"}', 0))
                if delay:
                    time.sleep(delay)
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
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

    def route(self, path, status=200, body=None, delay=0.0):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.routes[path] = (status, raw, delay)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake_ic():
    server = FakeIc()
    yield server
    server.close()


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
    def slow_connect(request, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    monkeypatch.setattr(urllib.request, "urlopen", slow_connect)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "(timeout)" in captured.err
    _assert_one_redacted_line(captured, env)


def test_urlopen_gets_an_explicit_timeout(env, fake_ic, monkeypatch):
    seen = {}
    real = urllib.request.urlopen

    def spy(request, timeout=None, **kwargs):
        seen["timeout"] = timeout
        return real(request, timeout=timeout, **kwargs)

    monkeypatch.setattr(urllib.request, "urlopen", spy)
    fake_ic.route(ic.CONTEXT_PACK_PATH, body=SYNTHETIC_PACK)
    assert main(["ic", "pull"]) == 0
    assert seen["timeout"] == ic.TIMEOUT_SECONDS


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

    def boom(request, timeout=None):
        raise RuntimeError(f"wire trace: {request.get_header('Authorization')}")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert ic.REDACTED in captured.err
    _assert_one_redacted_line(captured, env, token)


def test_error_from_get_json_is_redacted_and_unchained(monkeypatch):
    def boom(request, timeout=None):
        raise RuntimeError(f"echo {PLAIN_TOKEN}")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(ic.IcError) as exc:
        ic.get_json("http://127.0.0.1:9", "/x", ic._Token(PLAIN_TOKEN))
    assert PLAIN_TOKEN not in str(exc.value)
    assert exc.value.__suppress_context__ is True
    assert exc.value.__cause__ is None


def test_token_repr_and_str_are_redacted():
    token = ic._Token(TOKEN)
    assert TOKEN not in repr(token)
    assert TOKEN not in str(token)
    assert TOKEN not in repr({"token": token})


def test_redact_hides_token_shaped_strings():
    assert ic.redact("a ict_OTHER0value b") == f"a {ic.REDACTED} b"


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


def test_bad_base_url_error_is_redacted(env, capsys, monkeypatch):
    monkeypatch.setenv(ic.BASE_URL_ENV, f"ftp://example.invalid/{TOKEN}")
    assert main(["ic", "pull"]) == 1
    captured = capsys.readouterr()
    assert "must start with http://" in captured.err
    _assert_one_redacted_line(captured, env)
