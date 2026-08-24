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
from app.modules.wages.schemas import RateSet
from app.modules.wages.service import WageService


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
async def test_one_employee_multiple_operations(db):
    client = cm.Client(name="C"); db.add(client); await db.flush()
    po = cm.ClientOrder(client_id=client.id, order_number="PO1"); db.add(po); await db.flush()
    carnaby = cm.Style(client_order_id=po.id, name="CARNABY", code="CARNABY", production_status="RELEASED"); db.add(carnaby); await db.flush()
    sku = cm.SKU(style_id=carnaby.id, color_code="57", size="M", qty_ordered=50)
    db.add(sku); await db.flush()
    cut = pm.Operation(code="CUTTING", label="Cutting", sequence=1)
    paste = pm.Operation(code="PASTING", label="Pasting", sequence=3)
    db.add_all([cut, paste]); await db.flush()
    multi = em.Employee(name="MD Afzal", designation="CUTTER/MULTI", wage_type=WageType.PIECE_RATE)
    db.add(multi); await db.commit()

    # Same worker logged at two operations on two days. log_event was removed; insert
    # the equivalent qty-grouped ProductionEvents directly — piece_counts groups by
    # (employee, style, operation, day), which is all the wage engine consumes.
    db.add_all([
        pm.ProductionEvent(sku_id=sku.id, operation_id=cut.id, employee_id=multi.id,
                           work_date=date(2026, 3, 23), qty=20),
        pm.ProductionEvent(sku_id=sku.id, operation_id=paste.id, employee_id=multi.id,
                           work_date=date(2026, 3, 25), qty=15),
    ])
    await db.commit()

    ws = WageService(db)
    await ws.set_rate(RateSet(style_code="CARNABY", operation_code="CUTTING",
                              rate=80, effective_from=date(2026, 3, 1)))
    await ws.set_rate(RateSet(style_code="CARNABY", operation_code="PASTING",
                              rate=40, effective_from=date(2026, 3, 1)))

    # A piece run names the work it pays for; this employee is piece-rate.
    run = await ws.compute_run(date(2026, 3, 1), date(2026, 3, 31),
                               run_kind="piece", style_code="CARNABY")
    detail = await ws.get_run_detail(run["id"])
    line = next((l for l in detail["lines"] if l["employee_name"] == "MD Afzal"), None)
    assert line is not None
    assert line["pieces"] == 35                        # 20 + 15
    assert float(line["amount"]) == 20 * 80 + 15 * 40  # 2200
