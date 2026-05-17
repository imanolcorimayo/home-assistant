-- Migración 009: notificaciones proactivas por Telegram + preferencias por usuario.

-- ─────────────────────────────────────────
-- 1. notifications: cola de mensajes a despachar
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS notifications (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_chat_id          BIGINT NOT NULL,
    kind                    TEXT NOT NULL,        -- 'budget'|'reminder'|'monthly_summary'|'anomaly'|'event'|'task'
    title                   TEXT NOT NULL,
    body                    TEXT NOT NULL,
    scheduled_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at                 TIMESTAMPTZ,
    error                   TEXT,                 -- mensaje del último fallo de envío
    related_entity_type     TEXT,                 -- 'transaction'|'loan'|'card_statement'|'event'|...
    related_entity_id       UUID,
    dedupe_key              TEXT UNIQUE,          -- evita duplicados por mismo evento
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_notif_pending
    ON notifications (scheduled_at)
    WHERE sent_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_notif_kind ON notifications(kind);

-- ─────────────────────────────────────────
-- 2. user_preferences: qué tipos de notificación quiere cada miembro
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS user_preferences (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id    BIGINT NOT NULL,
    kind                TEXT NOT NULL,        -- mismo dominio que notifications.kind
    enabled             BOOLEAN NOT NULL DEFAULT TRUE,
    preferred_hour      INT,                  -- 0-23 hora local (Europe/Rome) para envíos diarios
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ,
    UNIQUE (telegram_user_id, kind)
);

-- ─────────────────────────────────────────
-- 3. Seed de preferencias: TODO activo para los miembros con telegram_user_id
-- ─────────────────────────────────────────

INSERT INTO user_preferences (telegram_user_id, kind, enabled, preferred_hour)
SELECT fm.telegram_user_id, k.kind, TRUE, 9
FROM family_members fm
CROSS JOIN (VALUES
    ('budget'),
    ('reminder'),
    ('monthly_summary'),
    ('anomaly')
) AS k(kind)
WHERE fm.telegram_user_id IS NOT NULL
ON CONFLICT (telegram_user_id, kind) DO NOTHING;
