"""
================================================================================
modules/imports/premint.py — Mint pieces + their barcodes when a style is released
================================================================================

WHAT THIS DOES
    For every ordered unit of every SKU in the release, mint the Piece, set its
    `needs_lining` flag from the breakdown, and register its two barcode rows —
    the compact PC- code that gets printed and scanned, and the long human code
    kept as an alias so labels printed before the change still resolve.

THERE IS NO DRAWER, AND THERE IS NO POOL.
    There used to be 200 physical drawers, bootstrapped as barcoded rows and
    handed out one per garment at release. A style releases 100+ garments, so the
    pool ran dry partway down the list and the remainder were minted onto a
    "waiting for a drawer" list. The merge gate then refused to line-stitch them
    — a piece with no drawer could not be proven complete — so a DM had to
    re-allocate boxes by hand, which was involved enough that it did not happen.

    The store is a STATE ON THE GARMENT now (`piece.store_state`, plus
    `leather_in` / `lining_in` / `accessories_in`), and a state has no capacity.
    A freshly minted piece starts at WAITING and enters the store the moment a
    part is scanned into it. Nothing is allocated, so nothing can run out, and
    the scan is two scans — employee, then piece — not three.

WHY THIS IS SYNC
    The importer runs on a synchronous Session and ends with db.commit();
    pre-minting must join that same unit of work so an order is never half-minted.

IDEMPOTENCY
    Pieces are keyed (sku_id, seq); a re-run tops up only the missing pieces and
    never duplicates one.
================================================================================
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import BarcodeStatus, BarcodeType, StoreState
from app.modules.barcode.models import BarcodeRegistry
from app.modules.barcode.repository import (
    SHORT_CODE_PREFIX, decode_short, encode_short,
)
from app.modules.clients.models import SKU, Style
from app.modules.production.models import Piece


# ──────────────────────────────────────────────────────────────────────────────
# needs_lining detection
# ──────────────────────────────────────────────────────────────────────────────
# THE VOCABULARY MOVED to core/lining_rules.py and is imported, not restated.
# It is now read by TWO sides — this importer (which WRITES the flag) and the
# store completeness gate (which decides what may MOVE) — and two copies would
# drift silently: the importer would flag a style the gate did not recognise, or
# the reverse. That drift is the bug where a KNIT jacket flagged False sailed
# through the store to PACKAGE_EXPORT. See core/lining_rules.py.
from app.core.lining_rules import (  # noqa: E402
    LINING_COLOUR_FIELDS, LINING_NAME_MARKERS, is_blank as _blank,
    name_signals_lining,
)


def _sku_needs_lining(sku: SKU, db: Session | None = None) -> bool:
    """Lining detection for one SKU.

    THE DM'S DECLARATION SHORT-CIRCUITS ALL OF THIS. Since the release gate asks
    the question outright, `Style.needs_lining` is normally already answered by
    the time a piece is minted, and that answer is authoritative in both
    directions (core/lining_rules.py). Everything below is the fallback for the
    styles nobody was asked about — pre-release-gate data, and the seed/test
    paths that call premint_order directly.

    POSITIVE EVIDENCE ONLY, and that is deliberate: no signal means NOT lined.
    Defaulting to True would wedge a leather-only piece at the completeness
    gate forever, blocking line-stitching for the whole order.

    TWO SOURCES, checked in order:

      1. An explicit lining colour on the SKU (knit_color / nylon_color). This
         is the intended source — but NOTHING POPULATES IT TODAY. The order
         sheet this importer reads (data/johnpeter.xlsx) has columns
         Date / Style / SUEDE COLOUR / ARTICLE / sizes / TOTAL QTY and no lining
         column at all, and _upsert_sku never writes either field. (lining_color
         and lining_type are not SKU columns at all; the getattr keeps them
         harmless and forward-compatible if they are ever added.)

      2. THE STYLE NAME — the only lining signal the real sheet actually
         carries. 8 of its 17 styles are named ADELE KNIT, FLAVIO KNIT + FUR
         DETACH, FRANCIS KNIT, SHINOBI KNIT, REESE WOOL … and a KNIT style has a
         knit lining by definition.

    WHY THIS MATTERS: on source 1 alone, needs_lining came back False for ALL
    1425 pieces of the real order. Every garment was then complete on leather
    alone, HOLDING_BOTH was unreachable, and the lining half of the merge gate —
    the whole reason the completeness gate exists — never fired once in
    production. Source 2 is what makes it fire.
    """
    # `db` is optional so source 1 stays a pure, session-free predicate (see
    # tests/unit/test_premint_lining_pure.py). Source 2 needs the style row.
    style = db.get(Style, sku.style_id) if db is not None and sku.style_id else None

    # SOURCE 0 — the human answer, when there is one. Checked BEFORE the colour
    # columns because it is the only source that can legitimately say "no" to a
    # style whose sheet carries a knit colour, and the DM has seen the garment.
    declared = getattr(style, "needs_lining", None) if style is not None else None
    if declared is not None:
        return bool(declared)

    for attr in LINING_COLOUR_FIELDS:
        if not _blank(getattr(sku, attr, None)):
            return True

    if style is not None and name_signals_lining(style.name, style.article):
        return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Barcode serial
# ──────────────────────────────────────────────────────────────────────────────
def _max_short_code_counter(db: Session) -> int:
    """Sync twin of BarcodeRepository.max_short_code_counter.

    The importer runs on a synchronous Session (see WHY THIS IS SYNC above) and
    cannot await the repository, so the one read it needs is duplicated here —
    deliberately, and it is one read for the WHOLE upload: premint counts up in
    Python from this base. The encode/decode functions themselves are imported,
    so the alphabet has exactly one definition.
    """
    top = db.scalar(
        select(BarcodeRegistry.code)
        .where(BarcodeRegistry.code.like(f"{SHORT_CODE_PREFIX}-%"))
        .order_by(BarcodeRegistry.code.desc())
        .limit(1)
    )
    return decode_short(top)


# ──────────────────────────────────────────────────────────────────────────────
# INSERT ORDER IS STILL LOAD-BEARING, for one remaining edge.
#
# `barcode_registry.piece_id → piece.id` is a raw ForeignKey COLUMN with no
# relationship() on the mapper. SQLAlchemy's unit of work orders INSERTs from
# mapper RELATIONSHIPS, not from raw FK columns, so it has no idea pieces must
# land before the barcodes that name them and is free to emit barcode_registry
# first. Every FK in this schema is NON-DEFERRABLE (checked per row), so the
# wrong order is an immediate ForeignKeyViolation.
#
# So: INSERT the pieces, flush, THEN add the barcodes. That is two flushes for a
# whole import rather than one per row — which is the point: the per-row
# db.flush() this replaced made a 1400-piece upload ~11,700 round trips.
#
# The drawer phases that used to bracket these are gone with the drawer: there is
# no drawer table to insert before the pieces and no piece↔drawer cycle to close
# with an UPDATE afterwards.
#
# NOTE FOR TESTS: SQLite does not enforce foreign keys unless
# `PRAGMA foreign_keys=ON` is set, so this ordering bug is INVISIBLE on the
# default test harness. tests/integration/test_premint_insert_order.py turns the
# pragma on precisely so it cannot regress unnoticed again.
# ──────────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────────
# The upload-time mint + allocate
# ──────────────────────────────────────────────────────────────────────────────
def premint_order(db: Session, order, *, style_ids=None, **_legacy) -> dict:
    """Mint pieces + their parent barcodes for a release.

    NO LONGER CALLED FROM THE UPLOAD (change-list item 9). An upload writes the
    breakdown and stops; this runs when the DM RELEASES a style, via
    BreakdownService.release_styles. `style_ids` is what scopes it to the styles
    the DM actually picked — omit it and the whole order is minted, which is the
    behaviour the seed scripts and the pre-change tests rely on.

    NOTHING IS ALLOCATED. A piece starts at store_state WAITING and enters the
    store when a part is scanned into it; there is no pool to draw from and
    therefore no shortfall, no waiting list and no `allow_pool_growth`. That
    keyword is swallowed by `**_legacy` rather than removed outright, so a caller
    that has not been updated keeps working instead of dying on a TypeError
    deep inside a release.

    Sync. Caller commits. Idempotent — a re-run tops up only what is missing.
    """
    stats = {
        "pieces_minted": 0,
        "pieces_needing_lining": 0,
        "sample_barcodes": [],
    }

    sku_q = (select(SKU).join(Style, Style.id == SKU.style_id)
             .where(Style.client_order_id == order.id))
    if style_ids:
        sku_q = sku_q.where(Style.id.in_(list(style_ids)))
    sku_rows = db.scalars(sku_q).all()

    new_pieces: list[Piece] = []                  # phase 1
    piece_barcodes: list[BarcodeRegistry] = []    # phase 2
    # ONE read for the whole upload, then count up in Python. Re-reading the max
    # per piece would put a MAX() query back in the inner loop — the exact
    # quadratic shape the four-phase rewrite removed.
    short_counter = _max_short_code_counter(db)

    for sku in sku_rows:
        qty = int(sku.qty_ordered or 0)
        if qty <= 0:
            continue

        # Continue seq from any already-minted pieces (idempotent top-up).
        base = db.scalar(
            select(func.coalesce(func.max(Piece.seq), 0)).where(Piece.sku_id == sku.id)
        ) or 0
        existing = db.scalar(
            select(func.count(Piece.id)).where(Piece.sku_id == sku.id)
        ) or 0
        to_mint = qty - int(existing)
        if to_mint <= 0:
            continue

        needs_lining = _sku_needs_lining(sku, db)
        # Style/colour/size are constant for the whole SKU, so the caption prefix
        # is resolved ONCE here rather than re-read per piece (it was a db.get
        # inside the loop — 1425 lookups for 223 distinct answers).
        caption_prefix = _caption_prefix(db, sku)

        for i in range(1, to_mint + 1):
            seq = base + i
            code = f"{(sku.code or 'NA').upper()}-{seq:03d}"

            # 1) the piece (not yet cut). Explicit id, buffered so the two
            #    phases below can be ordered — see the INSERT ORDER note.
            piece = Piece(id=uuid.uuid4(), code=code, seq=seq, sku_id=sku.id,
                          current_operation_id=None)
            if hasattr(piece, "needs_lining"):
                piece.needs_lining = needs_lining
            new_pieces.append(piece)

            # 2) the store is a STATE, and a state has no capacity. A freshly
            #    minted piece starts at WAITING and enters the store the moment a
            #    part is scanned into it — nothing is allocated, so nothing can
            #    run out. This is where the 200-slot drawer bottleneck used to be.
            piece.store_state = StoreState.WAITING.value

            # 3) register the parent barcodes. Buffered for phase 2: piece.id is
            #    not in the database until phase 1 flushes.
            #
            #    TWO ROWS PER PIECE since bug #19:
            #      • the COMPACT code (PC-…) — the PRIMARY. It is what gets
            #        printed and scanned, and it carries the order/sku/style FKs,
            #        so every per-order count and the history list resolve through
            #        it and see exactly one row per garment.
            #      • the LONG code — an ALIAS. Labels printed before this change
            #        must keep scanning, and the long code is still the piece's
            #        human identity, so it stays in the registry. It carries NO
            #        order/sku/style FK: see BarcodeRegistry.is_alias for why
            #        counting it would double every minted total.
            short_counter += 1
            short_code = encode_short(short_counter)
            caption = f"{caption_prefix} · #{seq}"
            piece_barcodes.append(BarcodeRegistry(
                code=short_code, type=BarcodeType.PIECE.value,
                status=BarcodeStatus.ACTIVE.value, piece_id=piece.id,
                caption=caption,
                order_id=order.id,
                sku_id=sku.id,
                style_id=sku.style_id,
                is_alias=False,
            ))
            piece_barcodes.append(BarcodeRegistry(
                code=code, type=BarcodeType.PIECE.value,
                status=BarcodeStatus.ACTIVE.value, piece_id=piece.id,
                caption=caption,
                is_alias=True,
            ))

            stats["pieces_minted"] += 1
            if needs_lining:
                stats["pieces_needing_lining"] += 1
            if len(stats["sample_barcodes"]) < 5:
                stats["sample_barcodes"].append(short_code)

    # ── the two ordered phases (see the INSERT ORDER note at the top) ────────
    # PHASE 1 — the pieces.
    db.add_all(new_pieces)
    db.flush()

    # PHASE 2 — barcodes last; every piece they name now exists in the
    # transaction. Left pending for the caller's commit to flush.
    db.add_all(piece_barcodes)

    return stats


def _caption_prefix(db: Session, sku: SKU) -> str:
    """'CLERMONT · GOAT SUEDE · PINE GREEN · M' — the part of a piece caption
    that is the same for every piece of a SKU. Resolved once per SKU; the seq is
    appended at the call site.

    THE CAPTION IS WHAT THE STICKER PRINTS. The whole reason the compact `PC-…`
    code exists is that the barcode carries a small unique id while the sticker
    carries the business identity (see barcode/repository.py, SHORT_CODE_PREFIX),
    and that split was introduced because "the client also wants ARTICLE on the
    sticker". The article was then never actually added here — the caption read
    style · colour · size, and the one field the change was made for was the one
    missing from it.

    ARTICLE IS OMITTED WHEN ABSENT rather than printed as 'NA', matching
    make_style_code: a placeholder segment on a physical label is noise a cutter
    has to read past every time.
    """
    style = db.get(Style, sku.style_id)
    colour = sku.color_name or sku.color_code or "NA"
    article = (getattr(style, "article", None) or "").strip() if style else ""
    parts = [style.name if style else "NA"]
    if article:
        parts.append(article)
    parts += [colour, sku.size or "NA"]
    return " · ".join(str(p) for p in parts)