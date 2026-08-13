"""
INTEGRATION · the manager dashboards — lining KPIs, stage isolation, piece trace.

THREE DEFECTS PINNED HERE
    #3  `/dashboard/lining` reported `overall` 12,417 against `lining_required`
        947. Neither number was wrong on its own; they counted DIFFERENT
        POPULATIONS and nothing said so. Two causes, and both are covered:

          A  `needs_lining` is written once at mint and never recomputed, so an
             order minted before the detection rules changed is flagged False
             forever. On live, one of two identical orders had 0 of 1,425 flagged.
          B  the ordered total spans every order; every piece-derived number can
             only span orders that have actually been pre-minted (4 of 6 had not).

    #4  the CUTTING consumption grid asked for both cut stages while joining the
        leather lot, so lining-cut events landed in it with a blank lot and their
        quantities counted toward leather totals.

    #5  piece-level tracking existed only for stitching.
"""
import datetime

import pytest

from app.core.enums import ScreenContext
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.dashboard.service import DashboardService
from app.modules.drawers.service import DrawerService
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.integrity

TODAY = datetime.date.today()


# ══════════════════════════════════════════════ #3 — the lining populations
@pytest.mark.asyncio
async def test_the_lining_kpis_name_every_population_they_count(db, pieces):
    k = (await DashboardService(db).lining_overview(client_scope=None)).production_kpis

    # The fixture: 5 pieces, all needs_lining=True, one order, all minted.
    assert k.total_order_pieces == 5      # Σ qty_ordered
    assert k.minted_pieces == 5           # actual piece rows — the real denominator
    assert k.lining_required_pieces == 5
    assert k.pending_basis == "lining_required_pieces"
    assert k.orders_without_pieces == 0

    # THE DIRECTION OF A DISAGREEMENT IS ITS MEANING. This fixture hand-sets
    # needs_lining=True on a CLERMONT style, which carries no lining signal — so
    # the stored flags say 5 and a live recount says 0. They differ, so `stale`
    # is true; but nothing is MISSING a flag, so the actionable count is zero and
    # the dashboard must not report phantom un-flagged work.
    assert k.lining_required_derived == 0
    assert k.lining_flag_stale is True
    assert k.lining_flag_undercount == 0


@pytest.mark.asyncio
async def test_an_order_with_no_pieces_is_reported_not_hidden(db, pieces, order_tree):
    """CAUSE B. An order that was never pre-minted still contributes to the
    ordered total, so the total legitimately outruns every piece count. That has
    to be VISIBLE — unexplained, it looks exactly like the bug that was filed."""
    client = order_tree["client"]
    ghost = ClientOrder(client_id=client.id, order_number="NOT-MINTED")
    db.add(ghost); await db.flush()
    style = Style(client_order_id=ghost.id, name="TOWER", article="T1",
                  code="NM-TOWER")
    db.add(style); await db.flush()
    db.add(SKU(style_id=style.id, color_code="B", color_name="BLACK", size="L",
               qty_ordered=100, code="NM-TOWER-B-L"))
    await db.commit()

    k = (await DashboardService(db).lining_overview(client_scope=None)).production_kpis
    assert k.total_order_pieces == 105     # 5 minted + 100 ordered-but-not-minted
    assert k.minted_pieces == 5            # ...and only 5 pieces exist
    assert k.orders_without_pieces == 1, (
        "the gap between the two numbers above must be explained, not left to "
        "look like a broken ratio")


@pytest.mark.asyncio
async def test_a_stale_needs_lining_flag_announces_itself(db, pieces):
    """CAUSE A. The stored flag is a snapshot. When it disagrees with a live
    recount the dashboard says so, instead of silently reporting the low number
    and leaving someone to wonder why lining_required is a fraction of the order.

    The fixture's style is CLERMONT — no lining marker — so clearing the flags
    makes the stored count 0 while the derived count follows the style/SKU
    signals. Here both are 0 and agree; setting an explicit knit colour on one
    SKU makes the derived count rise while the stored flags stay behind, which is
    exactly the live situation.
    """
    svc = DashboardService(db)
    for piece, _ in pieces:
        piece.needs_lining = False
    await db.commit()

    k = (await svc.lining_overview(client_scope=None)).production_kpis
    assert k.lining_required_pieces == 0
    assert k.lining_required_derived == 0
    assert k.lining_flag_stale is False        # nothing to detect yet

    # Give the SKU an explicit knit colour — a signal premint would act on today,
    # but which cannot retro-flag pieces already minted.
    sku_id = pieces[0][0].sku_id
    sku = await db.get(SKU, sku_id)
    sku.knit_color = "ECRU"
    await db.commit()

    k = (await svc.lining_overview(client_scope=None)).production_kpis
    assert k.lining_required_derived == 5, "the live recount sees the new signal"
    assert k.lining_required_pieces == 0, "the stored flags are still stale"
    assert k.lining_flag_stale is True, (
        "the disagreement is the whole point — it tells you to run "
        "scripts/backfill_needs_lining.py")
    assert k.lining_flag_undercount == 5, (
        "and THIS is the actionable number: five pieces whose lining work is "
        "currently invisible on this dashboard")


@pytest.mark.asyncio
async def test_the_derived_count_follows_the_style_name_markers(db, pieces, order_tree):
    """The derived recount must use the SAME vocabulary premint uses, not a
    second list that drifts from it."""
    from app.modules.imports.premint import LINING_NAME_MARKERS

    style = order_tree["style"]
    style.name = f"CLERMONT {LINING_NAME_MARKERS[0]}"      # e.g. "CLERMONT KNIT"
    for piece, _ in pieces:
        piece.needs_lining = False
    await db.commit()

    k = (await DashboardService(db).lining_overview(client_scope=None)).production_kpis
    assert k.lining_required_derived == 5
    assert k.lining_flag_stale is True


# ══════════════════════════════════════════════ #4 — stage isolation
@pytest.mark.asyncio
async def test_the_cutting_grid_never_shows_lining_cuts(
    db, operations, pieces, cutter, lining_cutter, cutting_mgr, lining_mgr,
    leather_lot
):
    """THE REGRESSION. Both a leather cut and a lining cut exist; each grid must
    show only its own."""
    svc = ProductionService(db)
    a, b = pieces[0][0], pieces[1][0]
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[a.id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=12.0)
    await svc.log_batch(user=lining_mgr, employee_id=lining_cutter[0].id,
                        piece_ids=[b.id], work_date=TODAY,
                        screen=ScreenContext.LINING_CUT,
                        lining_lot_id=leather_lot.id, consumption_qty=4.0)

    dash = DashboardService(db)
    cutting = await dash.consumption(client_scope=None)
    lining = await dash.lining_consumption(client_scope=None)

    assert {r.stage for r in cutting} == {"LEATHER_CUTTING"}
    assert {r.stage for r in lining} == {"LINING_CUTTING"}
    assert [r.piece_code for r in cutting] == [a.code]
    assert [r.piece_code for r in lining] == [b.code]
    # and the totals cannot bleed across
    assert sum(r.actual_consumption for r in cutting) == pytest.approx(12.0)
    assert sum(r.actual_consumption for r in lining) == pytest.approx(4.0)


@pytest.mark.asyncio
async def test_the_stage_parameter_selects_the_matching_lot_column(
    db, operations, pieces, lining_cutter, lining_mgr, leather_lot
):
    """Asking the cutting endpoint for the lining stage must also swap the lot
    column — stage and lot are one choice, not two."""
    await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id,
        piece_ids=[pieces[0][0].id], work_date=TODAY,
        screen=ScreenContext.LINING_CUT,
        lining_lot_id=leather_lot.id, consumption_qty=6.0)

    rows = await DashboardService(db).consumption(
        client_scope=None, stage="LINING_CUTTING")
    assert len(rows) == 1
    assert rows[0].stage == "LINING_CUTTING"
    # The lot resolved through lining_lot_id, so the article is populated.
    assert rows[0].material_article == leather_lot.article
    assert rows[0].leather_article == leather_lot.article   # deprecated alias


@pytest.mark.asyncio
async def test_an_unmeasured_lining_cut_is_hidden_by_default_and_findable_on_request(
    db, operations, pieces, lining_cutter, lining_mgr
):
    """Lining consumption is optional now, so an unmeasured cut is real work with
    no quantity. The grid is about consumption, so it is excluded by default —
    but it must be reachable, or the work looks like it never happened."""
    await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id,
        piece_ids=[pieces[0][0].id], work_date=TODAY,
        screen=ScreenContext.LINING_CUT)          # no consumption at all

    dash = DashboardService(db)
    assert await dash.lining_consumption(client_scope=None) == []

    shown = await dash.lining_consumption(client_scope=None, include_unmeasured=True)
    assert len(shown) == 1
    assert shown[0].actual_consumption is None
    assert shown[0].stage == "LINING_CUTTING"


@pytest.mark.asyncio
async def test_a_non_cut_stage_is_refused_rather_than_answered_emptily(db):
    """FUSING records no material. Returning [] would imply "none consumed";
    the honest answer is that the question does not apply."""
    with pytest.raises(ValueError, match="not a cut stage"):
        await DashboardService(db).consumption(client_scope=None, stage="FUSING")


@pytest.mark.asyncio
async def test_per_lot_totals_only_count_their_own_cut_stage(
    db, operations, pieces, cutter, lining_cutter, cutting_mgr, lining_mgr,
    leather_lot
):
    """The lot aggregate filtered on nothing but "this column is set". Both cut
    stages pointing at the same lot is the case that exposes it."""
    svc = ProductionService(db)
    await svc.log_batch(user=cutting_mgr, employee_id=cutter[0].id,
                        piece_ids=[pieces[0][0].id], work_date=TODAY,
                        screen=ScreenContext.LEATHER_CUT,
                        leather_lot_id=leather_lot.id, consumption_qty=10.0)
    await svc.log_batch(user=lining_mgr, employee_id=lining_cutter[0].id,
                        piece_ids=[pieces[1][0].id], work_date=TODAY,
                        screen=ScreenContext.LINING_CUT,
                        lining_lot_id=leather_lot.id, consumption_qty=3.0)

    rows = await DashboardService(db).repo.leather_by_lot()
    consumed = {r[2]: float(r[7]) for r in rows}      # article -> consumed
    assert consumed[leather_lot.article] == pytest.approx(10.0), (
        "the leather lot total must not absorb the lining cut's 3.0")


# ══════════════════════════════════════════════ #5 — shared piece tracking
@pytest.mark.asyncio
async def test_the_piece_trace_carries_consumption_and_the_drawer(
    db, operations, pieces, cutter, cutting_mgr, leather_lot
):
    piece, drawer = pieces[0]
    await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=leather_lot.id, consumption_qty=12.5)
    await DrawerService(db).store_scan(drawer_id=drawer.id, piece_id=piece.id)

    t = await DashboardService(db).piece_trace(piece_code=piece.code)

    # identity, matching every other piece payload
    assert t.piece_code == piece.code and t.serial == "001"
    assert t.article == "CL1"
    # WHERE it is — the code, not just the state
    assert t.drawer_code == drawer.code
    assert t.drawer_holding == "HOLDING LEATHER"
    # WHAT it consumed — the field the cutting and lining screens exist for
    cut_row = next(h for h in t.history if h.stage == "LEATHER_CUTTING")
    assert cut_row.consumption == pytest.approx(12.5)
    assert cut_row.lot_article == leather_lot.article
    assert t.total_consumption == pytest.approx(12.5)


@pytest.mark.asyncio
async def test_an_unmeasured_cut_still_appears_in_the_piece_story(
    db, operations, pieces, lining_cutter, lining_mgr
):
    """A piece's own history must never omit work that happened."""
    piece, _ = pieces[0]
    await ProductionService(db).log_batch(
        user=lining_mgr, employee_id=lining_cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.LINING_CUT)

    t = await DashboardService(db).piece_trace(piece_code=piece.code)
    row = next(h for h in t.history if h.stage == "LINING_CUTTING")
    assert row.consumption is None
    assert t.total_consumption is None, (
        "nothing was measured — that is not the same as measuring zero")


@pytest.mark.asyncio
async def test_an_unknown_piece_code_is_none_not_an_exception(db):
    assert await DashboardService(db).piece_trace(piece_code="NO-SUCH") is None
