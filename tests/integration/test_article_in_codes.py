"""
INTEGRATION · the ARTICLE reaches the style code, the SKU code, the piece code
              and the printed sticker — and the barcode stays a small unique id.

WHAT THE TWO IDENTIFIERS ARE FOR (they are not duplicates)

    the BARCODE   PC-23456A      a short, unique, scan-friendly id. Base-30,
                                 fixed width, no 0/O/1/I so a smudged label can
                                 be re-keyed. It encodes NOTHING about the
                                 garment — it is a pointer.

    the PIECE CODE JP-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-005
                                 the business identity a human reads: order,
                                 style, article, colour, size, serial.

    Both live in barcode_registry pointing at the SAME piece: the compact code as
    the PRIMARY row (is_alias=False, carries the order/sku/style FKs) and the long
    code as an ALIAS (is_alias=True, no FKs, so it never double-counts a mint).
    Scanning EITHER resolves to the same garment, and the resolve payload returns
    both — `short_code` and `code`.

BACKWARD COMPATIBILITY IS PART OF THE CONTRACT
    A style with no article must produce exactly the code it produced before the
    article parameter existed, so nothing already printed shifts.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import Base
from app.core.enums import BarcodeType
from app.modules.barcode.models import BarcodeRegistry
from app.modules.clients.utlis import make_sku_code, make_style_code


@pytest.fixture
def sync_db(tmp_path):
    """A SYNC session — premint runs inside the importer's own transaction."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path/'article.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as s:
        yield s
    engine.dispose()


# ── the code makers ──────────────────────────────────────────────────────────

def test_the_article_lands_in_the_style_and_sku_codes():
    style = make_style_code("JP", "CLERMONT + VEST", "GOAT SUEDE")
    sku = make_sku_code("JP", "CLERMONT + VEST", "DARK BROWN", "46", "GOAT SUEDE")
    assert style == "JP-CLERMONT_VEST-GOAT_SUEDE"
    assert sku == "JP-CLERMONT_VEST-GOAT_SUEDE-DARK_BROWN-46"


def test_the_style_code_is_still_the_exact_prefix_of_its_sku_codes():
    """A manager reads the style code off the front of a traveler. Composing
    make_sku_code ON make_style_code makes that true by construction."""
    style = make_style_code("JP", "CLERMONT", "GOAT SUEDE")
    sku = make_sku_code("JP", "CLERMONT", "DARK BROWN", "46", "GOAT SUEDE")
    assert sku.startswith(style + "-")


def test_no_article_produces_the_pre_change_code_unchanged():
    """The compatibility guarantee: no 'NA' segment, no shift."""
    assert make_style_code("JP", "CLERMONT + VEST") == "JP-CLERMONT_VEST"
    assert (make_sku_code("JP", "CLERMONT + VEST", "DARK BROWN", "46")
            == "JP-CLERMONT_VEST-DARK_BROWN-46")
    # blank / whitespace-only is the same as absent, never a segment
    assert make_style_code("JP", "CLERMONT", "   ") == "JP-CLERMONT"


def test_there_is_only_one_code_maker():
    """clients/service.py used to keep a byte-identical second copy, and
    imports/load_to_db.py imported THAT one. A format change would then have
    applied to half the system."""
    from app.modules.clients import service, utlis
    assert service.make_sku_code is utlis.make_sku_code
    assert service.make_style_code is utlis.make_style_code


# ── end to end: upload → release → sticker ───────────────────────────────────

def _sheet_with_article(sync_db, *, order_number="JP", article="GOAT SUEDE"):
    from app.modules.imports.load_to_db import (
        _get_or_create_client, _get_or_create_order, _get_or_create_style,
        _upsert_sku,
    )
    client = _get_or_create_client(sync_db, "ArticleCo", None)
    order = _get_or_create_order(sync_db, client, order_number)
    style = _get_or_create_style(sync_db, order, "CLERMONT", article)
    sku, _ = _upsert_sku(sync_db, order, style, "DARK BROWN", "46", 3)
    sync_db.commit()
    return order, style, sku


def test_the_importer_puts_the_article_in_both_codes(sync_db):
    _order, style, sku = _sheet_with_article(sync_db)
    assert style.code == "JP-CLERMONT-GOAT_SUEDE"
    assert sku.code == "JP-CLERMONT-GOAT_SUEDE-DARK_BROWN-46"
    assert style.article == "GOAT SUEDE"


def test_release_mints_piece_codes_carrying_the_article(sync_db):
    """The piece code is `{sku_code}-{seq:03d}`, so the article arrives for free."""
    from app.modules.imports.premint import premint_order

    order, _style, _sku = _sheet_with_article(sync_db)
    premint_order(sync_db, order, allow_pool_growth=True)
    sync_db.commit()

    from sqlalchemy import select
    from app.modules.production.models import Piece
    codes = sorted(sync_db.scalars(select(Piece.code)).all())
    assert codes == [
        "JP-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-001",
        "JP-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-002",
        "JP-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-003",
    ]


def test_the_sticker_caption_names_the_article(sync_db):
    """The compact barcode exists precisely so the sticker can carry the article;
    the caption had never actually been given it."""
    from sqlalchemy import select
    from app.modules.imports.premint import premint_order

    order, _style, _sku = _sheet_with_article(sync_db)
    premint_order(sync_db, order, allow_pool_growth=True)
    sync_db.commit()

    captions = set(sync_db.scalars(
        select(BarcodeRegistry.caption)
        .where(BarcodeRegistry.type == BarcodeType.PIECE.value)).all())
    assert "CLERMONT · GOAT SUEDE · DARK BROWN · 46 · #1" in captions


def test_every_piece_has_a_short_barcode_and_its_long_code_as_an_alias(sync_db):
    """The two-row shape: one scannable unique id, one human identity, one piece.

    The alias carries NO order/sku/style FK — that is what stops the per-order
    `minted` count reading double.
    """
    from sqlalchemy import select
    from app.modules.imports.premint import premint_order
    from app.modules.production.models import Piece

    order, _style, _sku = _sheet_with_article(sync_db)
    premint_order(sync_db, order, allow_pool_growth=True)
    sync_db.commit()

    piece = sync_db.scalars(select(Piece)).first()
    rows = sync_db.scalars(select(BarcodeRegistry).where(
        BarcodeRegistry.piece_id == piece.id)).all()
    assert len(rows) == 2

    primary = next(r for r in rows if not r.is_alias)
    alias = next(r for r in rows if r.is_alias)

    assert primary.code.startswith("PC-"), "the scanned code must be the short id"
    assert len(primary.code) == len("PC-") + 6
    assert primary.order_id is not None and primary.sku_id is not None

    assert alias.code == piece.code, "the alias is the full human piece code"
    assert "GOAT_SUEDE" in alias.code
    assert alias.order_id is None, "an alias must never be counted as a mint"
