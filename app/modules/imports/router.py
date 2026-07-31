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
import zipfile

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.core.database import SessionLocal, get_db
from app.modules.clients.service import ClientService
from app.modules.imports.load_to_db import load_preview_into_order
from app.modules.imports.import_engine import build_preview
from app.modules.users.deps import require_roles
from app.modules.users.models import User
from app.core.enums import UserRole
from starlette.concurrency import run_in_threadpool

router = APIRouter(prefix="/imports", tags=["Imports"])


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
    """Validate order number, parse, and write SKUs INTO that order (idempotent)."""
    await _require_order(db, order_number)
    path = _save_upload(file)
    try:
        return await run_in_threadpool(_do_commit_into_order, path, order_number)
    finally:
        os.remove(path)