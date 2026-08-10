"""
================================================================================
tests/integration/test_materials_strict.py — per-category material validation
================================================================================
Proves the STRICT "Add New" rules straight from the product spec: every category
and subtype accepts exactly its required fields and REJECTS a lot missing any of
them. Also proves stock arithmetic (on-hand/reserved/available), the quantity is
read from the right field per category, and the barcode caption is tailored.
================================================================================
"""
import pytest

from app.modules.materials.schemas import LotCreate
from app.modules.materials.service import MaterialService

from starlette.exceptions import HTTPException


async def _make(db, **kw):
    return await MaterialService(db).create_lot(LotCreate(**kw))


# ── ACCEPTS: every material class with its exact fields ──────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("kw,expect_uom,expect_qty", [
    # leather: article, colour, thickness, dcm
    (dict(category="LEATHER", article="SUEDE-A32", colour="PINE",
          attributes={"thickness": "1.2mm", "dcm": 300}), "dcm", 300.0),
    # plain lining: article, colour, thickness, mtrs
    (dict(category="LINING", subtype="PLAIN_LINING", article="POLY-L", colour="BLACK",
          attributes={"thickness": "0.3mm", "mtrs": 120}), "mtrs", 120.0),
    # ribs: article, colour, kg
    (dict(category="LINING", subtype="RIBS", article="RIB-2", colour="NAVY",
          attributes={"kg": 15}), "kg", 15.0),
    # knit: article, colour, pcs
    (dict(category="LINING", subtype="KNIT", article="KNIT-9", colour="GREY",
          attributes={"pcs": 40}), "pcs", 40.0),
    # button: article, colour, size, count
    (dict(category="ACCESSORY", subtype="BUTTON", article="BTN-17", colour="BROWN",
          attributes={"size": "20L", "count": 500}), "pcs", 500.0),
    # zip: article, colour, size, count
    (dict(category="ACCESSORY", subtype="ZIP", article="YKK-5", colour="BLACK",
          attributes={"size": "60cm", "count": 200}), "pcs", 200.0),
    # thread: article, colour, thickness, mtrs
    (dict(category="ACCESSORY", subtype="THREAD", article="TEX-40", colour="TAN",
          attributes={"thickness": "40wt", "mtrs": 5000}), "mtrs", 5000.0),
    # other: article, colour, description, count
    (dict(category="ACCESSORY", subtype="OTHER", article="LOGO-TAG", colour="—",
          attributes={"description": "woven brand tag", "count": 300}), "pcs", 300.0),
])
async def test_create_each_material_class(db, kw, expect_uom, expect_qty):
    res = await _make(db, **kw)
    assert res["uom"] == expect_uom
    assert res["on_hand"] == expect_qty
    assert res["available"] == expect_qty
    assert res["lot_barcode"]           # a child barcode was minted


# ── REJECTS: missing a required field per category ───────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("kw,missing", [
    # leather without thickness
    (dict(category="LEATHER", article="A", colour="C", attributes={"dcm": 100}), "thickness"),
    # leather without dcm (quantity)
    (dict(category="LEATHER", article="A", colour="C", attributes={"thickness": "1mm"}), "dcm"),
    # button without size
    (dict(category="ACCESSORY", subtype="BUTTON", article="B", colour="C",
          attributes={"count": 100}), "size"),
    # zip without count
    (dict(category="ACCESSORY", subtype="ZIP", article="Z", colour="C",
          attributes={"size": "50cm"}), "count"),
    # thread without thickness
    (dict(category="ACCESSORY", subtype="THREAD", article="T", colour="C",
          attributes={"mtrs": 100}), "thickness"),
    # other without description
    (dict(category="ACCESSORY", subtype="OTHER", article="O", colour="C",
          attributes={"count": 10}), "description"),
    # ribs without kg
    (dict(category="LINING", subtype="RIBS", article="R", colour="C",
          attributes={}), "kg"),
])
async def test_reject_missing_required_field(db, kw, missing):
    with pytest.raises(Exception) as ei:
        await _make(db, **kw)
    assert "422" in str(ei.value) or "requires" in str(ei.value).lower()


# ── REJECTS: structural errors ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_accessory_needs_subtype(db):
    with pytest.raises(Exception) as ei:
        await _make(db, category="ACCESSORY", article="X", colour="C",
                    attributes={"count": 5})
    assert "subtype" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_unknown_category_rejected(db):
    with pytest.raises(Exception):
        await _make(db, category="FOAM", article="X", colour="C",
                    attributes={"qty": 5})


@pytest.mark.asyncio
async def test_missing_colour_rejected(db):
    with pytest.raises(Exception) as ei:
        await _make(db, category="LEATHER", article="A", colour="",
                    attributes={"thickness": "1mm", "dcm": 100})
    assert "colour" in str(ei.value).lower()


@pytest.mark.asyncio
async def test_zero_quantity_rejected(db):
    with pytest.raises(Exception) as ei:
        await _make(db, category="LEATHER", article="A", colour="C",
                    attributes={"thickness": "1mm", "dcm": 0})
    assert "> 0" in str(ei.value) or "quantity" in str(ei.value).lower()


# ── STOCK: on-hand / reserved / available across lots of same article ────────
@pytest.mark.asyncio
async def test_stock_aggregates_and_reserves(db):
    """stock() sums every lot the FILTER matches.

    UPDATED FOR OPTION A (one lot per material spec). This used to create the
    same article+colour+thickness twice, which create_lot now refuses with a 409
    — re-supply tops the existing lot up via /materials/receive instead. The
    aggregation being tested is still real and still matters: a filter broader
    than the full spec (here article only, no colour) legitimately spans several
    lots, and that is exactly the "how much SUEDE-A32 do we have in total?"
    question the stock screen asks.
    """
    svc = MaterialService(db)
    await _make(db, category="LEATHER", article="SUEDE-A32", colour="PINE",
                attributes={"thickness": "1.2mm", "dcm": 300})
    await _make(db, category="LEATHER", article="SUEDE-A32", colour="FOREST",
                attributes={"thickness": "1.2mm", "dcm": 200})
    stock = await svc.stock(category="LEATHER", article="SUEDE-A32", required=600)
    assert stock["on_hand"] == 500.0
    assert stock["available"] == 500.0
    assert stock["lot_count"] == 2
    assert stock["short_by"] == 100.0      # 600 - 500

    # …and narrowing to the full spec resolves to exactly one lot — the promise
    # the cut-screen picker depends on.
    one = await svc.stock(category="LEATHER", article="SUEDE-A32",
                          colour="PINE", thickness="1.2mm")
    assert one["lot_count"] == 1 and one["on_hand"] == 300.0


# ── FILTER SPEC: the UI hint matches the spec ────────────────────────────────
@pytest.mark.asyncio
async def test_filter_fields_per_category(db):
    svc = MaterialService(db)
    assert svc.filter_fields("LEATHER", None)["filters"] == ["article", "colour", "thickness"]
    assert svc.filter_fields("ACCESSORY", "BUTTON")["filters"] == ["article", "colour", "size"]
    assert svc.filter_fields("ACCESSORY", "THREAD")["filters"] == ["article", "colour", "thickness"]
    assert svc.filter_fields("LINING", "RIBS")["filters"] == ["article", "colour"]
    assert set(svc.filter_fields("ACCESSORY", "BUTTON")["required_to_add"]) == {"size", "count"}
    
@pytest.mark.asyncio
async def test_order_rejects_supplier_without_article(db, dm):
    from app.modules.barcode.models import MaterialSupplier
    from app.modules.materials.schemas import SupplierOrderCreate
    s = MaterialSupplier(name="X", articles="NAP-11", is_active=True)
    db.add(s); await db.commit(); await db.refresh(s)
    with pytest.raises(HTTPException) as e:
        await MaterialService(db).create_order(
            SupplierOrderCreate(category="LEATHER", article="SUEDE-A32", qty=100,
                                supplier_id=s.id),
            actor_id=dm.id, actor_role=dm.role)
    assert e.value.status_code == 422
 
@pytest.mark.asyncio
async def test_receive_rejects_mismatch(db, dm):
    from app.modules.materials.schemas import (LotCreate, ReceiveRequest,
                                               SupplierOrderCreate)
    await MaterialService(db).create_lot(LotCreate(
        category="LEATHER", article="GOAT-SUEDE", colour="PINE",
        attributes={"thickness": "1.2mm", "dcm": 100}))
    order = await MaterialService(db).create_order(
        SupplierOrderCreate(category="LEATHER", article="SHEEP-NAPPA",
                            colour="PINE", thickness="1.2mm", dcm=100, qty=100),
        actor_id=dm.id, actor_role=dm.role)
    from app.modules.barcode.models import MaterialLot
    from sqlalchemy import select
    lot = await db.scalar(select(MaterialLot).where(MaterialLot.article == "GOAT-SUEDE"))
    with pytest.raises(HTTPException) as e:
        await MaterialService(db).receive(ReceiveRequest(
            lot_id=lot.id, supplier_order_id=order["order_id"], approved_qty=100),
            actor_id=dm.id, actor_role=dm.role)
    assert e.value.status_code == 409   # article mismatch → rejected
 
@pytest.mark.asyncio
async def test_dm_can_approve_mismatch_into_new_lot(db, dm):
    from app.modules.materials.schemas import (LotCreate, ReceiveRequest,
                                               SupplierOrderCreate)
    from app.core.enums import UserRole
    await MaterialService(db).create_lot(LotCreate(
        category="LEATHER", article="GOAT-SUEDE", colour="PINE",
        attributes={"thickness": "1.2mm", "dcm": 100}))
    order = await MaterialService(db).create_order(
        SupplierOrderCreate(category="LEATHER", article="SHEEP-NAPPA",
                            colour="PINE", thickness="1.2mm", dcm=100, qty=100),
        actor_id=dm.id, actor_role=dm.role)
    from app.modules.barcode.models import MaterialLot
    from sqlalchemy import select, func
    lot = await db.scalar(select(MaterialLot).where(MaterialLot.article == "GOAT-SUEDE"))
    res = await MaterialService(db).receive(ReceiveRequest(
        lot_id=lot.id, supplier_order_id=order["order_id"], approved_qty=100,
        approve_mismatch=True), actor_id=dm.id, actor_role=UserRole.DIRECT_MANAGER)
    assert res["substituted"] is True
    assert "article" in res["mismatch_fields"]
    # a NEW lot was created; the original GOAT-SUEDE lot stays at 0 on_hand
    assert res["lot_id"] != lot.id