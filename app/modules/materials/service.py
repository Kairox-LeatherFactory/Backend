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
from app.core.enums import UserRole
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


class MaterialService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = MaterialRepository(db)
        self.barcodes = BarcodeRepository(db)

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

        attrs = {k: v for k, v in dict(body.attributes or {}).items()
                 if v is not None and str(v).strip() != ""}

        # STRICT: every required attribute key must be present and non-empty.
        missing = spec["required"] - set(attrs)
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
        lot = self.repo.add_lot_nocommit(
            category=cat, subtype=subtype, article=body.article,
            colour=body.colour, thickness=attrs.get("thickness"),
            size=attrs.get("size"), uom=uom, on_hand=qty,
            supplier_id=body.supplier_id, attributes=attrs, is_active=True,
        )
        await self.db.flush()   # need lot.id for the barcode

        # per-category caption: exactly the fields the spec lists for the label.
        caption = self._caption(cat, subtype, body, attrs, qty, uom)
        bc = await self.barcodes.mint_lot_code_nocommit(
            lot.id, _LOT_BARCODE_TYPE[cat], caption)
        await self.db.commit()
        await self.db.refresh(lot)

        return {
            "lot_id": lot.id, "lot_barcode": bc.code,
            "category": lot.category, "subtype": lot.subtype,
            "article": lot.article, "colour": lot.colour,
            "on_hand": float(lot.on_hand), "reserved": 0.0,
            "available": float(lot.on_hand), "uom": lot.uom,
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

    async def stock(self, *, category=None, subtype=None, article=None,
                    colour=None, thickness=None, size=None,
                    required: float | None = None) -> dict:
        lots = await self.repo.find_lots(
            category=category, subtype=subtype, article=article,
            colour=colour, thickness=thickness, size=size)
        on_hand = Decimal(0)
        reserved = Decimal(0)
        for lot in lots:
            on_hand += lot.on_hand
            reserved += await self.repo.active_reserved(lot.id)
        available = on_hand - reserved
        uom = lots[0].uom if lots else uom_for(category or "", subtype)

        out = {
            "category": (category or "").upper() or None,
            "subtype": (subtype or None),
            "article": article, "colour": colour, "thickness": thickness,
            "size": size, "uom": uom,
            "on_hand": float(on_hand), "reserved": float(reserved),
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
            "lot_id": target_lot.id, "on_hand": float(target_lot.on_hand),
            "reserved": float(reserved),
            "available": float(target_lot.on_hand - reserved),
            "rejected_logged": float(rejected),
            "supplier_order_status": order_status,
            "substituted": substituted,
            "mismatch_fields": mismatch or None,
        }

    # ── consumption hook (called by the cutting log) ─────────────────────────
    async def decrement_for_cut_nocommit(self, lot_id: uuid.UUID,
                                         qty: float) -> float:
        """Drop a lot's on_hand by qty (dcm/mtrs) at cutting. NO commit — the
        production log owns the transaction, so the event and the stock move are
        atomic. Returns available AFTER the decrement for the response.

        Idempotency is the CALLER's responsibility: production writes one event
        per piece per operation, so a piece cut once decrements once. A re-posted
        identical scan is caught upstream by the per-piece 'already logged' check.
        """
        lot = await self.repo.get_lot(lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Consumption lot not found.")
        d = Decimal(str(qty))
        if d <= 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Consumption must be > 0 at cutting.")
        lot.on_hand = (lot.on_hand or 0) - d
        reserved = await self.repo.active_reserved(lot_id)
        return float(lot.on_hand - reserved)

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
        delivered lot. Empty list = exact match. Compares article, colour,
        thickness, and dcm (dcm read from lot.attributes)."""
        diffs = []
        def norm(x):
            return (str(x).strip().upper() if x is not None else None)
        if norm(order.article) != norm(lot.article):
            diffs.append("article")
        if norm(order.colour) != norm(lot.colour):
            diffs.append("colour")
        if norm(order.thickness) != norm(lot.thickness):
            diffs.append("thickness")
        # dcm: order.dcm (Decimal) vs lot.attributes["dcm"]
        lot_dcm = (lot.attributes or {}).get("dcm") if lot.attributes else None
        o_dcm = float(order.dcm) if order.dcm is not None else None
        l_dcm = float(lot_dcm) if lot_dcm is not None else None
        if o_dcm != l_dcm:
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

    async def _audit(self, actor_id, action, entity_id, after: dict) -> None:
        from datetime import datetime, timezone
        from app.core.models import AuditLog
        self.db.add(AuditLog(
            actor_user_id=actor_id, action=action, entity_type="material_lot",
            entity_id=entity_id, after=after, at=datetime.now(timezone.utc)))