-- ============================================================
-- household — schema.sql
-- PostgreSQL 16 · financial-only data model · 5 tables
-- ============================================================
-- Conventions:
--   · singular table names               (family_member, not family_members)
--   · PK column named {table}_id         (family_member_id)
--   · FK columns reuse the same name     (family_member_id everywhere)
--   · timestamps end in _ts              (created_ts, updated_ts, deleted_ts)
--   · dates end in _date                 (transaction_date, value_date)
-- ============================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ============================================================
-- shared trigger: keep updated_ts current on every UPDATE
-- ============================================================

CREATE OR REPLACE FUNCTION fn_set_updated_ts()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_ts = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- enums
-- ============================================================

CREATE TYPE account_kind AS ENUM (
    'checking',
    'savings',
    'cash',
    'credit_card'
);

CREATE TYPE transaction_kind AS ENUM (
    'income',
    'expense'
);

CREATE TYPE transaction_source AS ENUM (
    'manual',      -- typed in the dashboard
    'telegram',    -- captured via the bot
    'recurring'    -- auto-generated from a recurring_charge
);

-- ============================================================
-- 1. family_member
--    Foundation. Everything else FKs here (directly or via account).
-- ============================================================

CREATE TABLE family_member (
    family_member_id  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    full_name         VARCHAR(255) NOT NULL,
    telegram_user_id  BIGINT       UNIQUE,        -- NULL until they bind their Telegram
    is_active         BOOLEAN      NOT NULL DEFAULT TRUE,
    created_ts        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_ts        TIMESTAMPTZ
);

CREATE TRIGGER trg_family_member_updated_ts
    BEFORE UPDATE ON family_member
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

-- ============================================================
-- 2. account
--    A bucket of money: checking / savings / cash / credit_card.
--    Owned by a family_member, OR shared (family_member_id NULL).
--
--    Balance model: current_balance = initial_balance + Σ(transactions where
--    value_date > balance_date AND value_date <= today). This lets you load
--    historical transactions without double-counting against initial_balance.
-- ============================================================

CREATE TABLE account (
    account_id        UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    family_member_id  UUID            REFERENCES family_member(family_member_id),  -- NULL = shared
    name              TEXT            NOT NULL,
    kind              account_kind    NOT NULL,
    currency          VARCHAR(3)      NOT NULL DEFAULT 'EUR',
    initial_balance   NUMERIC(14, 2)  NOT NULL DEFAULT 0,
    balance_date      DATE            NOT NULL DEFAULT CURRENT_DATE,
    is_active         BOOLEAN         NOT NULL DEFAULT TRUE,
    created_ts        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_ts        TIMESTAMPTZ
);

CREATE TRIGGER trg_account_updated_ts
    BEFORE UPDATE ON account
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX idx_account_member  ON account (family_member_id) WHERE is_active;
CREATE INDEX idx_account_kind    ON account (kind)             WHERE is_active;

-- ============================================================
-- 3. recurring_charge
--    Definition of a periodic charge (Netflix €12 on the 5th, rent, etc.).
--    NOT the charges themselves — those are transactions with
--    recurring_charge_id pointing here, generated each cycle.
-- ============================================================

CREATE TABLE recurring_charge (
    recurring_charge_id UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          UUID            NOT NULL REFERENCES account(account_id),
    name                TEXT            NOT NULL,
    amount              NUMERIC(12, 2)  NOT NULL CHECK (amount > 0),
    day_of_month        INT             NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
    category            TEXT            NOT NULL,
    subcategory_1       TEXT,
    subcategory_2       TEXT,
    start_date          DATE            NOT NULL DEFAULT CURRENT_DATE,
    end_date            DATE,                                    -- NULL = open-ended
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_ts          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_ts          TIMESTAMPTZ
);

CREATE TRIGGER trg_recurring_charge_updated_ts
    BEFORE UPDATE ON recurring_charge
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX idx_recurring_charge_active ON recurring_charge (is_active, day_of_month);

-- ============================================================
-- 4. monthly_budget
--    One default monthly limit per subcategory_1. Not per-month.
--    Joined to transactions by subcategory_1 (no FK).
--    Per-(year,month) overrides can be added later if we need them.
-- ============================================================

CREATE TABLE monthly_budget (
    monthly_budget_id UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    subcategory_1     TEXT            NOT NULL UNIQUE,
    limit_amount      NUMERIC(12, 2)  NOT NULL CHECK (limit_amount > 0),
    created_ts        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_ts        TIMESTAMPTZ
);

CREATE TRIGGER trg_monthly_budget_updated_ts
    BEFORE UPDATE ON monthly_budget
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

-- ============================================================
-- 5. transaction
--    The fact table — center of the star. Every financial event lands here.
--    FKs point OUTWARD to the parent constructs that may have spawned it.
--
--    Date semantics:
--      transaction_date — when the event happened (when you bought the thing)
--      value_date       — when money actually moves
--                          · cash/checking: same as transaction_date
--                          · credit_card:   when the statement is paid
--      Only value_date counts toward current_balance.
--
--    LLM fields:
--      llm_confidence — extraction confidence (0..1). Below threshold the bot
--                       asks the user to confirm; above, it auto-saves.
--      llm_raw_output — full LLM JSON for audit / debugging.
--
--    Soft-deletion:
--      Set deleted_ts to NOW() instead of DELETE. All analytics views must
--      filter WHERE deleted_ts IS NULL.
-- ============================================================

CREATE TABLE transaction (
    transaction_id      UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id          UUID                NOT NULL REFERENCES account(account_id),
    family_member_id    UUID                NOT NULL REFERENCES family_member(family_member_id),
    recurring_charge_id UUID                REFERENCES recurring_charge(recurring_charge_id),
    kind                transaction_kind    NOT NULL,
    amount              NUMERIC(12, 2)      NOT NULL CHECK (amount > 0),
    currency            VARCHAR(3)          NOT NULL DEFAULT 'EUR',
    category            TEXT                NOT NULL,
    subcategory_1       TEXT,
    subcategory_2       TEXT,
    description         TEXT,
    transaction_date    DATE                NOT NULL DEFAULT CURRENT_DATE,
    value_date          DATE                NOT NULL DEFAULT CURRENT_DATE,
    source              transaction_source  NOT NULL DEFAULT 'manual',
    llm_confidence      NUMERIC(4, 3)       CHECK (llm_confidence BETWEEN 0 AND 1),
    llm_raw_output      JSONB,
    created_ts          TIMESTAMPTZ         NOT NULL DEFAULT NOW(),
    updated_ts          TIMESTAMPTZ,
    deleted_ts          TIMESTAMPTZ
);

CREATE TRIGGER trg_transaction_updated_ts
    BEFORE UPDATE ON transaction
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX idx_transaction_account_value
    ON transaction (account_id, value_date)
    WHERE deleted_ts IS NULL;

CREATE INDEX idx_transaction_member_date
    ON transaction (family_member_id, transaction_date)
    WHERE deleted_ts IS NULL;

CREATE INDEX idx_transaction_category
    ON transaction (category, subcategory_1)
    WHERE deleted_ts IS NULL;

CREATE INDEX idx_transaction_recurring
    ON transaction (recurring_charge_id)
    WHERE recurring_charge_id IS NOT NULL;
