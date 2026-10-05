"""triage-queue adapter — read-only against ``<queue_dir>/*.jsonl``.

The morning brief writes one ``<account>.jsonl`` per inbox with one JSON object
per ``[[mode]]`` newsletter routing; ``append_records`` dedups by ``message_id``
so a record never appears twice in the file. The hub's contract with the queue
is read-only: we never modify, move or truncate the jsonl files, and a smoke
test in ``tests/test_producers.py`` asserts those bytes and mtime are unchanged
after a read.

The cursor is keyed by the absolute source path and stores four things: the
byte offset of the last complete newline consumed, the file's size, its mtime,
and a content identity — the file's inode and device plus a hash of its first
line. Only complete lines are consumed, so a final line still being appended
is read once, when its newline lands. The file is treated as replaced — and
the read restarts from zero — when the identity changes (a rotation, or any
rewrite that moved the file), when the file shrinks below the offset
(truncation), or when the size is unchanged but the mtime moved (an in-place
rewrite that kept line 1). A pure append — same inode, size grown, line 1
untouched — resumes from the offset, exactly as before.
"""

from __future__ import annotations

import hashlib
import json
import os
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
    the file's identity. The offset never advances past an incomplete final
    line, so a record being appended right now is read exactly once, when its
    newline lands. A replaced file (a rotation, a truncation below the offset,
    or an in-place rewrite that kept line 1 and the size) restarts from zero.
    """
    stat = path.stat()
    size = stat.st_size
    mtime = stat.st_mtime_ns
    key = str(path.resolve())
    with open(path, "rb") as fh:
        identity = _file_identity(fh, stat)
        cursor = _read_cursor(conn, key) if conn is not None else None
        offset = 0 if _is_replacement(cursor, size, mtime, identity) else cursor[0]
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


def _is_replacement(
    cursor: tuple[int, str | None, int, int] | None,
    size: int,
    mtime: int,
    identity: str,
) -> bool:
    """True when the stored cursor cannot be trusted to resume from its offset.

    Three replacement signals, each covering a case the others cannot see:

    * the offset is past the end of the file — it was truncated or rolled over
      to something smaller, so the offset points into a different file's bytes;
    * the identity moved — a rotation or any rewrite that replaced the file
      (new inode), or one that changed line 1;
    * the size is unchanged but the mtime moved — an in-place rewrite that kept
      line 1 and the byte count; the first-line hash alone cannot see it, which
      is exactly the rewrite a positions board or a queue editor produces.
    """
    if cursor is None:
        return True
    offset, stored_identity, stored_size, stored_mtime = cursor
    if offset > size:
        return True
    if stored_identity != identity:
        return True
    return size == stored_size and mtime != stored_mtime


def _file_identity(fh: BinaryIO, stat: os.stat_result) -> str:
    """The file's inode and device, plus a hash of its first line.

    The first line, not the first N bytes: an append grows the file, and a
    fixed-size window eventually swallows the appended records, which would
    flip the identity on every append and replay the whole file. The inode and
    device separate a rewrite that replaced the file from an append to it even
    when line 1 survives byte-for-byte; the same-size, same-inode rewrite is
    left to the mtime check in ``_is_replacement``.
    """
    fh.seek(0)
    return f"{stat.st_dev}:{stat.st_ino}:{hashlib.sha256(fh.readline()).hexdigest()}"


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


def _read_cursor(
    conn: sqlite3.Connection, source_path: str
) -> tuple[int, str | None, int, int] | None:
    """The stored ``(offset, identity, size, mtime)`` for one source file, or None."""
    row = conn.execute(
        "SELECT last_read_size, file_identity, file_size, file_mtime"
        " FROM producer_cursors WHERE source_path = ?",
        (source_path,),
    ).fetchone()
    if row is None:
        return None
    return int(row[0]), row[1], int(row[2]), int(row[3])


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
