"""
================================================================================
modules/imports/premint.py — Mint pieces + barcodes + drawers at breakdown upload
================================================================================

THE INVERSION.
    Before this build, pieces were minted at cutting. Now they are minted HERE,
    at breakdown upload, so a garment has a barcode identity and a drawer before
    it is ever cut. cut() stops minting and becomes a scan-style logger (see
    production/service.py).

WHY THIS IS SYNC (not async like the rest of the feature).
    The importer (imports/load_to_db.py) runs on a synchronous Session and ends
    with db.commit(). Pre-minting must happen in that SAME session, right before
    the commit, so the SKUs, pieces, barcodes and drawers are one atomic write —
    a half-minted order (SKUs but no pieces) would be un-cuttable. So this module
    speaks the sync Session API and is CALLED FROM the loader, not from an async
    service.

CALL SITE (add ONE line to imports/load_to_db.py, in BOTH load_preview and
load_preview_into_order, immediately before the final `db.commit()`):

        from app.modules.imports.premint import premint_order
        stats.update(premint_order(db, order))     # <-- add this line

`order` is the ClientOrder already in scope in both loaders.

NEEDS_LINING.
    Read from the breakdown material dimension. johnpeter SKUs carry a knit/nylon
    lining colour; a SKU with any lining colour → its pieces need lining. When the
    sheet gives no lining signal we default needs_lining=True (the safe default:
    the completeness gate then waits for a lining, and a DM override can clear it),
    EXCEPT where the style/material is explicitly leather-only. Tune
    _sku_needs_lining as the material column is better understood.

IDEMPOTENCY.
    The loaders run with replace=True, deleting a client's prior styles/SKUs
    first. Pieces/drawers/barcodes hang off SKUs; when a SKU is deleted its pieces
    cascade. This module only mints for pieces that do NOT already exist (keyed by
    (sku_id, seq)), so a re-run of a partially-minted order tops up rather than
    duplicating.
================================================================================
"""
from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import BarcodeStatus, BarcodeType, DrawerState
from app.modules.barcode.models import BarcodeRegistry, Drawer
from app.modules.clients.models import SKU, Style
from app.modules.production.models import Piece


def _sku_needs_lining(sku: SKU) -> bool:
    """Lining detection from the SKU's parsed dimensions.

    H9: this used to `return True` on BOTH branches, so every piece was minted
    needing a lining. needs_lining drives the merge gate's completeness rule
    (drawers/service.py:141-146), so a leather-only garment could never reach
    RECEIVED — its drawer waited forever for a lining nobody would cut, and
    line-stitching stayed blocked for the whole order.

    Defaulting to True is not the conservative choice for a gate that BLOCKS.
    We now require positive evidence: a lining colour on the SKU means lined;
    no signal means not lined, and a DM who knows better can correct the piece.
    """
    for attr in ("knit_color", "nylon_color", "lining_color", "lining_type"):
        val = getattr(sku, attr, None)
        if val and str(val).strip().upper() not in {"", "NA", "N/A", "NONE", "-"}:
            return True
    return False


def _next_drawer_seq(db: Session) -> int:
    mx = db.scalar(select(func.coalesce(func.max(Drawer.seq), 0))) or 0
    return int(mx) + 1


def premint_order(db: Session, order) -> dict:
    """Mint pieces + parent barcodes + drawer assignments for every SKU of an
    order. Sync. Caller commits.

    Returns stats merged into the loader's result dict.
    """
    stats = {"pieces_minted": 0, "drawers_assigned": 0,
             "pieces_needing_lining": 0, "sample_barcodes": []}

    # All SKUs of this order, via its styles.
    sku_rows = db.scalars(
        select(SKU).join(Style, Style.id == SKU.style_id)
        .where(Style.client_order_id == order.id)
    ).all()

    drawer_seq = _next_drawer_seq(db)

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

        needs_lining = _sku_needs_lining(sku)

        for i in range(1, to_mint + 1):
            seq = base + i
            # Parent code = SKU code + seq. SKU.code already encodes
            # order-style-colour-size, so this is STYLE-COLOUR-SIZE-seq, globally
            # unique and human-readable.
            code = f"{(sku.code or 'NA').upper()}-{seq:03d}"

            # 1) a drawer for this piece (one piece = one drawer)
            drawer = Drawer(
                code=f"DRW-{drawer_seq:04d}", seq=drawer_seq,
                state=DrawerState.MERGED.value)
            db.add(drawer)
            db.flush()   # drawer.id

            # 2) the piece, merged to that drawer
            piece = Piece(
                code=code, seq=seq, sku_id=sku.id,
                current_operation_id=None,        # not yet cut
            )
            # set the barcode-build columns if present on the model
            if hasattr(piece, "needs_lining"):
                piece.needs_lining = needs_lining
            if hasattr(piece, "drawer_id"):
                piece.drawer_id = drawer.id
            db.add(piece)
            db.flush()   # piece.id

            drawer.current_piece_id = piece.id

            # 3) register the parent + drawer barcodes
            db.add(BarcodeRegistry(
                code=code, type=BarcodeType.PIECE.value,
                status=BarcodeStatus.ACTIVE.value, piece_id=piece.id,
                caption=_piece_caption(db, sku, seq)))
            db.add(BarcodeRegistry(
                code=drawer.code, type=BarcodeType.DRAWER.value,
                status=BarcodeStatus.ACTIVE.value, drawer_id=drawer.id,
                caption=f"Drawer {drawer.seq}"))

            stats["pieces_minted"] += 1
            stats["drawers_assigned"] += 1
            if needs_lining:
                stats["pieces_needing_lining"] += 1
            if len(stats["sample_barcodes"]) < 5:
                stats["sample_barcodes"].append(code)

            drawer_seq += 1

    return stats


def _piece_caption(db: Session, sku: SKU, seq: int) -> str:
    style = db.get(Style, sku.style_id)
    colour = sku.color_name or sku.color_code or "NA"
    parts = [style.name if style else "NA", colour, sku.size or "NA", f"#{seq}"]
    return " · ".join(str(p) for p in parts)