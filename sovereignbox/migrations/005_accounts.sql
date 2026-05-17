-- Migración 005: Cuentas y medios de pago
-- Aplicar con: cat migrations/005_accounts.sql | docker compose exec -T postgres psql -U sovereign -d sovereignbox

-- ─────────────────────────────────────────
-- 1. Tabla accounts
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    nombre              TEXT NOT NULL,
    tipo                TEXT NOT NULL CHECK (tipo IN ('corriente', 'efectivo', 'tarjeta_credito')),
    family_member_id    UUID REFERENCES family_members(id),  -- NULL = cuenta familiar/compartida
    moneda              TEXT NOT NULL DEFAULT 'EUR',
    saldo_inicial       NUMERIC(14, 2) NOT NULL DEFAULT 0,
    activa              BOOLEAN NOT NULL DEFAULT TRUE,
    -- Campos solo para tarjeta_credito
    cierre_dia          INT,           -- día del mes en que cierra el resumen (1-31)
    vencimiento_dia     INT,           -- día del mes en que vence el pago (1-31)
    cuenta_pago_id      UUID REFERENCES accounts(id),  -- desde qué cuenta se paga el resumen
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_accounts_member ON accounts(family_member_id) WHERE activa;
CREATE INDEX IF NOT EXISTS idx_accounts_tipo   ON accounts(tipo) WHERE activa;

-- ─────────────────────────────────────────
-- 2. Seed inicial de cuentas
-- ─────────────────────────────────────────

INSERT INTO accounts (nombre, tipo, family_member_id, saldo_inicial, cierre_dia, vencimiento_dia)
SELECT 'Cuenta Hector', 'corriente',
       (SELECT id FROM family_members WHERE full_name = 'Hector Marioni'),
       411.00, NULL, NULL
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE nombre = 'Cuenta Hector');

INSERT INTO accounts (nombre, tipo, family_member_id, saldo_inicial, cierre_dia, vencimiento_dia)
SELECT 'Cuenta Luisiana', 'corriente',
       (SELECT id FROM family_members WHERE full_name = 'Luisiana'),
       3500.00, NULL, NULL
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE nombre = 'Cuenta Luisiana');

INSERT INTO accounts (nombre, tipo, family_member_id, saldo_inicial)
SELECT 'Efectivo casa', 'efectivo', NULL, 1500.00
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE nombre = 'Efectivo casa');

-- Visa Hector — saldo inicial 0, cierre fin de mes (30), vto día 15 del mes siguiente.
-- cuenta_pago_id se setea después con un UPDATE porque depende de Cuenta Hector.
INSERT INTO accounts (nombre, tipo, family_member_id, saldo_inicial, cierre_dia, vencimiento_dia)
SELECT 'Visa Hector', 'tarjeta_credito',
       (SELECT id FROM family_members WHERE full_name = 'Hector Marioni'),
       0.00, 30, 15
WHERE NOT EXISTS (SELECT 1 FROM accounts WHERE nombre = 'Visa Hector');

UPDATE accounts
SET cuenta_pago_id = (SELECT id FROM accounts WHERE nombre = 'Cuenta Hector')
WHERE nombre = 'Visa Hector' AND cuenta_pago_id IS NULL;

-- ─────────────────────────────────────────
-- 3. transactions: agregar account_id y fecha_valor
-- ─────────────────────────────────────────

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS account_id   UUID REFERENCES accounts(id),
    ADD COLUMN IF NOT EXISTS fecha_valor  DATE;

-- Backfill account_id según el miembro de cada transacción
UPDATE transactions t
SET account_id = (
    SELECT a.id FROM accounts a
    WHERE a.family_member_id = t.family_member_id
      AND a.tipo = 'corriente'
    LIMIT 1
)
WHERE account_id IS NULL;

-- Backfill fecha_valor = transaction_date (asumimos que las históricas no son tarjeta)
UPDATE transactions
SET fecha_valor = transaction_date
WHERE fecha_valor IS NULL;

-- Hacerlos NOT NULL después del backfill
ALTER TABLE transactions
    ALTER COLUMN account_id  SET NOT NULL,
    ALTER COLUMN fecha_valor SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_tx_account_valor
    ON transactions (account_id, fecha_valor)
    WHERE deleted_at IS NULL;

-- ─────────────────────────────────────────
-- 4. Vista de saldo actual por cuenta
-- ─────────────────────────────────────────
-- Saldo actual = saldo_inicial + ingresos - gastos (con fecha_valor <= hoy)
-- Para tarjetas de crédito, "saldo" es la deuda actual (positiva = se debe).

CREATE OR REPLACE VIEW v_saldo_cuentas AS
SELECT
    a.id,
    a.nombre,
    a.tipo,
    a.family_member_id,
    fm.full_name AS miembro,
    a.moneda,
    a.saldo_inicial,
    a.activa,
    a.cierre_dia,
    a.vencimiento_dia,
    a.cuenta_pago_id,
    -- Para corriente/efectivo: saldo = saldo_inicial + ingresos - gastos.
    -- Para tarjeta_credito: deuda = gastos (cargados) - ingresos (pagos al resumen).
    --   El saldo_inicial de una tarjeta representa la deuda inicial (positiva).
    CASE
        WHEN a.tipo = 'tarjeta_credito' THEN
            ROUND((a.saldo_inicial + COALESCE(SUM(
                CASE
                    WHEN t.tipo = 'gasto'   AND t.fecha_valor <= CURRENT_DATE THEN t.amount
                    WHEN t.tipo = 'ingreso' AND t.fecha_valor <= CURRENT_DATE THEN -t.amount
                    ELSE 0
                END
            ), 0))::NUMERIC, 2)
        ELSE
            ROUND((a.saldo_inicial + COALESCE(SUM(
                CASE
                    WHEN t.tipo = 'ingreso' AND t.fecha_valor <= CURRENT_DATE THEN t.amount
                    WHEN t.tipo = 'gasto'   AND t.fecha_valor <= CURRENT_DATE THEN -t.amount
                    ELSE 0
                END
            ), 0))::NUMERIC, 2)
    END AS saldo_actual
FROM accounts a
LEFT JOIN family_members fm ON a.family_member_id = fm.id
LEFT JOIN transactions t ON t.account_id = a.id AND t.deleted_at IS NULL
GROUP BY a.id, fm.full_name;

-- ─────────────────────────────────────────
-- 5. Vista de patrimonio neto familiar
-- ─────────────────────────────────────────

CREATE OR REPLACE VIEW v_patrimonio_neto AS
SELECT
    ROUND(SUM(CASE WHEN tipo IN ('corriente', 'efectivo') THEN saldo_actual ELSE 0 END)::NUMERIC, 2) AS activos,
    ROUND(SUM(CASE WHEN tipo = 'tarjeta_credito'         THEN saldo_actual ELSE 0 END)::NUMERIC, 2) AS pasivos,
    ROUND((
        SUM(CASE WHEN tipo IN ('corriente', 'efectivo') THEN saldo_actual ELSE 0 END)
      - SUM(CASE WHEN tipo = 'tarjeta_credito'          THEN saldo_actual ELSE 0 END)
    )::NUMERIC, 2) AS patrimonio_neto
FROM v_saldo_cuentas
WHERE activa;
