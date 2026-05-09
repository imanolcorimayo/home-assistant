-- Migración 007: Préstamos automáticos
-- Aplicar con: cat migrations/007_loans.sql | docker compose exec -T postgres psql -U sovereign -d sovereignbox

-- ─────────────────────────────────────────
-- 1. Tabla loans
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS loans (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre              TEXT NOT NULL,
    cuenta_pago_id      UUID NOT NULL REFERENCES accounts(id),
    monto_cuota         NUMERIC(12, 2) NOT NULL CHECK (monto_cuota > 0),
    dia_vencimiento     INT NOT NULL CHECK (dia_vencimiento BETWEEN 1 AND 31),
    fecha_inicio        DATE NOT NULL,        -- fecha de la 1ª cuota
    fecha_fin           DATE NOT NULL,        -- fecha de la última cuota
    monto_ultima_cuota  NUMERIC(12, 2),       -- si es distinto al monto regular
    notas               TEXT,
    activo              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_loans_activo ON loans(activo);

-- ─────────────────────────────────────────
-- 2. transactions: agregar loan_id (trazabilidad de cuota → préstamo)
-- ─────────────────────────────────────────

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS loan_id UUID REFERENCES loans(id);

CREATE INDEX IF NOT EXISTS idx_tx_loan ON transactions(loan_id) WHERE loan_id IS NOT NULL;

-- ─────────────────────────────────────────
-- 3. Seed: 2 préstamos del usuario
--    (fecha_inicio = próximo día 15 desde hoy; el usuario edita después)
-- ─────────────────────────────────────────

INSERT INTO loans (nombre, cuenta_pago_id, monto_cuota, dia_vencimiento, fecha_inicio, fecha_fin, notas)
SELECT 'Préstamo 1',
       (SELECT id FROM accounts WHERE nombre = 'Cuenta Hector'),
       249.00, 15,
       DATE '2026-05-15', DATE '2027-03-15',
       'Última cuota con monto distinto — editar cuando se confirme'
WHERE NOT EXISTS (SELECT 1 FROM loans WHERE nombre = 'Préstamo 1');

INSERT INTO loans (nombre, cuenta_pago_id, monto_cuota, dia_vencimiento, fecha_inicio, fecha_fin)
SELECT 'Préstamo 2',
       (SELECT id FROM accounts WHERE nombre = 'Cuenta Hector'),
       250.00, 15,
       DATE '2026-05-15', DATE '2028-12-15'
WHERE NOT EXISTS (SELECT 1 FROM loans WHERE nombre = 'Préstamo 2');

-- ─────────────────────────────────────────
-- 4. Vista de estado de préstamos
-- ─────────────────────────────────────────

CREATE OR REPLACE VIEW v_loans_status AS
SELECT
    l.id,
    l.nombre,
    l.cuenta_pago_id,
    a.nombre AS cuenta_pago_nombre,
    l.monto_cuota,
    l.monto_ultima_cuota,
    l.dia_vencimiento,
    l.fecha_inicio,
    l.fecha_fin,
    l.notas,
    l.activo,
    -- Cuotas totales (meses entre fecha_inicio y fecha_fin, inclusive)
    ((EXTRACT(YEAR  FROM l.fecha_fin) - EXTRACT(YEAR  FROM l.fecha_inicio)) * 12
   + (EXTRACT(MONTH FROM l.fecha_fin) - EXTRACT(MONTH FROM l.fecha_inicio)) + 1)::INT AS cuotas_total,
    -- Cuotas restantes (desde el mes en curso hasta fecha_fin, no negativo)
    GREATEST(
        ((EXTRACT(YEAR  FROM l.fecha_fin) - EXTRACT(YEAR  FROM CURRENT_DATE)) * 12
       + (EXTRACT(MONTH FROM l.fecha_fin) - EXTRACT(MONTH FROM CURRENT_DATE)) + 1)::INT,
        0
    ) AS cuotas_restantes,
    -- Cuotas ya generadas (transactions con loan_id)
    (SELECT COUNT(*) FROM transactions t
     WHERE t.loan_id = l.id AND t.deleted_at IS NULL) AS cuotas_generadas
FROM loans l
LEFT JOIN accounts a ON a.id = l.cuenta_pago_id;
