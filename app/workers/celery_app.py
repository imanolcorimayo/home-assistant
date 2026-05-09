from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

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
    },
)
