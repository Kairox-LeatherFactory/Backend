"""
================================================================================
modules/barcode/service.py — The one front door every scan resolves through
================================================================================

resolve(code) is called the instant any scanner or camera produces a string. It
returns the code's type + a type-specific live payload, so no screen needs to
know how to parse a barcode and a scanner gun and a phone camera are identical to
the caller.

INTEGRITY, NOT GUESSING.
    Unknown code → 404. Retired employee code → 410 Gone (distinguish 'never
    existed' from 'retired', so the UI can say "this card was deactivated" rather
    than "invalid"). A guess is never returned.

CROSS-MODULE READS GO THROUGH SERVICES, NEVER REPOSITORIES.
    For a PIECE code we need the piece's SKU/style/order + current stage + drawer
    + consumption. Those live in production/clients/drawers. We read them via
    their services so barcode stays liftable into its own process later.
================================================================================
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BarcodeStatus, BarcodeType
from app.modules.barcode.repository import BarcodeRepository


class BarcodeService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = BarcodeRepository(db)

    # ── resolve ──────────────────────────────────────────────────────────────
    async def _get_active_or_410(self, code: str):
        """Shared lookup: 404 if unknown, 410 if the barcode is retired — for ANY
        barcode type, not just employee (F18). Retirement is a lifecycle state on
        the registry; a retired piece/lot/drawer label must not silently resolve.
        Returns the row on success."""
        row = await self.repo.get_by_code(code)
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown barcode '{code}'.")
        if row.status == BarcodeStatus.RETIRED.value:
            raise HTTPException(
                status.HTTP_410_GONE,
                "This barcode was deactivated. The underlying record and history "
                "are intact; issue a new label to scan again.",
            )
        return row

    async def resolve(self, code: str) -> dict:
        row = await self.repo.get_by_code(code)
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown barcode '{code}'.")

        # F18: retirement applies to every barcode type, not only EMPLOYEE.
        if row.status == BarcodeStatus.RETIRED.value:
            raise HTTPException(
                status.HTTP_410_GONE,
                "This barcode was deactivated. The underlying record and history "
                "are intact; issue a new label to scan again.",
            )

        out = {
            "code": row.code,
            "type": row.type,
            "active": row.status == BarcodeStatus.ACTIVE.value,
            "caption": row.caption,
        }
        if row.type == BarcodeType.PIECE.value and row.piece_id:
            out["piece"] = await self._piece_payload(row.piece_id)
        elif row.type == BarcodeType.EMPLOYEE.value and row.employee_id:
            out["employee"] = await self._employee_payload(row.employee_id)
        elif row.type == BarcodeType.DRAWER.value and row.drawer_id:
            out["drawer"] = await self._drawer_payload(row.drawer_id)
        elif row.material_lot_id:
            out["lot"] = await self._lot_payload(row.material_lot_id)
        return out

    async def resolve_piece_id(self, code: str) -> uuid.UUID:
        """resolve() narrowed to 'give me the piece id or 404'. Used by /production/log
        when the target came in as a piece barcode."""
        row = await self._get_active_or_410(code)   # F18: 410 on retired
        if row.type != BarcodeType.PIECE.value or not row.piece_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"'{code}' is not a known piece barcode.")
        return row.piece_id

    async def resolve_employee_id(self, code: str) -> uuid.UUID:
        """resolve() narrowed to 'give me the (active) employee id'. 410 on retired."""
        row = await self._get_active_or_410(code)   # F18
        if row.type != BarcodeType.EMPLOYEE.value or not row.employee_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"'{code}' is not a known employee barcode.")
        return row.employee_id

    async def resolve_lot_id(self, code: str) -> uuid.UUID:
        row = await self._get_active_or_410(code)   # F18
        if not row.material_lot_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"'{code}' is not a known material-lot barcode.")
        return row.material_lot_id

    async def resolve_drawer_id(self, code: str) -> uuid.UUID:
        row = await self._get_active_or_410(code)   # F18
        if row.type != BarcodeType.DRAWER.value or not row.drawer_id:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"'{code}' is not a known drawer barcode.")
        return row.drawer_id

    # ── payloads (read via ORM directly here to avoid a service import cycle) ─
    async def _piece_payload(self, piece_id: uuid.UUID) -> dict:
        from app.modules.clients.models import SKU, Client, ClientOrder, Style
        from app.modules.production.models import Operation, Piece

        row = (await self.db.execute(
            select(Piece, SKU, Style, ClientOrder.order_number, Client.name,
                   Operation.code)
            .join(SKU, SKU.id == Piece.sku_id)
            .join(Style, Style.id == SKU.style_id)
            .join(ClientOrder, ClientOrder.id == Style.client_order_id)
            .join(Client, Client.id == ClientOrder.client_id)
            .outerjoin(Operation, Operation.id == Piece.current_operation_id)
            .where(Piece.id == piece_id)
        )).first()
        if not row:
            return {"piece_id": str(piece_id)}
        piece, sku, style, order_number, client, stage = row

        # drawer + consumption (best-effort; may be null pre-cut / pre-merge)
        drawer_code = None
        drawer_id = getattr(piece, "drawer_id", None)
        if drawer_id:
            from app.modules.barcode.models import Drawer
            drawer = await self.db.get(Drawer, drawer_id)
            drawer_code = drawer.code if drawer else None

        consumption = await self._piece_consumption(piece_id)
        return {
            "piece_id": str(piece.id),
            "code": piece.code,
            "sku_code": sku.code,
            "style_name": style.name,
            "colour": sku.color_name or sku.color_code,
            "size": sku.size,
            "seq": piece.seq,
            "order_number": order_number,
            "client": client,
            "current_stage": stage,
            "drawer_code": drawer_code,
            "leather_consumption_dcm": consumption,
            "needs_lining": bool(getattr(piece, "needs_lining", True)),
        }

    async def _piece_consumption(self, piece_id: uuid.UUID):
        from app.modules.production.models import ProductionEvent
        # The leather-cut event's consumption for this piece, if recorded.
        val = await self.db.scalar(
            select(ProductionEvent.consumption_qty)
            .where(ProductionEvent.piece_id == piece_id,
                   ProductionEvent.consumption_qty.isnot(None))
            .order_by(ProductionEvent.created_at)
            .limit(1)
        )
        return float(val) if val is not None else None

    async def _employee_payload(self, employee_id: uuid.UUID) -> dict:
        from app.modules.employees.models import Employee
        emp = await self.db.get(Employee, employee_id)
        if not emp:
            return {"employee_id": str(employee_id)}
        return {
            "employee_id": str(emp.id),
            "name": emp.name,
            "designation": emp.designation,
            "wage_type": getattr(emp.wage_type, "value", str(emp.wage_type)),
            "is_active": emp.is_active,
        }

    async def _drawer_payload(self, drawer_id: uuid.UUID) -> dict:
        from app.modules.barcode.models import Drawer
        d = await self.db.get(Drawer, drawer_id)
        if not d:
            return {"drawer_id": str(drawer_id)}
        return {
            "drawer_id": str(d.id), "drawer_code": d.code, "seq": d.seq,
            "state": d.state, "current_piece_id": str(d.current_piece_id) if d.current_piece_id else None,
            "leather_in": d.leather_in, "lining_in": d.lining_in,
        }

    async def _lot_payload(self, lot_id: uuid.UUID) -> dict:
        from app.modules.barcode.models import MaterialLot
        lot = await self.db.get(MaterialLot, lot_id)
        if not lot:
            return {"lot_id": str(lot_id)}
        # available needs the reservation sum — delegate to MaterialService lazily.
        from app.modules.materials.service import MaterialService
        avail = await MaterialService(self.db).available_for_lot(lot.id)
        return {
            "lot_id": str(lot.id), "category": lot.category, "subtype": lot.subtype,
            "article": lot.article, "colour": lot.colour, "thickness": lot.thickness,
            "size": lot.size, "uom": lot.uom,
            "on_hand": float(lot.on_hand), "available": avail,
        }

    # ── employee barcode lifecycle ──────────────────────────────────────────
    async def issue_employee_barcode_nocommit(self, employee_id: uuid.UUID,
                                              caption: str | None) -> str:
        """Mint + register an employee's card. Caller commits (employees.create
        wraps it in the employee transaction so a worker never exists without a
        code)."""
        row = await self.repo.mint_employee_code_nocommit(employee_id, caption)
        await self.db.flush()
        return row.code

    async def employee_codes(self, employee_ids: list[uuid.UUID],
                             active_only: bool = True) -> dict[uuid.UUID, str]:
        """employee_id → active card code, batched. Used by the roster read so
        each row can carry the code the user scans / clicks through to
        PATCH /employees/{id}/barcode with."""
        return await self.repo.codes_for_employees(employee_ids, active_only)

    async def reissue_employee_barcode(self, employee_id: uuid.UUID,
                                       actor_id: uuid.UUID | None) -> dict:
        """Retire the old card, mint a new one. History untouched."""
        old = await self.repo.get_for_employee(employee_id, active_only=True)
        if old:
            await self.repo.retire_nocommit(old, reason="reissued")
        caption = old.caption if old else None
        new = await self.repo.mint_employee_code_nocommit(employee_id, caption)
        await self._audit(actor_id, "EMPLOYEE_BARCODE_REISSUE", employee_id,
                          {"old": old.code if old else None, "new": new.code})
        await self.repo.commit()
        return {"employee_id": str(employee_id), "employee_barcode": new.code,
                "active": True, "history_preserved": True}

    async def deactivate_employee_barcode(self, employee_id: uuid.UUID,
                                          actor_id: uuid.UUID | None) -> dict:
        """Retire the card (worker left). Employee row + all events + closed wage
        lines are untouched — this ONLY flips the registry status."""
        row = await self.repo.get_for_employee(employee_id, active_only=True)
        if not row:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "No active barcode for this employee.")
        await self.repo.retire_nocommit(row, reason="left_company")
        await self._audit(actor_id, "EMPLOYEE_BARCODE_DEACTIVATE", employee_id,
                          {"code": row.code})
        await self.repo.commit()
        return {"employee_id": str(employee_id), "employee_barcode": row.code,
                "active": False, "history_preserved": True}

    async def _audit(self, actor_id, action, entity_id, after: dict) -> None:
        from datetime import datetime, timezone
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_id, action=action, entity_type="employee_barcode",
            entity_id=entity_id, after=after, at=datetime.now(timezone.utc)))

    # ── print payload ────────────────────────────────────────────────────────
    async def print_payload(self, *, codes: list[str] | None = None,
                            sku_id: uuid.UUID | None = None,
                            order_id: uuid.UUID | None = None) -> dict:
        """Return {code, symbology, caption} for a set of codes so the frontend
        can render Code128 labels. Backend renders no images."""
        resolved_codes: list[str] = list(codes or [])

        if sku_id or order_id:
            from app.modules.clients.models import SKU, Style
            from app.modules.production.models import Piece
            stmt = select(Piece.code).join(SKU, SKU.id == Piece.sku_id)
            if sku_id:
                stmt = stmt.where(Piece.sku_id == sku_id)
            if order_id:
                stmt = stmt.join(Style, Style.id == SKU.style_id).where(
                    Style.client_order_id == order_id)
            resolved_codes += [c for (c,) in (await self.db.execute(stmt)).all()]

        rows = await self.repo.get_many(resolved_codes)
        found = {r.code: r for r in rows}
        labels = []
        for c in resolved_codes:
            r = found.get((c or "").strip().upper())
            labels.append({
                "code": r.code if r else (c or "").strip().upper(),
                "symbology": "code128",
                "caption": (r.caption if r else None) or (c or ""),
                "known": r is not None,
            })
        return {"labels": labels}
    
    # ── order picker ────────────────────────────────────────────────────────
    async def list_orders(self) -> list[dict]:
        """All orders that have barcodes. No client scoping (Hamthan #6)."""
        return await self.repo.list_orders_with_barcodes()
 
    # ── analytics: order totals + per-style breakdown ───────────────────────
    async def order_analytics(
        self, order_id: uuid.UUID, client_scope: uuid.UUID | None
    ) -> dict:
        await self._assert_order_exists(order_id)
 
        planned = await self.repo.order_planned_total(order_id)
        minted = await self.repo.order_minted_total(order_id)
        active = await self.repo.order_active_total(order_id)
        distinct = await self.repo.order_distinct_code_total(order_id)
        styles = await self.repo.style_breakdown(order_id)
 
        balance = max(planned - minted, 0)
        return {
            "order_id": order_id,
            "order_total": {
                "planned": planned,
                "generated": minted,
                "balance": balance,
                "active": active,
                "retired": minted - active,
                # integrity: distinct codes must equal minted; if not, the unique
                # index was somehow bypassed. Surfaced so the UI can prove it.
                "duplicates": max(minted - distinct, 0),
                # a positive balance = premint did not finish (or has not run) for
                # this order. A visible flag, not a silent gap.
                "half_minted": balance > 0,
                "fully_generated": balance == 0 and minted > 0,
            },
            "by_style": styles,   # each: planned / minted / balance per style
        }
 
    # ── filterable history list ─────────────────────────────────────────────
    async def list_history(
        self,
        order_id: uuid.UUID,
        client_scope: uuid.UUID | None,
        *,
        sku_id: uuid.UUID | None = None,
        style_id: uuid.UUID | None = None,
        size: str | None = None,
        status_filter: str | None = None,
        date_from=None,
        date_to=None,
        page: int = 1,
        page_size: int = 50,
    ) -> dict:
        await self._assert_order_exists(order_id)
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)   # hard ceiling; JP is ~1273 rows
 
        items, total = await self.repo.list_barcodes(
            order_id, sku_id=sku_id, style_id=style_id, size=size,
            status=status_filter, date_from=date_from, date_to=date_to,
            page=page, page_size=page_size,
        )
        return {
            "order_id": order_id,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
            "items": items,
        }
 
    async def list_order_skus(
        self, order_id: uuid.UUID, client_scope: uuid.UUID | None
    ) -> list[dict]:
        await self._assert_order_visible(order_id, client_scope)
        return await self.repo.list_order_skus(order_id)
 
    # ── detail on click (reuses existing _piece_payload) ────────────────────
    async def barcode_detail(self, code: str) -> dict:
        """Full detail for a scanned/clicked code. No client tenancy (Hamthan #6)."""
        await self._get_active_or_410(code)   # 404 unknown / 410 retired
        return await self.resolve(code)
 
    # ── order existence + order_number resolution (no client tenancy) ────────
    async def _assert_order_exists(self, order_id: uuid.UUID) -> None:
        """Order must exist. No client scoping (Hamthan #6): staff see all."""
        from app.modules.clients.models import ClientOrder
        if await self.db.get(ClientOrder, order_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")

    async def resolve_order_id(self, order_ref: str | uuid.UUID) -> uuid.UUID:
        """Accept an order UUID OR the human order_number and return the id.

        The four /orders* routes take an id in the path today; this lets the
        picker resolve the human order_number a user types/scans to that id.
        """
        if isinstance(order_ref, uuid.UUID):
            await self._assert_order_exists(order_ref)
            return order_ref
        from app.modules.clients.models import ClientOrder
        # try UUID string first, else treat as order_number
        try:
            oid = uuid.UUID(str(order_ref))
            await self._assert_order_exists(oid)
            return oid
        except ValueError:
            pass
        row = await self.db.scalar(
            select(ClientOrder.id).where(
                ClientOrder.order_number == str(order_ref).strip()))
        if row is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"No order with number '{order_ref}'.")
        return row