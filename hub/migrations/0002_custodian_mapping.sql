-- Add mapping column to custodian_snapshots to persist import-time --map overrides.
-- Holds JSON object of normalized-header -> field overrides, or NULL when none.

ALTER TABLE custodian_snapshots ADD COLUMN mapping TEXT;
