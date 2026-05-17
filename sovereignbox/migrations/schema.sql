-- ============================================================
-- SovereignBox AI — Family Lab Edition
-- PostgreSQL 16 — Schema Relacional Completo — v1.0
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- ENUM TYPES
-- ============================================================

CREATE TYPE user_role AS ENUM (
    'admin',
    'member'
);

CREATE TYPE transaction_category AS ENUM (
    'alimentacion',
    'farmacia',
    'salud',
    'transporte',
    'hogar',
    'servicios',
    'ocio',
    'ropa',
    'educacion',
    'otros'
);

CREATE TYPE document_type AS ENUM (
    'factura',
    'ticket',
    'recibo',
    'contrato',
    'certificado',
    'otro'
);

CREATE TYPE document_status AS ENUM (
    'pendiente_revision',
    'archivado',
    'pagado',
    'vencido',
    'marcado_purga',
    'purgado'
);

CREATE TYPE task_status AS ENUM (
    'pendiente',
    'en_progreso',
    'completada',
    'cancelada'
);

CREATE TYPE task_recurrence AS ENUM (
    'none',
    'daily',
    'weekly',
    'monthly'
);

CREATE TYPE shopping_item_status AS ENUM (
    'activo',
    'comprado'
);

-- ============================================================
-- FUNCIÓN REUTILIZABLE: auto-actualiza updated_at
-- ============================================================

CREATE OR REPLACE FUNCTION fn_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- TABLE: family_members
-- ============================================================

CREATE TABLE family_members (
    id                UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_user_id  BIGINT      NOT NULL UNIQUE,
    full_name         VARCHAR(255) NOT NULL,
    role              user_role   NOT NULL DEFAULT 'member',
    is_active         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ
);

CREATE TRIGGER trg_family_members_updated_at
    BEFORE UPDATE ON family_members
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

-- ============================================================
-- TABLE: transactions
-- ============================================================

CREATE TABLE transactions (
    id               UUID                 PRIMARY KEY DEFAULT gen_random_uuid(),
    family_member_id UUID                 NOT NULL REFERENCES family_members(id),
    amount           NUMERIC(12, 2)       NOT NULL CHECK (amount > 0),
    currency         VARCHAR(3)           NOT NULL DEFAULT 'EUR',
    category         transaction_category NOT NULL,
    description      TEXT,
    transaction_date DATE                 NOT NULL,
    -- output crudo del LLM, incluye campo confidence
    llm_confidence   NUMERIC(4, 3)        CHECK (llm_confidence BETWEEN 0 AND 1),
    llm_raw_output   JSONB,
    created_at       TIMESTAMPTZ          NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ,
    -- soft delete para soportar /undo
    deleted_at       TIMESTAMPTZ
);

CREATE TRIGGER trg_transactions_updated_at
    BEFORE UPDATE ON transactions
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE INDEX idx_transactions_member_date
    ON transactions (family_member_id, transaction_date)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_transactions_category
    ON transactions (category)
    WHERE deleted_at IS NULL;

CREATE INDEX idx_transactions_deleted_at
    ON transactions (deleted_at);

-- ============================================================
-- TABLE: documents
-- ============================================================

CREATE TABLE documents (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    family_member_id    UUID            NOT NULL REFERENCES family_members(id),
    document_type       document_type   NOT NULL,
    document_status     document_status NOT NULL DEFAULT 'pendiente_revision',
    vendor_name         VARCHAR(255),
    amount_total        NUMERIC(12, 2)  CHECK (amount_total >= 0),
    currency            VARCHAR(3)      NOT NULL DEFAULT 'EUR',
    issue_date          DATE,
    due_date            DATE,
    document_number     VARCHAR(100),
    tax_id_vendor       VARCHAR(50),
    -- ruta absoluta en FileSystem: /data/files/{año}/{mes}/{uuid}.ext
    file_path           TEXT            NOT NULL,
    -- formato: "Carpeta N, Folio M" — asignado por el sistema
    physical_location   VARCHAR(100),
    -- self-referencial: vincula factura ↔ recibo tras match automático
    related_document_id UUID            REFERENCES documents(id) ON DELETE SET NULL,
    -- output crudo de LLaVA para auditoría
    llm_raw_output      JSONB,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ,
    deleted_at          TIMESTAMPTZ
);

-- Trigger explícito requerido por la especificación (Sección 6, punto 6)
CREATE TRIGGER trg_documents_updated_at
    BEFORE UPDATE ON documents
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE INDEX idx_documents_status_issue_date
    ON documents (document_status, issue_date);

CREATE INDEX idx_documents_vendor_name
    ON documents (vendor_name)
    WHERE vendor_name IS NOT NULL;

CREATE INDEX idx_documents_physical_location
    ON documents (physical_location)
    WHERE physical_location IS NOT NULL;

CREATE INDEX idx_documents_due_date
    ON documents (due_date)
    WHERE due_date IS NOT NULL AND deleted_at IS NULL;

-- ============================================================
-- TABLE: tasks
-- ============================================================

CREATE TABLE tasks (
    id               UUID             PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by       UUID             NOT NULL REFERENCES family_members(id),
    assigned_to      UUID             REFERENCES family_members(id),
    title            VARCHAR(500)     NOT NULL,
    description      TEXT,
    due_datetime     TIMESTAMPTZ,
    task_status      task_status      NOT NULL DEFAULT 'pendiente',
    recurrence       task_recurrence  NOT NULL DEFAULT 'none',
    -- timestamp de último recordatorio enviado para evitar duplicados
    reminder_sent_at TIMESTAMPTZ,
    llm_raw_output   JSONB,
    created_at       TIMESTAMPTZ      NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ,
    deleted_at       TIMESTAMPTZ
);

CREATE TRIGGER trg_tasks_updated_at
    BEFORE UPDATE ON tasks
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

CREATE INDEX idx_tasks_assigned_due_status
    ON tasks (assigned_to, due_datetime, task_status)
    WHERE deleted_at IS NULL;

-- ============================================================
-- TABLE: shopping_items
-- ============================================================

CREATE TABLE shopping_items (
    id         UUID                 PRIMARY KEY DEFAULT gen_random_uuid(),
    added_by   UUID                 NOT NULL REFERENCES family_members(id),
    name       VARCHAR(255)         NOT NULL,
    -- cantidad libre: "2 kg", "1 pack", etc.
    quantity   VARCHAR(100),
    status     shopping_item_status NOT NULL DEFAULT 'activo',
    bought_by  UUID                 REFERENCES family_members(id),
    bought_at  TIMESTAMPTZ,
    created_at TIMESTAMPTZ          NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ
);

CREATE TRIGGER trg_shopping_items_updated_at
    BEFORE UPDATE ON shopping_items
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_at();

-- ============================================================
-- CONSULTA DE PRUEBA
-- Todas las facturas de un vendor en los últimos 12 meses,
-- con indicación de recibo vinculado (LEFT JOIN self-referencial),
-- ordenadas por due_date ASC.
--
-- Uso: reemplazar 'NOMBRE_DEL_VENDOR' por el vendor real.
-- ============================================================

SELECT
    d.id                   AS factura_id,
    d.document_number      AS factura_numero,
    d.vendor_name,
    d.amount_total,
    d.currency,
    d.issue_date,
    d.due_date,
    d.document_status,
    d.physical_location,
    r.id                   AS recibo_id,
    r.document_number      AS recibo_numero,
    r.issue_date           AS recibo_fecha
FROM
    documents d
LEFT JOIN
    documents r
        ON  r.related_document_id = d.id
        AND r.document_type       = 'recibo'
        AND r.deleted_at          IS NULL
WHERE
    d.document_type   = 'factura'
    AND d.vendor_name = 'NOMBRE_DEL_VENDOR'
    AND d.issue_date  >= NOW() - INTERVAL '12 months'
    AND d.deleted_at  IS NULL
ORDER BY
    d.due_date ASC NULLS LAST;
