-- Migración 008: Compras en cuotas, suscripciones recurrentes y resúmenes de tarjeta.

-- ─────────────────────────────────────────
-- 1. installment_plans (compras en cuotas en tarjeta)
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS installment_plans (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES accounts(id),  -- tarjeta de crédito
    fecha_compra    DATE NOT NULL,
    descripcion     TEXT NOT NULL,
    monto_total     NUMERIC(12, 2) NOT NULL CHECK (monto_total > 0),
    cuotas_total    INT NOT NULL CHECK (cuotas_total > 0),
    monto_cuota     NUMERIC(12, 2) NOT NULL CHECK (monto_cuota > 0),
    categoria       TEXT,
    subcategoria1   TEXT,
    subcategoria2   TEXT,
    notas           TEXT,
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_inst_plan_account ON installment_plans(account_id) WHERE activo;

-- ─────────────────────────────────────────
-- 2. recurring_charges (suscripciones / débitos automáticos)
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS recurring_charges (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id      UUID NOT NULL REFERENCES accounts(id),
    nombre          TEXT NOT NULL,
    monto           NUMERIC(12, 2) NOT NULL CHECK (monto > 0),
    dia_mes         INT NOT NULL CHECK (dia_mes BETWEEN 1 AND 31),
    categoria       TEXT NOT NULL DEFAULT 'Gastos variables',
    subcategoria1   TEXT,
    subcategoria2   TEXT,
    fecha_inicio    DATE NOT NULL DEFAULT CURRENT_DATE,
    fecha_fin       DATE,                       -- NULL = indefinido
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_rec_active ON recurring_charges(activo, dia_mes);

-- ─────────────────────────────────────────
-- 3. card_statements (resumen de ciclo de tarjeta)
-- ─────────────────────────────────────────

CREATE TABLE IF NOT EXISTS card_statements (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          UUID NOT NULL REFERENCES accounts(id),  -- tarjeta
    fecha_cierre        DATE NOT NULL,
    fecha_vencimiento   DATE NOT NULL,
    monto               NUMERIC(12, 2) NOT NULL,
    cuenta_pago_id      UUID NOT NULL REFERENCES accounts(id),  -- cuenta corriente que paga
    pagado              BOOLEAN NOT NULL DEFAULT FALSE,
    pagado_at           TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(account_id, fecha_cierre)
);

CREATE INDEX IF NOT EXISTS idx_statements_account ON card_statements(account_id, fecha_vencimiento);

-- ─────────────────────────────────────────
-- 4. transactions: agregar FKs de trazabilidad
-- ─────────────────────────────────────────

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS installment_plan_id UUID REFERENCES installment_plans(id),
    ADD COLUMN IF NOT EXISTS recurring_charge_id UUID REFERENCES recurring_charges(id),
    ADD COLUMN IF NOT EXISTS card_statement_id   UUID REFERENCES card_statements(id);

CREATE INDEX IF NOT EXISTS idx_tx_installment ON transactions(installment_plan_id) WHERE installment_plan_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tx_recurring   ON transactions(recurring_charge_id) WHERE recurring_charge_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tx_statement   ON transactions(card_statement_id)   WHERE card_statement_id IS NOT NULL;
