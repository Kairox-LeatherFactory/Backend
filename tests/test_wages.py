"""Wages tie out to the production card, and pieces need not conserve (async)."""
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


async def _setup(db):
    client = cm.Client(name="C"); db.add(client); await db.flush()
    po = cm.PurchaseOrder(client_id=client.id, po_number="PO1"); db.add(po); await db.flush()
    carnaby = cm.Style(purchase_order_id=po.id, name="CARNABY"); db.add(carnaby); await db.flush()
    # 152 ordered pieces across a couple of SKUs (matches the real Carnaby card).
    db.add_all([
        cm.SKU(style_id=carnaby.id, color_code="57", size="S", qty_ordered=100),
        cm.SKU(style_id=carnaby.id, color_code="57", size="M", qty_ordered=52),
    ])
    ops = {}
    for code, seq in [("CUTTING", 1), ("PASTING", 3)]:
        o = pm.Operation(code=code, label=code, sequence=seq)
        db.add(o); await db.flush(); ops[code] = o
    cutter = em.Employee(name="Cutter1", wage_type=WageType.PIECE_RATE)
    monthly = em.Employee(name="Monthly1", wage_type=WageType.MONTHLY, monthly_salary=18000)
    db.add_all([cutter, monthly]); await db.commit()
    return ops, carnaby, cutter, monthly


@pytest.mark.asyncio
async def test_carnaby_wage_matches_card(db):
    ops, carnaby, cutter, monthly = await _setup(db)
    skus = (await db.execute(select(cm.SKU).where(cm.SKU.style_id == carnaby.id))).scalars().all()
    assert sum(s.qty_ordered for s in skus) == 152

    ws = WageService(db)
    await ws.set_rate(carnaby.id, ops["CUTTING"].id, 80, date(2026, 3, 1))
    ps = ProductionService(db)
    direct = User(id=uuid.uuid4(), name="D", phone="2", role=UserRole.DIRECT_MANAGER,
                  password_hash="x", is_active=True)
    for s in skus:
        await ps.log_event(direct, s.id, ops["CUTTING"].id, cutter.id, date(2026, 3, 23), s.qty_ordered)

    run = await ws.compute_run(date(2026, 3, 1), date(2026, 3, 31))
    amounts = {}
    for line in run.lines:
        e = await db.get(em.Employee, line.employee_id)
        amounts[e.name] = float(line.amount)
    assert amounts["Cutter1"] == 152 * 80     # 12160, matches the card
    assert amounts["Monthly1"] == 18000       # monthly independent of production


@pytest.mark.asyncio
async def test_pieces_do_not_need_to_conserve(db):
    ops, carnaby, cutter, monthly = await _setup(db)
    skus = (await db.execute(select(cm.SKU).where(cm.SKU.style_id == carnaby.id))).scalars().all()
    ps = ProductionService(db)
    direct = User(id=uuid.uuid4(), name="D", phone="2", role=UserRole.DIRECT_MANAGER,
                  password_hash="x", is_active=True)
    # Cut 152, but paste 155 — the system must accept the spread, not reject it.
    await ps.log_event(direct, skus[0].id, ops["CUTTING"].id, cutter.id, date(2026, 3, 23), 152)
    ev = await ps.log_event(direct, skus[0].id, ops["PASTING"].id, cutter.id, date(2026, 3, 24), 155)
    assert ev.qty == 155
    progress = await ps.style_progress(carnaby.id)
    assert progress["CUTTING"] == 152 and progress["PASTING"] == 155
