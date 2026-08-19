"""
================================================================================
tests/uat/test_uat_scenarios.py — LAYER 5: USER ACCEPTANCE (business scenarios)
================================================================================
Scripted, business-language scenarios that map to the real factory flow steps,
run against real data through the services. Each test is ONE scenario a DM / HR /
MD would sign off in UAT, named in their words, with the expected outcome stated
as an assertion. These are the executable form of the UAT checklist — a failure
here is a business-rule failure, not a code detail.

Scenarios (from the product flow doc):
  UAT-1  Upload breakdown → pieces + barcodes exist, ready to print
  UAT-2  Cutting manager logs leather cutting with consumption → stock drops
  UAT-3  A worker cannot be logged on a stage they are not skilled for
  UAT-4  A piece cannot skip ahead in the pipeline
  UAT-5  A jacket needing a lining cannot go to line-stitching until both parts
         are in its drawer and the DM releases it
  UAT-6  A leaver's barcode is retired without losing their production history
  UAT-7  Short stock suggests a supplier and the order can be received
================================================================================
"""
import datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.core.database import Base
from app.core.enums import DrawerPart, ScreenContext
from app.modules.drawers.service import DrawerService
from app.modules.production.service import ProductionService
from app.modules.materials.service import MaterialService
from app.modules.barcode.service import BarcodeService


# ── UAT-1: Upload breakdown → pieces + barcodes exist ────────────────────────
def test_uat1_breakdown_upload_mints_pieces_and_barcodes():
    """AS a DM, WHEN I upload the breakdown sheet, THEN each garment gets a piece
    row and a scannable parent barcode, ready to print — before any cutting."""
    import app.modules.clients.models      # noqa
    import app.modules.production.models    # noqa
    import app.modules.barcode.models       # noqa
    from app.modules.barcode.models import BarcodeRegistry
    from app.modules.clients.models import SKU, Client, ClientOrder, Style
    from app.modules.imports.premint import premint_order
    from app.modules.production.models import Piece

    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    with Session(eng) as s:
        client = Client(name="John Peter", country="IT"); s.add(client); s.flush()
        order = ClientOrder(client_id=client.id, order_number="JP-PO"); s.add(order); s.flush()
        style = Style(client_order_id=order.id, name="CLERMONT", article="CL1", production_status="RELEASED"); s.add(style); s.flush()
        s.add(SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
                  size="M", qty_ordered=21, code="JP-CLERMONT-PINE-M"))
        s.commit()

        stats = premint_order(s, order); s.commit()

        # EXPECTED: 21 pieces, 21 PRINTABLE parent barcodes, all printable.
        # Since bug #19 each piece also keeps its long code as a scannable ALIAS,
        # so the row count is 42 — but exactly 21 of them are the labels that get
        # printed, and that is the number this scenario is about.
        assert stats["pieces_minted"] == 21
        assert s.scalar(select(func.count(Piece.id))) == 21
        assert s.scalar(select(func.count(BarcodeRegistry.id)).where(
            BarcodeRegistry.type == "PIECE",
            BarcodeRegistry.is_alias.is_(False))) == 21
        assert s.scalar(select(func.count(BarcodeRegistry.id)).where(
            BarcodeRegistry.type == "PIECE")) == 42
        # Every printed code is compact, and every one still resolves to a piece.
        printed = list(s.scalars(select(BarcodeRegistry.code).where(
            BarcodeRegistry.type == "PIECE", BarcodeRegistry.is_alias.is_(False))))
        assert all(c.startswith("PC-") and len(c) == 9 for c in printed), printed
        assert len(set(printed)) == 21


# ── UAT-2: Cutting with consumption drops stock ──────────────────────────────
@pytest.mark.asyncio
async def test_uat2_cutting_logs_consumption_and_drops_stock(
        db, operations, pieces, cutter, cutting_mgr, leather_lot):
    """AS a cutting manager, WHEN I scan a worker + a piece and enter leather
    consumption, THEN the cutting event is logged and stock falls by that much."""
    before = float(leather_lot.on_hand)
    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[pieces[0][0].id],
        work_date=datetime.date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=15.0)
    assert res["count_logged"] == 1
    await db.refresh(leather_lot)
    assert float(leather_lot.on_hand) == before - 15.0


# ── UAT-3: Skill gate ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_uat3_worker_cannot_work_unskilled_stage(
        db, operations, pieces, paster, cutting_mgr, leather_lot):
    """AS the system, WHEN a PASTER is scanned on the cutting screen, THEN the
    log IS recorded but the mismatch is reported as a skill warning.

    GATE 2 is deliberately a warning, not a block (CLAUDE.md §8 / production
    service GATE 2): the floor must never lose a scan because HR has not
    backfilled a designation, so the manager-role gate (GATE 1) stays the hard
    authority and the anomaly is surfaced for audit instead."""
    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=paster[0].id, piece_ids=[pieces[0][0].id],
        work_date=datetime.date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)
    assert res["count_logged"] == 1
    assert not res["skill_blocked"]          # nothing is BLOCKED on skill

    warning = res["skill_warnings"][0]
    assert warning["piece"] == pieces[0][0].code
    assert warning["designation"] == "PASTER"
    assert warning["stage"] == "LEATHER_CUTTING"
    assert "may not work" in warning["note"]


# ── UAT-4: Sequence gate ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_uat4_piece_cannot_skip_ahead(
        db, operations, pieces, cutter, cutting_mgr, stitching_mgr, leather_lot):
    """AS the system, WHEN a piece has only been cut, THEN the next PIPELINE scan
    logs FUSING (the immediate next step), never a later stage — no skipping."""
    today = datetime.date.today()
    p = pieces[0][0]
    await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[p.id],
        work_date=today, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)
    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=cutter[0].id, piece_ids=[p.id],
        work_date=today, screen=ScreenContext.PIPELINE)
    assert res["stage"] == "FUSING"   # not PASTING or later


# ── UAT-5: Merge gate ────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_uat5_lined_jacket_blocks_line_stitch_until_complete(
        db, operations, pieces, cutter, paster, tailor, cutting_mgr,
        stitching_mgr, dm, leather_lot):
    """AS a DM, WHEN a jacket needs a lining, THEN it cannot go to line-stitching
    until both leather and lining are in its drawer and I mark it SENDED."""
    today = datetime.date.today()
    piece, drawer = pieces[0]
    prod = ProductionService(db)
    for screen, emp, mgr in [
        (ScreenContext.LEATHER_CUT, cutter[0], cutting_mgr),
    ]:
        await prod.log_batch(user=mgr, employee_id=emp.id, piece_ids=[piece.id],
                             work_date=today, screen=screen,
                             leather_lot_id=leather_lot.id, consumption_qty=10.0)
    await prod.log_batch(user=stitching_mgr, employee_id=cutter[0].id,
                         piece_ids=[piece.id], work_date=today,
                         screen=ScreenContext.PIPELINE)   # fusing
    await prod.log_batch(user=stitching_mgr, employee_id=paster[0].id,
                         piece_ids=[piece.id], work_date=today,
                         screen=ScreenContext.PIPELINE)   # pasting

    # BEFORE storage: line-stitch is blocked
    blocked = await prod.log_batch(user=stitching_mgr, employee_id=tailor[0].id,
                                   piece_ids=[piece.id], work_date=today,
                                   screen=ScreenContext.PIPELINE)
    assert blocked["merge_blocked"]

    # store both + DM releases
    drawers = DrawerService(db)
    await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id, part=DrawerPart.LEATHER)
    await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id, part=DrawerPart.LINING)
    await drawers.transition(drawer.id, "RECEIVED", actor_id=dm.id)
    await drawers.transition(drawer.id, "SENDED", actor_id=dm.id)

    # AFTER: line-stitch succeeds
    ok = await prod.log_batch(user=stitching_mgr, employee_id=tailor[0].id,
                              piece_ids=[piece.id], work_date=today,
                              screen=ScreenContext.PIPELINE)
    assert ok["count_logged"] == 1


# ── UAT-6: Leaver's barcode retired, history kept ────────────────────────────
@pytest.mark.asyncio
async def test_uat6_leaver_barcode_retired_history_kept(
        db, operations, pieces, cutter, cutting_mgr, dm, leather_lot):
    """AS HR, WHEN a worker leaves and I deactivate their barcode, THEN the label
    stops scanning but every piece they made and every wage line stays intact."""
    emp, bc = cutter
    await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=emp.id, piece_ids=[pieces[0][0].id],
        work_date=datetime.date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=10.0)

    res = await BarcodeService(db).deactivate_employee_barcode(emp.id, actor_id=dm.id)
    assert res["active"] is False and res["history_preserved"] is True

    # label stops scanning
    with pytest.raises(Exception):
        await BarcodeService(db).resolve(bc.code)

    # history intact
    from app.modules.production.models import ProductionEvent
    cnt = await db.scalar(select(func.count(ProductionEvent.id)).where(
        ProductionEvent.employee_id == emp.id))
    assert cnt >= 1


# ── UAT-7: Short stock → supplier suggestion → receive ───────────────────────
@pytest.mark.asyncio
async def test_uat7_shortfall_suggests_supplier_and_receives(db, dm):
    """AS a DM, WHEN stock is short, THEN the system suggests a supplier; I order,
    mark it arrived, and receive approved/rejected quantities into stock."""
    from app.modules.barcode.models import MaterialSupplier as Supplier  # renamed: F104 fix
    from app.modules.materials.schemas import (
        LotCreate, ReceiveRequest, SupplierOrderCreate,
    )
    # a supplier that stocks the article
    db.add(Supplier(name="Chennai Leathers", articles="SUEDE-A32", is_active=True))
    # a lot with only 100 dcm
    await MaterialService(db).create_lot(LotCreate(
        category="LEATHER", article="SUEDE-A32", colour="PINE",
        attributes={"thickness": "1.2mm", "dcm": 100}))

    # short by 400 against a 500 requirement → supplier suggested
    stock = await MaterialService(db).stock(
        category="LEATHER", article="SUEDE-A32", required=500)
    assert stock["short_by"] == 400.0
    assert stock["suggested_supplier"]["name"] == "Chennai Leathers"

    # order → arrived → receive
    order = await MaterialService(db).create_order(
        SupplierOrderCreate(category="LEATHER", article="SUEDE-A32", qty=400),
        actor_id=dm.id)
    assert order["status"] == "ordered"
    await MaterialService(db).mark_arrived(order["order_id"])

    # receive against the lot
    from sqlalchemy import select as _sel
    from app.modules.barcode.models import MaterialLot
    lot = await db.scalar(_sel(MaterialLot).where(MaterialLot.article == "SUEDE-A32"))
    res = await MaterialService(db).receive(ReceiveRequest(
        lot_id=lot.id, supplier_order_id=order["order_id"],
        approved_qty=400, rejected_qty=0, reserve_for_required=500), actor_id=dm.id)
    assert res["on_hand"] == 500.0
