"""A manager may only log operations their role is granted (async)."""
import uuid
from datetime import date

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import UserRole, WageType
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.production.service import ProductionService
from app.modules.users.models import User


async def _seed_min(db):
    client = cm.Client(name="C"); db.add(client); await db.flush()
    po = cm.PurchaseOrder(client_id=client.id, po_number="PO1"); db.add(po); await db.flush()
    style = cm.Style(purchase_order_id=po.id, name="CARNABY"); db.add(style); await db.flush()
    sku = cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=10)
    db.add(sku); await db.flush()
    cut = pm.Operation(code="CUTTING", label="Cutting", sequence=1)
    shell = pm.Operation(code="SHELL", label="Shell", sequence=4)
    db.add_all([cut, shell]); await db.flush()
    db.add(pm.OperationAccess(role="cutting_manager", operation_id=cut.id))
    emp = em.Employee(name="W", wage_type=WageType.PIECE_RATE); db.add(emp)
    await db.commit()
    return sku, cut, shell, emp


@pytest.mark.asyncio
async def test_cutting_manager_cannot_log_stitching(db):
    sku, cut, shell, emp = await _seed_min(db)
    ps = ProductionService(db)
    cutting_mgr = User(id=uuid.uuid4(), name="C", phone="1", role=UserRole.CUTTING_MANAGER,
                       password_hash="x", is_active=True)

    # allowed: cutting
    await ps.log_event(cutting_mgr, sku.id, cut.id, emp.id, date(2026, 3, 23), 10)
    # forbidden: shell stitch
    with pytest.raises(HTTPException) as exc:
        await ps.log_event(cutting_mgr, sku.id, shell.id, emp.id, date(2026, 3, 23), 10)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_direct_manager_bypasses_access(db):
    sku, cut, shell, emp = await _seed_min(db)
    ps = ProductionService(db)
    direct = User(id=uuid.uuid4(), name="D", phone="2", role=UserRole.DIRECT_MANAGER,
                  password_hash="x", is_active=True)
    # direct manager can log ANY operation, including shell
    ev = await ps.log_event(direct, sku.id, shell.id, emp.id, date(2026, 3, 23), 5)
    assert ev.qty == 5
