"""
INTEGRATION · a style cannot be released into production without its recipe.

WHY THE GATE IS AT RELEASE AND NOWHERE ELSE
    Release is the last moment anyone can be asked what one of these garments
    takes. Before it the style is a spreadsheet row; after it there are barcoded
    pieces in drawers, the recipe is frozen, and it is already being spent. So
    "12.5 dcm, 4 buttons, 1 zip" is asked exactly where "does this take a
    lining?" already is.

    The gate REJECTS PER STYLE, in the same partial-accept shape every other
    reason in release_styles uses: one unspecced style must never lose the four
    the DM ticked with it.
"""
import pytest
from sqlalchemy import func, select

from app.modules.barcode.models import StyleMaterialSpec
from app.modules.clients.models import SKU, Style
from app.modules.imports.breakdown import BreakdownService
from app.modules.materials.style_spec_service import StyleSpecService
from app.modules.production.models import Piece

pytestmark = pytest.mark.integrity


@pytest.fixture
async def draft_style(db, order_tree):
    """A style as it lands from a breakdown upload: DRAFT, ordered, no recipe."""
    style = Style(client_order_id=order_tree["order"].id, name="ASTON",
                  article="AS1", code="JP-ASTON", production_status="DRAFT")
    db.add(style)
    await db.flush()
    db.add(SKU(style_id=style.id, color_code="BLK", color_name="BLACK",
               size="M", qty_ordered=10, code="JP-ASTON-BLK-M"))
    await db.commit()
    await db.refresh(style)
    return style


def _add(db, style, **kw):
    kw.setdefault("uom", "pcs")
    db.add(StyleMaterialSpec(style_id=style.id, **kw))


# ══════════════════════════════════════════════ the gate refuses, and mints nothing
@pytest.mark.asyncio
async def test_an_unconfirmed_style_is_rejected_and_mints_no_pieces(
        db, order_tree, draft_style):
    """The important half is the second clause: a rejection that still minted
    would leave barcoded garments behind a gate that said no."""
    before = int(await db.scalar(select(func.count()).select_from(Piece)))

    res = await BreakdownService(db).release_styles(
        order_tree["order"].order_number, [draft_style.id], user_name="DM")

    assert res["released"] == []
    row = res["rejected"][0]
    assert row["style_code"] == "JP-ASTON"
    assert "material spec has not been confirmed" in row["reason"]
    # The individual sentences ride alongside, so the screen can list them
    # against the row rather than only showing the joined `reason`.
    assert len(row["blockers"]) >= 1
    assert int(await db.scalar(select(func.count()).select_from(Piece))) == before
    await db.refresh(draft_style)
    assert draft_style.production_status == "DRAFT"


@pytest.mark.asyncio
async def test_one_unspecced_style_does_not_lose_the_others(
        db, order_tree, draft_style):
    """PARTIAL ACCEPT, the rule every batch surface in this codebase follows."""
    good = Style(client_order_id=order_tree["order"].id, name="BRENT",
                 article="BR1", code="JP-BRENT", production_status="DRAFT")
    db.add(good)
    await db.flush()
    db.add(SKU(style_id=good.id, color_code="TAN", color_name="TAN", size="L",
               qty_ordered=4, code="JP-BRENT-TAN-L"))
    _add(db, good, category="LEATHER", article="SUEDE-A32", qty_per_piece=12.5,
         uom="dcm")
    await db.commit()
    await StyleSpecService(db).confirm(good.id, no_accessories=True,
                                       actor_name="DM")

    blockers = await StyleSpecService(db).blockers_for_styles(
        [draft_style.id, good.id])

    assert blockers[good.id] == []                 # ready to release
    assert blockers[draft_style.id] != []          # held back, on its own


# ══════════════════════════════════════════════════ the blocker truth table
@pytest.mark.asyncio
async def test_a_confirmed_style_with_no_leather_line_is_still_blocked(
        db, draft_style):
    """"The user must enter the dcm consumption per piece" is the explicit ask,
    so a missing LEATHER line is a hard blocker, not a warning. The dcm is what
    the material ledger and the costing are built on."""
    _add(db, draft_style, category="ACCESSORY", subtype="BUTTON",
         article="BTN-4H", qty_per_piece=4)
    await db.commit()
    await StyleSpecService(db).confirm(draft_style.id, no_accessories=False,
                                       actor_name="DM")

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [draft_style.id]))[draft_style.id]

    assert any("no LEATHER line" in b for b in blockers)


@pytest.mark.asyncio
async def test_an_empty_accessory_list_needs_an_explicit_declaration(
        db, draft_style):
    """A recipe with no accessory lines is AMBIGUOUS on its own: a garment that
    genuinely takes none, or one whose buttons nobody entered yet. Releasing the
    second kind silently is how a whole order reaches the store with no kit — so
    NULL (nobody asked) does not pass, and only an explicit True does."""
    _add(db, draft_style, category="LEATHER", article="SUEDE-A32",
         qty_per_piece=12.5, uom="dcm")
    await db.commit()
    svc = StyleSpecService(db)

    await svc.confirm(draft_style.id, no_accessories=False, actor_name="DM")
    assert any("declared that it needs none" in b for b in
               (await svc.blockers_for_styles([draft_style.id]))[draft_style.id])

    await svc.confirm(draft_style.id, no_accessories=True, actor_name="DM")
    assert (await svc.blockers_for_styles([draft_style.id]))[draft_style.id] == []


@pytest.mark.asyncio
async def test_declaring_no_accessories_while_accessory_lines_exist_is_a_422(
        db, draft_style):
    """The two statements contradict each other, and guessing which one the DM
    meant is how a kit gets skipped for an entire order."""
    from fastapi import HTTPException
    _add(db, draft_style, category="ACCESSORY", subtype="ZIP",
         article="ZIP-YKK", qty_per_piece=1)
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).confirm(draft_style.id, no_accessories=True,
                                           actor_name="DM")
    assert exc.value.status_code == 422
    assert "ZIP-YKK" in str(exc.value.detail)
