"""
================================================================================
tests/integration/test_analytics_spreadsheet.py
    The order → style → piece drill-down: totals, per-stage progress, store.
================================================================================

WHAT THIS PINS

    The three analytics levels now answer the same three questions — ordered /
    completed / balance, per-stage progress, and where the garments physically
    are — and they are only useful if the levels AGREE. A style page that is not
    a strict subset of its order page is worse than no page: two screens, both
    plausible, one of them wrong, and no way to tell which.

    So the assertions here are mostly RECONCILIATION, not spot values:
      • order totals == Σ of its style totals
      • completed + balance == ordered, at every level
      • a stage's completed never exceeds the scope total
      • the store buckets account for every minted piece exactly once

THE STAGE SEMANTICS ARE THE OTHER HALF (change-list item 7 / dashboard):
    completed at a stage means an event AT THAT STAGE. A piece that has finished
    cutting and is waiting for fusing is completed at cutting and PENDING at
    fusing — asserted directly, because the old queue-depth reading reported 0
    pending for a stage nothing had reached yet, which is the opposite of true.
================================================================================
"""
from datetime import date

import pytest

from app.core.enums import DrawerState, ProductionStage
from app.modules.analytics.service import AnalyticsService
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, ProductionEvent

pytestmark = pytest.mark.asyncio

TODAY = date.today()


async def _log(db, operations, piece, sku, stage: str, emp):
    db.add(ProductionEvent(
        sku_id=sku.id, operation_id=operations[stage].id, employee_id=emp.id,
        work_date=TODAY, qty=1, piece_id=piece.id))
    piece.current_operation_id = operations[stage].id
    await db.commit()


@pytest.fixture
async def worker(db):
    emp = Employee(name="Analytics-Worker", designation="CUTTER")
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    return emp


# ══════════════════════════════════════════════════════════════════════════════
# ORDER LEVEL
# ══════════════════════════════════════════════════════════════════════════════
async def test_order_totals_reconcile(db, order_tree, pieces):
    """completed + balance == ordered. The arithmetic the whole page rests on."""
    order = order_tree["order"]
    out = await AnalyticsService(db).order_tree(order.id)

    t = out["totals"]
    assert t["qty_ordered"] == 5, "fixture orders 5"
    assert t["minted_pieces"] == 5
    assert t["completed"] == 0
    assert t["completed"] + t["balance"] == t["qty_ordered"]


async def test_order_totals_equal_the_sum_of_its_styles(db, order_tree, pieces,
                                                        operations, worker):
    """THE SUBSET GUARANTEE. The order page and the style rows must agree."""
    order, sku = order_tree["order"], order_tree["sku"]
    piece, _ = pieces[0]
    await _log(db, operations, piece, sku, ProductionStage.PACKAGE_EXPORT.value,
               worker)

    out = await AnalyticsService(db).order_tree(order.id)
    assert out["totals"]["completed"] == 1

    assert sum(s["qty_ordered"] for s in out["styles"]) == out["totals"]["qty_ordered"]
    assert sum(s["completed"] for s in out["styles"]) == out["totals"]["completed"]
    assert sum(s["balance"] for s in out["styles"]) == out["totals"]["balance"]


async def test_a_cut_piece_awaiting_fusing_is_pending_at_fusing(
    db, order_tree, pieces, operations, worker
):
    """THE STAGE SEMANTICS, stated as the change list states them.

    "when the cutting finish and waiting for fusing also means pending" — so the
    piece counts as COMPLETED at leather cutting and PENDING at fusing, and the
    fusing row must show the whole order outstanding, not zero.
    """
    order, sku = order_tree["order"], order_tree["sku"]
    piece, _ = pieces[0]
    await _log(db, operations, piece, sku, ProductionStage.LEATHER_CUTTING.value,
               worker)

    out = await AnalyticsService(db).order_tree(order.id)
    by_stage = {s["stage"]: s for s in out["stages"]}

    cut = by_stage[ProductionStage.LEATHER_CUTTING.value]
    assert cut["completed"] == 1
    assert cut["pending"] == 4, "the other four are still to be cut"

    fusing = by_stage[ProductionStage.FUSING.value]
    assert fusing["completed"] == 0
    assert fusing["pending"] == 5, (
        "a stage nothing has reached must show the whole order pending, not 0")

    # Every stage shares one denominator, which is what makes the columns
    # comparable down the page.
    assert {s["total"] for s in out["stages"]} == {5}


async def test_every_pipeline_stage_is_reported_even_when_empty(db, order_tree,
                                                                pieces):
    """A funnel that omits untouched stages renames the pipeline as work moves."""
    out = await AnalyticsService(db).order_tree(order_tree["order"].id)
    reported = [s["stage"] for s in out["stages"]]
    for stage in (ProductionStage.LEATHER_CUTTING, ProductionStage.LINING_CUTTING,
                  ProductionStage.FUSING, ProductionStage.PASTING,
                  ProductionStage.LINE_STITCHING, ProductionStage.SHELL_STITCHING,
                  ProductionStage.FINAL_FINISH, ProductionStage.FINAL_INSPECTION,
                  ProductionStage.PACKAGE_EXPORT):
        assert stage.value in reported


# ══════════════════════════════════════════════════════════════════════════════
# THE STORE BLOCK
# ══════════════════════════════════════════════════════════════════════════════
async def test_the_store_block_counts_every_piece_once(db, order_tree, pieces):
    """Store buckets must partition the pieces — no double counting, none lost."""
    order = order_tree["order"]

    # Three distinct store situations across the five fixture pieces.
    pieces[0][1].leather_in = True
    pieces[0][1].state = DrawerState.HOLDING_LEATHER.value
    pieces[1][1].leather_in = pieces[1][1].lining_in = True
    pieces[1][1].state = DrawerState.HOLDING_BOTH.value
    pieces[2][1].leather_in = pieces[2][1].lining_in = True
    pieces[2][1].state = DrawerState.SENDED.value
    await db.commit()

    store = (await AnalyticsService(db).order_tree(order.id))["store"]

    assert store["holding_leather"] == 1
    assert store["holding_both"] == 1
    assert store["sended"] == 1
    assert store["awaiting_parts"] == 2, "the two untouched drawers are MERGED"
    assert store["no_drawer"] == 0

    total_in_buckets = sum(b["pieces"] for b in store["buckets"])
    assert total_in_buckets + store["no_drawer"] == 5, (
        "the store block lost or double-counted a piece")


async def test_a_piece_with_no_drawer_shows_in_the_store_block(db, order_tree,
                                                               pieces):
    """The waiting list is a store fact, and it has no drawer state to live in."""
    piece, drawer = pieces[0]
    drawer.current_piece_id = None
    piece.drawer_id = None
    await db.commit()

    store = (await AnalyticsService(db).order_tree(order_tree["order"].id))["store"]
    assert store["no_drawer"] == 1


# ══════════════════════════════════════════════════════════════════════════════
# STYLE + PIECE LEVELS
# ══════════════════════════════════════════════════════════════════════════════
async def test_style_detail_carries_totals_stages_store_and_pieces(
    db, order_tree, pieces, operations, worker
):
    style, sku = order_tree["style"], order_tree["sku"]
    piece, drawer = pieces[0]
    await _log(db, operations, piece, sku, ProductionStage.LEATHER_CUTTING.value,
               worker)
    drawer.leather_in = True
    drawer.state = DrawerState.HOLDING_LEATHER.value
    await db.commit()

    out = await AnalyticsService(db).style_detail(style.id)

    assert out["totals"]["qty_ordered"] == 5
    assert out["totals"]["completed"] + out["totals"]["balance"] == 5
    assert {s["total"] for s in out["stages"]} == {5}
    assert out["store"]["holding_leather"] == 1
    assert out["piece_count"] == 5

    row = next(p for p in out["pieces"] if p["piece_id"] == str(piece.id))
    assert row["current_stage"] == ProductionStage.LEATHER_CUTTING.value
    assert row["drawer_code"] == drawer.code
    # The piece is parked in a drawer, so the board shows STORE rather than the
    # cut stage it last logged.
    assert row["in_store"] is True
    assert row["display_stage"] == "STORE"


async def test_piece_detail_names_the_employee_at_every_stage(
    db, order_tree, pieces, operations, worker
):
    """Per piece, per stage, WHO did it — the leaf of the drill-down."""
    sku = order_tree["sku"]
    piece, drawer = pieces[0]
    await _log(db, operations, piece, sku, ProductionStage.LEATHER_CUTTING.value,
               worker)

    out = await AnalyticsService(db).piece_detail(piece_code=piece.code)

    assert out["piece_code"] == piece.code
    assert out["stages"], "no stage history"
    cut = out["stages"][0]
    assert cut["stage_code"] == ProductionStage.LEATHER_CUTTING.value
    assert cut["employee_name"] == worker.name
    assert cut["employee_id"] == str(worker.id)
    assert cut["work_date"] == TODAY.isoformat()

    # The checklist shows what has NOT happened, which a history list cannot.
    checklist = {c["stage"]: c for c in out["checklist"]}
    assert checklist[ProductionStage.LEATHER_CUTTING.value]["state"] == "completed"
    assert checklist[ProductionStage.FUSING.value]["state"] == "pending"
    assert out["store"]["drawer_code"] == drawer.code


async def test_piece_detail_marks_the_lining_cut_not_applicable_when_declared(
    db, order_tree, pieces
):
    """A style released leather-only must not show a permanently pending lining cut."""
    style = order_tree["style"]
    style.needs_lining = False
    piece, _ = pieces[0]
    piece.needs_lining = False
    await db.commit()

    out = await AnalyticsService(db).piece_detail(piece_code=piece.code)
    assert out["needs_lining"] is False
    checklist = {c["stage"]: c for c in out["checklist"]}
    assert checklist[ProductionStage.LINING_CUTTING.value]["state"] == "not_applicable"
