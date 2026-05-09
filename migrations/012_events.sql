-- Migración 012: agenda familiar (events) + recordatorios automáticos.

CREATE TABLE IF NOT EXISTS events (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    titulo                      TEXT NOT NULL,
    fecha                       DATE NOT NULL,
    hora                        TIME,                         -- NULL = todo el día
    fin_fecha                   DATE,                         -- NULL = mismo día
    fin_hora                    TIME,
    categoria                   TEXT NOT NULL DEFAULT 'otro', -- medico|colegio|burocracia|familia|otro
    descripcion                 TEXT,
    ubicacion                   TEXT,
    recordatorio_horas_antes    INT NOT NULL DEFAULT 2,       -- 0 = sin recordatorio
    created_by                  UUID REFERENCES family_members(id),
    deleted_at                  TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ
);

-- Índice para vistas de calendario y consultas por rango
CREATE INDEX IF NOT EXISTS idx_events_fecha
    ON events (fecha)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_events_categoria
    ON events (categoria)
    WHERE deleted_at IS NULL;
