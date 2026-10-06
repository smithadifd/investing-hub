-- The approval-gated handoff flow is retired: the room writes to IC directly (`hub ic ...`),
-- and every write is logged in `ic_writes`. The `handoffs` table and its index stay exactly
-- as they are so existing rows are kept, but nothing reads or writes them any more.
-- Nothing to change in the schema; this migration records the decision in the version history.
SELECT 1;
