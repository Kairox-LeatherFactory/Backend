"""
================================================================================
modules/imports/premint.py — Mint pieces + barcodes at breakdown upload,
                             allocate them into a FIXED, RECYCLING DRAWER POOL
================================================================================

WHAT CHANGED IN THIS BUILD (the drawer pool inversion)
--------------------------------------------------------------------------------
BEFORE
    premint_order minted ONE BRAND-NEW drawer per piece, on every upload, forever.
    Drawers grew unbounded and were never reused; the pool concept did not exist.

NOW
    Drawers are a FIXED POOL that RECYCLES.

      1. BOOTSTRAP once: 200 permanent, barcoded drawers (DRW-0001 … DRW-0200),
         all in state WAITING. Their codes are STATIC and never change. See
         bootstrap_drawer_pool() — run from the seed script exactly once.

      2. On breakdown upload, each new piece is MERGED INTO AN EMPTY (WAITING)
         drawer — oldest seq first — NOT into a fresh drawer.

      3. When the pool has no WAITING drawer left AND there are still pieces to
         place, we MINT the exact shortfall as NEW PERMANENT drawers (barcoded,
         appended to the pool: 200 → 200+N → …). Those new drawers are permanent
         from then on; the next upload reuses them once they recycle. This is
         Hamthan's rule: "if we need 230 more, create 230 more; 200+230=430
         permanent drawers; when all 430 occupied, create more and append."

      4. A drawer returns to WAITING only after the piece it holds finishes
         PACKAGE (drawers/service.release_nocommit, wired into the PACKAGE_EXPORT
         branch of production.log_batch in THIS build — see the note there).

ATOMICITY / SHORTFALL
    A single upload is one atomic write. If free drawers < pieces, we mint the
    shortfall inside the same transaction (mint-the-shortfall, confirmed) rather
    than blocking the upload. The whole allocation commits with the loader.

WHY THIS IS SYNC
    Unchanged from before: the importer runs on a synchronous Session and ends
    with db.commit(); pre-minting must join that same unit of work so an order is
    never half-minted. bootstrap_drawer_pool is ALSO sync for the same reason and
    is safe to call from the seed script's sync session.

IDEMPOTENCY
    Pieces are keyed (sku_id, seq); a re-run tops up only the missing pieces.
    Drawer allocation is driven by "does this piece already have a drawer?": a
    piece that is already merged (piece.drawer_id set) is skipped, so a re-run
    never double-allocates or leaks a drawer.
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

# The size of the initial permanent drawer pool. Business constant.
INITIAL_DRAWER_POOL = 200


# ──────────────────────────────────────────────────────────────────────────────
# needs_lining detection (unchanged from the shipped build)
# ──────────────────────────────────────────────────────────────────────────────
def _sku_needs_lining(sku: SKU) -> bool:
    """Lining detection from the SKU's parsed dimensions.

    Positive evidence only: a lining colour on the SKU means lined; no signal
    means not lined (a DM who knows better corrects the piece). Defaulting to
    True would wedge a leather-only piece's drawer at the completeness gate
    forever, blocking line-stitching for the order.
    """
    for attr in ("knit_color", "nylon_color", "lining_color", "lining_type"):
        val = getattr(sku, attr, None)
        if val and str(val).strip().upper() not in {"", "NA", "N/A", "NONE", "-"}:
            return True
    return False


# ──────────────────────────────────────────────────────────────────────────────
# Pool primitives
# ──────────────────────────────────────────────────────────────────────────────
def _max_drawer_seq(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.max(Drawer.seq), 0))) or 0)


def _drawer_count(db: Session) -> int:
    return int(db.scalar(select(func.count(Drawer.id))) or 0)


def _mint_drawer(db: Session, seq: int) -> Drawer:
    """Create ONE permanent, barcoded drawer in WAITING state. Static code."""
    code = f"DRW-{seq:04d}"
    drawer = Drawer(code=code, seq=seq, state=DrawerState.WAITING.value)
    db.add(drawer)
    db.flush()  # drawer.id
    db.add(BarcodeRegistry(
        code=code, type=BarcodeType.DRAWER.value,
        status=BarcodeStatus.ACTIVE.value, drawer_id=drawer.id,
        caption=f"Drawer {seq}"))
    return drawer


def bootstrap_drawer_pool(db: Session, size: int = INITIAL_DRAWER_POOL) -> dict:
    """Create the initial permanent pool of `size` barcoded WAITING drawers.

    Idempotent and additive: if the pool already has >= size drawers, does
    nothing; if it has fewer, tops up to `size`. Never deletes. Safe to run from
    the seed script on every boot. Caller commits.
    """
    have = _drawer_count(db)
    if have >= size:
        return {"drawers_bootstrapped": 0, "pool_size": have}
    start = _max_drawer_seq(db) + 1
    minted = 0
    for seq in range(start, size + 1):
        _mint_drawer(db, seq)
        minted += 1
    return {"drawers_bootstrapped": minted, "pool_size": max(have + minted, size)}


class _PoolAllocator:
    """Hands out drawers for one upload: empties first, then mints the shortfall.

    Loads the current WAITING drawers ONCE (oldest seq first) and serves them
    in order. When they run out it mints new permanent drawers on demand,
    continuing the seq. All within the caller's transaction.
    """

    def __init__(self, db: Session):
        self.db = db
        # Oldest empty drawers first — deterministic, low-churn reuse.
        self._free: list[Drawer] = list(db.scalars(
            select(Drawer)
            .where(Drawer.state == DrawerState.WAITING.value,
                   Drawer.current_piece_id.is_(None))
            .order_by(Drawer.seq.asc())
        ).all())
        self._next_seq = _max_drawer_seq(db) + 1
        self.reused = 0
        self.minted = 0

    def take(self) -> Drawer:
        if self._free:
            drawer = self._free.pop(0)
            self.reused += 1
        else:
            drawer = _mint_drawer(self.db, self._next_seq)
            self._next_seq += 1
            self.minted += 1
        return drawer


# ──────────────────────────────────────────────────────────────────────────────
# The upload-time mint + allocate
# ──────────────────────────────────────────────────────────────────────────────
def premint_order(db: Session, order) -> dict:
    """Mint pieces + parent barcodes for every SKU of an order and MERGE each new
    piece into a drawer drawn from the pool (empties first, mint shortfall).
    Sync. Caller commits.

    Returns stats merged into the loader's result dict.
    """
    stats = {
        "pieces_minted": 0,
        "drawers_reused": 0,      # taken from existing WAITING pool
        "drawers_minted": 0,      # new permanent drawers appended this upload
        "pieces_needing_lining": 0,
        "sample_barcodes": [],
    }

    sku_rows = db.scalars(
        select(SKU).join(Style, Style.id == SKU.style_id)
        .where(Style.client_order_id == order.id)
    ).all()

    allocator = _PoolAllocator(db)

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
            code = f"{(sku.code or 'NA').upper()}-{seq:03d}"

            # 1) the piece (not yet cut)
            piece = Piece(code=code, seq=seq, sku_id=sku.id,
                          current_operation_id=None)
            if hasattr(piece, "needs_lining"):
                piece.needs_lining = needs_lining
            db.add(piece)
            db.flush()  # piece.id

            # 2) allocate a drawer from the pool (reuse empty, else mint)
            drawer = allocator.take()
            drawer.state = DrawerState.MERGED.value
            drawer.current_piece_id = piece.id
            # A fresh merge starts with neither part in.
            drawer.leather_in = False
            drawer.lining_in = False
            drawer.received_at = None
            drawer.sended_at = None
            if hasattr(piece, "drawer_id"):
                piece.drawer_id = drawer.id

            # 3) register the parent barcode (drawer barcode already exists — it
            #    is permanent and static; we do NOT re-register it on reuse)
            db.add(BarcodeRegistry(
                code=code, type=BarcodeType.PIECE.value,
                status=BarcodeStatus.ACTIVE.value, piece_id=piece.id,
                caption=_piece_caption(db, sku, seq),
                order_id=order.id,
                sku_id=sku.id,
                style_id=sku.style_id,
            ))

            stats["pieces_minted"] += 1
            if needs_lining:
                stats["pieces_needing_lining"] += 1
            if len(stats["sample_barcodes"]) < 5:
                stats["sample_barcodes"].append(code)

    stats["drawers_reused"] = allocator.reused
    stats["drawers_minted"] = allocator.minted
    return stats


def _piece_caption(db: Session, sku: SKU, seq: int) -> str:
    style = db.get(Style, sku.style_id)
    colour = sku.color_name or sku.color_code or "NA"
    parts = [style.name if style else "NA", colour, sku.size or "NA", f"#{seq}"]
    return " · ".join(str(p) for p in parts)