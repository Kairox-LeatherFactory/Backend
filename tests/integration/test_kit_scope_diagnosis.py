"""
INTEGRATION · a kit scan that finds nothing must say WHICH nothing.

THE FIELD REPORT. `/styles/{id}/material-spec/requirement` showed a healthy
three-line recipe — THREAD, GOAT_SUEDE, KNIT — and the store scan on a garment of
that style answered:

    "BF27P010501 has no accessory spec — there is nothing to kit for
     2222-BF27P010501-SUEDE_BOMBER-NAVY-S-004. If this style does take
     accessories, add them to its material spec first."

Both statements were true and nothing said so. The requirement view is
STYLE-WIDE: it lists every line on the style whatever its scope. A kit is issued
PER GARMENT: `merge_lines` keeps the style-wide lines plus the lines scoped to
THIS piece's own SKU, and drops the ones scoped to another colourway — because
issuing the PINE GREEN knit to a NAVY jacket puts the wrong colour in the bag.
Every line in that spec was `scope: "SKU"` against one colourway, and the
scanned garment was a different one.

The filter is right. The message was not: it sent the DM to add lines that were
already there. These tests pin the diagnosis, not the filter.
"""
import datetime

import pytest
from fastapi import HTTPException

from app.modules.barcode.models import MaterialLot, StyleMaterialSpec
from app.modules.clients.models import SKU
from app.modules.materials.style_spec_service import StyleSpecService
from app.modules.production.models import Piece

pytestmark = [pytest.mark.asyncio, pytest.mark.integrity]


@pytest.fixture
async def two_colourways(db, order_tree):
    """One style, two SKUs: the seeded PINE GREEN one and a NAVY one."""
    style = order_tree["style"]
    navy = SKU(style_id=style.id, color_code="NAVY", color_name="NAVY",
               size="S", qty_ordered=10, code="JP-CLERMONT-NAVY-S")
    db.add(navy)
    await db.flush()
    piece = Piece(code="JP-CLERMONT-NAVY-S-004", seq=4, sku_id=navy.id)
    db.add(piece)
    await db.commit()
    await db.refresh(navy)
    await db.refresh(piece)
    return {"style": style, "pine": order_tree["sku"], "navy": navy,
            "piece": piece}


async def _thread_lot(db):
    lot = MaterialLot(category="ACCESSORY", subtype="THREAD", article="THREAD",
                      colour="NAVY", uom="mtrs", on_hand=20000, is_active=True)
    db.add(lot)
    await db.commit()
    await db.refresh(lot)
    return lot


# ══════════════════════════ 1 · the scope case — the one that was reported
async def test_a_line_scoped_to_another_colourway_says_so(db, two_colourways,
                                                          cutter):
    """THE REPORTED BUG. The style HAS the accessory; this garment is not on it."""
    t = two_colourways
    await _thread_lot(db)
    db.add(StyleMaterialSpec(
        style_id=t["style"].id, sku_id=t["pine"].id,      # PINE GREEN only
        category="ACCESSORY", subtype="THREAD", article="THREAD",
        colour="NAVY", qty_per_piece=1, uom="mtrs"))
    await db.commit()

    svc = StyleSpecService(db)
    with pytest.raises(HTTPException) as exc:
        await svc.issue_kit_nocommit(piece=t["piece"], drawer=None,
                                     employee_id=cutter[0].id)

    detail = str(exc.value.detail)
    assert exc.value.status_code == 409
    # It must NOT claim the style has no spec — it plainly does.
    assert "has no accessory spec" not in detail
    assert "1 accessory line" in detail
    assert "THREAD" in detail
    assert "scoped to other colourways" in detail
    assert "PINE GREEN" in detail, "name the colourway that DOES own the line"
    assert "/materials" in detail, "point at the read that shows both halves"


async def test_the_same_line_made_style_wide_issues_normally(db, two_colourways,
                                                             cutter):
    """THE FIX THE MESSAGE TELLS YOU TO MAKE, proven to work.

    Clearing `sku_id` is the DM's answer when the accessory really is for every
    colourway — which is what "assigned in the breakdown sheet" usually means.
    """
    t = two_colourways
    lot = await _thread_lot(db)
    db.add(StyleMaterialSpec(
        style_id=t["style"].id, sku_id=None,              # style-wide
        category="ACCESSORY", subtype="THREAD", article="THREAD",
        colour="NAVY", qty_per_piece=1, uom="mtrs",
        material_lot_id=lot.id))
    await db.commit()

    kit = await StyleSpecService(db).issue_kit_nocommit(
        piece=t["piece"], drawer=None, employee_id=cutter[0].id)
    await db.commit()
    assert kit["status"] == "ISSUED"
    assert [r["article"] for r in kit["issued_now"]] == ["THREAD"]


# ══════════════════════════ 2 · the size case — an L zip is not an S zip
async def test_a_line_for_another_garment_size_says_so(db, two_colourways,
                                                       cutter):
    t = two_colourways                       # the piece is a size S
    await _thread_lot(db)
    db.add(StyleMaterialSpec(
        style_id=t["style"].id, sku_id=None,
        category="ACCESSORY", subtype="ZIP", article="ZIP-YKK",
        colour="NAVY", size="L", garment_size="L",
        qty_per_piece=1, uom="pcs"))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).issue_kit_nocommit(
            piece=t["piece"], drawer=None, employee_id=cutter[0].id)
    detail = str(exc.value.detail)
    assert "garment size(s) L" in detail
    assert "an L zip is not an S zip" in detail


# ══════════════════════════ 3 · a genuinely bare style keeps the old message
async def test_a_style_with_no_accessory_lines_still_says_add_them(
        db, two_colourways, cutter):
    """The message that was right all along, now only shown when it IS right."""
    t = two_colourways
    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).issue_kit_nocommit(
            piece=t["piece"], drawer=None, employee_id=cutter[0].id)
    assert "has no accessory spec" in str(exc.value.detail)


# ══════════════════════════ 4 · the read that shows BOTH halves
async def test_the_piece_materials_read_shows_what_does_not_apply(
        db, two_colourways):
    """The screen that makes the whole class of confusion visible.

    `applies` is this garment's recipe; `not_applicable` is every other line on
    the style WITH ITS REASON. Nothing showed the second half before, which is
    why the requirement view and the scan looked like they disagreed.
    """
    t = two_colourways
    await _thread_lot(db)
    db.add(StyleMaterialSpec(
        style_id=t["style"].id, sku_id=t["pine"].id,
        category="ACCESSORY", subtype="THREAD", article="THREAD",
        colour="NAVY", qty_per_piece=1, uom="mtrs"))
    db.add(StyleMaterialSpec(
        style_id=t["style"].id, sku_id=None,
        category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
        colour="NAVY", qty_per_piece=4, uom="pcs"))
    await db.commit()

    out = await StyleSpecService(db).piece_materials(t["piece"].id)

    assert out["piece_code"] == "JP-CLERMONT-NAVY-S-004"
    assert [a["article"] for a in out["applies"]["accessories"]] == ["BTN-4H"]
    assert len(out["not_applicable"]) == 1
    dropped = out["not_applicable"][0]
    assert dropped["article"] == "THREAD"
    assert dropped["reason"] == "other_sku"
    assert "PINE GREEN" in dropped["reason_note"]
    assert out["issued"] == [], "nothing has been issued to this garment yet"


async def test_piece_materials_carries_the_issue_ledger(db, two_colourways,
                                                        cutter):
    """`issued` is what physically went in — lot, quantity, time, card."""
    t = two_colourways
    lot = await _thread_lot(db)
    db.add(StyleMaterialSpec(
        style_id=t["style"].id, sku_id=None,
        category="ACCESSORY", subtype="THREAD", article="THREAD",
        colour="NAVY", qty_per_piece=1, uom="mtrs", material_lot_id=lot.id))
    await db.commit()
    svc = StyleSpecService(db)
    await svc.issue_kit_nocommit(piece=t["piece"], drawer=None,
                                 employee_id=cutter[0].id, entered_by="STORE")
    await db.commit()

    out = await svc.piece_materials(t["piece"].id)
    assert len(out["issued"]) == 1
    row = out["issued"][0]
    assert row["article"] == "THREAD"
    assert row["qty"] == 1.0 and row["uom"] == "mtrs"
    assert row["material_lot_id"] == str(lot.id)
    assert row["issued_by_employee_id"] == str(cutter[0].id)
    assert row["source"] == "STORE_KIT"
    assert isinstance(row["issued_at"], datetime.datetime)
