# SovereignBox

Asistente familiar 100 % local: bot de Telegram + dashboard web + LLM + Whisper, todo en Docker. Stack: FastAPI · Celery · PostgreSQL · Redis · Ollama (qwen2.5:3b) · Whisper. Documentación de uso y arquitectura: `CLAUDE.md`.

## Visión

Asistente familiar always-on que captura gastos + eventos de vida en el momento que ocurren, los devuelve como insight, y reduce a casi cero las decisiones admin del día a día.

**Reparto de canales:**

| | Telegram | Dashboard |
|---|---|---|
| Rol | Boca y oídos — captura + notifica | Ojos y manos — ver + configurar |
| Cuándo | Mientras vivís ("gasté 30 €") | Cuando te sentás a pensar |
| Fortalezas | Voz, instantáneo, mobile, push | Charts, historial, bulk ops, settings |
| Lo que vive acá | Inputs · confirmaciones · alertas · recordatorios · undo del último | Dashboards · historial · presupuestos · agenda · ahorros · categorías · parámetros |

Regla: si requiere más de 2 taps en Telegram, va al dashboard. Si es "¿acaba de pasar?", va a Telegram.

### V1 — definición de "listo"

> *La familia (4 personas) usa la app a diario durante 3 meses para gastos, tareas, agenda y compras, sin que el dueño tenga que tocar código.*

Bar concreto:
- Los 4 registran transacciones, tareas, eventos y compras vía Telegram en <5 s
- Dashboard muestra balance del mes, agenda, tareas pendientes, lista de compras
- Página de parámetros (issue #6) · categorías editables (issue #4) · ack "trabajando…" en Telegram (issue #5)

**Fuera de V1:** deudas (#7), ahorro / inversiones (#8), multi-tenant, app mobile, release open-source. Esos son V2+.

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

URLs: dashboard http://localhost:8080 · Adminer http://localhost:8888 (server `db`, user `sovereign`, password = `POSTGRES_PASSWORD` de `.env`).

## Seguridad / exposición pública

El stack está pensado para correr en una LAN o detrás de un túnel privado. Si lo exponés a internet (Cloudflare Tunnel, ngrok, etc.):

- **Cambiá `POSTGRES_PASSWORD`** en `.env` antes del primer `docker compose up`. Si la DB ya está inicializada, además hay que correr `ALTER USER sovereign WITH PASSWORD '...'` dentro de Postgres — el env var sólo se aplica en la primera inicialización del volumen.
- **Activá Basic Auth** en el dashboard seteando `BASIC_AUTH_USER` y `BASIC_AUTH_PASS` en `.env`. Reiniciar el `api` (`docker compose up -d api`) — `/webhook/*` y `/health` quedan abiertos para que Telegram y los probes sigan funcionando.
- **Cloudflare Access** (gating a nivel edge, sin pasar por el server) es lo recomendado como capa primaria — se configura en el dashboard de Zero Trust, no en este repo.

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

## Otras apps en este repo

Este repo es un mini-monorepo. Además de SovereignBox (en la raíz), contiene:

| App | Path | Descripción |
|---|---|---|
| vision-bench | `apps/vision-bench/` | Extractor de capturas de pago (PaddleOCR + llama3.2:3b) expuesto vía Cloudflare Tunnel |

Cada app tiene su propio `docker-compose.yml` y `.env`, y se levanta de forma independiente desde su directorio.
