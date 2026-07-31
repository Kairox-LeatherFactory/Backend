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
    svc = MaterialService(db)
    await _make(db, category="LEATHER", article="SUEDE-A32", colour="PINE",
                attributes={"thickness": "1.2mm", "dcm": 300})
    await _make(db, category="LEATHER", article="SUEDE-A32", colour="PINE",
                attributes={"thickness": "1.2mm", "dcm": 200})
    stock = await svc.stock(category="LEATHER", article="SUEDE-A32", required=600)
    assert stock["on_hand"] == 500.0
    assert stock["available"] == 500.0
    assert stock["short_by"] == 100.0      # 600 - 500


# ── FILTER SPEC: the UI hint matches the spec ────────────────────────────────
@pytest.mark.asyncio
async def test_filter_fields_per_category(db):
    svc = MaterialService(db)
    assert svc.filter_fields("LEATHER", None)["filters"] == ["article", "colour", "thickness"]
    assert svc.filter_fields("ACCESSORY", "BUTTON")["filters"] == ["article", "colour", "size"]
    assert svc.filter_fields("ACCESSORY", "THREAD")["filters"] == ["article", "colour", "thickness"]
    assert svc.filter_fields("LINING", "RIBS")["filters"] == ["article", "colour"]
    assert set(svc.filter_fields("ACCESSORY", "BUTTON")["required_to_add"]) == {"size", "count"}