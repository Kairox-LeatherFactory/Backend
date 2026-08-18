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
from app.modules.barcode.repository import (
    SHORT_CODE_PREFIX, decode_short, encode_short,
)
from app.modules.clients.models import SKU, Style
from app.modules.production.models import Piece

# The size of the initial permanent drawer pool. Business constant.
INITIAL_DRAWER_POOL = 200


# ──────────────────────────────────────────────────────────────────────────────
# needs_lining detection
# ──────────────────────────────────────────────────────────────────────────────
# THE VOCABULARY MOVED to core/lining_rules.py and is imported, not restated.
# It is now read by TWO sides — this importer (which WRITES the flag) and the
# drawers completeness gate (which decides what may MOVE) — and two copies would
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
    Defaulting to True would wedge a leather-only piece's drawer at the
    completeness gate forever, blocking line-stitching for the whole order.

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
    1425 pieces of the real order. Every drawer was then complete on leather
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
# Pool primitives
# ──────────────────────────────────────────────────────────────────────────────
def _max_drawer_seq(db: Session) -> int:
    return int(db.scalar(select(func.coalesce(func.max(Drawer.seq), 0))) or 0)


def _drawer_count(db: Session) -> int:
    return int(db.scalar(select(func.count(Drawer.id))) or 0)


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
# INSERT ORDER IS LOAD-BEARING — read this before touching the flushes below.
#
# barcode_registry.drawer_id → drawer.id and piece.drawer_id → drawer.id are RAW
# ForeignKey COLUMNS with NO relationship() on the mapper. SQLAlchemy's unit of
# work orders INSERTs from mapper RELATIONSHIPS, not from raw FK columns, so it
# has no idea drawers must land before the rows that reference them and is free
# to emit barcode_registry first. Every FK in this schema is NON-DEFERRABLE
# (checked per row), so wrong order is an immediate ForeignKeyViolation:
#
#     insert or update on table "barcode_registry" violates foreign key
#     constraint "fk_barcode_registry_drawer_id_drawer"
#
# piece.drawer_id → drawer.id and drawer.current_piece_id → piece.id also form a
# MUTUAL CYCLE, so no single insert order satisfies both — the link has to be an
# UPDATE after both rows exist.
#
# The order below is therefore explicit and phased:
#     1. INSERT drawers      (current_piece_id still NULL)
#     2. INSERT pieces       (drawer_id now resolves)
#     3. UPDATE drawers      (current_piece_id now resolves)
#     4. INSERT barcodes     (both parents now resolve)
#
# That is FOUR flushes for a whole import, not one per row — which is the point:
# the per-row db.flush() this replaced was what made a 1400-piece upload ~11,700
# round trips. Do not "simplify" these into a single flush.
#
# NOTE FOR TESTS: SQLite does not enforce foreign keys unless
# `PRAGMA foreign_keys=ON` is set, so this ordering bug is INVISIBLE on the
# default test harness. tests/integration/test_premint_insert_order.py turns the
# pragma on precisely so it cannot regress unnoticed again.
# ──────────────────────────────────────────────────────────────────────────────
def _build_drawer(seq: int, barcode_sink: list) -> Drawer:
    """Build ONE permanent drawer + its barcode row. Adds NOTHING to the session.

    The id is assigned here rather than discovered by a flush: `UUIDMixin.id` is
    a PYTHON-side default (core/models.py:68-71) that SQLAlchemy only fills AT
    flush time, which is why this used to flush just to learn drawer.id. Matches
    the project rule that ids come from the app (CLAUDE.md §13).
    """
    code = f"DRW-{seq:04d}"
    drawer = Drawer(id=uuid.uuid4(), code=code, seq=seq,
                    state=DrawerState.WAITING.value)
    barcode_sink.append(BarcodeRegistry(
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
    barcodes: list = []
    drawers = [_build_drawer(seq, barcodes) for seq in range(start, size + 1)]
    # Drawers first, then their barcodes — see the INSERT ORDER note above.
    db.add_all(drawers)
    db.flush()
    db.add_all(barcodes)
    minted = len(drawers)
    return {"drawers_bootstrapped": minted, "pool_size": max(have + minted, size)}


class _PoolAllocator:
    """Hands out drawers for one release: empties first, then either mints the
    shortfall or reports it as a WAITING LIST.

    THE POOL IS BOUNDED BY DEFAULT NOW (change-list item 9).
        The old allocator silently minted a new permanent drawer whenever the
        pool ran dry, so "200 static drawers" was true only until the first big
        order and the pool grew without anyone deciding it should. The client's
        rule is the opposite: 200 drawers, and when the pieces outrun them the
        remainder WAIT — with DM/MD permission required to grow the pool.

        `allow_pool_growth=True` restores the old mint-the-shortfall behaviour
        and is what POST /drawers/pool passes once a DM has authorised it.

    A piece with no drawer is not broken and not lost: it has its barcode and its
    identity, `piece.drawer_id` is NULL, and it appears in the waiting list until
    a drawer frees up (a piece ships) or the pool is grown. It simply cannot be
    stored, which means it cannot pass the merge gate — which is correct, because
    there is physically nowhere to put its parts.

    Nothing is added to the session here. Newly minted drawers land in
    `self.new_drawers` and their barcodes in `self.barcodes`, so premint_order
    can insert them in the right phase — see the INSERT ORDER note above.
    """

    def __init__(self, db: Session, *, allow_pool_growth: bool = False):
        self.db = db
        self.allow_pool_growth = allow_pool_growth
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
        self.starved = 0        # pieces that got no drawer — the waiting list
        self.new_drawers: list[Drawer] = []
        self.barcodes: list[BarcodeRegistry] = []

    def take(self) -> Drawer | None:
        """A free drawer, a newly minted one, or None (piece goes on the list)."""
        if self._free:
            drawer = self._free.pop(0)
            self.reused += 1
            return drawer
        if not self.allow_pool_growth:
            self.starved += 1
            return None
        drawer = _build_drawer(self._next_seq, self.barcodes)
        self.new_drawers.append(drawer)
        self._next_seq += 1
        self.minted += 1
        return drawer


# ──────────────────────────────────────────────────────────────────────────────
# The upload-time mint + allocate
# ──────────────────────────────────────────────────────────────────────────────
def premint_order(db: Session, order, *, style_ids=None,
                  allow_pool_growth: bool = False) -> dict:
    """Mint pieces + parent barcodes and MERGE each new piece into a pool drawer.

    NO LONGER CALLED FROM THE UPLOAD (change-list item 9). An upload writes the
    breakdown and stops; this runs when the DM RELEASES a style, via
    BreakdownService.release_styles. `style_ids` is what scopes it to the styles
    the DM actually picked — omit it and the whole order is minted, which is the
    behaviour the seed scripts and the pre-change tests rely on.

    `allow_pool_growth=False` (the default) means the drawer pool is FIXED: any
    piece beyond the free drawers is minted with `drawer_id = NULL` and reported
    in `pieces_waiting_for_drawer`. Growing the pool is a DM/MD decision made
    through POST /drawers/pool, not a side effect of an upload.

    Sync. Caller commits. Idempotent — a re-run tops up only what is missing.
    """
    stats = {
        "pieces_minted": 0,
        "drawers_reused": 0,      # taken from existing WAITING pool
        "drawers_minted": 0,      # new permanent drawers appended this release
        "pieces_waiting_for_drawer": 0,   # minted but unmerged — the waiting list
        "pieces_needing_lining": 0,
        "sample_barcodes": [],
    }

    sku_q = (select(SKU).join(Style, Style.id == SKU.style_id)
             .where(Style.client_order_id == order.id))
    if style_ids:
        sku_q = sku_q.where(Style.id.in_(list(style_ids)))
    sku_rows = db.scalars(sku_q).all()

    allocator = _PoolAllocator(db, allow_pool_growth=allow_pool_growth)
    new_pieces: list[Piece] = []          # phase 2
    links: list[tuple] = []               # phase 3: (drawer, piece) to wire up
    piece_barcodes: list[BarcodeRegistry] = []   # phase 4
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

            # 1) the piece (not yet cut). Explicit id, buffered — not added to
            #    the session here, so phase 1 can flush drawers on their own.
            piece = Piece(id=uuid.uuid4(), code=code, seq=seq, sku_id=sku.id,
                          current_operation_id=None)
            if hasattr(piece, "needs_lining"):
                piece.needs_lining = needs_lining
            new_pieces.append(piece)

            # 2) allocate a drawer from the pool (reuse empty, else mint, else
            #    the piece goes on the waiting list with drawer_id NULL).
            drawer = allocator.take()
            if drawer is not None:
                drawer.state = DrawerState.MERGED.value
                # current_piece_id is NOT set yet — piece.id does not exist in
                # the DB until phase 2, and fk_drawer_current_piece_id_piece is
                # checked immediately. Deferred to phase 3.
                links.append((drawer, piece))
                # A fresh merge starts with neither part in.
                drawer.leather_in = False
                drawer.lining_in = False
                drawer.received_at = None
                drawer.sended_at = None
                if hasattr(piece, "drawer_id"):
                    piece.drawer_id = drawer.id

            # 3) register the parent barcodes (the drawer barcode already exists —
            #    it is permanent and static; we do NOT re-register it on reuse).
            #    Buffered for phase 4: piece.id is not in the DB until phase 2.
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

    # ── the four ordered phases (see the INSERT ORDER note at the top) ────────
    # PHASE 1 — drawers land first, with current_piece_id still NULL. This flush
    # also carries the UPDATEs to reused drawers (state/leather_in/…), which is
    # safe for the same reason: none of them points at a piece yet.
    db.add_all(allocator.new_drawers)
    db.flush()

    # PHASE 2 — pieces. piece.drawer_id now resolves against a real drawer row.
    db.add_all(new_pieces)
    db.flush()

    # PHASE 3 — close the cycle. Both rows exist, so this is a plain UPDATE.
    for drawer, piece in links:
        drawer.current_piece_id = piece.id
    db.flush()

    # PHASE 4 — barcodes last; every parent they name is now committed-in-txn.
    # Left pending for the caller's commit to flush.
    db.add_all(allocator.barcodes)
    db.add_all(piece_barcodes)

    stats["drawers_reused"] = allocator.reused
    stats["drawers_minted"] = allocator.minted
    stats["pieces_waiting_for_drawer"] = allocator.starved
    return stats


def allocate_waiting_pieces(db: Session, *, limit: int | None = None) -> dict:
    """Merge already-minted, drawer-less pieces into whatever drawers are free.

    THE WAITING LIST DRAINS ITSELF. A piece minted while the pool was full keeps
    its barcode and its identity but has no drawer, so it cannot be stored. This
    is what gives it one — called after a drawer is freed (a piece ships), after
    the pool is grown, and on demand from POST /drawers/allocate-waiting.

    Oldest pieces first, oldest drawers first: deterministic, and a piece that
    has been waiting longest is the one the floor is asking about.

    Sync (it shares the pool primitives with premint). Caller commits.
    """
    q = (select(Piece)
         .where(Piece.drawer_id.is_(None), Piece.is_active.is_(True))
         .order_by(Piece.created_at.asc(), Piece.code.asc()))
    if limit:
        q = q.limit(limit)
    waiting = list(db.scalars(q).all())
    if not waiting:
        return {"allocated": 0, "still_waiting": 0}

    allocator = _PoolAllocator(db, allow_pool_growth=False)
    allocated = 0
    for piece in waiting:
        drawer = allocator.take()
        if drawer is None:
            break
        drawer.state = DrawerState.MERGED.value
        drawer.leather_in = False
        drawer.lining_in = False
        drawer.received_at = None
        drawer.sended_at = None
        drawer.current_piece_id = piece.id
        piece.drawer_id = drawer.id
        allocated += 1
    db.flush()

    still = int(db.scalar(
        select(func.count(Piece.id))
        .where(Piece.drawer_id.is_(None), Piece.is_active.is_(True))) or 0)
    return {"allocated": allocated, "still_waiting": still}


def grow_drawer_pool(db: Session, add: int) -> dict:
    """Append `add` NEW PERMANENT barcoded drawers to the pool. DM/MD only.

    The pool only ever grows, and only when a human says so — that is the whole
    point of bounding it. The new drawers are permanent from this moment: once a
    piece in one of them ships, the drawer recycles back to WAITING like any
    other. Caller commits.
    """
    if add <= 0:
        return {"added": 0, "pool_size": _drawer_count(db)}
    start = _max_drawer_seq(db) + 1
    barcodes: list = []
    drawers = [_build_drawer(seq, barcodes) for seq in range(start, start + add)]
    db.add_all(drawers)
    db.flush()          # drawers before their barcodes — see the INSERT ORDER note
    db.add_all(barcodes)
    db.flush()
    return {"added": len(drawers), "pool_size": _drawer_count(db)}


def drawer_pool_status(db: Session) -> dict:
    """Pool size, how much of it is free, and how many pieces are waiting."""
    free = int(db.scalar(
        select(func.count(Drawer.id))
        .where(Drawer.state == DrawerState.WAITING.value,
               Drawer.current_piece_id.is_(None))) or 0)
    waiting = int(db.scalar(
        select(func.count(Piece.id))
        .where(Piece.drawer_id.is_(None), Piece.is_active.is_(True))) or 0)
    total = _drawer_count(db)
    return {
        "pool_size": total,
        "initial_pool_size": INITIAL_DRAWER_POOL,
        "free_drawers": free,
        "occupied_drawers": total - free,
        "pieces_waiting_for_drawer": waiting,
        # What a DM would have to add to clear the waiting list right now.
        "shortfall": max(0, waiting - free),
    }


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