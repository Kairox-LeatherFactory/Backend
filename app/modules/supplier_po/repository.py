"""
================================================================================
modules/supplier_po/repository.py — Stage-5 supplier-PO data access
================================================================================

The only place that talks to the DB for supplier_po-owned tables (supplier,
supplier_supply_history, purchase_order, po_item, po_response, po_tracking_event,
production_tracking) plus the cross-cutting core `document` write path for the PO PDF.
BOM + inventory data (shortfall lines, aliases) flow through those modules' services.
================================================================================
"""
from __future__ import annotations

from sqlalchemy import delete, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.models import Document
from app.modules.supplier_po.enums import POStatus
from app.modules.supplier_po.models import (
    ProductionTracking,
    PoResponse,
    PoTrackingEvent,
    PurchaseOrder,
    Supplier,
    SupplierSupplyHistory,
)


class SupplierPoRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def commit(self) -> None:
        await self.db.commit()

    async def rollback(self) -> None:
        await self.db.rollback()

    # ── document (core table; supplier_po owns the PO-PDF write path) ────────
    async def get_document_by_sha(self, sha256: str) -> Document | None:
        res = await self.db.execute(select(Document).where(Document.sha256 == sha256))
        return res.scalar_one_or_none()

    async def add_document(self, doc: Document) -> Document:
        self.db.add(doc)
        await self.db.commit()
        await self.db.refresh(doc)
        return doc

    # ── supplier ──────────────────────────────────────────────────────────────
    async def get_supplier(self, supplier_id) -> Supplier | None:
        res = await self.db.execute(
            select(Supplier).where(Supplier.id == supplier_id)
            .options(selectinload(Supplier.supply_history)))
        return res.scalar_one_or_none()

    async def get_supplier_by_name(self, name: str) -> Supplier | None:
        res = await self.db.execute(select(Supplier).where(Supplier.name == name))
        return res.scalars().first()

    async def list_suppliers(self, *, search=None, service=None, active=None,
                             limit=100, offset=0) -> list[Supplier]:
        stmt = select(Supplier)
        if active is not None:
            stmt = stmt.where(Supplier.is_active.is_(active))
        if search:
            like = f"%{search}%"
            stmt = stmt.where(or_(Supplier.name.ilike(like), Supplier.service.ilike(like)))
        if service:
            stmt = stmt.where(Supplier.service.ilike(f"%{service}%"))
        stmt = stmt.order_by(Supplier.name).limit(limit).offset(offset)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def all_active_suppliers(self) -> list[Supplier]:
        res = await self.db.execute(select(Supplier).where(Supplier.is_active.is_(True)))
        return list(res.scalars())

    async def supplier_open_pos(self, supplier_id) -> list[PurchaseOrder]:
        res = await self.db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.supplier_id == supplier_id,
                PurchaseOrder.status.notin_(
                    [POStatus.CANCELLED.value, POStatus.CONFIRMED.value]),
            ))
        return list(res.scalars())

    # ── supplier_supply_history (the §1d matching index) ─────────────────────
    async def clear_supply_history(self) -> int:
        res = await self.db.execute(delete(SupplierSupplyHistory))
        return res.rowcount or 0

    async def supply_history_for_supplier(self, supplier_id):
        res = await self.db.execute(
            select(SupplierSupplyHistory).where(
                SupplierSupplyHistory.supplier_id == supplier_id)
            .order_by(SupplierSupplyHistory.last_purchased_at.desc()))
        return list(res.scalars())

    async def fetch_supply_history_candidates(self, *, like_terms, modes=None, limit=600):
        conds = []
        for t in like_terms:
            if t:
                conds.append(SupplierSupplyHistory.normalized_description.like(f"%{t}%"))
        if modes:
            conds.append(SupplierSupplyHistory.mode.in_(list(modes)))
        if not conds:
            return []
        stmt = (select(SupplierSupplyHistory, Supplier)
                .join(Supplier, Supplier.id == SupplierSupplyHistory.supplier_id)
                .where(Supplier.is_active.is_(True), or_(*conds)).limit(limit))
        res = await self.db.execute(stmt)
        return [(h, s) for (h, s) in res.all()]

    # ── purchase_order ────────────────────────────────────────────────────────
    async def get_po(self, po_id) -> PurchaseOrder | None:
        res = await self.db.execute(
            select(PurchaseOrder).where(PurchaseOrder.id == po_id).options(
                selectinload(PurchaseOrder.items),
                selectinload(PurchaseOrder.responses),
                selectinload(PurchaseOrder.supplier)))
        return res.scalar_one_or_none()

    async def get_po_by_token(self, token: str) -> PurchaseOrder | None:
        res = await self.db.execute(
            select(PurchaseOrder).where(PurchaseOrder.tracking_token == token)
            .options(selectinload(PurchaseOrder.responses)))
        return res.scalars().first()

    async def list_pos(self, *, status=None, needs_supplier=None, bom_id=None,
                       supplier_id=None, limit=200, offset=0):
        stmt = select(PurchaseOrder).options(
            selectinload(PurchaseOrder.items), selectinload(PurchaseOrder.supplier))
        if status is not None:
            stmt = stmt.where(PurchaseOrder.status == status)
        if needs_supplier is not None:
            stmt = stmt.where(PurchaseOrder.needs_supplier.is_(needs_supplier))
        if bom_id is not None:
            stmt = stmt.where(PurchaseOrder.bom_id == bom_id)
        if supplier_id is not None:
            stmt = stmt.where(PurchaseOrder.supplier_id == supplier_id)
        stmt = stmt.order_by(PurchaseOrder.created_at.desc()).limit(limit).offset(offset)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def pos_for_bom(self, bom_id):
        res = await self.db.execute(
            select(PurchaseOrder).where(PurchaseOrder.bom_id == bom_id)
            .options(selectinload(PurchaseOrder.items)))
        return list(res.scalars())

    async def claim_po_revision(self, po_id, base_revision: int) -> int:
        res = await self.db.execute(
            update(PurchaseOrder)
            .where(PurchaseOrder.id == po_id, PurchaseOrder.revision == base_revision)
            .values(revision=base_revision + 1))
        return res.rowcount

    async def next_po_number(self, fy: str) -> str:
        res = await self.db.execute(
            select(PurchaseOrder.po_number).where(PurchaseOrder.po_number.like(f"%({fy})")))
        max_n = 0
        for (num,) in res.all():
            if not num:
                continue
            head = num.split("(")[0].replace("PO-", "").strip()
            if head.isdigit():
                max_n = max(max_n, int(head))
        return f"PO-{max_n + 1:02d}({fy})"

    async def get_po_response(self, response_id) -> PoResponse | None:
        res = await self.db.execute(
            select(PoResponse).where(PoResponse.id == response_id))
        return res.scalar_one_or_none()

    async def latest_response(self, po_id) -> PoResponse | None:
        res = await self.db.execute(
            select(PoResponse).where(PoResponse.purchase_order_id == po_id)
            .order_by(PoResponse.created_at.desc()))
        return res.scalars().first()

    async def add_tracking_event(self, ev: PoTrackingEvent) -> None:
        self.db.add(ev)

    async def due_po_escalations(self, now) -> list[PurchaseOrder]:
        res = await self.db.execute(
            select(PurchaseOrder).where(
                PurchaseOrder.status.in_([POStatus.SENT.value, POStatus.ESCALATED.value]),
                PurchaseOrder.acknowledged_at.is_(None),
                PurchaseOrder.current_rung < 3,
                PurchaseOrder.next_escalation_at.isnot(None),
                PurchaseOrder.next_escalation_at <= now,
            ).options(selectinload(PurchaseOrder.supplier),
                      selectinload(PurchaseOrder.items)))
        return list(res.scalars())

    # ── production_tracking (§8) ──────────────────────────────────────────────
    async def get_production_tracking(self, client_order_id, style_id):
        res = await self.db.execute(
            select(ProductionTracking).where(
                ProductionTracking.client_order_id == client_order_id,
                ProductionTracking.style_id == style_id))
        return res.scalar_one_or_none()

    async def get_production_tracking_by_id(self, tracking_id):
        res = await self.db.execute(
            select(ProductionTracking).where(ProductionTracking.id == tracking_id))
        return res.scalar_one_or_none()

    async def list_production_tracking(self):
        res = await self.db.execute(
            select(ProductionTracking).order_by(ProductionTracking.created_at.desc()))
        return list(res.scalars())
