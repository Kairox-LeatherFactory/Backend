"""
INTEGRATION · one garment, two codes — bug #19 against a real database.

THE PROMISE THIS FILE HOLDS THE SYSTEM TO
    Shrinking the barcode must not break anything already printed. So after the
    change a piece has TWO active registry rows and BOTH resolve to it:

        PC-23456A                    the PRIMARY — printed, scanned, counted
        KJ2451-CLERMONT-57-M-005     the ALIAS  — every old label still works

    The dangerous half is the counting. Every per-order total selects
    `type=PIECE`, so two rows per piece would double `minted`, drive `balance`
    negative and light up the duplicates!=0 integrity alarm the factory
    reconciles against. That is what `is_alias` exists for, and what most of the
    assertions below are actually checking.
"""
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import Base
from app.core.enums import BarcodeType
from app.modules.barcode.models import BarcodeRegistry
from app.modules.barcode.repository import decode_short
from app.modules.barcode.service import BarcodeService
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.imports.premint import premint_order
from app.modules.production.models import Piece

pytestmark = pytest.mark.integrity


@pytest.fixture
def sync_db(tmp_path):
    """A SYNC session, because premint runs on one (see premint's WHY THIS IS
    SYNC note) — the importer's own transaction is the thing under test."""
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path/'compact.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, autoflush=False)() as s:
        yield s
    engine.dispose()


def _seed_and_premint(sync_db: Session, *, qty=4, order_number="JP-PO2"):
    client = sync_db.scalar(select(Client))
    if client is None:
        client = Client(name="John Peter", country="IT")
        sync_db.add(client)
        sync_db.flush()
    order = ClientOrder(client_id=client.id, order_number=order_number)
    sync_db.add(order); sync_db.flush()
    style = Style(client_order_id=order.id, name="CARNABY", article="CB9",
                  code=f"{order_number}-CARNABY", production_status="RELEASED")
    sync_db.add(style); sync_db.flush()
    sync_db.add(SKU(style_id=style.id, color_code="BLK", color_name="BLACK",
                    size="L", qty_ordered=qty, code=f"{order_number}-CARNABY-BLK-L"))
    sync_db.commit()
    stats = premint_order(sync_db, order)
    sync_db.commit()
    return order, stats


# ══════════════════════════════════════════════ both codes resolve
@pytest.mark.asyncio
async def test_the_compact_code_and_the_legacy_code_name_the_same_piece(
    db, pieces, order_tree
):
    """A piece minted before the switch keeps working, and gains a compact code
    when the backfill mints one — with the long code demoted to an alias."""
    piece, _ = pieces[0]
    repo = BarcodeService(db).repo

    # the fixture models a pre-switch piece: long code only, no compact code
    assert await repo.short_codes_for_pieces([piece.id]) == {}
    resolved = await BarcodeService(db).resolve(piece.code)
    assert resolved["piece"]["code"] == piece.code
    assert resolved["piece"]["short_code"] is None, (
        "an un-backfilled piece must report a MISSING compact code, never its "
        "long code dressed up as one")

    # backfill: mint the compact primary, demote the long row
    legacy = await repo.get_by_code(piece.code)
    new = await repo.mint_piece_short_code_nocommit(
        piece.id, caption=legacy.caption, order_id=legacy.order_id,
        sku_id=legacy.sku_id, style_id=legacy.style_id)
    legacy.is_alias = True
    legacy.order_id = legacy.sku_id = legacy.style_id = None
    await db.commit()

    # BOTH codes now resolve, to the same garment
    via_short = await BarcodeService(db).resolve(new.code)
    via_long = await BarcodeService(db).resolve(piece.code)
    assert via_short["piece"]["piece_id"] == via_long["piece"]["piece_id"]
    assert via_short["is_alias"] is False
    assert via_long["is_alias"] is True, (
        "the old label must announce itself as an alias so the UI can suggest a "
        "reprint — it still scans, it is just not the printed code any more")
    assert via_short["piece"]["short_code"] == new.code


@pytest.mark.asyncio
async def test_resolve_narrowing_accepts_either_code(db, pieces):
    piece, _ = pieces[0]
    svc = BarcodeService(db)
    repo = svc.repo
    legacy = await repo.get_by_code(piece.code)
    new = await repo.mint_piece_short_code_nocommit(piece.id, caption=None)
    legacy.is_alias = True
    await db.commit()

    # /production/log resolves targets through this — both doors must work.
    assert await svc.resolve_piece_id(new.code) == piece.id
    assert await svc.resolve_piece_id(piece.code) == piece.id


# ══════════════════════════════════════════════ premint mints both, counts once
def test_premint_mints_a_compact_primary_and_a_long_alias(sync_db):
    order, stats = _seed_and_premint(sync_db, qty=4)

    assert stats["pieces_minted"] == 4
    assert all(c.startswith("PC-") for c in stats["sample_barcodes"]), (
        "the sample the import reports back must be the code that gets printed")

    piece_rows = sync_db.scalars(select(BarcodeRegistry).where(
        BarcodeRegistry.type == BarcodeType.PIECE.value)).all()
    primaries = [r for r in piece_rows if not r.is_alias]
    aliases = [r for r in piece_rows if r.is_alias]

    assert len(primaries) == 4 and len(aliases) == 4
    # one of each per piece, never two of either
    assert len({r.piece_id for r in primaries}) == 4
    assert len({r.piece_id for r in aliases}) == 4

    # the FKs live on the primary only — that is what keeps the counts honest
    assert all(r.order_id == order.id for r in primaries)
    assert all(r.order_id is None and r.sku_id is None and r.style_id is None
               for r in aliases)

    # the alias is the piece's own long code
    long_codes = set(sync_db.scalars(select(Piece.code)))
    assert {r.code for r in aliases} == long_codes

    # compact codes are unique and contiguous
    counters = sorted(decode_short(r.code) for r in primaries)
    assert len(set(counters)) == 4
    assert counters == list(range(counters[0], counters[0] + 4))


def test_a_second_import_continues_the_counter_and_never_reuses_a_code(sync_db):
    _seed_and_premint(sync_db, qty=3, order_number="ORD-A")
    _seed_and_premint(sync_db, qty=3, order_number="ORD-B")

    codes = sync_db.scalars(select(BarcodeRegistry.code).where(
        BarcodeRegistry.type == BarcodeType.PIECE.value,
        BarcodeRegistry.is_alias.is_(False))).all()
    assert len(codes) == 6
    assert len(set(codes)) == 6, "a second import must not restart the counter"


@pytest.mark.asyncio
async def test_order_analytics_counts_garments_not_labels(db, order_tree, pieces):
    """THE REGRESSION THE is_alias FLAG EXISTS TO PREVENT.

    With 5 pieces the order has 5 garments. Adding an alias row per piece must
    not turn that into 10 minted, a negative balance, and duplicates != 0.
    """
    svc = BarcodeService(db)
    order_id = order_tree["order"].id
    # the fixture's rows carry no order_id, so give them one to make this a real
    # per-order count, exactly as premint does.
    for p, _ in pieces:
        row = await svc.repo.get_by_code(p.code)
        row.order_id = order_id
        row.sku_id = p.sku_id
        row.style_id = order_tree["style"].id
    await db.commit()

    before = (await svc.order_analytics(order_id))["order_total"]
    assert before["generated"] == 5 and before["duplicates"] == 0

    # now mint the compact primaries and demote the long rows (the backfill shape)
    for p, _ in pieces:
        legacy = await svc.repo.get_by_code(p.code)
        await svc.repo.mint_piece_short_code_nocommit(
            p.id, caption=legacy.caption, order_id=legacy.order_id,
            sku_id=legacy.sku_id, style_id=legacy.style_id)
        legacy.is_alias = True
        legacy.order_id = legacy.sku_id = legacy.style_id = None
    await db.commit()

    after = (await svc.order_analytics(order_id))["order_total"]
    assert after["generated"] == 5, "aliases must not inflate the minted count"
    assert after["duplicates"] == 0
    assert after["balance"] == before["balance"]

    # ...and the history table lists 5 garments, not 10 labels
    page = await svc.list_history(order_id)
    assert page["total"] == 5
    assert all(row["code"].startswith("PC-") for row in page["items"])


# ══════════════════════════════════════════════ the printed label (bug #7/#19)
@pytest.mark.asyncio
async def test_the_label_carries_the_business_identity_under_a_small_code(
    db, pieces, order_tree
):
    piece, _ = pieces[0]
    svc = BarcodeService(db)
    out = await svc.print_payload(codes=[piece.code])
    label = out["labels"][0]

    assert label["known"] is True and label["symbology"] == "code128"
    d = label["details"]
    assert d["order_number"] == "JP-PO"
    assert d["article"] == order_tree["style"].article      # bug #7 — was missing
    assert d["style"] == "CLERMONT"
    assert d["colour"] == "PINE GREEN"
    assert d["size"] == "M"
    assert d["serial"] == "001"                             # bug #7 — zero-padded
    assert label["label_line"] == "JP-PO · CLERMONT · CL1 · PINE GREEN · M · 001"


def test_printing_a_whole_order_prints_the_compact_codes(sync_db):
    """The print sheet must not expand to the long codes the change removed."""
    order, _ = _seed_and_premint(sync_db, qty=3, order_number="PRINT-1")
    codes = sync_db.scalars(select(BarcodeRegistry.code).where(
        BarcodeRegistry.type == BarcodeType.PIECE.value,
        BarcodeRegistry.is_alias.is_(False))).all()
    assert len(codes) == 3 and all(c.startswith("PC-") for c in codes)
    assert not any(c.startswith("PRINT-1") for c in codes)
