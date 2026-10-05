-- File identity for the triage read cursor. ``file_identity`` is a content hash of
-- the file's first bytes; a rotated or rewritten queue file (new inode, or an
-- in-place rewrite that keeps the same size) gets a different identity and the
-- cursor restarts from zero rather than resuming mid-record. Rows written before
-- this migration carry NULL, which reads as "identity unknown" and restarts once.

ALTER TABLE producer_cursors ADD COLUMN file_identity TEXT;
