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
         lot must leave the drawer unsendable rather than quietly pass.

    Everything else in this file is a gate around those three.
"""
import datetime

import pytest
from sqlalchemy import func, select

from app.core.enums import DrawerPart, DrawerState, KitStatus
from app.modules.barcode.models import (MaterialLot, PieceMaterialIssue,
                                        StyleMaterialSpec)
from app.modules.drawers.service import DrawerService

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


async def _kit(db, piece, drawer, employee_id=None, lines=None):
    return await DrawerService(db).store_scan(
        drawer_id=drawer.id, piece_id=piece.id, part=DrawerPart.ACCESSORY,
        employee_id=employee_id, lines=lines, entered_by="TESTER")


# ══════════════════════════════════════════════ 1 · one scan spends exactly once
@pytest.mark.asyncio
async def test_a_kit_scan_decrements_each_lot_exactly_once(
        db, pieces, spec, button_lot, zip_lot, cutter):
    piece, drawer = pieces[0]
    btn_before, zip_before = await _on_hand(db, button_lot.id), await _on_hand(db, zip_lot.id)

    res = await _kit(db, piece, drawer, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, button_lot.id) == pytest.approx(btn_before - 4)
    assert await _on_hand(db, zip_lot.id) == pytest.approx(zip_before - 1)
    # One ledger row per recipe line — not per scan, and not per unit.
    assert await _issue_rows(db, piece.id) == 2
    await db.refresh(drawer)
    assert drawer.accessories_in is True


# ══════════════════════════════ 2 · THE HEADLINE: a second tap spends nothing
@pytest.mark.asyncio
async def test_scanning_the_same_kit_twice_spends_nothing_the_second_time(
        db, pieces, spec, button_lot, zip_lot, cutter):
    """Scan guns double-tap; gateways retry. Both land here, and both must be
    no-ops rather than a second issue — `outstanding = required − issued`."""
    piece, drawer = pieces[0]
    await _kit(db, piece, drawer, employee_id=cutter[0].id)
    after_first = await _on_hand(db, button_lot.id)

    res = await _kit(db, piece, drawer, employee_id=cutter[0].id)

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
    piece, drawer = pieces[0]
    button_lot.on_hand = 2                      # spec wants 4
    await db.commit()

    res = await _kit(db, piece, drawer, employee_id=cutter[0].id)

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
    but `accessories_in` stays False, so the drawer cannot leave the store with
    an incomplete kit."""
    piece, drawer = pieces[0]
    # Retire the button lot and unpin it, so the line resolves to nothing.
    button_lot.is_active = False
    spec[0].material_lot_id = None
    await db.commit()

    res = await _kit(db, piece, drawer, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.PARTIAL.value
    assert [r["article"] for r in res["kit"]["unresolved"]] == ["BTN-4H"]
    assert [r["article"] for r in res["kit"]["issued_now"]] == ["ZIP-YKK"]
    await db.refresh(drawer)
    assert drawer.accessories_in is False
    assert "ACCESSORIES" in res["awaiting"]
    # And the operator is told what to actually do about it.
    assert "no stock lot matches" in res["next_action"].lower()


@pytest.mark.asyncio
async def test_an_ambiguous_line_is_reported_with_its_candidates_and_spends_nothing(
        db, pieces, spec, button_lot, cutter):
    """Two lots carry the same article/colour/size, so the recipe cannot say
    which one to spend. Same verdict the cut-lot picker reaches — but as DATA, so
    one ambiguous button does not lose the rest of the kit."""
    piece, drawer = pieces[0]
    twin = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                       colour="BLACK", size="18L", uom="pcs", on_hand=50,
                       is_active=True)
    db.add(twin)
    spec[0].material_lot_id = None          # unpin so it must match by key
    await db.commit()
    before = await _on_hand(db, button_lot.id)

    res = await _kit(db, piece, drawer, employee_id=cutter[0].id)

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
    piece, drawer = pieces[0]
    twin = MaterialLot(category="ACCESSORY", subtype="BUTTON", article="BTN-4H",
                       colour="BLACK", size="18L", uom="pcs", on_hand=50,
                       is_active=True)
    db.add(twin)
    await db.commit()
    await db.refresh(twin)

    res = await _kit(db, piece, drawer, employee_id=cutter[0].id)

    assert res["kit"]["status"] == KitStatus.ISSUED.value
    assert await _on_hand(db, twin.id) == pytest.approx(50)      # untouched
    assert await _on_hand(db, button_lot.id) == pytest.approx(996)


# ═══════════════════════════ 6 · the gate ordering inside store_scan is intact
@pytest.mark.asyncio
async def test_the_merge_map_still_refuses_first_and_no_stock_moves(
        db, pieces, spec, button_lot, cutter):
    """The merge map is the FIRST business authority in store_scan, and adding a
    kit branch must not have moved it. A piece scanned into the wrong drawer is a
    409 — and, critically, nothing is spent on the way to that 409."""
    from fastapi import HTTPException
    piece, _own = pieces[0]
    _other_piece, wrong_drawer = pieces[1]
    before = await _on_hand(db, button_lot.id)

    with pytest.raises(HTTPException) as exc:
        await _kit(db, piece, wrong_drawer, employee_id=cutter[0].id)

    assert exc.value.status_code == 409
    assert "not merged to drawer" in str(exc.value.detail)
    assert await _on_hand(db, button_lot.id) == pytest.approx(before)
    assert await _issue_rows(db, piece.id) == 0


@pytest.mark.asyncio
async def test_accessory_is_never_inferred(db, pieces, spec, cutter,
                                           operations, ready_for_store):
    """A mis-inferred cut part sets the wrong boolean, which a human can undo. A
    mis-inferred KIT would spend money. So inference still chooses only between
    the two cut parts, and a drawer holding both gets the old 409 rather than a
    surprise kit."""
    from fastapi import HTTPException
    piece, drawer = pieces[0]
    drawer.leather_in = True
    drawer.lining_in = True
    await db.commit()
    before = await _issue_rows(db, piece.id)

    with pytest.raises(HTTPException) as exc:
        await DrawerService(db).store_scan(
            drawer_id=drawer.id, piece_id=piece.id, part=None,
            employee_id=cutter[0].id)

    assert exc.value.status_code == 409
    assert "already holds both" in str(exc.value.detail)
    assert await _issue_rows(db, piece.id) == before


@pytest.mark.asyncio
async def test_a_style_with_no_accessory_spec_is_told_so_loudly(
        db, pieces, cutter):
    """A cheerful 200 here would let an operator believe they issued a kit that
    does not exist, and nobody would find out until finishing."""
    from fastapi import HTTPException
    piece, drawer = pieces[0]

    with pytest.raises(HTTPException) as exc:
        await _kit(db, piece, drawer, employee_id=cutter[0].id)

    assert exc.value.status_code == 409
    assert "no accessory spec" in str(exc.value.detail)


# ══════════════════ 7 · R1: the RECEIVED interaction, both halves together
@pytest.mark.asyncio
async def test_auto_received_waits_for_the_kit_when_the_style_has_one(
        db, pieces, spec, operations, cutter, lining_cutter):
    """HALF ONE OF R1. Auto-RECEIVE used to fire the instant both cut parts were
    in. With a recipe outstanding that is wrong: the drawer is not complete, and
    firing there is what made the third (kit) scan hit a RECEIVED drawer."""
    from tests.conftest import _ready_for_store
    piece, drawer = pieces[0]
    await _ready_for_store(db, operations, piece, cutter[0].id)

    await DrawerService(db).store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                       part=DrawerPart.LEATHER,
                                       employee_id=cutter[0].id)
    res = await DrawerService(db).store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                             part=DrawerPart.LINING,
                                             employee_id=lining_cutter[0].id)

    assert res["auto_received"] is False
    assert res["state"] == DrawerState.HOLDING_BOTH.value
    assert "ACCESSORIES" in res["awaiting"]
    # The checklist rides the CUT scan too — the person holding the leather is
    # the one who has to find the buttons.
    assert res["kit"]["status"] == KitStatus.PENDING.value
    assert "BTN-4H" in res["kit"]["summary_line"]

    # ...and the kit scan then completes it.
    kit = await _kit(db, piece, drawer, employee_id=cutter[0].id)
    assert kit["auto_received"] is True
    assert kit["state"] == DrawerState.RECEIVED.value


@pytest.mark.asyncio
async def test_a_kit_may_be_issued_into_an_already_received_drawer(
        db, pieces, spec, cutter):
    """HALF TWO OF R1, and the regression that would otherwise make the feature
    unusable. A drawer that reached RECEIVED before its spec existed must still
    accept its kit: an accessory issue is purely ADDITIVE and cannot revoke a
    gate the drawer has already passed, which is the property the RECEIVED/SENDED
    block actually protects."""
    piece, drawer = pieces[0]
    drawer.leather_in = True
    drawer.lining_in = True
    drawer.state = DrawerState.RECEIVED.value
    await db.commit()

    res = await _kit(db, piece, drawer, employee_id=cutter[0].id)

    assert res["late_kit"] is True
    assert res["kit"]["status"] == KitStatus.ISSUED.value
    await db.refresh(drawer)
    assert drawer.accessories_in is True


@pytest.mark.asyncio
async def test_a_sended_drawer_still_refuses_every_part_including_a_kit(
        db, pieces, spec, cutter):
    """SENDED is different in kind: the drawer has physically left the store, so
    there is nothing there to put anything into."""
    from fastapi import HTTPException
    piece, drawer = pieces[0]
    drawer.state = DrawerState.SENDED.value
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _kit(db, piece, drawer, employee_id=cutter[0].id)
    assert exc.value.status_code == 409


# ══════════════════════════ 8 · the kit gates the SEND, not line-stitching
@pytest.mark.asyncio
async def test_an_unkitted_drawer_cannot_leave_the_store(
        db, pieces, spec, operations, cutter, dm):
    """Sending is the garment physically LEAVING the store, which is the moment
    the accessories have to be in the drawer. (Line-stitching is deliberately NOT
    gated on the kit — buttons are an input to finishing, not to stitching.)"""
    from tests.conftest import _ready_for_store
    piece, drawer = pieces[0]
    await _ready_for_store(db, operations, piece, cutter[0].id)
    svc = DrawerService(db)
    await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                         part=DrawerPart.LEATHER, employee_id=cutter[0].id)
    await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                         part=DrawerPart.LINING, employee_id=cutter[0].id)

    blocked = await svc.send_batch(drawer_ids=[drawer.id], actor_id=dm.id)
    assert blocked["sent"] == []
    reason = blocked["not_ready"][0]["reason"]
    assert "accessory kit" in reason
    assert "part=ACCESSORY" in reason          # actionable, not just refused

    await _kit(db, piece, drawer, employee_id=cutter[0].id)
    ok = await DrawerService(db).send_batch(drawer_ids=[drawer.id], actor_id=dm.id)
    assert len(ok["sent"]) == 1


@pytest.mark.asyncio
async def test_the_drawer_recycles_with_all_three_buckets_empty(
        db, pieces, spec, cutter):
    """PACKAGE_EXPORT hands the drawer back to the pool. The kit went out with
    the garment, so the next piece merged here must start unkitted."""
    piece, drawer = pieces[0]
    await _kit(db, piece, drawer, employee_id=cutter[0].id)
    await db.refresh(drawer)
    assert drawer.accessories_in is True

    await DrawerService(db).release_nocommit(piece.id)
    await db.commit()
    await db.refresh(drawer)

    assert (drawer.leather_in, drawer.lining_in, drawer.accessories_in) == (
        False, False, False)
    assert drawer.state == DrawerState.WAITING.value


# ══════════════════════════ 9 · THE BACK-COMPATIBILITY PROOF
@pytest.mark.asyncio
async def test_a_style_with_no_spec_reaches_received_on_exactly_the_old_scans(
        db, pieces, operations, cutter):
    """Every style that predates this feature has no accessory lines, so
    `kit_required` is False, so the completeness predicate collapses to the two
    clauses it had before. This asserts that directly: leather + lining and the
    drawer auto-receives, with no third scan and no kit anywhere in sight.

    If this test ever fails, the feature has become retroactive — which is
    precisely what keying the requirement on "the style declares accessories"
    exists to prevent."""
    from tests.conftest import _ready_for_store
    piece, drawer = pieces[0]                 # NOTE: no `spec` fixture here
    await _ready_for_store(db, operations, piece, cutter[0].id)
    svc = DrawerService(db)

    await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                         part=DrawerPart.LEATHER, employee_id=cutter[0].id)
    res = await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                               part=DrawerPart.LINING, employee_id=cutter[0].id)

    assert res["auto_received"] is True
    assert res["state"] == DrawerState.RECEIVED.value
    assert "ACCESSORIES" not in res["awaiting"]
    # And the block that rides every scan says "nothing to kit", so the screen
    # hides the panel rather than rendering an empty checklist.
    assert res["kit"]["status"] == KitStatus.NOT_REQUIRED.value
