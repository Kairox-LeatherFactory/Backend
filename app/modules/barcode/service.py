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

NO SQL LIVES HERE. EVERY READ IS A REPOSITORY CALL.
    For a PIECE code we need the piece's SKU/style/order + current stage + drawer
    + consumption. That is one column-scoped join in BarcodeRepository.piece_card;
    this service only shapes the row into the response dict. The repository selects
    the ~12 scalars the payload prints rather than hydrating Piece/SKU/Style/Drawer
    entities — a scan reads columns, it does not need mapped objects.
================================================================================
"""
import uuid

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import BarcodeStatus, BarcodeType
from app.core.store_display import holding_label
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
        # 404 unknown / 410 retired — F18: retirement applies to every barcode
        # type, not only EMPLOYEE.
        row = await self._get_active_or_410(code)
        return await self._payload_for(row)

    async def _payload_for(self, row) -> dict:
        """Compose the response from an already-resolved registry row, so callers
        that have the row (barcode_detail) don't re-read it."""
        out = {
            "code": row.code,
            "type": row.type,
            "active": row.status == BarcodeStatus.ACTIVE.value,
            "caption": row.caption,
            # True for a legacy long code kept alive after the compact-code switch
            # (bug #19). The scan still works; the UI can nudge the operator to
            # reprint the label with the small code.
            "is_alias": bool(getattr(row, "is_alias", False)),
        }
        if row.type == BarcodeType.PIECE.value and row.piece_id:
            out["piece"] = await self._piece_payload(row.piece_id)
        elif row.type == BarcodeType.EMPLOYEE.value and row.employee_id:
            out["employee"] = await self._employee_payload(row.employee_id)
        elif row.type == BarcodeType.DRAWER.value and row.drawer_id:
            out["drawer"] = await self._drawer_payload(row.drawer_id)
        elif row.material_lot_id:
            out["lot"] = await self._lot_payload(row.material_lot_id)
        out["next_expected_scan"] = self._next_expected_scan(out)
        return out

    @staticmethod
    def _next_expected_scan(payload: dict) -> str | None:
        """Which barcode the store screen should ask for NEXT (bug #11).

        The drawer and piece guns feed one merged input, so after a scan the
        operator has to work out which code the system is now waiting for. The
        server already knows: a drawer that is still accumulating wants its piece,
        and a piece whose drawer has not taken both parts wants that drawer. This
        is GUIDANCE ONLY — the authority on whether a scan is legal remains
        DrawerService.store_scan, which still rejects a piece scanned into the
        wrong drawer with a 409.
        """
        drawer = payload.get("drawer")
        if drawer is not None:
            # A drawer with no piece merged to it has nothing to ask for yet.
            return "PIECE" if drawer.get("current_piece_id") else None
        piece = payload.get("piece")
        if piece is not None:
            pd = piece.get("drawer") or {}
            if pd.get("code") and not (pd.get("leather_in") and pd.get("lining_in")):
                return "DRAWER"
        return None

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

    # ── payloads (shape only; the repository owns every query) ───────────────
    async def _piece_payload(self, piece_id: uuid.UUID) -> dict:
        r = await self.repo.piece_card(piece_id)
        if not r:
            return {"piece_id": str(piece_id)}
        short = (await self.repo.short_codes_for_pieces([piece_id])).get(piece_id)
        serial = f"{r.seq:03d}" if r.seq is not None else None
        return {
            "piece_id": str(r.id),
            "code": r.code,
            # The compact code the label now carries (bug #19). None for a piece
            # minted before the switch and not yet backfilled — the long `code`
            # above still scans, so this is a gap to fill, not a failure.
            "short_code": short,
            "sku_id": str(r.sku_id) if r.sku_id else None,
            "sku_code": r.sku_code,
            "style_id": str(r.style_id) if r.style_id else None,
            "style_name": r.style_name,
            # BUG #7/#19: the two fields the cutting screen was missing. `seq` was
            # already here as a bare int; `serial` is the zero-padded form the
            # client asks for by name ("001, 002 or 003").
            "article": r.article,
            "serial": serial,
            "colour": r.color_name or r.color_code,
            "size": r.size,
            "seq": r.seq,
            "order_id": str(r.order_id) if r.order_id else None,
            "order_number": r.order_number,
            "client": r.client_name,
            "current_stage": r.current_stage,
            # BUG #12: the drawer, in every payload that names a piece — so an
            # operator on any production stage can see where the garment lives
            # without opening the Store Management hub. Null pre-merge / post-ship
            # (a LEFT join, not an error); `drawer_code` is kept flat alongside it
            # for callers written against the old shape.
            "drawer_code": r.drawer_code,
            "drawer": None if not r.drawer_id else {
                "drawer_id": str(r.drawer_id),
                "code": r.drawer_code,
                "state": r.drawer_state,
                "holding": holding_label(leather_in=r.leather_in,
                                         lining_in=r.lining_in),
                "leather_in": bool(r.leather_in),
                "lining_in": bool(r.lining_in),
            },
            "leather_consumption_dcm": (
                float(r.consumption_qty) if r.consumption_qty is not None else None),
            "needs_lining": bool(r.needs_lining),
            # The sticker text, pre-joined so every screen prints it identically.
            "label_line": " · ".join(str(v) for v in [
                r.order_number, r.style_name, r.article,
                r.color_name or r.color_code, r.size, serial] if v),
        }

    async def _employee_payload(self, employee_id: uuid.UUID) -> dict:
        r = await self.repo.employee_card(employee_id)
        if not r:
            return {"employee_id": str(employee_id)}
        return {
            "employee_id": str(r.id),
            "name": r.name,
            "designation": r.designation,
            "wage_type": getattr(r.wage_type, "value", str(r.wage_type)),
            "is_active": r.is_active,
        }

    async def _drawer_payload(self, drawer_id: uuid.UUID) -> dict:
        r = await self.repo.drawer_card(drawer_id)
        if not r:
            return {"drawer_id": str(drawer_id)}
        return {
            "drawer_id": str(r.id), "drawer_code": r.code, "seq": r.seq,
            "state": r.state,
            "current_piece_id": str(r.current_piece_id) if r.current_piece_id else None,
            "leather_in": r.leather_in, "lining_in": r.lining_in,
            "holding": holding_label(leather_in=r.leather_in,
                                     lining_in=r.lining_in),
            "sent_to": r.sent_to,
        }

    async def _lot_payload(self, lot_id: uuid.UUID) -> dict:
        r = await self.repo.lot_card(lot_id)
        if not r:
            return {"lot_id": str(lot_id)}
        return {
            "lot_id": str(r.id), "category": r.category, "subtype": r.subtype,
            "article": r.article, "colour": r.colour, "thickness": r.thickness,
            "size": r.size, "uom": r.uom,
            "on_hand": float(r.on_hand), "available": float(r.available),
        }

    # ── employee barcode lifecycle ──────────────────────────────────────────
    async def issue_employee_barcode_nocommit(self, employee_id: uuid.UUID,
                                              caption: str | None) -> str:
        """Mint + register an employee's card. Caller commits (employees.create
        wraps it in the employee transaction so a worker never exists without a
        code)."""
        row = await self.repo.mint_employee_code_nocommit(employee_id, caption)
        await self.repo.flush()
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
        """The transition is audited in the SAME transaction as the retire/mint —
        the repo stages the row, repo.commit() lands both or neither."""
        self.repo.add_audit_nocommit(
            actor_id=actor_id, action=action, entity_id=entity_id, after=after)

    # ── print payload ────────────────────────────────────────────────────────
    async def print_payload(self, *, codes: list[str] | None = None,
                            sku_id: uuid.UUID | None = None,
                            order_id: uuid.UUID | None = None) -> dict:
        """The print run: a SMALL barcode plus the business identity to set under
        it. Backend renders no images (bug #19).

        `code` is what gets encoded — now the compact `PC-…` id, so the symbol is
        short enough to scan reliably. `details` is what gets TYPESET below it:
        order, article, style, colour, size and the 3-digit serial. Nothing is
        lost by shrinking the symbol, because the information moved to the text
        instead of into the bars.
        """
        resolved_codes: list[str] = list(codes or [])
        if sku_id or order_id:
            resolved_codes += await self.repo.piece_codes_for(
                sku_id=sku_id, order_id=order_id)

        captions = await self.repo.captions_for_codes(resolved_codes)
        # One query for the whole sheet, not one per label.
        details = await self.repo.label_details_for_codes(resolved_codes)
        labels = []
        for c in resolved_codes:
            norm = (c or "").strip().upper()
            known = norm in captions
            detail = details.get(norm)
            labels.append({
                "code": norm,
                "symbology": "code128",
                "caption": captions.get(norm) or (c or ""),
                "known": known,
                # None for a drawer / employee / lot label: those name no garment,
                # so there is no order·article·style line to print under them.
                "details": detail,
                "label_line": None if not detail else " · ".join(
                    str(v) for v in [
                        detail["order_number"], detail["style"], detail["article"],
                        detail["colour"], detail["size"], detail["serial"],
                    ] if v),
            })
        return {"labels": labels}
    
    # ── order picker ────────────────────────────────────────────────────────
    async def list_orders(self) -> list[dict]:
        """All orders that have barcodes. No client scoping (Hamthan #6)."""
        return await self.repo.list_orders_with_barcodes()
 
    # ── analytics: order totals + per-style breakdown ───────────────────────
    async def order_analytics(self, order_id: uuid.UUID) -> dict:
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
 
    async def list_order_skus(self, order_id: uuid.UUID) -> list[dict]:
        await self._assert_order_exists(order_id)
        return await self.repo.list_order_skus(order_id)

    # ── detail on click (reuses existing _piece_payload) ────────────────────
    async def barcode_detail(self, code: str) -> dict:
        """Full detail for a scanned/clicked code. No client tenancy (Hamthan #6).

        Same payload as resolve(); the registry row is read ONCE and handed to the
        composer (this used to resolve the code, then resolve it again)."""
        row = await self._get_active_or_410(code)   # 404 unknown / 410 retired
        return await self._payload_for(row)

    # ── order existence + order_number resolution (no client tenancy) ────────
    async def _assert_order_exists(self, order_id: uuid.UUID) -> None:
        """Order must exist. No client scoping (Hamthan #6): staff see all."""
        if not await self.repo.order_exists(order_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")

    async def resolve_order_id(self, order_ref: str | uuid.UUID) -> uuid.UUID:
        """Accept an order UUID OR the human order_number and return the id.

        The four /orders* routes take an id in the path today; this lets the
        picker resolve the human order_number a user types/scans to that id.
        """
        if isinstance(order_ref, uuid.UUID):
            await self._assert_order_exists(order_ref)
            return order_ref
        # try UUID string first, else treat as order_number
        try:
            oid = uuid.UUID(str(order_ref))
        except ValueError:
            pass
        else:
            await self._assert_order_exists(oid)
            return oid
        order_id = await self.repo.order_id_by_number(str(order_ref))
        if order_id is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                f"No order with number '{order_ref}'.")
        return order_id