"""
================================================================================
modules/materials/service.py — Human-driven materials, inventory & suppliers
================================================================================
SEPARATE FROM THE BOM-DRIVEN INVENTORY MODULE. That module (inventory_item /
inventory_check) is Stage-4, August-20 scope, and is fed from an approved BOM.
THIS module is the floor-driven material_lot system: a cutting manager creates a
lot when material arrives, and every material — not just leather/lining — is
tracked with on-hand / reserved / available.

FOUR THINGS THIS SERVICE OWNS
    create_lot        a lot enters stock AND gets a child barcode, in one txn.
    stock             the DM's check: on-hand/reserved/available + shortfall.
    receive           approved adds stock + reserves the requirement; rejected is
                      logged for supplier quality history.
    decrement         the consumption hook the cutting log calls (idempotent).
================================================================================
"""
import uuid
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import (
    BarcodeType, MaterialCategory, SupplierOrderStatus, resolve_spec, uom_for,
)
from app.core.enums import UserRole, MaterialCategory, SheetStatus
from app.modules.barcode.repository import BarcodeRepository
from app.modules.materials.repository import MaterialRepository


# category → the child barcode type
_LOT_BARCODE_TYPE = {
    "LEATHER": BarcodeType.LEATHER_LOT,
    "LINING": BarcodeType.LINING_LOT,
    "ACCESSORY": BarcodeType.ACCESSORY_LOT,
}


# The fields that go into a lot's printed barcode caption, per category/subtype.
# Order matters — this is what the label reads left-to-right.
_CAPTION_FIELDS = {
    ("LEATHER", None): ["article", "colour", "thickness", "qty"],
    ("LINING", "PLAIN_LINING"): ["article", "colour", "thickness", "qty"],
    ("LINING", "RIBS"): ["article", "colour", "qty"],
    ("LINING", "KNIT"): ["article", "colour", "qty"],
    ("ACCESSORY", "BUTTON"): ["article", "colour", "size", "qty"],
    ("ACCESSORY", "ZIP"): ["article", "colour", "size", "qty"],
    ("ACCESSORY", "THREAD"): ["article", "colour", "thickness", "qty"],
    ("ACCESSORY", "OTHER"): ["article", "colour", "description", "qty"],
}


def display_stock(on_hand, used, active_reserved):
    """Present received, consumed/reserved, and remaining stock together.

    Pure — no DB, no self. It lives at module level because BOTH the service and
    StyleSpecService need it, and StyleSpecService only holds a MaterialRepository.
    It used to be a private staticmethod on MaterialService, and
    style_spec_service.py called it as `self.materials._display_stock(...)` where
    `self.materials` is a MaterialRepository — an AttributeError, i.e. a guaranteed
    500 on every style material-spec read that resolved a lot. Import this function;
    do not reach for a private attribute across the two objects again.
    """
    current = Decimal(str(on_hand or 0))
    consumed = Decimal(str(used or 0))
    committed = Decimal(str(active_reserved or 0))
    received = current + consumed
    reserved = consumed + committed
    return received, reserved, received - reserved


class MaterialService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MaterialRepository(db)
        self.barcodes = BarcodeRepository(db)
        # Set by decrement_for_cut_nocommit when a cut consumed more than was
        # available. Kept off the return value so the float signature (and its
        # existing callers) is unchanged; the caller reads it from the instance
        # it already holds. Always defined, so reading it before a decrement is
        # None rather than an AttributeError.
        self.last_decrement_warning: dict | None = None
        # THE KIT SPENDS SEVERAL LOTS IN ONE SCAN, so a single "last"
        # warning cannot describe it: four accessory lines can each be
        # short, and the operator needs all four. last_decrement_warning
        # is kept exactly as it was (production/service.py reads it after
        # its single cut decrement); this accumulates EVERY warning raised
        # on this instance, in order.
        self.last_used_after: float | None = None
        self.last_available_before: float | None = None
        self.decrement_warnings: list[dict] = []

    # Kept as a staticmethod so the existing in-class call sites are unchanged.
    # The real implementation is the module-level `display_stock` below, which
    # collaborators outside this class import directly — see the note there.
    _display_stock = staticmethod(display_stock)

    # ── create lot (+ child barcode + stock) ─────────────────────────────────
    async def create_lot(self, body) -> dict:
        """STRICT create: every field the product spec lists for this
        (category, subtype) MUST be present, else 422. article + colour are
        required on the lot itself; the rest are validated in `attributes` against
        MATERIAL_SPEC. The quantity is read from the category's own qty field
        (dcm / mtrs / kg / pcs / count), never a generic 'qty'."""
        cat = body.category.upper()
        if cat not in _LOT_BARCODE_TYPE:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Unknown category '{body.category}'.")
        subtype = (body.subtype or None)
        subtype = subtype.upper() if subtype else None

        spec = resolve_spec(cat, subtype)
        if spec is None:
            # ACCESSORY with no/unknown subtype, or an unrecognised lining subtype.
            hint = ("Accessory needs a subtype: BUTTON, ZIP, THREAD or OTHER."
                    if cat == "ACCESSORY"
                    else f"Unknown {cat} subtype '{body.subtype}'.")
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, hint)

        # article + colour are mandatory for EVERY material (spec: every line has
        # article + colour). article is a top-level field; colour too.
        if not body.article or not str(body.article).strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "article is required.")
        if not body.colour or not str(body.colour).strip():
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "colour is required.")
        # removing empty values
        attrs = {k: v for k, v in dict(body.attributes or {}).items()
                 if v is not None and str(v).strip() != ""}

        # STRICT: every required attribute key must be present and non-empty.
        missing = spec["required"] - set(attrs)
        
        # ", ".join(["colour", "dcm", "thickness"])

        # joins those strings using:

        # , 

        # (comma + space)

        # Result:

        # "colour, dcm, thickness"
        if missing:
            pretty = ", ".join(sorted(missing))
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{cat}{'/' + subtype if subtype else ''} lot requires: {pretty}. "
                f"(supplied: {', '.join(sorted(attrs)) or 'none'})")

        # quantity comes from the category's own field (dcm/mtrs/kg/pcs/count).
        qty_raw = attrs.get(spec["qty_field"])
        try:
            qty = Decimal(str(qty_raw))
        except Exception:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"{spec['qty_field']} must be a number.")
        if qty <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"{spec['qty_field']} (quantity) must be > 0.")

        uom = spec["qty_uom"]

        # ── OPTION A: ONE LOT PER MATERIAL SPEC ──────────────────────────────
        # A lot identifies WHAT the material is, not when it was bought. Buying
        # the same article/colour/thickness again is a TOP-UP of the existing
        # lot (POST /materials/receive), not a second row.
        #
        # Without this the picker's promise breaks: "filter article + colour +
        # thickness" would return two rows with no way for a cutting manager to
        # tell them apart, and the stock for one material would be split across
        # lots so neither shows the true on-hand. `receive` was already built to
        # top up an existing lot by id, so this makes the two halves agree.
        dup = await self.repo.find_duplicate_lot(
            category=cat, subtype=subtype, article=body.article,
            colour=body.colour, thickness=attrs.get("thickness"),
            size=attrs.get("size"))
        if dup is not None:
            # GOAT
            # BLACK
            # 0.8MM
            # M

            # And:

            # " · ".join(...)

            # combines them:

            # GOAT · BLACK · 0.8MM · M
            spec_desc = " · ".join(str(v) for v in
                                   [body.article, body.colour,
                                    attrs.get("thickness"), attrs.get("size")] if v)
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"A lot for {spec_desc} already exists ({float(dup.on_hand)} "
                f"{dup.uom} on hand). Material is one lot per spec — add this "
                f"delivery to it with POST /materials/receive using "
                f"lot_id={dup.id}, rather than creating a second lot.")

        lot = self.repo.add_lot_nocommit(
            category=cat, subtype=subtype, article=body.article,
            colour=body.colour, thickness=attrs.get("thickness"),
            size=attrs.get("size"), uom=uom, on_hand=qty,
            supplier_id=body.supplier_id, attributes=attrs, is_active=True,
        )
        await self.db.flush()   # need lot.id for the barcode

        # per-category caption: exactly the fields the spec lists for the label.
        #removing the unwanted fields from the body
        caption = self._caption(cat, subtype, body, attrs, qty, uom)
        bc = await self.barcodes.mint_lot_code_nocommit(
            lot.id, _LOT_BARCODE_TYPE[cat], caption)

        # THE HIDES, IF THIS DELIVERY WAS SHEETED. Same transaction as the lot
        # and its label: a lot that exists without the hides it was created with
        # is a stock figure nobody can trace, which is the state this feature
        # exists to end.
        # THE CREATE IS A DELIVERY TOO, and it needs its receipt row.
        #
        # Without this, `received` (bug #26) undercounts by exactly the opening
        # quantity: a lot created with 90 dcm and later topped up by 300 read
        # on_hand 390 / received 300, and the two disagreed with nothing to
        # explain the gap. The material physically arrived when the lot was
        # created — that is what creating it means — so it is a receipt like any
        # other and the history should show it as the first one.
        self.repo.add_receipt_nocommit(
            material_lot_id=lot.id, supplier_order_id=None,
            approved_qty=qty, rejected_qty=Decimal(0), received_by=None)

        sheets = await self._mint_sheets_for(lot, getattr(body, "sheets", None),
                                             declared_qty=qty)
        sheet_rows = [{"sheet_id": s.id, "code": s.code, "dcm": float(s.dcm),
                       "status": s.status, "cutting_row_id": None}
                      for s in sheets]

        await self.db.commit()
        await self.db.refresh(lot)

        return {
            "lot_id": lot.id, "lot_barcode": bc.code,
            "category": lot.category, "subtype": lot.subtype,
            "article": lot.article, "colour": lot.colour,
            "on_hand": float(lot.on_hand), "used": 0.0, "reserved": 0.0,
            "available": float(lot.on_hand), "uom": lot.uom,
            "sheets": sheet_rows,
        }

    async def _mint_sheets_for(self, lot, sheets, *, declared_qty=None) -> list:
        """Mint the hides a create/receive supplied, and check they add up.

        THE SUM IS CHECKED AND WARNED ABOUT, NOT ENFORCED. If the operator types
        ten hides that total 418 dcm against a declared 420, the delivery is
        still received: the discrepancy is two decimetres of measurement slop, and
        refusing the whole receipt over it would teach the store to stop sheeting.
        A LOUD difference is a different matter, and it shows up in the
        reconciliation block on the response where a human can see it.
        """
        if not sheets:
            return []
        minted = await self.mint_sheets_nocommit(lot, [
            s if isinstance(s, dict) else {"dcm": s.dcm, "note": s.note}
            for s in sheets])
        if declared_qty is not None:
            total = sum((Decimal(str(m.dcm)) for m in minted), Decimal(0))
            gap = Decimal(str(declared_qty)) - total
            if abs(gap) >= Decimal("0.001"):
                self.decrement_warnings.append({
                    "kind": "sheet_sum_mismatch",
                    "lot_id": str(lot.id),
                    "declared_qty": float(declared_qty),
                    "sheet_total": float(total),
                    "difference": float(gap),
                    "note": (f"{len(minted)} sheet(s) total {float(total):g} dcm "
                             f"but the delivery was entered as "
                             f"{float(declared_qty):g} dcm. The stock figure "
                             f"follows the delivery; check the sheet "
                             f"measurements."),
                })
        return minted

    # ══════════════════════════════════════════════════════════════════════
    # LEATHER SHEETS — one hide, one barcode
    # ══════════════════════════════════════════════════════════════════════
    # WHY THIS LIVES BESIDE THE LOT AND NOT IN THE CUTTING MODULE. A hide is a
    # STOCK item first: it is received, measured and shelved long before any
    # garment claims it, and it goes back to the shelf when a cutter returns it.
    # The cutting row is one episode in its life, not its owner.

    async def mint_sheets_nocommit(self, lot, sheets: list, *,
                                   received_at=None) -> list:
        """Create N hides against a lot and give each one its own barcode.

        NO COMMIT — the caller owns the transaction, so the hides, their labels
        and the receipt that brought them in all land together or not at all. A
        half-sheeted delivery is worse than an unsheeted one, because the stock
        figure and the hide count would disagree with nothing to say why.

        LEATHER ONLY, AND THAT IS A RULE ABOUT FUNGIBILITY, NOT A LIMITATION.
        A metre of lining is any other metre and a button is any other button —
        one code for the packet and a count that goes down says everything true
        about them. A hide is individually measured, individually priced and
        issued to one named cutter for one garment, so it is the only material
        here whose individual identity carries information.
        """
        from datetime import datetime, timezone
        if (lot.category or "").upper() != MaterialCategory.LEATHER.value:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"Only LEATHER is tracked sheet by sheet. This lot is "
                f"{lot.category}, which is measured by {lot.uom} and counted on "
                f"the lot, not per piece.")

        now = received_at or datetime.now(timezone.utc)
        seq = await self.repo.next_sheet_seq()
        out = []
        for entry in sheets:
            raw = entry.get("dcm") if isinstance(entry, dict) else getattr(entry, "dcm", None)
            note = entry.get("note") if isinstance(entry, dict) else getattr(entry, "note", None)
            try:
                dcm = Decimal(str(raw))
            except Exception:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    f"Sheet dcm must be a number (got {raw!r}).")
            if dcm <= 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "Every sheet needs a dcm greater than 0 — the measurement "
                    "written on the hide is what the whole ledger is built on.")
            seq += 1
            sheet = self.repo.add_sheet_nocommit(
                code=f"LS-{seq:06d}", material_lot_id=lot.id, dcm=dcm,
                status=SheetStatus.IN_STOCK.value, received_at=now, note=note)
            out.append(sheet)

        await self.db.flush()          # need sheet.id for each barcode
        for sheet in out:
            # SYNC, unlike mint_lot_code_nocommit: that one awaits _next_code to
            # derive a serial from the database. A sheet's code is already
            # allocated above, so there is nothing to look up.
            self.barcodes.mint_sheet_code_nocommit(
                sheet,
                caption=f"{lot.article}"
                        f"{' · ' + lot.colour if lot.colour else ''}"
                        f" · {float(sheet.dcm):g} dcm")
        return out

    async def sheet_reconciliation(self, lot) -> dict:
        """Does the hide count agree with the stock figure? REPORTED, not enforced.

        `lot.on_hand` stays the authority — it is what every existing consumption
        path, shortfall warning and analytics read already uses, and sheets are
        the detail beneath it. Making them agree by constraint would mean refusing
        a delivery nobody had time to sheet, which is how a floor learns to work
        around the system. So the mismatch is surfaced and left visible.
        """
        in_store = await self.repo.sheet_dcm_in_store(lot.id)
        on_hand = Decimal(str(lot.on_hand or 0))
        counts = await self.repo.sheet_counts_by_status(lot.id)
        sheeted = sum(v["count"] for v in counts.values())
        return {
            "sheets_total": sheeted,
            "sheets_by_status": counts,
            "sheet_dcm_in_store": float(in_store),
            "lot_on_hand": float(on_hand),
            "difference": float(on_hand - in_store),
            # A lot with no hides at all is not a mismatch, it is a lot received
            # before sheet tracking existed (or a delivery nobody sheeted).
            "reconciled": sheeted == 0 or abs(on_hand - in_store) < Decimal("0.001"),
        }
 
    @staticmethod
    def _caption(cat, subtype, body, attrs, qty, uom) -> str:
        key = (cat, None) if cat == "LEATHER" else (cat, subtype or "PLAIN_LINING")
        fields = _CAPTION_FIELDS.get(key, ["article", "colour", "qty"])
        values = {
            "article": body.article, "colour": body.colour,
            "thickness": attrs.get("thickness"), "size": attrs.get("size"),
            "description": attrs.get("description"), "qty": f"{qty} {uom}",
        }
        return " · ".join(str(values[f]) for f in fields if values.get(f))
 
    # ══════════════════════════════════════════════════════════════════════
    # LOT CRUD (change-list item 7) — the stock-management screen
    # ══════════════════════════════════════════════════════════════════════
    # Create already existed; read, correct and retire did not, so a typo in an
    # article code was permanent and a lot entered against the wrong material
    # could only be worked around by creating another. These three close it.
    #
    # WHAT IS DELIBERATELY *NOT* EDITABLE: category, subtype, and on_hand.
    #   • category/subtype decide the lot's UOM and its whole required-field set
    #     (MATERIAL_SPEC). Changing LEATHER→LINING would leave a lot measured in
    #     dcm claiming to be metres, and every cut event already pointing at it
    #     would silently re-denominate. That is a delete-and-recreate, not a patch.
    #   • on_hand is a LEDGER, not a field. It moves by receiving (POST
    #     /materials/receive) and by cutting (the two cut stages decrement it).
    #     A PATCH that set it directly would make the ledger and the stock
    #     disagree with no record of who changed what. Corrections go through
    #     `adjust` below, which is the same movement with a reason attached.

    async def get_lot(self, lot_id: uuid.UUID) -> dict:
        """One lot: identity, its three stock numbers, and its barcode."""
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        reserved = await self.repo.active_reserved(lot_id)
        barcodes = await self.repo.barcodes_by_lot([lot_id])
        spec = resolve_spec(lot.category, lot.subtype) or {}
        # BUG #26. `received` was rendering as 0 because no read path ever summed
        # the material_receipt rows — the number had no source, not a wrong one.
        totals = (await self.repo.received_totals([lot_id])).get(lot_id, {})
        # #18 — how many hides this lot has taken in, for the lot directory.
        sheets = await self.repo.sheet_counts_by_status(lot_id)
        return {
            "lot_id": lot.id, "barcode": barcodes.get(lot.id),
            "category": lot.category, "subtype": lot.subtype,
            "article": lot.article, "colour": lot.colour,
            "thickness": lot.thickness, "size": lot.size, "uom": lot.uom,
            "on_hand": float(lot.on_hand or 0),
            "reserved": float(reserved),
            "available": float((lot.on_hand or Decimal(0)) - reserved),
            # WHAT EVER ARRIVED, beside what is left. on_hand is the remainder
            # after consumption; received answers "how much of this have we
            # bought", which is a different question and the one the stock screen
            # was asking.
            "received": totals.get("received", 0.0),
            "rejected": totals.get("rejected", 0.0),
            "deliveries": totals.get("deliveries", 0),
            "sheets_total": sum(v["count"] for v in sheets.values()),
            "sheets_by_status": sheets,
            "attributes": dict(lot.attributes or {}),
            "supplier_id": lot.supplier_id,
            "is_active": bool(lot.is_active),
            # Echoed so the edit form can render exactly this material's fields
            # without a second call to /materials/spec.
            "editable_fields": sorted(
                {"article", "colour", "supplier_id"} | set(spec.get("filters", []))
                - {"article", "colour"}),
            "required_attributes": sorted(spec.get("required", [])),
        }

    async def lot_history(self, lot_id: uuid.UUID) -> dict:
        """Every delivery of one lot, newest first — #16.

        WHY THIS IS A READ AND NOT A NEW TABLE. material_receipt has recorded
        approved and rejected quantities on every receive since receiving was
        built; the gap was never the data, it was that nothing exposed it. So the
        "purchase history" feature is one query and a route, not a migration.
        """
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        rows = await self.repo.receipts_for_lot(lot_id)
        totals = (await self.repo.received_totals([lot_id])).get(lot_id, {})
        return {
            "lot_id": lot.id, "article": lot.article, "colour": lot.colour,
            "uom": lot.uom, "on_hand": float(lot.on_hand or 0),
            "received": totals.get("received", 0.0),
            "rejected": totals.get("rejected", 0.0),
            "receipts": [{
                "receipt_id": r.id,
                "approved_qty": float(r.approved_qty or 0),
                "rejected_qty": float(r.rejected_qty or 0),
                "supplier_order_id": r.supplier_order_id,
                "received_by": r.received_by,
                "received_at": r.created_at.isoformat() if r.created_at else None,
            } for r in rows],
        }

    async def page_leather_by_style(self, params, *, style_id=None):
        """One page of the arrived/consumed/available report, plus the total.

        PAGED WHERE THE COST IS. The first query is a GROUP BY returning one row
        per style — bounded by how many styles exist, not by pieces. What is
        expensive is the loop UNDER it: every row does a `find_lots`, then a
        `received_totals`, then an `active_reserved` per lot found. That is an
        N+1 whose N is the number of styles, and slicing the rows before the
        loop is what bounds it.

        So the slice is in Python, deliberately, and it is not a half-measure
        here: it removes the per-row queries, which are the actual work.
        """
        rows = await self.leather_by_style(style_id=style_id,
                                           _limit=params.limit,
                                           _offset=params.offset)
        total = len(await self.repo.consumption_by_style(style_id=style_id))
        return rows, total

    async def leather_by_style(self, *, style_id=None,
                               _limit: int | None = None,
                               _offset: int = 0) -> list:
        """Arrived / consumed / available, per style — #19.

        REPORTED: "the system only shows the available quantity — arrived and
        consumed quantities are not shown."

        All three now come from places that already record them, which is why
        this is a read and not a new ledger:
            arrived   Sigma material_receipt.approved_qty   (#26 made this real)
            consumed  Sigma production_event.consumption_qty at the cut stages
            available lot.on_hand minus active reservations

        THE CONSUMED FIGURE IS SPLIT BY REWORK (#3). "This style cost X, of which
        Y was rework" is the question the split exists for, and averaging the two
        hides how much the floor is losing to defects.
        """
        rows = await self.repo.consumption_by_style(style_id=style_id)
        rework = await self.repo.rework_consumption_by_style(style_id=style_id)

        # `_limit` is the paged caller's window, applied BEFORE the per-row lot
        # lookups below — see page_leather_by_style for why that is the point
        # that matters. None means "every row", which is what the unpaged
        # callers (and the tests) expect.
        if _limit is not None:
            rows = rows[_offset:_offset + _limit]

        out = []
        for row in rows:
            lots = await self.repo.find_lots(
                category=MaterialCategory.LEATHER.value,
                article=row["article"], colour=row["colour"])
            arrived = 0.0
            on_hand = 0.0
            reserved = 0.0
            if lots:
                totals = await self.repo.received_totals([l.id for l in lots])
                arrived = sum(v["received"] for v in totals.values())
                for lot in lots:
                    on_hand += float(lot.on_hand or 0)
                    reserved += float(await self.repo.active_reserved(lot.id))
            consumed = row["consumed"]
            rw = rework.get(row["style_id"], 0.0)
            out.append({
                **row,
                "arrived": arrived,
                "consumed": consumed,
                # The honest split: what the garments were supposed to cost, and
                # what defects added on top.
                "consumed_rework": rw,
                "consumed_original": max(0.0, consumed - rw),
                "on_hand": on_hand,
                "reserved": reserved,
                "available": on_hand - reserved,
                "per_piece": (consumed / row["pieces"]) if row["pieces"] else None,
            })
        return out

    async def piece_consumption(self, piece_id) -> dict:
        """What ONE garment took, hide by hide — #17 / #28.

        REPORTED: "there is no visibility into overall DCM spend or piece-level
        DCM consumption."
        """
        rows = await self.repo.consumption_by_piece(piece_id)
        total = sum(r["qty"] for r in rows)
        rework = sum(r["qty"] for r in rows if r["is_rework"])
        sheets = await self.repo.sheets_for_row_by_piece(piece_id)
        return {
            "piece_id": piece_id,
            "total": total,
            "rework": rework,
            "original": total - rework,
            "events": rows,
            # The actual hides, when the garment was cut through the new grid.
            "sheets": [{"code": sh.code, "dcm": float(sh.dcm or 0),
                        "status": sh.status} for sh in sheets],
        }

    async def update_lot(self, lot_id: uuid.UUID, patch: dict) -> dict:
        """Correct a lot's IDENTITY — article, colour, thickness, size, supplier.

        Re-runs the duplicate check: renaming a lot onto another lot's exact spec
        would give the picker two rows a cutting manager cannot tell apart and
        split one material's stock across both, which is the thing the
        one-lot-per-spec rule exists to prevent.
        """
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        
        #loop run four times for each blocked field and raise error if any of them is present in the patch
        for blocked in ("category", "subtype", "on_hand", "uom"):
            if patch.get(blocked) is not None:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"'{blocked}' cannot be patched — see the note in "
                    f"MaterialService. Change stock with POST /materials/receive "
                    f"or PATCH /materials/lots/{{id}}/adjust; change the material "
                    f"class by retiring this lot and creating the right one.")

        fields = {k: v for k, v in patch.items()
                  if k in {"article", "colour", "thickness", "size", "supplier_id"}
                  and v is not None}
        if not fields:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Nothing to update.")
        # here we are make a update
        # {
        #     "article": "GOAT",       # existing
        #     "colour": "BLACK",       # new
        #     "thickness": 0.8,        # existing
        #     "size": "M"              # existing
        # }
        candidate = {
            "article": fields.get("article", lot.article),
            "colour": fields.get("colour", lot.colour),
            "thickness": fields.get("thickness", lot.thickness),
            "size": fields.get("size", lot.size),
        }
        dup = await self.repo.find_duplicate_lot(
            category=lot.category, subtype=lot.subtype, **candidate)
        if dup is not None and dup.id != lot.id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Another lot already holds that spec ({float(dup.on_hand)} "
                f"{dup.uom} on hand). Material is one lot per spec — merge into "
                f"it with POST /materials/receive lot_id={dup.id} instead.")

        for key, value in fields.items():
            setattr(lot, key, value)
        # Keep the JSON attributes in step with the promoted columns, or the
        # printed caption and the filter columns start telling different stories.
        attrs = dict(lot.attributes or {})
        for key in ("thickness", "size"):
            if key in fields:
                attrs[key] = fields[key]
        lot.attributes = attrs
        await self.db.commit()
        await self.db.refresh(lot)
        return await self.get_lot(lot_id)

    async def adjust_lot(self, lot_id: uuid.UUID, *, delta: float, reason: str,
                         actor_id=None) -> dict:
        """A STOCK CORRECTION — a counted difference, with a reason, audited.

        This is the honest form of "edit the quantity": it records a MOVEMENT
        (+/- delta) rather than overwriting the number, so the ledger still adds
        up and someone can ask later why 40 dcm disappeared.

        Refuses to take on_hand below what is already reserved: that stock is
        committed to a cut, and a negative available is not a number anyone can
        act on.
        """
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Give a reason for a stock adjustment — it is the only record of "
                "why the counted stock and the system disagreed.")
        try:
            change = Decimal(str(delta))
        except Exception:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "delta must be a number.")
        if change == 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "delta must be non-zero.")

        reserved = await self.repo.active_reserved(lot_id)
        new_on_hand = (lot.on_hand or Decimal(0)) + change
        if new_on_hand < 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"That would take {lot.article} to {new_on_hand} {lot.uom}. "
                f"Stock cannot go negative.")
        if new_on_hand < reserved:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{float(reserved)} {lot.uom} of {lot.article} is reserved for a "
                f"cut. Adjusting to {float(new_on_hand)} would leave less stock "
                f"than is already committed. Release the reservation first.")

        before = float(lot.on_hand or 0)
        lot.on_hand = new_on_hand
        await self._audit(actor_id, "MATERIAL_STOCK_ADJUSTED", lot.id, {
            "article": lot.article, "colour": lot.colour, "uom": lot.uom,
            "before": before, "delta": float(change), "after": float(new_on_hand),
            "reason": reason})
        await self.db.commit()
        return await self.get_lot(lot_id)

    async def retire_lot(self, lot_id: uuid.UUID, *, actor_id=None) -> dict:
        """DEACTIVATE a lot and retire its barcode. Never a hard delete.

        Same principle as an employee leaving (CLAUDE.md §6): you remove the
        SCANNABLE CODE, never the record. Cut events point at this lot — deleting
        the row would orphan the consumption history that the costing and the
        traceability screens are built on.

        Refuses while stock is reserved: that stock is promised to a cut which
        would then have nothing to consume.
        """
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        reserved = await self.repo.active_reserved(lot_id)
        if reserved > 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{float(reserved)} {lot.uom} of {lot.article} is still reserved "
                f"for a cut. Release the reservation before retiring the lot.")

        lot.is_active = False
        retired = await self.barcodes.retire_lot_code_nocommit(lot.id)
        await self._audit(actor_id, "MATERIAL_LOT_RETIRED", lot.id, {
            "article": lot.article, "colour": lot.colour,
            "on_hand_at_retirement": float(lot.on_hand or 0),
            "barcode_retired": retired})
        await self.db.commit()
        return {
            "lot_id": lot.id, "is_active": False, "barcode_retired": retired,
            "on_hand": float(lot.on_hand or 0),
            "history_preserved": True,
            "message": (f"{lot.article} is retired. Its barcode no longer scans "
                        f"(410 Gone) and it is hidden from the lot picker; every "
                        f"cut recorded against it is untouched."),
        }

    async def _audit(self, actor_id, action: str, entity_id, after: dict) -> None:
        from datetime import datetime, timezone
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_id, action=action, entity_type="material_lot",
            entity_id=entity_id, after=after, at=datetime.now(timezone.utc)))

    # ── stock check ──────────────────────────────────────────────────────────
    async def available_for_lot(self, lot_id: uuid.UUID) -> float:
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            return 0.0
        reserved = await self.repo.active_reserved(lot_id)
        return float(lot.on_hand - reserved)

    # ── which filters apply to a category (drives the search UI) ─────────────
    @staticmethod
    def filter_fields(category: str, subtype: str | None) -> dict:
        """The fields the DM may filter this material by — so the frontend shows
        exactly the right search boxes (leather/lining/thread get thickness;
        buttons/zips get size; ribs/knit/other get just article+colour)."""
        spec = resolve_spec(category, subtype)
        return {
            "category": (category or "").upper() or None,
            "subtype": (subtype or None),
            "filters": spec["filters"] if spec else ["article", "colour"],
            "required_to_add": sorted(spec["required"]) if spec else [],
            "quantity_field": spec["qty_field"] if spec else None,
            "uom": spec["qty_uom"] if spec else None,
        }

    async def list_lots(self, *, category=None, subtype=None, article=None,
                        colour=None, thickness=None, size=None,
                        sku_id: uuid.UUID | None = None,
                        required: float | None = None) -> dict:
        """THE LOT PICKER — filter article/colour/thickness, get the lot to cut from.

        This is what `/materials/spec` and `/materials/stock` could not give you:
        spec returns the FORM (which boxes to render), stock returns the TOTALS
        (and throws the lot ids away). Neither hands the frontend a
        leather_lot_id, so there was no way to build a cut screen.

        AUTO-FILL (`sku_id`): flags the lot this SKU was last cut from, derived
        live from production_event — see repo.last_lot_for_sku for why it is
        derived rather than remembered, and why the key is the SKU (which
        carries colour) and not the style.

        `required` (the batch's total dcm/mtrs) marks which lots can actually
        cover this cut, so the UI can grey out ones that cannot.

        THREE queries regardless of how many lots match — the lots, their
        reservations, their barcodes — because this screen opens on every scan.
        """
        lots = await self.repo.find_lots(
            category=category, subtype=subtype, article=article,
            colour=colour, thickness=thickness, size=size)
        lot_ids = [lot.id for lot in lots]
        reserved_map = await self.repo.reserved_by_lot(lot_ids)
        barcode_map = await self.repo.barcodes_by_lot(lot_ids)

        last_used_id = None
        if sku_id is not None:
            # LINING lots suggest against the lining column, everything else
            # against leather — matching which cut screen will consume them.
            is_lining = (category or "").upper() == "LINING"
            last_used_id = await self.repo.last_lot_for_sku(sku_id, lining=is_lining)

        need = Decimal(str(required)) if required is not None else None
        items = []
        for lot in lots:
            reserved = reserved_map.get(lot.id, Decimal(0))
            available = (lot.on_hand or Decimal(0)) - reserved
            items.append({
                "lot_id": lot.id,
                "barcode": barcode_map.get(lot.id),
                "category": lot.category, "subtype": lot.subtype,
                "article": lot.article, "colour": lot.colour,
                "thickness": lot.thickness, "size": lot.size,
                "uom": lot.uom,
                "on_hand": float(self._display_stock(
                    lot.on_hand, lot.used, reserved)[0]),
                "used": float(lot.used or 0),
                "reserved": float(self._display_stock(
                    lot.on_hand, lot.used, reserved)[1]),
                "available": float(self._display_stock(
                    lot.on_hand, lot.used, reserved)[2]),
                # Pre-select this one in the UI, but SHOW it — never silently.
                "last_used_for_sku": lot.id == last_used_id,
                # None when the caller did not say how much it needs.
                "covers_required": None if need is None else bool(available >= need),
            })

        # Oldest first = FIFO, the order find_lots already returns. Then surface
        # the suggested lot at the top so the common case is the first row.
        items.sort(key=lambda i: (not i["last_used_for_sku"],))

        # IT TAKE A PARTICULAR FIELD LIKE EG COLOR IT ONLY LOOP THROUGH THE COLOR
        def _distinct(field: str) -> list:
            return sorted({i[field] for i in items if i[field] is not None})

        return {
            "count": len(items),
            "lots": items,
            # Drives the cascading dropdowns. `/materials/spec` says WHICH boxes
            # to show; this says what goes IN them, from the stock that exists.
            "options": {
                "article": _distinct("article"),
                "colour": _distinct("colour"),
                "thickness": _distinct("thickness"),
                "size": _distinct("size"),
            },
            "suggested_lot_id": last_used_id,
            "required": required,
        }

    async def stock(self, *, category=None, subtype=None, article=None,
                    colour=None, thickness=None, size=None,
                    required: float | None = None) -> dict:
        lots = await self.repo.find_lots(
            category=category, subtype=subtype, article=article,
            colour=colour, thickness=thickness, size=size)
        on_hand = Decimal(0)
        used = Decimal(0)
        active_reserved = Decimal(0)
        for lot in lots:
            on_hand += lot.on_hand
            used += lot.used or Decimal(0)
            active_reserved += await self.repo.active_reserved(lot.id)
        received, reserved, available = self._display_stock(
            on_hand, used, active_reserved)
        uom = lots[0].uom if lots else uom_for(category or "", subtype)

        out = {
            "category": (category or "").upper() or None,
            "subtype": (subtype or None),
            "article": article, "colour": colour, "thickness": thickness,
            "size": size, "uom": uom,
            "on_hand": float(received), "used": float(used),
            "reserved": float(reserved),
            "available": float(available),
            "lot_count": len(lots),
        }
        if required is not None:
            short = max(Decimal(0), Decimal(str(required)) - available)
            out["required"] = float(required)
            out["short_by"] = float(short)
            if short > 0 and article:
                sup = await self.repo.suggest_supplier(article)
                out["suggested_supplier"] = (
                    {"id": str(sup.id), "name": sup.name} if sup else None)
        return out

    # ── receive (approved / rejected) ────────────────────────────────────────
    async def receive(self, body, actor_id, actor_role=None) -> dict:
        """Approved adds to stock, rejected logged. NEW: when a supplier_order_id
        is supplied, the delivered lot must MATCH the order on
        article/colour/thickness/dcm. On mismatch:
            • default → 409 (rejected, nothing added)
            • approve_mismatch=True AND actor is DM/MD → the approved qty is
              received into a NEW lot carrying the DELIVERED spec (a substitution),
              audited, and the order is marked ARRIVED. The originally-targeted lot
              is never topped up with the wrong material."""
        lot = await self.repo.get_lot(body.lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lot not found.")
 
        approved = Decimal(str(body.approved_qty or 0))
        rejected = Decimal(str(body.rejected_qty or 0))
        if approved < 0 or rejected < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Quantities cannot be negative.")
 
        order = None
        mismatch = []
        substituted = False
        target_lot = lot
 
        if body.supplier_order_id:
            order = await self.repo.get_order(body.supplier_order_id)
            if not order:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier order not found.")
            mismatch = self._match_order_to_lot(order, lot)
            if mismatch:
                is_dm_md = actor_role in (UserRole.DIRECT_MANAGER,
                                          UserRole.MANAGING_DIRECTOR)
                if not (body.approve_mismatch and is_dm_md):
                    # reject: nothing enters stock
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        f"Received material does not match the order on: "
                        f"{', '.join(mismatch)}. A DM/MD may accept it as a "
                        f"substitution with approve_mismatch=true.")
                # DM/MD substitution: mint a NEW lot for the DELIVERED material,
                # copying the delivered lot's real spec, and receive into THAT.
                substituted = True
                new_attrs = dict(lot.attributes or {})
                target_lot = self.repo.add_lot_nocommit(
                    category=lot.category, subtype=lot.subtype, article=lot.article,
                    colour=lot.colour, thickness=lot.thickness, size=lot.size,
                    uom=lot.uom, on_hand=Decimal(0), supplier_id=lot.supplier_id,
                    attributes=new_attrs, is_active=True)
                await self.db.flush()
                from app.core.enums import BarcodeType
                _type = {"LEATHER": BarcodeType.LEATHER_LOT,
                         "LINING": BarcodeType.LINING_LOT,
                         "ACCESSORY": BarcodeType.ACCESSORY_LOT}[lot.category]
                await self.barcodes.mint_lot_code_nocommit(
                    target_lot.id, _type,
                    f"SUBSTITUTE · {lot.article} · {lot.colour or ''}")
 
        # add approved qty to whichever lot we settled on
        target_lot.on_hand = (target_lot.on_hand or 0) + approved
        self.repo.add_receipt_nocommit(
            material_lot_id=target_lot.id, supplier_order_id=body.supplier_order_id,
            approved_qty=approved, rejected_qty=rejected, received_by=actor_id)
 
        if body.reserve_for_required:
            self.repo.add_reservation_nocommit(
                target_lot.id, Decimal(str(body.reserve_for_required)),
                reason="receiving reservation")

        # THE HIDES IN THIS DELIVERY. Against target_lot, not `lot`: on an
        # approved substitution the leather physically went into the substitute
        # lot, and sheeting it to the ordered lot would file real hides under an
        # article nobody received.
        minted = await self._mint_sheets_for(
            target_lot, getattr(body, "sheets", None), declared_qty=approved)
 
        order_status = None
        if order and order.status != SupplierOrderStatus.ARRIVED.value:
            from datetime import datetime, timezone
            order.status = SupplierOrderStatus.ARRIVED.value
            order.arrived_at = datetime.now(timezone.utc)
            order_status = order.status
        elif order:
            order_status = order.status
 
        await self._audit(
            actor_id,
            "MATERIAL_RECEIVED_SUBSTITUTE" if substituted else "MATERIAL_RECEIVED",
            target_lot.id,
            {"approved": float(approved), "rejected": float(rejected),
             "mismatch_fields": mismatch or None,
             "original_lot_id": str(lot.id) if substituted else None})
        await self.db.commit()
        await self.db.refresh(target_lot)
 
        reserved = await self.repo.active_reserved(target_lot.id)
        return {
            "lot_id": target_lot.id,
            "on_hand": float(self._display_stock(
                target_lot.on_hand, target_lot.used, reserved)[0]),
            "used": float(target_lot.used or 0),
            "reserved": float(self._display_stock(
                target_lot.on_hand, target_lot.used, reserved)[1]),
            "available": float(self._display_stock(
                target_lot.on_hand, target_lot.used, reserved)[2]),
            "rejected_logged": float(rejected),
            "supplier_order_status": order_status,
            "substituted": substituted,
            "mismatch_fields": mismatch or None,
            "sheets": [{"sheet_id": s.id, "code": s.code, "dcm": float(s.dcm),
                        "status": s.status, "cutting_row_id": None}
                       for s in minted],
            # Only when this delivery was sheeted. A lot nobody sheets has
            # nothing to reconcile, and returning a zeroed block for it would
            # read as a mismatch rather than as an absence.
            "sheet_reconciliation": (
                await self.sheet_reconciliation(target_lot) if minted else None),
        }

    # ── consumption hooks (the cutting log and the store kit) ────────────────
    async def decrement_for_cut_nocommit(self, lot_id: uuid.UUID,
                                         qty: float) -> float:
        """Drop a lot's on_hand by qty (dcm/mtrs) at cutting. NO commit — the
        production log owns the transaction, so the event and the stock move are
        atomic. Returns available AFTER the decrement for the response.

        Idempotency is the CALLER's responsibility: production writes one event
        per piece per operation, so a piece cut once decrements once. A re-posted
        identical scan is caught upstream by the per-piece 'already logged' check.

        UNCHANGED SIGNATURE, RETURN TYPE AND MESSAGES. The body moved into
        _decrement_nocommit so the accessory kit can reuse the exact same
        warn-never-block arithmetic; every existing caller sees no difference.
        """
        return await self._decrement_nocommit(
            lot_id, qty, verb="Cut", context="at cutting",
            recorded="The cut WAS recorded", not_found="Consumption lot not found.")

    async def decrement_for_issue_nocommit(self, lot_id: uuid.UUID,
                                           qty: float) -> float:
        """Drop a lot's on_hand by qty when the store ISSUES it to a garment.

        Same engine, same warn-never-block rule, different vocabulary: nothing
        was cut here, a kit was issued. NO commit — DrawerService.store_scan owns
        the transaction so the ledger row, the stock move and the drawer flags
        land together or not at all.

        THE CALLER MUST HAVE RESOLVED THE LOT ALREADY. This still 404s on a
        missing lot, and a 404 mid-kit would abort a scan that had legitimately
        issued three other lines. StyleSpecService.issue_kit_nocommit therefore
        resolves every line FIRST and only calls this for lines that resolved —
        unresolvable ones come back as data in `unresolved`, not as an exception.
        """
        return await self._decrement_nocommit(
            lot_id, qty, verb="Issued", context="on this kit",
            recorded="The issue WAS recorded", not_found="Issue lot not found.")

    async def _decrement_nocommit(self, lot_id: uuid.UUID, qty: float, *,
                                  verb: str, context: str, recorded: str,
                                  not_found: str) -> float:
        """The one place stock comes off a lot. See the two wrappers above."""
        # LOCKED READ (not get_lot): everything below is a read-modify-write of
        # on_hand/used, and this method is reached concurrently by every cutting
        # and issuing scan on the floor. See get_lot_for_update for why.
        lot = await self.repo.get_lot_for_update(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, not_found)
        d = Decimal(str(qty))
        if d <= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Consumption must be > 0 {context}.")

        # ── STOCK VALIDATION: warn, never block ──────────────────────────────
        # Cutting more than the ledger says is on hand is a REAL and legitimate
        # event: stock drifts, a roll gets counted wrong, an offcut gets used.
        # The garment is physically on the table and already cut — refusing the
        # log would lose the production record to protect a number, and the floor
        # would work around it. So the cut is always recorded.
        #
        # What is NOT acceptable is doing it silently, which is what this did
        # before: `on_hand = on_hand - d` with no check, so a stale or wrong lot
        # quietly drove stock negative and nobody found out. Now the shortfall is
        # measured, returned, and surfaced in the /production/log response.
        reserved = await self.repo.active_reserved(lot_id)
        before = lot.on_hand or Decimal(0)
        available_before = before - reserved
        shortfall = d - available_before

        lot.on_hand = before - d
        lot.used = (lot.used or Decimal(0)) + d
        self.last_available_before = float(available_before)
        self.last_used_after = float(lot.used)
        self.last_decrement_warning = None
        if shortfall > 0:
            # `verb` is the only thing that differs between a cut and an issue:
            # "Cut 12.5 dcm of NAPPA" vs "Issued 4 pcs of BTN-4H". Both were
            # RECORDED, which is the sentence that matters to whoever reads this.
            self.last_decrement_warning = {
                "lot_id": str(lot_id),
                "article": lot.article,
                "colour": lot.colour,
                "uom": lot.uom,
                "requested": float(d),
                "available_before": float(available_before),
                "short_by": float(shortfall),
                "on_hand_after": float(lot.on_hand),
                "note": (
                    f"{verb} {float(d)} {lot.uom} of {lot.article}"
                    f"{' · ' + lot.colour if lot.colour else ''} but only "
                    f"{float(available_before)} {lot.uom} was available — short by "
                    f"{float(shortfall)}. {recorded}; stock now reads "
                    f"{float(lot.on_hand)} {lot.uom}. Check the physical count or "
                    f"whether the wrong lot was selected."),
            }
            # A kit decrements several lots on one service instance, so the
            # single "last" warning would report only the final line. Accumulate.
            self.decrement_warnings.append(self.last_decrement_warning)

        # RELEASE THE RESERVATION THIS CONSUMPTION JUST SATISFIED.
        #
        # A reservation is a claim on stock that has NOT been spent yet. Once it
        # IS spent, the claim must go, or the same quantity is subtracted twice:
        # once because on_hand fell, and again because the reservation is still
        # active in `available = on_hand - reserved`.
        #
        # Nothing called this before. add_reservation_nocommit was wired up at
        # receiving; consume_reservations_nocommit existed but had no caller in
        # the entire codebase, so every reservation ever created stayed "active"
        # forever. `available` therefore fell monotonically and never recovered:
        # given enough receipts every lot eventually reads as unavailable while
        # physically full, and the DM can no longer issue material.
        #
        # Oldest-first, and capped at what was actually consumed. Same
        # transaction as the stock move and the production event, so the three
        # land together or not at all.
        remaining_reserved = await self.repo.consume_reservations_nocommit(lot_id, d)
        return float(lot.on_hand - remaining_reserved)

    # ── supplier orders ──────────────────────────────────────────────────────
    async def create_order(self, body, actor_id, actor_role=None) -> dict:
        """Raise a manual supplier order (ORDERED). NEW: validate the requested
        spec against the supplier catalog before creating. If a supplier is named
        (or suggested) and does not supply this article, 422 — do not create an
        order the supplier cannot fill."""
        supplier = None
        if body.supplier_id:
            supplier = await self.repo.get_supplier(body.supplier_id)
            if supplier is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Supplier not found.")
            # explicit supplier MUST supply the article (hard 422)
            if not await self.repo.supplier_supplies(supplier, body.article):
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"Supplier '{supplier.name}' does not supply article "
                    f"'{body.article}'. Choose a supplier that carries it.")
        else:
            supplier = await self.repo.suggest_supplier(body.article)
            # suggestion is best-effort: an order with no supplier is allowed
            # (the DM assigns one later), so no 422 here.
 
        uom = uom_for(body.category, getattr(body, "subtype", None))
        cat = body.category.upper()
        dcm = None
        if getattr(body, "dcm", None) is not None:
            dcm = Decimal(str(body.dcm))
        order = self.repo.add_order_nocommit(
            category=cat, article=body.article, colour=body.colour,
            thickness=getattr(body, "thickness", None), dcm=dcm,
            qty=Decimal(str(body.qty)), uom=uom,
            status=SupplierOrderStatus.ORDERED.value,
            supplier_id=supplier.id if supplier else None, ordered_by=actor_id)
        await self.db.commit()
        await self.db.refresh(order)
        return {
            "order_id": order.id, "status": order.status,
            "article": order.article, "qty": float(order.qty), "uom": order.uom,
            "supplier": ({"id": str(supplier.id), "name": supplier.name}
                         if supplier else None),
        }
        
    # ── DM/MD edit an order's spec (NEW) ─────────────────────────────────────
    async def edit_order_spec(self, order_id, body, actor_id) -> dict:
        """Correct an order's article/colour/thickness/dcm/qty. DM/MD only
        (enforced in the router). Only allowed while ORDERED."""
        order = await self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
        if order.status != SupplierOrderStatus.ORDERED.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "Only an ORDERED order's spec may be edited.")
        before = {"article": order.article, "colour": order.colour,
                  "thickness": order.thickness,
                  "dcm": float(order.dcm) if order.dcm is not None else None,
                  "qty": float(order.qty)}
        if body.article is not None:   order.article = body.article
        if body.colour is not None:    order.colour = body.colour
        if body.thickness is not None: order.thickness = body.thickness
        if body.dcm is not None:       order.dcm = Decimal(str(body.dcm))
        if body.qty is not None:       order.qty = Decimal(str(body.qty))
        await self._audit(actor_id, "SUPPLIER_ORDER_SPEC_EDIT", order.id,
                          {"before": before, "after": {
                              "article": order.article, "colour": order.colour,
                              "thickness": order.thickness,
                              "dcm": float(order.dcm) if order.dcm is not None else None,
                              "qty": float(order.qty)}})
        await self.db.commit()
        await self.db.refresh(order)
        return {"order_id": order.id, "status": order.status}
 
    # ── PO-vs-received matching helper (NEW) ─────────────────────────────────
    @staticmethod
    def _match_order_to_lot(order, lot) -> list:
        """Return the list of fields that DIFFER between the ordered spec and the
        delivered lot. Empty list = match. Compares article, colour, thickness
        and dcm (dcm read from lot.attributes).

        A FIELD THE ORDER DID NOT SPECIFY IS NOT A MISMATCH. colour, thickness
        and dcm are all optional on SupplierOrderCreate: ordering "400 dcm of
        SUEDE-A32" without naming a colour means any colour of that article is
        acceptable. Comparing a null spec against the delivered lot's real
        attributes flagged every such order as a 3-field conflict and 409'd a
        perfectly good delivery — the DM could then only get it into stock by
        declaring it a substitution. Only what was actually ordered is checked;
        a wrong colour against an order that DID name one still conflicts."""
        diffs = []
        def norm(x):
            return (str(x).strip().upper() if x is not None else None)
        if norm(order.article) != norm(lot.article):
            diffs.append("article")
        if order.colour is not None and norm(order.colour) != norm(lot.colour):
            diffs.append("colour")
        if order.thickness is not None and norm(order.thickness) != norm(lot.thickness):
            diffs.append("thickness")
        # dcm: order.dcm (Decimal) vs lot.attributes["dcm"]
        if order.dcm is not None:
            lot_dcm = (lot.attributes or {}).get("dcm") if lot.attributes else None
            if float(order.dcm) != (float(lot_dcm) if lot_dcm is not None else None):
                diffs.append("dcm")
        return diffs

    async def mark_arrived(self, order_id: uuid.UUID) -> dict:
        order = await self.repo.get_order(order_id)
        if not order:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Order not found.")
        if order.status != SupplierOrderStatus.ARRIVED.value:
            from datetime import datetime, timezone
            order.status = SupplierOrderStatus.ARRIVED.value
            order.arrived_at = datetime.now(timezone.utc)
            await self.db.commit()
            await self.db.refresh(order)
        return {"order_id": order.id, "status": order.status,
                "arrived_at": order.arrived_at.isoformat() if order.arrived_at else None}

