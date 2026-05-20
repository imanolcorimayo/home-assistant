#!/usr/bin/env bash
# ============================================================
# Import the sovereignbox finance data into the local household DB.
#
#   1. Loads the dump into a throwaway `sbox_src` schema (staging).
#   2. Applies migrations/003_categories.sql (category table + FK).
#   3. Runs scripts/import_sovereignbox.sql (transform + load).
#   4. Prints row counts, then drops the staging schema.
#
# Re-runnable: the transform uses ON CONFLICT DO NOTHING.
# Run from the household/ project root:  ./scripts/import_sovereignbox.sh
#
# Env overrides: DB_CONTAINER, DB_USER, DB_NAME, PGPASSWORD, DUMP,
#                KEEP_STAGING=1 (don't drop sbox_src at the end).
# ============================================================
set -euo pipefail

DB_CONTAINER="${DB_CONTAINER:-household_db}"
DB_USER="${DB_USER:-household}"
DB_NAME="${DB_NAME:-household}"
export PGPASSWORD="${PGPASSWORD:-household123}"
DUMP="${DUMP:-.local/sovereignbox_finance_dump.sql}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DUMP_PATH="$ROOT/$DUMP"
[ -f "$DUMP_PATH" ] || { echo "dump not found: $DUMP_PATH"; exit 1; }

psql() { docker exec -i -e PGPASSWORD="$PGPASSWORD" "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" "$@"; }

echo "==> 1/4  build staging schema sbox_src from the dump"
psql -v ON_ERROR_STOP=1 -q \
  -c "DROP SCHEMA IF EXISTS sbox_src CASCADE" \
  -c "CREATE SCHEMA sbox_src" \
  -c "CREATE DOMAIN sbox_src.user_role          AS text" \
  -c "CREATE DOMAIN sbox_src.document_type       AS text" \
  -c "CREATE DOMAIN sbox_src.document_status      AS text" \
  -c "CREATE DOMAIN sbox_src.shopping_item_status AS text" \
  -c "CREATE DOMAIN sbox_src.task_status          AS text" \
  -c "CREATE DOMAIN sbox_src.task_recurrence       AS text" \
  -c "CREATE FUNCTION sbox_src.fn_set_updated_at() RETURNS trigger AS \$\$ BEGIN RETURN NEW; END; \$\$ LANGUAGE plpgsql"
# Redirect the dump's public.* objects into sbox_src (verified: 'public.' never appears in the data).
sed 's/public\./sbox_src./g' "$DUMP_PATH" | psql -q >/dev/null 2>&1 || true

echo "==> 2/4  apply migrations/003_categories.sql"
psql -v ON_ERROR_STOP=1 -q < "$ROOT/migrations/003_categories.sql"

echo "==> 3/4  transform + load"
psql -v ON_ERROR_STOP=1 -q < "$ROOT/scripts/import_sovereignbox.sql"

echo "==> 4/4  row counts in household"
psql -tA -c "
  SELECT 'family_member', count(*) FROM family_member
  UNION ALL SELECT 'account',        count(*) FROM account
  UNION ALL SELECT 'monthly_budget', count(*) FROM monthly_budget
  UNION ALL SELECT 'category',       count(*) FROM category
  UNION ALL SELECT 'transaction',    count(*) FROM transaction
  ORDER BY 1"

if [ "${KEEP_STAGING:-0}" != "1" ]; then
  psql -q -c "DROP SCHEMA IF EXISTS sbox_src CASCADE"
  echo "    (staging schema sbox_src dropped; set KEEP_STAGING=1 to keep it)"
fi
echo "done."
