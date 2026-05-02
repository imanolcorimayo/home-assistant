from celery import Celery

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
)
