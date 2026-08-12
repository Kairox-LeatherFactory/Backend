"""
FUNCTIONAL · one garment, breakdown to export, through the real services.

This is the capability the whole Phase-1 system exists to deliver: a single
jacket carries one barcode from upload to the shipping box, every stage is
logged against it, and the drawer that held it is returned to the pool for the
next garment.

The layer above (`tests/functional/test_uat_scenarios.py`) covers the seven
business scenarios as separate slices. Nothing yet walks ONE piece down the
entire nine-stage chain in order, which is the only way to catch a gate that is
individually correct but wrong in sequence — and the only way to reach
PACKAGE_EXPORT, which no test in the suite currently logs.

CAST
    RAMESH    CUTTER    — leather cutting, and FUSING (CUTTER is on both)
    LINA      LINING_CUTTER — the parallel lining path
    PADMA     PASTER    — pasting
    TARA      TAILOR    — line + shell stitching
    FINN      FINISHER  — final finish, inspection and packing

    The last three stages carry an EMPTY role set (enums_barcode.py:
    STAGE_ROLE_ACCESS), so only MD/DM may log them — the DM fixture drives the
    tail of the chain, which is what the spec intends.
"""
import datetime

import pytest
from sqlalchemy import func, select

from app.core.enums import (
    DrawerPart, DrawerState, ProductionStage, ScreenContext, WageType,
)
from app.modules.barcode.models import Drawer, MaterialLot
from app.modules.barcode.service import BarcodeService
from app.modules.drawers.service import DrawerService
from app.modules.employees.models import Employee
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService

TODAY = datetime.date.today()


@pytest.fixture
async def finisher(db, mark_present):
    """FINISHER covers FINAL_FINISH, FINAL_INSPECTION and PACKAGE_EXPORT — the
    three stages the shared fixtures do not staff."""
    emp = Employee(name="FINN", designation="FINISHER",
                   wage_type=WageType.PIECE_RATE, is_active=True)
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    await mark_present(emp.id)
    return emp


@pytest.fixture
async def lining_lot(db):
    lot = MaterialLot(category="LINING", subtype="PLAIN_LINING", article="KNIT-9",
                      colour="BLACK", thickness="0.5mm", uom="mtrs",
                      on_hand=500, is_active=True)
    db.add(lot)
    await db.commit()
    await db.refresh(lot)
    return lot


@pytest.mark.asyncio
async def test_one_lined_jacket_walks_the_whole_chain_and_recycles_its_drawer(
        db, operations, pieces, leather_lot, lining_lot,
        cutter, lining_cutter, paster, tailor, finisher,
        cutting_mgr, lining_mgr, stitching_mgr, dm):
    piece, drawer = pieces[0]
    piece_code, drawer_code, drawer_id = piece.code, drawer.code, drawer.id
    svc = ProductionService(db)
    drawers = DrawerService(db)

    # ── 0 · the state breakdown upload left behind ───────────────────────────
    assert piece.needs_lining is True
    assert drawer.state == DrawerState.MERGED.value
    resolved = await BarcodeService(db).resolve(piece_code)
    assert resolved["type"] == "PIECE"
    assert resolved["piece"]["current_stage"] is None, "a piece is uncut at upload"

    # ── 1 · both cut paths run in parallel, each charging its own lot ────────
    leather_before = float(await db.scalar(
        select(MaterialLot.on_hand).where(MaterialLot.id == leather_lot.id)))

    r = await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.LEATHER_CUT,
                            leather_lot_id=leather_lot.id, consumption_qty=14.0)
    assert r["count_logged"] == 1 and r["stage"] == "LEATHER_CUTTING"

    r = await svc.log_batch(user=lining_mgr, employee_id=lining_cutter[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.LINING_CUT,
                            lining_lot_id=lining_lot.id, consumption_qty=3.0)
    assert r["count_logged"] == 1 and r["stage"] == "LINING_CUTTING"

    assert float(await db.scalar(
        select(MaterialLot.on_hand).where(MaterialLot.id == leather_lot.id))
    ) == pytest.approx(leather_before - 14.0)

    # ── 2 · storage: drawer first, then each part ────────────────────────────
    s = await drawers.store_scan(drawer_id=drawer_id, piece_id=piece.id,
                                 part=DrawerPart.LEATHER)
    assert s["state"] == DrawerState.HOLDING_LEATHER.value
    assert s["ready_for_received"] is False

    s = await drawers.store_scan(drawer_id=drawer_id, piece_id=piece.id,
                                 part=DrawerPart.LINING)
    # Complete → the drawer receives itself (bug #13). Its CONTENTS are both.
    assert s["holding"] == "HOLDING BOTH"
    assert s["state"] == DrawerState.RECEIVED.value
    assert s["ready_for_received"] is True
    assert s["sent"] is False        # bug #15: still in the store

    # ── 3 · the leather chain up to the merge gate ───────────────────────────
    r = await svc.log_batch(user=stitching_mgr, employee_id=cutter[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.PIPELINE)
    assert r["stage"] == "FUSING" and r["count_logged"] == 1

    r = await svc.log_batch(user=stitching_mgr, employee_id=paster[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.PIPELINE)
    assert r["stage"] == "PASTING" and r["count_logged"] == 1

    # ── 4 · the gate holds while the drawer is merely complete ──────────────
    r = await svc.log_batch(user=stitching_mgr, employee_id=tailor[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.PIPELINE)
    assert r["stage"] == "LINE_STITCHING"
    assert r["count_logged"] == 0
    assert r["merge_blocked"], (
        "A complete drawer is not a release — the DM must still SEND it")

    # ── 5 · the DM's send: the one hard transition that is still a decision ──
    # RECEIVED is now automatic (it only ever restated what the last scan made
    # true). SEND is the judgement, and it is what opens the merge gate.
    out = await drawers.send_batch(drawer_ids=[drawer_id],
                                   destination="STITCHING", actor_id=dm.id)
    assert out["count_sent"] == 1
    assert out["sent"][0]["state"] == "sended"
    assert out["pieces_released"] == [piece.code]

    # ── 6 · the rest of the chain, now unblocked ────────────────────────────
    for expected, actor, user in [
        ("LINE_STITCHING",   tailor[0],   stitching_mgr),
        ("SHELL_STITCHING",  tailor[0],   stitching_mgr),
        ("FINAL_FINISH",     finisher,    dm),
        ("FINAL_INSPECTION", finisher,    dm),
        ("PACKAGE_EXPORT",   finisher,    dm),
    ]:
        r = await svc.log_batch(user=user, employee_id=actor.id,
                                piece_ids=[piece.id], work_date=TODAY,
                                screen=ScreenContext.PIPELINE)
        assert r["stage"] == expected, f"expected {expected}, got {r['stage']}"
        assert r["count_logged"] == 1, (
            f"{expected} was blocked: seq={r['sequence_blocked']} "
            f"skill={r['skill_blocked']} merge={r['merge_blocked']}")

    # ── 7 · every stage is on the record, exactly once ──────────────────────
    n_events = await db.scalar(select(func.count(ProductionEvent.id))
                               .where(ProductionEvent.piece_id == piece.id))
    assert n_events == 9, f"expected 9 stage events for one garment, got {n_events}"

    # ── 8 · the drawer recycled itself when the piece shipped ───────────────
    await db.refresh(drawer)
    assert drawer.state == DrawerState.WAITING.value, (
        "the drawer did not return to the pool after PACKAGE_EXPORT")
    assert drawer.current_piece_id is None
    assert drawer.leather_in is False and drawer.lining_in is False

    # F11: BOTH sides of the link are cleared, or the barcode payload and the
    # piece life story disagree permanently.
    await db.refresh(piece)
    assert piece.drawer_id is None

    # ── 9 · the codes still resolve — shipping does not retire a garment ────
    after = await BarcodeService(db).resolve(piece_code)
    assert after["piece"]["current_stage"] == "PACKAGE_EXPORT"
    assert after["piece"]["leather_consumption_dcm"] == pytest.approx(14.0)

    drawer_now = await BarcodeService(db).resolve(drawer_code)
    assert drawer_now["drawer"]["state"] == DrawerState.WAITING.value


@pytest.mark.asyncio
async def test_a_leather_only_garment_never_waits_for_a_lining(
        db, operations, pieces, leather_lot, cutter, paster, tailor,
        cutting_mgr, stitching_mgr, dm):
    """The other half of the completeness rule. `needs_lining=False` means the
    drawer is complete on leather alone (drawers/service.py:96), so RECEIVED is
    reachable without a lining scan and the merge gate opens.

    H9's regression made every piece need a lining, which stranded exactly this
    garment forever — the drawer waited for a lining nobody would ever cut.
    """
    piece, drawer = pieces[1]
    piece.needs_lining = False
    await db.commit()

    svc = ProductionService(db)
    drawers = DrawerService(db)

    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=11.0)

    s = await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                 part=DrawerPart.LEATHER)
    assert s["ready_for_received"] is True, "a leather-only piece is complete on leather"
    assert s["awaiting"] == [], f"still waiting on {s['awaiting']} for an unlined piece"

    await drawers.transition(drawer.id, "RECEIVED", dm.id)
    await drawers.transition(drawer.id, "SENDED", dm.id)

    await svc.log_batch(user=stitching_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.PIPELINE)          # FUSING
    await svc.log_batch(user=stitching_mgr, employee_id=paster[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.PIPELINE)          # PASTING
    r = await svc.log_batch(user=stitching_mgr, employee_id=tailor[0].id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.PIPELINE)

    assert r["stage"] == "LINE_STITCHING"
    assert r["count_logged"] == 1, f"unlined garment blocked: {r['merge_blocked']}"


@pytest.mark.asyncio
async def test_a_piece_cannot_skip_from_cutting_straight_to_inspection(
        db, operations, pieces, leather_lot, cutter, finisher, cutting_mgr, dm):
    """The sequence gate, stated as the business rule rather than the mechanism:
    a garment that has only been cut cannot be signed off as inspected.

    PIPELINE infers the NEXT stage rather than honouring a requested one
    (service.py:96-105), so the attempt lands on FUSING — the skip is
    structurally impossible, not merely refused. That is the stronger property
    and it is what this asserts.
    """
    piece, _ = pieces[2]
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    r = await svc.log_batch(user=dm, employee_id=finisher.id,
                            piece_ids=[piece.id], work_date=TODAY,
                            screen=ScreenContext.PIPELINE)

    assert r["stage"] == "FUSING", (
        "a freshly cut piece resolved to something other than the next stage")

    logged_ops = (await db.execute(
        select(ProductionEvent.operation_id)
        .where(ProductionEvent.piece_id == piece.id))).scalars().all()
    inspection = operations["FINAL_INSPECTION"].id
    assert inspection not in logged_ops, "a piece reached inspection uncut"
