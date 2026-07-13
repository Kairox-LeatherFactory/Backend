# app/core/celery.py (continued) or app/core/beat_schedule.py
from celery.schedules import crontab
from app.core.celery import celery_app

celery_app.conf.beat_schedule = {
    "freight-risk-scan": {
        "task": "app.modules.bom.tasks.scan_freight_risk",
        "schedule": crontab(minute=0, hour="*/2"),
    },
    "notification-escalation": {
        "task": "app.modules.bom.tasks.escalate_review_notifications",
        "schedule": crontab(minute="*/15"),
    },
}