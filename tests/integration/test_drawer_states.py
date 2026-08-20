"""
INTEGRATION · the drawer state machine — what the store ACTUALLY holds.

THE RULE THIS FILE DEFENDS
    The drawer state names its PHYSICAL CONTENTS, in whichever order the parts
    arrive:

        leather scanned first   → HOLDING_LEATHER  → (lining) → HOLDING_BOTH
        lining  scanned first   → HOLDING_LINING   → (leather) → HOLDING_BOTH

    COMPLETENESS is a different question, answered by `ready_for_received` /
    `awaiting`. A leather-only piece (needs_lining=False) is ready the moment its
    leather is in — but its drawer holds LEATHER, not "both". Naming that state
    HOLDING_BOTH (as it did) claimed a lining that was never coming, and the
    store screen then captioned it "holding both".

    Then: RECEIVED needs completeness, SENDED needs RECEIVED, line-stitching
    needs SENDED, and PACKAGE_EXPORT recycles the drawer to WAITING.

HOLDING BOTH AUTO-ADVANCES TO RECEIVED.
    The scan that puts the SECOND part in moves the drawer straight to RECEIVED,
    so `state` reads "received" the moment both parts are physically in and never
    rests at HOLDING_BOTH. That is not a loss of information: `holding` still
    reports HOLDING BOTH (contents), and `ready_for_received` still reports
    completeness. The assertions below therefore check the CONTENTS field for
    what is in the drawer and the STATE field for where it is in its lifecycle —
    the distinction this file was written to defend in the first place.

    ONE PART NEVER AUTO-RECEIVES, even for a piece flagged as needing no lining.
    That flag is written once at upload and is wrong on a large slice of live
    data, so a drawer must not advance itself on it; two physical scans are the
    only trigger. A genuinely leather-only drawer is confirmed by a human through
    the manual receive, which still validates completeness.

    SENDED stays manual, and is now plural: see test_drawer_batch_send.py.
"""
import pytest
from fastapi import HTTPException

from app.core.enums import DrawerPart, DrawerState
from app.modules.drawers.service import DrawerService
from app.modules.production.models import Piece

pytestmark = pytest.mark.integrity


async def _scan(db, drawer, piece, part):
    return await DrawerService(db).store_scan(
        drawer_id=drawer.id, piece_id=piece.id, part=part)


# ══════════════════════════════════════════════ the two arrival orders
@pytest.mark.asyncio
async def test_leather_first_then_lining(db, cut_pieces):
    """LEATHER in → HOLDING_LEATHER, still awaiting lining, not receivable."""
    piece, drawer = cut_pieces[0]

    out = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert out["state"] == DrawerState.HOLDING_LEATHER.value
    assert out["awaiting"] == ["LINING"]
    assert out["ready_for_received"] is False
    assert out["drawer_code"] == drawer.code and out["piece_code"] == piece.code

    out = await _scan(db, drawer, piece, DrawerPart.LINING)
    # Contents say BOTH; the lifecycle has already advanced past holding.
    assert out["holding"] == "HOLDING BOTH"
    assert out["state"] == DrawerState.RECEIVED.value
    assert out["auto_received"] is True
    assert out["awaiting"] == []
    assert out["ready_for_received"] is True
    # BUG #15: scanning is not completion. The piece is in the drawer and stays
    # there until someone sends it.
    assert out["sent"] is False
    assert "Send" in out["next_action"]


@pytest.mark.asyncio
async def test_lining_first_then_leather(db, cut_pieces):
    """The mirror image: LINING in → HOLDING_LINING (NOT holding_leather),
    still awaiting leather. Then leather completes it."""
    piece, drawer = cut_pieces[1]

    out = await _scan(db, drawer, piece, DrawerPart.LINING)
    assert out["state"] == DrawerState.HOLDING_LINING.value
    assert out["awaiting"] == ["LEATHER"]
    assert out["ready_for_received"] is False

    out = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert out["holding"] == "HOLDING BOTH"
    assert out["state"] == DrawerState.RECEIVED.value
    assert out["awaiting"] == []
    assert out["ready_for_received"] is True


@pytest.mark.asyncio
async def test_leather_only_piece_holds_leather_and_is_ready(db, pieces, ready_for_store):
    """needs_lining=False: complete on leather alone, but the drawer holds
    LEATHER — never HOLDING BOTH, because no lining exists for this piece.

    AND IT DOES NOT RECEIVE ITSELF. Auto-receive fires on HOLDING_BOTH only —
    two physical scans — not on `complete`, which depends on the needs_lining
    flag. That flag is written once at upload and is known to be wrong on live
    data, so letting it advance a drawer on a single scan meant a drawer
    receiving itself right after pasting with an empty lining side.
    """
    piece, drawer = pieces[2]
    piece.needs_lining = False
    await db.commit()
    # LEATHER SIDE ONLY. A logged LINING_CUTTING event would make this piece
    # lined whatever the flag says (core/lining_rules: a cut lining is a physical
    # fact that outranks the paperwork), so a leather-only test must never have
    # one.
    await ready_for_store(piece, lining=False)

    out = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert out["holding"] == "HOLDING LEATHER"
    assert out["state"] == DrawerState.HOLDING_LEATHER.value
    assert out["auto_received"] is False
    assert out["needs_lining"] is False
    assert out["awaiting"] == []              # nothing else is coming
    assert out["ready_for_received"] is True  # complete despite holding one part
    # Ready but not received — so the operator is told what closes the gap
    # instead of being left looking at a drawer that seems stuck.
    assert "Confirm receipt" in out["next_action"]


@pytest.mark.asyncio
async def test_a_leather_only_drawer_is_still_receivable_by_hand(db, pieces, ready_for_store):
    """The other half of the rule above: nothing is stranded. A human confirms
    it, and the manual route still validates completeness, so it accepts exactly
    this case and nothing weaker."""
    piece, drawer = pieces[2]
    piece.needs_lining = False
    await db.commit()
    await ready_for_store(piece, lining=False)   # leather side only — see above
    await _scan(db, drawer, piece, DrawerPart.LEATHER)

    svc = DrawerService(db)
    out = await svc.transition(drawer.id, "RECEIVED", actor_id=None)
    assert out["state"] == DrawerState.RECEIVED.value
    # ...and from there the normal batch send works.
    sent = await svc.send_batch(drawer_ids=[drawer.id], actor_id=None)
    assert sent["count_sent"] == 1


@pytest.mark.asyncio
async def test_only_both_parts_trigger_the_automatic_receive(db, cut_pieces):
    """The rule, stated directly: one part never auto-receives, whatever the
    needs_lining flag says; two parts always do."""
    lined_piece, lined_drawer = cut_pieces[0]
    only_piece, only_drawer = cut_pieces[1]
    only_piece.needs_lining = False
    await db.commit()

    # one part, flag says lining is coming   → not received
    a = await _scan(db, lined_drawer, lined_piece, DrawerPart.LEATHER)
    assert a["auto_received"] is False
    # one part, flag says nothing is coming  → STILL not received
    b = await _scan(db, only_drawer, only_piece, DrawerPart.LEATHER)
    assert b["auto_received"] is False
    assert b["state"] == DrawerState.HOLDING_LEATHER.value
    # the second part lands                  → received, by itself
    c = await _scan(db, lined_drawer, lined_piece, DrawerPart.LINING)
    assert c["auto_received"] is True
    assert c["state"] == DrawerState.RECEIVED.value
    assert c["holding"] == "HOLDING BOTH"


@pytest.mark.asyncio
async def test_rescanning_the_same_part_is_idempotent(db, cut_pieces):
    piece, drawer = cut_pieces[3]
    first = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    again = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert again["state"] == first["state"] == DrawerState.HOLDING_LEATHER.value
    assert again["awaiting"] == ["LINING"]


# ══════════════════════════════════════════════ the merge map is the authority
@pytest.mark.asyncio
async def test_a_piece_scanned_into_the_wrong_drawer_is_409(db, cut_pieces):
    piece, _ = cut_pieces[0]
    _, other_drawer = cut_pieces[1]
    with pytest.raises(HTTPException) as exc:
        await _scan(db, other_drawer, piece, DrawerPart.LEATHER)
    assert exc.value.status_code == 409
    assert "not merged to drawer" in str(exc.value.detail)


# ══════════════════════════════════════════════ RECEIVED / SENDED
@pytest.mark.asyncio
async def test_received_requires_completeness(db, cut_pieces):
    piece, drawer = cut_pieces[0]
    await _scan(db, drawer, piece, DrawerPart.LEATHER)      # lining still missing

    with pytest.raises(HTTPException) as exc:
        await DrawerService(db).transition(drawer.id, "RECEIVED", actor_id=None)
    assert exc.value.status_code == 409
    assert "awaiting lining" in str(exc.value.detail)

    await _scan(db, drawer, piece, DrawerPart.LINING)
    out = await DrawerService(db).transition(drawer.id, "RECEIVED", actor_id=None)
    assert out["state"] == DrawerState.RECEIVED.value


@pytest.mark.asyncio
async def test_sended_requires_received_and_cannot_go_backwards(db, cut_pieces):
    piece, drawer = cut_pieces[0]
    svc = DrawerService(db)

    with pytest.raises(HTTPException) as exc:
        await svc.transition(drawer.id, "SENDED", actor_id=None)
    assert exc.value.status_code == 409          # not RECEIVED yet

    await _scan(db, drawer, piece, DrawerPart.LEATHER)
    await _scan(db, drawer, piece, DrawerPart.LINING)
    await svc.transition(drawer.id, "RECEIVED", actor_id=None)
    assert (await svc.transition(drawer.id, "SENDED", actor_id=None))["state"] \
        == DrawerState.SENDED.value

    # a SENDED drawer never walks back to RECEIVED...
    with pytest.raises(HTTPException) as exc:
        await svc.transition(drawer.id, "RECEIVED", actor_id=None)
    assert exc.value.status_code == 409
    # ...nor accepts another part scan (that would revoke a merge gate already passed)
    with pytest.raises(HTTPException) as exc:
        await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_an_unknown_transition_is_422(db, cut_pieces):
    _, drawer = cut_pieces[0]
    with pytest.raises(HTTPException) as exc:
        await DrawerService(db).transition(drawer.id, "SHIPPED", actor_id=None)
    assert exc.value.status_code == 422


# ══════════════════════════════════════════════ the gate + the recycle
@pytest.mark.asyncio
async def test_is_sended_is_the_merge_gate_answer(db, cut_pieces):
    piece, drawer = cut_pieces[0]
    svc = DrawerService(db)
    assert await svc.is_sended(piece.id) is False

    await _scan(db, drawer, piece, DrawerPart.LEATHER)
    await _scan(db, drawer, piece, DrawerPart.LINING)
    assert await svc.is_sended(piece.id) is False        # holding both != released
    await svc.transition(drawer.id, "RECEIVED", actor_id=None)
    assert await svc.is_sended(piece.id) is False        # received != released
    await svc.transition(drawer.id, "SENDED", actor_id=None)
    assert await svc.is_sended(piece.id) is True


@pytest.mark.asyncio
async def test_release_recycles_the_drawer_and_clears_both_sides(db, cut_pieces):
    piece, drawer = cut_pieces[0]
    svc = DrawerService(db)
    await _scan(db, drawer, piece, DrawerPart.LEATHER)
    await _scan(db, drawer, piece, DrawerPart.LINING)
    await svc.transition(drawer.id, "RECEIVED", actor_id=None)
    await svc.transition(drawer.id, "SENDED", actor_id=None)

    await svc.release_nocommit(piece.id)
    await db.commit()
    await db.refresh(drawer)

    assert drawer.state == DrawerState.WAITING.value
    assert drawer.current_piece_id is None
    assert drawer.leather_in is False and drawer.lining_in is False
    assert drawer.received_at is None and drawer.sended_at is None
    # F11: BOTH sides of the link are cleared, or the piece payload and the
    # drawer would disagree about who holds what.
    assert (await db.get(Piece, piece.id)).drawer_id is None


@pytest.mark.asyncio
async def test_the_label_sheet_lists_drawers_with_their_barcodes(db, cut_pieces):
    out = await DrawerService(db).list_labels()
    assert out["total"] == 5 and out["count"] == 5
    first = out["items"][0]
    assert first["seq"] == 1
    assert first["barcode"] == first["code"]        # registry code == drawer code
    assert first["state"] == DrawerState.MERGED.value

    page = await DrawerService(db).list_labels(seq_from=2, seq_to=3)
    assert [i["seq"] for i in page["items"]] == [2, 3]
    assert page["total"] == 2


@pytest.mark.asyncio
async def test_the_label_sheet_reports_what_each_drawer_holds(db, cut_pieces):
    """`holding` names the CONTENTS; `state` names the LIFECYCLE position.

    The two answer different questions and stop agreeing at RECEIVED — which is
    exactly why contents get their own field instead of being read off `state`.
    """
    svc = DrawerService(db)
    piece, drawer = cut_pieces[0]

    async def holding_of(code: str) -> str:
        out = await svc.list_labels()
        return next(i["holding"] for i in out["items"] if i["code"] == code)

    # Nothing scanned in yet: merged to a piece, but physically empty.
    assert await holding_of(drawer.code) == "EMPTY"

    await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                         part=DrawerPart.LEATHER)
    assert await holding_of(drawer.code) == "HOLDING LEATHER"

    await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                         part=DrawerPart.LINING)
    assert await holding_of(drawer.code) == "HOLDING BOTH"

    # THE POINT: once the DM receives and sends, `state` reads received/sended and
    # no longer says what is inside. `holding` still does.
    for transition in ("RECEIVED", "SENDED"):
        await svc.transition(drawer_id=drawer.id, transition=transition,
                             actor_id=None)
    out = await svc.list_labels()
    row = next(i for i in out["items"] if i["code"] == drawer.code)
    assert row["state"] == DrawerState.SENDED.value
    assert row["holding"] == "HOLDING BOTH"

    # A lining-first scan must read HOLDING LINING, not HOLDING LEATHER.
    other_piece, other = cut_pieces[1]
    await svc.store_scan(drawer_id=other.id, piece_id=other_piece.id,
                         part=DrawerPart.LINING)
    assert await holding_of(other.code) == "HOLDING LINING"
