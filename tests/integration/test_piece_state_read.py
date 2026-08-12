"""
INTEGRATION · GET /production/piece-state — bugs #4, #6, #7, #8, #12.

WHY ONE READ ANSWERS FOUR BUGS
    They are all the same missing question: "what is true about this piece, right
    now, before I log anything?"

      #4  the operator had to pick the stage by hand, because stage inference ran
          only at write time and the UI could not see it in advance
      #6  later stage cards were scannable before their predecessor was done,
          because the UI had no lock map
      #7  the cutting screen showed no serial number and no article
      #8  per-piece scanning needs a server answer to "is this style finished?"
      #12 the drawer was invisible outside the Store hub

THE INVARIANT THIS FILE EXISTS TO PROTECT
    The read and the write must agree. piece_state reuses _infer_stage_for_piece,
    _sequence_ok and _merge_ok — the very predicates log_batch enforces — rather
    than restating them. A second copy would drift, and both drift directions are
    bad: a card the UI opens that the log then rejects, or (worse) a card the UI
    locks that the log would have accepted, silently stalling the line.

    So several tests below assert the read against the write, not against a
    hard-coded expectation.
"""
import datetime

import pytest

from app.core.enums import ScreenContext
from app.modules.drawers.service import DrawerService
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


def _card(state: dict, stage: str) -> dict:
    return next(s for s in state["stages"] if s["stage"] == stage)


# ══════════════════════════════════════════════ bug #4 — the stage is inferred
@pytest.mark.asyncio
async def test_a_fresh_piece_offers_the_two_cut_entries_and_nothing_else(
    db, operations, pieces, cutting_mgr
):
    piece, _ = pieces[0]
    state = await ProductionService(db).piece_state(piece.id, user=cutting_mgr)

    assert state["completed_stages"] == []
    assert _card(state, "LEATHER_CUTTING")["state"] == "next"
    assert _card(state, "LINING_CUTTING")["state"] == "next"
    # Everything downstream is shut, and says why.
    for stage in ("FUSING", "PASTING", "SHELL_STITCHING", "PACKAGE_EXPORT"):
        card = _card(state, stage)
        assert card["state"] == "locked"
        assert card["reason"], f"{stage} is locked with no reason given"


@pytest.mark.asyncio
async def test_the_next_stage_advances_as_work_is_logged(
    db, operations, pieces, cutter, paster, cutting_mgr, stitching_mgr,
    leather_lot
):
    piece, _ = pieces[0]
    svc = ProductionService(db)

    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=12.0)

    state = await svc.piece_state(piece.id, user=stitching_mgr)
    assert state["next_stage"] == "FUSING"                       # bug #4
    assert _card(state, "LEATHER_CUTTING")["state"] == "completed"
    assert _card(state, "FUSING")["state"] == "next"
    assert _card(state, "PASTING")["state"] == "locked"          # bug #6

    await svc.log_batch(user=stitching_mgr, employee_id=paster[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.PIPELINE)

    state = await svc.piece_state(piece.id, user=stitching_mgr)
    assert state["next_stage"] == "PASTING"
    assert _card(state, "PASTING")["state"] == "next"


# ══════════════════════════════════════════════ bug #6 — the read matches the write
@pytest.mark.asyncio
async def test_a_locked_card_is_exactly_what_the_log_refuses(
    db, operations, pieces, cutter, tailor, cutting_mgr, stitching_mgr,
    leather_lot
):
    """THE AGREEMENT TEST. If these two ever disagree the UI is lying."""
    piece, _ = pieces[0]
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=12.0)

    state = await svc.piece_state(piece.id, user=stitching_mgr)
    assert _card(state, "PASTING")["state"] == "locked"

    # The write agrees: PASTING is not reachable, and for the same reason.
    res = await svc.log_batch(user=stitching_mgr, employee_id=tailor[0].id,
                              piece_ids=[piece.id], work_date=TODAY,
                              screen=ScreenContext.PIPELINE)
    assert res["stage"] == "FUSING"      # inference lands on the OPEN card
    assert res["count_logged"] == 1


@pytest.mark.asyncio
async def test_line_stitching_is_locked_on_the_merge_gate_and_names_the_drawer(
    db, operations, pieces, cutting_mgr
):
    """bug #6 + #12: the lock reason has to be actionable, so it names the
    drawer the operator must go and deal with."""
    piece, drawer = pieces[0]
    state = await ProductionService(db).piece_state(piece.id, user=cutting_mgr)

    card = _card(state, "LINE_STITCHING")
    assert card["state"] == "locked" and card["gate"] == "merge"
    assert drawer.code in card["reason"]
    assert "SENDED" in card["reason"]


@pytest.mark.asyncio
async def test_the_lining_cut_is_not_applicable_to_an_unlined_piece(
    db, operations, pieces, cutting_mgr
):
    """"Locked" would be a lie: nothing will ever unlock it, because this garment
    has no lining. A card that can never open is a different thing from a card
    that is waiting its turn."""
    piece, _ = pieces[0]
    piece.needs_lining = False
    await db.commit()

    state = await ProductionService(db).piece_state(piece.id, user=cutting_mgr)
    card = _card(state, "LINING_CUTTING")
    assert card["state"] == "not_applicable"
    assert "needs no lining" in card["reason"]


# ══════════════════════════════════════════════ bug #12 — the drawer, everywhere
@pytest.mark.asyncio
async def test_the_drawer_travels_with_the_piece(db, operations, pieces, cutter,
                                                 cutting_mgr):
    piece, drawer = pieces[0]
    await DrawerService(db).store_scan(drawer_id=drawer.id, piece_id=piece.id)

    state = await ProductionService(db).piece_state(piece.id, user=cutting_mgr)
    assert state["drawer"]["code"] == drawer.code
    assert state["drawer"]["holding"] == "HOLDING LEATHER"
    assert state["drawer"]["leather_in"] is True
    # and the same block rides along inside the piece card
    assert state["piece"]["drawer"]["code"] == drawer.code


# ══════════════════════════════════════════════ bug #7 — article + serial
@pytest.mark.asyncio
async def test_the_piece_card_carries_article_and_a_padded_serial(
    db, operations, pieces, cutting_mgr, order_tree
):
    piece, _ = pieces[2]        # seq 3
    state = await ProductionService(db).piece_state(piece.id, user=cutting_mgr)

    card = state["piece"]
    assert card["article"] == order_tree["style"].article
    assert card["serial"] == "003"          # not 3 — the label reads 001/002/003
    assert card["order_number"] == "JP-PO"
    assert card["style_name"] == "CLERMONT"
    assert card["size"] == "M"
    # the ready-to-print sticker line, so every screen renders it identically
    assert card["label_line"].startswith("JP-PO · CLERMONT · CL1")
    assert card["label_line"].endswith("003")


# ══════════════════════════════════════════════ bug #8 — is the style finished?
@pytest.mark.asyncio
async def test_the_sku_closes_only_when_every_piece_is_logged(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    svc = ProductionService(db)
    first = pieces[0][0]

    state = await svc.piece_state(first.id, user=cutting_mgr)
    assert state["sku"]["total"] == 5
    assert state["sku"]["done"] == 0 and state["sku"]["remaining"] == 5
    assert state["sku"]["closed"] is False

    # cut four of the five
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[p.id for p, _ in pieces[:4]], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    last = pieces[4][0]
    state = await svc.piece_state(last.id, user=cutting_mgr)
    assert state["sku"]["done"] == 4 and state["sku"]["remaining"] == 1
    assert state["sku"]["closed"] is False, "one piece is still outstanding"

    res = await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                              piece_ids=[last.id], work_date=TODAY,
                              screen=ScreenContext.LEATHER_CUT,
                              leather_lot_id=leather_lot.id, consumption_qty=10.0)
    # The LOG ITSELF reports the close, so the screen needs no second call.
    assert res["sku_progress"]["remaining"] == 0
    assert res["sku_progress"]["closed"] is True


@pytest.mark.asyncio
async def test_the_log_response_carries_each_pieces_drawer(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    """bug #12 on the write path — the scan screen must not need a second call
    to find out where the garment it just logged actually lives."""
    piece, drawer = pieces[0]
    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=12.0)

    assert res["drawer_by_piece"][piece.code]["code"] == drawer.code


# ══════════════════════════════════════════════ role awareness
@pytest.mark.asyncio
async def test_can_log_next_reflects_the_role_gate(
    db, operations, pieces, cutter, cutting_mgr, lining_mgr, stitching_mgr,
    leather_lot
):
    """So the screen can say "ask the stitching manager" instead of letting the
    scan come back as a 403 the operator has to interpret."""
    piece, _ = pieces[0]
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=12.0)

    # next is FUSING, which belongs to the stitching manager
    assert (await svc.piece_state(piece.id, user=stitching_mgr))["can_log_next"] is True
    assert (await svc.piece_state(piece.id, user=lining_mgr))["can_log_next"] is False
