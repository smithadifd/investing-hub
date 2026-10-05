-- Read cursor for the morning-brief triage queue. Each row is one source file the
-- hub is reading; ``last_read_size`` is the byte offset the next read picks up from
-- (zero on first sight, full size on the first read). ``file_size`` and
-- ``file_mtime`` are recorded alongside so a future cursor can detect a file that
-- shrank or rolled over.

CREATE TABLE producer_cursors (
    source_path    TEXT PRIMARY KEY,
    last_read_size INTEGER NOT NULL,
    file_size      INTEGER NOT NULL,
    file_mtime     INTEGER NOT NULL,
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX producer_cursors_updated ON producer_cursors (updated_at);