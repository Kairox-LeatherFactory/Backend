# app/core/celery.py
from celery import Celery
from app.core.config import settings

# UPDATED 2026-09-11 (Hamthan): the Celery worker is a separate PROCESS from
# the API (see main.py's own "Model imports" block) and never runs main.py,
# so it never imported every models.py the way the API does. autodiscover_tasks
# below only imports each module's tasks.py, not its models.py, so tables like
# `client` (app/modules/clients/models.py) were never registered on
# Base.metadata in the worker process. OrderStyle.client_id's string FK
# ForeignKey("client.id") is resolved lazily against Base.metadata the first
# time the ORM configures its mappers, so a real breakdown-persist run in the
# worker died with: "Foreign key associated with column 'order_style.client_id'
# could not find table 'client'". Importing every model module here, exactly
# like main.py does, ensures Base.metadata is fully populated before any task
# body touches the ORM, regardless of which task happens to run first.
from app.modules.users import models as _users              # noqa: F401
from app.modules.employees import models as _employees      # noqa: F401
from app.modules.clients import models as _clients          # noqa: F401
from app.modules.production import models as _production    # noqa: F401
from app.modules.wages import models as _wages               # noqa: F401
from app.modules.attendance import models as _attendance    # noqa: F401
from app.modules.barcode import models as _barcode          # noqa: F401
from app.core import models as _core_models                 # noqa: F401
from app.modules.procurement import models as _procurement  # noqa: F401
from app.modules.bom import models as _bom                  # noqa: F401
from app.modules.inventory import models as _inventory      # noqa: F401
from app.modules.supplier_po import models as _supplier_po  # noqa: F401

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