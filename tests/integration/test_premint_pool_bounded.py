"""
INTEGRATION · THE DRAWER POOL IS FINITE, AND THE OVERFLOW IS A WAITING LIST.
    (change-list item 9 — the drawer-model half)

WHAT CHANGED AND WHY
    premint used to mint a brand-new permanent drawer every time the pool ran
    dry, so "200 static drawers" was true only until the first big order. The
    client's rule is the opposite: 200 drawers; when the pieces outrun them the
    remainder WAIT, and growing the pool is a DM/MD decision.

    A waiting piece is NOT broken and NOT lost. It has its barcode and its
    identity; it simply has no drawer, so it cannot be stored and therefore
    cannot pass the merge gate. That is a visible, explainable stall instead of
    a pool that silently grows to 4,000 drawers nobody built shelves for.

WHAT THIS FILE PINS
    1. A release bigger than the pool mints every piece and leaves the surplus
       drawer-less, reported honestly in the stats.
    2. Growing the pool and draining the waiting list places them.
    3. `allow_pool_growth=True` restores mint-the-shortfall for the callers that
       are entitled to it.
    4. drawer_pool_status tells the DM exactly how many drawers to add.

Uses its own FK-enforcing sync session, same reasoning as
test_premint_insert_order.py: premint runs on a sync Session inside the
importer's transaction, and SQLite waves through FK violations unless asked not
to.
"""
import pytest
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.modules.barcode.models import Drawer
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.imports.premint import (allocate_waiting_pieces,
                                         bootstrap_drawer_pool,
                                         drawer_pool_status, grow_drawer_pool,
                                         premint_order)
from app.modules.production.models import Piece


@pytest.fixture
def fk_db(tmp_path):
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path/'pool.db'}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as s:
        yield s
    engine.dispose()


def _order(db: Session, *, order_number: str, qty: int) -> ClientOrder:
    client = Client(name=f"C-{order_number}", country="IT")
    db.add(client); db.flush()
    order = ClientOrder(client_id=client.id, order_number=order_number)
    db.add(order); db.flush()
    style = Style(client_order_id=order.id, name="CLERMONT", article="CL1",
                  code=f"{order_number}-CLERMONT", production_status="RELEASED")
    db.add(style); db.flush()
    db.add(SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
               size="M", qty_ordered=qty, code=f"{order_number}-CLERMONT-PINE-M"))
    db.commit()
    return order


def test_a_release_bigger_than_the_pool_leaves_the_surplus_waiting(fk_db):
    """10 pieces, 4 drawers → 4 merged, 6 waiting, and NOTHING silently minted."""
    bootstrap_drawer_pool(fk_db, size=4)
    fk_db.commit()
    order = _order(fk_db, order_number="POOL-1", qty=10)

    stats = premint_order(fk_db, order)
    fk_db.commit()

    assert stats["pieces_minted"] == 10, "pieces must still be minted"
    assert stats["drawers_reused"] == 4
    assert stats["drawers_minted"] == 0, (
        "the pool grew without anyone authorising it")
    assert stats["pieces_waiting_for_drawer"] == 6

    assert fk_db.scalar(select(func.count(Drawer.id))) == 4
    unmerged = fk_db.scalar(
        select(func.count(Piece.id)).where(Piece.drawer_id.is_(None)))
    assert unmerged == 6


def test_growing_the_pool_then_draining_places_the_waiting_pieces(fk_db):
    """The DM adds drawers; the waiting list empties into them."""
    bootstrap_drawer_pool(fk_db, size=4)
    fk_db.commit()
    order = _order(fk_db, order_number="POOL-2", qty=10)
    premint_order(fk_db, order)
    fk_db.commit()

    status = drawer_pool_status(fk_db)
    assert status["pieces_waiting_for_drawer"] == 6
    # The number the DM needs to see: how many drawers to add right now.
    assert status["shortfall"] == 6

    grow_drawer_pool(fk_db, status["shortfall"])
    drained = allocate_waiting_pieces(fk_db)
    fk_db.commit()

    assert drained["allocated"] == 6
    assert drained["still_waiting"] == 0
    assert fk_db.scalar(
        select(func.count(Piece.id)).where(Piece.drawer_id.is_(None))) == 0
    # Every new drawer is PERMANENT and barcoded — the pool only ever grows.
    assert fk_db.scalar(select(func.count(Drawer.id))) == 10


def test_allow_pool_growth_restores_mint_the_shortfall(fk_db):
    """The authorised path still mints the shortfall in one step."""
    bootstrap_drawer_pool(fk_db, size=4)
    fk_db.commit()
    order = _order(fk_db, order_number="POOL-3", qty=10)

    stats = premint_order(fk_db, order, allow_pool_growth=True)
    fk_db.commit()

    assert stats["drawers_minted"] == 6
    assert stats["pieces_waiting_for_drawer"] == 0
    assert fk_db.scalar(
        select(func.count(Piece.id)).where(Piece.drawer_id.is_(None))) == 0


def test_style_ids_scopes_the_mint_to_the_released_styles(fk_db):
    """Release is per style: an unreleased style must mint nothing.

    This is the half of item 9 that makes the release gate real — without
    `style_ids`, "the DM picked 3 of 17 styles" would still mint all 17.
    """
    bootstrap_drawer_pool(fk_db, size=50)
    fk_db.commit()

    client = Client(name="C-SCOPE", country="IT")
    fk_db.add(client); fk_db.flush()
    order = ClientOrder(client_id=client.id, order_number="SCOPE-1")
    fk_db.add(order); fk_db.flush()
    keep = Style(client_order_id=order.id, name="CLERMONT", code="SC-CLERMONT", production_status="RELEASED")
    skip = Style(client_order_id=order.id, name="CARNABY", code="SC-CARNABY", production_status="RELEASED")
    fk_db.add_all([keep, skip]); fk_db.flush()
    fk_db.add(SKU(style_id=keep.id, color_code="P", color_name="PINE", size="M",
                  qty_ordered=3, code="SC-CLERMONT-P-M"))
    fk_db.add(SKU(style_id=skip.id, color_code="B", color_name="BLACK", size="M",
                  qty_ordered=7, code="SC-CARNABY-B-M"))
    fk_db.commit()

    stats = premint_order(fk_db, order, style_ids=[keep.id])
    fk_db.commit()

    assert stats["pieces_minted"] == 3, "the unreleased style was minted too"
    assert fk_db.scalar(select(func.count(Piece.id))) == 3
