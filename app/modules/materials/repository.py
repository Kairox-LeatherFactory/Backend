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

from app.modules.barcode.models import (
    MaterialLot, MaterialReceipt, MaterialReservation, MaterialSupplier, SupplierOrder,
)


class MaterialRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

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
        tokens = {t.strip().upper() for t in supplier.articles.split(",") if t.strip()}
        a = (article or "").strip().upper()
        return a in tokens or any(a in t or t in a for t in tokens)

    async def get_lot(self, lot_id: uuid.UUID) -> MaterialLot | None:
        return await self.db.get(MaterialLot, lot_id)

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
        off the physical label, so the picker can show it beside the article."""
        if not lot_ids:
            return {}
        from app.core.enums import BarcodeStatus
        from app.modules.barcode.models import BarcodeRegistry
        rows = await self.db.execute(
            select(BarcodeRegistry.material_lot_id, BarcodeRegistry.code)
            .where(BarcodeRegistry.material_lot_id.in_(lot_ids),
                   BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        )
        return {lot_id: code for lot_id, code in rows.all() if lot_id is not None}

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