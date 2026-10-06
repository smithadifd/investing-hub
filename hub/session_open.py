"""The session-open status block: pack age, contract drift, open work and stale documents.

Everything here is read-only and best effort: a problem in any section becomes a ``WARN``
line, never an exception, so the caller can always print the block and exit 0.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from hub import ic, ic_writes, producers, store
from hub.producers.common import truncate

DEFAULT_STALE_DAYS = 30
CONFIG_SECTION = "session_open"
CONFIG_STALE_KEY = "stale_days"
# Which contract doc carries which pack version (IC's contract-docs response: the handoff
# schema doc is stamped with schema_version, the advisor-actions doc with its own version).
DOC_VERSION_KEY = {"handoff_schema": "schema_version", "advisor_actions": "advisor_actions_version"}
_TITLE_WIDTH = 72
FETCH_TIMEOUT_SECONDS = 5.0  # a hung IC must not hold a session open


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


LOCALTIME = Path("/etc/localtime")


def _local_zone(environ: Mapping[str, str], localtime: Path) -> tuple[tzinfo | None, str | None]:
    """The operator's zone and its IANA name, from ``TZ`` or, with ``TZ`` unset, /etc/localtime.

    ``(None, None)`` means "use the system local zone, and do not name it": the name is only
    given when it is confirmed by loading it through ``ZoneInfo``.
    """
    tz = environ.get("TZ", "").strip().lstrip(":")
    if environ.get("TZ", "").strip():
        try:
            return ZoneInfo(tz), tz
        except Exception:  # unknown or malformed TZ: fall back to the system zone, unnamed
            return None, None
    try:
        resolved = str(localtime.resolve())
        marker = "zoneinfo/"
        name = resolved[resolved.index(marker) + len(marker) :]
        return ZoneInfo(name), name
    except Exception:  # no symlink into a zoneinfo tree, or not a loadable zone
        return None, None


def local_now_line(
    now: datetime,
    environ: Mapping[str, str] | None = None,
    localtime: Path = LOCALTIME,
) -> str:
    """``now: Tue 2026-10-06 20:04 EDT (America/New_York)``: local weekday, date and time."""
    zone, name = _local_zone(os.environ if environ is None else environ, localtime)
    local = now.astimezone(zone) if zone is not None else now.astimezone()
    stamp = local.strftime("%a %Y-%m-%d %H:%M ") + (local.tzname() or local.strftime("%z"))
    return f"now: {stamp}" + (f" ({name})" if name else "")


def _age(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    days, rest = divmod(seconds, 86400)
    if days:
        return f"{days}d {rest // 3600}h"
    return f"{rest // 3600}h {rest % 3600 // 60}m"


def _first_line(text: str) -> str:
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line if len(line) <= _TITLE_WIDTH else line[: _TITLE_WIDTH - 3] + "..."


def resolve_stale_days(flag: int | None, config_path: Path = ic.CONFIG_FILE) -> tuple[int, str]:
    """Flag wins, then ``session_open.stale_days`` in config.yaml, then the default.

    Returns the value and, when the config value is unusable, a warning text (else "").
    """
    if flag is not None:
        return flag, ""
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except OSError:
        return DEFAULT_STALE_DAYS, ""
    except (UnicodeDecodeError, yaml.YAMLError):
        return DEFAULT_STALE_DAYS, (
            f"{config_path} cannot be decoded; ignoring {CONFIG_SECTION}.{CONFIG_STALE_KEY}"
            f" and using {DEFAULT_STALE_DAYS}"
        )
    section = data.get(CONFIG_SECTION) if isinstance(data, dict) else None
    if not isinstance(section, dict) or CONFIG_STALE_KEY not in section:
        return DEFAULT_STALE_DAYS, ""
    value = section[CONFIG_STALE_KEY]
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value, ""
    return DEFAULT_STALE_DAYS, (
        f"{CONFIG_SECTION}.{CONFIG_STALE_KEY} in {config_path} must be a non-negative integer;"
        f" using {DEFAULT_STALE_DAYS}"
    )


@contextmanager
def _open_readonly(db: Path) -> Iterator[sqlite3.Connection]:
    """Open without creating or changing any file next to the database.

    A WAL database opened even read-only creates ``-wal`` and ``-shm`` files. When no ``-wal``
    exists (the usual case after a clean close) there is no WAL content to miss, so the file
    is opened ``immutable`` in place. When one exists (a writer is live, or it crashed) the
    database and its WAL are copied into a private temporary directory and read there, so
    whatever SQLite creates lands in the copy and is removed with it.
    """
    wal = db.with_name(db.name + "-wal")
    with tempfile.TemporaryDirectory(prefix="hub-session-open-") as scratch:
        if wal.exists():
            copy = Path(scratch) / db.name
            shutil.copyfile(wal, copy.with_name(wal.name))
            shutil.copyfile(db, copy)
            target = copy.resolve().as_uri() + "?mode=ro"
        else:
            target = Path(db).resolve().as_uri() + "?immutable=1"
        conn = sqlite3.connect(target, uri=True)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


def _pack_section(meta_path: Path, now: datetime) -> tuple[list[str], dict | None]:
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"WARN pack: {meta_path} not found (run `hub ic pull`)"], None
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else "not valid JSON"
        return [f"WARN pack: cannot read {meta_path}: {reason}"], None
    if not isinstance(meta, dict):
        return [f"WARN pack: {meta_path} is not a JSON object"], None
    fetched = _parse_time(meta.get("fetched_at"))
    if fetched is None:
        return [f"WARN pack: {meta_path} has no usable fetched_at"], meta
    return [
        f"pack: fetched {meta['fetched_at']} (age {_age(fetched, now)});"
        f" generated {meta.get('generated_at')}; schema {meta.get('schema_version')};"
        f" advisor-actions {meta.get('advisor_actions_version')}"
    ], meta


def _drift_section(meta: dict | None) -> list[str]:
    token = None
    try:
        token = ic.read_token()
        docs = ic.fetch_contract_docs(ic.load_base_url(), token, timeout=FETCH_TIMEOUT_SECONDS)
    except ic.IcError as exc:
        return [f"WARN contract: cannot check drift: {ic.redact(str(exc), token)}"]
    problems = []
    for doc in docs:
        parts = []
        version_key = DOC_VERSION_KEY.get(doc.key)
        pack_version = meta.get(version_key) if meta and version_key else None
        if pack_version is not None and doc.stamp != pack_version:
            parts.append(f"stamp {doc.stamp} != pack {version_key} {pack_version}")
        if not doc.stamp_matches:
            parts.append(f"stamp {doc.stamp} != IC expected_stamp {doc.expected_stamp}")
        if parts:
            problems.append(f"WARN contract drift in {doc.name}: " + "; ".join(parts))
    return problems or ["contract: in sync"]


def _titled(label: str, rows: list[str]) -> list[str]:
    return [f"{label}: {len(rows)}", *(f"  {row}" for row in rows)]


def _briefs_section(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT id, body, created_at FROM briefs WHERE status = 'pending' ORDER BY id"
    ).fetchall()
    return _titled(
        "pending briefs",
        [f"#{r['id']} ({r['created_at']}) {_first_line(r['body'])}" for r in rows],
    )


def _status_note(row: sqlite3.Row, now: datetime) -> str:
    if row["status"] == "applied":
        return ""
    then = _parse_time(row["at"])
    age = f", {_age(then, now)} ago" if then else ""
    return f" [{row['status']}{age}: check IC]" if row["status"] != "failed" else ""


def _ic_writes_section(conn: sqlite3.Connection, now: datetime) -> list[str]:
    rows = ic_writes.recent_writes(conn, now)
    return _titled(
        f"recent IC writes (last {ic_writes.RECENT_HOURS}h)",
        [
            f"#{r['id']} ({r['at']}) {r['action']} {r['target']}" + _status_note(r, now)
            for r in rows
        ],
    )


def _legacy_handoffs_section(conn: sqlite3.Connection) -> list[str]:
    """Rows left in the retired ``handoffs`` table must not vanish silently."""
    try:
        n = conn.execute(
            "SELECT COUNT(*) FROM handoffs WHERE status IN ('draft', 'approved')"
        ).fetchone()[0]
    except sqlite3.Error:
        return []
    if not n:
        return []
    return [f"WARN {n} legacy handoffs unapplied (handoffs retired; apply or ignore)"]


def _stale_section(conn: sqlite3.Connection, now: datetime, stale_days: int) -> list[str]:
    stale = []
    for doc in store.list_documents(conn):
        revised = _parse_time(doc["revised_at"])
        if revised is not None and now - revised > timedelta(days=stale_days):
            stale.append(
                f"{doc['slug']} r{doc['revision']} revised {doc['revised_at']}"
                f" ({(now - revised).days}d ago)"
            )
    return _titled(f"documents older than {stale_days}d", stale)


def _producers_section(
    *,
    mv_analyst_root: Path | None,
    week_ahead_root: Path | None,
    triage_queue_dir: Path | None,
) -> list[str]:
    """One line per producer — name, status, candidate count or 'absent', latest as-of."""
    lines: list[str] = []
    for report in producers.status_reports(
        mv_analyst_root=mv_analyst_root,
        week_ahead_root=week_ahead_root,
        triage_queue_dir=triage_queue_dir,
    ):
        as_of = max((c.as_of for c in report.candidates), default="")
        as_of_text = f" latest {as_of}" if as_of else ""
        count_text = f"{report.count} candidate" if report.count != 1 else "1 candidate"
        notes = f" — {truncate('; '.join(report.notes))}" if report.notes else ""
        lines.append(
            f"producer {report.producer}: {report.status} — {count_text}{as_of_text}{notes}"
        )
    return lines


def _guarded(name: str, section: Callable[[], list[str]]) -> list[str]:
    try:
        return section()
    except Exception as exc:  # a session must open whatever goes wrong here
        return [f"WARN {name}: unexpected {type(exc).__name__}"]


def build_block(
    db: Path,
    stale_days: int | None = None,
    *,
    now: datetime | None = None,
    pack_dir: Path = ic.PACK_DIR,
    config_path: Path = ic.CONFIG_FILE,
    mv_analyst_root: Path | None = None,
    week_ahead_root: Path | None = None,
    triage_queue_dir: Path | None = None,
    environ: Mapping[str, str] | None = None,
    localtime: Path = LOCALTIME,
) -> str:
    now = now or datetime.now(UTC)
    lines = ["== hub session-open =="]
    lines += _guarded("now", lambda: [local_now_line(now, environ, localtime)])
    days, config_warning = resolve_stale_days(stale_days, config_path)
    if config_warning:
        lines.append(f"WARN config: {config_warning}")

    try:
        pack_lines, meta = _pack_section(pack_dir / ic.META_FILE, now)
    except Exception as exc:
        pack_lines, meta = [f"WARN pack: unexpected {type(exc).__name__}"], None
    lines += pack_lines
    lines += _guarded("contract", lambda: _drift_section(meta))

    if not db.is_file():
        lines.append(f"WARN store: database not found at {db} (run `hub db init`)")
    else:
        try:
            with _open_readonly(db) as conn:
                lines += _store_sections(conn, now, days)
        except (sqlite3.Error, OSError) as exc:
            lines.append(f"WARN store: cannot open {db}: {exc}")

    lines += _guarded(
        "producers",
        lambda: _producers_section(
            mv_analyst_root=mv_analyst_root,
            week_ahead_root=week_ahead_root,
            triage_queue_dir=triage_queue_dir,
        ),
    )

    # Last line of defence: nothing token-shaped or equal to the live token leaves this block.
    return ic.redact("\n".join(lines), os.environ.get(ic.TOKEN_ENV, "").strip() or None)


def _store_sections(conn: sqlite3.Connection, now: datetime, days: int) -> list[str]:
    lines: list[str] = []
    for name, section in (
        ("briefs", lambda: _briefs_section(conn)),
        ("ic_writes", lambda: _ic_writes_section(conn, now)),
        ("handoffs", lambda: _legacy_handoffs_section(conn)),
        ("documents", lambda: _stale_section(conn, now, days)),
    ):
        try:
            lines += section()
        except sqlite3.Error as exc:
            lines.append(f"WARN {name}: store query failed: {exc}")
        except Exception as exc:
            lines.append(f"WARN {name}: unexpected {type(exc).__name__}")
    return lines
