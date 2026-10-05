"""session-open producer-line integration tests.

Every store row, pack and token here is synthetic; IC is mocked at the ``hub.ic``
seam (``fetch_contract_docs``), never over the network.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from hub import ic, session_open, store
from hub.cli import main
from hub.producers.common import STATUS_ABSENT, ProducerReport

TOKEN = "ict_SYNTHETIC0test0token0value0000"
ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures" / "producers"


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat(timespec="seconds")


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ic.TOKEN_ENV, TOKEN)
    monkeypatch.setenv(ic.BASE_URL_ENV, "http://ic.invalid:8000")
    conn = store.connect(tmp_path / "data" / "hub.db")
    store.migrate(conn)
    conn.close()
    meta = {
        "fetched_at": _iso(timedelta(hours=5)),
        "generated_at": "2026-01-02T03:04:05Z",
        "schema_version": "1.7",
        "advisor_actions_version": "1.4",
    }
    (tmp_path / "data" / "ic").mkdir()
    (tmp_path / "data" / "ic" / "pack-meta.json").write_text(json.dumps(meta))
    monkeypatch.setattr(ic, "fetch_contract_docs", lambda base, token, timeout=None: [])
    return tmp_path


def _build(env, **producer_kwargs):
    """Build the session-open block with explicit producer roots."""
    kwargs = {
        "mv_analyst_root": FIXTURES / "mv-analyst",
        "week_ahead_root": FIXTURES / "week-ahead",
        "triage_queue_dir": FIXTURES / "triage-queue",
    }
    kwargs.update(producer_kwargs)
    return session_open.build_block(env / "data" / "hub.db", **kwargs)


def test_session_open_prints_one_line_per_producer(env):
    block = _build(env)
    lines = [ln for ln in block.splitlines() if ln.startswith("producer ")]
    assert [ln.split(":", 1)[0] for ln in lines] == [
        "producer mv-analyst",
        "producer week-ahead",
        "producer triage-queue",
    ]
    assert "producer mv-analyst: ok" in block
    assert "producer week-ahead: ok" in block
    assert "producer triage-queue: ok" in block


def test_session_open_reports_missing_producers_as_absent(env):
    block = _build(
        env,
        mv_analyst_root=FIXTURES / "nope-mv",
        week_ahead_root=FIXTURES / "nope-wa",
        triage_queue_dir=FIXTURES / "nope-tq",
    )
    assert "producer mv-analyst: absent — 0 candidate" in block
    assert "producer week-ahead: absent — 0 candidate" in block
    assert "producer triage-queue: absent — 0 candidate" in block


def test_session_open_producer_section_is_guarded(env, monkeypatch):
    """An exception in any producer adapter becomes one WARN line; exit 0 still."""

    def explode(*args, **kwargs):
        raise RuntimeError("producer blew up")

    monkeypatch.setattr("hub.producers.mv_analyst_candidates", explode)
    block = _build(env)
    assert "WARN producers: unexpected RuntimeError" in block
    # Block still printed.
    assert "== hub session-open ==" in block


def test_session_open_cli_keeps_exit_zero(env, capsys, monkeypatch):
    """The CLI never raises on producer issues; the public override knobs work too."""
    monkeypatch.setattr("hub.producers.triage_queue_candidates", lambda *a, **kw: producers_obj())
    rc = main(["session-open"])
    assert rc == 0


def producers_obj():
    return ProducerReport(producer="triage-queue", candidates=[], status=STATUS_ABSENT)


def test_session_open_still_exits_zero_with_no_producer_root_overrides(env, capsys):
    """The default roots (~/mv-analyst etc.) do not have to exist for exit 0."""
    rc = main(["session-open"])
    assert rc == 0
    out = capsys.readouterr().out
    # The producer line is present regardless of whether the home paths exist.
    assert "producer mv-analyst:" in out
    assert "producer week-ahead:" in out
    assert "producer triage-queue:" in out
