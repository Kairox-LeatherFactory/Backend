"""
INTEGRATION · premint INSERT ORDER, with foreign keys ACTUALLY ENFORCED.

WHY THIS FILE EXISTS
    A production import died with:

        psycopg2.errors.ForeignKeyViolation: insert or update on table
        "barcode_registry" violates foreign key constraint
        "fk_barcode_registry_drawer_id_drawer"
        DETAIL: Key (drawer_id)=(…) is not present in table "drawer".

    and the change that caused it passed the entire existing suite. It passed
    because **SQLite does not enforce foreign keys unless you ask it to** —
    `PRAGMA foreign_keys` defaults to OFF, so the shared harness in
    tests/conftest.py silently accepts rows pointing at parents that do not
    exist. Every FK in the real schema is NON-DEFERRABLE, so Postgres rejects
    exactly what SQLite waved through.

    The root cause is that barcode_registry.drawer_id / piece.drawer_id are RAW
    ForeignKey COLUMNS with no relationship() on the mapper. SQLAlchemy's unit
    of work derives INSERT order from mapper RELATIONSHIPS, so it had no reason
    to write drawers before the rows referencing them.

    These tests use their own engine with the pragma turned ON. That is the
    whole point of the file — do not switch them to the shared `db` fixture.
"""
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy import create_engine

from app.core.database import Base
from app.modules.barcode.models import BarcodeRegistry, Drawer
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.imports.premint import bootstrap_drawer_pool, premint_order
from app.modules.production.models import Piece


@pytest.fixture
def fk_db(tmp_path):
    """A SYNC session on SQLite with `PRAGMA foreign_keys=ON`.

    premint runs on the sync Session inside the importer's transaction, so this
    mirrors production far more closely than an async session would.
    """
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path/'fk.db'}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as s:
        yield s
    engine.dispose()


def _order_with_skus(db: Session, *, order_number: str, qty_per_sku: int,
                     n_skus: int = 3) -> ClientOrder:
    client = Client(name=f"C-{order_number}", country="IT")
    db.add(client); db.flush()
    order = ClientOrder(client_id=client.id, order_number=order_number)
    db.add(order); db.flush()
    style = Style(client_order_id=order.id, name="ADELE KNIT", article="GOAT SUEDE",
                  code=f"{order_number}-ADELE")
    db.add(style); db.flush()
    for i in range(n_skus):
        db.add(SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
                   size=str(38 + i * 2), qty_ordered=qty_per_sku,
                   code=f"{order_number}-ADELE-PINE-{38 + i * 2}"))
    db.commit()
    return order


def test_the_pragma_is_actually_on(fk_db):
    """If this fails, every other test in the file is vacuous."""
    from sqlalchemy import text
    assert fk_db.execute(text("PRAGMA foreign_keys")).scalar() == 1


def test_a_dangling_barcode_is_rejected_here(fk_db):
    """Proves the harness catches the exact production failure — a barcode row
    naming a drawer that does not exist. Under the default (pragma-off) harness
    this INSERT succeeds, which is why the bug shipped."""
    from sqlalchemy.exc import IntegrityError
    fk_db.add(BarcodeRegistry(code="DRW-9999", type="DRAWER", status="active",
                              drawer_id=uuid.uuid4(), caption="ghost"))
    with pytest.raises(IntegrityError):
        fk_db.flush()


def test_premint_commits_with_foreign_keys_enforced(fk_db):
    """The regression itself: a full premint must commit cleanly."""
    order = _order_with_skus(fk_db, order_number="FK-1", qty_per_sku=4)
    stats = premint_order(fk_db, order)
    fk_db.commit()          # this is where the production import blew up

    assert stats["pieces_minted"] == 12
    assert fk_db.scalar(select(func.count(Piece.id))) == 12
    assert fk_db.scalar(select(func.count(Drawer.id))) == 12
    # one PIECE barcode + one DRAWER barcode each
    assert fk_db.scalar(select(func.count(BarcodeRegistry.id))) == 24


def test_every_barcode_points_at_a_row_that_exists(fk_db):
    order = _order_with_skus(fk_db, order_number="FK-2", qty_per_sku=3)
    premint_order(fk_db, order)
    fk_db.commit()

    drawer_ids = set(fk_db.scalars(select(Drawer.id)))
    piece_ids = set(fk_db.scalars(select(Piece.id)))
    for bc in fk_db.scalars(select(BarcodeRegistry)):
        if bc.drawer_id is not None:
            assert bc.drawer_id in drawer_ids, f"{bc.code} names a missing drawer"
        if bc.piece_id is not None:
            assert bc.piece_id in piece_ids, f"{bc.code} names a missing piece"


def test_the_piece_drawer_cycle_is_closed_both_ways(fk_db):
    """piece.drawer_id → drawer.id and drawer.current_piece_id → piece.id are a
    mutual, non-deferrable cycle. Both sides must resolve after commit."""
    order = _order_with_skus(fk_db, order_number="FK-3", qty_per_sku=2)
    premint_order(fk_db, order)
    fk_db.commit()

    pieces = list(fk_db.scalars(select(Piece)))
    assert pieces and all(p.drawer_id is not None for p in pieces)
    for p in pieces:
        drawer = fk_db.get(Drawer, p.drawer_id)
        assert drawer is not None
        assert drawer.current_piece_id == p.id, "the link must point back"
    # one drawer per piece — no sharing
    assert len({p.drawer_id for p in pieces}) == len(pieces)


def test_bootstrap_drawer_pool_orders_its_inserts_too(fk_db):
    """bootstrap mints drawers + barcodes on the same code path."""
    stats = bootstrap_drawer_pool(fk_db, size=25)
    fk_db.commit()
    assert stats["drawers_bootstrapped"] == 25
    assert fk_db.scalar(select(func.count(Drawer.id))) == 25
    drawer_ids = set(fk_db.scalars(select(Drawer.id)))
    for bc in fk_db.scalars(select(BarcodeRegistry)):
        assert bc.drawer_id in drawer_ids


def test_reusing_pooled_drawers_also_commits(fk_db):
    """The other allocator branch: drawers taken from the existing WAITING pool
    are UPDATEd rather than INSERTed, and must still link cleanly."""
    bootstrap_drawer_pool(fk_db, size=10)
    fk_db.commit()

    order = _order_with_skus(fk_db, order_number="FK-4", qty_per_sku=2, n_skus=2)
    stats = premint_order(fk_db, order)
    fk_db.commit()

    assert stats["drawers_reused"] == 4 and stats["drawers_minted"] == 0
    assert fk_db.scalar(select(func.count(Drawer.id))) == 10   # none added
    for p in fk_db.scalars(select(Piece)):
        assert fk_db.get(Drawer, p.drawer_id).current_piece_id == p.id


def test_a_rerun_tops_up_without_duplicating(fk_db):
    """Idempotency must survive the phased inserts."""
    order = _order_with_skus(fk_db, order_number="FK-5", qty_per_sku=3)
    premint_order(fk_db, order); fk_db.commit()
    again = premint_order(fk_db, order); fk_db.commit()

    assert again["pieces_minted"] == 0
    assert fk_db.scalar(select(func.count(Piece.id))) == 9
    assert fk_db.scalar(select(func.count(BarcodeRegistry.id))) == 18
