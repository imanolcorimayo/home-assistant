# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SovereignBox is a private family financial assistant running 100% locally via Docker. A Telegram bot receives text/voice messages, transcribes them with Whisper, extracts financial transactions via a local LLM (qwen2.5:3b via Ollama), and persists them to PostgreSQL. The family is Italian, expenses are in EUR, and messages are in Spanish.

**Product vision and V1 scope:** see the "Visión" section in `README.md`. Telegram = capture + notify; dashboard = see + configure. V1 is "family of 4 uses it daily for 3 months without code changes" — explicitly excludes loans (#7), savings goals (#8), multi-tenant, mobile, open-source.

## Running the Project

```bash
# Start everything (first time or after config changes)
docker compose up --build -d

# Start with Metabase analytics dashboard
docker compose --profile monitoring up --build -d

# After changing code in app/
docker compose build api worker && docker compose up -d api worker

# After changing only webhook/routers (no worker changes)
docker compose build api && docker compose up -d api

# View logs
docker compose logs api worker -f --tail=50

# DB admin UI — server: db, user: sovereign, pass from $POSTGRES_PASSWORD (.env; default `sovereign123` for local dev)
open http://localhost:8888
```

## Running Migrations

Migrations are plain SQL files in `migrations/`, applied manually in order:

```bash
cat migrations/004_budgets.sql | docker compose exec -T postgres psql -U sovereign -d sovereignbox
```

Migration order: `schema.sql` → `002_new_categories.sql` → `003_schema_improvements.sql` → `004_budgets.sql`

## Testing LLM Extraction

```bash
docker compose exec worker python -c "
from app.services.ollama_client import extract_transactions
print(extract_transactions('gasto de 25 euros en supermercado'))
"
```

## Architecture

### Data Model Dimensions

The schema is split into 6 dimensions, deliberately uneven. **Rule of thumb: Plata is power, everything else is ergonomics.** Deepen `transactions` / `accounts` / `loans` etc. when it adds real value; keep `events` / `tasks` / `shopping` / `attachments` / `notifications` lean — making them deeper turns the app into Asana, which it shouldn't be.

| Dimension | Tables | Weight |
|---|---|---|
| 🏛️ Personas (family) | `family_members` | XS (1) |
| 💰 Plata (financial) | `transactions`, `accounts`, `monthly_budgets`, `recurring_charges`, `card_statements`, `installment_plans`, `loans` | **XL (7)** |
| 🗓️ Tiempo (events/tasks) | `events`, `tasks` | S (2) |
| 🛒 Listas (shopping) | `shopping_items`, `shopping_list_items` | XS (1-2) |
| 📎 Documentos | `attachments`, `documents` | S (2) |
| ⚙️ Operacional | `notifications`, `user_preferences` | S (2) |

### Message Flow

```
Telegram → POST /webhook/telegram (FastAPI, <3s response)
  ├── callback_query  → _handle_callback_query() — undo/confirm/cancel buttons
  ├── /command        → _handle_command() — sync DB queries, immediate response
  ├── pending_tx?     → _handle_confirmation() → save_pending_transaction.delay()
  ├── voice/audio     → process_audio_message.delay()
  └── text            → process_text_message.delay()

Celery Worker (async):
  process_audio_message → Whisper transcribe → process_text_message.delay()
  process_text_message  → Ollama extract → split by confidence
    ├── confidence ≥ 0.75 → _save_transaction() + send ✅ with ↩️ undo button
    └── confidence < 0.75 → Redis pending_tx:{chat_id} (TTL 300s) + inline keyboard
```

### Confidence & Pending Transaction Flow

- Threshold `0.75` is hardcoded in `app/workers/finance_tasks.py`.
- Uncertain transactions stored as `LLMTransactionListOutput` JSON in Redis key `pending_tx:{chat_id}`.
- Resolved via: (a) inline keyboard callback_query `"confirm"/"cancel"`, or (b) text fallback `"si"/"no"`.
- Undo buttons encode the transaction UUID: `callback_data = f"undo:{tx_id}"`.

### Sync vs Async Split

- `telegram_client.py` has two versions of each function: async (used from FastAPI routes) and `_sync` suffix (used from Celery workers via `SyncSessionLocal`).
- Never use async functions from Celery tasks — they will deadlock.
- `app/core/database.py` exports both `AsyncSession` factory (FastAPI) and `SyncSessionLocal` (Celery).

### LLM Prompt

`_EXTRACTION_PROMPT` in `app/services/ollama_client.py` is the single source of truth for transaction classification. It contains:
- The 3-level expense hierarchy (Entradas / Gastos Fijos / Gastos variables → subcategoria1 → subcategoria2)
- Disambiguation rules (e.g., "Dacia" + cuota → Prestamos; "Dacia" + nafta → Transporte)
- ~18 examples covering nominal forms ("gasto de X€"), verbal forms ("gasté X€"), and Whisper transcription typos
- `format: "json"` enforces JSON output; `temperature: 0.1` for determinism

When adding categories, update **both** the MAPA and EJEMPLOS sections. Use `{{}}` to escape literal braces in the f-string template.

### Database

- All analytics use views defined in `migrations/003_schema_improvements.sql`: `v_balance_mensual`, `v_gastos_variables`, `v_ingresos`, `v_gastos_fijos`, `v_resumen_mensual`, `v_tendencia_subcategoria1`.
- All views filter `WHERE deleted_at IS NULL` — transactions are soft-deleted only.
- `monthly_budgets.subcategoria1` must match view casing (Title Case). Normalize on insert: `sub1[0].upper() + sub1[1:]`.

### Webhook Idempotency

Redis key `processed_update:{update_id}` (TTL 24h) deduplicates Telegram retries. The webhook always returns HTTP 200; errors are logged, never re-raised.

### RAM Constraints

Machine has 7.5GB RAM. Ollama uses 3GB, worker 2GB. Do not add heavy dependencies to the worker image. Celery runs with `--concurrency=1`.

## Key Files

| File | Purpose |
|---|---|
| `app/routers/webhook.py` | All Telegram command handlers and callback logic |
| `app/services/ollama_client.py` | LLM prompt + transaction extraction |
| `app/workers/finance_tasks.py` | Celery tasks: audio, text, save |
| `app/services/telegram_client.py` | Telegram API wrappers (sync + async) |
| `app/models/finance.py` | SQLAlchemy ORM: FamilyMember, Transaction, MonthlyBudget |
| `app/schemas/finance.py` | Pydantic: TelegramUpdate (with callback_query), LLMTransactionOutput |
| `app/core/auth.py` | HTTP Basic Auth middleware. Active when `BASIC_AUTH_USER`+`BASIC_AUTH_PASS` are set; allowlists `/webhook/*` and `/health`. |
| `migrations/003_schema_improvements.sql` | All analytics views |

## Environment

Required in `.env` (see `.env.example`):
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `OLLAMA_MODEL` (default: `qwen2.5:3b`)
- `WHISPER_MODEL` (default: `small`)
- `POSTGRES_PASSWORD` (default: `sovereign123` — change before exposing publicly; only applied on first volume init, otherwise `ALTER USER`)
- `BASIC_AUTH_USER` / `BASIC_AUTH_PASS` (optional; enables HTTP Basic on the dashboard. Empty = disabled)

Webhook must be registered with `allowed_updates: ["message", "callback_query"]`:
```bash
curl -X POST "https://api.telegram.org/bot$TOKEN/setWebhook" \
  -d "url=YOUR_NGROK_URL/webhook/telegram&allowed_updates=[\"message\",\"callback_query\"]"
```
