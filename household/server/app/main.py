import logging
import os
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.database import engine
from app.routers import (
    agent,
    telegram,
    telegram_consultant,
    telegram_director,
    telegram_observer,
    whatsapp,
)
from app.services import notifications, observer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")


# Daily generators run staggered between 09:00 and 09:15 so they don't all
# hit the DB at the same instant and so the dispatcher (every 5 min) has a
# clean window to start picking them up. Weekly summary fires Sunday 19:00.
# Timezone Europe/Rome matches the family.
#
# IMPORTANT: this assumes a single uvicorn worker. If we ever run >1
# workers, the scheduler will fire N times — switch to APScheduler with
# SQLAlchemyJobStore or move the scheduler to its own process.
_SCHEDULER_TZ = os.environ.get("OBSERVER_TZ", "Europe/Rome")


@asynccontextmanager
async def lifespan(app: FastAPI):
    scheduler = AsyncIOScheduler(timezone=_SCHEDULER_TZ)

    scheduler.add_job(observer.generate_budget_alerts,        CronTrigger(hour=9, minute=0),
                      id="budget_alerts", replace_existing=True)
    scheduler.add_job(observer.generate_recurring_reminders,  CronTrigger(hour=9, minute=5),
                      id="recurring_reminders", replace_existing=True)
    scheduler.add_job(observer.generate_recurring_overdue,    CronTrigger(hour=9, minute=5),
                      id="recurring_overdue", replace_existing=True)
    scheduler.add_job(observer.generate_inactivity_alerts,    CronTrigger(hour=9, minute=10),
                      id="inactivity", replace_existing=True)
    scheduler.add_job(observer.generate_unusual_tx_alerts,    CronTrigger(hour=9, minute=15),
                      id="unusual_tx", replace_existing=True)
    scheduler.add_job(observer.generate_weekly_summary,
                      CronTrigger(day_of_week="sun", hour=19, minute=0),
                      id="weekly_summary", replace_existing=True)
    scheduler.add_job(notifications.dispatch_pending_notifications,
                      IntervalTrigger(minutes=5),
                      id="dispatcher", replace_existing=True)

    scheduler.start()
    log.info("scheduler started (tz=%s, jobs=%d)", _SCHEDULER_TZ, len(scheduler.get_jobs()))
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)
        log.info("scheduler stopped")


app = FastAPI(title="household", lifespan=lifespan)


@app.middleware("http")
async def no_store(request, call_next):
    # MVP test tool: never cache, so UI edits show up immediately on the
    # phone instead of being masked by Safari / Cloudflare edge caching.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(whatsapp.router)
app.include_router(telegram.router)
app.include_router(telegram_consultant.router)
app.include_router(telegram_director.router)
app.include_router(telegram_observer.router)
app.include_router(agent.router)


@app.get("/health")
async def health():
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"db unreachable: {exc}")
    return {"status": "ok"}


# Mount LAST: a StaticFiles mount at "/" matches every path, so it must be
# registered after all explicit routes (/webhook, /api, /health) — those are
# checked first; only unmatched paths fall through to serve the chat UI.
# Guarded by isdir since the dir is a compose bind-mount (absent in tests).
_chat_dir = "/code/chat"
if os.path.isdir(_chat_dir):
    app.mount("/", StaticFiles(directory=_chat_dir, html=True), name="chat_static")
