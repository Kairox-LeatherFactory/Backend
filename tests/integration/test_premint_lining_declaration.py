"""
================================================================================
tests/integration/test_premint_lining_declaration.py
    The DM's declaration must reach the PIECES it mints.
================================================================================

WHY THIS IS ITS OWN FILE, ON THE SYNC HARNESS

    The declaration is only worth anything if it survives the mint. `Style.
    needs_lining` is stamped at release, and `premint_order` copies it onto every
    piece it creates (`_sku_needs_lining`) — a sync function, running inside the
    importer's own transaction, so it needs the sync session fixture the other
    premint tests use rather than the async one.

THE ORDERING TRAP THIS GUARDS
    In `BreakdownService._release_sync` the declaration is written and FLUSHED
    BEFORE premint runs. Write it afterwards and premint's `db.get(Style, …)`
    reads the pre-write value, minting the whole style against the previous
    answer — pieces whose flag disagrees with the style they belong to, which is
    exactly the stale-flag class of bug that caused the lining bypass. The last
    test in this file pins that ordering.

WHAT premint STILL INFERS
    A style nobody declared (NULL) keeps the old name/colour inference, so the
    seed scripts, the importer tests, and every style released before the release
    gate existed behave exactly as they did.
================================================================================
"""
import uuid

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.imports.premint import bootstrap_drawer_pool, premint_order
from app.modules.production.models import Piece


@pytest.fixture
def sdb(tmp_path):
    """Sync session with FK enforcement — premint's real execution shape."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path/'lin.db'}")

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as s:
        bootstrap_drawer_pool(s, size=20)
        s.commit()
        yield s
    engine.dispose()


def _order(db: Session, *, tag: str, style_name: str,
           declared: bool | None, knit_color: str | None = None) -> ClientOrder:
    client = Client(name=f"C-{tag}", country="IT")
    db.add(client); db.flush()
    order = ClientOrder(client_id=client.id, order_number=f"ORD-{tag}")
    db.add(order); db.flush()
    style = Style(client_order_id=order.id, name=style_name, article="ART",
                  code=f"STY-{tag}")
    style.needs_lining = declared
    db.add(style); db.flush()
    db.add(SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
               size="M", qty_ordered=3, code=f"SKU-{tag}",
               knit_color=knit_color))
    db.commit()
    return order


def _flags(db: Session, order: ClientOrder) -> list[bool]:
    return [p.needs_lining for p in db.scalars(
        __import__("sqlalchemy").select(Piece)
        .join(SKU, SKU.id == Piece.sku_id)
        .join(Style, Style.id == SKU.style_id)
        .where(Style.client_order_id == order.id)).all()]


def test_a_declared_no_overrides_a_knit_style_name(sdb):
    """The negative direction reaches the pieces, not just the style row."""
    order = _order(sdb, tag="p001", style_name="ADELE KNIT", declared=False)
    stats = premint_order(sdb, order, allow_pool_growth=True)
    sdb.commit()

    assert stats["pieces_minted"] == 3
    assert stats["pieces_needing_lining"] == 0
    assert _flags(sdb, order) == [False, False, False], (
        "pieces were minted lined despite the DM declaring the style leather-only")


def test_a_declared_no_overrides_even_a_lining_colour(sdb):
    """A knit COLOUR on the SKU is the strongest inference signal there is, and
    the declaration still outranks it — the DM has seen the garment."""
    order = _order(sdb, tag="p002", style_name="CLERMONT", declared=False,
                   knit_color="ECRU")
    premint_order(sdb, order, allow_pool_growth=True)
    sdb.commit()
    assert _flags(sdb, order) == [False, False, False]


def test_a_declared_yes_lines_a_plainly_named_style(sdb):
    """The positive direction: the DM adds a requirement nothing could infer."""
    order = _order(sdb, tag="p003", style_name="CLERMONT", declared=True)
    stats = premint_order(sdb, order, allow_pool_growth=True)
    sdb.commit()

    assert stats["pieces_needing_lining"] == 3
    assert _flags(sdb, order) == [True, True, True]


def test_an_undeclared_style_keeps_the_old_inference(sdb):
    """NULL changes nothing — the seed scripts and pre-gate data are untouched."""
    order = _order(sdb, tag="p004", style_name="REESE WOOL", declared=None)
    premint_order(sdb, order, allow_pool_growth=True)
    sdb.commit()
    assert _flags(sdb, order) == [True, True, True], (
        "an unanswered style stopped inferring its lining from the style name")


def test_the_declaration_must_be_flushed_before_the_mint(sdb):
    """THE ORDERING GUARD (BreakdownService._release_sync).

    Models the release sequence exactly: stamp the answer, flush, THEN mint. If a
    future edit moves the stamp after premint_order, the pieces come out carrying
    the previous answer and this test fails — which is the only way that mistake
    is visible, since the style row itself would look perfectly correct.
    """
    order = _order(sdb, tag="p005", style_name="ADELE KNIT", declared=None)

    style = sdb.scalars(
        __import__("sqlalchemy").select(Style)
        .where(Style.client_order_id == order.id)).one()
    style.needs_lining = False          # the DM answers at release
    sdb.flush()                          # ← the load-bearing line

    premint_order(sdb, order, allow_pool_growth=True)
    sdb.commit()

    assert _flags(sdb, order) == [False, False, False], (
        "the mint read the style's pre-declaration value — the flush ordering in "
        "_release_sync has regressed")
