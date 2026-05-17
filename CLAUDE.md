# CLAUDE.md — root

Este repo es un **monorepo**. La raíz no es un proyecto — agrupa proyectos independientes. Cada subcarpeta tiene su propio `CLAUDE.md` con las instrucciones de ese proyecto.

## Proyectos

- [`sovereignbox/CLAUDE.md`](./sovereignbox/CLAUDE.md) — bot Telegram + dashboard FastAPI + LLM local. Es el código vivo en producción y la referencia de la que se está migrando.
- [`household/`](./household/) — scaffolding del próximo asistente (arquitectura de 3 agentes, ver root `README.md`). Sin código aún.
- [`vision-bench/`](./vision-bench/) — benchmark de modelos de visión. Independiente, propio compose.

## Reglas que aplican a todo el monorepo

- **Un compose por proyecto**, en la carpeta del proyecto. **Sin `docker-compose.yml` en la raíz.**
- Comandos de cada proyecto se corren *desde su carpeta* (`cd sovereignbox && docker compose ...`), no desde la raíz.
- Cuando se trabaje en un proyecto puntual, leer su `CLAUDE.md` antes de cambiar nada — sobreescribe lo que diga este archivo.
- `sovereignbox/` es **referencia congelada-ish** mientras se construye `household/`. Cambios en `sovereignbox/` se aceptan sólo si la familia los necesita ya — refactors van a `household/`.

## Vision-level

Ver `README.md` en la raíz para visión del producto y la arquitectura de 3 agentes.
