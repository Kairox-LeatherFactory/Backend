"""
INTEGRATION · authoring the per-piece recipe.

THE TWO THINGS THAT ARE EASY TO GET WRONG HERE
    1. THE NULL HOLE. Four of the eight identity columns are nullable, and NULLs
       compare DISTINCT inside a unique index on both Postgres and SQLite. So the
       unique constraint alone does NOT stop two lines for the same colourless
       material; find_duplicate_line does, and that is what these tests pin.
    2. THE FREEZE. Once a style is RELEASED its garments are being issued against
       the recipe, so editing a line underneath them would make the ledger
       disagree with what was physically handed over.
"""
import pytest
from sqlalchemy import func, select

from app.modules.barcode.models import MaterialLot, StyleMaterialSpec
from app.modules.clients.models import SKU, Style
from app.modules.materials.style_spec_service import StyleSpecService

pytestmark = pytest.mark.integrity


@pytest.fixture
async def draft_style(db, order_tree):
    style = Style(client_order_id=order_tree["order"].id, name="ASTON",
                  article="AS1", code="JP-ASTON2", production_status="DRAFT")
    db.add(style)
    await db.flush()
    db.add(SKU(style_id=style.id, color_code="BLK", color_name="BLACK",
               size="M", qty_ordered=10, code="JP-ASTON2-BLK-M"))
    db.add(SKU(style_id=style.id, color_code="TAN", color_name="TAN",
               size="M", qty_ordered=6, code="JP-ASTON2-TAN-M"))
    await db.commit()
    await db.refresh(style)
    return style


LEATHER = {"category": "LEATHER", "article": "SUEDE-A32", "colour": "PINE GREEN",
           "thickness": "1.2mm", "qty_per_piece": 12.5}
BUTTON = {"category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-4H",
          "colour": "BLACK", "size": "18L", "qty_per_piece": 4}


# ══════════════════════════════════════════════════════ saving the grid
@pytest.mark.asyncio
async def test_saving_the_same_grid_twice_changes_nothing(db, draft_style):
    """The grid save is a DIFF, not delete-then-insert. Recreating a line would
    give the same recipe entry a new id and orphan every ledger row pointing at
    the old one — the exact history the ledger exists to keep."""
    svc = StyleSpecService(db)
    first = await svc.replace_spec(draft_style.id, [LEATHER, BUTTON],
                                   actor_name="DM")
    ids = sorted(l["line_id"] for l in first["lines"])

    second = await svc.replace_spec(draft_style.id, [LEATHER, BUTTON],
                                    actor_name="DM")

    assert sorted(l["line_id"] for l in second["lines"]) == ids
    assert int(await db.scalar(
        select(func.count()).select_from(StyleMaterialSpec))) == 2


@pytest.mark.asyncio
async def test_a_line_dropped_from_the_grid_deactivates_rather_than_vanishing(
        db, draft_style):
    svc = StyleSpecService(db)
    await svc.replace_spec(draft_style.id, [LEATHER, BUTTON], actor_name="DM")
    await svc.replace_spec(draft_style.id, [LEATHER], actor_name="DM")

    rows = (await db.execute(select(StyleMaterialSpec))).scalars().all()
    assert len(rows) == 2                                   # nothing deleted
    assert [r.is_active for r in sorted(rows, key=lambda r: r.category)] == [False, True]


@pytest.mark.asyncio
async def test_two_lines_for_the_same_colourless_material_are_refused(
        db, draft_style):
    """THE NULL HOLE. Both lines leave colour and size blank, so the unique
    constraint does not see them as equal — NULLs compare distinct in a unique
    index. find_duplicate_line is what actually catches it."""
    from fastapi import HTTPException
    svc = StyleSpecService(db)
    line = {"category": "ACCESSORY", "subtype": "THREAD", "article": "THR-40",
            "qty_per_piece": 120}
    await svc.add_line(draft_style.id, dict(line), actor_name="DM")

    with pytest.raises(HTTPException) as exc:
        await svc.add_line(draft_style.id, dict(line), actor_name="DM")
    assert exc.value.status_code == 409
    assert "already has a line for THR-40" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_the_same_material_twice_in_one_grid_save_is_refused(db, draft_style):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).replace_spec(
            draft_style.id, [BUTTON, dict(BUTTON)], actor_name="DM")
    assert exc.value.status_code == 409
    assert "appears twice" in str(exc.value.detail)


# ══════════════════════════════════════════════════════════ validation
@pytest.mark.asyncio
async def test_an_unknown_material_kind_is_refused_like_the_lot_form_refuses_it(
        db, draft_style):
    """The recipe and the lot form are matched on the same six columns, so a line
    the lot form would have rejected could never resolve to anything. Both ask
    resolve_spec()."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).add_line(
            draft_style.id,
            {"category": "ACCESSORY", "subtype": "RIVET", "article": "RV-1",
             "qty_per_piece": 8}, actor_name="DM")
    assert exc.value.status_code == 422
    assert "BUTTON" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_leather_line_needs_thickness_but_not_article_or_colour(
        db, draft_style):
    from fastapi import HTTPException
    line = await StyleSpecService(db).add_line(
        draft_style.id,
        {"category": "LEATHER", "thickness": "1.2", "qty_per_piece": 1.25},
        actor_name="DM")
    assert line["article"] is None
    assert line["colour"] is None

    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).add_line(
            draft_style.id, {"category": "LEATHER", "qty_per_piece": 5},
            actor_name="DM")
    assert exc.value.status_code == 422
    assert "thickness" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_lining_line_accepts_only_its_kind_thickness_and_quantity(
        db, draft_style):
    line = await StyleSpecService(db).add_line(
        draft_style.id,
        {"category": "LINING", "subtype": "KNIT", "thickness": "1.2",
         "qty_per_piece": 1.5}, actor_name="DM")
    assert line["article"] is None
    assert line["colour"] is None
    assert line["subtype"] == "KNIT"


@pytest.mark.asyncio
async def test_an_accessory_line_still_requires_article(db, draft_style):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).add_line(
            draft_style.id,
            {"category": "ACCESSORY", "subtype": "THREAD",
             "qty_per_piece": 5}, actor_name="DM")
    assert exc.value.status_code == 422
    assert "accessory" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_an_override_may_not_name_another_styles_sku(db, draft_style,
                                                           order_tree):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await StyleSpecService(db).add_line(
            draft_style.id, dict(BUTTON, sku_id=order_tree["sku"].id),
            actor_name="DM")
    assert exc.value.status_code == 422
    assert "does not belong to" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_zero_is_legal_on_an_override_and_illegal_style_wide(db, draft_style):
    """Zero on a SKU line is how a colourway says "this one does not take that".
    Zero on a style-wide line would be a recipe entry that consumes nothing."""
    from fastapi import HTTPException
    svc = StyleSpecService(db)
    sku = (await db.execute(select(SKU).where(
        SKU.style_id == draft_style.id, SKU.color_code == "TAN"))).scalar_one()

    ok = await svc.add_line(draft_style.id, dict(BUTTON, sku_id=sku.id,
                                                 qty_per_piece=0),
                            actor_name="DM")
    assert ok["qty_per_piece"] == 0

    with pytest.raises(HTTPException) as exc:
        await svc.add_line(draft_style.id, dict(BUTTON, qty_per_piece=0),
                           actor_name="DM")
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_the_uom_is_derived_not_taken_from_the_caller(db, draft_style):
    """The unit is a property of the material kind — buttons are pcs whatever a
    client posts. Accepting a caller's uom would let one style measure thread in
    metres and another in yards while both said "mtrs" to the ledger."""
    line = await StyleSpecService(db).add_line(
        draft_style.id, dict(BUTTON, uom="kilograms"), actor_name="DM")
    assert line["uom"] == "pcs"


# ══════════════════════════════════════════════════════════ the freeze
@pytest.mark.asyncio
async def test_a_released_style_refuses_every_write_and_says_why(db, order_tree):
    """order_tree's style is RELEASED. Its garments are already being issued
    against the recipe, so the recipe is frozen — and the message has to point at
    the escape hatch, or a typo'd article becomes unfixable for a whole order."""
    from fastapi import HTTPException
    svc = StyleSpecService(db)
    style_id = order_tree["style"].id

    for call in (svc.replace_spec(style_id, [LEATHER], actor_name="DM"),
                 svc.add_line(style_id, dict(BUTTON), actor_name="DM"),
                 svc.confirm(style_id, no_accessories=True, actor_name="DM")):
        with pytest.raises(HTTPException) as exc:
            await call
        assert exc.value.status_code == 409
        assert "/materials/issues" in str(exc.value.detail)


# ══════════════════════════════════════════ the style / SKU override merge
@pytest.mark.asyncio
async def test_a_sku_override_replaces_only_its_own_article(db, draft_style):
    """The real case: the TAN colourway takes TAN buttons of the same article.
    The override replaces that ONE line and leaves the rest of the recipe alone —
    set-level replacement would mean re-entering the whole recipe per colour."""
    svc = StyleSpecService(db)
    tan = (await db.execute(select(SKU).where(
        SKU.style_id == draft_style.id, SKU.color_code == "TAN"))).scalar_one()
    await svc.replace_spec(draft_style.id, [
        LEATHER,
        BUTTON,
        dict(BUTTON, sku_id=tan.id, colour="TAN"),
    ], actor_name="DM")

    black = await svc.effective_lines(draft_style.id, None)
    tan_lines = await svc.effective_lines(draft_style.id, tan.id)

    assert {l.article for l in black} == {"SUEDE-A32", "BTN-4H"}
    assert {l.article for l in tan_lines} == {"SUEDE-A32", "BTN-4H"}
    assert [l.colour for l in black if l.article == "BTN-4H"] == ["BLACK"]
    assert [l.colour for l in tan_lines if l.article == "BTN-4H"] == ["TAN"]


@pytest.mark.asyncio
async def test_a_zero_override_removes_that_material_for_one_colourway(
        db, draft_style):
    svc = StyleSpecService(db)
    tan = (await db.execute(select(SKU).where(
        SKU.style_id == draft_style.id, SKU.color_code == "TAN"))).scalar_one()
    await svc.replace_spec(draft_style.id, [
        LEATHER, BUTTON, dict(BUTTON, sku_id=tan.id, qty_per_piece=0),
    ], actor_name="DM")

    assert {l.article for l in await svc.effective_lines(draft_style.id, None)} \
        == {"SUEDE-A32", "BTN-4H"}
    assert {l.article for l in await svc.effective_lines(draft_style.id, tan.id)} \
        == {"SUEDE-A32"}


# ══════════════════════════════════════════════ the requirement projection
@pytest.mark.asyncio
async def test_requirement_multiplies_per_piece_by_the_pieces_that_need_it(
        db, draft_style):
    """The screen that should stop an order — read BEFORE release, while a
    shortfall is still a purchase order rather than a stoppage.

    An override's SKU is subtracted from the style-wide line, so the same garment
    is never counted against two lines for the same material."""
    db.add(MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                       colour="BLACK", size="18L", uom="pcs", on_hand=30,
                       is_active=True))
    svc = StyleSpecService(db)
    tan = (await db.execute(select(SKU).where(
        SKU.style_id == draft_style.id, SKU.color_code == "TAN"))).scalar_one()
    await svc.replace_spec(draft_style.id, [
        LEATHER, BUTTON, dict(BUTTON, sku_id=tan.id, colour="TAN"),
    ], actor_name="DM")

    req = await svc.requirement(draft_style.id)

    assert req["qty_ordered"] == 16                    # 10 BLACK + 6 TAN
    by = {(l["article"], l["scope"]): l for l in req["lines"]}
    # The style-wide button line serves only the 10 pieces TAN does not override.
    assert by[("BTN-4H", "STYLE")]["pieces"] == 10
    assert by[("BTN-4H", "STYLE")]["total_required"] == pytest.approx(40)
    assert by[("BTN-4H", "SKU")]["pieces"] == 6
    # 30 on hand against 40 required → short by 10, and the screen says so.
    assert by[("BTN-4H", "STYLE")]["short_by"] == pytest.approx(10)
    assert req["short_lines"] >= 1
    assert "short" in req["message"]
