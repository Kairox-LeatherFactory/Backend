"""
================================================================================
modules/procurement/router.py — Stage-1 intake API (upload & validation)
================================================================================

Endpoints (all under /api/v1/procurement; DM/MD-gated, MD bypasses as superuser):

  POST /submissions                                  open an empty upload batch
  POST /submissions/{id}/order-sheet                 upload + validate order slot
  POST /submissions/{id}/spec-sheet                  upload + validate spec slot
  GET  /submissions/{id}                             status + Stage-2 readiness gate
  POST /submissions/{id}/generate-bom                Stage-1 → Stage-2 trigger (→ DRAFT bom)
  GET  /submissions/{id}/documents/{document_id}     full per-document report

The BOM (/boms), inventory (/inventory), and supplier-PO (/suppliers, /pos) routes
moved to their own modules' routers when the monolith was split. The thin HTTP shell:
streams the body + aborts past MAX_UPLOAD_MB, maps UploadError → its stable status, and
delegates to the service.

FUNCTION GUIDE  (path → handler → service call; all gated by _DMMD = DM+MD)
  _read_capped(file) -> bytes   stream the upload, abort past MAX_UPLOAD_MB (UploadError → 413).
  _error_response(exc) -> JSONResponse   map an UploadError to its stable HTTP status + body.
  POST /submissions                      open_submission   → ProcurementService.open_submission
  POST /submissions/{id}/order-sheet     upload_order_sheet → upload_order_sheet (201 / 4xx diagnostics)
  POST /submissions/{id}/spec-sheet      upload_spec_sheet  → upload_spec_sheet
  GET  /submissions/{id}                 submission_status  → get_submission_status (the gate)
  POST /submissions/{id}/generate-bom    generate_bom       → generate_bom_from_submission ({submission_id, status:consumed, bom, flags, extraction})
  GET  /submissions/{id}/documents/{id}  document_report    → get_document_report
================================================================================
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.procurement.enums import RejectReason
from app.modules.procurement.errors import UploadError
from app.modules.procurement.schemas import OpenSubmissionRequest
from app.modules.procurement.service import ProcurementService
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/procurement", tags=["Procurement — Stage 1 intake"])

_DMMD = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)


async def _read_capped(file: UploadFile) -> bytes:
    """Stream the upload, aborting once it exceeds MAX_UPLOAD_MB (413)."""
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
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    # force=true → accept a `needs_manual_review` document anyway and generate an
    # editable BOM (the DM/MD vouches for it). Hard rejects are never overridable.
    try:
        data = await _read_capped(file)
        envelope = await ProcurementService(db).upload_order_sheet(
            user, submission_id, data, file.filename, override_manual_review=force)
        return JSONResponse(status_code=201, content=envelope)
    except UploadError as exc:
        return _error_response(exc)


@router.post("/submissions/{submission_id}/spec-sheet")
async def upload_spec_sheet(
    submission_id: uuid.UUID,
    file: UploadFile = File(...),
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    # force=true → accept a `needs_manual_review` document anyway and generate an
    # editable BOM (the DM/MD vouches for it). Hard rejects are never overridable.
    try:
        data = await _read_capped(file)
        envelope = await ProcurementService(db).upload_spec_sheet(
            user, submission_id, data, file.filename, override_manual_review=force)
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


@router.post("/submissions/{submission_id}/generate-bom", status_code=201)
async def generate_bom(
    submission_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    """Stage-1 → Stage-2 trigger: consume a COMPLETE submission into a DRAFT BOM and lock
    it (CONSUMED). DM/MD only — matches the upload gate. 409 unless the submission is
    COMPLETE with both slots accepted. No body: the BOM is built from the order + spec
    sheets alone; the order/style breakdown is created at MD approval."""
    return await ProcurementService(db).generate_bom_from_submission(user, submission_id)



@router.get("/submissions/{submission_id}/documents/{document_id}")
async def document_report(
    submission_id: uuid.UUID,
    document_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DMMD),
):
    return await ProcurementService(db).get_document_report(submission_id, document_id)
