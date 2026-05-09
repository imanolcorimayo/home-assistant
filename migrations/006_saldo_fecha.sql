-- Migración 006: saldo_fecha — el saldo_inicial es un snapshot en una fecha,
-- y solo las transacciones con fecha_valor > saldo_fecha modifican el saldo.

ALTER TABLE accounts
    ADD COLUMN IF NOT EXISTS saldo_fecha DATE NOT NULL DEFAULT CURRENT_DATE;

-- Para las cuentas ya creadas hoy, dejamos saldo_fecha = hoy.
-- Esto significa: las 535 transacciones históricas NO afectan el saldo actual,
-- solo afectarán las que se carguen desde ahora en adelante.

DROP VIEW IF EXISTS v_patrimonio_neto;
DROP VIEW IF EXISTS v_saldo_cuentas;

CREATE VIEW v_saldo_cuentas AS
SELECT
    a.id,
    a.nombre,
    a.tipo,
    a.family_member_id,
    fm.full_name AS miembro,
    a.moneda,
    a.saldo_inicial,
    a.saldo_fecha,
    a.activa,
    a.cierre_dia,
    a.vencimiento_dia,
    a.cuenta_pago_id,
    CASE
        WHEN a.tipo = 'tarjeta_credito' THEN
            ROUND((a.saldo_inicial + COALESCE(SUM(
                CASE
                    WHEN t.tipo = 'gasto'   AND t.fecha_valor > a.saldo_fecha AND t.fecha_valor <= CURRENT_DATE THEN t.amount
                    WHEN t.tipo = 'ingreso' AND t.fecha_valor > a.saldo_fecha AND t.fecha_valor <= CURRENT_DATE THEN -t.amount
                    ELSE 0
                END
            ), 0))::NUMERIC, 2)
        ELSE
            ROUND((a.saldo_inicial + COALESCE(SUM(
                CASE
                    WHEN t.tipo = 'ingreso' AND t.fecha_valor > a.saldo_fecha AND t.fecha_valor <= CURRENT_DATE THEN t.amount
                    WHEN t.tipo = 'gasto'   AND t.fecha_valor > a.saldo_fecha AND t.fecha_valor <= CURRENT_DATE THEN -t.amount
                    ELSE 0
                END
            ), 0))::NUMERIC, 2)
    END AS saldo_actual
FROM accounts a
LEFT JOIN family_members fm ON a.family_member_id = fm.id
LEFT JOIN transactions t ON t.account_id = a.id AND t.deleted_at IS NULL
GROUP BY a.id, fm.full_name;

CREATE VIEW v_patrimonio_neto AS
SELECT
    ROUND(SUM(CASE WHEN tipo IN ('corriente', 'efectivo') THEN saldo_actual ELSE 0 END)::NUMERIC, 2) AS activos,
    ROUND(SUM(CASE WHEN tipo = 'tarjeta_credito'         THEN saldo_actual ELSE 0 END)::NUMERIC, 2) AS pasivos,
    ROUND((
        SUM(CASE WHEN tipo IN ('corriente', 'efectivo') THEN saldo_actual ELSE 0 END)
      - SUM(CASE WHEN tipo = 'tarjeta_credito'          THEN saldo_actual ELSE 0 END)
    )::NUMERIC, 2) AS patrimonio_neto
FROM v_saldo_cuentas
WHERE activa;
