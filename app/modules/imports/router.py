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

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from app.core.database import SessionLocal
from app.core.enums import UserRole
from app.modules.users.deps import require_roles
from app.modules.imports.import_engine import build_preview
from app.modules.imports.load_to_db import load_preview
from app.modules.users.models import User

router = APIRouter(prefix="/imports", tags=["Imports"])


def _save_upload(file: UploadFile) -> str:
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(400, "Please upload an .xlsx file")
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    with os.fdopen(fd, "wb") as f:
        f.write(file.file.read())
    return path


def _do_preview(path: str) -> dict:
    return build_preview(path).summary()


def _do_commit(path: str) -> dict:
    preview = build_preview(path)
    summary = preview.summary()
    db = SessionLocal()
    try:
        stats = load_preview(db, preview)
    finally:
        db.close()
    return {"summary": summary, "written": stats}


@router.post("/preview")
async def preview_import(
    file: UploadFile = File(...),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Dry-run: parse the workbook and return a structured preview (no writes)."""
    path = _save_upload(file)
    try:
        return await run_in_threadpool(_do_preview, path)
    finally:
        os.remove(path)


@router.post("/commit")
async def commit_import(
    file: UploadFile = File(...),
    _: User = Depends(require_roles(UserRole.DIRECT_MANAGER)),
):
    """Parse, validate, and write the workbook to the database (idempotent)."""
    path = _save_upload(file)
    try:
        return await run_in_threadpool(_do_commit, path)
    finally:
        os.remove(path)
