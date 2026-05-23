-- ============================================================
-- 006 — proactive notifications (Observer agent)
-- ============================================================
-- Two tables + one column on family_member.
--
-- `notification` is a queue: generators write rows with a dedupe_key, the
-- dispatcher picks unsent rows and marks sent_ts. Dedupe via UNIQUE on
-- dedupe_key means re-running a generator is idempotent — at most one alert
-- per (kind, entity, period).
--
-- `notification_preference` is per-member opt-out by kind. Defaults to TRUE
-- (everything enabled); seeded for any member that has a Telegram bind.
--
-- Apply manually (DB already exists, this is not a first-volume-init):
--   cat migrations/006_notifications.sql | \
--     docker exec -i household_db psql -U household -d household
-- ============================================================

-- Persist the Telegram chat_id captured at /start. In 1-to-1 chats it equals
-- telegram_user_id, but storing it explicitly lets us send proactive messages
-- without re-parsing payloads and handles group chats correctly down the line.
ALTER TABLE family_member
    ADD COLUMN IF NOT EXISTS telegram_chat_id BIGINT;


CREATE TABLE IF NOT EXISTS notification (
    notification_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_chat_id       BIGINT       NOT NULL,
    kind                 TEXT         NOT NULL,
    title                TEXT         NOT NULL,
    body                 TEXT         NOT NULL,
    scheduled_ts         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    sent_ts              TIMESTAMPTZ,
    error                TEXT,
    related_entity_type  TEXT,
    related_entity_id    UUID,
    dedupe_key           TEXT UNIQUE,
    created_ts           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- Dispatcher only ever scans pending rows; partial index keeps it cheap.
CREATE INDEX IF NOT EXISTS idx_notification_pending
    ON notification (scheduled_ts)
    WHERE sent_ts IS NULL;

CREATE INDEX IF NOT EXISTS idx_notification_kind ON notification (kind);


CREATE TABLE IF NOT EXISTS notification_preference (
    notification_preference_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    family_member_id   UUID    NOT NULL REFERENCES family_member(family_member_id),
    kind               TEXT    NOT NULL,
    enabled            BOOLEAN NOT NULL DEFAULT TRUE,
    created_ts         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_ts         TIMESTAMPTZ,
    UNIQUE (family_member_id, kind)
);


-- Seed preferences for every kind, for every member already bound to Telegram.
-- Re-runnable: ON CONFLICT skips dupes if a kind is added later.
INSERT INTO notification_preference (family_member_id, kind, enabled)
SELECT fm.family_member_id, k.kind, TRUE
FROM family_member fm
CROSS JOIN (VALUES
    ('budget_80'),
    ('budget_100'),
    ('recurring_due_3d'),
    ('recurring_due_today'),
    ('recurring_overdue_1d'),
    ('recurring_overdue_7d'),
    ('weekly_summary'),
    ('inactivity'),
    ('unusual_tx')
) AS k(kind)
WHERE fm.telegram_user_id IS NOT NULL
ON CONFLICT (family_member_id, kind) DO NOTHING;
