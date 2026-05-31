# CLAUDE.md — assistant

El **app unificado** (issue #23). Reemplaza a `household/` (que partía api + web + chat
+ Telegram). Un solo servicio FastAPI, server-rendered con Jinja, **multi-tenant** y con
**login Google**. El chat in-house es el único canal de captura — sin Telegram/WhatsApp.

## Decisiones clave (no re-discutir sin avisar)

- **Multi-tenant**: `family` es el tenant. **Toda** tabla de datos lleva `family_id` y
  **toda** query filtra por él. El `family_id` sale de la **sesión autenticada**, nunca
  de input del usuario/agente.
- **Auth**: `member` = persona **y** login (mergeado, no hay tabla `user` aparte).
  `email` + `google_sub` vienen de Google sign-in. Sesión = cookie firmada.
- **Raw SQL con `asyncpg`** (no ORM). Solo placeholders `$1, $2...` — **nunca** f-string
  ni format de valores en SQL. Todo en `app/db.py` (el análogo a `lib/db.php`).
- **Sin MCP**: el agente llama funciones Python in-process (function calling nativo de
  Gemini/Ollama). El agente **nunca** emite SQL — llama tools tipados que corren writes
  parametrizados pre-escritos.
- `category_id` es la única fuente de verdad para categorías (sin texto libre ni
  subcategorías). Soft-delete (`deleted_ts` / `is_active`), sin hard-delete.

## Estructura

- `app/` — la app FastAPI. `main.py` (entry + lifespan + /health), `db.py` (pool asyncpg),
  `config.py` (lee env en un solo lugar).
- `migrations/` — `schema.sql` (esquema inicial) + futuros `0NN_*.sql`. Postgres los
  auto-corre en orden alfabético en el primer init del volumen.
- `docker-compose.yml` — postgres + app + adminer (sin servicio mcp). Correr **desde
  esta carpeta**.

## Comandos

```bash
cd assistant
docker compose up -d --build      # levantar
curl localhost:8083/health        # liveness + check de schema
# adminer: localhost:8890 · postgres: localhost:5434
docker compose down -v            # tirar todo + borrar volumen (re-aplica schema)
```

## Build sequence (issues)

#25 scaffold (✓) → #26 login Google + bootstrap de familia → #27 chat (orquestador único:
register + consult) → #28 displays/CRUD manual → #29 centro de notificaciones in-app
(web-push PWA diferido) → #30 onboarding guiado. Migración de datos (backfill EUR a la
familia #1) diferida.
