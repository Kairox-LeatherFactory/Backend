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

    async def get_lot(self, lot_id: uuid.UUID) -> MaterialLot | None:
        return await self.db.get(MaterialLot, lot_id)

    async def active_reserved(self, lot_id: uuid.UUID) -> Decimal:
        val = await self.db.scalar(
            select(func.coalesce(func.sum(MaterialReservation.qty), 0))
            .where(MaterialReservation.material_lot_id == lot_id,
                   MaterialReservation.status == "active")
        )
        return Decimal(str(val or 0))

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