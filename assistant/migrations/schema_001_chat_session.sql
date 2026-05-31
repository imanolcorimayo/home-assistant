-- ============================================================
-- delta 001 · chat_session + agent_run.chat_session_id
-- ============================================================
-- Adds the per-conversation thread table and repoints agent_run at it
-- (was a free-text session_id == member_id; now a real FK to a thread).
--
-- This is a DELTA for the ALREADY-INITIALIZED dev volume: schema.sql is the
-- canonical schema and already contains all of this for a fresh `down -v`
-- rebuild. The filename sorts AFTER "schema.sql" ('.' < '_'), so on a fresh
-- init this runs second and every statement here is a guarded no-op.
--
-- Apply to the live volume (no rebuild, keeps existing data):
--   docker compose cp migrations/schema_001_chat_session.sql postgres:/tmp/m.sql
--   docker compose exec postgres psql -U assistant -d assistant -f /tmp/m.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS chat_session (
    chat_session_id UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id       UUID         NOT NULL REFERENCES family(family_id),
    member_id       UUID         NOT NULL REFERENCES member(member_id),
    title           TEXT,
    created_ts      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_ts      TIMESTAMPTZ
);

DROP TRIGGER IF EXISTS trg_chat_session_updated_ts ON chat_session;
CREATE TRIGGER trg_chat_session_updated_ts
    BEFORE UPDATE ON chat_session
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX IF NOT EXISTS idx_chat_session_member
    ON chat_session (family_id, member_id, created_ts DESC);

-- Repoint agent_run. Old test rows kept the member_id in session_id (a TEXT
-- col); we just drop it — those runs become unlinked history, no data we need.
ALTER TABLE agent_run DROP COLUMN IF EXISTS session_id;
ALTER TABLE agent_run
    ADD COLUMN IF NOT EXISTS chat_session_id UUID REFERENCES chat_session(chat_session_id);

CREATE INDEX IF NOT EXISTS idx_agent_run_session
    ON agent_run (chat_session_id, created_ts);
