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
async def test_a_sized_accessory_missing_an_ordered_size_blocks_release(
        db, order_tree):
    """THE SAME FAILURE, CAUGHT A DAY EARLIER — at the last moment anyone can be
    asked. Zip lines for M and L, an order that also runs S: the S garments get
    kit_required=False and ship with no zip, and nothing else in the system would
    ever have said so."""
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    for size in ("S", "L"):
        db.add(SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
                   size=size, qty_ordered=2, code=f"JP-CLERMONT-PINE-{size}"))
    for size in ("M", "L"):
        db.add(StyleMaterialSpec(
            style_id=style.id, category="ACCESSORY", subtype="ZIP",
            article="ZIP-N", colour="BLACK", size=size, garment_size=size,
            qty_per_piece=1, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    sized = [b for b in blockers if "per garment size" in b]
    assert len(sized) == 1
    assert "ZIP-N (ZIP)" in sized[0]
    assert "S" in sized[0]


@pytest.mark.asyncio
async def test_one_unscoped_line_covers_every_ordered_size(db, order_tree):
    """A generic 18L button is one row, and must stay one row — a gate that fires
    on the normal case is a gate people learn to ignore."""
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    db.add(SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
               size="S", qty_ordered=2, code="JP-CLERMONT-PINE-S"))
    db.add(StyleMaterialSpec(
        style_id=style.id, category="ACCESSORY", subtype="BUTTON",
        article="BTN-18L", colour="BLACK", size="18L", garment_size=None,
        qty_per_piece=4, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    assert [b for b in blockers if "per garment size" in b] == []


@pytest.mark.asyncio
async def test_several_unscoped_sizes_of_one_article_block_release_as_ambiguous(
        db, order_tree):
    """THE HOLE THE FIX FOR THE OTHER HOLE OPENED.

    garment_size is no longer inferred from a numeric material size, because that
    inference scoped a 60cm zip to 4XL jackets. But the inference was also the only
    thing stopping ZIP 48 / 50 / 52 — plainly three garment sizes — from becoming
    three unscoped lines that all land on every jacket. '50' cannot be told from
    the token, so the DM is asked instead of guessed at.
    """
    style = order_tree["style"]
    for size in ("48", "50", "52"):
        db.add(StyleMaterialSpec(
            style_id=style.id, category="ACCESSORY", subtype="ZIP",
            article="ZIP-N", colour="BLACK", size=size, garment_size=None,
            qty_per_piece=1, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    ambiguous = [b for b in blockers if "none of them says" in b]
    assert len(ambiguous) == 1
    assert "48, 50, 52" in ambiguous[0]


@pytest.mark.asyncio
async def test_a_per_colourway_override_covers_its_own_sku_s_size(
        db, order_tree):
    """A DM who covered the sizes through per-colourway overrides must not be told
    they are uncovered. A false blocker on a release teaches people the gate is
    noise, which is worse than no gate."""
    from app.modules.clients.models import SKU
    style = order_tree["style"]
    small = SKU(style_id=style.id, color_code="NAVY", color_name="NAVY",
                size="S", qty_ordered=2, code="JP-CLERMONT-NAVY-S")
    db.add(small)
    await db.flush()
    db.add(StyleMaterialSpec(
        style_id=style.id, category="ACCESSORY", subtype="ZIP",
        article="ZIP-N", colour="BLACK", size="M", garment_size="M",
        qty_per_piece=1, uom="pcs"))
    # Scoped to the S colourway and saying nothing about garment size: being that
    # SKU's line already means it is for that SKU's size.
    db.add(StyleMaterialSpec(
        style_id=style.id, sku_id=small.id, category="ACCESSORY", subtype="ZIP",
        article="ZIP-N", colour="NAVY", size="S", garment_size=None,
        qty_per_piece=1, uom="pcs"))
    await db.commit()

    blockers = (await StyleSpecService(db).blockers_for_styles(
        [style.id]))[style.id]
    assert [b for b in blockers if "per garment size" in b] == []


@pytest.mark.asyncio
async def test_a_part_issue_under_an_approval_leaves_it_open_for_the_rest(
        db, pieces, sized_buttons, cutter, dm):
    """THE CORNER THAT MARKING IT SPENT UP FRONT GOT WRONG.

    Two of the four buttons go in. If the approval were consumed there, the other
    two could never be issued from the same packet — and the CONSUMED branch would
    tell the operator nothing was owed, which is flatly untrue. The approval covers
    "this packet into this garment", and a part-issue has not finished that.
    """
    from fastapi import HTTPException
    piece = pieces[0]
    with pytest.raises(HTTPException):
        await StoreService(db).store_scan(
            piece_id=piece.id, lot_id=sized_buttons["L"].id,
            employee_id=cutter[0].id, entered_by="STORE")
    request = (await _requests(db, piece.id))[0]
    await StoreService(db).decide_substitution(
        request.id, approve=True, actor_user_id=dm.id, actor_name="DM")

    part = await StoreService(db).store_scan(
        piece_id=piece.id, lot_id=sized_buttons["L"].id, qty=2,
        employee_id=cutter[0].id, entered_by="STORE")

    assert part["kit"]["status"] == KitStatus.PARTIAL.value
    await db.refresh(request)
    assert request.status == KitSubstitutionStatus.APPROVED.value, (
        "two buttons are still owed on the line")
    assert "still owed" in part["substitution"]["message"]
    await db.refresh(piece)
    assert piece.accessories_in is False

    # The rest goes in under the SAME decision, and now it is spent.
    rest = await StoreService(db).store_scan(
        piece_id=piece.id, lot_id=sized_buttons["L"].id,
        employee_id=cutter[0].id, entered_by="STORE")
    assert rest["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, sized_buttons["L"].id) == pytest.approx(496)
    await db.refresh(request)
    assert request.status == KitSubstitutionStatus.CONSUMED.value
    await db.refresh(piece)
    assert piece.accessories_in is True
