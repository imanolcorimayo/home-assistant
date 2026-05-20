-- ============================================================
-- household — 003_categories.sql
-- Editable category lookup table + transaction.category_id FK.
--
-- Replaces the old free-text `categoria` taxonomy from sovereignbox.
-- `category` holds the granular categories (Supermercado, Transporte…).
-- `grupo` is the coarse bucket the value used to live under:
--   'variable' | 'fijo' | 'ingreso'  (NULL = unsorted).
-- Idempotent: safe to re-run.
-- ============================================================

CREATE TABLE IF NOT EXISTS category (
    category_id  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name         TEXT         NOT NULL UNIQUE,
    grupo        TEXT,                              -- variable | fijo | ingreso | NULL
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_ts   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_ts   TIMESTAMPTZ
);

CREATE OR REPLACE TRIGGER trg_category_updated_ts
    BEFORE UPDATE ON category
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

-- Seed from the real categories found in the sovereignbox data.
INSERT INTO category (name, grupo) VALUES
    ('Supermercado',    'variable'),
    ('Transporte',      'variable'),
    ('Entretenimiento', 'variable'),
    ('Salud',           'variable'),
    ('Servicios',       'variable'),
    ('Bazar',           'variable'),
    ('Vestimenta',      'variable'),
    ('Estudio',         'variable'),
    ('Suscripciones',   'variable'),
    ('Farmacia',        'variable'),
    ('Peluqueria',      'variable'),
    ('Alquiler',        'fijo'),
    ('Prestamos',       'fijo'),
    ('Colegios',        'fijo'),
    ('Celulares',       'fijo'),
    ('Buroc',           'ingreso'),
    ('Hector',          'ingreso'),
    ('Luisiana',        'ingreso'),
    ('Sin categoría',   NULL)
ON CONFLICT (name) DO NOTHING;

-- FK on the fact table. Nullable for now: the live capture path still
-- writes the text `category`; backfilling category_id there comes later.
ALTER TABLE transaction
    ADD COLUMN IF NOT EXISTS category_id UUID REFERENCES category(category_id);

CREATE INDEX IF NOT EXISTS idx_transaction_category_id
    ON transaction (category_id)
    WHERE deleted_ts IS NULL;
