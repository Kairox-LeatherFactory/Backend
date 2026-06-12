"""
================================================================================
modules/procurement/inventory_service.py — Stage-4 inventory orchestration
================================================================================

The business brain of Stage 4 (the workflow's procurement reality check). Owns:

  - the inventory-master sync (§2): preview (dry-run) + commit (idempotent upsert
    keyed on normalized_key; sheet wins on qty_on_hand; absent rows soft-deactivate);
  - the per-approval inventory CHECK (§4–§7): match every stockable bom_item to stock
    (deterministic key → alias → flagged fuzzy → unmatched), compute required / on_hand
    / available / shortfall / status, and RESERVE min(required, available) against the
    BOM via the soft ledger — all in one transaction, with the candidate fetch locked
    FOR UPDATE so two approvals can't double-spend the same stock;
  - the report shapes (§8) via inventory_presenters.

LAYERING. Reads only procurement-owned tables; the client/order/style identity the
dashboard groups by is resolved through clients.service (a permitted service→service
call), never the clients repository (CLAUDE.md §3.2). Blocking openpyxl work runs in a
threadpool (house async rule).
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.modules.procurement import inventory_presenters as present
from app.modules.procurement.enums import (
    BomItemCategory,
    BomStatus,
    InventoryCheckStatus,
    InventoryLineStatus,
    ReservationStatus,
)
from app.modules.procurement.inventory_import import parse_inventory
from app.modules.procurement.inventory_match import Alias, convert_on_hand, match_line
from app.modules.procurement.inventory_normalize import bom_line_key, normalize_key, tokens
from app.modules.procurement.models import (
    AuditLog,
    InventoryCheck,
    InventoryCheckLine,
    InventoryReservation,
)
from app.modules.procurement.repository import ProcurementRepository

# Stockable BOM categories (§4a). Manufacturing (cutting/stitching) + FOB charge are
# services/charges with no inventory → excluded from the check.
STOCKABLE = {
    BomItemCategory.MAIN_MATERIAL.value,
    BomItemCategory.SUB_MATERIAL.value,
    BomItemCategory.LINING.value,
    BomItemCategory.INTERLINING.value,
    BomItemCategory.THREAD.value,
    BomItemCategory.ACCESSORY.value,
    BomItemCategory.PACKAGING.value,
}
_APPROVED_STATES = {BomStatus.APPROVED.value, BomStatus.LOCKED.value, BomStatus.EXPORTED.value}
_ZERO = Decimal("0")


class InventoryService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ProcurementRepository(db)

    # ══════════════════════════════════════════════════════════════════════
    # Inventory master sync (§2)
    # ══════════════════════════════════════════════════════════════════════
    async def preview(self, data: bytes) -> dict:
        prev = await run_in_threadpool(parse_inventory, data)
        return present.preview_block(prev)

    async def commit(self, data: bytes) -> dict:
        prev = await run_in_threadpool(parse_inventory, data)
        keep: set[str] = set()
        for r in prev.rows:
            await self.repo.upsert_inventory_item(
                normalized_key=r.normalized_key, description=r.description, uom=r.uom,
                qty_on_hand=r.qty_on_hand, rate=r.rate, color=r.color,
            )
            keep.add(r.normalized_key)
        deactivated = await self.repo.deactivate_keys_not_in(keep)
        await self.repo.commit()
        return {**present.preview_block(prev), "committed": len(prev.rows),
                "deactivated": deactivated}

    async def list_items(self, *, search=None, limit=100, offset=0) -> dict:
        items = await self.repo.list_inventory_items(search=search, limit=limit, offset=offset)
        return {"items": [present.item_block(i) for i in items], "count": len(items)}

    # ══════════════════════════════════════════════════════════════════════
    # The inventory check (§4–§7)
    # ══════════════════════════════════════════════════════════════════════
    async def run_check(self, user, bom_id: uuid.UUID) -> dict:
        bom = await self.repo.get_bom(bom_id)
        if bom is None:
            raise HTTPException(404, "BOM not found.")
        if bom.status not in _APPROVED_STATES:
            raise HTTPException(409, detail={
                "error": "bom_not_approved",
                "message": "Inventory check runs only on an approved/locked BOM."})

        # (§3c) re-run releases the BOM's prior claims first, then re-claims fresh.
        await self.repo.release_reservations(bom_id, reason="recheck")

        stockable = [i for i in bom.items if i.category in STOCKABLE]
        excluded = [i for i in bom.items if i.category not in STOCKABLE]

        # ── build the line keys + the set-based candidate fetch (§5/§7) ───────
        aliases = [Alias(bom_term=normalize_key(a.bom_term),
                         inventory_key=normalize_key(a.inventory_key))
                   for a in await self.repo.active_aliases()]
        conversions = await self.repo.uom_conversions()

        exact_keys: set[str] = set()
        like_terms: set[str] = set()
        line_keys: dict[uuid.UUID, str] = {}
        for item in stockable:
            key = bom_line_key(item.name, item.material_color)
            line_keys[item.id] = key
            exact_keys.add(key)
            # alias-target + a longest-token probe widen the candidate pool just enough
            # for alias/fuzzy matching without loading the whole master.
            toks = tokens(key)
            if toks:
                like_terms.add(max(toks, key=len))
            for al in aliases:
                if tokens(al.bom_term) and tokens(al.bom_term) <= toks:
                    like_terms.add(al.inventory_key)

        candidates = await self.repo.fetch_inventory_candidates(
            exact_keys=exact_keys, like_terms=like_terms, lock=True)
        cand_ids = [c.id for c in candidates]
        reserved_other = await self.repo.active_reservation_sums(
            cand_ids, exclude_bom_id=bom_id)

        # ── header ────────────────────────────────────────────────────────────
        now = datetime.now(timezone.utc)
        check = InventoryCheck(bom_id=bom_id, status=InventoryCheckStatus.RUNNING.value,
                               run_at=now, run_by=getattr(user, "id", None))
        self.db.add(check)
        await self.db.flush()   # need check.id for the lines

        line_views: list[dict] = []
        for item in stockable:
            view = await self._check_line(check, item, line_keys[item.id], candidates,
                                          aliases, conversions, reserved_other)
            line_views.append(view)

        check.status = InventoryCheckStatus.COMPLETE.value
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action="INVENTORY_CHECK_RUN",
            entity_type="bom", entity_id=bom_id, at=now,
            after={"inventory_check_id": str(check.id), "lines": len(line_views)},
        ))
        await self.db.commit()
        check = await self.repo.get_inventory_check(check.id)
        return present.check_view(check, line_views, excluded)

    async def _check_line(self, check, item, bom_key, candidates, aliases,
                          conversions, reserved_other) -> dict:
        # required = the bulk requirement Stage-2 costing already computed
        # (order_qty × qty_per_garment); see costing.recompute_bom.
        required = Decimal(str(item.bulk_qty if item.bulk_qty is not None else 0))
        bom_uom = item.uom

        result = match_line(bom_key, item.material_color, candidates, aliases)
        flags: list[str] = []
        matched_primary = None
        on_hand = _ZERO
        mismatch = False

        if result.rows:
            # sum on-hand across lots (§6 edge 3), each converted into the BOM line UOM.
            for row in result.rows:
                conv, mm = convert_on_hand(row.qty_on_hand or _ZERO, row.uom, bom_uom, conversions)
                if mm:
                    mismatch = True
                else:
                    on_hand += conv
            matched_primary = max(result.rows, key=lambda r: (r.qty_on_hand or _ZERO))
            if mismatch and on_hand == _ZERO:
                flags.append("uom_mismatch")
        elif result.suggestion:
            flags.append("suggestion")
            flags.append("unmatched")
        else:
            flags.append("unmatched")

        reserved_other_qty = sum(
            (Decimal(str(reserved_other.get(r.id, 0))) for r in result.rows), _ZERO)
        available = on_hand - reserved_other_qty
        if available < _ZERO:
            available = _ZERO
        reserve = min(required, available)
        shortfall = required - reserve
        if shortfall < _ZERO:
            shortfall = _ZERO

        if available >= required and required > _ZERO and not (mismatch and on_hand == _ZERO):
            status = InventoryLineStatus.SUFFICIENT.value
        elif available > _ZERO:
            status = InventoryLineStatus.PARTIAL.value
        else:
            status = InventoryLineStatus.OUT_OF_STOCK.value

        line = InventoryCheckLine(
            inventory_check_id=check.id, bom_item_id=item.id,
            inventory_item_id=matched_primary.id if matched_primary else None,
            required_qty=required, on_hand_qty=on_hand, shortfall_qty=shortfall,
            status=status, matched_method=result.method,
            flags={"flags": flags, "suggestion": result.suggestion} if (flags or result.suggestion) else None,
        )
        self.db.add(line)
        await self.db.flush()

        # ── reserve the claim across the matched lots (§3) ────────────────────
        remaining = reserve
        for row in result.rows:
            if remaining <= _ZERO:
                break
            conv, mm = convert_on_hand(row.qty_on_hand or _ZERO, row.uom, bom_uom, conversions)
            if mm:
                continue
            lot_cap = conv - Decimal(str(reserved_other.get(row.id, 0)))
            if lot_cap <= _ZERO:
                continue
            take = min(remaining, lot_cap)
            self.db.add(InventoryReservation(
                inventory_item_id=row.id, bom_id=check.bom_id,
                inventory_check_line_id=line.id, qty=take,
                status=ReservationStatus.ACTIVE.value))
            remaining -= take

        return present.line_view(item, line, matched_primary, result, available,
                                 reserve, on_hand, flags)

    # ══════════════════════════════════════════════════════════════════════
    # Reads (§8)
    # ══════════════════════════════════════════════════════════════════════
    async def get_check(self, check_id: uuid.UUID) -> dict:
        check = await self.repo.get_inventory_check(check_id)
        if check is None:
            raise HTTPException(404, "Inventory check not found.")
        return await self._rebuild_view(check)

    async def latest_for_bom(self, bom_id: uuid.UUID) -> dict:
        check = await self.repo.latest_check_for_bom(bom_id)
        if check is None:
            raise HTTPException(404, "No inventory check for this BOM yet.")
        return await self._rebuild_view(check)

    async def dashboard(self, *, client_id=None, order_id=None) -> dict:
        from app.modules.clients.service import ClientService

        cs = ClientService(self.db)
        client_names = {c.id: c.name for c in await cs.list_clients()}
        checks = await self.repo.latest_checks()
        rows = []
        for c in checks:
            bom = await self.repo.get_bom(c.bom_id)
            if bom is None:
                continue
            if order_id is not None and bom.client_order_id != order_id:
                continue
            order = await cs.get_order(bom.client_order_id)
            style = await cs.get_style(bom.style_id)
            cid = getattr(order, "client_id", None)
            if client_id is not None and cid != client_id:
                continue
            rows.append({
                "client_id": cid,
                "client_name": client_names.get(cid),
                "client_order_id": bom.client_order_id,
                "order_number": getattr(order, "order_number", None),
                "style_id": bom.style_id,
                "style_name": getattr(style, "name", None),
                "bom_id": c.bom_id, "inventory_check_id": c.id,
                "badge": present.badge_for(c.lines),
                "shortfall_lines": sum(1 for ln in c.lines
                                       if ln.status != InventoryLineStatus.SUFFICIENT.value),
                "checked_at": c.run_at,
            })
        return present.dashboard_block(rows)

    async def _rebuild_view(self, check) -> dict:
        """Re-render a stored check from its persisted lines (no recompute on read —
        the line rows ARE the source of truth, house rule)."""
        # map matched inventory items + bom items for the presenter
        bom = await self.repo.get_bom(check.bom_id)
        items_by_id = {i.id: i for i in (bom.items if bom else [])}
        line_views = []
        for ln in check.lines:
            item = items_by_id.get(ln.bom_item_id)
            inv = await self.repo.get_inventory_item(ln.inventory_item_id) \
                if ln.inventory_item_id else None
            line_views.append(present.stored_line_view(item, ln, inv))
        excluded = [i for i in (bom.items if bom else []) if i.category not in STOCKABLE]
        return present.check_view(check, line_views, excluded)
