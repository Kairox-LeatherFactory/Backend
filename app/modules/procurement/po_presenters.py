"""
================================================================================
modules/procurement/po_presenters.py — Stage-5 response shapes (§1–§9)
================================================================================

PURE serialization (ORM rows → the JSON the FE renders). Split out of the services
so they stay focused on orchestration. No DB, no business rules.
================================================================================
"""
from __future__ import annotations


def _f(v):
    return float(v) if v is not None else None


def _iso(dt):
    return dt.isoformat() if dt else None


# ── supplier (§9) ─────────────────────────────────────────────────────────────
def supplier_block(s, *, history=None, open_pos=None) -> dict:
    out = {
        "id": str(s.id), "name": s.name, "phone": s.phone, "email": s.email,
        "service": s.service, "gstin": s.gstin, "address": s.address,
        "currency": s.currency, "payment_terms_days": s.payment_terms_days,
        "lead_time_days": s.lead_time_days, "is_active": s.is_active,
        "email_status": s.email_status, "supplier_type": s.supplier_type,
        "state_code": s.state_code, "whatsapp_phone": s.whatsapp_phone,
        "has_contact": bool(s.email or s.phone),
    }
    if history is not None:
        out["supply_history"] = [{
            "normalized_description": h.normalized_description,
            "raw_description": h.raw_description, "mode": h.mode, "uom": h.uom,
            "txn_count": h.txn_count, "last_purchased_at": _iso(h.last_purchased_at),
            "last_rate": _f(h.last_rate), "min_rate": _f(h.min_rate),
            "max_rate": _f(h.max_rate),
        } for h in history]
    if open_pos is not None:
        out["open_pos"] = [{"id": str(p.id), "po_number": p.po_number,
                            "status": p.status, "total": _f(p.total)} for p in open_pos]
    return out


# ── purchase order (§1–§7) ────────────────────────────────────────────────────
def po_item_block(i) -> dict:
    return {
        "id": str(i.id), "item_no": i.item_no, "description": i.description,
        "color": i.color, "uom": i.uom, "qty": _f(i.qty),
        "unit_price": _f(i.unit_price), "amount": _f(i.amount),
        "inventory_item_id": str(i.inventory_item_id) if i.inventory_item_id else None,
        "bom_item_id": str(i.bom_item_id) if i.bom_item_id else None,
    }


def po_view(po, *, include_supplier=True) -> dict:
    sup = getattr(po, "supplier", None)
    out = {
        "id": str(po.id), "po_number": po.po_number, "status": po.status,
        "revision": po.revision,
        "supplier_id": str(po.supplier_id) if po.supplier_id else None,
        "bom_id": str(po.bom_id) if po.bom_id else None,
        "client_order_id": str(po.client_order_id) if po.client_order_id else None,
        "buyer_ref": po.buyer_ref, "issue_date": str(po.issue_date) if po.issue_date else None,
        "delivery_days": po.delivery_days, "payment_terms_days": po.payment_terms_days,
        "currency": po.currency, "gst_mode": po.gst_mode,
        "subtotal": _f(po.subtotal), "cgst": _f(po.cgst), "sgst": _f(po.sgst),
        "igst": _f(po.igst), "round_off": _f(po.round_off), "total": _f(po.total),
        "needs_supplier": po.needs_supplier, "no_contact_channel": po.no_contact_channel,
        "match_method": po.match_method, "candidates": po.candidates,
        "approved_at": _iso(po.approved_at), "rejected_at": _iso(po.rejected_at),
        "rejection_reason": po.rejection_reason, "sent_at": _iso(po.sent_at),
        "pdf_document_id": str(po.pdf_document_id) if po.pdf_document_id else None,
        "first_opened_at": _iso(po.first_opened_at),
        "first_clicked_at": _iso(po.first_clicked_at),
        "current_rung": po.current_rung, "next_escalation_at": _iso(po.next_escalation_at),
        "acknowledged_at": _iso(po.acknowledged_at),
        "acknowledged_channel": po.acknowledged_channel,
        "items": [po_item_block(i) for i in getattr(po, "items", [])],
    }
    if include_supplier and sup is not None:
        out["supplier"] = {"id": str(sup.id), "name": sup.name, "email": sup.email,
                           "phone": sup.phone, "gstin": sup.gstin, "address": sup.address,
                           "supplier_type": sup.supplier_type, "email_status": sup.email_status}
    return out


def po_list_block(pos: list) -> dict:
    return {"purchase_orders": [po_view(p) for p in pos], "count": len(pos)}


# ── production tracking (§8) ──────────────────────────────────────────────────
def tracking_view(t, *, order=None, style=None, client_name=None) -> dict:
    return {
        "id": str(t.id),
        "client_order_id": str(t.client_order_id),
        "order_number": getattr(order, "order_number", None),
        "client_name": client_name,
        "style_id": str(t.style_id), "style_name": getattr(style, "name", None),
        "bom_id": str(t.bom_id) if t.bom_id else None,
        "status": t.status, "po_count": t.po_count,
        "po_confirmed_count": t.po_confirmed_count,
        "material_ready_at": _iso(t.material_ready_at),
        "released_at": _iso(t.released_at),
    }
