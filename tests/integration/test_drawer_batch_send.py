"""
INTEGRATION · the store hub's new rhythm — bugs #13, #14, #15, #18.

WHAT CHANGED, AND WHY
    The store used to work one drawer at a time and two clicks deep: press
    RECEIVED on a drawer, then press SEND on the same drawer, then repeat. Both
    halves were wrong for different reasons.

    RECEIVED asserted exactly one thing — "this drawer holds everything its
    garment needs" — and the scan that just landed is what makes that true. There
    was no judgement left for a human to add, so the click was pure latency. It
    is now automatic (#13).

    SEND is a real decision and stays manual, but it became PLURAL, because the
    store does not release garments one at a time: it fills a bank of drawers and
    moves them together (#14). Sending to STITCHING is what opens the merge gate
    for that whole bunch of pieces.

    And scanning a part in is NOT completion (#15): the piece stays in the drawer
    until someone sends it. The response says so in as many words, because the
    frontend was marking items finished at scan time.

    The Hold Leather / Hold Lining buttons are gone (#18) — the bucket is read off
    the piece's own history and the drawer's contents.

PARTIAL ACCEPT is the design rule that ties this to the rest of the system: one
drawer that is not ready must never lose the twenty that are, exactly as one bad
piece never loses the 39 good ones in a production scan.
"""
import datetime

import pytest
from fastapi import HTTPException

from app.core.enums import DrawerPart, DrawerState, ScreenContext
from app.modules.drawers.service import DrawerService
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


async def _fill(db, piece, drawer, *parts):
    for part in parts:
        await DrawerService(db).store_scan(
            drawer_id=drawer.id, piece_id=piece.id, part=part)


# ══════════════════════════════════════════════ bug #18 — the bucket is inferred
@pytest.mark.asyncio
async def test_an_empty_drawer_takes_leather_then_lining_without_being_told(
    db, pieces
):
    piece, drawer = pieces[0]
    svc = DrawerService(db)

    first = await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id)
    assert first["part"] == "LEATHER" and first["part_inferred"] is True

    second = await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id)
    assert second["part"] == "LINING" and second["part_inferred"] is True
    assert second["holding"] == "HOLDING BOTH"


@pytest.mark.asyncio
async def test_a_piece_that_has_had_its_lining_cut_is_read_as_lining(
    db, operations, pieces, lining_cutter, lining_mgr
):
    """The piece's own history is the first and best evidence: it has been
    through the lining cut, so what is arriving in the store is the lining."""
    piece, drawer = pieces[0]
    await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LINING_CUT)

    out = await DrawerService(db).store_scan(drawer_id=drawer.id, piece_id=piece.id)
    assert out["part"] == "LINING"
    assert out["holding"] == "HOLDING LINING"


@pytest.mark.asyncio
async def test_an_explicit_part_still_overrides_the_inference(db, pieces):
    piece, drawer = pieces[0]
    out = await DrawerService(db).store_scan(
        drawer_id=drawer.id, piece_id=piece.id, part=DrawerPart.LINING)
    assert out["part"] == "LINING" and out["part_inferred"] is False


@pytest.mark.asyncio
async def test_a_full_drawer_refuses_a_third_scan(db, pieces):
    piece, drawer = pieces[0]
    svc = DrawerService(db)
    await _fill(db, piece, drawer, DrawerPart.LEATHER, DrawerPart.LINING)
    with pytest.raises(HTTPException) as exc:
        await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id)
    assert exc.value.status_code == 409


# ══════════════════════════════════════════════ bug #13 — completeness auto-receives
@pytest.mark.asyncio
async def test_completeness_advances_to_received_by_itself(db, pieces, dm):
    piece, drawer = pieces[0]
    svc = DrawerService(db)

    half = await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                part=DrawerPart.LEATHER)
    assert half["state"] == DrawerState.HOLDING_LEATHER.value
    assert half["auto_received"] is False

    full = await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                part=DrawerPart.LINING)
    assert full["auto_received"] is True
    assert full["state"] == DrawerState.RECEIVED.value
    # ...but NOT sent. Receiving records contents; sending releases the garment.
    assert full["sent"] is False and full["sent_to"] is None


@pytest.mark.asyncio
async def test_scanning_in_is_not_completion(db, pieces):
    """BUG #15 stated as an assertion: after the last part is scanned, the piece
    is still in the drawer and the response says what has to happen next."""
    piece, drawer = pieces[0]
    await _fill(db, piece, drawer, DrawerPart.LEATHER)
    out = await DrawerService(db).store_scan(
        drawer_id=drawer.id, piece_id=piece.id, part=DrawerPart.LINING)

    assert out["sent"] is False
    assert "Drawers List" in out["next_action"]
    assert await DrawerService(db).is_sended(piece.id) is False


# ══════════════════════════════════════════════ bugs #13/#14 — the batch send
@pytest.mark.asyncio
async def test_many_drawers_go_in_one_action_and_release_their_pieces(
    db, operations, pieces, dm
):
    svc = DrawerService(db)
    chosen = pieces[:3]
    for piece, drawer in chosen:
        await _fill(db, piece, drawer, DrawerPart.LEATHER, DrawerPart.LINING)

    out = await svc.send_batch(drawer_ids=[d.id for _, d in chosen],
                               destination="STITCHING", actor_id=dm.id)

    assert out["count_sent"] == 3 and out["requested"] == 3
    assert out["not_ready"] == [] and out["not_found"] == []
    assert out["pieces_released"] == [p.code for p, _ in chosen]
    assert "released for line-stitching" in out["message"]
    # THE POINT: the merge gate is open for every one of them, in one call.
    for piece, _ in chosen:
        assert await svc.is_sended(piece.id) is True


@pytest.mark.asyncio
async def test_one_unready_drawer_never_loses_the_ready_ones(db, pieces, dm):
    """PARTIAL ACCEPT — the same rule that makes the production gates per-piece."""
    svc = DrawerService(db)
    ready = pieces[0]
    await _fill(db, ready[0], ready[1], DrawerPart.LEATHER, DrawerPart.LINING)
    half_full = pieces[1]
    await _fill(db, half_full[0], half_full[1], DrawerPart.LEATHER)
    untouched = pieces[2]

    out = await svc.send_batch(
        drawer_ids=[ready[1].id, half_full[1].id, untouched[1].id],
        destination="STITCHING", actor_id=dm.id)

    assert out["count_sent"] == 1
    assert out["sent"][0]["drawer_code"] == ready[1].code
    assert {r["drawer_code"] for r in out["not_ready"]} == {
        half_full[1].code, untouched[1].code}
    # every rejection carries an actionable reason
    assert all("RECEIVED" in r["reason"] for r in out["not_ready"])
    assert await svc.is_sended(ready[0].id) is True
    assert await svc.is_sended(half_full[0].id) is False


@pytest.mark.asyncio
async def test_sending_to_lining_records_the_route_without_opening_the_gate(
    db, pieces, dm
):
    """The two destinations are not interchangeable: only STITCHING releases a
    piece into line-stitching."""
    svc = DrawerService(db)
    piece, drawer = pieces[0]
    await _fill(db, piece, drawer, DrawerPart.LEATHER, DrawerPart.LINING)

    out = await svc.send_batch(drawer_ids=[drawer.id], destination="LINING",
                               actor_id=dm.id)
    assert out["count_sent"] == 1
    assert out["sent"][0]["sent_to"] == "LINING"

    detail = await svc.drawer_detail(drawer.id)
    assert detail["sent"] is True and detail["sent_to"] == "LINING"


@pytest.mark.asyncio
async def test_a_resent_drawer_is_reported_not_double_counted(db, pieces, dm):
    svc = DrawerService(db)
    piece, drawer = pieces[0]
    await _fill(db, piece, drawer, DrawerPart.LEATHER, DrawerPart.LINING)
    await svc.send_batch(drawer_ids=[drawer.id], destination="STITCHING",
                         actor_id=dm.id)

    again = await svc.send_batch(drawer_ids=[drawer.id], destination="STITCHING",
                                 actor_id=dm.id)
    assert again["count_sent"] == 0
    assert "already sent" in again["not_ready"][0]["reason"]


@pytest.mark.asyncio
async def test_a_duplicate_selection_is_collapsed(db, pieces, dm):
    """Ticking the same row twice must not send it twice."""
    svc = DrawerService(db)
    piece, drawer = pieces[0]
    await _fill(db, piece, drawer, DrawerPart.LEATHER, DrawerPart.LINING)

    out = await svc.send_batch(drawer_ids=[drawer.id, drawer.id, drawer.id],
                               destination="STITCHING", actor_id=dm.id)
    assert out["requested"] == 1 and out["count_sent"] == 1


@pytest.mark.asyncio
async def test_an_unknown_drawer_is_bucketed_not_fatal(db, pieces, dm):
    import uuid as _uuid
    svc = DrawerService(db)
    piece, drawer = pieces[0]
    await _fill(db, piece, drawer, DrawerPart.LEATHER, DrawerPart.LINING)

    ghost = _uuid.uuid4()
    out = await svc.send_batch(drawer_ids=[drawer.id, ghost],
                               destination="STITCHING", actor_id=dm.id)
    assert out["count_sent"] == 1
    assert out["not_found"] == [str(ghost)]


@pytest.mark.asyncio
async def test_an_unknown_destination_is_422(db, pieces, dm):
    _, drawer = pieces[0]
    with pytest.raises(HTTPException) as exc:
        await DrawerService(db).send_batch(drawer_ids=[drawer.id],
                                           destination="WAREHOUSE", actor_id=dm.id)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_an_empty_selection_is_422(db, dm):
    with pytest.raises(HTTPException) as exc:
        await DrawerService(db).send_batch(drawer_ids=[], destination="STITCHING",
                                           actor_id=dm.id)
    assert exc.value.status_code == 422


# ══════════════════════════════════════════════ bug #13 — the list and the detail
@pytest.mark.asyncio
async def test_the_list_shows_the_garment_and_the_send_queue(db, pieces, dm):
    svc = DrawerService(db)
    ready, other = pieces[0], pieces[1]
    await _fill(db, ready[0], ready[1], DrawerPart.LEATHER, DrawerPart.LINING)
    await _fill(db, other[0], other[1], DrawerPart.LEATHER)

    rows = (await svc.list_labels())["items"]
    by_code = {r["code"]: r for r in rows}
    # the list is choosable because it names the garment, not just the drawer
    assert by_code[ready[1].code]["piece_code"] == ready[0].code
    assert by_code[ready[1].code]["piece_serial"] == "001"
    assert by_code[ready[1].code]["can_send"] is True
    assert by_code[other[1].code]["can_send"] is False

    queue = await svc.list_labels(sendable=True)
    assert [r["code"] for r in queue["items"]] == [ready[1].code]
    assert queue["total"] == 1

    empties = await svc.list_labels(has_piece=False)
    assert empties["total"] == 0        # every drawer in this fixture is merged


@pytest.mark.asyncio
async def test_drawer_detail_reports_what_it_is_waiting_for(db, pieces):
    piece, drawer = pieces[0]
    await _fill(db, piece, drawer, DrawerPart.LEATHER)

    detail = await DrawerService(db).drawer_detail(drawer.id)
    assert detail["code"] == drawer.code
    assert detail["holding"] == "HOLDING LEATHER"
    assert detail["awaiting"] == ["LINING"]
    assert detail["complete"] is False and detail["can_send"] is False
    assert detail["piece"]["code"] == piece.code
    assert detail["piece"]["serial"] == "001"


@pytest.mark.asyncio
async def test_a_recycled_drawer_forgets_where_it_was_sent(
    db, operations, pieces, dm
):
    """`sent_to` is per-garment. A drawer that comes back to WAITING and takes the
    NEXT piece must not still read "sent to stitching"."""
    svc = DrawerService(db)
    piece, drawer = pieces[0]
    await _fill(db, piece, drawer, DrawerPart.LEATHER, DrawerPart.LINING)
    await svc.send_batch(drawer_ids=[drawer.id], destination="STITCHING",
                         actor_id=dm.id)

    await svc.release_nocommit(piece.id)
    await db.commit()
    await db.refresh(drawer)

    assert drawer.state == DrawerState.WAITING.value
    assert drawer.sent_to is None
