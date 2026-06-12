"""
================================================================================
modules/inventory/router.py — Stage-4 inventory API
================================================================================

Endpoints (under /api/v1/procurement, preserving the pre-split URLs):

  POST /inventory/preview | /inventory/commit          master sync (dry-run / upsert)
  GET  /inventory/items                                the master stock list
  POST /boms/{id}/inventory-check                      run/re-run the per-BOM check
  GET  /inventory-checks/{id} | /inventory-checks      a check / the grouped dashboard
  GET  /boms/{id}/inventory-check                      the latest check for a BOM

Self-contained upload cap (raises HTTP 413 directly) so the inventory module does not
depend on the Stage-1 procurement upload machinery.
================================================================================
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.inventory.service import InventoryService
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/procurement", tags=["Procurement — Stage 4 inventory"])

_DMMD = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR)
_VIEW = require_roles(UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR, UserRole.VIEWER)


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


@router.post("/inventory/preview")
async def inventory_preview(file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                            user: User = Depends(_DMMD)):
    return await InventoryService(db).preview(await _read_capped(file))


@router.post("/inventory/commit")
async def inventory_commit(file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
                           user: User = Depends(_DMMD)):
    return await InventoryService(db).commit(await _read_capped(file))


@router.get("/inventory/items")
async def inventory_items(search: str | None = None, limit: int = 100, offset: int = 0,
                          db: AsyncSession = Depends(get_db), user: User = Depends(_VIEW)):
    return await InventoryService(db).list_items(search=search, limit=limit, offset=offset)


@router.post("/boms/{bom_id}/inventory-check")
async def run_inventory_check(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                              user: User = Depends(_DMMD)):
    return await InventoryService(db).run_check(user, bom_id)


@router.get("/inventory-checks/{check_id}")
async def get_inventory_check(check_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                              user: User = Depends(_VIEW)):
    return await InventoryService(db).get_check(check_id)


@router.get("/inventory-checks")
async def inventory_dashboard(client_id: uuid.UUID | None = None,
                              order_id: uuid.UUID | None = None,
                              db: AsyncSession = Depends(get_db), user: User = Depends(_VIEW)):
    return await InventoryService(db).dashboard(client_id=client_id, order_id=order_id)


@router.get("/boms/{bom_id}/inventory-check")
async def latest_inventory_check(bom_id: uuid.UUID, db: AsyncSession = Depends(get_db),
                                 user: User = Depends(_VIEW)):
    return await InventoryService(db).latest_for_bom(bom_id)
