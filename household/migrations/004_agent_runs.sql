-- ============================================================
-- household — 004_agent_runs.sql
-- Observability for the expense-registrar agent: one row per user
-- message run through run_agent(). Written fire-and-forget (after the
-- reply is sent) so logging never adds latency.
--
-- tool_calls is the list of tools the agent fired during the run
-- (e.g. [{"name":"add_transaction","args":{...}}]); an empty list [] means
-- it answered directly without calling any tool.
--
-- NOTE: this folder only auto-runs on a fresh Postgres volume. On the
-- live DB, apply it by hand:  psql "$DATABASE_URL" -f migrations/004_agent_runs.sql
-- Idempotent: safe to re-run.
-- ============================================================

CREATE TABLE IF NOT EXISTS agent_run (
    agent_run_id  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id    TEXT,
    input_text    TEXT,
    reply_text    TEXT,
    model_used    TEXT,                              -- model that produced the reply (after rotation)
    tool_calls    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    error         TEXT,                              -- NULL on success
    created_ts    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_run_created
    ON agent_run (created_ts DESC);
