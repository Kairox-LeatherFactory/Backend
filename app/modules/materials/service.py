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
    """THE THREE NUMBERS THE FLOOR ACTUALLY ASKS FOR: arrived, used, balance.

    Returns `(arrived, used, balance)`.

        arrived   everything that ever came in for this material = on_hand + used
        used      what has physically been cut or issued out of it (lot.used)
        balance   what is left on the shelf                       (lot.on_hand)

    `arrived − used == balance` ALWAYS, by construction, because arrived is
    derived from the other two rather than summed from a different table. That
    identity is the whole point: a stock screen whose three figures do not
    reconcile teaches people to stop believing any of them.

    WHAT THIS USED TO RETURN, AND WHY IT WAS WRONG. The second element used to be
    `used + active_reserved` under the name "reserved". Consumption is not a
    reservation — a reservation is a claim on stock that has NOT been spent, and
    stock that HAS been spent is already out of on_hand. Adding the two produced a
    "reserved" figure that grew with every cut, so the screen showed a lot as more
    and more committed the more of it was used up, and "used" itself was nowhere.
    `available` is unaffected either way (the term cancels), which is why this
    survived: the one number anybody checked was right for the wrong reason.

    Reservations are still real and still subtract from what can be promised —
    they are reported separately, as `reserved`, and `available = balance −
    reserved`. See MaterialService.stock_numbers.

    Pure — no DB, no self. It lives at module level because BOTH the service and
    StyleSpecService need it, and StyleSpecService only holds a MaterialRepository.
    It used to be a private staticmethod on MaterialService, and
    style_spec_service.py called it as `self.materials._display_stock(...)` where
    `self.materials` is a MaterialRepository — an AttributeError, i.e. a guaranteed
    500 on every style material-spec read that resolved a lot. Import this function;
    do not reach for a private attribute across the two objects again.
    """
    balance = Decimal(str(on_hand or 0))
    consumed = Decimal(str(used or 0))
    return balance + consumed, consumed, balance


def stock_numbers(on_hand, used, active_reserved) -> dict:
    """One block of stock figures, spelled out, used by every material read.

    Every screen that shows stock shows the same six numbers, and they were being
    assembled by hand at five call sites with three different meanings for
    `on_hand`. This is the single shape:

        arrived    on_hand + used      everything that ever came in
        used       lot.used            cut or issued out of it
        balance    lot.on_hand         what is on the shelf now
        reserved   Σ active            committed to a requirement, not yet spent
        available  balance − reserved  what may be promised to something new
        on_hand    == balance          the legacy key, kept so existing callers
                                       and saved API clients do not break

    `on_hand` MEANS BALANCE HERE, and on `/materials/stock` it used to mean
    `arrived`. That was the bug: two endpoints used one word for two quantities,
    so a manager comparing the stock screen with a lot's own page saw numbers that
    could not both be right. Read `arrived`, `used` and `balance` — they say what
    they are.
    """
    arrived, consumed, balance = display_stock(on_hand, used, active_reserved)
    reserved = Decimal(str(active_reserved or 0))
    return {
        "arrived": float(arrived),
        "used": float(consumed),
        "balance": float(balance),
        "reserved": float(reserved),
        "available": float(balance - reserved),
        "on_hand": float(balance),
    }


# Which SheetStatus values belong in which bucket of the sheet-wise roll-up.
# The dcm ledger above answers "how much leather"; this answers "how many
# hides", and the floor counts in both — a cutter is handed SKINS, and a store
# check is a count of what is on the shelf, not a sum of decimetres.
_SHEET_BUCKETS = {
    # On the shelf and claimable by a cutting row.
    "balance": ("IN_STOCK", "RETURNED"),
    # Out on a row but not yet cut — spoken for, still physically here.
    "allocated": ("ALLOCATED", "ISSUED"),
    # Gone: the cutting event was logged and its dcm is spent.
    "used": ("CONSUMED",),
    # Left stock without ever being cut.
    "scrapped": ("SCRAPPED",),
}


def sheet_rollup(counts_by_status: dict) -> dict:
    """{status: {count, dcm}} → the four buckets, plus the totals.

    Pure, like display_stock, and for the same reason: the lot page, the stock
    screen and the lot directory all render it and none of them should be
    computing it themselves.
    """
    out = {}
    for bucket, statuses in _SHEET_BUCKETS.items():
        count = sum(int(counts_by_status.get(s, {}).get("count", 0))
                    for s in statuses)
        dcm = sum(float(counts_by_status.get(s, {}).get("dcm", 0.0))
                  for s in statuses)
        out[f"sheets_{bucket}"] = count
        out[f"sheets_{bucket}_dcm"] = round(dcm, 3)
    out["sheets_arrived"] = sum(int(v.get("count", 0))
                                for v in counts_by_status.values())
    out["sheets_arrived_dcm"] = round(
        sum(float(v.get("dcm", 0.0)) for v in counts_by_status.values()), 3)
    out["sheets_by_status"] = counts_by_status
    return out


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
            # A brand-new lot has nothing used and nothing reserved, so arrived
            # == balance == on_hand. Built through the same helper as every other
            # read so the shape cannot drift from them.
            **stock_numbers(lot.on_hand, Decimal(0), Decimal(0)),
            # The hides this create minted are all IN_STOCK by definition — they
            # were measured a moment ago and nothing has claimed one — so the
            # roll-up is built from them rather than re-queried.
            **sheet_rollup({SheetStatus.IN_STOCK.value: {
                "count": len(sheets),
                "dcm": round(sum(float(x.dcm or 0) for x in sheets), 3),
            }} if sheets else {}),
            "uom": lot.uom,
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
        """One lot: identity, its stock numbers — in dcm AND in hides — and its
        barcode.

        `used` USED TO BE A LIE HERE, and it was the only figure on the page
        nobody could act on. The key was declared on the response schema with a
        default of 0.0 and this method never set it, so every lot in the building
        reported "used: 0" however much of it had been cut. It is `lot.used`, the
        same column `_decrement_nocommit` moves on every cut and every kit issue.

        THE THREE THAT MATTER: `arrived` came in, `used` went out, `balance` is
        left — and they reconcile. `reserved` / `available` sit beside them for
        what is promised. The same three are given SHEET-WISE, because a store
        check counts skins, not decimetres.
        """
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
        pending = (await self.repo.pending_intake_by_lot([lot_id])).get(lot_id) or {}
        return {
            "lot_id": lot.id, "barcode": barcodes.get(lot.id),
            "category": lot.category, "subtype": lot.subtype,
            "article": lot.article, "colour": lot.colour,
            "thickness": lot.thickness, "size": lot.size, "uom": lot.uom,
            # arrived / used / balance / reserved / available / on_hand
            **stock_numbers(lot.on_hand, lot.used, reserved),
            # WHAT WAS BOUGHT, beside what arrived. `received` sums the delivery
            # rows and so answers "how much of this have we ever paid for";
            # `arrived` is the ledger's own on_hand + used. They agree unless a
            # lot has been hand-adjusted, and seeing them diverge is exactly how
            # an adjustment is noticed.
            "received": totals.get("received", 0.0),
            "rejected": totals.get("rejected", 0.0),
            "deliveries": totals.get("deliveries", 0),
            # Deliveries recorded at the gate whose approved/rejected split and
            # per-hide measurements nobody has come back to enter yet.
            "pending_arrivals": pending.get("count", 0),
            "pending_arrival_qty": pending.get("declared_qty", 0.0),
            **sheet_rollup(sheets),
            "sheets_total": sum(v["count"] for v in sheets.values()),
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
                "status": r.status,
                "total_qty": (float(r.declared_qty)
                              if r.declared_qty is not None else None),
                "approved_qty": float(r.approved_qty or 0),
                "rejected_qty": float(r.rejected_qty or 0),
                "sheet_count": r.declared_sheet_count,
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
        # THE SERVICE, NOT `self.barcodes`. `self.barcodes` is a
        # BarcodeRepository and this method lives on BarcodeService, so calling
        # it through the repository handle was an AttributeError on every
        # retire — the same mistake, in the same direction, that the note on
        # `display_stock` above records (a service method reached through a
        # repository attribute). Lazy import per CLAUDE.md §15: barcode.service
        # reaches back into materials for the kit block, so hoisting this to
        # module scope would close the cycle.
        from app.modules.barcode.service import BarcodeService
        retired = await BarcodeService(self.db).retire_lot_code_nocommit(lot.id)
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

    # ══════════════════════════════════════════════════════════════════════
    # HIDE CRUD — the correction path for per-sheet data entry
    # ══════════════════════════════════════════════════════════════════════
    # WHY THIS EXISTS. A leather delivery is typed hide by hide, by a person
    # reading a number written on a skin, and there was no way to fix any of it:
    # a hide entered as 45 when the skin says 4.5 stayed wrong forever, and a
    # fifth hide typed by accident stayed in the count forever. The lot had
    # update/adjust/retire; its hides had nothing.
    #
    # THE STATE IS THE PERMISSION, and it is the same rule throughout: a hide
    # that is still IN_STOCK and on no cutting row has not been acted on by
    # anybody, so correcting it is data entry. The moment it is ALLOCATED,
    # ISSUED or CONSUMED it is part of a cutting decision somebody made, and
    # changing its measurement underneath them would rewrite what a garment
    # cost. Those are refused with a 409 that names the row holding it.

    #: A hide nobody has claimed yet. Editable and deletable; everything else is
    #: history and is not.
    _SHEET_EDITABLE = {SheetStatus.IN_STOCK.value, SheetStatus.RETURNED.value}

    async def _sheet_or_404(self, sheet_id: uuid.UUID):
        sheet = await self.repo.get_sheet(sheet_id)
        if sheet is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Hide not found.")
        return sheet

    def _assert_sheet_untouched(self, sheet, verb: str) -> None:
        if sheet.cutting_row_id is not None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{sheet.code} is on cutting row {sheet.cutting_row_id} — a hide "
                f"a cutter has been given cannot be {verb}. Take it off the row "
                f"first, or record what actually happened to it.")
        if sheet.status not in self._SHEET_EDITABLE:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{sheet.code} is {sheet.status}, so it cannot be {verb}: its "
                f"measurement is part of what a garment was cut from. Correct "
                f"the LOT's stock instead with PATCH /materials/lots/"
                f"{sheet.material_lot_id}/adjust, which records the movement "
                f"with a reason.")

    @staticmethod
    def _sheet_row(sheet) -> dict:
        return {"sheet_id": sheet.id, "code": sheet.code,
                "dcm": float(sheet.dcm or 0), "status": sheet.status,
                "cutting_row_id": sheet.cutting_row_id,
                "material_lot_id": sheet.material_lot_id,
                "note": sheet.note,
                "received_at": (sheet.received_at.isoformat()
                                if sheet.received_at else None)}

    async def list_sheets(self, lot_id: uuid.UUID, *,
                          status_filter: str | None = None) -> dict:
        """Every hide in one lot, with the roll-up the stock screen prints.

        Smallest first — the order `sheets_for_lot` already returns, because the
        allocator spends offcuts before it breaks into a big skin.
        """
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        statuses = ({s.strip().upper() for s in status_filter.split(",")}
                    if status_filter else None)
        sheets = await self.repo.sheets_for_lot(lot_id, statuses=statuses)
        counts = await self.repo.sheet_counts_by_status(lot_id)
        return {
            "lot_id": lot.id, "article": lot.article, "colour": lot.colour,
            "uom": lot.uom,
            "count": len(sheets),
            "sheets": [self._sheet_row(s) for s in sheets],
            "sheets_by_status": counts,
            "reconciliation": await self.sheet_reconciliation(lot),
        }

    async def get_sheet(self, sheet_id: uuid.UUID) -> dict:
        """One hide, plus the lot it belongs to — the click-through from a scan."""
        sheet = await self._sheet_or_404(sheet_id)
        lot = await self.repo.get_lot(sheet.material_lot_id)
        codes = await self.repo.barcodes_by_lot([sheet.material_lot_id])
        return dict(
            self._sheet_row(sheet),
            article=getattr(lot, "article", None),
            colour=getattr(lot, "colour", None),
            thickness=getattr(lot, "thickness", None),
            lot_barcode=codes.get(sheet.material_lot_id),
            editable=(sheet.cutting_row_id is None
                      and sheet.status in self._SHEET_EDITABLE),
        )

    async def add_sheets(self, lot_id: uuid.UUID, sheets: list, *,
                         actor_id=None) -> dict:
        """Add hides to a lot that already exists — the missed-one path.

        A delivery is sheeted at the gate under time pressure and a skin at the
        bottom of the bundle gets missed. Without this the only way to record it
        was to create a second lot for the same material, which the one-lot-per-
        spec rule refuses, so it was not recorded at all.

        STOCK IS NOT TOUCHED, deliberately. `lot.on_hand` is what the delivery
        note said arrived and the hides are the detail beneath it; adding the
        hide somebody forgot to type does not mean more leather walked in. The
        reconciliation block reports the gap either way, which is exactly the
        conversation this is meant to start.
        """
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")
        if not sheets:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Send at least one hide.")
        minted = await self.mint_sheets_nocommit(lot, [
            s if isinstance(s, dict) else {"dcm": s.dcm, "note": s.note}
            for s in sheets])
        await self._audit(actor_id, "MATERIAL_SHEETS_ADDED", lot.id, {
            "article": lot.article, "colour": lot.colour,
            "added": [{"code": m.code, "dcm": float(m.dcm)} for m in minted]})
        await self.db.commit()
        for m in minted:
            await self.db.refresh(m)
        return {
            "lot_id": lot.id,
            "added": [self._sheet_row(m) for m in minted],
            "reconciliation": await self.sheet_reconciliation(lot),
            "message": (f"{len(minted)} hide(s) added to {lot.article}. Stock is "
                        f"unchanged — the delivery total is what it always was; "
                        f"only the per-hide detail grew."),
        }

    async def update_sheet(self, sheet_id: uuid.UUID, patch: dict, *,
                           actor_id=None) -> dict:
        """Correct ONE hide's measurement or note. Audited.

        The measurement is the whole point of sheeting a delivery, so a wrong
        one is worth a row in the audit log: it changes what the reconciliation
        says and, once the hide is cut, what the garment is recorded as costing.
        """
        sheet = await self._sheet_or_404(sheet_id)
        self._assert_sheet_untouched(sheet, "corrected")

        fields = {k: v for k, v in patch.items() if k in {"dcm", "note"}
                  and v is not None}
        if not fields:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Nothing to update. Send dcm and/or note.")
        before = {"dcm": float(sheet.dcm or 0), "note": sheet.note}
        if "dcm" in fields:
            try:
                dcm = Decimal(str(fields["dcm"]))
            except Exception:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "dcm must be a number.")
            if dcm <= 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "A hide's dcm must be greater than 0 — the measurement "
                    "written on the skin is what the whole ledger is built on.")
            sheet.dcm = dcm
        if "note" in fields:
            sheet.note = fields["note"]

        await self._audit(actor_id, "MATERIAL_SHEET_UPDATED",
                          sheet.material_lot_id,
                          {"code": sheet.code, "before": before,
                           "after": {"dcm": float(sheet.dcm or 0),
                                     "note": sheet.note}})
        await self.db.commit()
        await self.db.refresh(sheet)
        lot = await self.repo.get_lot(sheet.material_lot_id)
        return dict(self._sheet_row(sheet),
                    reconciliation=await self.sheet_reconciliation(lot))

    async def delete_sheet(self, sheet_id: uuid.UUID, *, actor_id=None) -> dict:
        """Remove a hide entered by mistake. HARD delete, and only while untouched.

        THE ONE PLACE THIS CODEBASE DELETES A ROW, and the reason is that there
        is nothing here to keep. An employee who leaves worked shifts; a retired
        lot has cut events pointing at it; a deactivated spec line has issues
        pointing at it — all three are retired rather than deleted because
        history hangs off them. A hide that is still IN_STOCK and on no cutting
        row has no history at all: nothing has been allocated from it, issued
        from it or cut from it. It is a typed line that should not have been
        typed, and keeping it forever would leave a skin that does not exist in
        every hide count on the stock screen.

        Its BARCODE is retired rather than deleted, because a label may already
        have been printed and stuck on something — a scan of it must say "this
        was removed" (410 Gone), never "unknown code".

        `_assert_sheet_untouched` is what keeps this safe: the moment a cutter
        has been given the skin, this is a 409 and the honest correction is
        PATCH /materials/lots/{id}/adjust with a reason.
        """
        sheet = await self._sheet_or_404(sheet_id)
        self._assert_sheet_untouched(sheet, "deleted")

        lot_id, code, dcm = sheet.material_lot_id, sheet.code, float(sheet.dcm or 0)
        from app.modules.barcode.models import BarcodeRegistry
        from sqlalchemy import select as _select
        from app.modules.barcode.service import BarcodeService

        row = await self.db.scalar(
            _select(BarcodeRegistry)
            .where(BarcodeRegistry.material_sheet_id == sheet.id)
            .limit(1))
        retired = False
        if row is not None:
            # Retire, then unhook: the registry row keeps its own history but
            # must stop pointing at a sheet that is about to stop existing, or
            # the FK fails and the delete takes the label down with it.
            await BarcodeService(self.db).repo.retire_nocommit(
                row, reason="sheet_deleted")
            row.material_sheet_id = None
            retired = True

        await self.db.delete(sheet)
        await self._audit(actor_id, "MATERIAL_SHEET_DELETED", lot_id, {
            "code": code, "dcm": dcm, "barcode_retired": retired})
        await self.db.commit()

        lot = await self.repo.get_lot(lot_id)
        return {
            "sheet_id": sheet_id, "code": code, "deleted": True,
            "barcode_retired": retired,
            "lot_id": lot_id,
            "reconciliation": await self.sheet_reconciliation(lot) if lot else None,
            "message": (f"{code} ({dcm:g} dcm) is gone. The lot's stock figure is "
                        f"unchanged — a hide nobody had claimed was never part "
                        f"of it — and a scan of its label now reads 410 Gone."),
        }

    # ══════════════════════════════════════════════════════════════════════
    # ARRIVAL CRUD — correcting and voiding a gate entry
    # ══════════════════════════════════════════════════════════════════════
    # An arrival is typed at the gate, in a hurry, from a delivery note. It is
    # the single most mistake-prone entry in the module, it puts stock straight
    # onto the floor, and until now it could only be created and completed —
    # never corrected, never withdrawn. A van entered twice was two lots' worth
    # of stock that nobody could take back out.

    async def _pending_arrival_or_404(self, receipt_id: uuid.UUID, verb: str):
        from app.core.enums import IntakeStatus
        receipt = await self.repo.get_receipt(receipt_id)
        if receipt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Arrival not found.")
        if receipt.status != IntakeStatus.PENDING.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"This arrival is {receipt.status}. A completed arrival is the "
                f"QC record of a delivery and cannot be {verb} — correct the "
                f"stock with PATCH /materials/lots/"
                f"{receipt.material_lot_id}/adjust, which records the movement "
                f"with a reason.")
        return receipt

    async def get_arrival(self, receipt_id: uuid.UUID) -> dict:
        """One arrival, in the same shape the queue lists it."""
        receipt = await self.repo.get_receipt(receipt_id)
        if receipt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Arrival not found.")
        lot = await self.repo.get_lot(receipt.material_lot_id)
        return self._arrival_row(receipt, lot)

    async def update_arrival(self, receipt_id: uuid.UUID, patch: dict, *,
                             actor_id=None) -> dict:
        """Correct a PENDING arrival's declared quantity, bundle count or note.

        THE QUANTITY MOVES STOCK, because the declared quantity IS the stock this
        arrival put on the floor. Correcting 3400 to 340 has to take 3060 back
        out of the lot, or the correction is cosmetic and the shelf still claims
        material that never arrived. It is applied as a DELTA for the same reason
        `complete_arrival` uses one: the floor may have cut some of this delivery
        in between, and assigning the new figure would silently undo that.

        It refuses to drive the lot negative. A gate entry wrong by more than
        what is left on the shelf is not a typo, it is a different delivery, and
        the honest fix is to void this arrival and enter the real one.
        """
        receipt = await self._pending_arrival_or_404(receipt_id, "corrected")
        lot = await self.repo.get_lot_for_update(receipt.material_lot_id)
        if lot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "This arrival's lot no longer exists.")

        # IDENTITY IS CHECKED, NOT EDITED. The arrival's quantity is already in
        # THIS lot's stock; a different article/colour is a different lot, and
        # renaming here would either rename the whole lot (every other delivery
        # of it too) or silently ignore what was typed. Both are wrong.
        for field in ("article", "colour"):
            sent = patch.get(field)
            have = getattr(lot, field, None)
            if sent is not None and (sent or "").strip().upper() != (have or "").strip().upper():
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"This arrival is on lot {lot.article} · {lot.colour or '-'}, "
                    f"but {field} '{sent}' was sent. An arrival cannot be moved "
                    f"to another material by editing it: void it with DELETE "
                    f"/materials/arrivals/{receipt_id} and enter it again. If "
                    f"the LOT itself is misnamed, correct it with PATCH "
                    f"/materials/lots/{lot.id}.")

        before = {"declared_qty": float(receipt.declared_qty or 0),
                  "declared_sheet_count": receipt.declared_sheet_count,
                  "note": receipt.note}
        delta = Decimal(0)
        if patch.get("declared_qty") is not None:
            try:
                new_qty = Decimal(str(patch["declared_qty"]))
            except Exception:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                    "declared_qty must be a number.")
            if new_qty <= 0:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    "declared_qty must be > 0. An arrival of nothing is not an "
                    "arrival — void it instead.")
            delta = new_qty - Decimal(str(receipt.declared_qty or 0))
            new_on_hand = (lot.on_hand or Decimal(0)) + delta
            if new_on_hand < 0:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Correcting this arrival to {float(new_qty):g} {lot.uom} "
                    f"would take {lot.article} to {float(new_on_hand):g} — below "
                    f"zero, because {float(delta * -1):g} {lot.uom} of it has "
                    f"already been cut or issued. Void the arrival and enter the "
                    f"real delivery instead.")
            lot.on_hand = new_on_hand
            receipt.declared_qty = new_qty
        if patch.get("declared_sheet_count") is not None:
            receipt.declared_sheet_count = patch["declared_sheet_count"]
        if patch.get("note") is not None:
            receipt.note = patch["note"]

        await self._audit(actor_id, "MATERIAL_ARRIVAL_UPDATED",
                          receipt.material_lot_id,
                          {"receipt_id": str(receipt.id), "before": before,
                           "after": {"declared_qty": float(receipt.declared_qty or 0),
                                     "declared_sheet_count": receipt.declared_sheet_count,
                                     "note": receipt.note},
                           "stock_delta": float(delta)})

        # WITH THE SPLIT, THIS IS THE SECOND SITTING. Handed to complete_arrival
        # uncommitted, so the correction above and the completion land in ONE
        # transaction — never a corrected-but-not-completed arrival.
        if patch.get("approved_qty") is not None or patch.get("rejected_qty") is not None:
            from app.modules.materials import schemas
            declared = Decimal(str(receipt.declared_qty or 0))
            a, r = patch.get("approved_qty"), patch.get("rejected_qty")
            if a is None:
                a = declared - Decimal(str(r))
                if a < 0:
                    raise HTTPException(
                        status.HTTP_422_UNPROCESSABLE_ENTITY,
                        f"rejected_qty ({float(r):g}) is more than the "
                        f"{float(declared):g} {lot.uom} this arrival declared.")
            if r is None:
                r = max(declared - Decimal(str(a)), Decimal(0))
            done = await self.complete_arrival(receipt_id, schemas.ArrivalComplete(
                approved_qty=float(a), rejected_qty=float(r),
                sheets=patch.get("sheets"), thickness=patch.get("thickness")),
                actor_id=actor_id)
            await self.db.refresh(receipt)
            row = self._arrival_row(receipt, lot)
            row.update({k: done[k] for k in done if k not in row})
            row["on_hand_delta"] = done["on_hand_delta"] + float(delta)
            return row

        await self.db.commit()
        await self.db.refresh(receipt)
        await self.db.refresh(lot)
        return dict(self._arrival_row(receipt, lot),
                    stock_delta=float(delta),
                    **stock_numbers(lot.on_hand, lot.used,
                                    await self.repo.active_reserved(lot.id)))

    async def delete_arrival(self, receipt_id: uuid.UUID, *,
                             actor_id=None) -> dict:
        """Void a PENDING arrival — the van that was entered twice.

        HARD delete of the receipt, and the stock it put on the floor comes back
        out. Same reasoning as a hide: a PENDING arrival that nobody has QC'd is
        a typed line, not history. The moment it is COMPLETED it is the record of
        what a supplier delivered and what went back on the van, and it is
        refused with a 409.

        THE LOT SURVIVES even when this was its only delivery. It may already
        carry a printed barcode, a material spec line pinned to it and a cut
        event, and deleting it would orphan all three; a lot at zero is an empty
        shelf, which is a true statement. Retire it separately if it was minted
        in error.
        """
        receipt = await self._pending_arrival_or_404(receipt_id, "voided")
        lot = await self.repo.get_lot_for_update(receipt.material_lot_id)
        qty = Decimal(str(receipt.declared_qty or 0))

        if lot is not None:
            new_on_hand = (lot.on_hand or Decimal(0)) - qty
            if new_on_hand < 0:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"Voiding this arrival would take {lot.article} to "
                    f"{float(new_on_hand):g} {lot.uom}: some of its "
                    f"{float(qty):g} has already been cut or issued, so the "
                    f"material was real. Correct the quantity with PATCH "
                    f"/materials/arrivals/{receipt_id} instead.")
            lot.on_hand = new_on_hand

        await self.db.delete(receipt)
        await self._audit(actor_id, "MATERIAL_ARRIVAL_VOIDED",
                          receipt.material_lot_id,
                          {"receipt_id": str(receipt_id),
                           "declared_qty": float(qty),
                           "article": getattr(lot, "article", None)})
        await self.db.commit()
        if lot is not None:
            await self.db.refresh(lot)

        return {
            "receipt_id": receipt_id, "voided": True,
            "lot_id": getattr(lot, "id", None),
            "qty_removed": float(qty),
            "lot_retained": lot is not None,
            **(stock_numbers(lot.on_hand, lot.used,
                             await self.repo.active_reserved(lot.id))
               if lot is not None else {}),
            "message": (
                f"The arrival is void and {float(qty):g} "
                f"{getattr(lot, 'uom', '')} came back off the shelf. The lot "
                f"itself is kept — its barcode may be printed and a recipe may "
                f"already point at it; retire it separately if it should not "
                f"exist."),
        }

    async def adjust_receipt(self, receipt_id: uuid.UUID, patch: dict, *,
                             actor_id=None) -> dict:
        """Correct a RECEIVED delivery's approved/rejected split, with a reason.

        A CHANGE TO APPROVED MOVES STOCK, by (new approved − old approved) — a
        delta, never an assignment, so a cut made from this delivery since it was
        received is not silently undone. Rejected was never in stock (it went
        back on the van), so changing it corrects the supplier's quality history
        and nothing else.

        PENDING arrivals are refused: their split does not exist yet. That is
        PATCH /materials/arrivals/{id} or its /complete.
        """
        from app.core.enums import IntakeStatus

        receipt = await self.repo.get_receipt(receipt_id)
        if receipt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Receipt not found.")
        if receipt.status == IntakeStatus.PENDING.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This delivery is a PENDING arrival — it has no approved/"
                "rejected split yet. Enter it with POST /materials/arrivals/"
                f"{receipt_id}/complete, or correct the gate figure with "
                f"PATCH /materials/arrivals/{receipt_id}.")
        lot = await self.repo.get_lot_for_update(receipt.material_lot_id)
        if lot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "This receipt's lot no longer exists.")
        reason = (patch.get("reason") or "").strip()
        if len(reason) < 3:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Give a reason for correcting a delivery — it is the only record "
                "of why the received figures changed.")

        old_a = Decimal(str(receipt.approved_qty or 0))
        old_r = Decimal(str(receipt.rejected_qty or 0))
        new_a = (Decimal(str(patch["approved_qty"]))
                 if patch.get("approved_qty") is not None else old_a)
        new_r = (Decimal(str(patch["rejected_qty"]))
                 if patch.get("rejected_qty") is not None else old_r)
        if new_a < 0 or new_r < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Quantities cannot be negative.")

        # Same arithmetic as POST /materials/receive.
        total = patch.get("total_qty")
        if total is not None:
            total = Decimal(str(total))
            if new_a > total:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"approved_qty ({float(new_a):g}) is more than total_qty "
                    f"({float(total):g}) — more cannot be approved than arrived.")
            if patch.get("rejected_qty") is None:
                new_r = total - new_a
            elif new_a + new_r != total:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"approved_qty ({float(new_a):g}) + rejected_qty "
                    f"({float(new_r):g}) = {float(new_a + new_r):g}, but "
                    f"total_qty is {float(total):g}. Correct one of them, or "
                    f"leave rejected_qty out and it is worked out for you.")

        sheet_count = patch.get("sheet_count")
        if (new_a == old_a and new_r == old_r and total is None
                and sheet_count is None):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Nothing to change — send approved_qty, rejected_qty, "
                "total_qty or sheet_count.")

        delta = new_a - old_a
        new_on_hand = (lot.on_hand or Decimal(0)) + delta
        if new_on_hand < 0:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"Approving {float(new_a):g} instead of {float(old_a):g} would "
                f"take {lot.article} to {float(new_on_hand):g} {lot.uom} — "
                f"{float(-new_on_hand):g} of it has already been cut or issued. "
                f"Stock cannot go negative.")
        reserved = await self.repo.active_reserved(lot.id)
        if delta < 0 and new_on_hand < reserved:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"{float(reserved):g} {lot.uom} of {lot.article} is reserved for "
                f"a cut. Lowering approved by {float(-delta):g} would leave "
                f"{float(new_on_hand):g} — less than is committed. Release the "
                f"reservation first.")

        before = {"total_qty": (float(receipt.declared_qty)
                                if receipt.declared_qty is not None else None),
                  "approved_qty": float(old_a), "rejected_qty": float(old_r),
                  "sheet_count": receipt.declared_sheet_count}
        lot.on_hand = new_on_hand
        receipt.approved_qty = new_a
        receipt.rejected_qty = new_r
        # The receipt's total is approved + rejected once it has a split. Kept in
        # step so the purchase history does not show a total the split disagrees
        # with.
        receipt.declared_qty = new_a + new_r
        if sheet_count is not None:
            receipt.declared_sheet_count = sheet_count

        after = {"total_qty": float(new_a + new_r),
                 "approved_qty": float(new_a), "rejected_qty": float(new_r),
                 "sheet_count": receipt.declared_sheet_count}
        await self._audit(actor_id, "MATERIAL_RECEIPT_ADJUSTED", lot.id, {
            "receipt_id": str(receipt.id), "before": before, "after": after,
            "stock_delta": float(delta), "reason": reason})
        await self.db.commit()
        await self.db.refresh(lot)
        await self.db.refresh(receipt)

        reserved = await self.repo.active_reserved(lot.id)
        sheets_now = await self.repo.sheet_counts_by_status(lot.id)
        return {
            "receipt_id": receipt.id,
            "lot_id": lot.id,
            "status": receipt.status,
            **after,
            **stock_numbers(lot.on_hand, lot.used, reserved),
            **sheet_rollup(sheets_now),
            "stock_delta": float(delta),
            "before": before,
            "reason": reason,
            "message": (
                f"Stock corrected by {float(delta):+g} {lot.uom}."
                if delta else
                "Stock unchanged — only the rejected figure / counts moved."),
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
        # Sheet-wise figures for the whole page in ONE query, and the unfinished
        # arrivals in one more — a lot carrying a PENDING arrival is holding
        # PROVISIONAL stock, and a directory that cannot say so is presenting an
        # estimate as a measurement.
        sheets_map = await self.repo.sheet_totals_for_lots(lot_ids)
        pending_map = await self.repo.pending_intake_by_lot(lot_ids)
        items = []
        for lot in lots:
            reserved = reserved_map.get(lot.id, Decimal(0))
            numbers = stock_numbers(lot.on_hand, lot.used, reserved)
            pending = pending_map.get(lot.id)
            items.append({
                "lot_id": lot.id,
                "barcode": barcode_map.get(lot.id),
                "category": lot.category, "subtype": lot.subtype,
                "article": lot.article, "colour": lot.colour,
                "thickness": lot.thickness, "size": lot.size,
                "uom": lot.uom,
                # arrived / used / balance / reserved / available / on_hand
                **numbers,
                **sheet_rollup(sheets_map.get(lot.id, {})),
                # Pre-select this one in the UI, but SHOW it — never silently.
                "last_used_for_sku": lot.id == last_used_id,
                # None when the caller did not say how much it needs.
                "covers_required": (None if need is None
                                    else bool(numbers["available"] >= float(need))),
                "pending_arrivals": (pending or {}).get("count", 0),
                "pending_arrival_qty": (pending or {}).get("declared_qty", 0.0),
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
        """THE STOCK CHECK, summed over every lot that matches the filter.

        THREE NUMBERS, AND THEY RECONCILE: `arrived` came in, `used` was cut or
        issued, `balance` is what is left — and arrived − used == balance, always.
        `reserved` is what is committed to a requirement but not yet spent, and
        `available` is balance − reserved: what may still be promised.

        The same three are given SHEET-WISE for leather (`sheets_arrived`,
        `sheets_used`, `sheets_balance`, each with its dcm), because the floor
        counts hides as well as decimetres — a cutter is handed skins, and "how
        many are on the shelf" is not answerable from a sum of measurements.

        WHAT CHANGED. `on_hand` here used to be the ARRIVED total while `on_hand`
        on a lot's own page was the BALANCE, and `reserved` here silently included
        everything already consumed. So the two screens disagreed and neither said
        how much had been used. `on_hand` now means balance everywhere; read
        `arrived` / `used` / `balance`, which say what they are.
        """
        lots = await self.repo.find_lots(
            category=category, subtype=subtype, article=article,
            colour=colour, thickness=thickness, size=size)
        lot_ids = [lot.id for lot in lots]
        # ONE query for every lot's reservations instead of one per lot: this is
        # the DM's first screen and it is opened on every shortfall check.
        reserved_map = await self.repo.reserved_by_lot(lot_ids)
        on_hand = Decimal(0)
        used = Decimal(0)
        active_reserved = Decimal(0)
        for lot in lots:
            on_hand += lot.on_hand or Decimal(0)
            used += lot.used or Decimal(0)
            active_reserved += reserved_map.get(lot.id, Decimal(0))
        numbers = stock_numbers(on_hand, used, active_reserved)
        uom = lots[0].uom if lots else uom_for(category or "", subtype)

        # SHEET-WISE, rolled up across the same lots. Empty for anything that is
        # not leather, which is the honest answer rather than a row of zeroes:
        # lining is metres and a button is a button — neither has hides to count.
        sheets_map = await self.repo.sheet_totals_for_lots(lot_ids)
        merged: dict = {}
        for per_lot in sheets_map.values():
            for st, v in per_lot.items():
                cell = merged.setdefault(st, {"count": 0, "dcm": 0.0})
                cell["count"] += int(v.get("count", 0))
                cell["dcm"] = round(cell["dcm"] + float(v.get("dcm", 0.0)), 3)

        pending_map = await self.repo.pending_intake_by_lot(lot_ids)
        out = {
            "category": (category or "").upper() or None,
            "subtype": (subtype or None),
            "article": article, "colour": colour, "thickness": thickness,
            "size": size, "uom": uom,
            **numbers,
            **sheet_rollup(merged),
            "lot_count": len(lots),
            # DELIVERIES NOT YET FINISHED. The qty they brought in is already in
            # `arrived` and cuttable — it is PROVISIONAL until somebody enters the
            # approved/rejected split, and a stock figure that cannot say which
            # part of itself is provisional is the number people stop trusting.
            "pending_arrivals": sum(v["count"] for v in pending_map.values()),
            "pending_arrival_qty": round(
                sum(v["declared_qty"] for v in pending_map.values()), 3),
        }
        if required is not None:
            available = Decimal(str(numbers["available"]))
            short = max(Decimal(0), Decimal(str(required)) - available)
            out["required"] = float(required)
            out["short_by"] = float(short)
            if short > 0 and article:
                sup = await self.repo.suggest_supplier(article)
                out["suggested_supplier"] = (
                    {"id": str(sup.id), "name": sup.name} if sup else None)
        return out

    # ══════════════════════════════════════════════════════════════════════
    # ARRIVALS — a delivery entered in TWO SITTINGS
    # ══════════════════════════════════════════════════════════════════════
    # THE PROBLEM THIS EXISTS FOR, in the floor's own words: the van turns up and
    # whoever signs for it has ten seconds. Article, colour, total dcm — that is
    # all anybody knows at the gate. Splitting the total into approved and
    # rejected, counting the hides and measuring every one of them is twenty
    # minutes of quiet work that happens later the same day, or the next.
    #
    # `create_lot` and `receive` both demand the whole story at once: the strict
    # per-category field set (leather needs a THICKNESS nobody has read off the
    # packing note yet) and an approved/rejected split that has not been done.
    # A form like that gets one of two answers, and both are worse than nothing:
    # the delivery goes unrecorded until somebody has twenty minutes, or numbers
    # are invented to get past the required fields.
    #
    # So: ARRIVE now, FINISH later.
    #   arrive()    article + colour + total + (optional) sheet count. Mints or
    #               tops up the lot, prints the barcode, puts the material into
    #               stock PROVISIONALLY so the floor can cut from it, and leaves
    #               a PENDING receipt behind as the thing to come back to.
    #   complete()  the approved/rejected split and the per-hide measurements.
    #               on_hand is corrected by (approved − declared), the rejection
    #               is logged against the supplier, the hides are minted and
    #               labelled, and the receipt closes.
    #
    # THE STOCK IS PROVISIONAL, NOT FICTIONAL, and everything that reads it says
    # so — `pending_arrivals` and `pending_arrival_qty` ride along on the lot
    # page, the lot directory and the stock check. That is the whole difference
    # between a number the floor can use and one it has to guess about.

    async def arrive(self, body, actor_id=None) -> dict:
        """Record a delivery at the gate. The smallest honest entry there is.

        Finds the lot this material belongs to by its spec and tops it up; mints
        the lot and its barcode when this is the first delivery of it. The
        per-category STRICT field set is deliberately NOT enforced here — it is
        enforced by `create_lot`, which is the considered path. A gate entry
        carries what the person at the gate actually knows, and `complete` (or a
        later PATCH of the lot) fills the rest in.
        """
        from app.core.enums import IntakeStatus

        cat = (getattr(body, "category", None) or
               MaterialCategory.LEATHER.value).upper()
        if cat not in _LOT_BARCODE_TYPE:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                f"Unknown category '{cat}'.")
        subtype = (body.subtype or None)
        subtype = subtype.upper() if subtype else None

        article = (body.article or "").strip()
        colour = (body.colour or "").strip()
        if not article:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "article is required — it is what the lot IS.")
        if not colour:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "colour is required.")
        try:
            qty = Decimal(str(body.total_qty))
        except Exception:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "total_qty must be a number.")
        if qty <= 0:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "total_qty (the quantity that arrived) must be > 0. An arrival "
                "of nothing is not an arrival.")

        spec = resolve_spec(cat, subtype) or {}
        uom = spec.get("qty_uom") or uom_for(cat, subtype)
        thickness = (body.thickness or None)
        size = (body.size or None)

        lot = await self.repo.find_duplicate_lot(
            category=cat, subtype=subtype, article=article, colour=colour,
            thickness=thickness, size=size)
        created = lot is None
        barcode = None
        if created:
            # ONE LOT PER SPEC still holds (see create_lot). A gate entry for
            # material nobody has received before mints the lot, so the first
            # delivery of a new article does not have to wait for somebody with
            # the full field set in front of them.
            attrs = {k: v for k, v in {
                "thickness": thickness, "size": size,
                spec.get("qty_field") or "qty": float(qty),
            }.items() if v is not None}
            lot = self.repo.add_lot_nocommit(
                category=cat, subtype=subtype, article=article, colour=colour,
                thickness=thickness, size=size, uom=uom, on_hand=qty,
                supplier_id=body.supplier_id, attributes=attrs, is_active=True)
            await self.db.flush()          # need lot.id for the barcode
            caption = " · ".join(str(v) for v in
                                 [article, colour, thickness, f"{qty} {uom}"] if v)
            bc = await self.barcodes.mint_lot_code_nocommit(
                lot.id, _LOT_BARCODE_TYPE[cat], caption)
            barcode = bc.code
        else:
            lot.on_hand = (lot.on_hand or Decimal(0)) + qty
            if body.supplier_id and lot.supplier_id is None:
                lot.supplier_id = body.supplier_id
            codes = await self.repo.barcodes_by_lot([lot.id])
            barcode = codes.get(lot.id)

        # THE RECEIPT IS THE THING TO COME BACK TO. Nothing is approved or
        # rejected until QC says so, so both are 0; what arrived is declared_qty,
        # and `received` counts a PENDING receipt by it (repo.received_totals).
        # `complete` fills the split in.
        receipt = self.repo.add_receipt_nocommit(
            material_lot_id=lot.id, supplier_order_id=body.supplier_order_id,
            approved_qty=Decimal(0), rejected_qty=Decimal(0), received_by=actor_id,
            status=IntakeStatus.PENDING.value, declared_qty=qty,
            declared_sheet_count=body.sheet_count, note=body.note)
        await self.db.flush()

        await self._audit(actor_id, "MATERIAL_ARRIVED", lot.id, {
            "article": article, "colour": colour, "declared_qty": float(qty),
            "declared_sheet_count": body.sheet_count,
            "lot_created": created, "receipt_id": str(receipt.id)})
        await self.db.commit()
        await self.db.refresh(lot)

        reserved = await self.repo.active_reserved(lot.id)
        is_leather = (lot.category or "").upper() == MaterialCategory.LEATHER.value
        return {
            "receipt_id": receipt.id,
            "lot_id": lot.id,
            "lot_barcode": barcode,
            "lot_created": created,
            "category": lot.category, "subtype": lot.subtype,
            "article": lot.article, "colour": lot.colour,
            "thickness": lot.thickness, "uom": lot.uom,
            "declared_qty": float(qty),
            "declared_sheet_count": body.sheet_count,
            "status": IntakeStatus.PENDING.value,
            **stock_numbers(lot.on_hand, lot.used, reserved),
            "outstanding": self._outstanding_fields(lot, body.sheet_count),
            "message": (
                f"{float(qty):g} {lot.uom} of {article}"
                f"{' · ' + colour if colour else ''} is in stock and cuttable. "
                f"The approved/rejected split"
                + (" and the per-hide measurements are" if is_leather else " is")
                + f" still to be entered — POST /materials/arrivals/"
                  f"{receipt.id}/complete when there is time."),
        }

    @staticmethod
    def _pending_block(pending: dict | None) -> dict:
        return {"pending_arrivals": (pending or {}).get("count", 0),
                "pending_arrival_qty": (pending or {}).get("declared_qty", 0.0)}

    @staticmethod
    def _outstanding_fields(lot, sheet_count) -> list:
        """What a PENDING arrival is still waiting for, named one by one.

        A worklist that only says "unfinished" makes somebody open every row to
        find out what it wants. These are the field names the completion form
        renders, in the order it renders them.
        """
        out = ["approved_qty", "rejected_qty"]
        category = (getattr(lot, "category", "") or "").upper()
        if category == MaterialCategory.LEATHER.value:
            out.append("sheets")
            if sheet_count is None:
                out.append("sheet_count")
        if not getattr(lot, "thickness", None) and category in ("LEATHER", "LINING"):
            out.append("thickness")
        return out

    async def list_arrivals(self, *, status_filter: str | None = "PENDING",
                            lot_id=None, limit: int = 200,
                            offset: int = 0) -> dict:
        """The come-back-to-it queue, oldest first. THIS IS THE WHOLE POINT.

        A two-sitting intake is only safe if the second sitting is findable.
        Without this list a PENDING receipt is a row nobody knows exists, and
        provisional stock silently becomes permanent stock that was never checked.
        """
        rows = await self.repo.find_receipts(
            status=status_filter, lot_id=lot_id, limit=limit, offset=offset)
        total = await self.repo.count_receipts(status=status_filter, lot_id=lot_id)
        lots = {}
        for r in rows:
            if r.material_lot_id not in lots:
                lots[r.material_lot_id] = await self.repo.get_lot(r.material_lot_id)
        return {
            "count": len(rows),
            "total": total,
            "status": (status_filter or None),
            "arrivals": [self._arrival_row(r, lots.get(r.material_lot_id))
                         for r in rows],
        }

    def _arrival_row(self, receipt, lot) -> dict:
        from app.core.enums import IntakeStatus
        return {
            "receipt_id": receipt.id,
            "lot_id": receipt.material_lot_id,
            "status": receipt.status,
            "article": getattr(lot, "article", None),
            "colour": getattr(lot, "colour", None),
            "thickness": getattr(lot, "thickness", None),
            "category": getattr(lot, "category", None),
            "uom": getattr(lot, "uom", None),
            "declared_qty": float(receipt.declared_qty or 0),
            "declared_sheet_count": receipt.declared_sheet_count,
            "approved_qty": float(receipt.approved_qty or 0),
            "rejected_qty": float(receipt.rejected_qty or 0),
            "supplier_order_id": receipt.supplier_order_id,
            "note": receipt.note,
            "arrived_at": receipt.created_at.isoformat() if receipt.created_at else None,
            "completed_at": (receipt.completed_at.isoformat()
                             if receipt.completed_at else None),
            "outstanding": (
                self._outstanding_fields(lot, receipt.declared_sheet_count)
                if (lot is not None
                    and receipt.status == IntakeStatus.PENDING.value) else []),
        }

    async def complete_arrival(self, receipt_id: uuid.UUID, body,
                               actor_id=None) -> dict:
        """The second sitting: the QC split, and the hides one by one.

        WHAT THIS CORRECTS, AND WHY IT IS A DELTA. The arrival put `declared_qty`
        into on_hand so the floor could work. Between then and now the floor may
        well have cut some of it. So the correction is `approved − declared`
        applied to on_hand — NOT `on_hand = approved`, which would silently undo
        every cut logged in between and is the one arithmetic mistake here that
        would lose real production data.

        Rejected quantity was never in stock (it went back on the van), so it is
        logged for the supplier's quality history and nothing else.
        """
        from datetime import datetime, timezone
        from app.core.enums import IntakeStatus

        receipt = await self.repo.get_receipt(receipt_id)
        if receipt is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Arrival not found.")
        if receipt.status == IntakeStatus.COMPLETED.value:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "This arrival was already completed"
                + (f" at {receipt.completed_at.isoformat()}"
                   if receipt.completed_at else "")
                + ". Correcting a finished delivery is an adjustment with a "
                  "reason on it — PATCH /materials/lots/<lot_id>/adjust — so "
                  "the movement keeps a name against it.")
        lot = await self.repo.get_lot(receipt.material_lot_id)
        if lot is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Material lot not found.")

        declared = Decimal(str(receipt.declared_qty or receipt.approved_qty or 0))
        approved = Decimal(str(body.approved_qty))
        rejected = Decimal(str(body.rejected_qty or 0))
        if approved < 0 or rejected < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Quantities cannot be negative.")

        # A DELTA, not an assignment — see the docstring.
        delta = approved - declared
        lot.on_hand = (lot.on_hand or Decimal(0)) + delta
        if body.thickness and not lot.thickness:
            # The one identity field a gate entry routinely cannot supply. It is
            # filled in here rather than needing a separate PATCH nobody makes.
            lot.thickness = body.thickness
            attrs = dict(lot.attributes or {})
            attrs["thickness"] = body.thickness
            lot.attributes = attrs

        receipt.approved_qty = approved
        receipt.rejected_qty = rejected
        receipt.status = IntakeStatus.COMPLETED.value
        receipt.completed_at = datetime.now(timezone.utc)
        receipt.completed_by = actor_id
        if body.note:
            receipt.note = body.note

        # THE HIDES. Checked against the approved quantity, not the declared one:
        # what was rejected went back on the van and was never sheeted.
        minted = await self._mint_sheets_for(
            lot, getattr(body, "sheets", None), declared_qty=approved)

        warnings = list(self.decrement_warnings)
        expected = receipt.declared_sheet_count
        if expected is not None and minted and len(minted) != expected:
            # REPORTED, NEVER ENFORCED, exactly like the dcm reconciliation: a
            # bundle count taken at the gate is a glance, and refusing the
            # measurements over it is how a floor learns to stop counting at all.
            warnings.append({
                "kind": "sheet_count_mismatch",
                "lot_id": str(lot.id),
                "declared_sheet_count": expected,
                "sheets_entered": len(minted),
                "note": (f"{expected} sheet(s) were counted at the gate but "
                         f"{len(minted)} were measured. The hides that exist are "
                         f"the ones entered here; check whether one is missing."),
            })
        if delta != 0:
            warnings.append({
                "kind": "arrival_corrected",
                "lot_id": str(lot.id),
                "declared_qty": float(declared),
                "approved_qty": float(approved),
                "difference": float(delta),
                "note": (f"{float(declared):g} {lot.uom} was entered at the gate; "
                         f"{float(approved):g} was approved. Stock has been "
                         f"corrected by {float(delta):+g} {lot.uom}."),
            })

        await self._audit(actor_id, "MATERIAL_ARRIVAL_COMPLETED", lot.id, {
            "receipt_id": str(receipt.id),
            "declared_qty": float(declared), "approved_qty": float(approved),
            "rejected_qty": float(rejected), "on_hand_delta": float(delta),
            "sheets_entered": len(minted)})
        await self.db.commit()
        await self.db.refresh(lot)
        await self.db.refresh(receipt)

        reserved = await self.repo.active_reserved(lot.id)
        sheets_now = await self.repo.sheet_counts_by_status(lot.id)
        return {
            "receipt_id": receipt.id,
            "lot_id": lot.id,
            "status": receipt.status,
            "declared_qty": float(declared),
            "approved_qty": float(approved),
            "rejected_logged": float(rejected),
            "on_hand_delta": float(delta),
            **stock_numbers(lot.on_hand, lot.used, reserved),
            **sheet_rollup(sheets_now),
            "sheets": [{"sheet_id": s.id, "code": s.code, "dcm": float(s.dcm),
                        "status": s.status, "cutting_row_id": None}
                       for s in minted],
            "sheet_reconciliation": (
                await self.sheet_reconciliation(lot) if minted else None),
            "warnings": warnings,
        }

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
 
        from app.core.enums import IntakeStatus

        approved_sent = body.approved_qty is not None
        rejected_sent = "rejected_qty" in body.model_fields_set
        approved = Decimal(str(body.approved_qty or 0))
        rejected = Decimal(str(body.rejected_qty or 0))
        if approved < 0 or rejected < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Quantities cannot be negative.")

        # THE TOTAL THAT CAME OFF THE VAN. Optional; when sent, the split has to
        # account for it. Unlike the sheet count this IS enforced — it is
        # arithmetic on three numbers typed on one form, not a glance at a bundle.
        total = getattr(body, "total_qty", None)
        if total is not None:
            total = Decimal(str(total))

        # NO SPLIT YET → A PENDING ARRIVAL. Same two-sitting rule as
        # POST /materials/arrivals: the total goes into stock provisionally (the
        # material is in the building) and approved/rejected are entered later
        # at PATCH /materials/arrivals/{receipt_id}, which corrects stock by the
        # difference.
        pending = not approved_sent and not rejected_sent
        if not approved_sent and total is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Send approved_qty, or send total_qty and enter the approved/"
                "rejected split later at PATCH /materials/arrivals/{receipt_id}.")
        if pending:
            # Nothing is approved or rejected until QC says so.
            approved, rejected = Decimal(0), Decimal(0)
        elif not approved_sent:
            # Only the rejection is known: the rest of the delivery is approved.
            if rejected > total:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"rejected_qty ({float(rejected):g}) is more than "
                    f"total_qty ({float(total):g}).")
            approved = total - rejected
        elif total is not None:
            if approved > total:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"approved_qty ({float(approved):g}) is more than "
                    f"total_qty ({float(total):g}) — more cannot be approved "
                    f"than arrived.")
            if "rejected_qty" not in body.model_fields_set:
                rejected = total - approved
            elif approved + rejected != total:
                raise HTTPException(
                    status.HTTP_422_UNPROCESSABLE_ENTITY,
                    f"approved_qty ({float(approved):g}) + rejected_qty "
                    f"({float(rejected):g}) = {float(approved + rejected):g}, "
                    f"but total_qty is {float(total):g}. Correct one of them, "
                    f"or leave rejected_qty out and it is worked out for you.")
        else:
            total = approved + rejected
        sheet_count = getattr(body, "sheet_count", None)

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
 
        # add approved qty to whichever lot we settled on — or, while the split
        # is still owed, the whole total PROVISIONALLY. The receipt says nothing
        # is approved yet (approved_qty 0); the completion corrects stock by
        # (approved − declared), which is why the total has to be in it now.
        stock_in = total if pending else approved
        target_lot.on_hand = (target_lot.on_hand or 0) + stock_in
        receipt_id = uuid.uuid4()
        self.repo.add_receipt_nocommit(
            id=receipt_id,
            material_lot_id=target_lot.id, supplier_order_id=body.supplier_order_id,
            approved_qty=approved, rejected_qty=rejected, received_by=actor_id,
            declared_qty=total, declared_sheet_count=sheet_count,
            status=(IntakeStatus.PENDING.value if pending
                    else IntakeStatus.COMPLETED.value))

        if body.reserve_for_required:
            self.repo.add_reservation_nocommit(
                target_lot.id, Decimal(str(body.reserve_for_required)),
                reason="receiving reservation")

        # THE HIDES IN THIS DELIVERY. Against target_lot, not `lot`: on an
        # approved substitution the leather physically went into the substitute
        # lot, and sheeting it to the ordered lot would file real hides under an
        # article nobody received.
        minted = await self._mint_sheets_for(
            target_lot, getattr(body, "sheets", None), declared_qty=stock_in)

        warnings = list(self.decrement_warnings)
        if sheet_count is not None and minted and len(minted) != sheet_count:
            # REPORTED, NEVER ENFORCED — the same rule as complete_arrival.
            warnings.append({
                "kind": "sheet_count_mismatch",
                "lot_id": str(target_lot.id),
                "declared_sheet_count": sheet_count,
                "sheets_entered": len(minted),
                "note": (f"{sheet_count} sheet(s) were counted but "
                         f"{len(minted)} were measured. The hides that exist are "
                         f"the ones entered here; check whether one is missing."),
            })

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
            {"receipt_id": str(receipt_id), "pending": pending,
             "total": float(total), "approved": float(approved),
             "rejected": float(rejected), "sheet_count": sheet_count,
             "sheets_entered": len(minted),
             "mismatch_fields": mismatch or None,
             "original_lot_id": str(lot.id) if substituted else None})
        await self.db.commit()
        await self.db.refresh(target_lot)
 
        reserved = await self.repo.active_reserved(target_lot.id)
        sheets_now = await self.repo.sheet_counts_by_status(target_lot.id)
        return {
            "lot_id": target_lot.id,
            # arrived / used / balance / reserved / available / on_hand
            **stock_numbers(target_lot.on_hand, target_lot.used, reserved),
            **sheet_rollup(sheets_now),
            **self._pending_block(
                (await self.repo.pending_intake_by_lot([target_lot.id]))
                .get(target_lot.id)),
            "receipt_id": receipt_id,
            "status": (IntakeStatus.PENDING.value if pending
                       else IntakeStatus.COMPLETED.value),
            "outstanding": (self._outstanding_fields(target_lot, sheet_count)
                            if pending else []),
            "message": (
                f"{float(total):g} {target_lot.uom} is in stock provisionally. "
                f"Enter approved_qty / rejected_qty at PATCH /materials/"
                f"arrivals/{receipt_id} when QC is done." if pending else None),
            "total_qty": float(total),
            "approved_qty": float(approved),
            "rejected_logged": float(rejected),
            "sheet_count": sheet_count,
            "sheets_entered": len(minted),
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
            "warnings": warnings,
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
        was cut here, a kit was issued. NO commit — StoreService.store_scan owns
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

