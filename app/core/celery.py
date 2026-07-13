# app/core/celery.py
from celery import Celery
from app.core.config import settings

celery_app = Celery(
    "kairox",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_time_limit=300,
    task_soft_time_limit=270,
)
# Autodiscover tasks from every module that has a tasks.py
celery_app.autodiscover_tasks([
    "app.modules.bom",
    "app.modules.production",       # future: freight scan lives here
    # add modules as they gain tasks
])