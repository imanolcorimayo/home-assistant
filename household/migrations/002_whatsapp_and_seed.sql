-- ============================================================
-- 002 — whatsapp source + initial seed
-- ============================================================
-- Manually apply to a running DB:
--   cat migrations/002_whatsapp_and_seed.sql | \
--     docker compose exec -T postgres psql -U household -d household
-- ============================================================

-- Add 'whatsapp' to the transaction_source enum so transactions
-- captured via the WhatsApp webhook have a faithful provenance.
-- ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
ALTER TYPE transaction_source ADD VALUE IF NOT EXISTS 'whatsapp';

-- Minimal seed so the FK-constrained transaction table can accept inserts.
-- Identity-by-sender and account routing come later; for now everything
-- lands on the first family_member and the first (shared) cash account.
INSERT INTO family_member (full_name)
VALUES ('Imanol'), ('Fercho')
ON CONFLICT DO NOTHING;

INSERT INTO account (family_member_id, name, kind, currency)
SELECT NULL, 'Efectivo', 'cash', 'ARS'
WHERE NOT EXISTS (SELECT 1 FROM account);
