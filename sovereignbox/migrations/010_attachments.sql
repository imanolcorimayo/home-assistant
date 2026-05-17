-- Migración 010: tabla attachments polimórfica + estado_pago en transactions.

-- ─────────────────────────────────────────
-- 1. attachments: archivos asociables a cualquier entidad
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS attachments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    file_path       TEXT NOT NULL,           -- relativo a /app/data/files
    original_name   TEXT NOT NULL,
    mime_type       TEXT NOT NULL,
    size_bytes      BIGINT NOT NULL,
    uploaded_by     UUID REFERENCES family_members(id),
    uploaded_via    TEXT NOT NULL,           -- 'telegram' | 'web'
    -- Polimorfismo: vínculo opcional a una entidad
    entity_type     TEXT,                    -- 'transaction'|'event'|'task'|null (huérfano)
    entity_id       UUID,
    role            TEXT,                    -- 'boleta'|'comprobante'|'foto'|'documento'|'otro'
    notas           TEXT,
    deleted_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_att_entity
    ON attachments (entity_type, entity_id)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_att_orphan
    ON attachments (uploaded_by, created_at DESC)
    WHERE entity_type IS NULL AND deleted_at IS NULL;

-- ─────────────────────────────────────────
-- 2. transactions: estado_pago para flujo "boleta pendiente → pagada"
-- ─────────────────────────────────────────

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS estado_pago TEXT;
-- valores: NULL (pago directo, default), 'pendiente' (hay boleta sin pagar),
-- 'pagado' (la boleta fue pagada y hay comprobante)

CREATE INDEX IF NOT EXISTS idx_tx_estado_pago
    ON transactions (estado_pago, fecha_valor)
    WHERE deleted_at IS NULL AND estado_pago IS NOT NULL;
