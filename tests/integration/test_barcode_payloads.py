"""
INTEGRATION · the resolve() payloads — the shape every scanner screen reads.

WHY THIS FILE EXISTS
    resolve() is the one front door (CLAUDE.md §6): a scan comes in, a type + a
    live payload goes out, and every floor screen branches on that dict. The
    payload builders were rewritten to read through BarcodeRepository with
    column-scoped queries (one query per card instead of a piece join + a drawer
    get + a consumption select), so these tests pin the CONTRACT — the exact keys
    and values — rather than the implementation that produces it.

    The lot card derives `available` in SQL; test_lot_payload asserts it equals
    MaterialService.available_for_lot, which stays the authority for the material
    module. If those two ever drift, this fails.
"""
from datetime import date
from decimal import Decimal

import pytest

from app.modules.barcode.models import MaterialReservation
from app.modules.barcode.service import BarcodeService
from app.modules.production.models import ProductionEvent

pytestmark = pytest.mark.integrity


@pytest.mark.asyncio
async def test_piece_payload_carries_the_whole_traveler_card(
    db, pieces, operations, cutter, leather_lot
):
    """Piece → order/style/colour/size/seq + stage + drawer + consumption. The
    drawer and the consumption are LEFT joins: null before merge / before cutting
    is a normal state, never an error."""
    p, drawer = pieces[0]
    out = await BarcodeService(db).resolve(p.code)
    assert out["type"].upper() == "PIECE"

    pl = out["piece"]
    assert pl["piece_id"] == str(p.id)
    assert pl["code"] == p.code
    assert pl["sku_code"] == "JP-CLERMONT-PINE-M"
    assert pl["style_name"] == "CLERMONT"
    assert pl["colour"] == "PINE GREEN"
    assert pl["size"] == "M"
    assert pl["seq"] == 1
    assert pl["order_number"] == "JP-PO"
    assert pl["client"] == "John Peter"
    assert pl["current_stage"] is None          # not yet logged at any stage
    assert pl["drawer_code"] == drawer.code
    assert pl["leather_consumption_dcm"] is None
    assert pl["needs_lining"] is True

    # cut it: the consumption recorded on the EVENT surfaces on the card
    emp, _ = cutter
    db.add(ProductionEvent(
        sku_id=p.sku_id, operation_id=operations["LEATHER_CUTTING"].id,
        employee_id=emp.id, work_date=date.today(), qty=1, piece_id=p.id,
        leather_lot_id=leather_lot.id, consumption_qty=Decimal("12.5")))
    await db.commit()

    pl = (await BarcodeService(db).resolve(p.code))["piece"]
    assert pl["leather_consumption_dcm"] == 12.5


@pytest.mark.asyncio
async def test_drawer_payload_reports_state_and_contents(db, pieces):
    p, drawer = pieces[2]
    d = (await BarcodeService(db).resolve(drawer.code))["drawer"]
    assert d["drawer_code"] == drawer.code
    assert d["seq"] == 3
    assert d["state"].upper() == "MERGED"
    assert d["current_piece_id"] == str(p.id)
    assert d["leather_in"] is False and d["lining_in"] is False


@pytest.mark.asyncio
async def test_lot_payload_available_is_on_hand_minus_active_reservations(
    db, leather_lot
):
    from app.modules.materials.service import MaterialService

    lot = (await BarcodeService(db).resolve("LOT-LEA-000001"))["lot"]
    assert lot["article"] == "SUEDE-A32"
    assert lot["on_hand"] == 1000.0
    assert lot["available"] == 1000.0

    db.add(MaterialReservation(material_lot_id=leather_lot.id,
                               qty=Decimal("100"), status="active"))
    db.add(MaterialReservation(material_lot_id=leather_lot.id,
                               qty=Decimal("50"), status="released"))
    await db.commit()

    lot = (await BarcodeService(db).resolve("LOT-LEA-000001"))["lot"]
    assert lot["on_hand"] == 1000.0             # a reservation never moves stock
    assert lot["available"] == 900.0            # released one does not count
    assert await MaterialService(db).available_for_lot(leather_lot.id) == 900.0


@pytest.mark.asyncio
async def test_detail_print_and_order_helpers(db, pieces, order_tree):
    svc = BarcodeService(db)
    p, _ = pieces[0]

    # /detail returns the same payload as /resolve
    assert (await svc.barcode_detail(p.code))["piece"]["code"] == p.code

    # print by order expands to every piece label under it
    out = await svc.print_payload(order_id=order_tree["order"].id)
    assert len(out["labels"]) == 5
    assert all(lbl["known"] and lbl["symbology"] == "code128"
               for lbl in out["labels"])

    # codes are normalised; an unknown code is labelled, not dropped
    out = await svc.print_payload(codes=[p.code.lower(), "NOPE-1"])
    assert out["labels"][0]["known"] is True
    assert out["labels"][0]["code"] == p.code
    assert out["labels"][1] == {"code": "NOPE-1", "symbology": "code128",
                                "caption": "NOPE-1", "known": False}

    oid = order_tree["order"].id
    assert await svc.resolve_order_id("JP-PO") == oid       # human order_number
    assert await svc.resolve_order_id(str(oid)) == oid      # uuid as a string
    assert await svc.resolve_order_id(oid) == oid           # uuid
    assert [s["sku_code"] for s in await svc.list_order_skus(oid)] == [
        "JP-CLERMONT-PINE-M"]
    assert (await svc.order_analytics(oid))["order_total"]["planned"] == 5
    assert (await svc.list_history(oid, page=1, page_size=10))["page"] == 1
