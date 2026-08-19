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
    # The pipeline is the FULL factory view, not just the linear leather chain:
    # the parallel lining cut and the (event-less, drawer-derived) store node are
    # in it too, each in its real position.
    repo = DashboardService(db).repo
    assert [n.stage for n in d.pipeline] == [
        code for code, _ in repo._DM_DISPLAY_PIPELINE]
    assert [n.kind for n in d.pipeline] == [
        kind for _, kind in repo._DM_DISPLAY_PIPELINE]
    assert [n.sequence for n in d.pipeline] == list(range(1, len(d.pipeline) + 1))
    by_stage = {n.stage: n for n in d.pipeline}
    assert by_stage["LINING_CUTTING"].kind == "PARALLEL"
    assert by_stage["STORE"].kind == "STORE"
    # Every CHAIN node is measured against the order quantity; the two special
    # nodes are not — that is exactly why they carry their own `total`.
    assert all(n.total == d.overall.total_target
               for n in d.pipeline if n.kind == "CHAIN")
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
    # Each CHAIN node's completed count never exceeds its predecessor's — the
    # linear chain is a funnel. LINING_CUTTING and STORE are excluded: the
    # lining cut is a PARALLEL entry with no predecessor in this list, and the
    # store is drawer state rather than an event count, so neither is bound by
    # the monotonicity the chain has.
    counts = [n.completed for n in d.pipeline if n.kind == "CHAIN"]
    assert counts == sorted(counts, reverse=True)

    assert d.bottleneck.stage in ("LEATHER_CUTTING", "FUSING")
    # The bottleneck skips the PARALLEL node (unlike denominator), so the max it
    # reports is the max over the nodes that actually compete for it.
    assert d.bottleneck.queue == max(
        n.pending for n in d.pipeline if n.kind != "PARALLEL")


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
    await drawers.send_batch(drawer_ids=[drawer.id], actor_id=dm.id)
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
async def test_order_tracking_reports_the_lining_cut_and_the_store(
    db, operations, pieces, order_tree, cutter, tailor, cutting_mgr, dm, leather_lot
):
    """REGRESSION: both were missing from this endpoint.

    `stages` used to be the linear leather chain only, so an order held up in
    the store, or waiting on lining, showed as "stalled after pasting" with
    nothing on screen to say why. Both nodes must be present, in their real
    pipeline positions, each carrying its own denominator.
    """
    await _walk(db, pieces[0][0], cutter=cutter, tailor=tailor,
                cutting_mgr=cutting_mgr, dm=dm, leather_lot=leather_lot, stages=2)

    t = await DashboardService(db).dm_order_tracking(
        order_id=order_tree["order"].id)
    by_stage = {r.stage: r for r in t.stages}

    assert "LINING_CUTTING" in by_stage, "the parallel cut path must be reported"
    assert "STORE" in by_stage, "the store must be reported"

    # Real pipeline order: the lining cut sits beside the leather cut, and the
    # store sits between pasting and line-stitching.
    order = [r.stage for r in t.stages]
    assert order.index("LEATHER_CUTTING") < order.index("LINING_CUTTING")
    assert order.index("PASTING") < order.index("STORE") < order.index("LINE_STITCHING")

    # Each is priced on its own terms, not on the order quantity.
    assert by_stage["LINING_CUTTING"].kind == "PARALLEL"
    assert by_stage["LINING_CUTTING"].total == 5      # all 5 pieces need lining
    assert by_stage["STORE"].kind == "STORE"
    assert by_stage["LEATHER_CUTTING"].kind == "CHAIN"
    assert by_stage["LEATHER_CUTTING"].total == t.total_quantity

    # pct is computed against the node's OWN total, so it can never exceed 100
    # on a node whose denominator is smaller than the order.
    assert all(0.0 <= r.pct <= 100.0 for r in t.stages)


@pytest.mark.asyncio
async def test_style_tracking_reports_the_lining_cut_and_the_store(
    db, operations, pieces, order_tree, cutter, tailor, cutting_mgr, dm, leather_lot
):
    """The style drill-down shares the order drill-down's builder, so the two
    can never disagree about which stages exist."""
    s = await DashboardService(db).dm_style_tracking(
        style_id=order_tree["style"].id)
    stages = [r.stage for r in s.stages]
    assert "LINING_CUTTING" in stages and "STORE" in stages
    assert stages == [
        r.stage for r in (await DashboardService(db).dm_order_tracking(
            order_id=order_tree["order"].id)).stages]


@pytest.mark.asyncio
async def test_a_piece_held_in_the_store_is_counted_as_pending_there(
    db, operations, pieces, order_tree, cutter, cutting_mgr, dm, leather_lot
):
    """The number the DM actually wants: garments sitting in a drawer.

    A piece whose leather has been stored is IN the store — not released — so it
    must land on the store node's `pending`, which is precisely the signal that
    used to be absent from this screen."""
    from app.core.enums import DrawerPart
    from app.modules.drawers.service import DrawerService

    piece, sku = pieces[0]
    await _walk(db, piece, cutter=cutter, tailor=None, cutting_mgr=cutting_mgr,
                dm=dm, leather_lot=leather_lot, stages=1)
    await DrawerService(db).store_scan(
        drawer_id=piece.drawer_id, piece_id=piece.id,
        part=DrawerPart.LEATHER, actor_id=dm.id)

    t = await DashboardService(db).dm_order_tracking(
        order_id=order_tree["order"].id)
    store = next(r for r in t.stages if r.stage == "STORE")
    assert store.pending >= 1, "a stored piece is waiting in the store"
    assert store.completed == 0, "nothing has been released by the DM yet"


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
