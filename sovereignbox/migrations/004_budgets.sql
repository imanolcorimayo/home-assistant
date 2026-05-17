CREATE TABLE IF NOT EXISTS monthly_budgets (
    id            SERIAL PRIMARY KEY,
    subcategoria1 TEXT NOT NULL UNIQUE,
    limit_amount  NUMERIC(12,2) NOT NULL CHECK (limit_amount > 0),
    created_at    TIMESTAMPTZ DEFAULT NOW(),
    updated_at    TIMESTAMPTZ DEFAULT NOW()
);
