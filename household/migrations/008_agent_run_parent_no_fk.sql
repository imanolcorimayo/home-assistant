-- ============================================================
-- household — 008_agent_run_parent_no_fk.sql
-- Drop the FK constraint on agent_run.parent_run_id.
--
-- Why: parent and child rows are persisted by two separate
-- asyncio.create_task fire-and-forget jobs (each with its own
-- AsyncSession / transaction). There is no ordering guarantee,
-- and a race where the child INSERT lands before the parent's
-- triggers a ForeignKeyViolationError. We kept the column +
-- partial index (the dashboard's LEFT JOIN still works); we
-- only drop the FK enforcement.
--
-- Apply by hand on an existing DB:
--   docker compose exec -T postgres psql -U household -d household \
--     -f /docker-entrypoint-initdb.d/008_agent_run_parent_no_fk.sql
-- Idempotent.
-- ============================================================

ALTER TABLE agent_run
    DROP CONSTRAINT IF EXISTS agent_run_parent_run_id_fkey;
