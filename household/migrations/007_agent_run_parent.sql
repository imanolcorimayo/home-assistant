-- ============================================================
-- household — 007_agent_run_parent.sql
-- Add parent_run_id to agent_run so we can link a subagent run
-- (Registrador / Consultor) to the Director run that triggered
-- it. NULL for any standalone run (the 3 existing bots keep
-- writing rows with parent_run_id NULL).
--
-- Apply by hand on an existing DB (the migrations dir only auto-runs
-- on a fresh volume):
--   docker compose exec -T postgres psql -U household -d household \
--     -f /docker-entrypoint-initdb.d/007_agent_run_parent.sql
-- Idempotent: safe to re-run.
-- ============================================================

ALTER TABLE agent_run
    ADD COLUMN IF NOT EXISTS parent_run_id UUID
        REFERENCES agent_run(agent_run_id);

-- Partial index: only the routed runs (most rows will be NULL).
-- Speeds up the dashboard query that joins child→parent.
CREATE INDEX IF NOT EXISTS agent_run_parent_run_id_idx
    ON agent_run(parent_run_id)
    WHERE parent_run_id IS NOT NULL;
