"""
SYSTEM · the wage-run scope survives the HTTP layer.

The service-level tests prove the guard's logic. This proves the thing the
frontend actually calls: that `order_number` / `style_code` in the POST body
reach the scope, that two orders over the SAME dates both return 201, and that
a genuine double-payment still comes back as a 409 rather than a 500.
"""
from datetime import date, timedelta

import pytest

from app.core.enums import UserRole, WageType
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.wages.models import Rate

END = date.today() - timedelta(days=1)
START = END - timedelta(days=13)
DAY = START + timedelta(days=2)


async def _two_orders(db):
    client = cm.Client(name="HttpCo"); db.add(client); await db.flush()
    op = pm.Operation(code="LEATHER_CUTTING", label="LC", sequence=1)
    db.add(op); await db.flush()
    cutter = em.Employee(name="HTTPCUTTER", designation="CUTTER",
                         wage_type=WageType.PIECE_RATE, is_active=True)
    db.add(cutter); await db.flush()
    for tag in ("AAA", "BBB"):
        order = cm.ClientOrder(client_id=client.id, order_number=f"HTTP-{tag}")
        db.add(order); await db.flush()
        style = cm.Style(client_order_id=order.id, name=f"HS{tag}",
                         code=f"HS{tag}", production_status="RELEASED")
        db.add(style); await db.flush()
        sku = cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=10,
                     code=f"HTTP-{tag}-HS{tag}-57-M")
        db.add(sku); await db.flush()
        db.add(pm.ProductionEvent(sku_id=sku.id, operation_id=op.id,
                                  employee_id=cutter.id, work_date=DAY, qty=5,
                                  entered_by="t"))
        db.add(Rate(style_id=style.id, operation_id=op.id, rate=10.0,
                    effective_from=START))
    await db.commit()


def _body(**kw):
    return {"period_start": START.isoformat(), "period_end": END.isoformat(), **kw}


@pytest.mark.asyncio
async def test_two_orders_same_dates_both_return_201(db, api_client, as_role):
    await _two_orders(db)
    as_role(UserRole.DIRECT_MANAGER)

    a = await api_client.post("/api/v1/wages/runs", json=_body(order_number="HTTP-AAA"))
    b = await api_client.post("/api/v1/wages/runs", json=_body(order_number="HTTP-BBB"))

    assert a.status_code == 201, a.text
    assert b.status_code == 201, b.text
    assert a.json()["scope_order_number"] == "HTTP-AAA"
    assert b.json()["scope_order_number"] == "HTTP-BBB"
    # Each pays only its own order's 5 pieces.
    assert a.json()["total_pieces"] == 5
    assert b.json()["total_pieces"] == 5
    # A scoped run is piece-rate only — the screen must be able to say so.
    assert a.json()["piece_rate_only"] is True


@pytest.mark.asyncio
async def test_a_style_in_another_order_is_not_blocked_over_http(db, api_client, as_role):
    await _two_orders(db)
    as_role(UserRole.DIRECT_MANAGER)
    await api_client.post("/api/v1/wages/runs", json=_body(order_number="HTTP-AAA"))
    r = await api_client.post("/api/v1/wages/runs", json=_body(style_code="HSBBB"))
    assert r.status_code == 201, r.text
    assert r.json()["scope_style_code"] == "HSBBB"


@pytest.mark.asyncio
async def test_a_real_double_payment_is_a_409_not_a_500(db, api_client, as_role):
    await _two_orders(db)
    as_role(UserRole.DIRECT_MANAGER)
    await api_client.post("/api/v1/wages/runs", json=_body(order_number="HTTP-AAA"))
    r = await api_client.post("/api/v1/wages/runs", json=_body(style_code="HSAAA"))
    assert r.status_code == 409, r.text
    assert "paid twice" in r.json()["detail"]
