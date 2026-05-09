# SovereignBox

Asistente familiar 100 % local: bot de Telegram + dashboard web + LLM + Whisper, todo en Docker. Stack: FastAPI · Celery · PostgreSQL · Redis · Ollama (qwen2.5:3b) · Whisper. Documentación de uso y arquitectura: `CLAUDE.md`.

## Servicios

| Servicio | Puerto | Descripción |
|---|---|---|
| `api` | 8080 | FastAPI — backend + dashboard SPA |
| `worker` | — | Celery (worker + beat embebido) |
| `postgres` | 5432 | PostgreSQL 16 |
| `redis` | 6379 | broker de Celery + caché |
| `ollama` | 11434 | LLM local (qwen2.5:3b por default) |
| `adminer` | 8888 | DB admin UI |
| `backup` | — | `pg_dump` diario 03:00 + tar de `media_data` |
| `metabase` | 3000 | BI (con `--profile monitoring`) |

## Requisitos

- Docker Engine ≥ 24, Docker Compose ≥ 2.20
- 8 GB RAM mínimo (Ollama 3 GB, worker 2 GB).

## Levantar el sistema

```bash
cp .env.example .env       # configurar TELEGRAM_BOT_TOKEN, etc.
docker compose up --build -d
docker compose --profile monitoring up --build -d   # opcional: Metabase
```

URLs: dashboard http://localhost:8080 · Adminer http://localhost:8888 (server `db`, user/pass `sovereign`/`sovereign123`).

## Migraciones

Aplicar en orden contra `postgres`:

```bash
for m in migrations/*.sql; do
  echo "→ $m"
  cat "$m" | docker compose exec -T postgres psql -U sovereign -d sovereignbox
done
```

Las migraciones son idempotentes (`CREATE … IF NOT EXISTS`, `INSERT … WHERE NOT EXISTS`).

## Backups automáticos

El servicio `backup` corre `pg_dump` cada noche a las 03:00 UTC y guarda en el volumen Docker `db_backups`. Incluye también un tar de `media_data` (attachments). Retención por default 30 días — cambiable con `RETENTION_DAYS` en `docker-compose.yml`.

### Verificar backups

```bash
docker compose exec backup ls -lh /backups
docker compose logs backup --tail=20
```

### Restore manual

```bash
# Listar dumps disponibles
docker compose exec backup ls /backups

# Copiar al host (opcional)
docker compose cp backup:/backups/db-YYYYMMDD-HHMMSS.sql.gz ./

# Restaurar (CUIDADO: pisa datos actuales)
gunzip -c db-YYYYMMDD-HHMMSS.sql.gz | docker compose exec -T postgres psql -U sovereign -d sovereignbox

# Restaurar media_data
docker compose cp backup:/backups/media-YYYYMMDD-HHMMSS.tar.gz ./
docker run --rm -v home-assistant_media_data:/data -v "$PWD":/host alpine \
  sh -c "cd /data && tar -xzf /host/media-YYYYMMDD-HHMMSS.tar.gz"
```

## Modelo de saldos (importante)

Cada cuenta tiene `saldo_inicial` declarado a una `saldo_fecha`. **El saldo actual computa solamente las transacciones con `fecha_valor > saldo_fecha`**. Esto evita doble-conteo cuando declarás un saldo inicial.

Si cargás transacciones retroactivas (`transaction_date < saldo_fecha`), no afectarán el saldo actual. Para recalcular el saldo desde el primer movimiento histórico:

- Web: pantalla **Cuentas** → botón **Recalcular saldo**.
- API: `POST /api/cuentas/{id}/recalcular-saldo` ajusta `saldo_fecha = MIN(transaction_date) - 1`.

## Comandos útiles

```bash
# Logs
docker compose logs api worker -f --tail=50

# Probar el LLM
docker compose exec worker python -c "
from app.services.ollama_client import extract_transactions
print(extract_transactions('gasté 25€ en supermercado'))
"

# Health del API
curl http://localhost:8080/health
```

## Estructura

| Directorio | Contenido |
|---|---|
| `app/routers/` | Endpoints FastAPI (webhook Telegram, dashboard) |
| `app/services/` | LLM, Whisper, Telegram, generadores automáticos |
| `app/workers/` | Celery tasks + beat schedule |
| `app/models/` | SQLAlchemy ORM |
| `app/schemas/` | Pydantic v2 |
| `migrations/` | SQL idempotente, en orden numérico |
| `scripts/` | Helpers (backup-loop, etc.) |
| `app/static/` | Dashboard SPA (Alpine.js + Chart.js) |

## Detener

```bash
docker compose down                 # mantiene volúmenes
docker compose down -v              # ⚠️ borra DB + media + backups
```
