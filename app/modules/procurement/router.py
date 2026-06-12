"""
================================================================================
modules/procurement/router.py — Stage-1 upload & validation API
================================================================================

Endpoints (all under /api/v1/procurement; DM/MD-gated, MD bypasses as superuser):

  POST /submissions                                  open an empty upload batch
  POST /submissions/{id}/order-sheet                 upload + validate order slot
  POST /submissions/{id}/spec-sheet                  upload + validate spec slot
  GET  /submissions/{id}                             status + Stage-2 readiness gate
  GET  /submissions/{id}/documents/{document_id}     full per-document report

The thin HTTP shell: it enforces the size cap by STREAMING the body and aborting
past MAX_UPLOAD_MB (closing the unbounded-read gap in the imports handler), maps
UploadError → its stable HTTP status with the §5b diagnostics envelope, and
delegates everything else to the service (which pushes the blocking work into a
threadpool). No business logic lives here.
================================================================================
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.procurement.bom_service import BomService
from app.modules.procurement.enums import RejectReason
from app.modules.procurement.errors import UploadError
from app.modules.procurement.inventory_service import InventoryService
from app.modules.procurement.notification_service import NotificationService
from app.modules.procurement.schemas import (
    BomApproveRequest,
    BomBulkPatch,
    BomRejectRequest,
    OpenSubmissionRequest,
)
from app.modules.procurement.service import ProcurementService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/procurement", tags=["Procurement — Stage 1"])

_DMMD = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)
# Stage 2: the cutting manager edits drafts + works the confirmation gate; MD/DM
# bypass as superusers (users/deps.SUPERUSER_ROLES).
_CUTTING = require_roles(UserRole.CUTTING_MANAGER)
_MD = require_roles(UserRole.MANAGING_DIRECTOR)
# Stage 4: inventory reads are open to HR/accounts viewers too (stage-0 §5).
_VIEW = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.VIEWER)


async def _read_capped(file: UploadFile) -> bytes:
    """Stream the upload, aborting once it exceeds MAX_UPLOAD_MB (413). Avoids the
    unbounded `file.read()` the imports handler does (CLAUDE.md §13.4)."""
    cap = settings.max_upload_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise UploadError(
                RejectReason.FILE_TOO_LARGE,
                f"File exceeds the {settings.max_upload_mb} MB limit.",
                payload={"document_fingerprint": {"filename": file.filename}},
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _error_response(exc: UploadError) -> JSONResponse:
    payload = dict(exc.payload)
    payload.setdefault("error", "document_validation_failed")
    payload.setdefault("reason_code", exc.reason.value)
    return JSONResponse(status_code=exc.http_status, content=payload)


@router.post("/submissions", status_code=201)
async def open_submission(
    body: OpenSubmissionRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    sub = await ProcurementService(db).open_submission(
        user, body.client_id if body else None)
    return {"submission_id": str(sub.id), "status": sub.status}


@router.post("/submissions/{submission_id}/order-sheet")
async def upload_order_sheet(
    submission_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    try:
        data = await _read_capped(file)
        envelope = await ProcurementService(db).upload_order_sheet(
            user, submission_id, data, file.filename)
        return JSONResponse(status_code=201, content=envelope)
    except UploadError as exc:
        return _error_response(exc)


@router.post("/submissions/{submission_id}/spec-sheet")
async def upload_spec_sheet(
    submission_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    try:
        data = await _read_capped(file)
        envelope = await ProcurementService(db).upload_spec_sheet(
            user, submission_id, data, file.filename)
        return JSONResponse(status_code=201, content=envelope)
    except UploadError as exc:
        return _error_response(exc)


@router.get("/submissions/{submission_id}")
async def submission_status(
    submission_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    return await ProcurementService(db).get_submission_status(submission_id)


@router.get("/submissions/{submission_id}/documents/{document_id}")
async def document_report(
    submission_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    return await ProcurementService(db).get_document_report(submission_id, document_id)


# ══════════════════════════════════════════════════════════════════════════
# Stage 2 — BOM: editable contract (§7), confirmation gate (§10), approval (§9)
# ══════════════════════════════════════════════════════════════════════════
@router.get("/boms/{bom_id}")
async def get_bom(
    bom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    return await BomService(db).get_bom(bom_id)


@router.patch("/boms/{bom_id}/items")
async def patch_bom_items(
    bom_id: uuid.UUID,
    body: BomBulkPatch,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """Bulk-edit the changed cells in ONE atomic request, guarded by optimistic
    locking on bom.revision (§7b). 409 stale_revision on a stale base; 409 bom_locked
    on a LOCKED BOM; else the recomputed tree + revision+1."""
    edits = [e.model_dump() for e in body.edits]
    return await BomService(db).edit_bom_items(user, bom_id, body.base_revision, edits)


@router.post("/boms/{bom_id}/confirm-cutting")
async def confirm_cutting(
    bom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_CUTTING),
):
    """The mandatory cutting-manager gate (§10): stamps cutting_confirmed_*, writes
    an audit row, and back-fills the DCM memory so the next order of this style is a
    Source-1 template hit."""
    return await BomService(db).confirm_cutting(user, bom_id)


@router.post("/boms/{bom_id}/approve")
async def approve_bom(
    bom_id: uuid.UUID,
    body: BomApproveRequest | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_MD),
):
    """Stage-3 MD approve/lock — REFUSED (409) while cutting_confirmed_at is null.
    Approval always locks the BOM (immutable thereafter)."""
    return await BomService(db).approve_bom(user, bom_id, lock=bool(body and body.lock))


@router.post("/boms/{bom_id}/reject")
async def reject_bom(
    bom_id: uuid.UUID,
    body: BomRejectRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_MD),
):
    """Stage-3 MD reject (§1b) — only from `ready_for_review`; reason mandatory."""
    return await BomService(db).reject_bom(user, bom_id, reason=body.reason)


@router.post("/boms/{bom_id}/reopen")
async def reopen_bom(
    bom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    """Stage-3 reopen a rejected BOM for rework (§1b) → draft, revision+1."""
    return await BomService(db).reopen_bom(user, bom_id)


@router.post("/boms/{bom_id}/export")
async def export_bom(
    bom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_MD),
):
    """Stage-3 PDF export (§4) — renders the locked BOM, stores a sha256-deduped
    Document(kind=bom_quote), → `exported`. Idempotent on the rendered bytes."""
    return await BomService(db).export_bom(user, bom_id)


# ══════════════════════════════════════════════════════════════════════════
# Stage 3 — notifications: in-app BOM-ready notice + 2-hour escalation (§2)
# ══════════════════════════════════════════════════════════════════════════
@router.get("/notifications")
async def list_notifications(
    unread_only: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """The caller's in-app notifications (the SSE poll fallback). Recipient-scoped."""
    return await NotificationService(db).list_for_user(user.id, unread_only=unread_only)


@router.get("/notifications/stream")
async def stream_notifications(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """SSE live push of the caller's notifications (the table stays the source of
    truth; a dropped stream replays from GET /notifications)."""
    gen = NotificationService(db).stream(user.id)
    return StreamingResponse(gen, media_type="text/event-stream")


@router.post("/notifications/{notification_id}/open")
async def open_notification(
    notification_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Mark a notice SEEN — sets `opened_at` and CANCELS the 2-hour email escalation."""
    return await NotificationService(db).mark_opened(notification_id, user)


# ══════════════════════════════════════════════════════════════════════════
# Stage 4 — inventory: master sync (§2), the BOM-vs-stock check (§4–§7), report (§8)
# ══════════════════════════════════════════════════════════════════════════
@router.post("/inventory/preview")
async def inventory_preview(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    """Dry-run the inventory-master sync (§2): normalize + dedup + drop noise, no writes."""
    try:
        data = await _read_capped(file)
        return await InventoryService(db).preview(data)
    except UploadError as exc:
        return _error_response(exc)


@router.post("/inventory/commit")
async def inventory_commit(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    """Idempotent upsert of the inventory master (§2c): sheet wins on qty_on_hand,
    absent rows soft-deactivate; reservations (a separate ledger) are untouched."""
    try:
        data = await _read_capped(file)
        return await InventoryService(db).commit(data)
    except UploadError as exc:
        return _error_response(exc)


@router.get("/inventory/items")
async def inventory_items(
    search: str | None = None,
    limit: int = 100,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_VIEW),
):
    """The master stock list (the picker + the master view)."""
    return await InventoryService(db).list_items(search=search, limit=limit, offset=offset)


@router.post("/boms/{bom_id}/inventory-check")
async def run_inventory_check(
    bom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    """Run (or re-run) the inventory check for an approved BOM (§4–§7). Releases the
    BOM's prior reservations, matches every stockable line to stock, computes
    required/on_hand/available/shortfall/status, and reserves min(required, available)."""
    return await InventoryService(db).run_check(user, bom_id)


@router.get("/inventory-checks/{check_id}")
async def get_inventory_check(
    check_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_VIEW),
):
    """The per-check result (§8a)."""
    return await InventoryService(db).get_check(check_id)


@router.get("/inventory-checks")
async def inventory_dashboard(
    client_id: uuid.UUID | None = None,
    order_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_VIEW),
):
    """The grouped dashboard (§8b): client → order → style with a per-BOM badge."""
    return await InventoryService(db).dashboard(client_id=client_id, order_id=order_id)


@router.get("/boms/{bom_id}/inventory-check")
async def latest_inventory_check(
    bom_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_VIEW),
):
    """The latest inventory check for a BOM (§8c)."""
    return await InventoryService(db).latest_for_bom(bom_id)
