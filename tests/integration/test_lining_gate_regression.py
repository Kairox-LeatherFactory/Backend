"""
================================================================================
tests/integration/test_lining_gate_regression.py
    THE LINING BYPASS — a piece with no lining stage reached STORE and ran all
    the way to PACKAGE_EXPORT (change-list item 10, flagged CRITICAL).
================================================================================

THE BUG, PRECISELY
    `piece.needs_lining` is written ONCE at breakdown upload and never
    recomputed. On the live database it is wrong for most of an order (925 of
    1,425 pieces in one order disagree with the current rules — see
    scripts/backfill_needs_lining.py). The completeness gate read that frozen
    flag as gospel:

        complete = leather_in and (lining_in or not needs_lining)

    A KNIT jacket whose stored flag said False was therefore "complete" on its
    leather alone. It received, it sent, `_merge_ok` saw DrawerState.SENDED and
    opened, and the garment walked the whole chain to PACKAGE_EXPORT having never
    had a lining cut.

WHAT THESE TESTS PIN
    1. A garment whose STYLE NAME says KNIT cannot be sent on leather alone, even
       when the stored flag says it needs no lining.  ← the actual bug
    2. …and cannot be RECEIVED either, with a reason that names WHY.
    3. Once the lining is physically scanned in, it sends normally.
    4. A GENUINELY leather-only garment (no flag, no marker, no lining colour,
       no lining cut) still sends on its leather alone. This is the
       false-positive guard: a fix that blocked everything would also "pass"
       tests 1-3, and would stop the whole factory.
    5. The merge gate downstream stays shut for the blocked piece, so the bypass
       is closed end to end and not just at the store screen.

WHY INTEGRATION AND NOT UNIT
    The pure rule is unit-tested in tests/unit/test_lining_rules.py. What broke
    here was not the rule — there was no rule — it was the GATE trusting one
    column. So these exercise the real DrawerService against a real session.
================================================================================
"""
import pytest

from app.core.enums import DrawerState, ProductionStage
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.drawers.service import DrawerService
from app.modules.production.models import Piece

pytestmark = pytest.mark.asyncio


async def _order_with_style(db, *, style_name: str, article: str = "A1",
                            knit_color: str | None = None):
    """A client → order → style → sku tree with a controllable style name.

    The style NAME is the variable under test: it is the only lining signal the
    real order sheet carries (8 of its 17 styles are named ADELE KNIT,
    REESE WOOL, FLAVIO KNIT + FUR DETACH …), and it is the signal the stored
    flag was missing.
    """
    client = Client(name=f"C-{style_name}", country="IT")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number=f"ORD-{style_name[:6]}")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name=style_name, article=article)
    db.add(style)
    await db.flush()
    sku = SKU(style_id=style.id, color_code="BLK", color_name="BLACK", size="M",
              qty_ordered=1, code=f"SKU-{style_name[:6]}-BLK-M",
              knit_color=knit_color)
    db.add(sku)
    await db.commit()
    return order, style, sku


async def _piece_in_drawer(db, sku, *, seq: int, stored_flag: bool):
    """One garment merged into one drawer, with the stored flag we choose.

    `stored_flag=False` on a lined style is the exact corrupt state the live
    database is in — that is the whole point of the fixture.
    """
    from app.modules.barcode.models import Drawer

    drawer = Drawer(code=f"DRW-9{seq:03d}", seq=900 + seq,
                    state=DrawerState.MERGED.value)
    db.add(drawer)
    await db.flush()
    piece = Piece(code=f"{sku.code}-{seq:03d}", seq=seq, sku_id=sku.id,
                  current_operation_id=None)
    piece.needs_lining = stored_flag
    piece.drawer_id = drawer.id
    db.add(piece)
    await db.flush()
    drawer.current_piece_id = piece.id
    await db.commit()
    await db.refresh(piece)
    await db.refresh(drawer)
    return piece, drawer


# ══════════════════════════════════════════════════════════════════════════════
# 1. THE BUG ITSELF
# ══════════════════════════════════════════════════════════════════════════════
async def test_knit_style_cannot_send_on_leather_alone_despite_false_flag(db):
    """A KNIT jacket flagged needs_lining=False must NOT be sendable.

    This is the regression. Before the fix, `complete` was True here, the drawer
    went SENDED, and the piece was free to run to PACKAGE_EXPORT.
    """
    _, _, sku = await _order_with_style(db, style_name="ADELE KNIT")
    piece, drawer = await _piece_in_drawer(db, sku, seq=1, stored_flag=False)

    drawer.leather_in = True
    drawer.lining_in = False
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    svc = DrawerService(db)
    result = await svc.send_batch(drawer_ids=[drawer.id], actor_id=None)

    assert result["count_sent"] == 0, "a KNIT garment was sent with no lining"
    assert result["not_ready"], "the drawer was neither sent nor explained"
    reason = result["not_ready"][0]["reason"]
    assert "lining" in reason.lower()
    # The rejection must say WHY, or an operator holding a garment the sheet
    # says needs no lining reads it as a system fault.
    assert "KNIT" in reason, f"reason does not name the evidence: {reason}"

    await db.refresh(drawer)
    assert drawer.state != DrawerState.SENDED.value


async def test_knit_style_cannot_be_received_on_leather_alone(db):
    """The same block one step earlier, at the RECEIVED transition."""
    from fastapi import HTTPException

    _, _, sku = await _order_with_style(db, style_name="REESE WOOL")
    piece, drawer = await _piece_in_drawer(db, sku, seq=2, stored_flag=False)
    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    with pytest.raises(HTTPException) as exc:
        await DrawerService(db).transition(drawer.id, "RECEIVED", actor_id=None)
    assert exc.value.status_code == 409
    assert "lining" in str(exc.value.detail).lower()


async def test_lining_scanned_in_unblocks_the_send(db):
    """The gate is a gate, not a wall: put the lining in and it opens."""
    _, _, sku = await _order_with_style(db, style_name="FRANCIS KNIT")
    piece, drawer = await _piece_in_drawer(db, sku, seq=3, stored_flag=False)
    drawer.leather_in = True
    drawer.lining_in = True
    drawer.state = DrawerState.HOLDING_BOTH.value
    await db.commit()

    result = await DrawerService(db).send_batch(drawer_ids=[drawer.id],
                                                actor_id=None)
    assert result["count_sent"] == 1, result
    await db.refresh(drawer)
    assert drawer.state == DrawerState.SENDED.value


# ══════════════════════════════════════════════════════════════════════════════
# 2. THE FALSE-POSITIVE GUARD — the test that stops the fix breaking the factory
# ══════════════════════════════════════════════════════════════════════════════
async def test_genuinely_leather_only_garment_still_sends_on_leather_alone(db):
    """No flag, no marker in the name, no lining colour, no lining cut.

    A fix that simply required lining_in everywhere would pass all three tests
    above and strand every leather-only garment in the store — which is the
    failure mode send_batch's completeness rule was written to avoid in the
    first place. This is the test that would catch it.
    """
    _, _, sku = await _order_with_style(db, style_name="CLERMONT", article="CL1")
    piece, drawer = await _piece_in_drawer(db, sku, seq=4, stored_flag=False)
    drawer.leather_in = True
    drawer.lining_in = False
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    result = await DrawerService(db).send_batch(drawer_ids=[drawer.id],
                                                actor_id=None)
    assert result["count_sent"] == 1, (
        "a genuinely leather-only garment was stranded: " + str(result))


async def test_lining_colour_on_the_sku_also_requires_a_lining(db):
    """Source 1 of the rule: an explicit lining colour on the SKU."""
    _, _, sku = await _order_with_style(db, style_name="CLERMONT",
                                        knit_color="ECRU")
    piece, drawer = await _piece_in_drawer(db, sku, seq=5, stored_flag=False)
    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    result = await DrawerService(db).send_batch(drawer_ids=[drawer.id],
                                                actor_id=None)
    assert result["count_sent"] == 0
    assert "lining colour" in result["not_ready"][0]["reason"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# 3. END TO END — the merge gate stays shut, which is what the bug walked through
# ══════════════════════════════════════════════════════════════════════════════
async def test_merge_gate_stays_shut_for_the_blocked_piece(db, operations):
    """The store block is only worth having if production honours it.

    `_merge_ok` reads DrawerState.SENDED. Since the blocked drawer never reaches
    SENDED, LINE_STITCHING must remain closed — and with it every stage after it
    on the chain, which is how the garment used to reach PACKAGE_EXPORT.
    """
    from app.modules.production.service import ProductionService

    _, _, sku = await _order_with_style(db, style_name="SHINOBI KNIT")
    piece, drawer = await _piece_in_drawer(db, sku, seq=6, stored_flag=False)
    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    ok, why = await ProductionService(db)._merge_ok(
        piece, ProductionStage.LINE_STITCHING)
    assert ok is False
    assert drawer.code in (why or ""), why


async def test_list_and_send_agree_about_the_same_drawer(db):
    """The send queue must never offer a row the server then refuses.

    `sendable=true` filters in SQL; send_batch decides in Python. If those two
    ever disagree the operator ticks a box and gets a rejection, which is how a
    store screen loses the floor's trust. Both must exclude the KNIT garment.
    """
    _, _, sku = await _order_with_style(db, style_name="FLAVIO KNIT")
    piece, drawer = await _piece_in_drawer(db, sku, seq=7, stored_flag=False)
    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    svc = DrawerService(db)
    queue = await svc.list_labels(sendable=True, limit=500)
    assert drawer.id not in {row["drawer_id"] for row in queue["items"]}

    row = next((r for r in (await svc.list_labels(code=drawer.code))["items"]
                if r["drawer_id"] == drawer.id), None)
    assert row is not None, "search-by-code did not find the drawer"
    assert row["needs_lining"] is True
    assert row["can_send"] is False
