-- Initial schema: the tables named in ROADMAP.md § Data model.
-- Timestamps are ISO-8601 UTC text. JSON columns hold JSON text.

CREATE TABLE documents (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    kind       TEXT NOT NULL,
    title      TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE document_revisions (
    id          INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES documents (id),
    revision    INTEGER NOT NULL CHECK (revision >= 1),
    body        TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('receipt', 'digest', 'session', 'import')),
    source_ref  TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (document_id, revision)
);

CREATE TABLE custodian_snapshots (
    id          INTEGER PRIMARY KEY,
    custodian   TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('positions', 'transactions')),
    as_of       TEXT NOT NULL,
    source_ref  TEXT NOT NULL,
    raw         TEXT NOT NULL,
    imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE findings (
    id         INTEGER PRIMARY KEY,
    kind       TEXT NOT NULL,
    subject    TEXT NOT NULL,
    score      REAL NOT NULL,
    evidence   TEXT NOT NULL DEFAULT '{}',
    status     TEXT NOT NULL DEFAULT 'new',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE asks (
    id             INTEGER PRIMARY KEY,
    finding_id     INTEGER REFERENCES findings (id),
    prompt         TEXT NOT NULL,
    options        TEXT NOT NULL DEFAULT '[]',
    default_option TEXT,
    deadline       TEXT,
    answer         TEXT,
    answered_at    TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE briefs (
    id         INTEGER PRIMARY KEY,
    finding_id INTEGER REFERENCES findings (id),
    ask_id     INTEGER REFERENCES asks (id),
    body       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'pending'
               CHECK (status IN ('pending', 'accepted', 'consumed')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT
);

CREATE TABLE handoffs (
    id             INTEGER PRIMARY KEY,
    body           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'approved', 'applied')),
    ic_receipt_ref TEXT,
    created_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    approved_at    TEXT,
    applied_at     TEXT
);

CREATE TABLE decisions (
    id         INTEGER PRIMARY KEY,
    subject    TEXT NOT NULL,
    decision   TEXT NOT NULL,
    rationale  TEXT,
    links      TEXT NOT NULL DEFAULT '[]',
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE calls (
    id          INTEGER PRIMARY KEY,
    subject     TEXT NOT NULL,
    call        TEXT NOT NULL,
    called_at   TEXT NOT NULL,
    resolves_by TEXT,
    resolution  TEXT,
    resolved_at TEXT
);

CREATE TABLE beats_proposals (
    id         INTEGER PRIMARY KEY,
    proposal   TEXT NOT NULL,
    rationale  TEXT,
    status     TEXT NOT NULL DEFAULT 'proposed',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX document_revisions_document ON document_revisions (document_id);
CREATE INDEX findings_status ON findings (status);
CREATE INDEX briefs_status ON briefs (status);
CREATE INDEX handoffs_status ON handoffs (status);
