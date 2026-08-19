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
from app.modules.wages.schemas import RateSet
from app.modules.wages.service import WageService


async def _setup(db):
    client = cm.Client(name="C"); db.add(client); await db.flush()
    po = cm.ClientOrder(client_id=client.id, order_number="PO1"); db.add(po); await db.flush()
    carnaby = cm.Style(client_order_id=po.id, name="CARNABY", code="CARNABY", production_status="RELEASED"); db.add(carnaby); await db.flush()
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


@pytest.mark.xfail(
    reason="AUDIT F139 (BLOCKER, docs/audit/pass-01-business-logic.md): "
           "app/modules/production/repository.py:213,222 GROUP/ORDER BY "
           "Piece.style_id, a column Piece does not have (models.py:71-94); "
           "the SELECT correctly projects SKU.style_id. Constructing the "
           "statement raises AttributeError, so EVERY piece-rate wage run "
           "fails before emitting SQL. Not fixed here: audit rule is that "
           "application code is never edited to make a test pass. "
           "Flips to XPASS the moment the two-line fix lands.",
    raises=AttributeError, strict=False)
@pytest.mark.asyncio
async def test_carnaby_wage_matches_card(db):
    ops, carnaby, cutter, monthly = await _setup(db)
    skus = (await db.execute(select(cm.SKU).where(cm.SKU.style_id == carnaby.id))).scalars().all()
    assert sum(s.qty_ordered for s in skus) == 152

    ws = WageService(db)
    await ws.set_rate(RateSet(style_code="CARNABY", operation_code="CUTTING",
                              rate=80, effective_from=date(2026, 3, 1)))
    # log_event was removed (per-piece cut()/scan() flow now); the 152 cutting pieces
    # are inserted as ProductionEvents directly — the wage engine reads qty per
    # (employee, style, operation, day) and nothing else.
    for s in skus:
        db.add(pm.ProductionEvent(sku_id=s.id, operation_id=ops["CUTTING"].id,
                                  employee_id=cutter.id, work_date=date(2026, 3, 23),
                                  qty=s.qty_ordered))
    await db.commit()

    # THE FORTNIGHT IS TWO RUNS. A piece run must name the work it pays for; a
    # monthly run is priced from the calendar and takes no paying scope. Together
    # they are the whole payroll, and neither can pay the other's people.
    piece = await ws.compute_run(date(2026, 3, 1), date(2026, 3, 31),
                                 run_kind="piece", style_code="CARNABY")
    salaries = await ws.compute_run(date(2026, 3, 1), date(2026, 3, 31),
                                    run_kind="monthly")

    amounts = {}
    for run in (piece, salaries):
        detail = await ws.get_run_detail(run["id"])
        for line in detail["lines"]:
            e = await db.get(em.Employee, line["employee_id"])
            amounts[e.name] = float(line["amount"])
    assert amounts["Cutter1"] == 152 * 80     # 12160, matches the card
    assert amounts["Monthly1"] == 18000       # monthly independent of production


@pytest.mark.xfail(
    reason="AUDIT F139 (BLOCKER, docs/audit/pass-01-business-logic.md): "
           "app/modules/production/repository.py:213,222 GROUP/ORDER BY "
           "Piece.style_id, a column Piece does not have (models.py:71-94); "
           "the SELECT correctly projects SKU.style_id. Constructing the "
           "statement raises AttributeError, so EVERY piece-rate wage run "
           "fails before emitting SQL. Not fixed here: audit rule is that "
           "application code is never edited to make a test pass. "
           "Flips to XPASS the moment the two-line fix lands.",
    raises=AttributeError, strict=False)
@pytest.mark.asyncio
async def test_compute_run_returns_employee_level_lines(db):
    ops, carnaby, cutter, monthly = await _setup(db)
    skus = (await db.execute(select(cm.SKU).where(cm.SKU.style_id == carnaby.id))).scalars().all()
    ws = WageService(db)
    await ws.set_rate(RateSet(style_code="CARNABY", operation_code="CUTTING",
                              rate=80, effective_from=date(2026, 3, 1)))

    db.add(pm.ProductionEvent(sku_id=skus[0].id, operation_id=ops["CUTTING"].id,
                              employee_id=cutter.id, work_date=date(2026, 3, 23), qty=10))
    await db.commit()

    run = await ws.compute_run(date(2026, 3, 1), date(2026, 3, 31),
                               run_kind="piece", style_code="CARNABY")
    salaries = await ws.compute_run(date(2026, 3, 1), date(2026, 3, 31),
                                    run_kind="monthly")

    assert run["lines"]
    assert any(line["employee_name"] == "Cutter1" for line in run["lines"])
    # Monthly1 is on the OTHER run — that separation is the contract, not an
    # omission, so assert both halves of it.
    assert not any(line["employee_name"] == "Monthly1" for line in run["lines"])
    assert any(line["employee_name"] == "Monthly1" for line in salaries["lines"])
    cutter_line = next(line for line in run["lines"] if line["employee_name"] == "Cutter1")
    assert cutter_line["pieces"] == 10
    assert cutter_line["amount"] == 800


@pytest.mark.asyncio
async def test_pieces_do_not_need_to_conserve(db):
    ops, carnaby, cutter, monthly = await _setup(db)
    skus = (await db.execute(select(cm.SKU).where(cm.SKU.style_id == carnaby.id))).scalars().all()
    ps = ProductionService(db)
    # Cut 152, but paste 155 — the system must accept the spread, not reject it.
    # (log_event removed; insert the events directly, then read the stage totals.)
    db.add(pm.ProductionEvent(sku_id=skus[0].id, operation_id=ops["CUTTING"].id,
                              employee_id=cutter.id, work_date=date(2026, 3, 23), qty=152))
    db.add(pm.ProductionEvent(sku_id=skus[0].id, operation_id=ops["PASTING"].id,
                              employee_id=cutter.id, work_date=date(2026, 3, 24), qty=155))
    await db.commit()
    progress = await ps.style_progress(carnaby.id)
    assert progress["CUTTING"] == 152 and progress["PASTING"] == 155
