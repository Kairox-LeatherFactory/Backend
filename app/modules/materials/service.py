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
    async def receive(self, body, actor_id: uuid.UUID | None) -> dict:
        lot = await self.repo.get_lot(body.lot_id)
        if not lot:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lot not found.")

        approved = Decimal(str(body.approved_qty or 0))
        rejected = Decimal(str(body.rejected_qty or 0))
        if approved < 0 or rejected < 0:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                                "Quantities cannot be negative.")

        lot.on_hand = (lot.on_hand or 0) + approved
        self.repo.add_receipt_nocommit(
            material_lot_id=lot.id, supplier_order_id=body.supplier_order_id,
            approved_qty=approved, rejected_qty=rejected, received_by=actor_id)

        if body.reserve_for_required:
            self.repo.add_reservation_nocommit(
                lot.id, Decimal(str(body.reserve_for_required)),
                reason="receiving reservation")

        order_status = None
        if body.supplier_order_id:
            order = await self.repo.get_order(body.supplier_order_id)
            if order and order.status != SupplierOrderStatus.ARRIVED.value:
                from datetime import datetime, timezone
                order.status = SupplierOrderStatus.ARRIVED.value
                order.arrived_at = datetime.now(timezone.utc)
            order_status = order.status if order else None

        await self._audit(actor_id, "MATERIAL_RECEIVED", lot.id,
                          {"approved": float(approved), "rejected": float(rejected)})
        await self.db.commit()
        await self.db.refresh(lot)

        reserved = await self.repo.active_reserved(lot.id)
        return {
            "lot_id": lot.id, "on_hand": float(lot.on_hand),
            "reserved": float(reserved),
            "available": float(lot.on_hand - reserved),
            "rejected_logged": float(rejected),
            "supplier_order_status": order_status,
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
    async def create_order(self, body, actor_id: uuid.UUID | None) -> dict:
        supplier = None
        if body.supplier_id:
            supplier = await self.repo.get_supplier(body.supplier_id)
        else:
            supplier = await self.repo.suggest_supplier(body.article)
        uom = uom_for(body.category, getattr(body, "subtype", None))
        order = self.repo.add_order_nocommit(
            category=body.category.upper(), article=body.article,
            colour=body.colour, qty=Decimal(str(body.qty)), uom=uom,
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