# tasks.py
"""
================================================================================
modules/bom/tasks.py — Celery task layer (heavy + scheduled background work)
================================================================================

SCOPE
    The two reasons Celery + Beat enter this system (NOT scale):
      1. LATENCY DECOUPLING — Gemini-vision extraction is 5-30s + retries. Running
         it inside the HTTP request blocks a worker for that whole window. The
         extraction task moves it off the request; the router returns a job id and
         the result is pushed back over Supabase Realtime.
      2. SCHEDULED JOBS (Beat) — freight-risk deadline alerts and the 2-hour
         notification-escalation are periodic. They have no request to hang on.

WHY THIS IS SAFE TO BOLT ON NOW
    The generate path is already (a) idempotent — generate_for_order short-circuits
    on an existing submission BOM, so an at-least-once redelivery / autoretry can't
    double-mint — and (b) typed-return, so the result serialises cleanly across the
    broker. Those two prerequisites are exactly the #13 + #3/#4 work that already
    landed in service.py.

GOTCHAS ENCODED BELOW
    * async-in-celery: tasks are sync; we drive the async service via asyncio.run
      on a fresh event loop per task (the worker process has no running loop).
    * session-per-task: a Celery worker is a different PROCESS from the API — it
      cannot reuse the request's AsyncSession. Each task opens its own.
    * DON'T ship raw bytes through the broker: a multi-MB PDF in the message body
      bloats Redis and every log line. Pass the STORAGE KEY; the task re-fetches the
      bytes from object storage. (b64 fallback kept only for tiny payloads / tests.)
    * acks_late + retry: combined with the idempotency guard, a crash mid-task is
      safe to redeliver.

WIRING CHECKLIST (the parts that live OUTSIDE this file)
    settings   : celery_broker_url, celery_result_backend (Redis/Upstash), and the
                 existing async DB url. Add `AsyncSessionLocal` to app.core.db
                 if not already exported.
    processes  : `celery -A app.modules.bom.tasks.celery_app worker -l info`
                 `celery -A app.modules.bom.tasks.celery_app beat   -l info`
    router     : POST /boms/generate enqueues generate_bom_for_submission.delay(...)
                 and returns 202 {job_id}; the frontend subscribes to Realtime on
                 the submission/job channel for completion.
    realtime   : implement _push_realtime() against your Supabase channel.
    beat hooks : implement the bodies of scan_freight_risk / escalate_review_*.
================================================================================
"""
from __future__ import annotations

import asyncio
import base64
import logging
import uuid
from types import SimpleNamespace
from typing import Any

from celery import Celery
from celery.schedules import crontab
from app.core.celery import celery_app

from app.core.config import settings

logger = logging.getLogger(__name__)




# ── async plumbing ──────────────────────────────────────────────────────────
def _run_async(coro):
    """Drive an async coroutine to completion from a sync Celery task. The worker
    process has no running event loop, so asyncio.run owns one for the task's life."""
    return asyncio.run(coro)


async def _with_session(fn):
    """Open a fresh AsyncSession (worker process ≠ API process), run fn(db), and
    ensure it's closed. Commit/rollback are owned by the service inside fn."""
    from app.core.database import AsyncSessionLocal  # exported by your db module

    async with AsyncSessionLocal() as db:
        return await fn(db)


def _fetch_bytes(*, storage_key: str | None, b64: str | None) -> bytes | None:
    """Prefer the storage key (don't move multi-MB blobs through the broker); fall
    back to an inline base64 payload for tiny inputs / tests."""
    if storage_key:
        from app.core.storage import get_storage
        return get_storage().get(storage_key)
    if b64:
        return base64.b64decode(b64)
    return None


async def _push_realtime(channel: str, event: str, payload: dict[str, Any]) -> None:
    """Push a completion/failure event to the frontend over Supabase Realtime.
    TODO: implement against your Realtime client. Best-effort — a push failure must
    never fail the task (the BOM is already persisted; the UI can poll as a fallback)."""
    try:
        # from app.core.realtime import get_realtime
        # await get_realtime().broadcast(channel, event, payload)
        logger.info("realtime push [%s/%s]: %s", channel, event, payload)
    except Exception:
        logger.exception("realtime push failed for %s/%s", channel, event)


# ══════════════════════════════════════════════════════════════════════════════
# HEAVY TASK — extraction + BOM generation off the request path.
# ══════════════════════════════════════════════════════════════════════════════
@celery_app.task(
    bind=True,
    name="app.modules.bom.tasks.generate_bom_for_submission",
    max_retries=2,
    default_retry_delay=10,
)
def generate_bom_for_submission(
    self,
    *,
    user_id: str | None,
    submission_id: str,
    client_id: str | None,
    spec_storage_key: str | None = None,
    spec_b64: str | None = None,
    spec_filename: str = "spec",
    spec_type: str | None = None,
    client_match_code: str | None = None,
    order_storage_key: str | None = None,
    order_b64: str | None = None,
    order_filename: str | None = None,
    order_mime: str | None = None,
    order_match_code: str | None = None,
    source_document_id: str | None = None,
) -> dict[str, Any]:
    """Run the Stage-2 generate pipeline as a background job. The router enqueues
    this with storage keys (not raw bytes) and returns a job id; the result is
    pushed over Realtime on the submission channel. Safe to autoretry — the
    service's idempotency guard returns the existing BOM on redelivery."""
    spec_bytes = _fetch_bytes(storage_key=spec_storage_key, b64=spec_b64)
    order_bytes = _fetch_bytes(storage_key=order_storage_key, b64=order_b64)
    if not spec_bytes:
        # Nothing to extract — don't retry a structurally-impossible job.
        result = {"error": "spec_bytes_missing", "submission_id": submission_id}
        _run_async(_push_realtime(f"submission:{submission_id}", "bom_failed", result))
        return result

    async def _job(db):
        from app.modules.procurement.service import ProcurementService
        from app.core.storage import get_storage

        user = SimpleNamespace(id=uuid.UUID(user_id)) if user_id else None
        spec_bytes = get_storage().get(spec_storage_key)
        order_bytes = get_storage().get(order_storage_key) if order_storage_key else None
        return await ProcurementService(db).build_bom_for_submission(
            user, uuid.UUID(submission_id),
            spec_bytes=spec_bytes, order_bytes=order_bytes,
            filename=spec_filename, spec_type=spec_type,
            client_match_code=client_match_code,
            client_id=uuid.UUID(client_id) if client_id else None,
            order_filename=order_filename, order_mime=order_mime,
            order_match_code=order_match_code,
            source_document_id=uuid.UUID(source_document_id) if source_document_id else None,
        )

    try:
        result = _run_async(_with_session(_job))
    except Exception as exc:  # noqa: BLE001
        logger.exception("generate_bom_for_submission failed: submission=%s", submission_id)
        # Retry transient failures; after max_retries Celery re-raises and the
        # job lands in the dead state — surfaced to the UI below on the final attempt.
        try:
            raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            payload = {"error": "generation_failed", "submission_id": submission_id,
                       "detail": str(exc)}
            _run_async(_push_realtime(f"submission:{submission_id}", "bom_failed", payload))
            return payload

    payload = {
        "submission_id": submission_id,
        "bom_id": result["bom"]["id"],
        "status": result["bom"]["status"],
        "flags": result["flags"],
        "manual_entry_required": result["extraction"].get("manual_entry_required", False),
        "idempotent_replay": result.get("idempotent_replay", False),
    }
    _run_async(_push_realtime(f"submission:{submission_id}", "bom_ready", payload))
    return payload


# ══════════════════════════════════════════════════════════════════════════════
# SCHEDULED TASKS (Beat) — the periodic-job reason. Bodies are stubs that call
# into the owning services; wire them to your freight / notification modules.
# ══════════════════════════════════════════════════════════════════════════════
@celery_app.task(name="app.modules.bom.tasks.scan_freight_risk")
def scan_freight_risk() -> dict[str, Any]:
    """Periodic: re-evaluate every open order against its sea-shipping deadline and
    raise/clear freight-risk alerts. Runs every 2h via Beat."""
    async def _job(db):
        # from app.modules.production.freight_service import FreightService
        # return await FreightService(db).scan_open_orders()
        logger.info("scan_freight_risk: TODO wire FreightService(db).scan_open_orders()")
        return {"scanned": 0, "alerts_raised": 0}

    return _run_async(_with_session(_job))


@celery_app.task(name="app.modules.bom.tasks.escalate_review_notifications")
def escalate_review_notifications() -> dict[str, Any]:
    """Periodic: find review notifications past their 2h acknowledgement SLA and
    escalate them to email. Runs every 15m via Beat; the SLA check is inside."""
    async def _job(db):
        from app.modules.bom.notification_service import NotificationService

        # Expected to exist on NotificationService; returns the count escalated.
        return {"escalated": await NotificationService(db).escalate_overdue()}

    return _run_async(_with_session(_job))