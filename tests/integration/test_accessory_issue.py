"""
INTEGRATION · the accessory kit — the money path.

WHAT THIS FILE PROTECTS
    Before this feature, accessory stock was fiction: buttons, zips and thread
    could be received but nothing ever decremented them. The kit scan is what
    spends them, and it is the newest place in the app where a scan moves money.
    Three properties matter more than anything else here, in this order:

      1. ONE SCAN SPENDS EXACTLY ONCE. Four buttons leave stock, not eight.
      2. A SECOND TAP SPENDS NOTHING. Scan guns double-tap and gateways retry;
         `outstanding = required − issued` must make the repeat a no-op.
      3. A PARTIAL KIT DOES NOT LOOK COMPLETE. A line whose article matches no
         lot must leave the garment unsendable rather than quietly pass.

    Everything else in this file is a gate around those three.
"""
import datetime

import pytest
from sqlalchemy import func, select

from app.core.enums import KitStatus, StorePart, StoreState
from app.modules.barcode.models import (MaterialLot, PieceMaterialIssue,
                                        StyleMaterialSpec)
from app.modules.store.service import StoreService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


async def _on_hand(db, lot_id) -> float:
    return float(await db.scalar(
        select(MaterialLot.on_hand).where(MaterialLot.id == lot_id)))


async def _issue_rows(db, piece_id) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(PieceMaterialIssue)
        .where(PieceMaterialIssue.piece_id == piece_id)))


# ══════════════════════════════════════════════════════════════════ fixtures
@pytest.fixture
async def button_lot(db):
    lot = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                      colour="BLACK", size="18L", uom="pcs", on_hand=1000,
                      is_active=True)
    db.add(lot)
    await db.commit()
    await db.refresh(lot)
    return lot


@pytest.fixture
async def zip_lot(db):
    lot = MaterialLot(category="ACCESSORY", subtype="ZIP", article="ZIP-YKK",
                      colour="BLACK", size="60cm", uom="pcs", on_hand=500,
                      is_active=True)
    db.add(lot)
    await db.commit()
    await db.refresh(lot)
    return lot


@pytest.fixture
async def spec(db, order_tree, button_lot, zip_lot):
    """CLERMONT takes 4 buttons and 1 zip per garment."""
    style = order_tree["style"]
    rows = [
        StyleMaterialSpec(
            style_id=style.id, category="ACCESSORY", subtype="BUTTON",
            article="BTN-4H", colour="BLACK", size="18L",
            qty_per_piece=4, uom="pcs", material_lot_id=button_lot.id),
        StyleMaterialSpec(
            style_id=style.id, category="ACCESSORY", subtype="ZIP",
            article="ZIP-YKK", colour="BLACK", size="60cm",
            qty_per_piece=1, uom="pcs", material_lot_id=zip_lot.id),
    ]
    for r in rows:
        db.add(r)
    await db.commit()
    for r in rows:
        await db.refresh(r)
    return rows


async def _kit(db, piece, employee_id=None, lines=None):
    """The kit scan: the worker, the garment, and what is being issued.

    TWO SCANS, NOT THREE. It used to take a drawer id as well, to find the box
    the garment was assigned to at upload — a scan that existed only to locate a
    number and a 409 that existed only to police it.
    """
    return await StoreService(db).store_scan(
        piece_id=piece.id, part=StorePart.ACCESSORY,
        employee_id=employee_id, lines=lines, entered_by="TESTER")


# ══════════════════════════════════════════════ 1 · one scan spends exactly once
@pytest.mark.asyncio
async def test_a_kit_scan_decrements_each_lot_exactly_once(
        db, pieces, spec, button_lot, zip_lot, cutter):
    piece = pieces[0]
    btn_before, zip_before = await _on_hand(db, button_lot.id), await _on_hand(db, zip_lot.id)

    res = await _kit(db, piece, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, button_lot.id) == pytest.approx(btn_before - 4)
    assert await _on_hand(db, zip_lot.id) == pytest.approx(zip_before - 1)
    # One ledger row per recipe line — not per scan, and not per unit.
    assert await _issue_rows(db, piece.id) == 2
    await db.refresh(piece)
    assert piece.accessories_in is True


# ══════════════════════════════ 2 · THE HEADLINE: a second tap spends nothing
@pytest.mark.asyncio
async def test_scanning_the_same_kit_twice_spends_nothing_the_second_time(
        db, pieces, spec, button_lot, zip_lot, cutter):
    """Scan guns double-tap; gateways retry. Both land here, and both must be
    no-ops rather than a second issue — `outstanding = required − issued`."""
    piece = pieces[0]
    await _kit(db, piece, employee_id=cutter[0].id)
    after_first = await _on_hand(db, button_lot.id)

    res = await _kit(db, piece, employee_id=cutter[0].id)

    assert res["kit"]["issued_now"] == []
    assert len(res["kit"]["already_issued"]) == 2
    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, button_lot.id) == pytest.approx(after_first)
    assert await _issue_rows(db, piece.id) == 2          # still two, not four


# ══════════════════════════════════════ 3 · shortfall warns, never blocks
@pytest.mark.asyncio
async def test_a_short_lot_still_issues_and_reports_the_shortfall(
        db, pieces, spec, button_lot, cutter):
    """The same rule the cut path has always had. The buttons are physically in
    the operator's hand; refusing to record them to protect a number would lose
    the record and teach the floor to work around the system."""
    piece = pieces[0]
    button_lot.on_hand = 2                      # spec wants 4
    await db.commit()

    res = await _kit(db, piece, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, button_lot.id) == pytest.approx(-2)
    warnings = res["kit"]["stock_warnings"]
    assert len(warnings) == 1
    assert warnings[0]["short_by"] == pytest.approx(2)
    assert "BTN-4H" in warnings[0]["note"]


# ══════════════════════ 4 · an unresolvable line must not look like a complete kit
@pytest.mark.asyncio
async def test_a_line_with_no_matching_lot_partial_accepts_and_blocks_the_send(
        db, pieces, spec, button_lot, zip_lot, cutter):
    """The resolvable lines still go out — one bad button must not lose the zip —
    but `accessories_in` stays False, so the garment cannot leave the store with
    an incomplete kit."""
    piece = pieces[0]
    # Retire the button lot and unpin it, so the line resolves to nothing.
    button_lot.is_active = False
    spec[0].material_lot_id = None
    await db.commit()

    res = await _kit(db, piece, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.PARTIAL.value
    assert [r["article"] for r in res["kit"]["unresolved"]] == ["BTN-4H"]
    assert [r["article"] for r in res["kit"]["issued_now"]] == ["ZIP-YKK"]
    await db.refresh(piece)
    assert piece.accessories_in is False
    assert "ACCESSORIES" in res["awaiting"]
    # And the operator is told what to actually do about it. The reason rides on
    # the LINE, not on `next_action`: this garment has no leather in it yet, so
    # its single most useful next action is the leather — which is the store
    # answering the more urgent question first, not losing the kit one.
    assert res["kit"]["unresolved"][0]["reason"] == "NONE",         "NONE means 'no lot matches this line' — receive that stock first"


@pytest.mark.asyncio
async def test_an_ambiguous_line_is_reported_with_its_candidates_and_spends_nothing(
        db, pieces, spec, button_lot, cutter):
    """Two lots carry the same article/colour/size, so the recipe cannot say
    which one to spend. Same verdict the cut-lot picker reaches — but as DATA, so
    one ambiguous button does not lose the rest of the kit."""
    piece = pieces[0]
    twin = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                       colour="BLACK", size="18L", uom="pcs", on_hand=50,
                       is_active=True)
    db.add(twin)
    spec[0].material_lot_id = None          # unpin so it must match by key
    await db.commit()
    before = await _on_hand(db, button_lot.id)

    res = await _kit(db, piece, employee_id=cutter[0].id)

    unresolved = res["kit"]["unresolved"]
    assert [r["article"] for r in unresolved] == ["BTN-4H"]
    assert unresolved[0]["reason"] == "AMBIGUOUS"
    assert len(unresolved[0]["candidate_lot_ids"]) == 2
    assert await _on_hand(db, button_lot.id) == pytest.approx(before)


# ══════════════════════════════════ 5 · a pinned lot beats key resolution
@pytest.mark.asyncio
async def test_a_pinned_lot_wins_over_a_key_match(
        db, pieces, spec, button_lot, cutter):
    """Two lots would match the key; the recipe names one, so ambiguity never
    arises. This is why the authoring screen pins material_lot_id."""
    piece = pieces[0]
    twin = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                       colour="BLACK", size="18L", uom="pcs", on_hand=50,
                       is_active=True)
    db.add(twin)
    await db.commit()
    await db.refresh(twin)

    res = await _kit(db, piece, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, twin.id) == pytest.approx(50)      # untouched
    assert await _on_hand(db, button_lot.id) == pytest.approx(996)


# ═══════════════════════════ 6 · the gate ordering inside store_scan is intact
@pytest.mark.asyncio
async def test_a_garment_already_sent_refuses_the_kit_and_spends_nothing(
        db, pieces, spec, button_lot, cutter):
    """THE FIRST BUSINESS AUTHORITY IN store_scan, and adding a kit branch must
    not have moved it: nothing is spent on the way to the refusal.

    THIS TEST USED TO BE "the wrong drawer is a 409". There is no wrong drawer —
    that rejection policed an assignment the system invented at upload, and its
    absence is the feature. What remains genuinely refusable is a garment that
    has already LEFT the store, which is a fact about the world rather than about
    the paperwork.
    """
    from fastapi import HTTPException
    piece = pieces[0]
    piece.store_state = StoreState.SENDED.value
    await db.commit()
    before = await _on_hand(db, button_lot.id)

    with pytest.raises(HTTPException) as exc:
        await _kit(db, piece, employee_id=cutter[0].id)

    assert exc.value.status_code == 409
    assert "already been sent" in str(exc.value.detail)
    assert await _on_hand(db, button_lot.id) == pytest.approx(before)
    assert await _issue_rows(db, piece.id) == 0


@pytest.mark.asyncio
async def test_accessory_is_never_inferred(db, pieces, spec, cutter,
                                           operations, ready_for_store):
    """A mis-inferred cut part sets the wrong boolean, which a human can undo. A
    mis-inferred KIT would spend money. So inference still chooses only between
    the two cut parts, and a garment holding both gets the old 409 rather than a
    surprise kit."""
    from fastapi import HTTPException
    piece = pieces[0]
    piece.leather_in = True
    piece.lining_in = True
    await db.commit()
    before = await _issue_rows(db, piece.id)

    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=piece.id, part=None, employee_id=cutter[0].id)

    assert exc.value.status_code == 409
    assert "already has both" in str(exc.value.detail)
    assert await _issue_rows(db, piece.id) == before


@pytest.mark.asyncio
async def test_a_style_with_no_accessory_spec_is_told_so_loudly(
        db, pieces, cutter):
    """A cheerful 200 here would let an operator believe they issued a kit that
    does not exist, and nobody would find out until finishing."""
    from fastapi import HTTPException
    piece = pieces[0]

    with pytest.raises(HTTPException) as exc:
        await _kit(db, piece, employee_id=cutter[0].id)

    assert exc.value.status_code == 409
    assert "no accessory spec" in str(exc.value.detail)


# ══════════════════ 7 · R1: the RECEIVED interaction, both halves together
@pytest.mark.asyncio
async def test_auto_received_waits_for_the_kit_when_the_style_has_one(
        db, pieces, spec, operations, cutter, lining_cutter):
    """HALF ONE OF R1. Auto-RECEIVE used to fire the instant both cut parts were
    in. With a recipe outstanding that is wrong: the garment is not complete, and
    firing there is what made the kit scan hit an already-RECEIVED garment."""
    from tests.conftest import _ready_for_store
    piece = pieces[0]
    await _ready_for_store(db, operations, piece, cutter[0].id)

    await StoreService(db).store_scan(piece_id=piece.id,
                                      part=StorePart.LEATHER,
                                      employee_id=cutter[0].id)
    res = await StoreService(db).store_scan(piece_id=piece.id,
                                            part=StorePart.LINING,
                                            employee_id=lining_cutter[0].id)

    assert res["auto_received"] is False
    assert res["store_state"] == StoreState.HOLDING_BOTH.value
    assert "ACCESSORIES" in res["awaiting"]
    # The checklist rides the CUT scan too — the person holding the leather is
    # the one who has to find the buttons.
    assert res["kit"]["status"] == KitStatus.PENDING.value
    assert "BTN-4H" in res["kit"]["summary_line"]

    # ...and the kit scan then completes it.
    kit = await _kit(db, piece, employee_id=cutter[0].id)
    assert kit["auto_received"] is True
    assert kit["store_state"] == StoreState.RECEIVED.value


@pytest.mark.asyncio
async def test_a_kit_may_be_issued_into_an_already_received_garment(
        db, pieces, spec, cutter):
    """HALF TWO OF R1, and the regression that would otherwise make the feature
    unusable. A garment that reached RECEIVED before its spec existed must still
    accept its kit: an accessory issue is purely ADDITIVE and cannot revoke a
    gate the garment has already passed, which is the property the
    RECEIVED/SENDED block actually protects."""
    piece = pieces[0]
    piece.leather_in = True
    piece.lining_in = True
    piece.store_state = StoreState.RECEIVED.value
    await db.commit()

    res = await _kit(db, piece, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    await db.refresh(piece)
    assert piece.accessories_in is True


@pytest.mark.asyncio
async def test_a_sent_garment_still_refuses_every_part_including_a_kit(
        db, pieces, spec, cutter):
    """SENDED is different in kind: the garment has physically left the store, so
    there is nothing there to put anything into."""
    from fastapi import HTTPException
    piece = pieces[0]
    piece.store_state = StoreState.SENDED.value
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _kit(db, piece, employee_id=cutter[0].id)
    assert exc.value.status_code == 409


# ══════════════════════════ 8 · the kit gates the SEND, not line-stitching
@pytest.mark.asyncio
async def test_an_unkitted_garment_cannot_leave_the_store(
        db, pieces, spec, operations, cutter, dm):
    """Sending is the garment physically LEAVING the store, which is the moment
    the accessories have to be with it. (Line-stitching is deliberately NOT
    gated on the kit — buttons are an input to finishing, not to stitching.)"""
    from tests.conftest import _ready_for_store
    piece = pieces[0]
    await _ready_for_store(db, operations, piece, cutter[0].id)
    svc = StoreService(db)
    await svc.store_scan(piece_id=piece.id, part=StorePart.LEATHER,
                         employee_id=cutter[0].id)
    await svc.store_scan(piece_id=piece.id, part=StorePart.LINING,
                         employee_id=cutter[0].id)

    blocked = await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert blocked["sent"] == []
    assert blocked["not_ready"][0]["missing"] == "accessory kit"

    await _kit(db, piece, employee_id=cutter[0].id)
    ok = await StoreService(db).send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert len(ok["sent"]) == 1


@pytest.mark.asyncio
async def test_the_garment_leaves_the_store_with_all_three_buckets_empty(
        db, pieces, spec, cutter):
    """PACKAGE_EXPORT takes the garment OUT OF THE STORE, and it takes its kit
    with it.

    THIS USED TO BE "the drawer recycles to WAITING for the next piece". There is
    no box to hand back — a drawer recycled because it was reused, and a garment
    ships once. What still has to be true is that nothing is left claiming to
    hold parts that physically left the building.
    """
    piece = pieces[0]
    await _kit(db, piece, employee_id=cutter[0].id)
    await db.refresh(piece)
    assert piece.accessories_in is True

    await StoreService(db).release_nocommit(piece.id)
    await db.commit()
    await db.refresh(piece)

    assert (piece.leather_in, piece.lining_in, piece.accessories_in) == (
        False, False, False)
    assert piece.store_state == StoreState.WAITING.value


# ══════════════════════════ 9 · THE BACK-COMPATIBILITY PROOF
@pytest.mark.asyncio
async def test_a_style_with_no_spec_reaches_received_on_exactly_the_old_scans(
        db, pieces, operations, cutter):
    """Every style that predates this feature has no accessory lines, so
    `kit_required` is False, so the completeness predicate collapses to the two
    clauses it had before. This asserts that directly: leather + lining and the
    garment auto-receives, with no third scan and no kit anywhere in sight.

    If this test ever fails, the feature has become retroactive — which is
    precisely what keying the requirement on "the style declares accessories"
    exists to prevent."""
    from tests.conftest import _ready_for_store
    piece = pieces[0]                 # NOTE: no `spec` fixture here
    await _ready_for_store(db, operations, piece, cutter[0].id)
    svc = StoreService(db)

    await svc.store_scan(piece_id=piece.id, part=StorePart.LEATHER,
                         employee_id=cutter[0].id)
    res = await svc.store_scan(piece_id=piece.id, part=StorePart.LINING,
                               employee_id=cutter[0].id)

    assert res["auto_received"] is True
    assert res["store_state"] == StoreState.RECEIVED.value
    assert "ACCESSORIES" not in res["awaiting"]
    # And the block that rides every scan says "nothing to kit", so the screen
    # hides the panel rather than rendering an empty checklist.
    assert res["kit"]["status"] == KitStatus.NOT_REQUIRED.value
