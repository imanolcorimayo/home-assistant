-- Migración 002: reemplaza el enum category por jerarquía categoria/subcategoria1/subcategoria2
-- Aplicar con: docker exec -i sovereignbox_db psql -U sovereign -d sovereignbox < migrations/002_new_categories.sql

ALTER TABLE transactions
    DROP COLUMN IF EXISTS category,
    ADD COLUMN IF NOT EXISTS categoria     TEXT,
    ADD COLUMN IF NOT EXISTS subcategoria1 TEXT,
    ADD COLUMN IF NOT EXISTS subcategoria2 TEXT;

DROP TYPE IF EXISTS transaction_category;
