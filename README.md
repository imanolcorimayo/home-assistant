# SovereignBox AI — Family Lab Edition

Asistente local, privado y multimodal de gestión familiar y documental.
Corre 100% en local bajo Docker Compose. Canal de entrada: Telegram Bot.

## Servicios

| Servicio | Puerto | Descripción |
|---|---|---|
| `api` | 8080 | FastAPI — Backend principal |
| `postgres` | 5432 | PostgreSQL 16 — Base de datos |
| `redis` | 6379 | Redis 7 — Broker de Celery + caché |
| `ollama` | 11434 | Modelos LLM locales (Llama 3, LLaVA) |
| `metabase` | 3000 | BI & Analytics (conectado a PostgreSQL) |

## Requisitos

- Docker Engine >= 24
- Docker Compose >= 2.20
- 8 GB RAM mínimo (16 GB recomendado para Ollama)

## Levantar el proyecto

```bash
# 1. Clonar el repositorio
git clone <repo-url>
cd sovereignbox

# 2. Copiar y ajustar las variables de entorno
cp .env.example .env
# Editar .env con los valores reales (tokens de Telegram, etc.)

# 3. Construir e iniciar todos los servicios
docker compose up --build -d

# 4. Verificar que todo esté corriendo
docker compose ps

# 5. Aplicar el schema de base de datos
docker compose exec postgres psql -U sovereign -d sovereignbox -f /migrations/schema.sql
```

## Verificar el API

```bash
curl http://localhost:8080/health
# {"status":"ok"}
```

## Detener el proyecto

```bash
docker compose down
# Para borrar también los volúmenes (⚠️ borra los datos):
docker compose down -v
```

## Estructura del proyecto

```
sovereignbox/
├── docker-compose.yml
├── .env                    # secrets — nunca en git
├── app/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py
│   ├── core/               # config y database engine
│   ├── routers/            # endpoints por módulo
│   ├── services/           # clientes de Ollama, Whisper, Telegram
│   ├── workers/            # tareas Celery
│   ├── models/             # SQLAlchemy ORM
│   └── schemas/            # Pydantic v2
├── migrations/
│   └── schema.sql
└── data/
    └── files/              # almacenamiento físico de documentos
```
