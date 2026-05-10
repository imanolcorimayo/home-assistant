# vision-bench

OCR + small text LLM pipeline for extracting structured data from payment-screenshot images. Runs 100% locally; exposed via Cloudflare Tunnel with HTTP Basic Auth for sharing.

## Pipeline

1. **PaddleOCR** reads the screenshot to plain text (~300–600 ms on CPU).
2. **llama3.2:3b** (via Ollama) structures the text into JSON: `kind`, `title`, `amount`, `date`, `currency`.

End-to-end latency: ~5–10 s per image on CPU.

## Run

```bash
# First time: copy .env.example to .env and fill in BASIC_AUTH_PASS + TUNNEL_TOKEN
cp .env.example .env

# Bring it up
docker compose up -d --build

# Pull the LLM into the bench's Ollama (one-time, ~2 GB)
docker compose exec ollama ollama pull llama3.2:3b

# Logs
docker compose logs web cloudflared -f --tail=50
```

UI: `http://localhost:8091` (local) or your Cloudflare hostname (public).

## Files

| File | Purpose |
|---|---|
| `web/app.py` | FastAPI app — auth, `/run` endpoint, OCR + LLM glue |
| `web/static/index.html` | Drop-zone UI, results card |
| `docker-compose.yml` | 3 services: `ollama`, `web`, `cloudflared` |
| `.env` | Secrets — auth password + tunnel token (gitignored) |

## Notes

- The Cloudflare Tunnel route (`<subdomain>.<domain>` → `http://web:8080`) is configured in the Cloudflare Zero Trust dashboard, not in code. The tunnel token in `.env` is what binds this docker stack to that dashboard config.
- Volume names are pinned with explicit `name:` directives to reuse the model cache from the previous standalone-repo location. Safe to remove the `name:` lines if you ever move this elsewhere.
