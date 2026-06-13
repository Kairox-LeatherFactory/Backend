"""
================================================================================
modules/bom/router.py — Stage-2/3 BOM API + the in-app notification surface
================================================================================

Endpoints (under /api/v1/procurement, preserving the pre-split URLs):

  GET  /boms/{id}                     the editable BOM tree
  PATCH/boms/{id}/items               bulk edit (optimistic revision lock)
  POST /boms/{id}/confirm-cutting     the cutting-manager gate (§10)
  POST /boms/{id}/approve | /reject | /reopen | /export    Stage-3 lifecycle
  GET  /notifications | /notifications/stream | POST /notifications/{id}/open

The notification routes live here because NotificationService (BOM-review notice +
2-hour email escalation) lives in this module; it resolves recipients via users.service
and reads the cross-cutting core `notification` table.

LAYERING: this router is a THIN HTTP shell — every handler just resolves the role
dependency, unpacks the request body, and delegates to BomService / NotificationService.
No business logic here (house rule). Role gates: _DMMD (DM+MD), _CUTTING (cutting mgr;
MD/DM bypass as superusers), _MD (MD only — the sole approver/rejecter/exporter).

FUNCTION GUIDE  (path → handler → service call → returns)
  GET   /boms/{id}                 get_bom          → BomService.get_bom            the editable tree (dict)
  PATCH /boms/{id}/items           patch_bom_items  → edit_bom_items                {revision, recomputed, reconfirm_required}
  POST  /boms/{id}/confirm-cutting confirm_cutting  → confirm_cutting               {status, templates_backfilled, ...}
  POST  /boms/{id}/approve         approve_bom      → approve_bom(lock=?)           {status, inventory_check_id}  [MD]
  POST  /boms/{id}/reject          reject_bom       → reject_bom(reason)            {status, rejection_reason}    [MD]
  POST  /boms/{id}/reopen          reopen_bom       → reopen_bom                    {status, revision}            [DM/MD]
  POST  /boms/{id}/export          export_bom       → export_bom                    {export_document_id, sha256}  [MD]
  GET   /notifications             list_notifications     → NotificationService.list_for_user   the caller's rows
  GET   /notifications/stream      stream_notifications   → NotificationService.stream          an SSE event stream
  POST  /notifications/{id}/open   open_notification      → NotificationService.mark_opened     stamps opened_at (cancels escalation)
================================================================================
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.bom.notification_service import NotificationService
from app.modules.bom.schemas import BomApproveRequest, BomBulkPatch, BomRejectRequest
from app.modules.bom.service import BomService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/procurement", tags=["Procurement — Stage 2/3 BOM"])

_DMMD = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)
_CUTTING = require_roles(UserRole.CUTTING_MANAGER)
_MD = require_roles(UserRole.MANAGING_DIRECTOR)


@router.get("/boms/{bom_id}")
async def get_bom(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                  user: User = Depends(_CUTTING)):
    return await BomService(db).get_bom(bom_id)


@router.patch("/boms/{bom_id}/items")
async def patch_bom_items(bom_id: uuid.UUID, body: BomBulkPatch,
                          db: AsyncSession = Depends(get_db), user: User = Depends(_CUTTING)):
    edits = [e.model_dump() for e in body.edits]
    return await BomService(db).edit_bom_items(user, bom_id, body.base_revision, edits)


@router.post("/boms/{bom_id}/confirm-cutting")
async def confirm_cutting(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                          user: User = Depends(_CUTTING)):
    return await BomService(db).confirm_cutting(user, bom_id)


@router.post("/boms/{bom_id}/approve")
async def approve_bom(bom_id: uuid.UUID, body: BomApproveRequest | None = None,
                      db: AsyncSession = Depends(get_db), user: User = Depends(_MD)):
    return await BomService(db).approve_bom(user, bom_id, lock=bool(body and body.lock))


@router.post("/boms/{bom_id}/reject")
async def reject_bom(bom_id: uuid.UUID, body: BomRejectRequest,
                     db: AsyncSession = Depends(get_db), user: User = Depends(_MD)):
    return await BomService(db).reject_bom(user, bom_id, reason=body.reason)


@router.post("/boms/{bom_id}/reopen")
async def reopen_bom(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(_DMMD)):
    return await BomService(db).reopen_bom(user, bom_id)


@router.post("/boms/{bom_id}/export")
async def export_bom(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(_MD)):
    return await BomService(db).export_bom(user, bom_id)


# ── notifications (in-app BOM-ready notice + 2-hour escalation, §2) ──────────
@router.get("/notifications")
async def list_notifications(unread_only: bool = False, db: AsyncSession = Depends(get_db),
                             user: User = Depends(get_current_user)):
    return await NotificationService(db).list_for_user(user.id, unread_only=unread_only)


@router.get("/notifications/stream")
async def stream_notifications(db: AsyncSession = Depends(get_db),
                               user: User = Depends(get_current_user)):
    gen = NotificationService(db).stream(user.id)
    return StreamingResponse(gen, media_type="text/event-stream")


@router.post("/notifications/{notification_id}/open")
async def open_notification(notification_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                            user: User = Depends(get_current_user)):
    return await NotificationService(db).mark_opened(notification_id, user)