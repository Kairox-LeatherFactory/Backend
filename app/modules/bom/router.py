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

from fastapi import APIRouter, Depends, UploadFile , HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import APIRouter, Depends, File, UploadFile, status

from app.core.database import get_db
from app.core.enums import UserRole
from app.core.storage import get_storage
from app.modules.bom.notification_service import NotificationService
from app.modules.bom.schemas import(BomApproveRequest, BomBulkPatch, BomRejectRequest, ClientChecksPut, CostCatalogPut, 
FabricRoleIn, PomMappingIn,AttachmentsIn, BreakdownAccepted, BreakdownOut, StyleBomAccepted, OrderStyleOut,DxfYieldIn)
from app.modules.bom.service import BomService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User
from app.modules.bom.tasks import (
    build_order_breakdown_for_submission, generate_bom_for_style_task,
)


router = APIRouter(prefix="/procurement", tags=["Procurement — Stage 2/3 BOM"])

_DMMD = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)
_CUTTING = require_roles(UserRole.CUTTING_MANAGER)
_MD = require_roles(UserRole.MANAGING_DIRECTOR)


@router.get("/boms/{bom_id}")
async def get_bom(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                  _: User = Depends(_CUTTING)):
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

@router.post("/patterns", status_code=202)
async def upload_pattern(file: UploadFile = File(...), style_signature: str | None = None,
                         db: AsyncSession = Depends(get_db), user=Depends(get_current_user)):
    data = await file.read()
    key = f"patterns/incoming/{uuid.uuid4()}.dxf"
    get_storage().put(key, data)                       # store, then hand the KEY to the task
    from app.modules.bom.tasks import parse_pattern_dxf
    job = parse_pattern_dxf.delay(user_id=str(user.id), style_signature=style_signature,
                                  client_id=str(getattr(user,"client_id",None) or "") or None,
                                  storage_key=key)
    return {"job_id": job.id, "channel": f"pattern:{style_signature}"}

@router.put("/admin/dxf-yields/{species}")
async def put_dxf_yield(species: str, body: DxfYieldIn,
                        db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await BomService(db).set_dxf_yield(species, body.factor, note=body.note)

@router.post("/admin/fabric-roles")
async def post_fabric_role(body: FabricRoleIn,
                           db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await BomService(db).upsert_fabric_role(body.model_dump())

@router.get("/admin/cost-catalog")
async def get_cost_catalog(db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    from app.modules.bom import config_store
    return config_store.get_cost_catalog()

@router.put("/admin/cost-catalog/{garment_code}")
async def put_cost_catalog(garment_code: str, body: CostCatalogPut,
                           db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await BomService(db).set_cost_catalog(
        garment_code, [l.model_dump() for l in body.lines])
    
@router.get("/admin/checks")
async def get_checks(db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    from app.modules.bom import config_store
    return config_store.get_bom_checks()

@router.put("/admin/checks/{client_code}")
async def put_checks(client_code: str, body: ClientChecksPut,
                     db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    return await BomService(db).set_client_checks(
        client_code, [r.model_dump() for r in body.rules])
    
@router.get("/admin/pom-dictionary")
async def list_pom_dictionary(db: AsyncSession = Depends(get_db), user: User = Depends(_DMMD)):
    rows = await BomService(db).repo.list_pom_mappings()
    return [{"source_term": r.source_term, "pom_code": r.pom_code, "language": r.language,
             "garment_type_id": str(r.garment_type_id) if r.garment_type_id else None,
             "weight": r.weight} for r in rows]

@router.post("/admin/pom-dictionary")
async def add_pom_mapping(body: PomMappingIn, db: AsyncSession = Depends(get_db),
                          user: User = Depends(_DMMD)):
    return await BomService(db).add_pom_mapping(body.model_dump())

@router.post("/submissions/{submission_id}/order-breakdown",
             response_model=BreakdownAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def create_order_breakdown(
    submission_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """ENQUEUE breakdown extraction for the submission's accepted ORDER document.
    Runs in Celery (Gemini extraction is 30–200s — never on the request path).
    202 + task id; the UI polls GET or listens for the Realtime push.

    Idempotent: rows already exist -> already_ready (no re-run); a claim is held
    but rows aren't written yet -> already_processing (no double-enqueue)."""
    svc = BomService(db)

    state = await svc.breakdown_state(submission_id)      # validates submission too
    if state == "ready":
        return BreakdownAccepted(submission_id=submission_id, status="already_ready")
    if state == "processing":
        return BreakdownAccepted(submission_id=submission_id,
                                 status="already_processing")

    # Gate + claim (reuses the procurement claim; new gate: ORDER slot accepted —
    # the spec is per-style now, so it is NOT required to start the breakdown).
    claimed = await svc.claim_submission_for_breakdown(submission_id)
    if not claimed:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "submission not ready: order document not accepted, "
                            "or already claimed")

    task = build_order_breakdown_for_submission.delay(str(submission_id))
    return BreakdownAccepted(submission_id=submission_id, status="queued",
                             task_id=task.id)


@router.get("/submissions/{submission_id}/order-breakdown",
            response_model=BreakdownOut)
async def get_order_breakdown(
    submission_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """The breakdown for the selection UI. status:
    not_started (nothing yet) | processing (claimed, worker running) | ready."""
    svc = BomService(db)
    state = await svc.breakdown_state(submission_id)
    if state != "ready":
        return BreakdownOut(submission_id=submission_id, status=state)
    styles = await svc.repo.get_order_styles(submission_id)
    return BreakdownOut(
        submission_id=submission_id, status="ready",
        styles=[OrderStyleOut.model_validate(s) for s in styles],
    )


@router.post("/order-styles/{order_style_id}/attachments",
             response_model=OrderStyleOut)
async def confirm_attachments(
    order_style_id: uuid.UUID,
    body: AttachmentsIn,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """Confirm or override the suggested spec/DXF for ONE style. Synchronous —
    it's a couple of column writes, no LLM. Explicit ids are validated to exist
    (and to belong to the same client) inside the service."""
    row = await BomService(db).confirm_style_attachments(
        order_style_id,
        spec_document_id=body.spec_document_id,
        pattern_reference_id=body.pattern_reference_id,
        clear_spec=body.clear_spec,
        clear_dxf=body.clear_dxf,
    )
    return OrderStyleOut.model_validate(row)


@router.post("/order-styles/{order_style_id}/generate-bom",
             response_model=StyleBomAccepted,
             status_code=status.HTTP_202_ACCEPTED)
async def generate_style_bom(
    order_style_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user=Depends(get_current_user),
):
    """ENQUEUE BOM generation for ONE style. Preconditions checked HERE, before
    queuing, so the operator gets the 4xx immediately instead of a task failure:
    - style exists;  - not already generated (idempotent replay);
    - spec status is not a dangling 'suggested' (confirm or clear it first)."""
    svc = BomService(db)
    row = await svc.repo.get_order_style(order_style_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "order style not found")
    if row.bom_id is not None:
        return StyleBomAccepted(order_style_id=order_style_id,
                                status="already_generated", bom_id=row.bom_id)
    if row.spec_match_status == "suggested":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "spec suggestion is unconfirmed — confirm or clear it before generating")

    task = generate_bom_for_style_task.delay(str(order_style_id),
                                             str(getattr(user, "id", "")))
    return StyleBomAccepted(order_style_id=order_style_id, status="queued",
                            task_id=task.id)