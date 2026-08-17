"""
================================================================================
modules/imports/router.py — Excel import endpoints (async-friendly)
================================================================================

TWO-STEP FLOW (the safe pattern for messy uploads)
  POST /imports/preview  — upload an .xlsx, get a parsed PREVIEW + warnings.
                           Nothing is written to the database.
  POST /imports/commit   — upload the same .xlsx, parse, validate, and WRITE it.

Splitting preview from commit lets a human see exactly what will happen — how
many clients, styles, pieces, and any warnings — before any data is stored.

ASYNC NOTE
    Parsing (openpyxl) and the bulk DB load are CPU/IO-blocking and use the SYNC
    SQLAlchemy session. Rather than rewrite the batch loader as async, we run it
    in a worker thread via Starlette's run_in_threadpool, so the event loop is
    never blocked. This keeps the proven idempotent loader intact while the API
    stays fully async.
================================================================================
"""
from __future__ import annotations

import os
import tempfile
import uuid
import zipfile

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.modules.clients.service import ClientService
from app.modules.imports.breakdown import BreakdownService
from app.modules.imports.load_to_db import load_preview_into_order
from app.modules.imports.import_engine import build_preview
from app.modules.users.deps import require_roles
from app.modules.users.models import User
from app.core.enums import UserRole
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/imports", tags=["Imports"])

# Uploading, correcting and releasing a breakdown are all DM work; MD passes via
# SUPERUSER_ROLES inside require_roles.
_DM = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)


def _save_upload(file: UploadFile) -> str:
    # F118: filename is optional in the multipart spec — guard before .lower().
    fname = (file.filename or "").lower()
    if not fname.endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload an .xlsx file")

    # F38: stream the body with a hard cap instead of an unbounded .read() that
    # pulls the whole upload into memory. The cap comes from settings.
    max_bytes = settings.max_upload_mb * 1024 * 1024
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    written = 0
    with os.fdopen(fd, "wb") as f:
        while True:
            chunk = file.file.read(1024 * 1024)
            if not chunk:
                break
            written += len(chunk)
            if written > max_bytes:
                f.close()
                try:
                    os.remove(path)
                except OSError:
                    pass
                raise HTTPException(
                    413, f"File exceeds the {settings.max_upload_mb} MB limit.")
            f.write(chunk)

    # F45: validate the CONTAINER, not just the extension. An .xlsx is a zip;
    # anything renamed to .xlsx that is not a valid zip is rejected before it
    # reaches openpyxl (guards against a decompression bomb / mislabelled file).
    if not zipfile.is_zipfile(path):
        try:
            os.remove(path)
        except OSError:
            pass
        raise HTTPException(400, "File is not a valid .xlsx workbook.")
    return path

async def _require_order(db: AsyncSession, order_number: str):
    """Reject early if the entered number doesn't match a client's order."""
    order = await ClientService(db).get_order_by_number(order_number)
    if not order:
        raise HTTPException(
            404, "Order number not found. Please verify with the client record.")
    return order

def _do_commit_into_order(path: str, order_number: str) -> dict:
    preview = build_preview(path)
    summary = preview.summary()
    db = SessionLocal()
    try:
        stats = load_preview_into_order(db, preview, order_number=order_number)
    finally:
        db.close()
    return {"summary": summary, "written": stats}

def _do_preview(path: str) -> dict:
    return build_preview(path).summary()


@router.post("/preview")
async def preview_import(
    order_number: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Dry-run. Validates the order number first, then parses (no writes)."""
    await _require_order(db, order_number)
    path = _save_upload(file)
    try:
        return await run_in_threadpool(_do_preview, path)
    finally:
        os.remove(path)


@router.post("/commit")
async def commit_import(
    order_number: str = Form(...),
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Validate order number, parse, and write SKUs INTO that order (idempotent).

    NOTHING IS MINTED HERE ANY MORE (change-list item 9). The sheet lands as
    DRAFT styles you can correct; per-piece barcodes and drawer merges are
    created only when the DM releases a style. The response carries
    `release_required: true` and `pieces_minted: 0` to say so.

    Next call: GET /imports/breakdown/{order_number}.
    """
    await _require_order(db, order_number)
    path = _save_upload(file)
    try:
        return await run_in_threadpool(_do_commit_into_order, path, order_number)
    finally:
        os.remove(path)


# ══════════════════════════════════════════════════════════════════════════════
# THE BREAKDOWN TABLE + THE RELEASE GATE  (change-list item 9)
# ══════════════════════════════════════════════════════════════════════════════
class SkuPatch(BaseModel):
    """Correct one DRAFT breakdown line. Send only the fields you are changing."""
    qty_ordered: int | None = Field(default=None, ge=0)
    color_name: str | None = None
    color_code: str | None = None
    size: str | None = None
    knit_color: str | None = None
    nylon_color: str | None = None


class ReleaseRequest(BaseModel):
    """The DM names the styles that go to production."""
    style_ids: list[uuid.UUID] = Field(min_length=1)
    # Growing the drawer pool is a separate, explicit decision (POST /drawers/pool).
    # Setting this true releases AND mints the shortfall of drawers in one step —
    # offered because a DM who has just seen "230 pieces have no drawer" should not
    # have to make two calls, but it is opt-in so the pool never grows by accident.
    grow_drawer_pool: bool = False


class StyleIdsRequest(BaseModel):
    style_ids: list[uuid.UUID] = Field(min_length=1)


@router.get("/breakdown/{order_number}")
async def breakdown_table(
    order_number: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """THE BREAKDOWN, AS A TABLE. Styles + their SKUs + release status.

    This is the screen between upload and production. Rows with
    `production_status: DRAFT` are editable and have minted nothing; rows with
    RELEASED have real barcoded garments behind them (`minted_pieces`) and are
    read-only.

    `needs_lining` per style is the same verdict the store completeness gate will
    apply, so the DM can see before releasing whether these garments will need a
    lining leg merged in the drawer.
    """
    return await BreakdownService(db).get_table(order_number)


@router.patch("/breakdown/skus/{sku_id}")
async def patch_breakdown_sku(
    sku_id: uuid.UUID,
    body: SkuPatch,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """Correct one DRAFT line — quantity, colour or size.

    409 once the style is RELEASED: its pieces carry printed barcodes, and
    rewriting the breakdown behind a garment already on the floor is how a
    factory loses traceability. To make MORE of a released style, raise the
    quantity on a new upload and release again — release tops up.
    """
    return await BreakdownService(db).update_sku(
        sku_id, body.model_dump(exclude_unset=True))


@router.delete("/breakdown/skus/{sku_id}")
async def delete_breakdown_sku(
    sku_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_DM),
):
    """Drop a line the sheet should not have had. DRAFT + zero minted pieces only."""
    return await BreakdownService(db).delete_sku(sku_id)


@router.post("/breakdown/{order_number}/cancel")
async def cancel_breakdown_styles(
    order_number: str,
    body: StyleIdsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """Withdraw DRAFT styles so they stop appearing on the release screen."""
    return await BreakdownService(db).cancel_styles(
        body.style_ids, user_name=user.name)


@router.post("/breakdown/{order_number}/release", status_code=201)
async def release_breakdown_styles(
    order_number: str,
    body: ReleaseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_DM),
):
    """RELEASE STYLES INTO PRODUCTION. This is the mint.

    Creates, atomically and only now: the Piece rows, their per-piece barcodes
    (compact PC-XXXXXX + the long alias), and the drawer merge for each. Stamps
    each style RELEASED with the actor and the time, and writes an audit_log row.

    PARTIAL ACCEPT: an already-released style comes back in `rejected` with its
    reason; the rest still release. Read `minted`, not the HTTP status.

    WATCH `minted.pieces_waiting_for_drawer`. The drawer pool is finite. Pieces
    beyond the free drawers are minted with no drawer — they have barcodes but
    nowhere to be stored, so they cannot pass the merge gate until a drawer frees
    up or the pool is grown. Send `grow_drawer_pool: true`, or call
    POST /drawers/pool, to clear it.
    """
    return await BreakdownService(db).release_styles(
        order_number, body.style_ids, user_name=user.name,
        allow_pool_growth=body.grow_drawer_pool)