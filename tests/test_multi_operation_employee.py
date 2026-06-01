"""The SAME employee can be logged under DIFFERENT operations on different days
('CUTTER/MULTI'), and wages must sum across operations at each op's rate (async)."""
import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.core.enums import UserRole, WageType
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.production.service import ProductionService
from app.modules.users.models import User
from app.modules.wages.service import WageService


@pytest.mark.asyncio
async def test_one_employee_multiple_operations(db):
    client = cm.Client(name="C"); db.add(client); await db.flush()
    po = cm.PurchaseOrder(client_id=client.id, po_number="PO1"); db.add(po); await db.flush()
    carnaby = cm.Style(purchase_order_id=po.id, name="CARNABY"); db.add(carnaby); await db.flush()
    sku = cm.SKU(style_id=carnaby.id, color_code="57", size="M", qty_ordered=50)
    db.add(sku); await db.flush()
    cut = pm.Operation(code="CUTTING", label="Cutting", sequence=1)
    paste = pm.Operation(code="PASTING", label="Pasting", sequence=3)
    db.add_all([cut, paste]); await db.flush()
    multi = em.Employee(name="MD Afzal", designation="CUTTER/MULTI", wage_type=WageType.PIECE_RATE)
    db.add(multi); await db.commit()

    ps = ProductionService(db)
    direct = User(id=uuid.uuid4(), name="D", phone="2", role=UserRole.DIRECT_MANAGER,
                  password_hash="x", is_active=True)
    await ps.log_event(direct, sku.id, cut.id, multi.id, date(2026, 3, 23), 20)
    await ps.log_event(direct, sku.id, paste.id, multi.id, date(2026, 3, 25), 15)

    ws = WageService(db)
    await ws.set_rate(carnaby.id, cut.id, 80, date(2026, 3, 1))
    await ws.set_rate(carnaby.id, paste.id, 40, date(2026, 3, 1))

    run = await ws.compute_run(date(2026, 3, 1), date(2026, 3, 31))
    line = None
    for l in run.lines:
        e = await db.get(em.Employee, l.employee_id)
        if e.name == "MD Afzal":
            line = l
    assert line is not None
    assert line.pieces == 35                      # 20 + 15
    assert float(line.amount) == 20 * 80 + 15 * 40  # 2200
