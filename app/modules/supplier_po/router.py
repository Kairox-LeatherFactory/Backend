"""
================================================================================
modules/supplier_po/router.py — Stage-5 supplier-PO API
================================================================================

Endpoints (under /api/v1/procurement, preserving the module-prefix convention):

  Suppliers (§9):  POST /suppliers/import/{preview,commit}; GET /suppliers[/{id}];
                   POST /suppliers; PATCH /suppliers/{id}; DELETE /suppliers/{id};
                   POST /suppliers/{id}/reactivate
  POs (§1–§7):     POST /boms/{id}/generate-pos; GET /pos[/{id}]; PATCH /pos/{id}/items;
                   POST /pos/{id}/{submit,approve,reject,send,cancel,acknowledge}
  Tracking (§6):   GET /t/o/{token}.gif (open pixel); GET /t/c/{token} (click redirect)
  Webhooks (§5/§7):POST /webhooks/ses; /webhooks/twilio/{whatsapp,voice}
  Board (§8):      GET /production-tracking; POST /production-tracking/{id}/transition

The tracking + webhook routes are intentionally UNAUTHENTICATED (a supplier's mail
client / Twilio / SNS call them); they carry an opaque per-send token, not a session.
This module is the resume point for the in-progress Stage-5 build.
================================================================================
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.supplier_po import po_tracking
from app.modules.supplier_po.po_service import PoService
from app.modules.supplier_po.production_tracking_service import ProductionTrackingService
from app.modules.supplier_po.schemas import (
    PoAcknowledgeRequest,
    PoBulkPatch,
    PoRejectRequest,
    ProductionTransitionRequest,
    SupplierCreate,
    SupplierUpdate,
)
from app.modules.supplier_po.supplier_service import SupplierService
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/procurement", tags=["Procurement — Stage 5 supplier PO"])

_DMMD = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)
_MD = require_roles(UserRole.MANAGING_DIRECTOR)
_VIEW = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.VIEWER)
# Cross-check approvers — the service enforces the leather→Cutting / accessory→MD/DM/HR
# routing; the router just admits the candidate roles.
_APPROVERS = require_roles(UserRole.CUTTING_MANAGER, UserRole.MANAGING_DIRECTOR,
                           UserRole.DIRECT_MANAGER, UserRole.HR)
_BOARD = require_roles(UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
                       UserRole.CUTTING_MANAGER)


async def _read_capped(file: UploadFile) -> bytes:
    cap = settings.max_upload_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise HTTPException(413, f"File exceeds the {settings.max_upload_mb} MB limit.")
        chunks.append(chunk)
    return b"".join(chunks)


# ── suppliers (§9) ────────────────────────────────────────────────────────────
@router.post("/suppliers/import/preview")
async def suppliers_import_preview(file: UploadFile = File(...),
                                   db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await SupplierService(db).preview(await _read_capped(file))


@router.post("/suppliers/import/commit")
async def suppliers_import_commit(file: UploadFile = File(...),
                                  db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await SupplierService(db).commit(user, await _read_capped(file))


@router.get("/suppliers")
async def list_suppliers(q: str | None = None, service: str | None = None,
                         active: bool | None = None, limit: int = 100, offset: int = 0,
                         db: AsyncSession = Depends(get_db), user: User = Depends(_VIEW)):
    return await SupplierService(db).list_suppliers(
        search=q, service=service, active=active, limit=limit, offset=offset)


@router.get("/suppliers/{supplier_id}")
async def get_supplier(supplier_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                       user: User = Depends(_VIEW)):
    return await SupplierService(db).get_supplier(supplier_id)


@router.post("/suppliers", status_code=201)
async def create_supplier(body: SupplierCreate, db: AsyncSession = Depends(get_db),
                          user: User = Depends(_DMMD)):
    return await SupplierService(db).create_supplier(user, body.model_dump(exclude_none=True))


@router.patch("/suppliers/{supplier_id}")
async def update_supplier(supplier_id: uuid.UUID, body: SupplierUpdate,
                          db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await SupplierService(db).update_supplier(
        user, supplier_id, body.model_dump(exclude_none=True))


@router.delete("/suppliers/{supplier_id}")
async def deactivate_supplier(supplier_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                              user: User = Depends(_MD)):
    return await SupplierService(db).deactivate_supplier(user, supplier_id)


@router.post("/suppliers/{supplier_id}/reactivate")
async def reactivate_supplier(supplier_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                              user: User = Depends(_MD)):
    return await SupplierService(db).reactivate_supplier(user, supplier_id)


# ── purchase orders (§1–§7) ───────────────────────────────────────────────────
@router.post("/boms/{bom_id}/generate-pos")
async def generate_pos(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                       user: User = Depends(_DMMD)):
    return await PoService(db).generate_for_bom(user, bom_id)


@router.get("/pos")
async def list_pos(status: str | None = None, needs_supplier: bool | None = None,
                   bom_id: uuid.UUID | None = None, supplier_id: uuid.UUID | None = None,
                   db: AsyncSession = Depends(get_db), user: User = Depends(_VIEW)):
    return await PoService(db).list_pos(status=status, needs_supplier=needs_supplier,
                                        bom_id=bom_id, supplier_id=supplier_id)


@router.get("/pos/{po_id}")
async def get_po(po_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                 user: User = Depends(_VIEW)):
    return await PoService(db).get_po(po_id)


@router.patch("/pos/{po_id}/items")
async def patch_po_items(po_id: uuid.UUID, body: PoBulkPatch,
                         db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await PoService(db).edit_po(
        user, po_id, body.base_revision,
        item_edits=[e.model_dump() for e in body.item_edits],
        po_edits=body.po_edits,
        add_items=[a.model_dump() for a in body.add_items],
        remove_item_ids=[str(r) for r in body.remove_item_ids])


@router.post("/pos/{po_id}/submit")
async def submit_po(po_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                    user: User = Depends(_DMMD)):
    return await PoService(db).submit_po(user, po_id)


@router.post("/pos/{po_id}/approve")
async def approve_po(po_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(_APPROVERS)):
    return await PoService(db).approve_po(user, po_id)


@router.post("/pos/{po_id}/reject")
async def reject_po(po_id: uuid.UUID, body: PoRejectRequest,
                    db: AsyncSession = Depends(get_db), user: User = Depends(_APPROVERS)):
    return await PoService(db).reject_po(user, po_id, body.reason)


@router.post("/pos/{po_id}/send")
async def send_po(po_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                  user: User = Depends(_DMMD)):
    return await PoService(db).send_po(user, po_id)


@router.post("/pos/{po_id}/cancel")
async def cancel_po(po_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                    user: User = Depends(_DMMD)):
    return await PoService(db).cancel_po(user, po_id)


@router.post("/pos/{po_id}/acknowledge")
async def acknowledge_po(po_id: uuid.UUID, body: PoAcknowledgeRequest,
                         db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await PoService(db).acknowledge_po(
        user, po_id, channel=body.channel, confirmed_qty=body.confirmed_qty, notes=body.notes)


# ── open tracking (§6) — unauthenticated, token-bearing ──────────────────────
@router.get("/t/o/{token}.gif")
async def tracking_pixel(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    await PoService(db).record_open(
        token, ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"))
    return Response(content=po_tracking.PIXEL_GIF, media_type="image/gif")


@router.get("/t/c/{token}")
async def tracking_click(token: str, u: str = "", s: str = "", request: Request = None,
                         db: AsyncSession = Depends(get_db)):
    target = po_tracking.unwrap_target(u)
    if not target or not po_tracking.verify(target, s, settings.secret_key):
        raise HTTPException(400, "Invalid tracking link.")
    await PoService(db).record_click(
        token, ip=request.client.host if request and request.client else None,
        user_agent=request.headers.get("user-agent") if request else None)
    return RedirectResponse(url=target, status_code=302)


# ── webhooks (§5b/§7) — unauthenticated, provider-signed (verification TODO) ──
@router.post("/webhooks/ses")
async def ses_webhook(payload: dict, db: AsyncSession = Depends(get_db)):
    token = payload.get("tracking_token") or (payload.get("mail") or {}).get("tracking_token")
    event = payload.get("eventType") or payload.get("notificationType") or payload.get("type")
    await PoService(db).record_ses_event(token=token, event_type=event or "delivery", meta=payload)
    return {"ok": True}


@router.post("/webhooks/twilio/whatsapp")
async def twilio_whatsapp_webhook(payload: dict, db: AsyncSession = Depends(get_db)):
    token = payload.get("tracking_token")
    if token:
        await PoService(db).acknowledge_by_token(token, channel="whatsapp",
                                                 notes=payload.get("Body"))
    return {"ok": True}


@router.post("/webhooks/twilio/voice")
async def twilio_voice_webhook(payload: dict, db: AsyncSession = Depends(get_db)):
    token = payload.get("tracking_token")
    if token and str(payload.get("Digits")) == "1":
        await PoService(db).acknowledge_by_token(token, channel="call")
    return {"ok": True}


# ── production board (§8) ─────────────────────────────────────────────────────
@router.get("/production-tracking")
async def production_board(client_id: uuid.UUID | None = None,
                           order_id: uuid.UUID | None = None,
                           db: AsyncSession = Depends(get_db), user: User = Depends(_VIEW)):
    return await ProductionTrackingService(db).board(client_id=client_id, order_id=order_id)


@router.post("/production-tracking/{tracking_id}/transition")
async def production_transition(tracking_id: uuid.UUID, body: ProductionTransitionRequest,
                                db: AsyncSession = Depends(get_db), user: User = Depends(_BOARD)):
    return await ProductionTrackingService(db).transition(user, tracking_id, body.status)
