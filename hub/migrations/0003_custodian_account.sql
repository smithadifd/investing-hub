-- Add account column to custodian_snapshots so snapshots from one custodian can be told apart.
-- Holds the account label given at import (or derived from the drop layout), or NULL when none.

ALTER TABLE custodian_snapshots ADD COLUMN account TEXT;
