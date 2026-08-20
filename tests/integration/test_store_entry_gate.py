"""
INTEGRATION · the STORE-ENTRY gate — a part may not be stored before its cut
path is finished.

THE RULE THIS FILE DEFENDS

    LEATHER_CUTTING → FUSING → PASTING ─┐
                                         ├─► STORE ─► LINE_STITCHING → …
    LINING_CUTTING ──────────────────────┘

    The store is where the two cut PATHS MEET. It is not a stop part-way along
    either of them, so:

        leather may enter a drawer only after PASTING
        lining  may enter a drawer only after LINING_CUTTING

WHY IT HAD TO BE ENFORCED AT THE WRITE
    core/store_display.py has always said this — `_CUT_SIDE_TERMINALS` is the set
    of stages after which a piece READS as "in store". But store_scan checked only
    the merge map and the drawer's lifecycle state, never the piece's own
    progress. So a piece minted an hour earlier, with no production events at all,
    could be scanned into its drawer; the drawer then read HOLDING LEATHER over an
    empty slot, and nothing downstream could tell that apart from a real one. The
    second such scan auto-RECEIVED the drawer, the DM sent it, and the merge gate
    opened LINE_STITCHING for a garment that had never been pasted.

    The read overlay and the write gate now share one constant
    (core/store_display.STORE_ENTRY_STAGE), so they cannot drift again.

WHAT IS DELIBERATELY *NOT* GATED HERE
    The merge map still wins: a piece scanned into the wrong drawer is a 409
    about the drawer, not about its stages — the operator holding the wrong
    drawer needs to be told that first.
"""
import pytest
from fastapi import HTTPException

from app.core.enums import DrawerPart, DrawerState
from app.modules.drawers.service import DrawerService

pytestmark = pytest.mark.integrity


async def _scan(db, drawer, piece, part=None):
    return await DrawerService(db).store_scan(
        drawer_id=drawer.id, piece_id=piece.id, part=part)


# ══════════════════════════════════════════════ the gate fires
@pytest.mark.asyncio
async def test_an_uncut_piece_cannot_be_stored_at_all(db, pieces):
    """The state breakdown upload leaves behind: barcoded, merged to a drawer,
    and nothing else. Its drawer must stay empty."""
    piece, drawer = pieces[0]

    with pytest.raises(HTTPException) as exc:
        await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert exc.value.status_code == 409
    assert "PASTING" in exc.value.detail

    await db.refresh(drawer)
    assert drawer.leather_in is False
    assert drawer.state == DrawerState.MERGED.value, (
        "a rejected scan must leave the drawer exactly as it found it")


@pytest.mark.asyncio
async def test_leather_that_is_cut_but_not_pasted_is_refused(
    db, pieces, operations, cutter, ready_for_store
):
    """THE CASE THAT MOTIVATED THE GATE. Cut is not stored — there are two more
    stages on the leather side before the drawer is where the piece belongs."""
    piece, drawer = pieces[0]
    # Only the lining side is finished, so the leather is still on the floor.
    await ready_for_store(piece, leather=False)

    with pytest.raises(HTTPException) as exc:
        await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert exc.value.status_code == 409
    assert "PASTING" in exc.value.detail
    # The message has to be actionable: name what the piece HAS done.
    assert "LINING_CUTTING" in exc.value.detail


@pytest.mark.asyncio
async def test_lining_is_refused_until_its_own_cut_is_logged(
    db, pieces, ready_for_store
):
    """The leather being finished says nothing about the lining — that is what
    'parallel' means. A pasted piece may store its leather and NOT its lining."""
    piece, drawer = pieces[0]
    await ready_for_store(piece, lining=False)

    ok = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert ok["state"] == DrawerState.HOLDING_LEATHER.value

    with pytest.raises(HTTPException) as exc:
        await _scan(db, drawer, piece, DrawerPart.LINING)
    assert exc.value.status_code == 409
    assert "LINING_CUTTING" in exc.value.detail


@pytest.mark.asyncio
async def test_naming_the_part_does_not_skip_the_gate(db, pieces):
    """A gate a caller can walk around by naming the bucket is not a gate — and
    the explicit path is the one the barcode screen posts on."""
    piece, drawer = pieces[0]

    for part in (DrawerPart.LEATHER, DrawerPart.LINING):
        with pytest.raises(HTTPException) as exc:
            await _scan(db, drawer, piece, part)
        assert exc.value.status_code == 409


# ══════════════════════════════════════════════ the gate opens
@pytest.mark.asyncio
async def test_a_pasted_piece_stores_its_leather(db, pieces, ready_for_store):
    piece, drawer = pieces[0]
    await ready_for_store(piece, lining=False)

    out = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert out["state"] == DrawerState.HOLDING_LEATHER.value
    assert out["holding"] == "HOLDING LEATHER"


@pytest.mark.asyncio
async def test_both_paths_finished_stores_both_parts(db, cut_pieces):
    piece, drawer = cut_pieces[0]

    first = await _scan(db, drawer, piece)
    second = await _scan(db, drawer, piece)
    assert {first["part"], second["part"]} == {"LEATHER", "LINING"}
    assert second["holding"] == "HOLDING BOTH"
    assert second["state"] == DrawerState.RECEIVED.value


@pytest.mark.asyncio
async def test_a_leather_only_garment_is_gated_on_pasting_too(
    db, pieces, ready_for_store
):
    """needs_lining=False removes the OTHER path, not this one's stages. An
    unlined garment still reaches its drawer at PASTING and no earlier."""
    piece, drawer = pieces[0]
    piece.needs_lining = False
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert exc.value.status_code == 409

    # No lining cut for this one: a cut lining is a physical fact that outranks
    # needs_lining=False (core/lining_rules), and would re-line the garment.
    await ready_for_store(piece, lining=False)
    out = await _scan(db, drawer, piece, DrawerPart.LEATHER)
    assert out["ready_for_received"] is True     # complete on leather alone


# ══════════════════════════════════════════════ gate ORDER
@pytest.mark.asyncio
async def test_the_wrong_drawer_is_reported_before_the_missing_stage(db, pieces):
    """Both are wrong; the operator is told about the DRAWER in their hand first.

    A message about PASTING would send them to the floor to check a garment when
    the actual problem is that they are standing at the wrong slot.
    """
    piece_a, _ = pieces[0]
    _, drawer_b = pieces[1]

    with pytest.raises(HTTPException) as exc:
        await _scan(db, drawer_b, piece_a, DrawerPart.LEATHER)
    assert exc.value.status_code == 409
    assert "not merged" in exc.value.detail.lower()
    assert "PASTING" not in exc.value.detail
