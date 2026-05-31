-- ============================================================
-- delta 002 · media (chat attachments)
-- ============================================================
-- Idempotent delta for the already-initialized dev volume. schema.sql is
-- canonical for a fresh `down -v`; the filename sorts after "schema.sql"
-- ('.' < '_') so on a fresh init this runs second as a no-op.
--
-- Apply to the live volume:
--   docker compose cp migrations/schema_002_media.sql postgres:/tmp/m.sql
--   docker compose exec postgres psql -U assistant -d assistant -f /tmp/m.sql
-- ============================================================

CREATE TABLE IF NOT EXISTS media (
    media_id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id         UUID         NOT NULL REFERENCES family(family_id),
    member_id         UUID         NOT NULL REFERENCES member(member_id),
    chat_session_id   UUID         REFERENCES chat_session(chat_session_id),
    kind              TEXT         NOT NULL,
    mime              TEXT         NOT NULL,
    size_bytes        INTEGER      NOT NULL,
    original_filename TEXT,
    storage_path      TEXT         NOT NULL,
    sha256            TEXT,
    created_ts        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    deleted_ts        TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_media_family
    ON media (family_id, created_ts DESC) WHERE deleted_ts IS NULL;
CREATE INDEX IF NOT EXISTS idx_media_session ON media (chat_session_id);
