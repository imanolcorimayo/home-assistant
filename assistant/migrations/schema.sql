-- ============================================================
-- assistant — schema.sql
-- PostgreSQL 16 · multi-family financial assistant · initial schema
-- ============================================================
-- This is the new unified app (replaces household's dashboard + Telegram
-- split). Two big changes vs. household:
--   1. MULTI-TENANT: a `family` is the tenant; `family_id` is on every
--      data table and every query must scope by it.
--   2. AUTH: `member` is a person AND their Google login (merged — there
--      is no separate user table). `email`/`google_sub` come from Google
--      sign-in and are NOT NULL: everyone who appears here has logged in.
--
-- Conventions (carried over from household):
--   · singular table names               (member, not members)
--   · PK column named {table}_id          (member_id)
--   · FK columns reuse the same name      (member_id, family_id everywhere)
--   · timestamps end in _ts               (created_ts, updated_ts, deleted_ts)
--   · dates end in _date                  (transaction_date, value_date)
--   · soft-delete via deleted_ts          (analytics views filter IS NULL)
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

-- No more 'telegram' / 'whatsapp': the in-house chat is the only capture
-- channel now. 'manual' = typed into a form; 'chat' = captured by the agent
-- from a chat message; 'recurring' = auto-generated from a recurring_charge.
CREATE TYPE transaction_source AS ENUM (
    'manual',
    'chat',
    'recurring'
);

-- ============================================================
-- 1. family
--    The tenant root. Everything else FKs here. One row per household
--    using the app. `currency` is the family default; an account may
--    override it per-account.
-- ============================================================

CREATE TABLE family (
    family_id   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT         NOT NULL,
    currency    VARCHAR(3)   NOT NULL DEFAULT 'EUR',
    created_ts  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_ts  TIMESTAMPTZ
);

CREATE TRIGGER trg_family_updated_ts
    BEFORE UPDATE ON family
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

-- ============================================================
-- 2. member
--    A person in the family AND their login (merged — no separate user
--    table). `email` + `google_sub` come from Google sign-in. Both NOT
--    NULL and globally unique: one Google identity = one member.
--    Money is attributed to a member via transaction.member_id; accounts
--    are owned by a member (or shared, member_id NULL on account).
-- ============================================================

CREATE TABLE member (
    member_id   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id   UUID         NOT NULL REFERENCES family(family_id),
    full_name   TEXT         NOT NULL,
    email       TEXT         NOT NULL UNIQUE,
    google_sub  TEXT         NOT NULL UNIQUE,   -- Google's stable subject id
    is_active   BOOLEAN      NOT NULL DEFAULT TRUE,
    created_ts  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_ts  TIMESTAMPTZ
);

CREATE TRIGGER trg_member_updated_ts
    BEFORE UPDATE ON member
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX idx_member_family ON member (family_id) WHERE is_active;

-- ============================================================
-- 3. account
--    A bucket of money: checking / savings / cash / credit_card.
--    Owned by a member, OR shared across the family (member_id NULL).
--    NOT a login — that's `member`.
--
--    Balance model: current_balance = initial_balance + Σ(transactions
--    where value_date > balance_date AND value_date <= today).
-- ============================================================

CREATE TABLE account (
    account_id       UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id        UUID            NOT NULL REFERENCES family(family_id),
    member_id        UUID            REFERENCES member(member_id),  -- NULL = shared
    name             TEXT            NOT NULL,
    kind             account_kind    NOT NULL,
    currency         VARCHAR(3)      NOT NULL DEFAULT 'EUR',
    initial_balance  NUMERIC(14, 2)  NOT NULL DEFAULT 0,
    balance_date     DATE            NOT NULL DEFAULT CURRENT_DATE,
    is_active        BOOLEAN         NOT NULL DEFAULT TRUE,
    created_ts       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_ts       TIMESTAMPTZ
);

CREATE TRIGGER trg_account_updated_ts
    BEFORE UPDATE ON account
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX idx_account_family ON account (family_id)            WHERE is_active;
CREATE INDEX idx_account_member ON account (family_id, member_id) WHERE is_active;

-- ============================================================
-- 4. category
--    Editable category lookup, now per-family (each family curates its
--    own). `grupo` is the coarse bucket: variable | fijo | ingreso | NULL.
-- ============================================================

CREATE TABLE category (
    category_id  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id    UUID         NOT NULL REFERENCES family(family_id),
    name         TEXT         NOT NULL,
    grupo        TEXT,                              -- variable | fijo | ingreso | NULL
    is_active    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_ts   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_ts   TIMESTAMPTZ,
    UNIQUE (family_id, name)
);

CREATE TRIGGER trg_category_updated_ts
    BEFORE UPDATE ON category
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX idx_category_family ON category (family_id) WHERE is_active;

-- ============================================================
-- 5. monthly_budget
--    One default monthly limit per category, per family. Matched to spend
--    by joining transaction.category_id — no text-join, single source of
--    truth is the category table.
-- ============================================================

CREATE TABLE monthly_budget (
    monthly_budget_id UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id         UUID            NOT NULL REFERENCES family(family_id),
    category_id       UUID            NOT NULL REFERENCES category(category_id),
    limit_amount      NUMERIC(12, 2)  NOT NULL CHECK (limit_amount > 0),
    created_ts        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_ts        TIMESTAMPTZ,
    UNIQUE (family_id, category_id)
);

CREATE TRIGGER trg_monthly_budget_updated_ts
    BEFORE UPDATE ON monthly_budget
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

-- ============================================================
-- 6. recurring_charge
--    Definition of a periodic charge (Netflix €12 on the 5th, rent, …).
--    NOT the charges themselves — those are transactions with
--    recurring_charge_id pointing here, generated each cycle.
-- ============================================================

CREATE TABLE recurring_charge (
    recurring_charge_id UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id           UUID            NOT NULL REFERENCES family(family_id),
    account_id          UUID            NOT NULL REFERENCES account(account_id),
    name                TEXT            NOT NULL,
    amount              NUMERIC(12, 2)  NOT NULL CHECK (amount > 0),
    day_of_month        INT             NOT NULL CHECK (day_of_month BETWEEN 1 AND 31),
    category_id         UUID            NOT NULL REFERENCES category(category_id),
    start_date          DATE            NOT NULL DEFAULT CURRENT_DATE,
    end_date            DATE,                                    -- NULL = open-ended
    is_active           BOOLEAN         NOT NULL DEFAULT TRUE,
    created_ts          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_ts          TIMESTAMPTZ
);

CREATE TRIGGER trg_recurring_charge_updated_ts
    BEFORE UPDATE ON recurring_charge
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

CREATE INDEX idx_recurring_charge_active
    ON recurring_charge (family_id, is_active, day_of_month);

-- ============================================================
-- 7. transaction
--    The fact table — every financial event lands here. FKs point OUTWARD
--    to the constructs that may have spawned it.
--
--    Date semantics:
--      transaction_date — when the event happened (when you bought it)
--      value_date       — when money actually moves
--                          · cash/checking: same as transaction_date
--                          · credit_card:   when the statement is paid
--      Only value_date counts toward current_balance.
--
--    LLM fields:
--      llm_confidence — extraction confidence (0..1)
--      llm_raw_output — full LLM JSON for audit / debugging
--
--    Soft-delete: set deleted_ts instead of DELETE; views filter IS NULL.
-- ============================================================

CREATE TABLE transaction (
    transaction_id      UUID                PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id           UUID                NOT NULL REFERENCES family(family_id),
    account_id          UUID                NOT NULL REFERENCES account(account_id),
    member_id           UUID                NOT NULL REFERENCES member(member_id),
    recurring_charge_id UUID                REFERENCES recurring_charge(recurring_charge_id),
    category_id         UUID                NOT NULL REFERENCES category(category_id),
    kind                transaction_kind    NOT NULL,
    amount              NUMERIC(12, 2)      NOT NULL CHECK (amount > 0),
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

-- All hot-path queries are family-scoped, so every index leads with family_id.
CREATE INDEX idx_transaction_account_value
    ON transaction (family_id, account_id, value_date)
    WHERE deleted_ts IS NULL;

CREATE INDEX idx_transaction_member_date
    ON transaction (family_id, member_id, transaction_date)
    WHERE deleted_ts IS NULL;

CREATE INDEX idx_transaction_category
    ON transaction (family_id, category_id)
    WHERE deleted_ts IS NULL;

CREATE INDEX idx_transaction_recurring
    ON transaction (recurring_charge_id)
    WHERE recurring_charge_id IS NOT NULL;

-- ============================================================
-- 8. notification
--    Queue for the Observer agent's proactive nudges. Delivery is now
--    IN-APP (to a member), not Telegram — so the target is member_id,
--    not a chat_id. Generators write rows with a dedupe_key; the
--    dispatcher picks unsent rows and stamps sent_ts. UNIQUE(dedupe_key)
--    makes re-running a generator idempotent.
-- ============================================================

CREATE TABLE notification (
    notification_id      UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id            UUID         NOT NULL REFERENCES family(family_id),
    member_id            UUID         NOT NULL REFERENCES member(member_id),
    kind                 TEXT         NOT NULL,
    title                TEXT         NOT NULL,
    body                 TEXT         NOT NULL,
    scheduled_ts         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    sent_ts              TIMESTAMPTZ,
    error                TEXT,
    related_entity_type  TEXT,
    related_entity_id    UUID,
    dedupe_key           TEXT         UNIQUE,
    created_ts           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_notification_pending
    ON notification (family_id, scheduled_ts)
    WHERE sent_ts IS NULL;

CREATE INDEX idx_notification_kind ON notification (family_id, kind);

-- ============================================================
-- 9. notification_preference
--    Per-member opt-out by notification kind. Defaults to enabled.
-- ============================================================

CREATE TABLE notification_preference (
    notification_preference_id UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id                  UUID         NOT NULL REFERENCES family(family_id),
    member_id                  UUID         NOT NULL REFERENCES member(member_id),
    kind                       TEXT         NOT NULL,
    enabled                    BOOLEAN      NOT NULL DEFAULT TRUE,
    created_ts                 TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_ts                 TIMESTAMPTZ,
    UNIQUE (member_id, kind)
);

CREATE TRIGGER trg_notification_preference_updated_ts
    BEFORE UPDATE ON notification_preference
    FOR EACH ROW EXECUTE FUNCTION fn_set_updated_ts();

-- ============================================================
-- 10. agent_run
--     Observability: one row per chat message run through the agent.
--     Written fire-and-forget so logging never adds latency. member_id
--     is the person who triggered it (NULL for system/Observer runs).
-- ============================================================

CREATE TABLE agent_run (
    agent_run_id  UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    family_id     UUID         NOT NULL REFERENCES family(family_id),
    member_id     UUID         REFERENCES member(member_id),
    session_id    TEXT,
    input_text    TEXT,
    reply_text    TEXT,
    model_used    TEXT,
    tool_calls    JSONB        NOT NULL DEFAULT '[]'::jsonb,
    error         TEXT,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    total_tokens  INTEGER,
    created_ts    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_agent_run_created ON agent_run (family_id, created_ts DESC);
