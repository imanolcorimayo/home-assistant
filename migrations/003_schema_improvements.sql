-- Migración 003: schema optimizado para contabilidad familiar con analytics
-- Aplicar con: docker exec -i sovereignbox_db psql -U sovereign -d sovereignbox < migrations/003_schema_improvements.sql

-- ─────────────────────────────────────────
-- 1. TRANSACTIONS: nuevos campos
-- ─────────────────────────────────────────

ALTER TABLE transactions
    RENAME COLUMN description TO nota;

ALTER TABLE transactions
    ADD COLUMN IF NOT EXISTS tipo          TEXT,        -- 'ingreso' | 'gasto'
    ADD COLUMN IF NOT EXISTS subcategoria3 TEXT,        -- solo Transporte: Combustible, Mantenimiento…
    ADD COLUMN IF NOT EXISTS origen        TEXT;        -- 'telegram' | 'whatsapp' | 'computadora' | 'importado'

-- Backfill tipo desde categoria para registros existentes
UPDATE transactions
SET tipo = CASE
    WHEN categoria = 'Entradas' THEN 'ingreso'
    ELSE 'gasto'
END
WHERE tipo IS NULL AND categoria IS NOT NULL;

-- ─────────────────────────────────────────
-- 2. FAMILY_MEMBERS: telegram_user_id opcional
--    (Luisiana puede no usar Telegram)
-- ─────────────────────────────────────────

ALTER TABLE family_members
    ALTER COLUMN telegram_user_id DROP NOT NULL;

INSERT INTO family_members (id, telegram_user_id, full_name, role, is_active, created_at)
VALUES (gen_random_uuid(), NULL, 'Luisiana', 'member', true, now())
ON CONFLICT DO NOTHING;

-- ─────────────────────────────────────────
-- 3. ÍNDICES para queries de analytics
-- ─────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_tx_date_tipo
    ON transactions (transaction_date, tipo)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tx_categoria
    ON transactions (categoria, subcategoria1, subcategoria2)
    WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_tx_member_date
    ON transactions (family_member_id, transaction_date)
    WHERE deleted_at IS NULL;

-- ─────────────────────────────────────────
-- 4. VISTAS DE ANALYTICS
-- ─────────────────────────────────────────

-- Base: resumen mensual por subcategoría y miembro
CREATE OR REPLACE VIEW v_resumen_mensual AS
SELECT
    EXTRACT(YEAR  FROM t.transaction_date)::INT  AS anio,
    EXTRACT(MONTH FROM t.transaction_date)::INT  AS mes,
    t.tipo,
    t.categoria,
    t.subcategoria1,
    t.subcategoria2,
    t.subcategoria3,
    fm.full_name                                 AS miembro,
    t.origen,
    SUM(t.amount)                                AS total,
    COUNT(*)                                     AS cantidad
FROM transactions t
JOIN family_members fm ON t.family_member_id = fm.id
WHERE t.deleted_at IS NULL
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9;

-- Balance mensual: ingresos vs gastos vs ahorro
CREATE OR REPLACE VIEW v_balance_mensual AS
SELECT
    anio,
    mes,
    ROUND(SUM(CASE WHEN tipo = 'ingreso' THEN total ELSE 0    END)::NUMERIC, 2) AS ingresos,
    ROUND(SUM(CASE WHEN tipo = 'gasto'   THEN total ELSE 0    END)::NUMERIC, 2) AS gastos,
    ROUND(SUM(CASE WHEN tipo = 'ingreso' THEN total ELSE -total END)::NUMERIC, 2) AS balance,
    ROUND(
        CASE WHEN SUM(CASE WHEN tipo='ingreso' THEN total ELSE 0 END) > 0
        THEN 100.0 * SUM(CASE WHEN tipo='gasto' THEN total ELSE 0 END)
                  / SUM(CASE WHEN tipo='ingreso' THEN total ELSE 0 END)
        ELSE NULL END
    ::NUMERIC, 1) AS pct_gasto_sobre_ingreso
FROM v_resumen_mensual
GROUP BY anio, mes
ORDER BY anio, mes;

-- Gastos variables desglosados por mes
CREATE OR REPLACE VIEW v_gastos_variables AS
SELECT
    anio,
    mes,
    subcategoria1,
    subcategoria2,
    subcategoria3,
    miembro,
    ROUND(SUM(total)::NUMERIC, 2) AS total,
    SUM(cantidad)                  AS cantidad
FROM v_resumen_mensual
WHERE tipo = 'gasto' AND categoria = 'Gastos variables'
GROUP BY anio, mes, subcategoria1, subcategoria2, subcategoria3, miembro
ORDER BY anio, mes, total DESC;

-- Ingresos por miembro y fuente
CREATE OR REPLACE VIEW v_ingresos AS
SELECT
    anio,
    mes,
    subcategoria1  AS persona,
    subcategoria2  AS fuente,
    ROUND(SUM(total)::NUMERIC, 2) AS total,
    SUM(cantidad)                  AS cantidad
FROM v_resumen_mensual
WHERE tipo = 'ingreso'
GROUP BY anio, mes, subcategoria1, subcategoria2
ORDER BY anio, mes, total DESC;

-- Gastos fijos por mes (para detectar variaciones)
CREATE OR REPLACE VIEW v_gastos_fijos AS
SELECT
    anio,
    mes,
    subcategoria1,
    subcategoria2,
    ROUND(SUM(total)::NUMERIC, 2) AS total
FROM v_resumen_mensual
WHERE tipo = 'gasto' AND categoria = 'Gastos Fijos'
GROUP BY anio, mes, subcategoria1, subcategoria2
ORDER BY anio, mes, subcategoria1;

-- Evolución mensual por subcategoria1 (para gráficos de tendencia)
CREATE OR REPLACE VIEW v_tendencia_subcategoria1 AS
SELECT
    anio,
    mes,
    tipo,
    subcategoria1,
    ROUND(SUM(total)::NUMERIC, 2) AS total
FROM v_resumen_mensual
GROUP BY anio, mes, tipo, subcategoria1
ORDER BY anio, mes, tipo, total DESC;
