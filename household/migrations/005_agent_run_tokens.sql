-- ============================================================
-- household — 005_agent_run_tokens.sql
-- Token usage per agent run, so we can total "cost" per day.
-- Pulled from the Gemini response's usage_metadata; NULL when the
-- run errored before a response (no usage available).
--
-- Apply by hand on an existing DB (the migrations dir only auto-runs
-- on a fresh volume):
--   docker compose exec -T postgres psql -U household -d household \
--     -f /docker-entrypoint-initdb.d/005_agent_run_tokens.sql
-- Idempotent: safe to re-run.
-- ============================================================

ALTER TABLE agent_run
    ADD COLUMN IF NOT EXISTS prompt_tokens  INTEGER,
    ADD COLUMN IF NOT EXISTS output_tokens  INTEGER,
    ADD COLUMN IF NOT EXISTS total_tokens   INTEGER;
