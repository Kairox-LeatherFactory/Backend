"""
================================================================================
tests/test_procurement_stage5.py — Stage-5 supplier-PO acceptance (stage-5 §11)
================================================================================

Drives the supplier-PO module end to end at the service layer (the same posture as
test_procurement_stage4.py — real queries against in-memory SQLite, no mocks):

  1. Matching is deterministic-first + grouped: shortfall lines resolve to suppliers via
     the supply-history ledger, lines for one vendor collapse into ONE PO, an article with
     no history yields a held `needs_supplier` PO (never a fabricated vendor).
  2. GST reproduces the real forms: intra-state subtotal 6,500 → CGST 390 + SGST 390 →
     7,280; an inter-state supplier produces IGST instead.
  3. Cross-check routes by material type: leather → Cutting Manager, accessory → MD/DM/HR,
     the wrong role gets 403.
  4. Editable PO = the BOM contract: stale base_revision → 409, a fresh edit recomputes +
     bumps revision, a sent PO is frozen (409 po_locked).
  5. Send + tracking + escalation + stop: a contactable PO sends (PDF stored); a no-email
     supplier flags no_contact_channel; an open stamps first_opened_at; the sweeper advances
     the ladder one idempotent rung; any acknowledgement halts it forever.
  6. Production tracking advances BOM_APPROVED → INVENTORY_CHECKED → PO_RAISED → PO_CONFIRMED
     → MATERIAL_READY; MD manually releases.
  7. Supplier CRUD: create / edit-unblocks-contact / soft-delete-excludes-from-matching /
     reactivate, each audited.
================================================================================
"""
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.enums import UserRole
from app.core.models import AuditLog
from app.modules.bom.enums import BomItemCategory, BomStatus
from app.modules.bom.models import Bom, BomItem
from app.modules.bom.service import BomService
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.inventory.service import InventoryService
from app.modules.supplier_po.enums import (
    POResponseStatus,
    POStatus,
    POTrackingEventType,
    ProductionTrackingStatus,
    SupplierEmailStatus,
    SupplierType,
)
from app.modules.supplier_po.models import (
    PurchaseOrder,
    Supplier,
    SupplierSupplyHistory,
)
from app.modules.supplier_po.po_service import PoService
from app.modules.supplier_po.production_tracking_service import ProductionTrackingService
from app.modules.supplier_po.supplier_service import SupplierService
from app.modules.users.models import User


# ── actors ──────────────────────────────────────────────────────────────────
async def _user(db, role, phone) -> User:
    u = User(id=uuid.uuid4(), name=role.value, phone=phone, role=role,
             password_hash="x", is_active=True)
    db.add(u)
    await db.commit()
    return u


# ── supplier + supply-history fixtures ──────────────────────────────────────
async def _supplier(db, name, *, supplier_type, state_code="33", email=None,
                    phone=None) -> Supplier:
    s = Supplier(name=name, supplier_type=supplier_type, state_code=state_code,
                 email=email, phone=phone, whatsapp_phone=phone,
                 email_status=(SupplierEmailStatus.VALID.value if email
                               else SupplierEmailStatus.UNKNOWN.value),
                 currency="INR", payment_terms_days=60, lead_time_days=10, is_active=True)
    db.add(s)
    await db.flush()
    return s


async def _history(db, supplier, normalized_description, *, mode, rate="6.00",
                   days_ago=10, txns=3):
    last = (datetime.now(timezone.utc) - timedelta(days=days_ago)).date()
    db.add(SupplierSupplyHistory(
        supplier_id=supplier.id, normalized_description=normalized_description,
        raw_description=normalized_description, mode=mode, uom="DCM", txn_count=txns,
        first_purchased_at=last, last_purchased_at=last,
        last_rate=Decimal(rate), min_rate=Decimal(rate), max_rate=Decimal(rate)))
    await db.commit()


# ── approved BOM (no inventory → every stockable line is a shortfall) ────────
async def _approved_bom(db, *, lines, order_qty=60, order_no="5001", code="BG",
                        client_name="Beau Geste"):
    """lines = list of (category, name, color, uom, qty_per_garment)."""
    client = (await db.execute(select(Client).where(Client.code == code))).scalar_one_or_none()
    if client is None:
        client = Client(name=client_name, country="Japan", code=code, currency="USD")
        db.add(client)
        await db.flush()
    order = ClientOrder(client_id=client.id, order_number=order_no, currency="USD")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name="TRACK PANT",
                  customer_ref=f"CR-{order_no}", currency="USD")
    db.add(style)
    await db.flush()
    db.add(SKU(style_id=style.id, color_code="BLK", size="M", qty_ordered=order_qty))
    now = datetime.now(timezone.utc)
    bom = Bom(client_order_id=order.id, style_id=style.id, status=BomStatus.LOCKED.value,
              currency="USD", order_qty=order_qty, revision=1, approved_at=now, locked_at=now)
    bom.items = [
        BomItem(category=cat, name=name, material_color=color, uom=uom,
                qty_per_garment=Decimal(str(qpg)), unit_price=Decimal("1"),
                bulk_qty=Decimal(str(order_qty)) * Decimal(str(qpg)))
        for (cat, name, color, uom, qpg) in lines
    ]
    db.add(bom)
    await db.commit()
    return client, order, style, bom


def _po_by_supplier(view_list, supplier_id):
    for p in view_list:
        if p["supplier_id"] == str(supplier_id):
            return p
    return None


# ════════════════════════════════════════════════════════════════════════════
# §11.1 / §11.2 — matching is deterministic-first, grouped, never a silent guess
# ════════════════════════════════════════════════════════════════════════════
async def test_generate_groups_matches_and_holds_unresolved(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            email="bismi@x.com")
    mvpnk = await _supplier(db, "M.V.P.N.K", supplier_type=SupplierType.ACCESSORY.value,
                            email="mvpnk@x.com")
    await _history(db, bismi, "SHEEP NAPPA BLACK", mode="LEATHER", rate="6.15")
    await _history(db, bismi, "GOAT SUEDE BROWN", mode="LEATHER", rate="7.20")
    await _history(db, mvpnk, "NYLON FUSING", mode="MATERIALS", rate="40")

    _, _, _, bom = await _approved_bom(db, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP NAPPA", "BLACK", "dm²", 10),
        (BomItemCategory.MAIN_MATERIAL.value, "GOAT SUEDE", "BROWN", "dm²", 8),
        (BomItemCategory.ACCESSORY.value, "NYLON FUSING", None, "NOS", 2),
        (BomItemCategory.ACCESSORY.value, "UNOBTANIUM WIDGET", None, "NOS", 1),
    ])
    await InventoryService(db).run_check(md, bom.id)
    out = await PoService(db).generate_for_bom(md, bom.id)

    assert out["resolved"] == 2 and out["needs_supplier"] == 1
    pos = out["purchase_orders"]
    assert len(pos) == 3

    # the two leather lines GROUP into ONE BISMI PO, each item keeping its bom_item link
    bismi_po = _po_by_supplier(pos, bismi.id)
    assert bismi_po is not None
    assert len(bismi_po["items"]) == 2
    assert bismi_po["match_method"] == "ledger"
    assert all(it["bom_item_id"] for it in bismi_po["items"])

    # the accessory line resolves to its own PO
    mvpnk_po = _po_by_supplier(pos, mvpnk.id)
    assert mvpnk_po is not None and len(mvpnk_po["items"]) == 1

    # the article with no history is HELD, never a fabricated vendor
    held = [p for p in pos if p["needs_supplier"]]
    assert len(held) == 1 and held[0]["supplier_id"] is None
    assert held[0]["items"][0]["description"] == "UNOBTANIUM WIDGET"

    # re-running is idempotent — no duplicate POs
    again = await PoService(db).generate_for_bom(md, bom.id)
    assert again.get("already_generated") is True
    assert len(again["purchase_orders"]) == 3


# ════════════════════════════════════════════════════════════════════════════
# §11.4 — GST math reproduces the real forms (intra CGST+SGST, inter IGST)
# ════════════════════════════════════════════════════════════════════════════
async def test_gst_intra_reproduces_form(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            state_code="33", email="bismi@x.com")          # TN = buyer state
    await _history(db, bismi, "SHEEP NAPPA BLACK", mode="LEATHER")
    _, _, _, bom = await _approved_bom(db, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP NAPPA", "BLACK", "dm²", 10)])
    await InventoryService(db).run_check(md, bom.id)
    out = await PoService(db).generate_for_bom(md, bom.id)
    po_id = uuid.UUID(out["purchase_orders"][0]["id"])

    svc = PoService(db)
    po = await svc.get_po(po_id)
    # set the single line to subtotal 6,500 (PO-01 on the real form)
    item_id = po["items"][0]["id"]
    edited = await svc.edit_po(md, po_id, po["revision"],
                               item_edits=[{"po_item_id": item_id, "field": "qty", "value": "1"},
                                           {"po_item_id": item_id, "field": "unit_price",
                                            "value": "6500"}],
                               po_edits=None, add_items=None, remove_item_ids=None)
    v = edited["recomputed"]
    assert v["gst_mode"] == "INTRA"
    assert v["subtotal"] == 6500.0
    assert v["cgst"] == 390.0 and v["sgst"] == 390.0 and v["igst"] == 0.0
    assert v["total"] == 7280.0


async def test_gst_inter_state_uses_igst(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    glob = await _supplier(db, "GLOBAL EXIM", supplier_type=SupplierType.LEATHER.value,
                           state_code="29", email="glob@x.com")            # not TN → inter-state
    await _history(db, glob, "GOAT SUEDE BROWN", mode="LEATHER")
    _, _, _, bom = await _approved_bom(db, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "GOAT SUEDE", "BROWN", "dm²", 10)])
    await InventoryService(db).run_check(md, bom.id)
    out = await PoService(db).generate_for_bom(md, bom.id)
    po_id = uuid.UUID(out["purchase_orders"][0]["id"])

    svc = PoService(db)
    po = await svc.get_po(po_id)
    edited = await svc.edit_po(md, po_id, po["revision"],
                               item_edits=[{"po_item_id": po["items"][0]["id"], "field": "qty",
                                            "value": "1"},
                                           {"po_item_id": po["items"][0]["id"],
                                            "field": "unit_price", "value": "6500"}],
                               po_edits=None, add_items=None, remove_item_ids=None)
    v = edited["recomputed"]
    assert v["gst_mode"] == "INTER"
    assert v["igst"] == 780.0 and v["cgst"] == 0.0 and v["sgst"] == 0.0
    assert v["total"] == 7280.0


# ════════════════════════════════════════════════════════════════════════════
# §11.5 — cross-check routes by material type; wrong role 403
# ════════════════════════════════════════════════════════════════════════════
async def test_cross_check_routing_by_material_type(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    dm = await _user(db, UserRole.DIRECT_MANAGER, "9000000001")
    cutting = await _user(db, UserRole.CUTTING_MANAGER, "9000000002")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            email="bismi@x.com")
    mvpnk = await _supplier(db, "M.V.P.N.K", supplier_type=SupplierType.ACCESSORY.value,
                            email="mvpnk@x.com")
    await _history(db, bismi, "SHEEP NAPPA BLACK", mode="LEATHER")
    await _history(db, mvpnk, "NYLON FUSING", mode="MATERIALS")
    _, _, _, bom = await _approved_bom(db, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP NAPPA", "BLACK", "dm²", 10),
        (BomItemCategory.ACCESSORY.value, "NYLON FUSING", None, "NOS", 2)])
    await InventoryService(db).run_check(md, bom.id)
    out = await PoService(db).generate_for_bom(md, bom.id)
    leather_po = uuid.UUID(_po_by_supplier(out["purchase_orders"], bismi.id)["id"])
    acc_po = uuid.UUID(_po_by_supplier(out["purchase_orders"], mvpnk.id)["id"])

    svc = PoService(db)
    await svc.submit_po(md, leather_po)
    await svc.submit_po(md, acc_po)

    # leather PO → Cutting Manager; a DM (accessory approver) is the WRONG role here
    with pytest.raises(Exception) as ei:
        await svc.approve_po(dm, leather_po)
    assert ei.value.status_code == 403
    res = await svc.approve_po(cutting, leather_po)
    assert res["status"] == POStatus.APPROVED.value

    # accessory PO → MD/DM/HR; the Cutting Manager is the WRONG role here
    with pytest.raises(Exception) as ei2:
        await svc.approve_po(cutting, acc_po)
    assert ei2.value.status_code == 403
    res2 = await svc.approve_po(dm, acc_po)
    assert res2["status"] == POStatus.APPROVED.value


# ════════════════════════════════════════════════════════════════════════════
# §11.6 — editable PO = the BOM contract (stale revision, recompute, sent lock)
# ════════════════════════════════════════════════════════════════════════════
async def test_editable_contract_revision_and_lock(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            email="bismi@x.com")
    await _history(db, bismi, "SHEEP NAPPA BLACK", mode="LEATHER")
    _, _, _, bom = await _approved_bom(db, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, "SHEEP NAPPA", "BLACK", "dm²", 10)])
    await InventoryService(db).run_check(md, bom.id)
    out = await PoService(db).generate_for_bom(md, bom.id)
    po_id = uuid.UUID(out["purchase_orders"][0]["id"])

    svc = PoService(db)
    po = await svc.get_po(po_id)
    item_id = po["items"][0]["id"]

    # a STALE base_revision is rejected
    with pytest.raises(Exception) as ei:
        await svc.edit_po(md, po_id, po["revision"] + 5,
                          item_edits=[{"po_item_id": item_id, "field": "qty", "value": "2"}],
                          po_edits=None, add_items=None, remove_item_ids=None)
    assert ei.value.status_code == 409
    assert ei.value.detail["error"] == "stale_revision"

    # a FRESH edit recomputes + bumps the revision
    edited = await svc.edit_po(md, po_id, po["revision"],
                               item_edits=[{"po_item_id": item_id, "field": "unit_price",
                                            "value": "10"}],
                               po_edits=None, add_items=None, remove_item_ids=None)
    assert edited["revision"] == po["revision"] + 1
    assert edited["recomputed"]["items"][0]["unit_price"] == 10.0

    # drive to sent, then every edit is frozen (409 po_locked)
    await svc.submit_po(md, po_id)
    await svc.approve_po(md, po_id)
    await svc.send_po(md, po_id)
    fresh = await svc.get_po(po_id)
    with pytest.raises(Exception) as ei2:
        await svc.edit_po(md, po_id, fresh["revision"],
                          item_edits=[{"po_item_id": item_id, "field": "qty", "value": "9"}],
                          po_edits=None, add_items=None, remove_item_ids=None)
    assert ei2.value.status_code == 409
    assert ei2.value.detail["error"] == "po_locked"


# ════════════════════════════════════════════════════════════════════════════
# §11.7 / §11.8 / §11.9 — send, no-contact, open tracking, escalation, stop
# ════════════════════════════════════════════════════════════════════════════
async def _sent_po(db, svc, md, supplier, *, line_name="SHEEP NAPPA", color="BLACK",
                   order_no="5100"):
    await _history(db, supplier, f"{line_name} {color}".strip(), mode="LEATHER")
    _, _, _, bom = await _approved_bom(db, lines=[
        (BomItemCategory.MAIN_MATERIAL.value, line_name, color, "dm²", 10)],
        order_no=order_no)
    await InventoryService(db).run_check(md, bom.id)
    out = await PoService(db).generate_for_bom(md, bom.id)
    po_id = uuid.UUID(out["purchase_orders"][0]["id"])
    await svc.submit_po(md, po_id)
    await svc.approve_po(md, po_id)
    sent = await svc.send_po(md, po_id)
    return po_id, sent, bom


async def test_send_with_email_renders_pdf_and_tracks_open(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            email="bismi@x.com")
    svc = PoService(db)
    po_id, sent, _ = await _sent_po(db, svc, md, bismi)
    assert sent["status"] == POStatus.SENT.value
    assert sent["channel"] == "email"
    assert sent["no_contact_channel"] is False
    assert sent["po_number"].endswith(")") and sent["po_number"].startswith("PO-")
    assert sent["pdf_document_id"] is not None

    po = await svc.get_po(po_id)
    token = (await svc.repo.get_po(po_id)).tracking_token
    assert token

    # the open pixel stamps first_opened_at + a tracking event
    assert await svc.record_open(token, ip="1.2.3.4", user_agent="Mail") is True
    po2 = await svc.get_po(po_id)
    assert po2["first_opened_at"] is not None


async def test_no_email_supplier_flags_no_contact_channel(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    # frequent vendor, but no email AND no phone → no_contact_channel after send
    quiet = await _supplier(db, "CONTACTLESS TANNERY",
                            supplier_type=SupplierType.LEATHER.value, email=None, phone=None)
    svc = PoService(db)
    _, sent, _ = await _sent_po(db, svc, md, quiet, order_no="5200")
    assert sent["channel"] is None
    assert sent["no_contact_channel"] is True


async def test_escalation_ladder_advances_then_stops_on_ack(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            email="bismi@x.com", phone="+919000011111")
    svc = PoService(db)
    po_id, _, _ = await _sent_po(db, svc, md, bismi, order_no="5300")

    # force the escalation clock due, then sweep → RUNG 1 (WhatsApp), status escalated
    po = await svc.repo.get_po(po_id)
    po.next_escalation_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    await db.commit()
    advanced = await svc.sweep_escalations()
    assert advanced == 1
    po2 = await svc.get_po(po_id)
    assert po2["status"] == POStatus.ESCALATED.value
    assert po2["current_rung"] == 1

    # the supplier acknowledges (WhatsApp reply) → confirmed, ladder halts
    await svc.acknowledge_by_token(po2 and (await svc.repo.get_po(po_id)).tracking_token,
                                   channel="whatsapp", notes="ok will deliver")
    po3 = await svc.get_po(po_id)
    assert po3["status"] == POStatus.CONFIRMED.value
    assert po3["acknowledged_channel"] == "whatsapp"

    # a second sweep is a no-op (idempotent — acknowledged_at filters it out forever)
    assert await svc.sweep_escalations() == 0


async def test_hard_bounce_invalidates_email_and_short_circuits(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            email="dead@x.com", phone="+919000022222")
    svc = PoService(db)
    po_id, _, _ = await _sent_po(db, svc, md, bismi, order_no="5400")
    token = (await svc.repo.get_po(po_id)).tracking_token

    assert await svc.record_ses_event(token=token, event_type="Bounce",
                                      meta={"bounceType": "Permanent"}) is True
    supplier = await svc.repo.get_supplier(bismi.id)
    assert supplier.email_status == SupplierEmailStatus.INVALID.value
    # the PO is queued to escalate immediately (don't burn 5h on a dead address)
    po = await svc.repo.get_po(po_id)
    assert po.next_escalation_at is not None
    assert po.next_escalation_at <= datetime.now(timezone.utc)


# ════════════════════════════════════════════════════════════════════════════
# §11.10 — production tracking ladder, system-driven + MD manual release
# ════════════════════════════════════════════════════════════════════════════
async def test_production_tracking_ladder(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    bismi = await _supplier(db, "BISMI LEATHERS", supplier_type=SupplierType.LEATHER.value,
                            email="bismi@x.com", phone="+919000033333")
    await _history(db, bismi, "SHEEP NAPPA BLACK", mode="LEATHER")

    # build a confirmable DRAFT bom, then approve → board lands at INVENTORY_CHECKED
    client = Client(name="Beau Geste", country="Japan", code="BG", currency="USD")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number="5500", currency="USD")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name="TRACK PANT", customer_ref="CR-5500",
                  currency="USD")
    db.add(style)
    await db.flush()
    db.add(SKU(style_id=style.id, color_code="BLK", size="M", qty_ordered=60))
    bom = Bom(client_order_id=order.id, style_id=style.id,
              status=BomStatus.READY_FOR_REVIEW.value, currency="USD", order_qty=60, revision=1,
              cutting_confirmed_at=datetime.now(timezone.utc), cutting_confirmed_by=md.id)
    bom.items = [BomItem(category=BomItemCategory.MAIN_MATERIAL.value, name="SHEEP NAPPA",
                         material_color="BLACK", uom="dm²", qty_per_garment=Decimal("10"),
                         unit_price=Decimal("1"), bulk_qty=Decimal("600"))]
    db.add(bom)
    await db.commit()

    await BomService(db).approve_bom(md, bom.id, lock=True)
    pts = ProductionTrackingService(db)
    board = await pts.board()
    assert board["count"] == 1
    row = board["trackers"][0]
    assert row["status"] == ProductionTrackingStatus.INVENTORY_CHECKED.value

    svc = PoService(db)
    out = await svc.generate_for_bom(md, bom.id)
    po_id = uuid.UUID(out["purchase_orders"][0]["id"])
    await svc.submit_po(md, po_id)
    await svc.approve_po(md, po_id)
    await svc.send_po(md, po_id)                          # → PO_RAISED
    board = await pts.board()
    assert board["trackers"][0]["status"] == ProductionTrackingStatus.PO_RAISED.value

    await svc.acknowledge_po(md, po_id, channel="email")  # all POs confirmed → MATERIAL_READY
    board = await pts.board()
    assert board["trackers"][0]["status"] == ProductionTrackingStatus.MATERIAL_READY.value
    assert board["trackers"][0]["po_confirmed_count"] == 1

    # MD manually releases to the shop floor (the human go/no-go)
    tracking_id = uuid.UUID(board["trackers"][0]["id"])
    res = await pts.transition(md, tracking_id,
                               ProductionTrackingStatus.RELEASED_TO_PRODUCTION.value)
    assert res["status"] == ProductionTrackingStatus.RELEASED_TO_PRODUCTION.value


# ════════════════════════════════════════════════════════════════════════════
# §11.11 — supplier CRUD: create, edit unblocks contact, soft-delete, reactivate
# ════════════════════════════════════════════════════════════════════════════
async def test_supplier_crud_and_soft_delete(db):
    md = await _user(db, UserRole.MANAGING_DIRECTOR, "9000000000")
    svc = SupplierService(db)

    created = await svc.create_supplier(md, {"name": "GATEWAY ENTERPRISES",
                                             "supplier_type": SupplierType.ACCESSORY.value,
                                             "gstin": "33ABCDE1234F1Z5"})
    sid = uuid.UUID(created["id"])
    assert created["state_code"] == "33"                 # derived from the GSTIN
    assert created["has_contact"] is False

    # a duplicate name is rejected
    with pytest.raises(Exception) as ei:
        await svc.create_supplier(md, {"name": "GATEWAY ENTERPRISES"})
    assert ei.value.status_code == 409

    # editing a contact onto a contactless vendor unblocks email send
    edited = await svc.update_supplier(md, sid, {"email": "gateway@x.com"})
    assert edited["email"] == "gateway@x.com"
    assert edited["email_status"] == SupplierEmailStatus.VALID.value

    # soft-delete hides it from the matcher but preserves the row
    await _history(db, await svc.repo.get_supplier(sid), "BUTTON", mode="MATERIALS")
    await svc.deactivate_supplier(md, sid)
    assert (await svc.repo.get_supplier(sid)).is_active is False
    listing = await svc.list_suppliers(active=True)
    assert all(s["id"] != str(sid) for s in listing["suppliers"])

    # reactivate restores it
    await svc.reactivate_supplier(md, sid)
    assert (await svc.repo.get_supplier(sid)).is_active is True

    # every mutation left a SUPPLIER_* audit row
    actions = set((await db.execute(
        select(AuditLog.action).where(AuditLog.entity_type == "supplier"))).scalars())
    assert {"SUPPLIER_CREATE", "SUPPLIER_EDIT", "SUPPLIER_DEACTIVATE",
            "SUPPLIER_REACTIVATE"} <= actions
