# household

The next version of the family assistant — rebuilt around the 3-agent architecture (Orchestrator / Consultant / Observer) documented in the repo root `README.md`.

Status: **scaffolding**. Nothing runs yet. The working reference is `../sovereignbox/`.

## Planned layout

```
household/
├── web/                  # frontend (browser-facing)
├── server/               # backend (API + workers + agents)
└── docker-compose.yml    # shared infra (Postgres, Redis, Ollama)
```

`web/` and `server/` will be added when there's code to put in them. Until then this folder is intentionally empty.

## Why "household"

The group of people who share a home and the daily running of it. See the suggestion-history conversation in the repo for the full picking process.
