"""
================================================================================
modules/inventory/repository.py — Stage-4 inventory data access
================================================================================

The only place that talks to the DB for inventory-owned tables (inventory_item,
inventory_check[_line], inventory_reservation, material_alias, uom_conversion). BOM
data the check needs is fetched through bom.service (a DTO), never via the bom
repository/models.

FUNCTION GUIDE  (all async; called only by InventoryService)
  commit()   flush the session.
  inventory_item:
    get_inventory_item_by_key(key) / get_inventory_item(id) -> InventoryItem | None.
    upsert_inventory_item(...)        sheet-wins-on-qty upsert keyed on normalized_key.
    deactivate_keys_not_in(keep_keys) -> count   soft-deactivate rows absent from a re-sync.
    list_inventory_items(*, search, limit, offset)   the paged stock list.
  matching:
    fetch_inventory_candidates(*, exact_keys, like_terms, lock=True) -> [InventoryItem]
        the set-based candidate pull (NOT the whole table); with_for_update() locks the
        rows for the reservation claim (race-safety, §7).
    active_aliases() -> [MaterialAlias]              the curated synonyms.
    uom_conversions() -> {(from,to): factor}         the unit reference.
  reservations (the soft ledger):
    active_reservation_sums(item_ids, exclude_bom_id?) -> {item_id: Σ active qty}   the
        "committed by other BOMs" subtracted from on-hand to get `available`.
    release_reservations(bom_id, *, reason) -> count   free a BOM's claims (re-run/cancel).
  inventory_check:
    get_inventory_check(id) / latest_check_for_bom(bom_id) -> InventoryCheck | None (lines eager).
    latest_checks() -> [InventoryCheck]   one latest per BOM (for the dashboard).
================================================================================
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.modules.inventory.enums import ReservationStatus
from app.modules.inventory.models import (
    InventoryCheck,
    InventoryItem,
    InventoryReservation,
    MaterialAlias,
    UomConversion,
)


class InventoryRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def commit(self) -> None:
        await self.db.commit()

    # ── inventory_item (the master) ──────────────────────────────────────────
    async def get_inventory_item_by_key(self, normalized_key: str) -> InventoryItem | None:
        res = await self.db.execute(
            select(InventoryItem).where(InventoryItem.normalized_key == normalized_key))
        return res.scalars().first()

    async def get_inventory_item(self, item_id) -> InventoryItem | None:
        res = await self.db.execute(select(InventoryItem).where(InventoryItem.id == item_id))
        return res.scalar_one_or_none()

    async def upsert_inventory_item(self, *, normalized_key, description, uom,
                                    qty_on_hand, rate, color) -> InventoryItem:
        existing = await self.get_inventory_item_by_key(normalized_key)
        if existing:
            existing.qty_on_hand = qty_on_hand
            existing.description = description
            existing.uom = uom or existing.uom
            existing.rate = rate if rate is not None else existing.rate
            existing.color = color or existing.color
            existing.is_active = True
            return existing
        row = InventoryItem(
            description=description, normalized_key=normalized_key, uom=uom,
            qty_on_hand=qty_on_hand, rate=rate, color=color, is_active=True,
        )
        self.db.add(row)
        return row

    async def deactivate_keys_not_in(self, keep_keys: set[str]) -> int:
        res = await self.db.execute(
            select(InventoryItem).where(InventoryItem.is_active.is_(True)))
        n = 0
        for item in res.scalars():
            if item.normalized_key not in keep_keys:
                item.is_active = False
                n += 1
        return n

    async def list_inventory_items(self, *, search=None, limit=100, offset=0):
        stmt = select(InventoryItem).where(InventoryItem.is_active.is_(True))
        if search:
            stmt = stmt.where(InventoryItem.normalized_key.like(f"%{search.upper()}%"))
        stmt = stmt.order_by(InventoryItem.description).limit(limit).offset(offset)
        res = await self.db.execute(stmt)
        return list(res.scalars())

    # ── matching candidates (set-based; lock for the reservation claim) ──────
    async def fetch_inventory_candidates(self, *, exact_keys, like_terms, lock=True):
        conds = []
        if exact_keys:
            conds.append(InventoryItem.normalized_key.in_(exact_keys))
        for t in like_terms:
            if t:
                conds.append(InventoryItem.normalized_key.like(f"%{t}%"))
        if not conds:
            return []
        stmt = select(InventoryItem).where(InventoryItem.is_active.is_(True), or_(*conds))
        if lock:
            stmt = stmt.with_for_update()
        res = await self.db.execute(stmt)
        return list(res.scalars())

    async def active_aliases(self) -> list[MaterialAlias]:
        res = await self.db.execute(
            select(MaterialAlias).where(MaterialAlias.is_active.is_(True)))
        return list(res.scalars())

    async def uom_conversions(self) -> dict[tuple[str, str], object]:
        res = await self.db.execute(select(UomConversion))
        return {(r.from_uom.upper(), r.to_uom.upper()): r.factor for r in res.scalars()}

    # ── reservations (the soft allocation ledger) ────────────────────────────
    async def active_reservation_sums(self, item_ids, exclude_bom_id=None) -> dict:
        if not item_ids:
            return {}
        stmt = select(
            InventoryReservation.inventory_item_id,
            func.coalesce(func.sum(InventoryReservation.qty), 0),
        ).where(
            InventoryReservation.inventory_item_id.in_(item_ids),
            InventoryReservation.status == ReservationStatus.ACTIVE.value,
        )
        if exclude_bom_id is not None:
            stmt = stmt.where(InventoryReservation.bom_id != exclude_bom_id)
        stmt = stmt.group_by(InventoryReservation.inventory_item_id)
        res = await self.db.execute(stmt)
        return {row[0]: row[1] for row in res.all()}

    async def release_reservations(self, bom_id, *, reason: str) -> int:
        res = await self.db.execute(
            update(InventoryReservation)
            .where(InventoryReservation.bom_id == bom_id,
                   InventoryReservation.status == ReservationStatus.ACTIVE.value)
            .values(status=ReservationStatus.RELEASED.value,
                    released_at=datetime.now(timezone.utc), released_reason=reason)
        )
        return res.rowcount

    # ── inventory_check + lines ──────────────────────────────────────────────
    async def get_inventory_check(self, check_id) -> InventoryCheck | None:
        res = await self.db.execute(
            select(InventoryCheck).where(InventoryCheck.id == check_id)
            .options(selectinload(InventoryCheck.lines)))
        return res.scalar_one_or_none()

    async def latest_check_for_bom(self, bom_id) -> InventoryCheck | None:
        res = await self.db.execute(
            select(InventoryCheck).where(InventoryCheck.bom_id == bom_id)
            .options(selectinload(InventoryCheck.lines))
            .order_by(InventoryCheck.created_at.desc()))
        return res.scalars().first()

    async def latest_checks(self) -> list[InventoryCheck]:
        res = await self.db.execute(
            select(InventoryCheck).options(selectinload(InventoryCheck.lines))
            .order_by(InventoryCheck.created_at.desc()))
        seen: set = set()
        out: list[InventoryCheck] = []
        for c in res.scalars():
            if c.bom_id in seen:
                continue
            seen.add(c.bom_id)
            out.append(c)
        return out
