"""
================================================================================
modules/procurement/po_service.py — Stage-5 supplier-PO orchestration
================================================================================

The business brain of Stage 5. Consumes the Stage-4 shortfall lines and drives a
supplier PO through its whole life:

  - GENERATE (§1/§2): match each shortfall to a supplier (deterministic ledger →
    alias → category → fuzzy → unresolved), GROUP by supplier into ONE PO per vendor
    (unresolved lines → a held `needs_supplier` draft), compute GST (config-driven
    CGST+SGST intra / IGST inter), each `po_item` keeping its bom_item/inventory_item
    back-link.
  - EDIT (§4): the SAME bulk-PATCH + optimistic `revision` contract as the BOM —
    server recompute, 409 stale_revision / 409 po_locked once sent.
  - CROSS-CHECK (§3): submit → approve/reject, material-type-routed (leather → Cutting
    Manager; accessory/service → MD/DM/HR; MD superuser), with the Stage-3 in-app→email
    notification.
  - SEND (§5): allocate the FY PO number, render + store the PDF, email via the Notifier
    (pixel + wrapped links + the PDF attached), start the escalation clock; the no-email /
    bounce branch short-circuits to the WhatsApp rung.
  - TRACK (§6) + ESCALATE (§7): self-hosted open pixel/click, the email → WhatsApp →
    auto-call ladder on the DB-idempotent sweeper, stopped the instant the supplier
    acknowledges by ANY channel.

LAYERING. Reads only procurement-owned tables; order/style identity resolves via
clients.service, recipients via users.service (permitted service→service calls). Blocking
render/send run in a threadpool.
================================================================================
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.enums import UserRole
from app.modules.supplier_po import po_costing, presenters as present, po_tracking
from app.core.enums import (
    DocumentKind,
    NotificationChannel,
    NotificationStatus,
    NotificationType,
)
from app.modules.inventory.enums import InventoryLineStatus
from app.modules.supplier_po.enums import (
    POResponseChannel,
    POResponseStatus,
    POStatus,
    POTrackingEventType,
    SupplierEmailStatus,
    SupplierType,
)
from app.modules.supplier_po.escalation import get_escalation_transport
from app.modules.inventory.inventory_match import Alias
from app.modules.inventory.inventory_normalize import normalize_key
from app.core.models import AuditLog, Document, Notification
from app.modules.supplier_po.models import (
    PoItem,
    PoResponse,
    PoTrackingEvent,
    PurchaseOrder,
)
from app.core.notifier import get_notifier
from app.modules.supplier_po.po_export import DEFAULT_TEMPLATE_CFG, render_po_pdf
from app.modules.supplier_po.repository import SupplierPoRepository
from app.core.storage import get_storage

# Cross-check approver routing (§3a). MD is superuser → may approve any PO.
_LEATHER_APPROVERS = {UserRole.CUTTING_MANAGER, UserRole.MANAGING_DIRECTOR}
_ACCESSORY_APPROVERS = {UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR}
# Statuses past which every edit path is frozen (§4 lock semantics).
_SENT_STATES = {POStatus.SENT.value, POStatus.RESPONDED.value,
                POStatus.CONFIRMED.value, POStatus.ESCALATED.value}
_ZERO = Decimal("0")


def _financial_year(d: date) -> str:
    """FY runs Apr–Mar (matching the provision sheet's APRIL-2025 months). 2025-04 → 25-26."""
    yy = d.year % 100
    return f"{yy:02d}-{(yy + 1) % 100:02d}" if d.month >= 4 else f"{(yy - 1) % 100:02d}-{yy:02d}"


def _po_template_cfg(supplier_type: str | None) -> dict:
    """The §2b registry → template cfg. Read from the seeded `po_templates.yaml` via the
    registry module; a missing type falls back to the accessory default."""
    from app.modules.supplier_po.po_templates import template_cfg_for
    return template_cfg_for(supplier_type) or DEFAULT_TEMPLATE_CFG


class PoService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = SupplierPoRepository(db)

    async def _bom_dto(self, bom_id):
        from app.modules.bom.service import BomService
        return await BomService(self.db).get_bom_dto(bom_id)

    async def _latest_check(self, bom_id):
        from app.modules.inventory.service import InventoryService
        return await InventoryService(self.db).get_latest_check_dto(bom_id)

    async def _alias_pairs(self):
        from app.modules.inventory.service import InventoryService
        return await InventoryService(self.db).get_alias_pairs()

    # ══════════════════════════════════════════════════════════════════════
    # Generate (§1/§2) — shortfall lines → grouped supplier POs
    # ══════════════════════════════════════════════════════════════════════
    async def generate_for_bom(self, user, bom_id: uuid.UUID) -> dict:
        from app.modules.supplier_po.supplier_service import SupplierService

        bom = await self._bom_dto(bom_id)
        if bom is None:
            raise HTTPException(404, "BOM not found.")
        check = await self._latest_check(bom_id)
        if check is None:
            raise HTTPException(409, detail={
                "error": "no_inventory_check",
                "message": "Run the inventory check before generating supplier POs."})
        existing = [p for p in await self.repo.pos_for_bom(bom_id)
                    if p.status != POStatus.CANCELLED.value]
        if existing:
            return {"bom_id": str(bom_id), "already_generated": True,
                    "purchase_orders": [present.po_view(await self.repo.get_po(p.id))
                                        for p in existing]}

        shortfalls = [ln for ln in check.lines
                      if ln.status != InventoryLineStatus.SUFFICIENT.value
                      and (ln.shortfall_qty or _ZERO) > _ZERO]
        if not shortfalls:
            return {"bom_id": str(bom_id), "purchase_orders": [],
                    "message": "No shortfall lines — nothing to order."}

        items_by_id = {i.id: i for i in bom.items}
        aliases = [Alias(bom_term=normalize_key(a.bom_term),
                         inventory_key=normalize_key(a.inventory_key))
                   for a in await self._alias_pairs()]
        identity = await self._identity(bom)
        today = datetime.now(timezone.utc).date()
        supplier_svc = SupplierService(self.db)

        groups: dict[uuid.UUID, list[dict]] = {}
        group_meta: dict[uuid.UUID, dict] = {}
        unresolved: list[dict] = []

        for ln in shortfalls:
            item = items_by_id.get(ln.bom_item_id)
            if item is None:
                continue
            mr = await supplier_svc.match_line(
                name=item.name, color=item.material_color, category=item.category,
                aliases=aliases, today=today)
            shortlist = [{
                "supplier_id": str(s.supplier_id), "supplier_name": s.supplier_name,
                "score": s.score, "txn_count": s.txn_count, "has_contact": s.has_contact,
                "last_rate": float(s.last_rate) if s.last_rate else None,
                "last_purchased_at": s.last_purchased_at.isoformat() if s.last_purchased_at else None,
            } for s in mr.ranked]
            default_rate = (mr.ranked[0].last_rate if mr.ranked and mr.ranked[0].last_rate
                            else item.unit_price)
            default_uom = (mr.ranked[0].uom if mr.ranked and mr.ranked[0].uom else item.uom)
            payload = {
                "description": item.name, "color": item.material_color,
                "uom": default_uom, "qty": ln.shortfall_qty,
                "unit_price": default_rate, "bom_item_id": item.id,
                "inventory_item_id": ln.inventory_item_id,
                "candidates": {"method": mr.method, "ranked": shortlist,
                               "suggestion": mr.suggestion, "ambiguous": mr.ambiguous},
            }
            if mr.chosen_supplier_id is not None:
                sid = mr.chosen_supplier_id
                groups.setdefault(sid, []).append(payload)
                group_meta[sid] = {"method": mr.method, "ranked": shortlist}
            else:
                unresolved.append(payload)

        created: list[PurchaseOrder] = []
        for sid, lines in groups.items():
            supplier = await self.repo.get_supplier(sid)
            po = PurchaseOrder(
                supplier_id=sid, bom_id=bom_id,
                client_order_id=bom.client_order_id,
                buyer_ref=self._buyer_ref(identity), status=POStatus.DRAFT.value,
                currency="INR", delivery_days=settings.po_default_delivery_days,
                payment_terms_days=(getattr(supplier, "payment_terms_days", None)
                                    or settings.po_default_payment_terms_days),
                revision=1, created_by=getattr(user, "id", None),
                match_method=group_meta[sid]["method"],
                candidates={"ranked": group_meta[sid]["ranked"]},
                needs_supplier=False,
                no_contact_channel=not bool(getattr(supplier, "email", None)
                                            or getattr(supplier, "phone", None)),
            )
            po.items = [self._new_item(n, l) for n, l in enumerate(lines, start=1)]
            self._recompute(po, getattr(supplier, "state_code", None))
            self.db.add(po)
            created.append(po)

        for payload in unresolved:
            po = PurchaseOrder(
                supplier_id=None, bom_id=bom_id, client_order_id=bom.client_order_id,
                buyer_ref=self._buyer_ref(identity), status=POStatus.DRAFT.value,
                currency="INR", delivery_days=settings.po_default_delivery_days,
                payment_terms_days=settings.po_default_payment_terms_days, revision=1,
                created_by=getattr(user, "id", None), match_method=None,
                candidates=payload["candidates"], needs_supplier=True,
                no_contact_channel=False,
            )
            po.items = [self._new_item(1, payload)]
            self._recompute(po, None)
            self.db.add(po)
            created.append(po)

        await self.db.flush()
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action="PO_GENERATE",
            entity_type="bom", entity_id=bom_id, at=datetime.now(timezone.utc),
            after={"purchase_orders": len(created),
                   "resolved": len(groups), "needs_supplier": len(unresolved)},
        ))
        await self.db.commit()
        views = [present.po_view(await self.repo.get_po(p.id)) for p in created]
        return {"bom_id": str(bom_id), "purchase_orders": views,
                "resolved": len(groups), "needs_supplier": len(unresolved)}

    @staticmethod
    def _new_item(no: int, payload: dict) -> PoItem:
        return PoItem(
            item_no=no, description=payload["description"], color=payload.get("color"),
            uom=payload.get("uom"),
            qty=Decimal(str(payload["qty"])) if payload.get("qty") is not None else _ZERO,
            unit_price=(Decimal(str(payload["unit_price"]))
                        if payload.get("unit_price") is not None else _ZERO),
            bom_item_id=payload.get("bom_item_id"),
            inventory_item_id=payload.get("inventory_item_id"),
        )

    # ══════════════════════════════════════════════════════════════════════
    # Reads
    # ══════════════════════════════════════════════════════════════════════
    async def get_po(self, po_id: uuid.UUID) -> dict:
        po = await self._load(po_id)
        return present.po_view(po)

    async def list_pos(self, *, status=None, needs_supplier=None, bom_id=None,
                       supplier_id=None) -> dict:
        rows = await self.repo.list_pos(status=status, needs_supplier=needs_supplier,
                                        bom_id=bom_id, supplier_id=supplier_id)
        return present.po_list_block(rows)

    # ══════════════════════════════════════════════════════════════════════
    # Editable contract (§4) — bulk PATCH + optimistic revision lock
    # ══════════════════════════════════════════════════════════════════════
    _ITEM_FIELDS = {"description", "color", "uom", "qty", "unit_price"}
    _PO_FIELDS = {"supplier_id", "buyer_ref", "delivery_days", "payment_terms_days"}

    async def edit_po(self, user, po_id: uuid.UUID, base_revision: int,
                      item_edits: list[dict], po_edits: dict | None,
                      add_items: list[dict] | None, remove_item_ids: list[str] | None) -> dict:
        po = await self._load(po_id)
        if po.status in _SENT_STATES:
            raise HTTPException(409, detail={"error": "po_locked",
                                             "message": "A sent PO is frozen — cancel + re-issue."})
        if po.revision != base_revision:
            raise HTTPException(409, detail={"error": "stale_revision",
                                             "current_revision": po.revision})
        before = self._snap(po)
        by_id = {str(i.id): i for i in po.items}
        supplier_changed = False

        for e in item_edits or []:
            item = by_id.get(str(e.get("po_item_id")))
            if item is None:
                raise HTTPException(422, detail={"error": "unknown_po_item",
                                                 "po_item_id": e.get("po_item_id")})
            field_ = e.get("field")
            if field_ not in self._ITEM_FIELDS:
                raise HTTPException(422, detail={"error": "unsupported_field", "field": field_})
            value = e.get("value")
            if field_ in ("qty", "unit_price"):
                try:
                    value = Decimal(str(value))
                except (InvalidOperation, TypeError, ValueError):
                    raise HTTPException(422, detail={"error": "invalid_value", "field": field_})
            setattr(item, field_, value)

        for rid in remove_item_ids or []:
            item = by_id.get(str(rid))
            if item is not None:
                po.items.remove(item)

        for a in add_items or []:
            po.items.append(self._new_item(len(po.items) + 1, {
                "description": a.get("description") or "", "color": a.get("color"),
                "uom": a.get("uom"), "qty": a.get("qty"), "unit_price": a.get("unit_price"),
                "bom_item_id": a.get("bom_item_id"), "inventory_item_id": a.get("inventory_item_id"),
            }))

        for f in (po_edits or {}):
            if f not in self._PO_FIELDS:
                raise HTTPException(422, detail={"error": "unsupported_field", "field": f})
            if f == "supplier_id":
                new_sid = po_edits[f]
                if new_sid:
                    sup = await self.repo.get_supplier(uuid.UUID(str(new_sid)))
                    if sup is None or not sup.is_active:
                        raise HTTPException(422, detail={"error": "unknown_supplier"})
                    po.supplier_id = sup.id
                    po.needs_supplier = False
                    po.no_contact_channel = not bool(sup.email or sup.phone)
                    po.match_method = "manual"
                    supplier_changed = True
            else:
                setattr(po, f, po_edits[f])

        # a supplier change re-routes the §3 approver class and resets any approval (§4)
        if supplier_changed and po.status != POStatus.DRAFT.value:
            po.status = POStatus.DRAFT.value
            po.approved_by = po.approved_at = None

        supplier = await self.repo.get_supplier(po.supplier_id) if po.supplier_id else None
        self._recompute(po, getattr(supplier, "state_code", None))

        if await self.repo.claim_po_revision(po_id, base_revision) == 0:
            await self.repo.rollback()
            fresh = await self.repo.get_po(po_id)
            raise HTTPException(409, detail={"error": "stale_revision",
                                             "current_revision": fresh.revision if fresh else None})
        await self.db.commit()
        po = await self._load(po_id)
        await self._audit(user, "PO_EDIT", po.id, before=before, after=self._snap(po))
        await self.db.commit()
        return {"revision": po.revision, "recomputed": present.po_view(po)}

    # ══════════════════════════════════════════════════════════════════════
    # Cross-check state machine (§3)
    # ══════════════════════════════════════════════════════════════════════
    async def submit_po(self, user, po_id: uuid.UUID) -> dict:
        po = await self._load(po_id)
        if po.status not in (POStatus.DRAFT.value, POStatus.REJECTED.value):
            raise HTTPException(409, detail={"error": "not_submittable",
                                             "current_status": po.status})
        if po.needs_supplier or po.supplier_id is None:
            raise HTTPException(409, detail={"error": "needs_supplier",
                                             "message": "Assign a supplier before submitting."})
        if not po.items:
            raise HTTPException(409, detail={"error": "empty_po"})
        po.status = POStatus.PENDING_APPROVAL.value
        await self.db.commit()
        notes = await self._notify_approvers(po)
        await self._audit(user, "PO_SUBMIT_FOR_APPROVAL", po.id,
                          after={"status": po.status, "notifications": notes})
        await self.db.commit()
        return {"po_id": str(po.id), "status": po.status, "notifications_created": notes}

    async def approve_po(self, user, po_id: uuid.UUID) -> dict:
        po = await self._load(po_id)
        if po.status != POStatus.PENDING_APPROVAL.value:
            raise HTTPException(409, detail={"error": "not_pending_approval",
                                             "current_status": po.status})
        await self._assert_router_role(user, po)
        po.status = POStatus.APPROVED.value
        po.approved_by = getattr(user, "id", None)
        po.approved_at = datetime.now(timezone.utc)
        await self._audit(user, "PO_APPROVE", po.id,
                          after={"status": po.status,
                                 "approved_at": po.approved_at.isoformat()})
        await self.db.commit()
        return {"po_id": str(po.id), "status": po.status}

    async def reject_po(self, user, po_id: uuid.UUID, reason: str) -> dict:
        if not reason or not reason.strip():
            raise HTTPException(422, detail={"error": "reason_required"})
        po = await self._load(po_id)
        if po.status != POStatus.PENDING_APPROVAL.value:
            raise HTTPException(409, detail={"error": "not_pending_approval",
                                             "current_status": po.status})
        await self._assert_router_role(user, po)
        po.status = POStatus.REJECTED.value
        po.rejected_by = getattr(user, "id", None)
        po.rejected_at = datetime.now(timezone.utc)
        po.rejection_reason = reason.strip()
        await self._audit(user, "PO_REJECT", po.id,
                          after={"status": po.status, "rejection_reason": po.rejection_reason})
        await self.db.commit()
        return {"po_id": str(po.id), "status": po.status,
                "rejection_reason": po.rejection_reason}

    async def cancel_po(self, user, po_id: uuid.UUID) -> dict:
        po = await self._load(po_id)
        if po.status in _SENT_STATES:
            raise HTTPException(409, detail={"error": "po_locked",
                                             "message": "A sent PO cannot be cancelled in place."})
        po.status = POStatus.CANCELLED.value
        await self._audit(user, "PO_CANCEL", po.id, after={"status": po.status})
        await self.db.commit()
        return {"po_id": str(po.id), "status": po.status}

    # ══════════════════════════════════════════════════════════════════════
    # Send (§5) — number, render, email, start the escalation clock
    # ══════════════════════════════════════════════════════════════════════
    async def send_po(self, user, po_id: uuid.UUID) -> dict:
        po = await self._load(po_id)
        if po.status != POStatus.APPROVED.value:
            raise HTTPException(409, detail={"error": "not_approved",
                                             "current_status": po.status})
        supplier = await self.repo.get_supplier(po.supplier_id)
        now = datetime.now(timezone.utc)
        today = now.date()
        if not po.po_number:
            po.po_number = await self.repo.next_po_number(_financial_year(today))
        po.issue_date = today
        po.tracking_token = po_tracking.new_token()

        # render + store the PDF (idempotent on sha256, mirrors the BOM export)
        view = present.po_view(po)
        view["supplier"] = self._supplier_block(supplier)
        meta = {"buyer": self._buyer_block(), "generated_at": today.isoformat()}
        cfg = _po_template_cfg(getattr(supplier, "supplier_type", None))
        pdf_bytes, mime, ext = await run_in_threadpool(render_po_pdf, view, meta, cfg)
        sha = hashlib.sha256(pdf_bytes).hexdigest()
        doc = await self.repo.get_document_by_sha(sha)
        if doc is None:
            try:
                url = get_storage().put(f"pos/{po.id}/{sha}{ext}", pdf_bytes)
            except Exception:
                url = None
            doc = await self.repo.add_document(Document(
                kind=DocumentKind.SUPPLIER_PO_PDF.value, filename=f"{po.po_number}{ext}",
                mime=mime, storage_url=url, sha256=sha, size_bytes=len(pdf_bytes),
                uploaded_by=getattr(user, "id", None)))
        po.pdf_document_id = doc.id

        # contact routing (§5b): real email → send; else short-circuit to WhatsApp rung
        email_ok = bool(supplier.email) and supplier.email_status != SupplierEmailStatus.INVALID.value
        sent_channel = None
        if email_ok:
            await self._send_email(po, supplier, pdf_bytes, mime, now)
            sent_channel = "email"
            po.current_rung = 0
        elif supplier.phone or supplier.whatsapp_phone:
            po.no_contact_channel = False
            po.current_rung = 0     # the sweeper's first rung will WhatsApp them
        else:
            po.no_contact_channel = True

        po.status = POStatus.SENT.value
        po.sent_at = now
        po.next_escalation_at = now + timedelta(hours=settings.po_escalation_hours)
        await self._audit(user, "PO_SEND", po.id,
                          after={"po_number": po.po_number, "channel": sent_channel,
                                 "pdf_document_id": str(doc.id),
                                 "no_contact_channel": po.no_contact_channel})
        await self.db.commit()
        # advance the production board (§8): first PO sent → PO_RAISED
        await self._on_po_raised(po)
        return {"po_id": str(po.id), "po_number": po.po_number, "status": po.status,
                "channel": sent_channel, "no_contact_channel": po.no_contact_channel,
                "pdf_document_id": str(doc.id)}

    async def _send_email(self, po, supplier, pdf_bytes, mime, now) -> None:
        base = settings.po_tracking_base_url
        link = f"{settings.frontend_base_url}/pos/{po.id}"
        html = (f"<html><body><p>Dear {supplier.name},</p>"
                f"<p>Please find attached our purchase order <b>{po.po_number}</b> "
                f"(ref {po.buyer_ref or '-'}). Kindly confirm receipt and delivery.</p>"
                f'<p><a href="{link}">View / confirm this PO</a></p>'
                f"<p>Delivery {po.delivery_days} days · payment {po.payment_terms_days} days.</p>"
                f"<p>Regards,<br>{settings.po_buyer_name}</p></body></html>")
        html = po_tracking.inject_tracking(html, base, po.tracking_token, settings.secret_key)
        text = (f"Dear {supplier.name},\nPlease find attached PO {po.po_number} "
                f"(ref {po.buyer_ref or '-'}). Kindly confirm receipt.\n"
                f"View/confirm: {link}\nRegards, {settings.po_buyer_name}")
        ext = ".pdf" if mime == "application/pdf" else ".html"
        attachments = [{"filename": f"{po.po_number}{ext}", "content": pdf_bytes, "mime": mime}]
        ok = await run_in_threadpool(
            get_notifier().send, to=supplier.email,
            subject=f"Purchase Order {po.po_number} — {settings.po_buyer_name}",
            body=text, html=html, attachments=attachments)
        resp = PoResponse(
            purchase_order_id=po.id, channel=POResponseChannel.EMAIL.value,
            sent_at=now, status=(POResponseStatus.PENDING.value if ok
                                 else POResponseStatus.FAILED.value),
            tracking_token=po.tracking_token)
        self.db.add(resp)
        self.db.add(Notification(
            supplier_id=supplier.id, channel=NotificationChannel.EMAIL.value,
            type=NotificationType.PO_DISPATCH.value,
            subject=f"PO {po.po_number} dispatched", body=text,
            entity_type="purchase_order", entity_id=po.id,
            status=(NotificationStatus.SENT.value if ok else NotificationStatus.FAILED.value),
            sent_at=now if ok else None))

    # ══════════════════════════════════════════════════════════════════════
    # Open tracking (§6) + acknowledgement (§7d)
    # ══════════════════════════════════════════════════════════════════════
    async def record_open(self, token: str, *, ip=None, user_agent=None) -> bool:
        po = await self.repo.get_po_by_token(token)
        if po is None:
            return False
        now = datetime.now(timezone.utc)
        if po.first_opened_at is None:
            po.first_opened_at = now
        resp = await self.repo.latest_response(po.id)
        if resp is not None and resp.status == POResponseStatus.PENDING.value:
            resp.status = POResponseStatus.OPENED.value
            resp.responded_at = now
        self.db.add(PoTrackingEvent(
            purchase_order_id=po.id, po_response_id=resp.id if resp else None,
            tracking_token=token, event_type=POTrackingEventType.OPEN.value,
            channel="email", ip=ip, user_agent=user_agent, at=now))
        await self.db.commit()
        return True

    async def record_click(self, token: str, *, ip=None, user_agent=None) -> bool:
        po = await self.repo.get_po_by_token(token)
        if po is None:
            return False
        now = datetime.now(timezone.utc)
        if po.first_clicked_at is None:
            po.first_clicked_at = now
        if po.first_opened_at is None:
            po.first_opened_at = now
        self.db.add(PoTrackingEvent(
            purchase_order_id=po.id, tracking_token=token,
            event_type=POTrackingEventType.CLICK.value, channel="email",
            ip=ip, user_agent=user_agent, at=now))
        await self.db.commit()
        return True

    async def record_ses_event(self, *, token: str | None, event_type: str,
                               meta: dict | None = None) -> bool:
        """SES → SNS webhook (§5b): a hard bounce flags the email invalid, fails the
        response, and short-circuits the PO to the WhatsApp rung (don't burn 5h on a
        dead address). Complaint suppresses further email."""
        po = await self.repo.get_po_by_token(token) if token else None
        if po is None:
            return False
        now = datetime.now(timezone.utc)
        et = (event_type or "").lower()
        self.db.add(PoTrackingEvent(
            purchase_order_id=po.id, tracking_token=token, event_type=et,
            channel="email", meta=meta, at=now))
        if et in (POTrackingEventType.BOUNCE.value, POTrackingEventType.COMPLAINT.value):
            supplier = await self.repo.get_supplier(po.supplier_id) if po.supplier_id else None
            if supplier is not None:
                supplier.email_status = SupplierEmailStatus.INVALID.value
            resp = await self.repo.latest_response(po.id)
            if resp is not None:
                resp.status = POResponseStatus.FAILED.value
            if po.acknowledged_at is None and po.status in (POStatus.SENT.value,
                                                            POStatus.ESCALATED.value):
                po.next_escalation_at = now      # escalate on the next sweep, now
        await self.db.commit()
        return True

    async def acknowledge_po(self, user, po_id: uuid.UUID, *, channel: str,
                             confirmed_qty=None, notes=None) -> dict:
        po = await self._load(po_id)
        return await self._acknowledge(po, channel=channel, confirmed_qty=confirmed_qty,
                                       notes=notes, actor=user)

    async def acknowledge_by_token(self, token: str, *, channel: str,
                                   confirmed_qty=None, notes=None) -> bool:
        po = await self.repo.get_po_by_token(token)
        if po is None:
            return False
        await self._acknowledge(po, channel=channel, confirmed_qty=confirmed_qty,
                                notes=notes, actor=None)
        return True

    async def _acknowledge(self, po, *, channel, confirmed_qty, notes, actor) -> dict:
        """Stop the ladder the instant the supplier acknowledges by ANY channel (§7d)."""
        if po.acknowledged_at is not None:
            return present.po_view(po)
        now = datetime.now(timezone.utc)
        po.acknowledged_at = now
        po.acknowledged_channel = channel
        po.status = POStatus.CONFIRMED.value
        po.next_escalation_at = None
        resp = await self.repo.latest_response(po.id)
        if resp is None or resp.channel != channel:
            resp = PoResponse(purchase_order_id=po.id, channel=channel)
            self.db.add(resp)
        resp.status = POResponseStatus.CONFIRMED.value
        resp.responded_at = now
        if confirmed_qty is not None:
            try:
                resp.confirmed_qty = Decimal(str(confirmed_qty))
            except (InvalidOperation, TypeError, ValueError):
                pass
        if notes:
            resp.notes = notes
        self.db.add(PoTrackingEvent(
            purchase_order_id=po.id, tracking_token=po.tracking_token,
            event_type=channel if channel in ("whatsapp", "call") else "click",
            channel=channel, at=now))
        await self._audit(actor, "PO_ACKNOWLEDGE", po.id,
                          after={"channel": channel, "acknowledged_at": now.isoformat()})
        await self.db.commit()
        await self._on_po_confirmed(po)
        return present.po_view(await self._load(po.id))

    # ══════════════════════════════════════════════════════════════════════
    # Escalation ladder (§7) — driven by the lifespan sweeper
    # ══════════════════════════════════════════════════════════════════════
    async def sweep_escalations(self) -> int:
        """One rung per due PO per sweep (idempotent — the `acknowledged_at IS NULL` +
        `current_rung < 3` guards make a second pass a no-op). Returns rungs advanced."""
        now = datetime.now(timezone.utc)
        due = await self.repo.due_po_escalations(now)
        if not due:
            return 0
        transport = get_escalation_transport()
        advanced = 0
        for po in due:
            supplier = po.supplier
            rung = po.current_rung
            next_at = now + timedelta(hours=settings.po_escalation_hours)
            if rung == 0:
                # RUNG 1 — WhatsApp
                to = (getattr(supplier, "whatsapp_phone", None)
                      or getattr(supplier, "phone", None))
                body = (f"Reminder: PO {po.po_number} from {settings.po_buyer_name} "
                        f"awaits your confirmation. Please reply to confirm.")
                if to:
                    res = await run_in_threadpool(transport.send_whatsapp, to=to, body=body)
                else:
                    res = {"ok": False, "error": "no_whatsapp"}
                self.db.add(PoResponse(
                    purchase_order_id=po.id, channel=POResponseChannel.WHATSAPP.value,
                    sent_at=now, status=POResponseStatus.PENDING.value,
                    notes=res.get("error")))
                self.db.add(PoTrackingEvent(
                    purchase_order_id=po.id, event_type=POTrackingEventType.WHATSAPP.value,
                    channel="whatsapp", meta=res, at=now))
                po.status = POStatus.ESCALATED.value
                po.current_rung = 1
                po.next_escalation_at = next_at
            elif rung == 1:
                # RUNG 2 — auto-call
                to = getattr(supplier, "phone", None)
                msg = f"Purchase order {po.po_number} from {settings.po_buyer_name}"
                if to:
                    res = await run_in_threadpool(transport.place_call, to=to, message=msg)
                else:
                    res = {"ok": False, "error": "no_phone"}
                self.db.add(PoResponse(
                    purchase_order_id=po.id, channel=POResponseChannel.CALL.value,
                    sent_at=now, status=POResponseStatus.PENDING.value,
                    notes=res.get("error")))
                self.db.add(PoTrackingEvent(
                    purchase_order_id=po.id, event_type=POTrackingEventType.CALL.value,
                    channel="call", meta=res, at=now))
                po.current_rung = 2
                po.next_escalation_at = next_at
            else:
                # RUNG 3 — exhausted, hand to the buyer
                self.db.add(Notification(
                    channel=NotificationChannel.IN_APP.value,
                    type=NotificationType.PO_ESCALATION_EXHAUSTED.value,
                    subject=f"PO {po.po_number} — no supplier response",
                    body=f"PO {po.po_number} got no response after email, WhatsApp and a call. "
                         f"Please follow up manually.",
                    entity_type="purchase_order", entity_id=po.id,
                    status=NotificationStatus.PENDING.value))
                po.current_rung = 3
                po.next_escalation_at = None
            advanced += 1
        await self.db.commit()
        return advanced

    # ══════════════════════════════════════════════════════════════════════
    # Helpers
    # ══════════════════════════════════════════════════════════════════════
    async def _load(self, po_id: uuid.UUID) -> PurchaseOrder:
        po = await self.repo.get_po(po_id)
        if po is None:
            raise HTTPException(404, "Purchase order not found.")
        return po

    def _recompute(self, po: PurchaseOrder, supplier_state_code: str | None) -> None:
        items = [{"qty": i.qty, "unit_price": i.unit_price} for i in po.items]
        totals = po_costing.compute_totals(
            items, supplier_state_code=supplier_state_code,
            buyer_state_code=settings.po_buyer_state_code, gst_rate=settings.po_gst_rate)
        for item, amt in zip(po.items, totals["amounts"]):
            item.amount = amt
        po.subtotal = totals["subtotal"]
        po.cgst = totals["cgst"]
        po.sgst = totals["sgst"]
        po.igst = totals["igst"]
        po.gst_mode = totals["gst_mode"]
        po.round_off = totals["round_off"]
        po.total = totals["total"]

    def _po_type(self, supplier) -> str:
        st = getattr(supplier, "supplier_type", None)
        return st or SupplierType.ACCESSORY.value

    async def _assert_router_role(self, user, po) -> None:
        role = getattr(user, "role", None)
        if role == UserRole.MANAGING_DIRECTOR:        # superuser approves anything
            return
        supplier = await self.repo.get_supplier(po.supplier_id) if po.supplier_id else None
        allowed = (_LEATHER_APPROVERS if self._po_type(supplier) == SupplierType.LEATHER.value
                   else _ACCESSORY_APPROVERS)
        if role not in allowed:
            raise HTTPException(403, detail={
                "error": "wrong_approver",
                "message": f"This PO routes to {sorted(r.value for r in allowed)}."})

    async def _notify_approvers(self, po) -> int:
        from app.modules.users.service import UserService

        supplier = await self.repo.get_supplier(po.supplier_id) if po.supplier_id else None
        roles = (list(_LEATHER_APPROVERS) if self._po_type(supplier) == SupplierType.LEATHER.value
                 else list(_ACCESSORY_APPROVERS))
        recipients = await UserService(self.db).list_by_roles(roles)
        if not recipients:
            return 0
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=settings.bom_review_escalation_hours)
        link = f"{settings.frontend_base_url}/pos/{po.id}"
        seen: set = set()
        n = 0
        for u in recipients:
            if u.id in seen:
                continue
            seen.add(u.id)
            self.db.add(Notification(
                recipient_user_id=u.id, channel=NotificationChannel.IN_APP.value,
                type=NotificationType.PO_AWAITING_APPROVAL.value,
                subject=f"PO {po.po_number or 'draft'} awaiting cross-check",
                body=f"A supplier PO is awaiting your cross-check approval. {link}",
                entity_type="purchase_order", entity_id=po.id,
                status=NotificationStatus.PENDING.value, scheduled_for=deadline))
            n += 1
        await self.db.commit()
        return n

    async def _identity(self, bom):
        from app.modules.clients.service import ClientService
        cs = ClientService(self.db)
        order = await cs.get_order(bom.client_order_id) if bom.client_order_id else None
        style = await cs.get_style(bom.style_id) if bom.style_id else None
        return {"order": order, "style": style}

    @staticmethod
    def _buyer_ref(identity) -> str | None:
        style = identity.get("style")
        order = identity.get("order")
        ref = getattr(style, "customer_ref", None) or getattr(order, "order_number", None)
        return f"#{ref}" if ref else None

    def _buyer_block(self) -> dict:
        return {"name": settings.po_buyer_name, "address": settings.po_buyer_address,
                "gstin": settings.po_buyer_gstin, "email": settings.po_buyer_email,
                "phone": settings.po_buyer_phone}

    @staticmethod
    def _supplier_block(s) -> dict:
        return {"name": getattr(s, "name", None), "address": getattr(s, "address", None),
                "gstin": getattr(s, "gstin", None), "email": getattr(s, "email", None),
                "phone": getattr(s, "phone", None)}

    @staticmethod
    def _snap(po: PurchaseOrder) -> dict:
        return {"revision": po.revision, "status": po.status,
                "supplier_id": str(po.supplier_id) if po.supplier_id else None,
                "subtotal": str(po.subtotal), "total": str(po.total),
                "items": [{"id": str(i.id), "description": i.description,
                           "qty": str(i.qty), "unit_price": str(i.unit_price),
                           "amount": str(i.amount)} for i in po.items]}

    async def _audit(self, user, action, entity_id, *, before=None, after=None) -> None:
        self.db.add(AuditLog(
            actor_user_id=getattr(user, "id", None), action=action,
            entity_type="purchase_order", entity_id=entity_id, before=before, after=after,
            at=datetime.now(timezone.utc)))

    async def _on_po_raised(self, po) -> None:
        try:
            from app.modules.supplier_po.production_tracking_service import (
                ProductionTrackingService,
            )
            await ProductionTrackingService(self.db).on_po_raised(po.bom_id)
        except Exception:
            pass

    async def _on_po_confirmed(self, po) -> None:
        try:
            from app.modules.supplier_po.production_tracking_service import (
                ProductionTrackingService,
            )
            await ProductionTrackingService(self.db).on_po_confirmed(po.bom_id)
        except Exception:
            pass
