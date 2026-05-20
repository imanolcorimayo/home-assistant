-- ============================================================
-- household — import_sovereignbox.sql
-- Transform + load the sovereignbox finance data into household.
--
-- Assumes the sovereignbox dump has been loaded into schema `sbox_src`
-- (the orchestrator import_sovereignbox.sh does that), and that
-- migrations/003_categories.sql has been applied (category table exists).
--
-- Idempotent: original UUIDs are preserved and every INSERT uses
-- ON CONFLICT DO NOTHING, so re-running adds nothing new.
--
-- Column mapping (sovereignbox -> household):
--   nota         -> description
--   tipo         -> kind        (gasto->expense, ingreso->income)
--   categoria    -> (folded into kind; only variable/fijo/ingreso survives as category.grupo)
--   subcategoria1-> category    (FK via category.name; NULL -> 'Sin categoría')
--   subcategoria2-> subcategory_1
--   subcategoria3-> subcategory_2
--   origen       -> source      (telegram/whatsapp kept; automatico->recurring; rest->manual)
--   fecha_valor  -> value_date
--   deleted_at   -> deleted_ts
-- ============================================================

BEGIN;

-- 1. family members (drop the `role` column, no home in household)
INSERT INTO family_member (family_member_id, full_name, telegram_user_id, is_active, created_ts, updated_ts)
SELECT id, full_name, telegram_user_id, is_active, created_at, updated_at
FROM sbox_src.family_members
ON CONFLICT (family_member_id) DO NOTHING;

-- 2. accounts (tipo -> kind enum)
INSERT INTO account (account_id, family_member_id, name, kind, currency, initial_balance, balance_date, is_active, created_ts, updated_ts)
SELECT
    id,
    family_member_id,
    nombre,
    CASE tipo
        WHEN 'corriente'       THEN 'checking'
        WHEN 'efectivo'        THEN 'cash'
        WHEN 'tarjeta_credito' THEN 'credit_card'
    END::account_kind,
    moneda,
    saldo_inicial,
    saldo_fecha,
    activa,
    created_at,
    updated_at
FROM sbox_src.accounts
ON CONFLICT (account_id) DO NOTHING;

-- 3. monthly budgets (keyed by subcategory_1)
INSERT INTO monthly_budget (subcategory_1, limit_amount, created_ts, updated_ts)
SELECT subcategoria1, limit_amount, created_at, updated_at
FROM sbox_src.monthly_budgets
ON CONFLICT (subcategory_1) DO NOTHING;

-- 4. transactions (the fact table)
INSERT INTO transaction (
    transaction_id, account_id, family_member_id, recurring_charge_id,
    kind, amount, currency, category, category_id, subcategory_1, subcategory_2,
    description, transaction_date, value_date, source,
    llm_confidence, llm_raw_output, created_ts, updated_ts, deleted_ts
)
SELECT
    t.id,
    t.account_id,
    t.family_member_id,
    NULL,                              -- recurring_charges has no rows to link to
    CASE
        WHEN t.tipo = 'ingreso'      THEN 'income'
        WHEN t.tipo = 'gasto'        THEN 'expense'
        WHEN t.categoria = 'Entradas' THEN 'income'
        ELSE 'expense'                 -- 24 fully-null rows default to expense
    END::transaction_kind,
    t.amount,
    t.currency,
    COALESCE(t.subcategoria1, 'Sin categoría'),
    c.category_id,
    t.subcategoria2,
    t.subcategoria3,
    t.nota,
    t.transaction_date,
    t.fecha_valor,
    CASE t.origen
        WHEN 'telegram'   THEN 'telegram'
        WHEN 'whatsapp'   THEN 'whatsapp'
        WHEN 'automatico' THEN 'recurring'
        ELSE 'manual'                  -- importado / computadora / NULL
    END::transaction_source,
    t.llm_confidence,
    t.llm_raw_output,
    t.created_at,
    t.updated_at,
    t.deleted_at
FROM sbox_src.transactions t
LEFT JOIN category c ON c.name = COALESCE(t.subcategoria1, 'Sin categoría')
ON CONFLICT (transaction_id) DO NOTHING;

COMMIT;
