"""
================================================================================
tests/integration/test_breakdown_style_edit.py
    Editing the STYLE behind a DRAFT breakdown line, not just its SKUs.
================================================================================

WHAT THIS COVERS

    The breakdown table shows a style and its SKUs as one editable row group, but
    only the SKU half was writable: quantity, colour, size. Everything a sheet
    actually gets wrong about the STYLE — the article number, the thickness, the
    season, the unit price — required re-uploading the whole workbook, which is
    not a correction workflow.

    Now: `PATCH /imports/breakdown/skus/{id}` takes a nested `style: {...}` and
    writes both halves in ONE transaction, and `PATCH .../styles/{id}` edits a
    style on its own.

THE THREE THINGS THAT MUST HOLD

    1. DRAFT ONLY. Once a style is RELEASED its pieces carry printed barcodes,
       and rewriting the breakdown behind a garment on the floor is how a factory
       loses traceability. 409, and the message says what to do instead.
    2. ATOMIC. A style-code collision must roll the SKU edit back with it —
       a half-saved row is worse than a rejected one, because nothing on screen
       says which half survived.
    3. `needs_lining: false` must actually persist. It is the one field whose
       false value is meaningful rather than "absent", and a naive
       `if value is None: continue` PATCH loop drops it.
================================================================================
"""
import pytest
from fastapi import HTTPException

from app.core.enums import ProductionReleaseStatus
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.imports.breakdown import BreakdownService

pytestmark = pytest.mark.asyncio


async def _draft(db, *, tag: str, status: str = ProductionReleaseStatus.DRAFT.value):
    client = Client(name=f"C-{tag}", country="IT")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number=f"ORD-{tag}")
    db.add(order)
    await db.flush()
    # UPPERCASE, like every real style code: they are minted through
    # clients.utlis._slug, which uppercases. The patch path normalises the same
    # way, so a fixture with a lowercase code would be testing a value the system
    # never produces.
    style = Style(client_order_id=order.id, name="CLERMONT", article="CL1",
                  code=f"STY-{tag}".upper(), production_status=status)
    db.add(style)
    await db.flush()
    sku = SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
              size="M", qty_ordered=5, code=f"SKU-{tag}")
    db.add(sku)
    await db.commit()
    for o in (order, style, sku):
        await db.refresh(o)
    return order, style, sku


async def test_style_and_sku_move_together_in_one_call(db):
    """The row group saves as a unit — both halves, one request."""
    _, style, sku = await _draft(db, tag="e001")

    out = await BreakdownService(db).update_sku(
        sku.id, {"qty_ordered": 12, "color_name": "OLIVE"},
        style_patch={"article": "CL1-REV2", "thickness": "1.4mm",
                     "season": "2026AW"})

    assert out["qty_ordered"] == 12
    assert out["colour"] == "OLIVE"
    assert out["style"]["changed"]["article"] == "CL1-REV2"

    await db.refresh(style)
    await db.refresh(sku)
    assert style.article == "CL1-REV2"
    assert style.thickness == "1.4mm"
    assert style.season == "2026AW"
    assert sku.qty_ordered == 12


async def test_style_only_patch_edits_the_header(db):
    """The style-only door, for the header that has no SKU to hang off."""
    _, style, _ = await _draft(db, tag="e002")

    out = await BreakdownService(db).update_style(
        style.id, {"name": "CLERMONT II", "unit_price": 74.50,
                   "currency": "EUR", "customer_ref": "CR1-02F5"})

    assert out["style_name"] == "CLERMONT II"
    assert out["unit_price"] == 74.50
    await db.refresh(style)
    assert style.currency == "EUR"
    assert style.customer_ref == "CR1-02F5"


async def test_needs_lining_false_survives_the_patch(db):
    """THE FALSE-IS-NOT-ABSENT TEST.

    `needs_lining: false` is the single most consequential value on this form —
    it removes a garment's lining leg — and it is exactly the value a
    `if value is None: continue` PATCH loop silently discards, leaving the style
    unanswered while the screen shows it as answered.
    """
    _, style, _ = await _draft(db, tag="e003")
    assert style.needs_lining is None, "fixture should start unanswered"

    await BreakdownService(db).update_style(style.id, {"needs_lining": False})
    await db.refresh(style)
    assert style.needs_lining is False, "an explicit 'no lining' was dropped"

    # And it can be put back to unanswered, which is a third distinct state.
    await BreakdownService(db).update_style(style.id, {"needs_lining": None})
    await db.refresh(style)
    assert style.needs_lining is None


async def test_a_released_style_refuses_edits(db):
    """409 once released — the pieces behind it carry printed barcodes."""
    _, style, sku = await _draft(
        db, tag="e004", status=ProductionReleaseStatus.RELEASED.value)

    with pytest.raises(HTTPException) as exc:
        await BreakdownService(db).update_style(style.id, {"article": "NOPE"})
    assert exc.value.status_code == 409

    # …through the SKU door as well, or the rule has a hole in it.
    with pytest.raises(HTTPException) as exc2:
        await BreakdownService(db).update_sku(
            sku.id, {}, style_patch={"article": "NOPE"})
    assert exc2.value.status_code == 409

    await db.refresh(style)
    assert style.article == "CL1", "a released style was edited"


async def test_a_duplicate_style_code_is_a_409_not_a_500(db):
    """Style codes are unique — a barcode caption and a wage rate resolve
    through them — so a collision is an ordinary conflict, not a crash."""
    await _draft(db, tag="e005")
    _, style_b, _ = await _draft(db, tag="e006")

    with pytest.raises(HTTPException) as exc:
        await BreakdownService(db).update_style(style_b.id, {"code": "STY-E005"})
    assert exc.value.status_code == 409
    assert "already used" in str(exc.value.detail)


async def test_a_collision_rolls_the_sku_edit_back_with_it(db):
    """ATOMICITY. Both halves commit or neither does.

    A 200 on the quantity and a 409 on the style would leave the operator's
    screen half-saved with nothing to say which half took.
    """
    await _draft(db, tag="e007")
    _, style, sku = await _draft(db, tag="e008")
    sku_id, original_qty = sku.id, sku.qty_ordered

    with pytest.raises(HTTPException):
        await BreakdownService(db).update_sku(
            sku_id, {"qty_ordered": 99}, style_patch={"code": "STY-E007"})

    # THE REQUEST BOUNDARY. `get_db` closes the session when a handler raises,
    # which discards everything uncommitted — so the SKU write, which was applied
    # to the ORM object before the style check rejected it, must never reach the
    # database. Rolling back here is what that close does, and reading afterwards
    # is the only way to prove the row itself is untouched rather than merely
    # looking untouched through a dirty identity map.
    await db.rollback()
    fresh = await db.get(SKU, sku_id)
    assert fresh.qty_ordered == original_qty, (
        "the SKU edit survived a rejected style edit — the two are not atomic")


async def test_an_empty_patch_is_rejected(db):
    """Nothing to update is a 422, not a silent 200 the caller reads as saved."""
    _, _, sku = await _draft(db, tag="e009")
    with pytest.raises(HTTPException) as exc:
        await BreakdownService(db).update_sku(sku.id, {}, style_patch={})
    assert exc.value.status_code == 422
