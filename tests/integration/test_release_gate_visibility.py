"""
INTEGRATION · an uploaded breakdown sheet is NOT production until it is released.

THE BUG THIS PINS
    Uploading a breakdown sheet immediately added its ordered quantity to the
    dashboard. Releasing the same sheet then minted its pieces and added those
    too, so a 1,425-piece sheet read as 1,425 before release and ~2,850 after —
    the same garments counted twice, in two figures that had silently stopped
    describing the same population:

        total_order_pieces  = SUM(SKU.qty_ordered)  -- existed from upload
        minted_pieces       = COUNT(piece)          -- exists only after release

    Uploading is not production. A DRAFT sheet mints nothing, consumes no
    drawers and has no scanned work, so it must be invisible to every service
    that reports on the factory — dashboard, payroll, analytics. The one shared
    predicate is `clients.models.style_in_production()`; anything that counts
    work applies it, so the two figures above can never drift apart again.

CANCELLED IS HIDDEN, NOT DELETED
    A cancelled style drops out of the live counts (otherwise pulled work
    inflates the floor's targets forever) while its production events and wage
    lines stay exactly where they are.
"""
from datetime import date

import pytest

from app.core.enums import ProductionReleaseStatus
from app.modules.clients import models as cm
from app.modules.dashboard.repository import DashboardRepository
from app.modules.production import models as pm


async def _uploaded_sheet(db, *, qty: int):
    """What a breakdown upload leaves behind: styles + SKUs, DRAFT, no pieces."""
    client = cm.Client(name="RelCo")
    db.add(client)
    await db.flush()
    order = cm.ClientOrder(client_id=client.id, order_number="REL-1")
    db.add(order)
    await db.flush()
    style = cm.Style(client_order_id=order.id, name="CLERMONT", code="CLERMONT")
    db.add(style)
    await db.flush()
    sku = cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=qty,
                 code="REL-1-CLERMONT-57-M")
    db.add(sku)
    await db.flush()
    await db.commit()
    # The default is what the release gate depends on — assert it, don't assume.
    assert style.production_status == ProductionReleaseStatus.DRAFT.value
    return dict(order=order, style=style, sku=sku)


async def _release(db, world, *, qty: int):
    """What release does: flip the status and mint the pieces."""
    world["style"].production_status = ProductionReleaseStatus.RELEASED.value
    for i in range(1, qty + 1):
        db.add(pm.Piece(code=f"PC-{i:05d}", seq=i, sku_id=world["sku"].id,
                        is_active=True))
    await db.commit()


@pytest.mark.asyncio
async def test_a_draft_sheet_is_invisible_to_the_dashboard(db):
    await _uploaded_sheet(db, qty=20)
    kpis = await DashboardRepository(db).production_kpis(
        today=date.today(), client_scope=None)
    assert kpis["total_order_pieces"] == 0, "a DRAFT sheet leaked into the dashboard"
    assert kpis["minted_pieces"] == 0


@pytest.mark.asyncio
async def test_releasing_counts_the_garments_exactly_once(db):
    """The double-count regression, stated as a number.

    Both figures must land on the SAME 20 — not 20 then 40.
    """
    world = await _uploaded_sheet(db, qty=20)
    repo = DashboardRepository(db)

    before = await repo.production_kpis(today=date.today(), client_scope=None)
    await _release(db, world, qty=20)
    after = await repo.production_kpis(today=date.today(), client_scope=None)

    assert (before["total_order_pieces"], before["minted_pieces"]) == (0, 0)
    assert (after["total_order_pieces"], after["minted_pieces"]) == (20, 20)
    # The two columns describe one population, so they agree. That equality IS
    # the fix: previously one counted from upload and the other from release.
    assert after["total_order_pieces"] == after["minted_pieces"]


@pytest.mark.asyncio
async def test_a_cancelled_style_leaves_the_counts_but_keeps_its_rows(db):
    world = await _uploaded_sheet(db, qty=10)
    await _release(db, world, qty=10)
    repo = DashboardRepository(db)
    assert (await repo.production_kpis(
        today=date.today(), client_scope=None))["total_order_pieces"] == 10

    world["style"].production_status = ProductionReleaseStatus.CANCELLED.value
    await db.commit()

    gone = await repo.production_kpis(today=date.today(), client_scope=None)
    assert gone["total_order_pieces"] == 0
    assert gone["minted_pieces"] == 0

    # Hidden from the counts, still on disk: history is never deleted.
    from sqlalchemy import func, select
    surviving = await db.scalar(select(func.count(pm.Piece.id)))
    assert surviving == 10


@pytest.mark.asyncio
async def test_an_order_whose_styles_are_all_draft_still_resolves(db):
    """order_head applies the predicate in its ON clause, not a WHERE.

    As a WHERE it would turn the outer join inner and make the order vanish,
    404ing a page that should render an order with an empty funnel.
    """
    world = await _uploaded_sheet(db, qty=15)
    head = await DashboardRepository(db).order_head(order_id=world["order"].id)
    assert head is not None, "an all-DRAFT order disappeared instead of showing 0"
    order_number, qty = head
    assert order_number == "REL-1"
    assert qty == 0


@pytest.mark.asyncio
async def test_the_payroll_landing_screen_only_offers_released_styles(db):
    """A DRAFT style has no scanned work, so pricing it prices nothing.

    Covers the wages side of the same gate: WageService.list_styles /
    list_orders both read clients.list_style_options, which now carries the
    predicate.
    """
    from app.modules.wages.service import WageService

    world = await _uploaded_sheet(db, qty=12)
    svc = WageService(db)

    assert await svc.list_styles() == [], "a DRAFT style reached the rate screen"
    assert await svc.list_orders() == []

    await _release(db, world, qty=12)
    styles = await svc.list_styles()
    assert [s["style_code"] for s in styles] == ["CLERMONT"]
    orders = await svc.list_orders()
    assert [o["order_number"] for o in orders] == ["REL-1"]
