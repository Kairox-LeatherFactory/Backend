"""
INTEGRATION · the accessory PACKET scan, and the wrong size it exists to catch.

THE MISTAKE THIS FILE IS ABOUT. An M-size button in an L-size jacket. It is
invisible on the factory floor, it is found by the client in Dubai, and it is paid
for in return freight plus a remade garment. Before the packet scan the system
could not detect it even in principle: one tap on the garment issued every
accessory line the recipe named, straight from the recipe, so no physical packet
was ever part of the exchange.

THE THREE PROPERTIES THAT MATTER, in this order:

  1. THE WRONG SIZE IS REFUSED, AND NOTHING MOVES. Not flagged, not recorded with
     a warning — refused. Every other mid-scan problem in this app (the skill
     gate, the stock shortfall) records the work and warns, because the work
     physically happened and losing the record is worse. This is the exception:
     there is nothing worth preserving about the wrong size going in.
  2. THE REFUSAL LEAVES A QUESTION SOMEBODY CAN ANSWER. An operator told "wait for
     approval" with no row anywhere is an operator waiting on nobody.
  3. AN APPROVAL IS PERMISSION, NOT THE ISSUE. The DM grants it; the operator
     re-scans, and that is what moves the stock against the worker's own card.
"""
import uuid

import pytest
from sqlalchemy import func, select

from app.core.enums import KitStatus, KitSubstitutionStatus, StorePart
from app.modules.barcode.models import (KitSubstitutionRequest, MaterialLot,
                                        PieceMaterialIssue, StyleMaterialSpec)
from app.modules.materials.style_spec_service import StyleSpecService
from app.modules.store.service import StoreService

pytestmark = pytest.mark.integrity


async def _on_hand(db, lot_id) -> float:
    return float(await db.scalar(
        select(MaterialLot.on_hand).where(MaterialLot.id == lot_id)))


async def _issue_rows(db, piece_id) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(PieceMaterialIssue)
        .where(PieceMaterialIssue.piece_id == piece_id)))


async def _requests(db, piece_id) -> list:
    return list((await db.execute(
        select(KitSubstitutionRequest)
        .where(KitSubstitutionRequest.piece_id == piece_id))).scalars())


# ══════════════════════════════════════════════════════════════════ fixtures
@pytest.fixture
async def sized_buttons(db, order_tree):
    """CLERMONT takes 4 buttons, and the button is SIZED to the garment.

    order_tree's only SKU is an M, so `pieces` are M garments. An L packet and an
    L line exist alongside, which is the whole shape of the mistake: the right
    article, the right colour, the wrong size, and nothing physical to stop it.
    """
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    large = SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
                size="L", qty_ordered=3, code="JP-CLERMONT-PINE-L")
    db.add(large)

    lots = {}
    for size in ("M", "L"):
        lot = MaterialLot(category="ACCESSORY", subtype="BUTTON",
                          article="BTN-4H", colour="BLACK", size=size,
                          uom="pcs", on_hand=500, is_active=True)
        db.add(lot)
        lots[size] = lot
    await db.flush()

    for size in ("M", "L"):
        db.add(StyleMaterialSpec(
            style_id=style.id, category="ACCESSORY", subtype="BUTTON",
            article="BTN-4H", colour="BLACK", size=size, garment_size=size,
            qty_per_piece=4, uom="pcs", material_lot_id=lots[size].id))
    await db.commit()
    for lot in lots.values():
        await db.refresh(lot)
    return lots


# ═══════════════════════════ 1 · THE HEADLINE: refused, and nothing moves
@pytest.mark.asyncio
async def test_the_wrong_size_packet_is_refused_and_spends_nothing(
        db, pieces, sized_buttons, cutter):
    """An M garment, an L packet. The 409 is the feature."""
    from fastapi import HTTPException
    piece = pieces[0]                          # size M
    before = await _on_hand(db, sized_buttons["L"].id)

    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")

    assert exc.value.status_code == 409
    assert "WRONG SIZE" in str(exc.value.detail)
    # It says BOTH sizes, because "wrong size" without them is a puzzle.
    assert "M garment" in str(exc.value.detail)
    assert await _on_hand(db, sized_buttons["L"].id) == pytest.approx(before)
    assert await _issue_rows(db, piece.id) == 0
    await db.refresh(piece)
    assert piece.accessories_in is False


@pytest.mark.asyncio
async def test_the_refusal_leaves_a_request_a_dm_can_answer(
        db, pieces, sized_buttons, cutter):
    """PROPERTY 2, and the reason the row is committed before the 409 is raised.

    The scan must fail, but the ask has to survive the failed request — otherwise
    the operator is told to wait for an approval that exists nowhere.
    """
    from fastapi import HTTPException
    piece = pieces[0]
    with pytest.raises(HTTPException):
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE",
            substitution_reason="the M packet is empty")

    rows = await _requests(db, piece.id)
    assert len(rows) == 1
    assert rows[0].status == KitSubstitutionStatus.PENDING.value
    assert (rows[0].garment_size, rows[0].lot_size) == ("M", "L")
    assert rows[0].reason == "the M packet is empty"
    assert rows[0].decided_at is None


@pytest.mark.asyncio
async def test_a_rescan_finds_the_SAME_request_and_does_not_queue_another(
        db, pieces, sized_buttons, cutter):
    """The protocol IS "scan, get refused, wait, scan again", so a re-scan must
    not pile up identical asks for the DM to wade through."""
    from fastapi import HTTPException
    piece = pieces[0]
    for _ in range(3):
        with pytest.raises(HTTPException):
            await StoreService(db).store_scan(
                piece_id=piece.id, lot_id=sized_buttons["L"].id,
                employee_id=cutter[0].id, entered_by="STORE")
    assert len(await _requests(db, piece.id)) == 1


# ══════════════════════════ 2 · the decision, and what it does and does not do
@pytest.mark.asyncio
async def test_approving_does_not_by_itself_issue_anything(
        db, pieces, sized_buttons, cutter, dm):
    """PROPERTY 3. The operator is holding the packet; the DM is not.

    An approval that spent stock by itself would record a manager as having issued
    a packet they never touched, and the ledger's whole job is to say whose hands
    the material passed through.
    """
    from fastapi import HTTPException
    piece = pieces[0]
    with pytest.raises(HTTPException):
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")
    request = (await _requests(db, piece.id))[0]
    before = await _on_hand(db, sized_buttons["L"].id)

    out = await StoreService(db).decide_substitution(
        request.id, approve=True, note="one-off, client agreed",
        actor_user_id=dm.id, actor_name="DM")

    assert out["status"] == KitSubstitutionStatus.APPROVED.value
    assert "not issued until" in out["message"]
    assert await _on_hand(db, sized_buttons["L"].id) == pytest.approx(before)
    assert await _issue_rows(db, piece.id) == 0


@pytest.mark.asyncio
async def test_an_approved_substitution_issues_on_the_rescan_and_is_consumed(
        db, pieces, sized_buttons, cutter, dm):
    """The full loop: refused, approved, re-scanned, spent — once."""
    from fastapi import HTTPException
    piece = pieces[0]
    with pytest.raises(HTTPException):
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")
    request = (await _requests(db, piece.id))[0]
    await StoreService(db).decide_substitution(
        request.id, approve=True, actor_user_id=dm.id, actor_name="DM")
    before = await _on_hand(db, sized_buttons["L"].id)

    res = await StoreService(db).store_scan(
        piece_id=piece.id, lot_id=sized_buttons["L"].id,
        employee_id=cutter[0].id, entered_by="STORE")

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, sized_buttons["L"].id) == pytest.approx(before - 4)
    # THE RESPONSE SAYS SO OUT LOUD. A substitution that reads like a clean issue
    # is a substitution nobody reviews.
    assert res["substitution"] is not None
    assert res["substitution"]["lot_size"] == "L"
    assert "DM" in res["substitution"]["message"]
    await db.refresh(request)
    assert request.status == KitSubstitutionStatus.CONSUMED.value
    await db.refresh(piece)
    assert piece.accessories_in is True


@pytest.mark.asyncio
async def test_one_approval_cannot_license_a_second_garment(
        db, pieces, sized_buttons, cutter, dm):
    """CONSUMED is terminal. One decision, one garment — the same reason a closed
    wage run is a frozen snapshot rather than a standing rule."""
    from fastapi import HTTPException
    first, second = pieces[0], pieces[1]
    with pytest.raises(HTTPException):
        await StoreService(db).store_scan(
            piece_id=first.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")
    request = (await _requests(db, first.id))[0]
    await StoreService(db).decide_substitution(
        request.id, approve=True, actor_user_id=dm.id, actor_name="DM")
    await StoreService(db).store_scan(
        piece_id=first.id, lot_id=sized_buttons["L"].id,
        employee_id=cutter[0].id, entered_by="STORE")
    before = await _on_hand(db, sized_buttons["L"].id)

    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=second.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")

    assert exc.value.status_code == 409
    assert await _on_hand(db, sized_buttons["L"].id) == pytest.approx(before)
    assert await _issue_rows(db, second.id) == 0


@pytest.mark.asyncio
async def test_a_refused_substitution_stays_refused_and_says_who_refused_it(
        db, pieces, sized_buttons, cutter, dm):
    from fastapi import HTTPException
    piece = pieces[0]
    with pytest.raises(HTTPException):
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")
    request = (await _requests(db, piece.id))[0]
    await StoreService(db).decide_substitution(
        request.id, approve=False, note="ship the right size",
        actor_user_id=dm.id, actor_name="DM")
    before = await _on_hand(db, sized_buttons["L"].id)

    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")

    assert exc.value.status_code == 409
    assert "REFUSED" in str(exc.value.detail)
    assert "ship the right size" in str(exc.value.detail)
    assert await _on_hand(db, sized_buttons["L"].id) == pytest.approx(before)


@pytest.mark.asyncio
async def test_a_refusal_may_be_approved_later(
        db, pieces, sized_buttons, cutter, dm):
    """"No" on Tuesday and "yes" on Wednesday is a real sequence, and forcing a
    second row for it would lose the first decision."""
    from fastapi import HTTPException
    piece = pieces[0]
    with pytest.raises(HTTPException):
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")
    request = (await _requests(db, piece.id))[0]
    await StoreService(db).decide_substitution(
        request.id, approve=False, actor_user_id=dm.id, actor_name="DM")
    await StoreService(db).decide_substitution(
        request.id, approve=True, actor_user_id=dm.id, actor_name="MD")

    res = await StoreService(db).store_scan(
        piece_id=piece.id, lot_id=sized_buttons["L"].id,
        employee_id=cutter[0].id, entered_by="STORE")
    assert res["substitution"]["approved_by"] == "MD"
    assert len(await _requests(db, piece.id)) == 1


@pytest.mark.asyncio
async def test_the_queue_is_oldest_first_and_counts_what_is_pending(
        db, pieces, sized_buttons, cutter):
    from fastapi import HTTPException
    for piece in pieces[:2]:
        with pytest.raises(HTTPException):
            await StoreService(db).store_scan(
                piece_id=piece.id, lot_id=sized_buttons["L"].id,
                employee_id=cutter[0].id, entered_by="STORE")

    out = await StoreService(db).list_substitutions(status_filter="PENDING")
    assert out["pending"] == 2
    assert out["count"] == 2
    # BOTH ROWS, NOT A PARTICULAR ORDER. Two asks raised in the same instant tie on
    # created_at, so asserting which one came first would be asserting a coin flip.
    # What the queue owes the DM is that neither garment goes missing from it.
    assert ({r["piece_code"] for r in out["requests"]}
            == {pieces[0].code, pieces[1].code})
    assert "into a M garment" in out["requests"][0]["summary"]


# ══════════════════════════════════ 3 · the right packet, and the wrong packet
@pytest.mark.asyncio
async def test_the_matching_packet_issues_exactly_its_own_line(
        db, pieces, sized_buttons, cutter):
    piece = pieces[0]                          # size M
    m_before = await _on_hand(db, sized_buttons["M"].id)
    l_before = await _on_hand(db, sized_buttons["L"].id)

    res = await StoreService(db).store_scan(
        piece_id=piece.id, lot_id=sized_buttons["M"].id,
        employee_id=cutter[0].id, entered_by="STORE")

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert res["substitution"] is None
    assert await _on_hand(db, sized_buttons["M"].id) == pytest.approx(m_before - 4)
    assert await _on_hand(db, sized_buttons["L"].id) == pytest.approx(l_before), (
        "the L line is not this garment's and must not be touched")
    assert await _issue_rows(db, piece.id) == 1


@pytest.mark.asyncio
async def test_a_packet_this_style_does_not_use_is_refused_by_name(
        db, pieces, sized_buttons, cutter):
    """A genuinely wrong packet, not a wrong size — so it points at the off-spec
    door rather than at a DM approval."""
    from fastapi import HTTPException
    stranger = MaterialLot(category="ACCESSORY", subtype="ZIP",
                           article="ZIP-NOT-OURS", colour="RED", size="60CM",
                           uom="pcs", on_hand=10, is_active=True)
    db.add(stranger)
    await db.commit()
    await db.refresh(stranger)

    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=pieces[0].id, lot_id=stranger.id,
            employee_id=cutter[0].id, entered_by="STORE")

    assert exc.value.status_code == 422
    assert "does not name" in str(exc.value.detail)
    assert "ZIP-NOT-OURS" in str(exc.value.detail)
    assert "/materials/issues" in str(exc.value.detail)
    assert await _on_hand(db, stranger.id) == pytest.approx(10)


@pytest.mark.asyncio
async def test_a_leather_lot_is_not_an_accessory_packet(
        db, pieces, sized_buttons, cutter):
    """Leather is consumed at the cut, against the production event. Issuing it
    here would be a second, competing ledger for the same material."""
    from fastapi import HTTPException
    hide = MaterialLot(category="LEATHER", article="COW-1", colour="BLACK",
                       thickness="1.2mm", size=None, uom="dcm", on_hand=300,
                       is_active=True)
    db.add(hide)
    await db.commit()
    await db.refresh(hide)

    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=pieces[0].id, lot_id=hide.id,
            employee_id=cutter[0].id, entered_by="STORE")
    assert exc.value.status_code == 422
    assert "not an accessory" in str(exc.value.detail)


@pytest.mark.asyncio
async def test_a_size_with_no_line_at_all_asks_for_the_RECIPE_to_be_fixed(
        db, pieces, order_tree, cutter):
    """THE RELEASE-GATE HOLE, SHOWING UP ON THE FLOOR.

    The style's only button line is for L garments and this is an M. That is not a
    substitution to approve — approving one packet would leave every other M in
    the order in the same state — so it says the recipe is what needs changing.
    """
    from fastapi import HTTPException
    style = order_tree["style"]
    lot = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                      colour="BLACK", size="L", uom="pcs", on_hand=100,
                      is_active=True)
    db.add(lot)
    await db.flush()
    db.add(StyleMaterialSpec(
        style_id=style.id, category="ACCESSORY", subtype="BUTTON",
        article="BTN-4H", colour="BLACK", size="L", garment_size="L",
        qty_per_piece=4, uom="pcs", material_lot_id=lot.id))
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=pieces[0].id, lot_id=lot.id,
            employee_id=cutter[0].id, entered_by="STORE")

    assert exc.value.status_code == 409
    assert "no line for this size" in str(exc.value.detail)
    assert "recipe needs a line" in str(exc.value.detail)
    assert await _requests(db, pieces[0].id) == []


# ══════════════════════════════════════ 4 · the blanket scan is really gone
@pytest.mark.asyncio
async def test_part_ACCESSORY_without_a_packet_is_refused(
        db, pieces, sized_buttons, cutter):
    """THE WHOLE REASON THE FEATURE WORKS. A blanket kit scan spent every line the
    recipe named from one tap, so no physical packet was ever compared to
    anything. Leaving it available would leave it as the path of least resistance.
    """
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=pieces[0].id, part=StorePart.ACCESSORY,
            employee_id=cutter[0].id, entered_by="STORE")
    assert exc.value.status_code == 422
    assert "packet" in str(exc.value.detail)
    assert await _issue_rows(db, pieces[0].id) == 0


@pytest.mark.asyncio
async def test_a_packet_scan_is_never_read_as_a_cut_part(
        db, pieces, sized_buttons, cutter):
    """A lot label can only mean the kit, so a `part` that says otherwise is a
    contradiction rather than something to silently prefer."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=pieces[0].id, part=StorePart.LEATHER,
            lot_id=sized_buttons["M"].id,
            employee_id=cutter[0].id, entered_by="STORE")
    assert exc.value.status_code == 422
    assert "not issued from a lot label" in str(exc.value.detail)


# ══════════════════════════════ 5 · the release gate, on real ordered sizes
@pytest.mark.asyncio
async def test_an_accessory_missing_from_some_skus_blocks_release(
        db, order_tree):
    """A SKU WITH NOTHING, caught at the last moment anyone can be asked.

    Only the M colourway has a line, so PINE GREEN · L gets kit_required=False,
    completeness collapses to leather-and-lining, and it ships with no accessories
    at all. Nothing else in the system would ever have said so.

    THE GATE ASKS "IS THIS SKU EMPTY", NOT "DOES IT MATCH THE OTHERS". Its
    predecessor `accessory_sku_gaps` demanded every SKU carry every article any SKU
    declared, which blocked order 1996 for legitimately giving two colourways
    different buttons — see test_disjoint_accessories_per_sku_release_clean below.
    """
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    large = SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
                size="L", qty_ordered=2, code="JP-CLERMONT-PINE-L")
    db.add(large)
    await db.flush()
    # The zip is on the M SKU only.
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=order_tree["sku"].id, category="ACCESSORY",
        subtype="ZIP", article="ZIP-N", colour="BLACK", size="60",
        qty_per_piece=1, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    coverage = [b for b in blockers if "none at all on" in b]
    assert len(coverage) == 1
    assert "PINE GREEN · L" in coverage[0]
    # It must say the missing lines need NOT match the other colourways, or the DM
    # clears it by copying and re-creates the restriction this change removed.
    assert "need not match" in coverage[0]
    assert "no_accessories: true" in coverage[0]


async def test_an_accessory_on_every_ordered_sku_does_not_block(db, order_tree):
    """A gate that fires on the normal case is a gate people learn to ignore."""
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    large = SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
                size="L", qty_ordered=2, code="JP-CLERMONT-PINE-L")
    db.add(large)
    await db.flush()
    for sku_id in (order_tree["sku"].id, large.id):
        db.add(StyleMaterialSpec(
            style_id=style.id, sku_id=sku_id, category="ACCESSORY",
            subtype="BUTTON", article="BTN-18L", colour="BLACK", size="18L",
            qty_per_piece=4, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    assert [b for b in blockers if "none at all on" in b] == []


async def test_disjoint_accessories_per_sku_release_clean(db, order_tree):
    """THE RULE THIS WHOLE CHANGE EXISTS FOR (Hamthan, 2026-09-29).

    Each SKU takes whatever it takes. The M colourway gets a horn button, the L one
    gets a metal shank and a zip, and they share NOTHING — different subtype,
    different article, different colour, different size. That is the normal case on
    this floor, and it must release without a word.

    Order 1996 was rejected for exactly this shape, with one blocker sentence per
    article. Every SKU has SOMETHING, so there is nothing to report.
    """
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    large = SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
                size="L", qty_ordered=2, code="JP-CLERMONT-PINE-L")
    db.add(large)
    await db.flush()
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=order_tree["sku"].id, category="ACCESSORY",
        subtype="BUTTON", article="HORN BROWN", colour="BROWN", size="18L",
        qty_per_piece=4, uom="pcs"))
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=large.id, category="ACCESSORY",
        subtype="BUTTON", article="METAL SHANK", colour="GUNMETAL", size="24L",
        qty_per_piece=6, uom="pcs"))
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=large.id, category="ACCESSORY",
        subtype="ZIP", article="YKK-60", colour="BLACK", size="60",
        qty_per_piece=1, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    # NO accessory blocker of any kind — not the coverage one, not the style one.
    assert [b for b in blockers if "accessor" in b.lower()] == [], blockers


async def test_a_sku_nobody_ordered_is_not_a_gap(db, order_tree):
    """An importer can leave a zero-quantity row for a colour/size the client did
    not buy. Blocking a release over a garment that will never be made is a false
    blocker, and those teach people the gate is noise."""
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    db.add(SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
               size="XXL", qty_ordered=0, code="JP-CLERMONT-PINE-XXL"))
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=order_tree["sku"].id, category="ACCESSORY",
        subtype="BUTTON", article="BTN-18L", colour="BLACK", size="18L",
        qty_per_piece=4, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    assert [b for b in blockers if "but not on" in b] == []


async def test_a_leather_line_is_not_judged_by_accessory_coverage(db, order_tree):
    """Leather and lining can still be style-wide, so "every SKU must have one" is
    not a question that can be asked of them."""
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    db.add(SKU(style_id=style.id, color_code="NAVY", color_name="NAVY",
               size="S", qty_ordered=2, code="JP-CLERMONT-NAVY-S"))
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=order_tree["sku"].id, category="LEATHER",
        article="SUEDE-A32", colour="PINE", thickness="1.2mm",
        qty_per_piece=12.5, uom="dcm"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    assert [b for b in blockers if "but not on" in b] == []


# ══════════════════════ 7 · A GARMENT WITH SEVERAL ACCESSORIES MUST FINISH
@pytest.fixture
async def three_trims(db, order_tree):
    """CLERMONT takes a button, a zip and thread — all on its one SKU.

    THE SHAPE THE FLOOR REPORTED IT IN. A garment needs everything the DM put on the
    release breakdown, and none of it is size-specific here: the point is the COUNT,
    not the sizing. Three packets, three scans, and the garment must then be
    sendable — which is what unblocks LINE_STITCHING.
    """
    style = order_tree["style"]
    sku_id = order_tree["sku"].id
    trims = [("BUTTON", "BTN-4H", "18L", 4), ("ZIP", "ZIP-N", "60", 1),
             ("THREAD", "THR-40", None, 120)]
    lots = {}
    for subtype, article, size, _qty in trims:
        lot = MaterialLot(category="ACCESSORY", subtype=subtype, article=article,
                          colour="BLACK", size=size, uom="pcs", on_hand=500,
                          is_active=True)
        db.add(lot)
        lots[article] = lot
    await db.flush()
    for subtype, article, size, qty in trims:
        db.add(StyleMaterialSpec(
            style_id=style.id, sku_id=sku_id, category="ACCESSORY",
            subtype=subtype, article=article, colour="BLACK", size=size,
            garment_size=None, qty_per_piece=qty, uom="pcs",
            material_lot_id=lots[article].id))
    await db.commit()
    for lot in lots.values():
        await db.refresh(lot)
    return lots


@pytest.mark.asyncio
async def test_THREE_accessories_complete_the_garment_in_three_scans(
        db, pieces, three_trims, cutter, dm, operations):
    """THE BUG AS REPORTED: "I can't scan multiple accessories."

    Each scan DID register — stock moved and the ledger row was written — but
    `accessories_in` was computed from a SELECT that could not see the row just
    added, because sessions are `autoflush=False` and nothing had flushed. So it was
    persisted False after every scan, the garment never became sendable, and
    LINE_STITCHING (gated on SENDED) was unreachable however many times the operator
    scanned.

    `no_autoflush` IS THE WHOLE POINT OF THIS TEST. The harness used to default to
    autoflush=True, which flushed the pending row before the read and hid the bug
    behind a green suite. This block reproduces production exactly, so the test
    keeps failing if anyone ever makes the harness permissive again.
    """
    from tests.conftest import _ready_for_store
    piece = pieces[0]
    svc = StoreService(db)

    # The cut parts first, because completeness is leather AND lining AND the kit —
    # this test is about the kit half, so the other two have to be genuinely in.
    await _ready_for_store(db, operations, piece, cutter[0].id)
    await svc.store_scan(piece_id=piece.id, part=StorePart.LEATHER,
                         employee_id=cutter[0].id)
    await svc.store_scan(piece_id=piece.id, part=StorePart.LINING,
                         employee_id=cutter[0].id)

    with db.no_autoflush:
        for article, lot in three_trims.items():
            res = await svc.store_scan(
                piece_id=piece.id, lot_id=lot.id,
                employee_id=cutter[0].id, entered_by="STORE")

    # The LAST scan completed the set, so it must say so on the spot.
    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert res["kit"]["outstanding"] == []
    assert res["accessories_in"] is True
    await db.refresh(piece)
    assert piece.accessories_in is True

    # …and the garment can now leave the store, which is what unblocks
    # LINE_STITCHING — the thing the floor could not reach.
    out = await StoreService(db).send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert out["sent"] == [piece.code], out


@pytest.mark.asyncio
async def test_each_scan_reports_what_is_still_owed(db, pieces, three_trims,
                                                   cutter):
    """The operator is standing at the terminal and needs to know what is left —
    "2 of 3, the thread is still owed" — not a bare boolean."""
    piece = pieces[0]
    svc = StoreService(db)
    with db.no_autoflush:
        first = await svc.store_scan(
            piece_id=piece.id, lot_id=three_trims["BTN-4H"].id,
            employee_id=cutter[0].id, entered_by="STORE")

    assert first["kit"]["status"] == KitStatus.PARTIAL.value
    owed = {r["article"] for r in first["kit"]["outstanding"]}
    assert owed == {"ZIP-N", "THR-40"}, owed
    assert first["accessories_in"] is False
    assert "ACCESSORIES" in first["awaiting"]


@pytest.mark.asyncio
async def test_a_batch_issues_every_packet_in_one_call(db, pieces, three_trims,
                                                      cutter):
    """THE FLOOR'S OWN SEQUENCE. Three packets collected on the screen, submitted
    together. The stored shape is still one issue per line — the convenience is in
    the request, not the data."""
    piece = pieces[0]
    befores = {a: await _on_hand(db, lot.id) for a, lot in three_trims.items()}

    with db.no_autoflush:
        res = await StoreService(db).store_scan(
            piece_id=piece.id,
            lot_ids=[lot.id for lot in three_trims.values()],
            employee_id=cutter[0].id, entered_by="STORE")

    batch = res["accessory_batch"]
    assert (batch["scanned"], batch["issued"]) == (3, 3)
    assert batch["refused"] == [] and batch["still_owed"] == []
    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert res["accessories_in"] is True
    # Each packet spent its own line's quantity, once.
    assert await _on_hand(db, three_trims["BTN-4H"].id) == pytest.approx(
        befores["BTN-4H"] - 4)
    assert await _on_hand(db, three_trims["ZIP-N"].id) == pytest.approx(
        befores["ZIP-N"] - 1)
    assert await _on_hand(db, three_trims["THR-40"].id) == pytest.approx(
        befores["THR-40"] - 120)
    assert await _issue_rows(db, piece.id) == 3


@pytest.mark.asyncio
async def test_the_same_packet_twice_in_one_batch_spends_once(db, pieces,
                                                             three_trims, cutter):
    """THE DOUBLE-SPEND THE PER-PACKET FLUSH PREVENTS.

    `issue_kit_nocommit` opens with `issued_by_piece`, its idempotency read. Without
    a flush after each packet, the second pass over the same lot would miss the
    first's unflushed ledger row, compute the full quantity as still owed, and spend
    it again — four buttons becoming eight inside one request.
    """
    piece = pieces[0]
    button = three_trims["BTN-4H"]
    before = await _on_hand(db, button.id)

    with db.no_autoflush:
        res = await StoreService(db).store_scan(
            piece_id=piece.id, lot_ids=[button.id, button.id],
            employee_id=cutter[0].id, entered_by="STORE")

    assert res["accessory_batch"]["scanned"] == 2
    assert await _on_hand(db, button.id) == pytest.approx(before - 4), \
        "four buttons, not eight"
    assert await _issue_rows(db, piece.id) == 1, "one ledger row per line"


@pytest.fixture
async def wrong_size_button(db, three_trims):
    """A 20L button packet, where the recipe asks for 18L.

    ITS OWN FIXTURE, not `sized_buttons`. Combining the two put TWO BTN-4H lines on
    one garment — `three_trims`' 18L line and `sized_buttons`' M line — so the packet
    matched both and the verdict was AMBIGUOUS rather than WRONG_SIZE. A test whose
    fixtures fight each other tests the fight, not the rule.
    """
    lot = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                      colour="BLACK", size="20L", uom="pcs", on_hand=500,
                      is_active=True)
    db.add(lot)
    await db.commit()
    await db.refresh(lot)
    return lot


@pytest.mark.asyncio
async def test_a_batch_issues_the_good_packets_and_reports_the_wrong_size_one(
        db, pieces, three_trims, wrong_size_button, cutter):
    """PARTIAL ACCEPT — the rule the rest of this codebase follows.

    One bad packet must never lose the good ones a manager scanned with it. The zip
    and the thread go in; the L-size button against an M garment is refused, carries
    its approval request id, and the garment stays unsendable until a DM answers.
    """
    piece = pieces[0]
    wrong = wrong_size_button                  # 20L, where the recipe asks 18L
    zip_lot, thread = three_trims["ZIP-N"], three_trims["THR-40"]
    wrong_before = await _on_hand(db, wrong.id)

    with db.no_autoflush:
        res = await StoreService(db).store_scan(
            piece_id=piece.id, lot_ids=[zip_lot.id, wrong.id, thread.id],
            employee_id=cutter[0].id, entered_by="STORE")

    batch = res["accessory_batch"]
    assert (batch["scanned"], batch["issued"]) == (3, 2)
    assert len(batch["refused"]) == 1
    bad = batch["refused"][0]
    assert bad["reason"] == "WRONG_SIZE"
    assert bad["substitution_request_id"]
    assert "WRONG SIZE" in bad["detail"]

    # The good two are really in, the bad one really is not.
    assert await _on_hand(db, wrong.id) == pytest.approx(wrong_before)
    assert await _issue_rows(db, piece.id) == 2

    # The ask survived the same commit as the issues — that is the whole reason the
    # batch door does not commit inside the refusal.
    rows = await _requests(db, piece.id)
    assert len(rows) == 1
    assert rows[0].status == KitSubstitutionStatus.PENDING.value

    # And the garment is still owed its button, so it cannot leave the store.
    assert res["accessories_in"] is False
    # STILL OWED, BY NAME. The old flat shape said `BTN-4H` and nothing else — the
    # one field on the row an operator cannot read off the packet in their hand.
    # Each row now describes itself, and the sentence is ready to put on screen.
    assert [r["article"] for r in batch["still_owed"]] == ["BTN-4H"]
    assert batch["still_owed"][0]["label"] == "BUTTON · BTN-4H BLACK 18L"
    assert batch["still_owed"][0]["state"] == "PENDING"
    assert "Waiting for BUTTON · BTN-4H BLACK 18L" in batch["still_owed_line"]
    # The flat list is kept so nothing already reading it breaks.
    assert batch["still_owed_articles"] == ["BTN-4H"]


@pytest.mark.asyncio
async def test_naming_the_packets_twice_is_refused(db, pieces, three_trims,
                                                   cutter):
    """Two ways of naming the packets cannot be reconciled, and silently preferring
    one is how the wrong packet gets issued."""
    from pydantic import ValidationError
    from app.modules.store import schemas
    with pytest.raises(ValidationError) as exc:
        schemas.StoreScanRequest(
            employee_id=cutter[0].id, piece_id=pieces[0].id,
            lot_id=three_trims["BTN-4H"].id,
            lot_ids=[three_trims["ZIP-N"].id])
    assert "more than once" in str(exc.value)


# ══════════════════════ 8 · DECLARED / SCANNED / PENDING, in one answer
@pytest.mark.asyncio
async def test_the_scan_says_what_is_declared_scanned_and_pending(
        db, pieces, three_trims, cutter):
    """THE OPERATOR'S WHOLE QUESTION, ANSWERED ON THE SCAN THEY WERE ALREADY DOING.

    Button and thread in, zip still owed. `accessories_in` is a roll-up and can only
    say "not all of them"; it cannot say WHICH — and the person who can fetch the zip
    is standing at the terminal right now. So the response carries the three lists
    and the sentence, all from one `material_requirement_block`.
    """
    piece = pieces[0]
    with db.no_autoflush:
        res = await StoreService(db).store_scan(
            piece_id=piece.id,
            lot_ids=[three_trims["BTN-4H"].id, three_trims["THR-40"].id],
            employee_id=cutter[0].id, entered_by="STORE")

    kit = res["kit"]
    assert {r["article"] for r in kit["declared"]} == {"BTN-4H", "ZIP-N", "THR-40"}
    assert {r["article"] for r in kit["scanned"]} == {"BTN-4H", "THR-40"}
    assert [r["article"] for r in kit["pending"]] == ["ZIP-N"]
    assert kit["progress"]["declared"] == 3
    assert kit["progress"]["scanned"] == 2
    assert kit["progress"]["pending"] == 1

    # EVERY LINE CARRIES ITS OWN STATE, so no screen re-derives the subtraction.
    states = {r["article"]: r["state"] for r in kit["declared"]}
    assert states == {"BTN-4H": "ISSUED", "THR-40": "ISSUED", "ZIP-N": "PENDING"}

    # AND THE SENTENCE. This is the user's own example: "if I miss zip then system
    # says waiting for the zip, button and thread are scanned."
    line = kit["pending_line"]
    assert line.startswith("Waiting for ZIP")
    assert "Scanned:" in line and "BTN-4H" in line and "THR-40" in line

    assert res["accessories_in"] is False
    batch = res["accessory_batch"]
    assert [r["subtype"] for r in batch["still_owed"]] == ["ZIP"]
    assert batch["still_owed_line"] == line


@pytest.mark.asyncio
async def test_the_last_packet_turns_the_sentence_over(db, pieces, three_trims,
                                                      cutter):
    """Nothing pending, and it says so rather than going quiet. A blank field where
    a sentence was is indistinguishable from a screen that failed to load."""
    piece = pieces[0]
    with db.no_autoflush:
        res = await StoreService(db).store_scan(
            piece_id=piece.id, lot_ids=[l.id for l in three_trims.values()],
            employee_id=cutter[0].id, entered_by="STORE")

    kit = res["kit"]
    assert kit["pending"] == [] and len(kit["scanned"]) == 3
    assert kit["pending_line"].startswith("All accessories scanned:")
    assert res["accessories_in"] is True
    assert {r["state"] for r in kit["declared"]} == {"ISSUED"}


@pytest.mark.asyncio
async def test_the_garment_lookup_gives_the_same_answer_as_the_scan(
        db, pieces, three_trims, cutter):
    """ONE ENDPOINT ANYONE CAN ASK, LATER. The scan tells the operator mid-scan;
    this tells a DM at a desk. Both read the same computation, so a garment cannot
    look half-kitted on one screen and fully kitted on the other — which is exactly
    what happened while the store screen did its own subtraction."""
    piece = pieces[0]
    svc = StoreService(db)
    with db.no_autoflush:
        scan = await svc.store_scan(
            piece_id=piece.id, lot_ids=[three_trims["BTN-4H"].id],
            employee_id=cutter[0].id, entered_by="STORE")

    detail = await svc.piece_detail(await svc.get_piece(piece.id))
    assert detail["accessories_progress"] == scan["kit"]["progress"]
    assert detail["pending_line"] == scan["kit"]["pending_line"]
    assert ({r["article"] for r in detail["accessories_pending"]}
            == {r["article"] for r in scan["kit"]["pending"]})
    assert [r["article"] for r in detail["accessories_scanned"]] == ["BTN-4H"]
    # And the full checklist is still there, every line with its own state.
    assert len(detail["accessories"]) == 3


@pytest.mark.asyncio
async def test_a_style_that_declares_no_accessories_reads_empty_not_broken(
        db, pieces):
    """THE EMPTY SHAPE MUST CARRY EVERY KEY THE FULL ONE DOES.

    Most of what is already on the floor was released before the material spec
    existed, so `material_requirement_block` returns its `empty` dict for it — and
    `kit_view` reads these fields straight through. A key missing from `empty` is a
    KeyError on the store scan of those garments, not a cosmetic gap. This caught
    exactly that during the change.

    Asserted through `kit_view` rather than a scan because it is the shape that is
    at issue, and a leather scan would first have to satisfy the stage gate.
    """
    kit = await StyleSpecService(db).kit_view(pieces[0].id)

    assert kit["declared"] == [] and kit["pending"] == [] and kit["scanned"] == []
    assert kit["progress"]["declared"] == 0
    assert kit["progress"]["pending"] == 0
    # NULL, not "waiting for nothing".
    assert kit["pending_line"] is None
    assert kit["status"] == KitStatus.NOT_REQUIRED.value
    # And a null piece_id takes the same path, for a screen with nothing selected.
    assert (await StyleSpecService(db).kit_view(None))["pending_line"] is None


# ══════════════════════ 9 · THE SCAN CARRIES NO QUANTITY
def test_the_scan_request_refuses_a_qty():
    """THE RECIPE ALREADY SAID HOW MANY (Hamthan, 2026-09-29).

    The piece code gives the SKU, whose lines were declared at the breakdown
    release; the packet label gives article/colour/size. Their intersection is one
    recipe line and its `qty_per_piece` IS the number — so asking the operator for
    it asked them to restate something the system holds, and let them restate it
    wrong. It also fanned out: one `qty` with three packets issued that quantity of
    EVERY one.

    `extra="forbid"` is why this raises instead of silently dropping the field.
    Pydantic's default is `ignore`, which would have given a screen still sending
    `qty` a cheerful 201 and no way to learn it had stopped working.
    """
    from pydantic import ValidationError

    from app.modules.store.schemas import StoreScanRequest

    ok = StoreScanRequest(employee_barcode="EMP-1", piece_barcode="PC-1",
                          lot_barcode="LOT-ACC-000001")
    assert not hasattr(ok, "qty")

    with pytest.raises(ValidationError) as exc:
        StoreScanRequest(employee_barcode="EMP-1", piece_barcode="PC-1",
                         lot_barcode="LOT-ACC-000001", qty=2)
    assert "qty" in str(exc.value)


@pytest.mark.asyncio
async def test_a_batch_spends_each_line_its_own_quantity_not_one_number(
        db, pieces, three_trims, cutter):
    """The defect the removal deletes rather than patches: three packets whose lines
    ask for 4, 1 and 120 must spend 4, 1 and 120. While the scan carried a `qty` it
    was passed to every packet in the loop, so one number flattened all three."""
    piece = pieces[0]
    befores = {a: await _on_hand(db, lot.id) for a, lot in three_trims.items()}

    with db.no_autoflush:
        res = await StoreService(db).store_scan(
            piece_id=piece.id, lot_ids=[l.id for l in three_trims.values()],
            employee_id=cutter[0].id, entered_by="STORE")

    assert res["accessory_batch"]["issued"] == 3
    spent = {a: befores[a] - await _on_hand(db, lot.id)
             for a, lot in three_trims.items()}
    # Three different numbers, each its own line's — never one repeated.
    assert spent == pytest.approx({"BTN-4H": 4, "ZIP-N": 1, "THR-40": 120})
