# home-assistant — monorepo

Monorepo de proyectos para asistencia familiar y experimentos relacionados. La raíz **no** es un proyecto — sólo agrupa. Cada subcarpeta es un proyecto con sus propios `Dockerfile`, `docker-compose.yml` y docs.

## Proyectos

| Carpeta | Qué es | Estado |
|---|---|---|
| [`sovereignbox/`](./sovereignbox/) | Bot de Telegram + dashboard web + LLM local para gastos familiares. Funcionando hoy en producción. | **Vivo** — referencia |
| [`household/`](./household/) | Re-build del asistente familiar alrededor de la arquitectura de 3 agentes (ver abajo). Web + server separados. | **Scaffolding** |
| [`vision-bench/`](./vision-bench/) | Benchmark de modelos de visión sobre tickets/boletas. Independiente del resto. | **Vivo** |

## Visión

Asistente familiar always-on que captura gastos + eventos de vida en el momento que ocurren, los devuelve como insight, y reduce a casi cero las decisiones admin del día a día.

**V1 — definición de "listo":**
> *Una familia (4 personas) usa la app a diario durante 3 meses para gastos, tareas, agenda y compras, sin que el dueño tenga que tocar código.*

**Fuera de V1:** deudas, ahorro / inversiones, multi-tenant, app mobile, release open-source. Eso es V2+.

## Arquitectura de IA — 3 agentes, 1 modelo

El nuevo proyecto (`household/`) se piensa como **3 agentes con roles distintos**, pero **comparten infraestructura** (mismo cliente LLM, misma DB, mismo worker). No son 3 microservicios — son **3 prompts y 3 puntos de entrada** sobre la misma plomería. Esta separación es mental/funcional, no de despliegue.

| Agente | Rol | Entrada → Salida | Estado |
|---|---|---|---|
| 🎯 **Orquestador** | Recibe input y lo guarda donde corresponde | Mensaje (texto/voz) → fila en la dimensión correcta | En `sovereignbox`, parcial — sólo extrae gastos. |
| 💬 **Consultor** | Responde preguntas sobre los datos | Pregunta NL → respuesta humana (apoyada en vistas SQL) | Falta. **Gap más grande.** |
| 👁️ **Observador** | Mira patrones y notifica proactivamente | Estado de la DB → fila en `notifications` | Falta. |

**Por qué importa esta forma:**
- Cada agente tiene un prompt chico y enfocado, en vez de un megaprompt que hace de todo.
- Permite probar modelos distintos por rol (ej: qwen local para Orquestador, Gemini para Consultor).
- Cualquiera que toque el repo trabaja contra **esta** división — no contra una arquitectura de servicios.

**Lo que NO es:**
- No son 3 procesos / contenedores / colas distintas.
- No es un framework multi-agente con orquestación entre ellos.

## Cómo trabajar en este repo

Cada proyecto se levanta desde su propia carpeta:

```bash
cd sovereignbox && docker compose up -d        # asistente actual
cd household    && docker compose up -d        # cuando exista
cd vision-bench && docker compose up -d        # benchmark de visión
```

No hay `docker-compose.yml` en la raíz. Cada proyecto tiene su propio stack y no comparte infra con los otros.

Para detalles de cada proyecto, ver su `README.md` y `CLAUDE.md`.
