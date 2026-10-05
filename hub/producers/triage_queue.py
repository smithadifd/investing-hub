"""triage-queue adapter — read-only against ``<queue_dir>/*.jsonl``.

The morning brief writes one ``<account>.jsonl`` per inbox with one JSON object
per ``[[mode]]`` newsletter routing; ``append_records`` dedups by ``message_id``
so a record never appears twice in the file. The hub's contract with the queue
is read-only: we never modify, move or truncate the jsonl files, and a small
smoke test in ``tests/test_producers.py`` asserts those bytes and mtime are
unchanged after a read.

The cursor is keyed by the absolute source path and stores the byte size at the
last successful read; when the file grows, only the new tail is parsed. If the
file shrinks (truncated or rolled over) the cursor resets to zero — that's a
real signal, not a regression.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hub.producers.common import (
    DEFAULT_TRIAGE_QUEUE_DIR,
    STATUS_ABSENT,
    STATUS_EMPTY,
    STATUS_OK,
    TRIAGE_QUEUE,
    Candidate,
    ProducerReport,
)


def triage_queue_candidates(
    queue_dir: Path | str | None = None,
    *,
    conn: sqlite3.Connection | None = None,
) -> ProducerReport:
    """Read ``<queue_dir>/*.jsonl`` records, honouring the hub-store read cursor.

    A second read without new lines yields no repeat candidates. The adapter
    never opens the jsonl files for writing, even when ``conn`` is None — the
    cursor is only updated when ``conn`` is provided.
    """
    base = Path(queue_dir) if queue_dir is not None else DEFAULT_TRIAGE_QUEUE_DIR
    if not base.is_dir():
        return ProducerReport(producer=TRIAGE_QUEUE, candidates=[], status=STATUS_ABSENT)
    jsonl_files = sorted(p for p in base.iterdir() if p.suffix == ".jsonl" and p.is_file())
    if not jsonl_files:
        return ProducerReport(producer=TRIAGE_QUEUE, candidates=[], status=STATUS_EMPTY)
    candidates: list[Candidate] = []
    for path in jsonl_files:
        candidates.extend(_browse_one(path, conn))
    status = STATUS_EMPTY if not candidates else STATUS_OK
    return ProducerReport(producer=TRIAGE_QUEUE, candidates=candidates, status=status)


def _browse_one(path: Path, conn: sqlite3.Connection | None) -> list[Candidate]:
    """One jsonl file's worth of new candidates, cursor-aware.

    The cursor is the byte size of the file at the last successful read; on a
    subsequent call, lines whose byte offset is above the stored size are not
    re-counted. If the file shrank (someone truncated and started fresh), we
    reset the cursor to zero — that's a real signal, not a regression.
    """
    stat = path.stat()
    size = stat.st_size
    mtime = stat.st_mtime_ns
    previous_size = _read_cursor(conn, str(path.resolve())) if conn is not None else None
    out: list[Candidate] = []
    if size == 0:
        if conn is not None:
            _write_cursor(conn, str(path.resolve()), 0, file_size=0, file_mtime=mtime)
        return out
    # Decide what to read: full file (no cursor yet, or file shrank) vs. tail
    # (file grew since last read) vs. nothing (file unchanged).
    if previous_size is None or previous_size > size:
        tail_offset = 0
    elif previous_size < size:
        tail_offset = previous_size
    else:
        tail_offset = size  # signal: nothing new
    with open(path, "rb") as fh:
        if tail_offset < size:
            fh.seek(tail_offset)
            tail = fh.read()
        else:
            tail = b""
    for raw in tail.splitlines():
        if not raw.strip():
            continue
        try:
            rec = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        queued = str(rec.get("queued", "")).strip()
        account = str(rec.get("account", "")).strip()
        subject = str(rec.get("subject", "")).strip()
        if not queued:
            continue
        summary = f"{account} — {subject}".strip(" —") or account or subject
        out.append(
            Candidate(
                producer=TRIAGE_QUEUE,
                kind="triage_record",
                summary=summary[:200] + ("…" if len(summary) > 200 else ""),
                source_path=str(path.resolve()),
                as_of=queued,
            )
        )
    if conn is not None:
        _write_cursor(conn, str(path.resolve()), size, file_size=size, file_mtime=mtime)
    return out


def _read_cursor(conn: sqlite3.Connection, source_path: str) -> int | None:
    row = conn.execute(
        "SELECT last_read_size FROM producer_cursors WHERE source_path = ?",
        (source_path,),
    ).fetchone()
    return None if row is None else int(row[0])


def _write_cursor(
    conn: sqlite3.Connection,
    source_path: str,
    last_read_size: int,
    *,
    file_size: int,
    file_mtime: int,
) -> None:
    conn.execute(
        "INSERT INTO producer_cursors (source_path, last_read_size, file_size, file_mtime)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(source_path) DO UPDATE SET"
        " last_read_size = excluded.last_read_size,"
        " file_size = excluded.file_size,"
        " file_mtime = excluded.file_mtime",
        (source_path, last_read_size, file_size, file_mtime),
    )
