-- Migración 011: lista de compras familiar (shopping_list_items).

CREATE TABLE IF NOT EXISTS shopping_list_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    texto           TEXT NOT NULL,
    cantidad        NUMERIC(10, 2),       -- opcional, ej: 2 (kg), 6 (unidades)
    unidad          TEXT,                 -- opcional, ej: 'kg', 'l', 'u'
    completed_at    TIMESTAMPTZ,          -- NULL = pendiente
    created_by      UUID REFERENCES family_members(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ
);

-- Index para listar pendientes (lo más frecuente)
CREATE INDEX IF NOT EXISTS idx_shop_pendientes
    ON shopping_list_items (created_at DESC)
    WHERE completed_at IS NULL;

-- Index para búsqueda por texto (futuro auto-complete)
CREATE INDEX IF NOT EXISTS idx_shop_texto
    ON shopping_list_items (LOWER(texto));
