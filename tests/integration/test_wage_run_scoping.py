"""
INTEGRATION · a payroll run is scoped by PIECES, not by dates.

THE BUG THIS PINS
    Computing wages "date to date" was the only thing that reliably worked.
    Sending an order_number or a style_code was accepted, stored and even applied
    to the piece query — but the moment a second run touched the same fortnight
    the window guard rejected it, so in practice a manager could only ever run
    one payroll per date range for the whole factory.

    The guard compared the runs' scope KEYS as strings. A string cannot answer
    "does this style belong to that order", so it fell back to a conservative
    rule: any order-scoped run in the window blocked any style-scoped run — even
    for a style in a completely different order.

THE RULE NOW
    Two runs conflict only if they could pay the SAME GARMENT: their date
    windows intersect AND the sets of styles they pay intersect. Each scope is
    resolved to real style ids (WageService._scope_style_ids), so:

        unscoped        -> None (every style, present and future)
        style_code A    -> {A}
        order_number X  -> every style id in X

    Same dates + different order/style is normal, correct payroll and must never
    be refused. Same dates + genuinely shared styles is a double payment and must
    always be refused.
"""
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.core.enums import WageType
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.wages.models import Rate
from app.modules.wages.service import WageService

END = date.today() - timedelta(days=1)
START = END - timedelta(days=13)
DAY = START + timedelta(days=2)


async def _two_orders(db):
    """Two orders, one style each, both worked on the SAME day by one cutter."""
    client = cm.Client(name="ScopeCo")
    db.add(client)
    await db.flush()
    op = pm.Operation(code="LEATHER_CUTTING", label="LC", sequence=1)
    db.add(op)
    await db.flush()
    cutter = em.Employee(name="SCOPECUTTER", designation="CUTTER",
                         wage_type=WageType.PIECE_RATE, is_active=True)
    db.add(cutter)
    await db.flush()

    for tag in ("AAA", "BBB"):
        order = cm.ClientOrder(client_id=client.id, order_number=f"ORD-{tag}")
        db.add(order)
        await db.flush()
        style = cm.Style(client_order_id=order.id, name=f"STYLE{tag}",
                         code=f"STYLE{tag}", production_status="RELEASED")
        db.add(style)
        await db.flush()
        sku = cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=10,
                     code=f"ORD-{tag}-STYLE{tag}-57-M")
        db.add(sku)
        await db.flush()
        db.add(pm.ProductionEvent(sku_id=sku.id, operation_id=op.id,
                                  employee_id=cutter.id, work_date=DAY, qty=5,
                                  entered_by="t"))
        db.add(Rate(style_id=style.id, operation_id=op.id, rate=10.0,
                    effective_from=START))
    await db.commit()


# ── what must be ALLOWED ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_dates_different_orders_both_compute(db):
    """'DATE CAN BE SAME UNTIL ORDER CHANGE' — two orders, one fortnight."""
    await _two_orders(db)
    svc = WageService(db)
    a = await svc.compute_run(START, END, order_number="ORD-AAA")
    b = await svc.compute_run(START, END, order_number="ORD-BBB")
    # Each run pays ITS order only — 5 pieces at 10.00, never the other's.
    assert a["total_pieces"] == 5 and a["total_amount"] == 50.0
    assert b["total_pieces"] == 5 and b["total_amount"] == 50.0


@pytest.mark.asyncio
async def test_same_dates_different_styles_both_compute(db):
    await _two_orders(db)
    svc = WageService(db)
    a = await svc.compute_run(START, END, style_code="STYLEAAA")
    b = await svc.compute_run(START, END, style_code="STYLEBBB")
    assert a["total_pieces"] == 5
    assert b["total_pieces"] == 5


@pytest.mark.asyncio
async def test_an_order_run_does_not_block_a_style_in_a_different_order(db):
    """THE REGRESSION. This was refused by the old string-comparison guard,
    which blocked every style-scoped run once any order-scoped run existed."""
    await _two_orders(db)
    svc = WageService(db)
    await svc.compute_run(START, END, order_number="ORD-AAA")
    b = await svc.compute_run(START, END, style_code="STYLEBBB")
    assert b["total_pieces"] == 5


# ── what must still be REFUSED ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_an_order_run_blocks_a_style_that_belongs_to_it(db):
    """The other half of the same fix: a real intersection is still caught."""
    await _two_orders(db)
    svc = WageService(db)
    await svc.compute_run(START, END, order_number="ORD-AAA")
    with pytest.raises(HTTPException) as e:
        await svc.compute_run(START, END, style_code="STYLEAAA")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_the_same_order_twice_is_still_a_double_payment(db):
    await _two_orders(db)
    svc = WageService(db)
    await svc.compute_run(START, END, order_number="ORD-AAA")
    with pytest.raises(HTTPException) as e:
        await svc.compute_run(START, END, order_number="ORD-AAA")
    assert e.value.status_code == 409


@pytest.mark.asyncio
async def test_an_unscoped_run_still_covers_every_style_in_its_window(db):
    """An unscoped run already paid everything in those dates, so a scoped
    re-run over the same days would pay those same garments a second time."""
    await _two_orders(db)
    svc = WageService(db)
    await svc.compute_run(START, END)
    with pytest.raises(HTTPException) as e:
        await svc.compute_run(START, END, order_number="ORD-AAA")
    assert e.value.status_code == 409
    assert "paid twice" in e.value.detail
