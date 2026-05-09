import logging

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_ready

from app.core.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "sovereignbox",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.finance_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="Europe/Rome",
    enable_utc=True,
    # Reintentos automáticos para tareas que fallen por problemas transitorios
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # Schedule: día 15 a las 06:00 Europe/Rome generamos las cuotas del mes.
    # La task es idempotente, así que correrla 1 vez/día (a las 06:00) cubre catch-up
    # si el worker estuvo caído un día.
    beat_schedule={
        "generate-loan-installments": {
            "task": "app.workers.finance_tasks.generate_loan_installments",
            "schedule": crontab(hour=6, minute=0),
        },
        "generate-recurring-charges": {
            "task": "app.workers.finance_tasks.generate_recurring_charges",
            "schedule": crontab(hour=6, minute=5),
        },
        "close-card-statements": {
            "task": "app.workers.finance_tasks.close_card_statements",
            "schedule": crontab(hour=6, minute=10),
        },
        # Notificaciones
        "dispatch-notifications": {
            "task": "app.workers.finance_tasks.dispatch_notifications",
            "schedule": crontab(minute="*/5"),
        },
        "schedule-due-reminders": {
            "task": "app.workers.finance_tasks.schedule_due_reminders",
            "schedule": crontab(hour=9, minute=0),
        },
        "schedule-monthly-summary": {
            "task": "app.workers.finance_tasks.schedule_monthly_summary",
            "schedule": crontab(day_of_month=1, hour=9, minute=0),
        },
        "schedule-daily-agenda": {
            "task": "app.workers.finance_tasks.schedule_daily_agenda",
            "schedule": crontab(hour=8, minute=0),
        },
    },
)


@worker_ready.connect
def _warmup_ollama_on_start(**_kwargs) -> None:
    """Cuando el worker está listo, precarga Ollama para que la 1ª dictada
    no espere los 30-60s del cold start. Si falla, no aborta el arranque."""
    try:
        from app.services import ollama_client
        ollama_client.warm_up()
    except Exception as exc:
        logger.warning("warm-up Ollama omitido: %s", exc)
