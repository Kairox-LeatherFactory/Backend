"""
================================================================================
modules/procurement/production_tracking_service.py — Stage-5 §8 production board
================================================================================

One `production_tracking` row per style/order advancing through the §8c status ladder.
System-driven on each procurement stage transition (service→service side-effects from
bom_service / inventory_service / po_service) + manually nudgeable by DM/Cutting/MD.

LAYERING. Owns only `production_tracking` (procurement). Style/order/client identity the
board groups by resolves through clients.service (CLAUDE.md §3.2). The order's POs are
read through the procurement repository (same module).
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.modules.procurement import po_presenters as present
from app.modules.procurement.enums import POStatus, ProductionTrackingStatus
from app.modules.procurement.models import AuditLog, ProductionTracking
from app.modules.procurement.repository import ProcurementRepository

# The §8c ladder order (an index makes "advance only forward" comparisons cheap).
_LADDER = [
    ProductionTrackingStatus.AWAITING_BOM.value,
    ProductionTrackingStatus.BOM_APPROVED.value,
    ProductionTrackingStatus.INVENTORY_CHECKED.value,
    ProductionTrackingStatus.PO_RAISED.value,
    ProductionTrackingStatus.PO_CONFIRMED.value,
    ProductionTrackingStatus.MATERIAL_READY.value,
    ProductionTrackingStatus.RELEASED_TO_PRODUCTION.value,
    ProductionTrackingStatus.IN_PRODUCTION.value,
    ProductionTrackingStatus.COMPLETED.value,
]
_RANK = {s: i for i, s in enumerate(_LADDER)}
# Manual transitions and who may drive them (§8c). System edges are unrestricted (the
# calling service already gated the action).
_MANUAL_ROLES = {UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.CUTTING_MANAGER}


class ProductionTrackingService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ProcurementRepository(db)

    async def ensure_for_bom(self, bom_id: uuid.UUID) -> ProductionTracking | None:
        """Get-or-create the tracker for a BOM's style/order."""
        bom = await self.repo.get_bom(bom_id)
        if bom is None or bom.client_order_id is None or bom.style_id is None:
            return None
        t = await self.repo.get_production_tracking(bom.client_order_id, bom.style_id)
        if t is None:
            t = ProductionTracking(client_order_id=bom.client_order_id, style_id=bom.style_id,
                                   bom_id=bom_id,
                                   status=ProductionTrackingStatus.AWAITING_BOM.value)
            self.db.add(t)
            await self.db.flush()
        elif t.bom_id is None:
            t.bom_id = bom_id
        return t

    async def _advance(self, t: ProductionTracking, target: str, *, actor=None,
                       force: bool = False) -> None:
        """Move a tracker forward to `target` (never backward unless `force`). System
        edges set actor=None; manual edges audit the actor + before/after."""
        if t is None:
            return
        if not force and _RANK.get(target, 0) <= _RANK.get(t.status, 0):
            return
        before = t.status
        t.status = target
        now = datetime.now(timezone.utc)
        if target == ProductionTrackingStatus.MATERIAL_READY.value and t.material_ready_at is None:
            t.material_ready_at = now
        if target == ProductionTrackingStatus.RELEASED_TO_PRODUCTION.value and t.released_at is None:
            t.released_at = now
        t.updated_by = getattr(actor, "id", None)
        self.db.add(AuditLog(
            actor_user_id=getattr(actor, "id", None), action="PRODUCTION_TRACKING_UPDATE",
            entity_type="production_tracking", entity_id=t.id,
            before={"status": before}, after={"status": target}, at=now))

    # ── system-driven edges (called by the procurement services) ─────────────
    async def on_po_raised(self, bom_id: uuid.UUID) -> None:
        t = await self.ensure_for_bom(bom_id)
        pos = await self.repo.pos_for_bom(bom_id)
        active = [p for p in pos if p.status != POStatus.CANCELLED.value]
        t.po_count = len(active)
        await self._advance(t, ProductionTrackingStatus.PO_RAISED.value)
        await self.db.commit()

    async def on_po_confirmed(self, bom_id: uuid.UUID) -> None:
        t = await self.ensure_for_bom(bom_id)
        pos = await self.repo.pos_for_bom(bom_id)
        active = [p for p in pos if p.status != POStatus.CANCELLED.value]
        confirmed = [p for p in active if p.status == POStatus.CONFIRMED.value]
        t.po_count = len(active)
        t.po_confirmed_count = len(confirmed)
        if active and len(confirmed) == len(active):
            await self._advance(t, ProductionTrackingStatus.PO_CONFIRMED.value)
            # all shortfalls confirmed → MATERIAL_READY emits the Stage-6 MD alert (owned
            # by Stage 6; here we advance the board + stamp the trigger time).
            await self._advance(t, ProductionTrackingStatus.MATERIAL_READY.value)
        await self.db.commit()

    async def on_bom_approved(self, bom_id: uuid.UUID) -> None:
        t = await self.ensure_for_bom(bom_id)
        await self._advance(t, ProductionTrackingStatus.BOM_APPROVED.value)
        await self.db.commit()

    async def on_inventory_checked(self, bom_id: uuid.UUID) -> None:
        t = await self.ensure_for_bom(bom_id)
        await self._advance(t, ProductionTrackingStatus.INVENTORY_CHECKED.value)
        await self.db.commit()

    # ── manual transition (DM/MD/Cutting) ────────────────────────────────────
    async def transition(self, user, tracking_id: uuid.UUID, target: str) -> dict:
        if getattr(user, "role", None) not in _MANUAL_ROLES \
                and getattr(user, "role", None) != UserRole.MANAGING_DIRECTOR:
            raise HTTPException(403, "Only DM/MD/Cutting may transition the board.")
        if target not in _RANK:
            raise HTTPException(422, detail={"error": "unknown_status", "status": target})
        t = await self.repo.get_production_tracking_by_id(tracking_id)
        if t is None:
            raise HTTPException(404, "Tracker not found.")
        # a manual nudge may move forward OR correct a stuck status (force) — the human
        # go/no-go for RELEASED_TO_PRODUCTION especially (§8c).
        await self._advance(t, target, actor=user, force=True)
        await self.db.commit()
        return {"id": str(t.id), "status": t.status}

    # ── board read (§8b) ──────────────────────────────────────────────────────
    async def board(self, *, client_id=None, order_id=None) -> dict:
        from app.modules.clients.service import ClientService
        cs = ClientService(self.db)
        client_names = {c.id: c.name for c in await cs.list_clients()}
        rows = await self.repo.list_production_tracking()
        out = []
        for t in rows:
            if order_id is not None and t.client_order_id != order_id:
                continue
            order = await cs.get_order(t.client_order_id)
            cid = getattr(order, "client_id", None)
            if client_id is not None and cid != client_id:
                continue
            style = await cs.get_style(t.style_id)
            out.append(present.tracking_view(t, order=order, style=style,
                                             client_name=client_names.get(cid)))
        return {"trackers": out, "count": len(out)}
