"""
================================================================================
tests/integration/test_dashboard_stage_progress.py
    completed / pending on the dashboards — one definition, four screens.
================================================================================

THE CHANGE THIS PINS (change-list item 7)

    "when the stage finish only it will add in the complete filed, pending means
     balance pieces and when the cutting finish and waiting for fushing also
     means pending."

    Two rules, and the second is the one that was wrong:

      completed @ stage  a LOGGED EVENT at that stage. Not "reached", not
                         "current_operation_id points here".
      pending   @ stage  the BALANCE — scope total minus completed.

    `StageBlock.pending_pieces` used to be `total_received − completed`, i.e.
    queue depth against the upstream stage. Under that reading a stage nothing
    has reached yet reports ZERO pending — nothing is queued for it — while in
    fact the entire order still has to pass through it. Every stage also measured
    against a different denominator, so the column did not reconcile with the
    order quantity and nothing on the page added up.

    Queue depth is still the right measure for a BOTTLENECK, so it did not go
    away — it moved to `queue_pieces`, and the DM pipeline still uses it. Both
    are asserted here, precisely because they are different numbers and the whole
    defect was them sharing one name.

ONE SOURCE. `stage_progress` is computed by a single service method and attached
to all four dashboards, so the cutting, lining, stitching and DM screens cannot
report different figures for the same stage.
================================================================================
"""
from datetime import date

import pytest

from app.core.enums import ProductionStage
from app.modules.dashboard.service import DashboardService
from app.modules.employees.models import Employee
from app.modules.production.models import ProductionEvent

pytestmark = pytest.mark.asyncio

TODAY = date.today()


@pytest.fixture
async def worker(db):
    emp = Employee(name="Dash-Worker", designation="CUTTER")
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    return emp


async def _log(db, operations, piece, sku, stage: str, emp):
    db.add(ProductionEvent(
        sku_id=sku.id, operation_id=operations[stage].id, employee_id=emp.id,
        work_date=TODAY, qty=1, piece_id=piece.id))
    piece.current_operation_id = operations[stage].id
    await db.commit()


async def test_pending_is_the_balance_not_the_queue(
    db, order_tree, pieces, operations, worker
):
    """Two of five pieces cut. Fusing must show 5 pending, not 2."""
    sku = order_tree["sku"]
    for piece, _ in pieces[:2]:
        await _log(db, operations, piece, sku,
                   ProductionStage.LEATHER_CUTTING.value, worker)

    rows = await DashboardService(db)._stage_progress(client_scope=None)
    by_stage = {r.stage: r for r in rows}

    cut = by_stage[ProductionStage.LEATHER_CUTTING.value]
    assert cut.completed == 2
    assert cut.pending == 3

    fusing = by_stage[ProductionStage.FUSING.value]
    assert fusing.completed == 0, "no fusing event was logged"
    assert fusing.pending == 5, (
        "pending fell back to queue depth — a stage nothing has reached must "
        "still show the whole order outstanding")


async def test_every_stage_shares_one_denominator(db, order_tree, pieces):
    """The columns only compare if they measure against the same total."""
    rows = await DashboardService(db)._stage_progress(client_scope=None)
    assert {r.total for r in rows} == {5}
    for r in rows:
        assert r.completed + r.pending == r.total


async def test_completed_counts_only_a_logged_event_at_that_stage(
    db, order_tree, pieces, operations, worker
):
    """A piece PAST a stage still counts it as completed; a piece merely
    approaching it does not."""
    sku = order_tree["sku"]
    piece, _ = pieces[0]
    await _log(db, operations, piece, sku,
               ProductionStage.LEATHER_CUTTING.value, worker)
    await _log(db, operations, piece, sku, ProductionStage.FUSING.value, worker)

    rows = await DashboardService(db)._stage_progress(client_scope=None)
    by_stage = {r.stage: r for r in rows}
    assert by_stage[ProductionStage.LEATHER_CUTTING.value].completed == 1
    assert by_stage[ProductionStage.FUSING.value].completed == 1
    assert by_stage[ProductionStage.PASTING.value].completed == 0


async def test_stitching_block_separates_balance_from_queue(
    db, order_tree, pieces, operations, worker
):
    """THE TWO NUMBERS, SIDE BY SIDE.

    Three pieces fused, none pasted. Pasting's QUEUE is the three waiting in
    front of it; its BALANCE is all five, because five still have to be pasted.
    The defect was reporting the first under the name of the second.
    """
    sku = order_tree["sku"]
    for piece, _ in pieces[:3]:
        await _log(db, operations, piece, sku,
                   ProductionStage.FUSING.value, worker)

    dash = await DashboardService(db).stitching_overview(client_scope=None)
    pasting = next(s for s in dash.stages
                   if s.stage == ProductionStage.PASTING.value)

    assert pasting.completed_pieces == 0
    assert pasting.total_pieces == 5
    assert pasting.pending_pieces == 5, "pending must be the balance"
    assert pasting.queue_pieces == 3, "queue depth must survive under its own name"


async def test_all_four_dashboards_report_the_same_stage_numbers(
    db, order_tree, pieces, operations, worker
):
    """ONE SOURCE. If these ever diverge, every screen becomes untrustworthy."""
    sku = order_tree["sku"]
    piece, _ = pieces[0]
    await _log(db, operations, piece, sku,
               ProductionStage.LEATHER_CUTTING.value, worker)

    svc = DashboardService(db)
    cutting = await svc.overview(client_scope=None)
    lining = await svc.lining_overview(client_scope=None)
    stitching = await svc.stitching_overview(client_scope=None)
    dm = await svc.direct_manager_overview(client_scope=None)

    def as_pairs(dash):
        return {(r.stage, r.completed, r.pending) for r in dash.stage_progress}

    assert as_pairs(cutting) == as_pairs(lining) == as_pairs(stitching) \
        == as_pairs(dm)
    assert (ProductionStage.LEATHER_CUTTING.value, 1, 4) in as_pairs(cutting)
