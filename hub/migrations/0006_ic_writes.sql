-- Audit log of writes the hub makes straight to Investing Companion (`hub ic ...` verbs).
-- One row per applied write, and one per revert. JSON columns hold JSON text (NULL = none).
-- `before`/`after` are the resource state read around the write; `request` is the body sent.
-- `reverted_by` points at the `ic_writes` row of the revert that undid this one.

CREATE TABLE ic_writes (
    id            INTEGER PRIMARY KEY,
    at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    action        TEXT NOT NULL,
    target        TEXT NOT NULL,
    method        TEXT NOT NULL,
    path          TEXT NOT NULL,
    request       TEXT,
    before        TEXT,
    after         TEXT,
    ic_id         TEXT,
    receipt_id    TEXT,
    receipt_error TEXT,
    source_ref    TEXT,
    reverted_by   INTEGER REFERENCES ic_writes (id)
);

CREATE INDEX ic_writes_at ON ic_writes (at);
