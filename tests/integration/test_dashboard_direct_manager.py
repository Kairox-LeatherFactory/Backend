"""
INTEGRATION · the Direct Manager dashboard — the factory in one view.

WHAT THIS SCREEN IS
    A composition, not a fifth calculation. It reads the same aggregates the
    cutting / lining / stitching / store dashboards read, so the MD's numbers and
    the floor's numbers cannot disagree. Several tests below assert exactly that
    rather than checking a hard-coded figure.

THE TWO IDEAS WORTH PINNING
    1. `pending` at a pipeline node is a QUEUE — what the upstream stage finished
       and this one has not — and the bottleneck is the DEEPEST queue, not the
       earliest stage with unfinished work. On a healthy line the earliest stage
       always has some WIP, so "first unfinished" would name it every time and
       tell the MD nothing.
    2. Figures with no table behind them are NULL, never a plausible zero. This
       is the screen decisions get made on; an invented rejection count is worse
       than an honest gap, and `meta.unsupported` says which is which.
"""
import datetime

import pytest

from app.core.enums import ScreenContext
from app.modules.dashboard.service import DashboardService
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


async def _walk(db, piece, *, cutter, tailor, cutting_mgr, dm, leather_lot,
                drawer=None, stages=1):
    """Log `stages` steps of the chain for one piece, starting at the leather cut."""
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[piece.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=10.0)
    for _ in range(stages - 1):
        res = await svc.log_batch(user=dm, employee_id=tailor[0].id,
                                  piece_ids=[piece.id], work_date=TODAY,
                                  screen=ScreenContext.PIPELINE)
        if res["count_logged"] == 0:
            break


# ══════════════════════════════════════════════ the panel holds together
@pytest.mark.asyncio
async def test_the_overview_returns_every_block(
    db, operations, pieces, cutter, tailor, cutting_mgr, dm, leather_lot
):
    await _walk(db, pieces[0][0], cutter=cutter, tailor=tailor,
                cutting_mgr=cutting_mgr, dm=dm, leather_lot=leather_lot, stages=3)

    d = await DashboardService(db).direct_manager_overview(client_scope=None)

    assert d.overall.total_target == 5
    assert d.overall.total_pending == d.overall.total_target - d.overall.total_produced
    assert len(d.departments) == 6
    assert [n.stage for n in d.pipeline] == list(
        DashboardService(db).repo._DM_PIPELINE)
    assert d.attendance.employees_assigned >= 1
    assert d.meta.scope == "all_clients"


@pytest.mark.asyncio
async def test_the_pipeline_is_a_funnel_and_the_bottleneck_is_its_deepest_queue(
    db, operations, pieces, cutter, tailor, cutting_mgr, dm, leather_lot
):
    """Three pieces cut, one of them carried two stages further. The queue in
    front of FUSING is therefore the largest gap after the cut."""
    for piece, _ in pieces[:3]:
        await _walk(db, piece, cutter=cutter, tailor=tailor,
                    cutting_mgr=cutting_mgr, dm=dm, leather_lot=leather_lot,
                    stages=1)
    await ProductionService(db).log_batch(
        user=dm, employee_id=tailor[0].id, piece_ids=[pieces[0][0].id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)      # FUSING on one

    d = await DashboardService(db).direct_manager_overview(client_scope=None)
    by_stage = {n.stage: n for n in d.pipeline}

    assert by_stage["LEATHER_CUTTING"].completed == 3
    assert by_stage["FUSING"].completed == 1
    # 5 ordered, 3 cut → a queue of 2 in front of the cut;
    # 3 cut, 1 fused    → a queue of 2 in front of fusing.
    assert by_stage["LEATHER_CUTTING"].pending == 2
    assert by_stage["FUSING"].pending == 2
    # Each node's completed count never exceeds its predecessor's — it is a funnel.
    counts = [n.completed for n in d.pipeline]
    assert counts == sorted(counts, reverse=True)

    assert d.bottleneck.stage in ("LEATHER_CUTTING", "FUSING")
    assert d.bottleneck.queue == max(n.pending for n in d.pipeline)


@pytest.mark.asyncio
async def test_a_department_counts_a_garment_once_however_many_of_its_stages_it_passed(
    db, operations, pieces, cutter, tailor, cutting_mgr, dm, leather_lot
):
    """THE COUNT THAT IS EASY TO GET WRONG. Stitching folds three stages. A piece
    through all three is ONE garment stitched, not three — which is why the
    department count is a distinct-on-piece per department label, not a sum of
    per-stage counts."""
    piece, drawer = pieces[0]
    await _walk(db, piece, cutter=cutter, tailor=tailor, cutting_mgr=cutting_mgr,
                dm=dm, leather_lot=leather_lot, stages=3)      # cut, fusing, pasting

    from app.core.enums import DrawerPart
    from app.modules.drawers.service import DrawerService
    drawers = DrawerService(db)
    for part in (DrawerPart.LEATHER, DrawerPart.LINING):
        await drawers.store_scan(drawer_id=drawer.id, piece_id=piece.id, part=part)
    await drawers.send_batch(drawer_ids=[drawer.id], destination="STITCHING",
                             actor_id=dm.id)
    # line, shell, final finish — all three Stitching stages
    for _ in range(3):
        await ProductionService(db).log_batch(
            user=dm, employee_id=tailor[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.PIPELINE)

    d = await DashboardService(db).direct_manager_overview(client_scope=None)
    stitching = next(r for r in d.departments if r.department == "Stitching")
    assert stitching.produced == 1, (
        "one garment through three stitching stages is one produced piece")
    assert stitching.stages == ["LINE_STITCHING", "SHELL_STITCHING", "FINAL_FINISH"]


@pytest.mark.asyncio
async def test_the_dm_and_the_stage_dashboards_agree(
    db, operations, pieces, cutter, tailor, cutting_mgr, dm, leather_lot
):
    """THE COMPOSITION TEST. If these ever diverge, both screens are worthless —
    and the MD's is the one people act on."""
    for piece, _ in pieces[:2]:
        await _walk(db, piece, cutter=cutter, tailor=tailor,
                    cutting_mgr=cutting_mgr, dm=dm, leather_lot=leather_lot)

    svc = DashboardService(db)
    dm_view = await svc.direct_manager_overview(client_scope=None)
    cutting = await svc.overview(client_scope=None)
    store = await svc.store_overview(client_scope=None)

    assert dm_view.overall.total_target == cutting.production_kpis.total_order_pieces
    assert dm_view.overall.total_produced == cutting.production_kpis.overall_completed
    assert dm_view.quality.rework_pieces == cutting.production_kpis.rework_pieces
    assert dm_view.store.drawers_in_store == store.kpis.drawers_in_store
    assert dm_view.store.drawers_sent == store.kpis.drawers_sent


# ══════════════════════════════════════════════ honesty about missing data
@pytest.mark.asyncio
async def test_figures_with_no_table_behind_them_are_null_not_zero(db, pieces):
    d = await DashboardService(db).direct_manager_overview(client_scope=None)

    assert d.quality.accepted is None
    assert d.quality.rejected is None
    assert d.quality.defective_pct is None
    assert d.overall.total_rejected is None
    assert d.production_rate.per_piece_rate is None
    assert d.production_rate.pieces_per_shift is None
    # ...and every gap is explained rather than left for someone to discover
    assert "quality_rejection" in d.meta.unsupported
    assert "rate_shift_costing" in d.meta.unsupported


@pytest.mark.asyncio
async def test_an_empty_factory_reports_zeroes_without_dividing_by_zero(db):
    """No orders, no pieces, no events — every ratio has a zero denominator."""
    d = await DashboardService(db).direct_manager_overview(client_scope=None)
    assert d.overall.total_target == 0
    assert d.overall.overall_achievement_pct == 0.0
    assert d.production_rate.pieces_per_day == 0.0
    assert d.production_rate.pieces_per_employee_today == 0.0
    assert all(r.achievement_pct == 0.0 for r in d.departments)
    assert d.bottleneck.stage is None


# ══════════════════════════════════════════════ the drill-downs
@pytest.mark.asyncio
async def test_order_tracking_walks_the_whole_chain(
    db, operations, pieces, order_tree, cutter, tailor, cutting_mgr, dm, leather_lot
):
    await _walk(db, pieces[0][0], cutter=cutter, tailor=tailor,
                cutting_mgr=cutting_mgr, dm=dm, leather_lot=leather_lot, stages=2)

    t = await DashboardService(db).dm_order_tracking(
        order_id=order_tree["order"].id)
    assert t is not None
    assert t.order_number == "JP-PO"
    assert t.total_quantity == 5
    by_stage = {s.stage: s for s in t.stages}
    assert by_stage["LEATHER_CUTTING"].completed == 1
    assert by_stage["LEATHER_CUTTING"].status == "IN_PROGRESS"
    assert by_stage["PACKAGE_EXPORT"].status == "PENDING"
    assert t.blocked_stage == "LEATHER_CUTTING"   # the deepest queue: 5 → 1


@pytest.mark.asyncio
async def test_an_order_with_no_styles_still_answers(db, order_tree):
    """An order that has not been broken down yet is a real order. It must return
    an empty funnel, not a 404 — the OUTER joins in order_head are what make that
    true."""
    from app.modules.clients.models import ClientOrder
    bare = ClientOrder(client_id=order_tree["client"].id, order_number="BARE-1")
    db.add(bare)
    await db.commit()

    t = await DashboardService(db).dm_order_tracking(order_id=bare.id)
    assert t is not None
    assert t.total_quantity == 0
    assert t.completion_pct == 0.0
    assert all(s.status == "PENDING" for s in t.stages)


@pytest.mark.asyncio
async def test_style_tracking_reports_per_stage_quantities(
    db, operations, pieces, order_tree, cutter, tailor, cutting_mgr, dm, leather_lot
):
    for piece, _ in pieces[:2]:
        await _walk(db, piece, cutter=cutter, tailor=tailor,
                    cutting_mgr=cutting_mgr, dm=dm, leather_lot=leather_lot)

    s = await DashboardService(db).dm_style_tracking(
        style_id=order_tree["style"].id)
    assert s is not None
    assert s.style == "CLERMONT" and s.order_number == "JP-PO"
    assert s.total_quantity == 5
    assert next(r for r in s.stages if r.stage == "LEATHER_CUTTING").completed == 2


@pytest.mark.asyncio
async def test_unknown_ids_return_none_rather_than_raising(db):
    import uuid as _uuid
    svc = DashboardService(db)
    assert await svc.dm_order_tracking(order_id=_uuid.uuid4()) is None
    assert await svc.dm_style_tracking(style_id=_uuid.uuid4()) is None
