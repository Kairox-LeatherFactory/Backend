"""
================================================================================
tests/test_service_smoke.py — Service-layer smoke tests (async, in-memory SQLite)
================================================================================
Exercises the PUBLIC service interfaces of four modules against the `db` fixture
(conftest.py), using hand-built MOCK data (no real workbook needed here):

    clients      list_clients / create_client / create_order_with_breakdown /
                 get_style / get_sku / get_skus_for_style / get_client_orders /
                 sku_label
    employees    create / get / list_all / monthly_employees
    production   list_operations / cut / scan / log_event / style_progress /
                 piece_counts / list_events
    analytics    factory_overview / stage_spread_alerts / freight_risk /
                 production_feed / piece_history

MOCK DATA (one deterministic world, see _seed_catalog + _run_production):
    Client   MockCo (Italy, EUR)
    Order    MOCK-PO-1  ship=sea  sea_cutoff=2026-07-15
    Styles   CARNABY, CLERMONT
    SKUs     CARNABY/57 PINE GREEN/M qty=50 ; CLERMONT/J24 BLACK/L qty=30
    Ops      CUTTING(seq1), PASTING(seq3), FF(seq7)
    Workers  Afzal (piece_rate, cutter) ; Rahim (monthly, tailor, 20000)
    Actor    DIRECT_MANAGER (bypasses op-access; past work_date bypasses attendance)
    Flow     cut 40 CARNABY pieces -> scan 5 at PASTING -> scan 3 at FF
             + legacy log_event: 30 qty on CLERMONT at CUTTING (no piece linkage)
================================================================================
"""
import uuid
from datetime import date

import pytest

from app.core.enums import ShipMode, UserRole, WageType
from app.modules.analytics.service import AnalyticsService
from app.modules.clients import models as cm
from app.modules.clients.service import ClientService, sku_label
from app.modules.employees.service import EmployeeService
from app.modules.production import models as pm
from app.modules.production.service import ProductionService
from app.modules.users.models import User

PAST = date(2026, 3, 10)          # historical -> skips the attendance-today gate
TODAY = date(2026, 7, 12)         # matches the harness "current date"


def _actor() -> User:
    return User(id=uuid.uuid4(), name="Boss", phone="9000000001",
                role=UserRole.DIRECT_MANAGER, password_hash="x", is_active=True)


async def _seed_catalog(db) -> dict:
    """Insert the static mock world (no production events yet). Returns handles."""
    client = cm.Client(name="MockCo", country="Italy", currency="EUR")
    db.add(client); await db.flush()

    order = cm.ClientOrder(client_id=client.id, order_number="MOCK-PO-1",
                           ship_mode=ShipMode.SEA.value,
                           sea_cutoff_date=date(2026, 7, 15))
    db.add(order); await db.flush()

    carnaby = cm.Style(client_order_id=order.id, name="CARNABY")
    clermont = cm.Style(client_order_id=order.id, name="CLERMONT")
    db.add_all([carnaby, clermont]); await db.flush()

    sku_carnaby = cm.SKU(style_id=carnaby.id, color_code="57",
                         color_name="PINE GREEN", size="M", qty_ordered=50)
    sku_clermont = cm.SKU(style_id=clermont.id, color_code="J24",
                          color_name="BLACK", size="L", qty_ordered=30)
    db.add_all([sku_carnaby, sku_clermont]); await db.flush()

    cutting = pm.Operation(code="CUTTING", label="Cutting", sequence=1)
    pasting = pm.Operation(code="PASTING", label="Pasting", sequence=3)
    ff = pm.Operation(code="FF", label="Final finish", sequence=7)
    db.add_all([cutting, pasting, ff]); await db.flush()

    afzal = await EmployeeService(db).create(
        name="Afzal", designation="CUTTER", wage_type=WageType.PIECE_RATE,
        phone="9100000001", email="afzal@factory.local")
    rahim = await EmployeeService(db).create(
        name="Rahim", designation="TAILOR", wage_type=WageType.MONTHLY,
        monthly_salary=20000, phone="9100000002", email="rahim@factory.local")
    await db.commit()

    return dict(client=client, order=order, carnaby=carnaby, clermont=clermont,
                sku_carnaby=sku_carnaby, sku_clermont=sku_clermont,
                cutting=cutting, pasting=pasting, ff=ff, afzal=afzal, rahim=rahim)


async def _run_production(db, cat, actor) -> list[str]:
    """cut 40 CARNABY pieces, advance 5 to PASTING and 3 to FF, and log a legacy
    30-qty CLERMONT cutting event. Returns the minted piece codes."""
    ps = ProductionService(db)
    pieces = await ps.cut(user=actor, sku_id=cat["sku_carnaby"].id,
                          employee_id=cat["afzal"].id, work_date=PAST, count=40)
    codes = [p.code for p in pieces]

    await ps.scan(user=actor, operation_id=cat["pasting"].id,
                  employee_id=cat["afzal"].id, work_date=PAST,
                  piece_codes=codes[:5])
    await ps.scan(user=actor, operation_id=cat["ff"].id,
                  employee_id=cat["afzal"].id, work_date=PAST,
                  piece_codes=codes[:3])

    # legacy qty path (no piece linkage) on the other style
    await ps.log_event(actor, cat["sku_clermont"].id, cat["cutting"].id,
                       cat["afzal"].id, PAST, qty=30)
    return codes


# ────────────────────────────────────────────────────────────── CLIENTS
@pytest.mark.asyncio
async def test_clients_service(db):
    cat = await _seed_catalog(db)
    cs = ClientService(db)

    clients = await cs.list_clients()
    assert [c.name for c in clients] == ["MockCo"]

    made = await cs.create_client("Extra Buyer", "France")
    assert made.id is not None
    assert len(await cs.list_clients()) == 2

    style = await cs.get_style(cat["carnaby"].id)
    assert style is not None and style.name == "CARNABY"

    sku = await cs.get_sku(cat["sku_carnaby"].id)
    assert sku is not None and sku.qty_ordered == 50

    skus = await cs.get_skus_for_style(cat["carnaby"].id)
    assert {s.color_code for s in skus} == {"57"}

    orders = await cs.get_client_orders(cat["client"].id)
    assert [o.order_number for o in orders] == ["MOCK-PO-1"]

    # create_order_with_breakdown: dict-in, (order_id, style_id)-out
    order_id, style_id = await cs.create_order_with_breakdown(
        client_id=cat["client"].id,
        order={"order_number": "MOCK-PO-2"},
        style={"name": "TOWER"},
        lines=[{"color_code": "12", "color_name": "TAN", "sizes": {"M": 4, "L": 6}}],
    )
    assert order_id is not None and style_id is not None
    tower_skus = await cs.get_skus_for_style(style_id)
    assert sum(s.qty_ordered for s in tower_skus) == 10

    assert cs.sku_label("CARNABY", "PINE GREEN", "57", "M") == "CARNABY · PINE GREEN · M"
    assert sku_label("X", None, "57", None) == "X · 57 · NA"   # module-level fallback


# ────────────────────────────────────────────────────────────── EMPLOYEES
@pytest.mark.asyncio
async def test_employees_service(db):
    cat = await _seed_catalog(db)
    es = EmployeeService(db)

    everyone = await es.list_all()
    assert {e.name for e in everyone} == {"Afzal", "Rahim"}

    fetched = await es.get(cat["rahim"].id)
    assert fetched is not None and fetched.wage_type == WageType.MONTHLY

    monthly = await es.monthly_employees()
    assert [e.name for e in monthly] == ["Rahim"]        # Afzal is piece_rate

    fresh = await es.create(name="Zaid", designation="HELPER",
                            wage_type=WageType.PIECE_RATE, phone="9100000009")
    assert fresh.id is not None
    assert len(await es.list_all()) == 3


# ────────────────────────────────────────────────────────────── PRODUCTION
@pytest.mark.asyncio
async def test_production_service(db):
    cat = await _seed_catalog(db)
    ps = ProductionService(db)
    actor = _actor()

    ops = await ps.list_operations()
    assert {o.code for o in ops} == {"CUTTING", "PASTING", "FF"}

    codes = await _run_production(db, cat, actor)
    assert len(codes) == 40 and len(set(codes)) == 40      # unique piece codes

    progress = await ps.style_progress(cat["carnaby"].id)
    assert progress == {"CUTTING": 40, "PASTING": 5, "FF": 3}

    # legacy log_event landed on CLERMONT/CUTTING as a 30-qty event
    clermont_progress = await ps.style_progress(cat["clermont"].id)
    assert clermont_progress == {"CUTTING": 30}

    events = await ps.list_events(sku_id=cat["sku_carnaby"].id)
    assert len(events) == 40 + 5 + 3                       # cut + pasting + ff

    counts = await ps.piece_counts(PAST, PAST)
    assert sum(row[-1] for row in counts) == 40 + 5 + 3 + 30

    # scan rejects unknown codes without writing them
    res = await ps.scan(user=actor, operation_id=cat["pasting"].id,
                        employee_id=cat["afzal"].id, work_date=PAST,
                        piece_codes=["NOPE-1", "NOPE-2"])
    assert res["count_logged"] == 0 and len(res["not_found"]) == 2


# ────────────────────────────────────────────────────────────── ANALYTICS
@pytest.mark.asyncio
async def test_analytics_service(db):
    cat = await _seed_catalog(db)
    actor = _actor()
    codes = await _run_production(db, cat, actor)
    an = AnalyticsService(db)

    overview = await an.factory_overview()
    assert overview["clients"] == 1
    assert overview["styles"] == 2
    assert overview["total_pieces_ordered"] == 80          # 50 + 30
    assert overview["total_operations_logged"] == 78       # 40 + 5 + 3 + 30

    alerts = await an.stage_spread_alerts()
    carnaby_alerts = [a for a in alerts if a["style"] == "CARNABY"]
    assert carnaby_alerts, "expected a bottleneck alert on CARNABY (cut 40, few downstream)"
    assert all(a["cut"] == 40 for a in carnaby_alerts)

    risks = await an.freight_risk(today=TODAY)
    mine = [r for r in risks if r["order_number"] == "MOCK-PO-1"]
    assert mine, "order within sea-cutoff window should surface a freight risk"
    assert mine[0]["days_left"] == 3 and mine[0]["ordered"] == 80

    feed = await an.production_feed(style_id=cat["carnaby"].id)
    assert feed, "per-piece events should show in the feed"
    assert all("bundle_id" in row for row in feed)

    hist = await an.piece_history(codes[0])
    assert hist["bundle_id"] == codes[0].upper()
    assert len(hist["stages"]) >= 1
