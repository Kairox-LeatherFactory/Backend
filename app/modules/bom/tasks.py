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
from app.core.database import AsyncSessionLocal
from app.modules.bom.service import BomService

logger = logging.getLogger(__name__)




# ── async plumbing ──────────────────────────────────────────────────────────
def _run_async(coro):
    """Drive an async coroutine to completion from a sync Celery task. The worker
    process has no running event loop, so asyncio.run owns one for the task's life."""
    return asyncio.run(coro)


async def _with_session(fn):
    """Open a fresh AsyncSession (worker process ≠ API process), run fn(db), and
    ensure it's closed. Commit/rollback are owned by the service inside fn."""

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
@celery_app.task(name="bom.build_order_breakdown_for_submission",
                 bind=True, max_retries=0)
def build_order_breakdown_for_submission(self, submission_id: str) -> dict:
    """Worker side of POST /order-breakdown. Loads the submission's accepted
    ORDER document bytes, runs extract_order_doc (Gemini, 30–200s), persists
    OrderStyle/OrderStyleColor rows, pushes the Realtime event. The router
    already claimed the submission, so this never double-runs."""
    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            svc = BomService(db)
            try:
                order_doc = await svc.repo.get_accepted_order_document(submission_id)
                if order_doc is None:
                    await svc.release_breakdown_claim(submission_id)
                    return {"status": "failed", "reason": "no_accepted_order_document"}
                data, filename, mime = await svc.repo.load_document_bytes_meta(order_doc.id)
                result = await svc.build_order_breakdown(
                    submission_id,
                    order_bytes=data, order_filename=filename, order_mime=mime,
                    client_id=order_doc.client_id,
                )
                return {"status": "ready", **result}
            except Exception:
                # Release the claim so the operator can re-trigger after a fix;
                # rows are only committed at the end, so a failure leaves nothing.
                await svc.release_breakdown_claim(submission_id)
                raise

    result = _run_async(_run())
    _run_async(_push_realtime(
        f"submission:{submission_id}", "order_breakdown_ready",
        {"submission_id": submission_id, "status": result.get("status"),
         "style_count": len(result.get("styles", []))}))
    return result


@celery_app.task(name="bom.generate_bom_for_style", bind=True, max_retries=0)
def generate_bom_for_style_task(self, order_style_id: str, user_id: str) -> dict:
    """Worker side of POST /order-styles/{id}/generate-bom. All business guards
    live in BomService.generate_bom_for_style (idempotent replay, unconfirmed-
    suggestion refusal) — the router pre-checks them only for fast 4xxs."""
    async def _run() -> dict:
        async with AsyncSessionLocal() as db:
            svc = BomService(db)
            user = await svc.repo.get_user(user_id) if user_id else None
            return await svc.generate_bom_for_style(user, order_style_id)

    result = _run_async(_run())
    _run_async(_push_realtime(
        f"order_style:{order_style_id}", "bom_generated",
        {"order_style_id": order_style_id, "bom_id": result.get("bom_id"),
         "warnings": result.get("warnings", [])}))
    return result


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


@celery_app.task(bind=True, name="app.modules.bom.tasks.parse_pattern_dxf",
                 max_retries=2, default_retry_delay=10)
def parse_pattern_dxf(self, *, user_id=None, style_signature=None, client_id=None,
                      storage_key=None, b64=None):
    data = _fetch_bytes(storage_key=storage_key, b64=b64)   # your existing helper
    if not data:
        return {"error": "pattern_bytes_missing", "style_signature": style_signature}
    async def _job(db):
        from app.modules.bom.service import BomService
        user = SimpleNamespace(id=uuid.UUID(user_id)) if user_id else None
        return await BomService(db).ingest_pattern_dxf(
            user, data=data, style_signature=style_signature,
            client_id=uuid.UUID(client_id) if client_id else None) 
    try:
        result = _run_async(_with_session(_job))            # your existing plumbing
    except Exception as exc:
        try: raise self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            payload = {"error": "pattern_parse_failed", "style_signature": style_signature, "detail": str(exc)}
            _run_async(_push_realtime(f"pattern:{style_signature}", "pattern_failed", payload))
            return payload
    _run_async(_push_realtime(f"pattern:{style_signature}", "pattern_ready", result))
    return result