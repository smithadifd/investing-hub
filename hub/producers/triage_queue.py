"""triage-queue adapter — read-only against ``<queue_dir>/*.jsonl``.

The morning brief writes one ``<account>.jsonl`` per inbox with one JSON object
per ``[[mode]]`` newsletter routing; ``append_records`` dedups by ``message_id``
so a record never appears twice in the file. The hub's contract with the queue
is read-only: we never modify, move or truncate the jsonl files, and a smoke
test in ``tests/test_producers.py`` asserts those bytes and mtime are unchanged
after a read.

The cursor is keyed by the absolute source path and stores three things: the
byte offset of the last complete newline consumed, the file's size, and a
content identity — a hash of the file's first bytes. Only complete lines are
consumed, so a final line still being appended is read once, when its newline
lands. If the identity changes (the file was rotated or rewritten, even to the
same size) or the file shrank below the offset (truncated), the read restarts
from zero — that is a real signal, not a regression.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import BinaryIO

from hub.producers.common import (
    DEFAULT_TRIAGE_QUEUE_DIR,
    STATUS_ABSENT,
    STATUS_EMPTY,
    STATUS_OK,
    TRIAGE_QUEUE,
    Candidate,
    ProducerReport,
    truncate,
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

    The cursor holds the byte offset of the last complete newline consumed plus
    the file's content identity. The offset never advances past an incomplete
    final line, so a record being appended right now is read exactly once, when
    its newline lands. A different identity (rotation, or an in-place rewrite
    that keeps the same size) or a size below the offset (truncation) restarts
    the file from zero.
    """
    stat = path.stat()
    size = stat.st_size
    mtime = stat.st_mtime_ns
    key = str(path.resolve())
    with open(path, "rb") as fh:
        identity = _file_identity(fh)
        cursor = _read_cursor(conn, key) if conn is not None else None
        if cursor is None or cursor[0] > size or cursor[1] != identity:
            offset = 0
        else:
            offset = cursor[0]
        fh.seek(offset)
        tail = fh.read()
    # Only complete lines count: a final line without its newline is a record
    # still being written, and it is read next time once it completes.
    last_newline = tail.rfind(b"\n")
    complete = b"" if last_newline < 0 else tail[: last_newline + 1]
    out = _parse_records(complete, path)
    if conn is not None:
        _write_cursor(
            conn,
            key,
            offset + len(complete),
            file_size=size,
            file_mtime=mtime,
            file_identity=identity,
        )
    return out


def _file_identity(fh: BinaryIO) -> str:
    """A hash of the file's first line — its identity across appends and rotations.

    The first line, not the first N bytes: an append grows the file, and a
    fixed-size window eventually swallows the appended records, which would
    flip the identity on every append and replay the whole file. The first
    line is stable under appends and changes on any real rotation; a rotation
    that reproduces the first line byte-for-byte is indistinguishable on purpose.
    """
    fh.seek(0)
    return hashlib.sha256(fh.readline()).hexdigest()


def _parse_records(data: bytes, path: Path) -> list[Candidate]:
    """One candidate per complete JSON line in ``data``."""
    out: list[Candidate] = []
    for raw in data.splitlines():
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
                summary=truncate(summary),
                source_path=str(path.resolve()),
                as_of=queued,
            )
        )
    return out


def _read_cursor(conn: sqlite3.Connection, source_path: str) -> tuple[int, str | None] | None:
    """The stored ``(offset, identity)`` for one source file, or None."""
    row = conn.execute(
        "SELECT last_read_size, file_identity FROM producer_cursors WHERE source_path = ?",
        (source_path,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), row[1]


def _write_cursor(
    conn: sqlite3.Connection,
    source_path: str,
    last_read_size: int,
    *,
    file_size: int,
    file_mtime: int,
    file_identity: str,
) -> None:
    conn.execute(
        "INSERT INTO producer_cursors"
        " (source_path, last_read_size, file_size, file_mtime, file_identity)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(source_path) DO UPDATE SET"
        " last_read_size = excluded.last_read_size,"
        " file_size = excluded.file_size,"
        " file_mtime = excluded.file_mtime,"
        " file_identity = excluded.file_identity",
        (source_path, last_read_size, file_size, file_mtime, file_identity),
    )
