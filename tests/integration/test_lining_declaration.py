"""
================================================================================
tests/integration/test_lining_declaration.py
    THE DM's RELEASE-TIME LINING DECLARATION, and the HOLDING_BOTH merge gate.
================================================================================

WHAT CHANGED, AND WHY IT NEEDS ITS OWN REGRESSION FILE

    Until now "does this garment take a lining?" was INFERRED — from the style
    name (KNIT / WOOL / FUR), from the SKU's lining colours, from a per-piece
    flag written once at upload. Inference is one-directional by design: every
    signal can only ADD a lining requirement, never remove one, because a stale
    False is what let a KNIT jacket run to PACKAGE_EXPORT unlined
    (test_lining_gate_regression.py).

    The release gate now ASKS. `Style.needs_lining` holds the DM's answer, and
    that answer decides in BOTH directions — it is the only term that can switch
    a lining requirement OFF. That is a deliberate loosening of the rule above,
    so it needs pinning from both sides:

      • it must actually work in the negative direction (a style named KNIT can
        be released leather-only), or the question is decorative; and
      • it must not become a way to lose a lining that physically exists — a
        logged LINING_CUTTING event still outranks a declared False.

THE SECOND HALF OF THIS FILE is the merge gate. "Only after HOLDING BOTH may a
piece move to line-stitching" — enforced at the gate itself, not merely inherited
from the store's send check, because DrawerState.SENDED is reachable by more than
one route (transition(), the deprecated single-drawer endpoint, a repair script)
and every one of those was a door into LINE_STITCHING on an unlined garment.

NULL IS NOT FALSE. Every style released before this column existed reads NULL and
must keep exactly the verdict it has today. That is asserted too — a migration
that quietly declared 1,400 live pieces leather-only would be the original bug
arriving by a different route.
================================================================================
"""
import pytest

from app.core.enums import DrawerState, ProductionStage
from app.core.lining_rules import lining_required
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.drawers.service import DrawerService
from app.modules.production.models import Piece

pytestmark = pytest.mark.asyncio


async def _tree(db, *, style_name: str, declared: bool | None, tag: str,
                stored_flag: bool = False, knit_color: str | None = None):
    """client → order → style(needs_lining=declared) → sku → piece → drawer.

    `declared` is the variable under test: None models a style released before
    the question existed, True/False the DM's explicit answer. `stored_flag` is
    the per-piece copy, deliberately settable to the WRONG value so the tests can
    prove which of the two the gate actually reads.
    """
    from app.modules.barcode.models import Drawer

    client = Client(name=f"C-{tag}", country="IT")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number=f"ORD-{tag}")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name=style_name, article=f"ART-{tag}",
                  code=f"STY-{tag}", production_status="RELEASED")
    style.needs_lining = declared
    db.add(style)
    await db.flush()
    sku = SKU(style_id=style.id, color_code="BLK", color_name="BLACK", size="M",
              qty_ordered=1, code=f"SKU-{tag}", knit_color=knit_color)
    db.add(sku)
    await db.flush()

    drawer = Drawer(code=f"DRW-8{tag[-3:]}", seq=8000 + abs(hash(tag)) % 900,
                    state=DrawerState.MERGED.value)
    db.add(drawer)
    await db.flush()
    piece = Piece(code=f"{sku.code}-001", seq=1, sku_id=sku.id,
                  current_operation_id=None)
    piece.needs_lining = stored_flag
    piece.drawer_id = drawer.id
    db.add(piece)
    await db.flush()
    drawer.current_piece_id = piece.id
    await db.commit()
    await db.refresh(piece)
    await db.refresh(drawer)
    return order, style, sku, piece, drawer


# ══════════════════════════════════════════════════════════════════════════════
# 1. THE DECLARATION DECIDES — IN BOTH DIRECTIONS
# ══════════════════════════════════════════════════════════════════════════════
async def test_declared_no_lining_beats_a_knit_style_name(db):
    """A style named KNIT, declared leather-only by the DM, sends on leather alone.

    THE NEGATIVE DIRECTION — the one inference cannot express. Without it a DM
    looking at a wool-shell garment with no lining could never release it, and
    the release question would be unable to say no.
    """
    _, _, _, piece, drawer = await _tree(
        db, style_name="ADELE KNIT", declared=False, tag="d001")

    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    svc = DrawerService(db)
    needs, _why = await svc._needs_lining(piece)
    assert needs is False, "the DM's explicit 'no' lost to the style name"

    result = await svc.send_batch(drawer_ids=[drawer.id], actor_id=None)
    assert result["count_sent"] == 1, result["not_ready"]


async def test_declared_lining_beats_a_plain_style_name(db):
    """A plainly-named style declared LINED must not send on leather alone.

    The positive direction. Inference would have said no lining here — the name
    carries no marker and the SKU no lining colour — so this is the DM adding a
    requirement the system could not have known about.
    """
    _, _, _, piece, drawer = await _tree(
        db, style_name="CLERMONT", declared=True, tag="d002")

    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    svc = DrawerService(db)
    needs, why = await svc._needs_lining(piece)
    assert needs is True
    assert why and "Direct Manager" in why

    result = await svc.send_batch(drawer_ids=[drawer.id], actor_id=None)
    assert result["count_sent"] == 0, "a declared-lined garment sent without lining"
    assert "lining" in result["not_ready"][0]["reason"].lower()


async def test_an_unanswered_style_still_falls_back_to_inference(db):
    """NULL means 'never asked', NOT 'no'.

    THE MIGRATION GUARD. Every style released before this column existed reads
    NULL, and must keep the verdict it had — otherwise adding the column silently
    declares the entire live database leather-only, which is the lining-bypass
    bug reintroduced wholesale.
    """
    _, _, _, piece, drawer = await _tree(
        db, style_name="REESE WOOL", declared=None, tag="d003")

    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    svc = DrawerService(db)
    needs, why = await svc._needs_lining(piece)
    assert needs is True, "an unanswered style stopped requiring its lining"
    assert why and "WOOL" in why

    result = await svc.send_batch(drawer_ids=[drawer.id], actor_id=None)
    assert result["count_sent"] == 0


async def test_a_logged_lining_cut_outranks_a_declared_no(db, operations):
    """Somebody physically cut a lining. It has to be merged, whatever was declared.

    THE SAFETY VALVE ON THE NEGATIVE DIRECTION. A declared False is a statement
    about a garment; a LINING_CUTTING event is a fact about one. If the fact lost,
    a real cut lining would sit in a drawer nobody was waiting for, and the
    garment would ship without it.
    """
    from app.modules.production.models import Operation, ProductionEvent
    from app.modules.employees.models import Employee
    from datetime import date

    _, _, sku, piece, drawer = await _tree(
        db, style_name="CLERMONT", declared=False, tag="d004")

    emp = Employee(name="LiningCutter-d004", designation="LINING_CUTTER")
    db.add(emp)
    await db.flush()
    op = await db.scalar(
        __import__("sqlalchemy").select(Operation).where(
            Operation.code == ProductionStage.LINING_CUTTING.value))
    db.add(ProductionEvent(sku_id=sku.id, operation_id=op.id, employee_id=emp.id,
                           work_date=date.today(), qty=1, piece_id=piece.id))
    await db.commit()

    needs, why = await DrawerService(db)._needs_lining(piece)
    assert needs is True, "a physically-cut lining was cancelled by paperwork"
    assert why and "lining cut" in why.lower()


async def test_the_pure_rule_matches_the_gate(db):
    """The pure predicate and the DB-backed gate must agree. One rule, one answer."""
    assert lining_required(style_name="ADELE KNIT", explicit=False) is False
    assert lining_required(style_name="CLERMONT", explicit=True) is True
    assert lining_required(style_name="ADELE KNIT", explicit=None) is True
    # The fact beats the declaration, in the pure rule too.
    assert lining_required(style_name="X", explicit=False,
                           has_lining_cut_event=True) is True


# ══════════════════════════════════════════════════════════════════════════════
# 2. THE MERGE GATE — HOLDING BOTH BEFORE LINE-STITCHING
# ══════════════════════════════════════════════════════════════════════════════
async def test_merge_gate_rejects_a_sended_drawer_that_never_held_the_lining(db):
    """THE HOLE THIS CLOSES.

    The gate used to accept `state == SENDED` as proof of completeness, on the
    reasoning that only send_batch can set it and send_batch checks completeness.
    That reasoning fails the moment SENDED is reachable another way — and it is
    (transition(), the deprecated single-drawer route, any repair script). Here
    the drawer is forced to SENDED with an empty lining side, exactly as one of
    those routes would leave it, and the gate must still refuse.
    """
    _, _, _, piece, drawer = await _tree(
        db, style_name="CLERMONT", declared=True, tag="m001")

    # Forced past the store without the lining ever arriving.
    drawer.leather_in = True
    drawer.lining_in = False
    drawer.state = DrawerState.SENDED.value
    await db.commit()

    from app.modules.production.service import ProductionService
    ok, why = await ProductionService(db)._merge_ok(
        piece, ProductionStage.LINE_STITCHING)

    assert ok is False, "an unlined garment entered LINE_STITCHING via a forced SENDED"
    assert drawer.code in why
    assert "lining" in why.lower()


async def test_merge_gate_opens_on_holding_both_and_sended(db):
    """The positive path: both parts in AND the store has released it."""
    _, _, _, piece, drawer = await _tree(
        db, style_name="CLERMONT", declared=True, tag="m002")

    drawer.leather_in = True
    drawer.lining_in = True
    drawer.state = DrawerState.SENDED.value
    await db.commit()

    from app.modules.production.service import ProductionService
    ok, why = await ProductionService(db)._merge_ok(
        piece, ProductionStage.LINE_STITCHING)
    assert ok is True, why


async def test_merge_gate_still_requires_the_send_after_holding_both(db):
    """Holding both is necessary, not sufficient — the store still has to send.

    The two conditions are separate on purpose. A drawer that is complete but
    unsent is work the store has not released yet, and letting the line pull from
    it would make the send button decorative.
    """
    _, _, _, piece, drawer = await _tree(
        db, style_name="CLERMONT", declared=True, tag="m003")

    drawer.leather_in = True
    drawer.lining_in = True
    drawer.state = DrawerState.HOLDING_BOTH.value
    await db.commit()

    from app.modules.production.service import ProductionService
    ok, why = await ProductionService(db)._merge_ok(
        piece, ProductionStage.LINE_STITCHING)
    assert ok is False
    assert "has not been sent" in why


async def test_a_declared_leather_only_piece_clears_the_gate_on_leather(db):
    """The false-positive guard, and the reason HOLDING_BOTH cannot be universal.

    A garment declared leather-only can NEVER reach HOLDING_BOTH — there is no
    second part coming. Requiring it literally would strand every unlined drawer
    forever, which is a worse failure than the one being fixed: it stops the
    factory rather than leaking one garment.
    """
    _, _, _, piece, drawer = await _tree(
        db, style_name="ADELE KNIT", declared=False, tag="m004")

    drawer.leather_in = True
    drawer.lining_in = False
    drawer.state = DrawerState.SENDED.value
    await db.commit()

    from app.modules.production.service import ProductionService
    ok, why = await ProductionService(db)._merge_ok(
        piece, ProductionStage.LINE_STITCHING)
    assert ok is True, why


async def test_a_piece_with_no_drawer_is_refused_and_told_why(db):
    """A piece minted while the drawer pool was full has nowhere to be stored.

    It is not broken and not lost — it has a barcode — but it cannot pass a gate
    whose whole subject is what is in its drawer. The message has to say that
    rather than naming a drawer that does not exist.
    """
    _, _, _, piece, drawer = await _tree(
        db, style_name="CLERMONT", declared=False, tag="m005")
    drawer.current_piece_id = None
    piece.drawer_id = None
    await db.commit()

    from app.modules.production.service import ProductionService
    ok, why = await ProductionService(db)._merge_ok(
        piece, ProductionStage.LINE_STITCHING)
    assert ok is False
    assert "no drawer" in why.lower()
