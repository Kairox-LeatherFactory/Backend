"""
================================================================================
modules/materials/repository.py — Async data access for material lots & suppliers
================================================================================
available = on_hand − Σ active reservations, computed here so the two can never
drift. on_hand is only ever moved by receiving (+approved) and consumption
(−dcm); reservations are a separate ledger that never touches on_hand.
================================================================================
"""
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import SHEET_ALLOCATABLE, SheetStatus
from app.modules.barcode.models import (
    MaterialLot, MaterialReceipt, MaterialReservation, MaterialSheet,
    MaterialSupplier, SupplierOrder,
)


class MaterialRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── consumption, by style and by garment (#17 / #19 / #28) ───────────────
    # The cut EVENT is where leather consumption lives — never the piece — so
    # "what did this style cost in hide" is a sum over production_event, not a
    # column anybody maintains. Splitting it by is_rework is what makes
    # "of which N was rework" answerable (#3).

    async def consumption_by_style(self, *, style_id=None, article=None) -> list:
        from app.modules.clients.models import SKU, Style
        from app.modules.production.models import Piece, ProductionEvent
        stmt = (select(Style.id, Style.name, Style.article,
                       MaterialLot.article, MaterialLot.colour, MaterialLot.uom,
                       func.coalesce(func.sum(ProductionEvent.consumption_qty), 0),
                       func.count(func.distinct(Piece.id)))
                .join(Piece, Piece.id == ProductionEvent.piece_id)
                .join(SKU, SKU.id == Piece.sku_id)
                .join(Style, Style.id == SKU.style_id)
                .join(MaterialLot,
                      MaterialLot.id == ProductionEvent.leather_lot_id)
                .where(ProductionEvent.consumption_qty.is_not(None))
                .group_by(Style.id, Style.name, Style.article,
                          MaterialLot.article, MaterialLot.colour,
                          MaterialLot.uom))
        if style_id:
            stmt = stmt.where(Style.id == style_id)
        if article:
            stmt = stmt.where(MaterialLot.article == article)
        return [{"style_id": r[0], "style_name": r[1], "style_article": r[2],
                 "article": r[3], "colour": r[4], "uom": r[5],
                 "consumed": float(r[6] or 0), "pieces": int(r[7])}
                for r in (await self.db.execute(stmt)).all()]

    async def rework_consumption_by_style(self, *, style_id=None) -> dict:
        """{style_id: dcm spent on REWORK}. The other half of the cost split."""
        from app.modules.clients.models import SKU, Style
        from app.modules.production.models import Piece, ProductionEvent
        stmt = (select(Style.id,
                       func.coalesce(func.sum(ProductionEvent.consumption_qty), 0))
                .join(Piece, Piece.id == ProductionEvent.piece_id)
                .join(SKU, SKU.id == Piece.sku_id)
                .join(Style, Style.id == SKU.style_id)
                .where(ProductionEvent.consumption_qty.is_not(None),
                       ProductionEvent.is_rework.is_(True))
                .group_by(Style.id))
        if style_id:
            stmt = stmt.where(Style.id == style_id)
        return {r[0]: float(r[1] or 0) for r in (await self.db.execute(stmt)).all()}

    async def consumption_by_piece(self, piece_id) -> list:
        """What one garment actually took, hide by hide (#17, #28)."""
        from app.modules.production.models import Operation, ProductionEvent
        res = await self.db.execute(
            select(Operation.code, ProductionEvent.consumption_qty,
                   ProductionEvent.is_rework, MaterialLot.article,
                   MaterialLot.colour, MaterialLot.uom, ProductionEvent.work_date)
            .join(Operation, Operation.id == ProductionEvent.operation_id)
            .outerjoin(MaterialLot,
                       MaterialLot.id == ProductionEvent.leather_lot_id)
            .where(ProductionEvent.piece_id == piece_id,
                   ProductionEvent.consumption_qty.is_not(None))
            .order_by(ProductionEvent.work_date.asc()))
        return [{"stage": r[0], "qty": float(r[1] or 0), "is_rework": bool(r[2]),
                 "article": r[3], "colour": r[4], "uom": r[5],
                 "work_date": r[6]} for r in res.all()]

    # ── receipts: what has ever ARRIVED, as opposed to what is left ──────────
    # BUG #26 — "the Received value is showing as 0 in the backend, even though
    # material has been recorded". It was showing 0 because nothing ever asked.
    # Every receive has always written a material_receipt row, but no read path
    # summed them, so the field the stock screen renders had no source at all.
    #
    # RECEIVED IS NOT on_hand. on_hand is what is LEFT after consumption; received
    # is everything that ever arrived. A lot that took 500 dcm and has spent 400
    # reads on_hand 100 / received 500, and both numbers are needed to answer
    # "how much of this article have we bought this season".

    async def received_totals(self, lot_ids: list) -> dict:
        """{lot_id: {"received": x, "rejected": y, "deliveries": n}} in ONE query.

        Batched because the lot directory renders a page of lots at a time and a
        per-row query is how a stock screen becomes slow enough to stop being
        opened.
        """
        if not lot_ids:
            return {}
        res = await self.db.execute(
            select(MaterialReceipt.material_lot_id,
                   func.coalesce(func.sum(MaterialReceipt.approved_qty), 0),
                   func.coalesce(func.sum(MaterialReceipt.rejected_qty), 0),
                   func.count())
            .where(MaterialReceipt.material_lot_id.in_(tuple(lot_ids)))
            .group_by(MaterialReceipt.material_lot_id))
        return {row[0]: {"received": float(row[1] or 0),
                         "rejected": float(row[2] or 0),
                         "deliveries": int(row[3] or 0)} for row in res.all()}

    async def receipts_for_lot(self, lot_id: uuid.UUID) -> list:
        """Every delivery of this lot, newest first — the purchase history (#16).

        The rows have always been here; there was simply no way to read them.
        """
        res = await self.db.execute(
            select(MaterialReceipt)
            .where(MaterialReceipt.material_lot_id == lot_id)
            .order_by(MaterialReceipt.created_at.desc()))
        return list(res.scalars().all())

    # ── arrivals: the half-entered delivery (the two-sitting intake) ─────────
    async def get_receipt(self, receipt_id: uuid.UUID) -> MaterialReceipt | None:
        return await self.db.get(MaterialReceipt, receipt_id)

    async def find_receipts(self, *, status: str | None = None,
                            lot_id: uuid.UUID | None = None,
                            limit: int = 200, offset: int = 0) -> list:
        """The worklist: deliveries still waiting to be finished, oldest first.

        OLDEST FIRST, unlike `receipts_for_lot`. This is a queue of unfinished
        work rather than a history — the arrival nobody has come back to for
        three days is the one that matters, and newest-first buries it.
        """
        stmt = select(MaterialReceipt)
        if status:
            stmt = stmt.where(MaterialReceipt.status == status.strip().upper())
        if lot_id is not None:
            stmt = stmt.where(MaterialReceipt.material_lot_id == lot_id)
        stmt = stmt.order_by(MaterialReceipt.created_at.asc()).limit(limit).offset(offset)
        return list((await self.db.execute(stmt)).scalars().all())

    async def count_receipts(self, *, status: str | None = None,
                             lot_id: uuid.UUID | None = None) -> int:
        stmt = select(func.count()).select_from(MaterialReceipt)
        if status:
            stmt = stmt.where(MaterialReceipt.status == status.strip().upper())
        if lot_id is not None:
            stmt = stmt.where(MaterialReceipt.material_lot_id == lot_id)
        return int(await self.db.scalar(stmt) or 0)

    async def pending_intake_by_lot(self, lot_ids: list) -> dict:
        """{lot_id: {"count": n, "declared_qty": x}} for PENDING arrivals only.

        What the lot directory needs to put "2 deliveries not finished" on a row
        without a query per lot. A lot with an unfinished arrival is carrying
        provisional stock, and a stock screen that cannot say so is presenting an
        estimate as a measurement.
        """
        if not lot_ids:
            return {}
        res = await self.db.execute(
            select(MaterialReceipt.material_lot_id, func.count(),
                   func.coalesce(func.sum(MaterialReceipt.declared_qty), 0))
            .where(MaterialReceipt.material_lot_id.in_(tuple(lot_ids)),
                   MaterialReceipt.status == "PENDING")
            .group_by(MaterialReceipt.material_lot_id))
        return {row[0]: {"count": int(row[1] or 0),
                         "declared_qty": float(row[2] or 0)} for row in res.all()}

    # ── sheets (LEATHER only) ────────────────────────────────────────────────
    # A hide is a STOCK item first and a cutting-row member second: it exists
    # before any row claims it and survives the row being deleted. So its data
    # access lives here, with the lot it belongs to, not in the cutting module.

    def add_sheet_nocommit(self, **kw) -> MaterialSheet:
        sheet = MaterialSheet(**kw)
        self.db.add(sheet)
        return sheet

    async def get_sheet(self, sheet_id: uuid.UUID) -> MaterialSheet | None:
        return await self.db.get(MaterialSheet, sheet_id)

    async def get_sheet_by_code(self, code: str) -> MaterialSheet | None:
        res = await self.db.execute(
            select(MaterialSheet)
            .where(MaterialSheet.code == (code or "").strip().upper()))
        return res.scalar_one_or_none()

    async def next_sheet_seq(self) -> int:
        """The next LS- serial.

        COUNTS, RATHER THAN READING THE HIGHEST CODE. The barcode repository's
        `_next_code` derives its counter from the lexicographically greatest code
        in the prefix, which pins the counter at 0 forever the moment one
        non-numeric code enters the namespace — a live, open bug that
        test_one_non_numeric_code_jams_minting_for_that_prefix_forever documents
        for EMP-. Sheets are minted only here, in bulk, inside one transaction,
        so a count is both correct and immune to that failure.
        """
        return int(await self.db.scalar(
            select(func.count()).select_from(MaterialSheet)) or 0)

    async def sheets_for_lot(self, lot_id: uuid.UUID,
                             statuses: set | None = None) -> list:
        stmt = select(MaterialSheet).where(MaterialSheet.material_lot_id == lot_id)
        if statuses:
            stmt = stmt.where(MaterialSheet.status.in_(tuple(statuses)))
        # Smallest hide first: the allocator wants to spend the offcuts before it
        # breaks into a big skin, and a stable order makes allocation repeatable.
        stmt = stmt.order_by(MaterialSheet.dcm.asc(), MaterialSheet.code.asc())
        return list((await self.db.execute(stmt)).scalars().all())

    async def sheets_for_row_by_piece(self, piece_id) -> list:
        """The hides that went into one garment, via its cutting row.

        Empty for a piece cut the old way (a typed dcm with no sheet record),
        which is the honest answer: nobody wrote down which hides those were.
        """
        from app.modules.cutting.models import CuttingRow
        res = await self.db.execute(
            select(MaterialSheet)
            .join(CuttingRow, CuttingRow.id == MaterialSheet.cutting_row_id)
            .where(CuttingRow.piece_id == piece_id)
            .order_by(MaterialSheet.code.asc()))
        return list(res.scalars().all())

    async def sheets_for_row(self, cutting_row_id: uuid.UUID) -> list:
        res = await self.db.execute(
            select(MaterialSheet)
            .where(MaterialSheet.cutting_row_id == cutting_row_id)
            .order_by(MaterialSheet.code.asc()))
        return list(res.scalars().all())

    async def allocatable_sheets(self, lot_id: uuid.UUID) -> list:
        """Hides this lot can still give out — IN_STOCK or RETURNED.

        ALLOCATED is deliberately excluded: re-offering a hide another draft row
        already holds is the double-claim bug the status exists to prevent.
        """
        return await self.sheets_for_lot(lot_id, statuses=set(SHEET_ALLOCATABLE))

    async def allocatable_sheets_for_lots(self, lot_ids: list) -> list:
        """The same shelf, across SEVERAL lots, in ONE query.

        The cut screen's DCM lookup asks "which hide of this article and colour
        measures 223.5" and an article/colour can legitimately span more than one
        lot (a substitution receipt mints one; a thickness variant is another).
        Looping `allocatable_sheets` per lot would make the hot path on every
        sheet entry N round trips, so the fan-out happens in SQL.

        Smallest hide first, same as `sheets_for_lot`: it keeps the nearest-match
        search deterministic when two hides tie on distance.
        """
        if not lot_ids:
            return []
        res = await self.db.execute(
            select(MaterialSheet)
            .where(MaterialSheet.material_lot_id.in_(tuple(lot_ids)),
                   MaterialSheet.status.in_(tuple(SHEET_ALLOCATABLE)))
            .order_by(MaterialSheet.dcm.asc(), MaterialSheet.code.asc()))
        return list(res.scalars().all())

    async def sheet_totals_for_lots(self, lot_ids: list) -> dict:
        """{lot_id: {status: {"count": n, "dcm": x}}} for many lots in ONE query.

        `sheet_counts_by_status` answers this for a single lot and the stock
        screen needs it for every lot it is summing — see MaterialService.stock,
        where the sheet-wise figures are rolled up beside the dcm ones.
        """
        if not lot_ids:
            return {}
        res = await self.db.execute(
            select(MaterialSheet.material_lot_id, MaterialSheet.status,
                   func.count(),
                   func.coalesce(func.sum(MaterialSheet.dcm), 0))
            .where(MaterialSheet.material_lot_id.in_(tuple(lot_ids)))
            .group_by(MaterialSheet.material_lot_id, MaterialSheet.status))
        out: dict = {}
        for lot_id, st, count, dcm in res.all():
            out.setdefault(lot_id, {})[st] = {
                "count": int(count), "dcm": float(dcm or 0)}
        return out

    async def sheet_dcm_in_store(self, lot_id: uuid.UUID) -> Decimal:
        """Σ dcm of the hides physically on the shelf for this lot.

        Reconciled against lot.on_hand as a REPORTED check, never a constraint —
        a delivery nobody sheeted must stay receivable.
        """
        val = await self.db.scalar(
            select(func.coalesce(func.sum(MaterialSheet.dcm), 0))
            .where(MaterialSheet.material_lot_id == lot_id,
                   MaterialSheet.status.in_(tuple(SHEET_ALLOCATABLE))))
        return Decimal(str(val or 0))

    async def sheet_counts_by_status(self, lot_id: uuid.UUID) -> dict:
        res = await self.db.execute(
            select(MaterialSheet.status, func.count(), func.coalesce(func.sum(MaterialSheet.dcm), 0))
            .where(MaterialSheet.material_lot_id == lot_id)
            .group_by(MaterialSheet.status))
        return {row[0]: {"count": int(row[1]), "dcm": float(row[2] or 0)}
                for row in res.all()}

    # ── lots ─────────────────────────────────────────────────────────────────
    def add_lot_nocommit(self, **kw) -> MaterialLot:
        lot = MaterialLot(**kw)
        self.db.add(lot)
        return lot
    
    async def supplier_supplies(self, supplier, article: str) -> bool:
        """Does this supplier list this article? `articles` is a comma list
        ("SUEDE-A32,NAP-11"). Case-insensitive substring on a token match."""
        if supplier is None or not (supplier.articles or "").strip():
            return False
        
        # "SUEDE-A32,NAP-11"
        tokens = {t.strip().upper() for t in supplier.articles.split(",") if t.strip()}
        a = (article or "").strip().upper()
        return a in tokens or any(a in t or t in a for t in tokens)

    async def get_lot(self, lot_id: uuid.UUID) -> MaterialLot | None:
        return await self.db.get(MaterialLot, lot_id)

    async def get_lot_for_update(self, lot_id: uuid.UUID) -> MaterialLot | None:
        """Fetch a lot with the row LOCKED for the rest of the transaction.

        USE THIS BEFORE ANY READ-MODIFY-WRITE OF on_hand / used. Stock moves are
        `on_hand = on_hand - d` in Python, so two cutting managers scanning
        against the same lot concurrently both read the same `before` and the
        second write silently overwrites the first: stock is lost with no error
        and no warning. SELECT ... FOR UPDATE serialises them on the row, so the
        second transaction reads the first one's result.

        SQLite has no row locks and ignores `with_for_update`, which is fine —
        the tests run single-writer. The lock is what protects Postgres, where
        the concurrency is real.
        """
        return await self.db.get(MaterialLot, lot_id, with_for_update=True)

    async def active_reserved(self, lot_id: uuid.UUID) -> Decimal:
        val = await self.db.scalar(
            select(func.coalesce(func.sum(MaterialReservation.qty), 0))
            .where(MaterialReservation.material_lot_id == lot_id,
                   MaterialReservation.status == "active")
        )
        return Decimal(str(val or 0))

    # ── the Option-A identity key ────────────────────────────────────────────
    # A material is identified by WHAT IT IS, not by when it was bought:
    #   (category, subtype, article, colour, thickness, size)
    # Re-supply tops the SAME lot up through /materials/receive; it never mints a
    # second row. That is what makes "filter article+colour+thickness → one lot"
    # a rule the picker can rely on instead of a coincidence of the seed data.
    LOT_KEY = ("category", "subtype", "article", "colour", "thickness", "size")

    async def find_duplicate_lot(self, *, category: str, subtype: str | None,
                                 article: str, colour: str | None,
                                 thickness: str | None,
                                 size: str | None):
        """The existing ACTIVE lot with this exact spec, or None.

        `is` comparisons matter: colour/thickness/size are nullable, and
        `col == None` renders as `IS NULL` in SQLAlchemy, which is what we want —
        two lots that both leave thickness blank ARE the same material.
        """
        stmt = select(MaterialLot).where(
            MaterialLot.is_active.is_(True),
            MaterialLot.category == (category or "").upper(),
            MaterialLot.article == article,
        )
        for col, val in ((MaterialLot.subtype, subtype),
                         (MaterialLot.colour, colour),
                         (MaterialLot.thickness, thickness),
                         (MaterialLot.size, size)):
            stmt = stmt.where(col.is_(None) if val is None else col == val)
        return (await self.db.execute(stmt.limit(1))).scalar_one_or_none()

    async def reserved_by_lot(self, lot_ids: list[uuid.UUID]) -> dict:
        """{lot_id: active reserved qty} for many lots in ONE query.

        `active_reserved` above is right for a single aggregate, but the picker
        lists every matching lot — calling it per row is an N+1 on the screen a
        cutting manager opens on every scan.
        """
        if not lot_ids:
            return {}
        rows = await self.db.execute(
            select(MaterialReservation.material_lot_id,
                   func.coalesce(func.sum(MaterialReservation.qty), 0))
            .where(MaterialReservation.material_lot_id.in_(lot_ids),
                   MaterialReservation.status == "active")
            .group_by(MaterialReservation.material_lot_id)
        )
        return {lot_id: Decimal(str(total or 0)) for lot_id, total in rows.all()}

    async def barcodes_by_lot(self, lot_ids: list[uuid.UUID]) -> dict:
        """{lot_id: printed lot barcode} in ONE query — the code a manager reads
        off the physical label, so the picker can show it beside the article.

        THE TYPE FILTER IS LOAD-BEARING, not tidiness. A LEATHER_SHEET registry
        row carries `material_lot_id` as well as `material_sheet_id` — by design,
        so one scan of a hide can answer "which hide" and "what article is it"
        together (`mint_sheet_code_nocommit`). Without the filter those rows come
        back here too, and since this is a dict comprehension the LAST row wins:
        every sheeted lot reported one of its hides' codes (LS-000004) as the
        lot's own barcode, on the lot picker, the lot detail page and anything
        else that renders `barcode`. Restricting it to the three LOT types is
        what makes the answer the lot's label and nothing else.
        """
        if not lot_ids:
            return {}
        from app.core.enums import BarcodeStatus, BarcodeType
        from app.modules.barcode.models import BarcodeRegistry
        lot_types = (BarcodeType.LEATHER_LOT.value, BarcodeType.LINING_LOT.value,
                     BarcodeType.ACCESSORY_LOT.value)
        rows = await self.db.execute(
            select(BarcodeRegistry.material_lot_id, BarcodeRegistry.code)
            .where(BarcodeRegistry.material_lot_id.in_(lot_ids),
                   BarcodeRegistry.type.in_(lot_types),
                   BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        )
        return {lot_id: code for lot_id, code in rows.all() if lot_id is not None}

    # we are finding the recent style was cut in factory
    async def last_lot_for_sku(self, sku_id: uuid.UUID, *,
                               lining: bool = False) -> uuid.UUID | None:
        """The lot this SKU was most recently CUT from — derived, never stored.

        This is the auto-fill. It is a query over what actually happened
        (production_event.leather_lot_id / lining_lot_id) rather than a saved
        preference, so it cannot go stale, needs no new table, and is keyed on
        the SKU — which carries the COLOUR. Keying it on the style would hand a
        FOREST lot to a WHISKY garment of the same style.

        ORDERING: work_date first, created_at second. work_date is the business
        fact — the day the cutting actually happened — and created_at only breaks
        ties within a day. Ordering on created_at ALONE is not safe: it defaults
        to `now()`, which on Postgres is TRANSACTION-START time (so every event
        in one batch shares a timestamp) and on SQLite has whole-second
        granularity. Ties within a batch are harmless because a batch may only
        consume one lot, but the ordering should not depend on that.
        """
        from app.modules.production.models import ProductionEvent
        col = (ProductionEvent.lining_lot_id if lining
               else ProductionEvent.leather_lot_id)
        return await self.db.scalar(
            select(col)
            .where(ProductionEvent.sku_id == sku_id, col.isnot(None))
            .order_by(ProductionEvent.work_date.desc(),
                      ProductionEvent.created_at.desc())
            .limit(1)
        )

    async def find_lots(self, *, category: str | None = None,
                        subtype: str | None = None, article: str | None = None,
                        colour: str | None = None, thickness: str | None = None,
                        size: str | None = None) -> list[MaterialLot]:
        stmt = select(MaterialLot).where(MaterialLot.is_active.is_(True))
        if category:
            stmt = stmt.where(MaterialLot.category == category.upper())
        if subtype:
            stmt = stmt.where(MaterialLot.subtype == subtype.upper())
        if article:
            stmt = stmt.where(MaterialLot.article == article)
        if colour:
            stmt = stmt.where(MaterialLot.colour == colour)
        if thickness:
            stmt = stmt.where(MaterialLot.thickness == thickness)
        if size:
            stmt = stmt.where(MaterialLot.size == size)
        res = await self.db.execute(stmt.order_by(MaterialLot.created_at))
        return list(res.scalars())

    # ── reservations ─────────────────────────────────────────────────────────
    def add_reservation_nocommit(self, lot_id: uuid.UUID, qty: Decimal,
                                 reason: str | None) -> MaterialReservation:
        r = MaterialReservation(material_lot_id=lot_id, qty=qty, status="active",
                                reason=reason)
        self.db.add(r)
        return r

    async def consume_reservations_nocommit(self, lot_id: uuid.UUID,
                                            qty: Decimal) -> Decimal:
        """Consume active reservations oldest first as stock is used."""
        remaining = qty
        rows = await self.db.execute(
            select(MaterialReservation)
            .where(MaterialReservation.material_lot_id == lot_id,
                   MaterialReservation.status == "active")
            .order_by(MaterialReservation.created_at,
                      MaterialReservation.id)
        )
        for reservation in rows.scalars():
            if remaining <= 0:
                break
            consumed = min(remaining, reservation.qty)
            reservation.qty -= consumed
            remaining -= consumed
            if reservation.qty == 0:
                reservation.status = "consumed"
                reservation.released_at = datetime.now(timezone.utc)
        return await self.active_reserved(lot_id)

    # ── receipts ─────────────────────────────────────────────────────────────
    def add_receipt_nocommit(self, **kw) -> MaterialReceipt:
        rec = MaterialReceipt(**kw)
        self.db.add(rec)
        return rec

    async def rejected_history(self, supplier_id: uuid.UUID) -> Decimal:
        """Total rejected qty ever recorded against a supplier's orders — the
        quality-history read."""
        val = await self.db.scalar(
            select(func.coalesce(func.sum(MaterialReceipt.rejected_qty), 0))
            .join(SupplierOrder, SupplierOrder.id == MaterialReceipt.supplier_order_id)
            .where(SupplierOrder.supplier_id == supplier_id)
        )
        return Decimal(str(val or 0))

    # ── suppliers ────────────────────────────────────────────────────────────
    async def suggest_supplier(self, article: str) -> MaterialSupplier | None:
        """Simple article→supplier lookup (v1). NOT the AI classifier."""
        if not article:
            return None
        res = await self.db.execute(
            select(MaterialSupplier).where(
                MaterialSupplier.is_active.is_(True),
                MaterialSupplier.articles.ilike(f"%{article}%"))
            .limit(1)
        )
        return res.scalar_one_or_none()

    async def get_supplier(self, supplier_id: uuid.UUID) -> MaterialSupplier | None:
        return await self.db.get(MaterialSupplier, supplier_id)

    # ── supplier orders ──────────────────────────────────────────────────────
    def add_order_nocommit(self, **kw) -> SupplierOrder:
        o = SupplierOrder(**kw)
        self.db.add(o)
        return o

    async def get_order(self, order_id: uuid.UUID) -> SupplierOrder | None:
        return await self.db.get(SupplierOrder, order_id)

    async def commit(self) -> None:
        await self.db.commit()