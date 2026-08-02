"""
gen_mock_data.py — deterministic mock data for the 2-Factor backend.
 
Rebuilt from the actual SQLAlchemy models in:
  users, employees, clients, bom, procurement, inventory, supplier_po,
  production, barcode, wages
 
Scenario: Beau Geste's CARNABY leather jacket (style 57 / Pine Green),
order ORD-1001, walking through intake -> BOM -> inventory check ->
supplier PO -> cutting/stitching production -> wages.
 
Every status/category/enum string below is copied from the real
`app/modules/<module>/enums.py` `.value`s (all lowercase snake_case for
VARCHAR-backed fields) or, where the column is a real SQLAlchemy `Enum`
(app_user.role, employee.wage_type, wage_run.status), the enum MEMBER
NAME (UPPERCASE), which is what SQLAlchemy persists by default.
 
NOTE: app/core/enums.py (UserRole, RunStatus, WageType, BarcodeType,
MaterialCategory, DrawerPart, DrawerState) was not included in the
uploaded modules, so those values are reconstructed from every usage
site found across the codebase (grepped) rather than read directly.
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta
 
NAMESPACE = uuid.UUID("12345678-1234-5678-1234-567812345678")
 
 
def uid(name: str) -> str:
    """Deterministic UUID so re-running the script yields stable ids."""
    return str(uuid.uuid5(NAMESPACE, name))
 
 
def iso(d, t="00:00:00"):
    return f"{d}T{t}Z"
 
 
today = date(2026, 7, 20)
data = {}
 
# ============================================================
# EMPLOYEES  (app/modules/employees/models.py)
# ============================================================
emp_raman = uid("employee.raman_kumar")
emp_suresh = uid("employee.suresh_babu")
emp_lakshmi = uid("employee.lakshmi_supervisor")
 
data["employees"] = {
    "employee": [
        {"id": emp_raman, "name": "Raman Kumar", "designation": "CUTTER",
         "wage_type": "PIECE_RATE", "monthly_salary": None, "is_active": True,
         "phone": "9000000011", "email": None},
        {"id": emp_suresh, "name": "Suresh Babu", "designation": "TAILOR",
         "wage_type": "PIECE_RATE", "monthly_salary": None, "is_active": True,
         "phone": "9000000012", "email": None},
        {"id": emp_lakshmi, "name": "Lakshmi Menon", "designation": "SUPERVISOR",
         "wage_type": "MONTHLY", "monthly_salary": 22000.00, "is_active": True,
         "phone": "9000000013", "email": None},
    ],
}
 
# ============================================================
# CLIENTS  (app/modules/clients/models.py) — Client -> ClientOrder -> Style -> SKU
# ============================================================
client_id = uid("client.beau_geste")
order_id = uid("client_order.ORD-1001")
style_id = uid("style.CARNABY")
sku_57_S = uid("sku.CARNABY.57.S")
sku_57_M = uid("sku.CARNABY.57.M")
sku_57_L = uid("sku.CARNABY.57.L")
 
data["clients"] = {
    "client": [
        {"id": client_id, "name": "Beau Geste", "country": "France", "code": "BG",
         "currency": "USD", "default_size_system": "LETTER", "brand": "Beau Geste",
         "label": "Beau Geste", "contact_email": "buying@beaugeste.test",
         "contact_phone": "+33-1-40-00-00-00", "address": "12 Rue de la Mode, Paris",
         "is_active": True},
    ],
    "client_order": [
        {"id": order_id, "client_id": client_id, "order_number": "ORD-1001",
         "order_date": str(today - timedelta(days=10)),
         "delivery_deadline": str(today + timedelta(days=60)),
         "sea_cutoff_date": str(today + timedelta(days=45)),
         "ship_mode": "sea", "currency": "USD", "agent": None, "line": None,
         "source_document_id": None},
    ],
    "style": [
        {"id": style_id, "client_order_id": order_id, "name": "CARNABY",
         "gender": "MENS", "label": "Carnaby Leather Jacket", "article": "CARNABY-J1",
         "thickness": "1.0-1.2mm", "code": "BEAUGESTE::CARNABY::2026AW",
         "season": "2026AW", "customer_ref": "CR1-02F5-PL02", "internal_ref": "A32073-44",
         "unit_price": 128.00, "currency": "USD", "base_style_id": None},
    ],
    "sku": [
        {"id": sku_57_S, "style_id": style_id, "color_code": "57", "color_name": "PINE GREEN",
         "size": "S", "qty_ordered": 90, "code": "BG-CARNABY-57-S",
         "nylon_color": None, "knit_color": None},
        {"id": sku_57_M, "style_id": style_id, "color_code": "57", "color_name": "PINE GREEN",
         "size": "M", "qty_ordered": 120, "code": "BG-CARNABY-57-M",
         "nylon_color": None, "knit_color": None},
        {"id": sku_57_L, "style_id": style_id, "color_code": "57", "color_name": "PINE GREEN",
         "size": "L", "qty_ordered": 90, "code": "BG-CARNABY-57-L",
         "nylon_color": None, "knit_color": None},
    ],
}
 
# ============================================================
# USERS  (app/modules/users/models.py) — role is a real Enum -> member NAME
# ============================================================
u_admin = uid("user.priya_md")            # Managing Director — final approver
u_manager = uid("user.rowan_dm")          # Direct Manager — runs day-to-day ops
u_cutting_mgr = uid("user.arjun_cutting")
u_stitching_mgr = uid("user.meera_stitch")
u_client_bg = uid("user.beaugeste_login")
u_emp_raman = uid("user.raman_login")
u_emp_suresh = uid("user.suresh_login")
 
data["users"] = {
    "app_user": [
        {"id": u_admin, "name": "Priya Sharma", "phone": "9000000001",
         "email": "priya@factory.test", "password_hash": "hash(9000000001)",
         "role": "MANAGING_DIRECTOR", "is_active": True, "must_change_password": False,
         "employee_id": None, "client_id": None},
        {"id": u_manager, "name": "Rowan Fernandes", "phone": "9000000002",
         "email": "rowan@factory.test", "password_hash": "hash(9000000002)",
         "role": "DIRECT_MANAGER", "is_active": True, "must_change_password": False,
         "employee_id": None, "client_id": None},
        {"id": u_cutting_mgr, "name": "Arjun Nair", "phone": "9000000003",
         "email": "arjun@factory.test", "password_hash": "hash(9000000003)",
         "role": "CUTTING_MANAGER", "is_active": True, "must_change_password": True,
         "employee_id": None, "client_id": None},
        {"id": u_stitching_mgr, "name": "Meera Iyer", "phone": "9000000004",
         "email": "meera@factory.test", "password_hash": "hash(9000000004)",
         "role": "STITCHING_MANAGER", "is_active": True, "must_change_password": True,
         "employee_id": None, "client_id": None},
        {"id": u_client_bg, "name": "Beau Geste Buyer", "phone": "3310000001",
         "email": "buyer@beaugeste.test", "password_hash": "hash(3310000001)",
         "role": "CLIENT", "is_active": True, "must_change_password": True,
         "employee_id": None, "client_id": client_id},
        {"id": u_emp_raman, "name": "Raman Kumar", "phone": "9000000011",
         "email": None, "password_hash": "hash(9000000011)",
         "role": "EMPLOYEE", "is_active": True, "must_change_password": True,
         "employee_id": emp_raman, "client_id": None},
        {"id": u_emp_suresh, "name": "Suresh Babu", "phone": "9000000012",
         "email": None, "password_hash": "hash(9000000012)",
         "role": "EMPLOYEE", "is_active": True, "must_change_password": True,
         "employee_id": emp_suresh, "client_id": None},
    ],
}
 
# ============================================================
# BOM  (app/modules/bom/models.py + enums.py)
# ============================================================
garment_type_id = uid("garment_type.JACKET")
bom_id = uid("bom.CARNABY")
bi_leather = uid("bom_item.leather")
bi_lining = uid("bom_item.lining")
bi_thread = uid("bom_item.thread")
bi_buttons = uid("bom_item.buttons")
bi_cutting = uid("bom_item.cutting_cost")
bi_stitching = uid("bom_item.stitching_cost")
bi_packaging = uid("bom_item.packaging")
 
data["bom"] = {
    "garment_type": [
        {"id": garment_type_id, "code": "JACKET", "label": "Leather Jacket",
         "required_poms": ["CHEST", "LENGTH", "SLEEVE"],
         "area_formula": {"method": "source3"}, "default_wastage_pct": 12.5},
    ],
    "bom": [
        {"id": bom_id, "submission_id": None,
         "style_signature": "BEAUGESTE::CARNABY::2026AW",
         "client_id": client_id, "client_order_id": order_id, "style_id": style_id,
         "order_identity": None, "status": "approved", "currency": "USD",
         "garment_fob_price": 128.00, "bulk_total": 300, "order_qty": 300, "revision": 1,
         "approved_by": u_admin, "approved_at": iso(today, "10:00:00"),
         "locked_at": iso(today, "10:00:00"), "rejected_by": None, "rejected_at": None,
         "rejection_reason": None, "export_document_id": None, "exported_at": None,
         "cutting_confirmed_by": None, "cutting_confirmed_at": None,
         "garment_type_id": garment_type_id, "dcm_base_size": "M",
         "source_document_id": None},
    ],
    "bom_item": [
        {"id": bi_leather, "bom_id": bom_id, "category": "main_material",
         "name": "Sheep Nappa Leather", "material_color": "PINE GREEN",
         "qty_per_garment": 34.5, "uom": "SQFT", "unit_price": 1.80,
         "bulk_qty": 10350, "total_cost": 18630.00, "annotation": "57 colourway",
         "source_ref": "BMO-1 L1", "dcm_source": "template", "dcm_confidence": 0.9500,
         "attribution_source": "lexicon", "attribution_confidence": 0.9800,
         "attribution_status": "confirmed"},
        {"id": bi_lining, "bom_id": bom_id, "category": "lining",
         "name": "Poly Twill Lining", "material_color": "BLACK",
         "qty_per_garment": 1.4, "uom": "MTR", "unit_price": 3.20,
         "bulk_qty": 420, "total_cost": 1344.00, "annotation": None,
         "source_ref": "BMO-1 L2", "dcm_source": "template", "dcm_confidence": 0.9000,
         "attribution_source": "lexicon", "attribution_confidence": 0.9500,
         "attribution_status": "confirmed"},
        {"id": bi_thread, "bom_id": bom_id, "category": "thread",
         "name": "Polyester Thread 40/2", "material_color": "BLACK",
         "qty_per_garment": 0.08, "uom": "CONE", "unit_price": 2.10,
         "bulk_qty": 24, "total_cost": 50.40, "annotation": None,
         "source_ref": "BMO-1 L3", "dcm_source": "similar_style", "dcm_confidence": 0.8000,
         "attribution_source": "lexicon", "attribution_confidence": 0.9000,
         "attribution_status": "confirmed"},
        {"id": bi_buttons, "bom_id": bom_id, "category": "accessory",
         "name": "Metal Snap Button 15mm", "material_color": "GUNMETAL",
         "qty_per_garment": 5, "uom": "PCS", "unit_price": 0.12,
         "bulk_qty": 1500, "total_cost": 180.00,
         "annotation": "TBC pending sample approval", "source_ref": "BMO-1 L4",
         "dcm_source": "manual", "dcm_confidence": None,
         "attribution_source": "ai_estimate", "attribution_confidence": 0.7000,
         "attribution_status": "estimated"},
        {"id": bi_cutting, "bom_id": bom_id, "category": "manufacturing",
         "name": "Cutting Cost", "material_color": None, "qty_per_garment": 1,
         "uom": "PCS", "unit_price": 2.50, "bulk_qty": 300, "total_cost": 750.00,
         "annotation": None, "source_ref": "BMO-1 L5", "dcm_source": None,
         "dcm_confidence": None, "attribution_source": None,
         "attribution_confidence": None, "attribution_status": "confirmed"},
        {"id": bi_stitching, "bom_id": bom_id, "category": "manufacturing",
         "name": "Stitching Cost", "material_color": None, "qty_per_garment": 1,
         "uom": "PCS", "unit_price": 6.00, "bulk_qty": 300, "total_cost": 1800.00,
         "annotation": None, "source_ref": "BMO-1 L6", "dcm_source": None,
         "dcm_confidence": None, "attribution_source": None,
         "attribution_confidence": None, "attribution_status": "confirmed"},
        {"id": bi_packaging, "bom_id": bom_id, "category": "packaging",
         "name": "Poly Bag + Carton Share", "material_color": None, "qty_per_garment": 1,
         "uom": "PCS", "unit_price": 0.90, "bulk_qty": 300, "total_cost": 270.00,
         "annotation": None, "source_ref": "BMO-1 L7", "dcm_source": None,
         "dcm_confidence": None, "attribution_source": None,
         "attribution_confidence": None, "attribution_status": "confirmed"},
    ],
}
 
# ============================================================
# PROCUREMENT  (app/modules/procurement/models.py + enums.py)
# ============================================================
submission_id = uid("submission.ORD-1001")
 
data["procurement"] = {
    "submission": [
        {"id": submission_id, "client_id": client_id, "created_by": u_manager,
         "status": "consumed",  # both docs paired + Stage-2 (BOM) already picked it up
         "order_document_id": None, "spec_document_id": None,
         "client_order_id": order_id},
    ],
    "client_template": [
        {"id": uid("client_template.BG.order_sheet"), "client_code": "BG",
         "display_name": "Beau Geste", "doc_kind": "ORDER_SHEET", "language": "en",
         "size_system": "LETTER", "currency": "USD", "expected_layout": "GRID",
         "spec_type_hint": "MEASUREMENT_GRID", "accepted_mime": ["xlsx", "pdf"],
         "anchors": None, "fingerprints": None, "grid_signals": None,
         "thresholds": None, "is_active": True},
    ],
}
 
# ============================================================
# INVENTORY  (app/modules/inventory/models.py + enums.py)
# ============================================================
inv_leather = uid("inventory_item.sheep_nappa_pine_green")
inv_lining = uid("inventory_item.poly_twill_black")
inv_thread = uid("inventory_item.thread_black")
inv_buttons = uid("inventory_item.snap_button_gunmetal")
 
inv_check_id = uid("inventory_check.CARNABY")
icl_leather = uid("icl.leather")
icl_lining = uid("icl.lining")
icl_thread = uid("icl.thread")
icl_buttons = uid("icl.buttons")
 
data["inventory"] = {
    "inventory_item": [
        {"id": inv_leather, "description": "Sheep Nappa Leather - Pine Green",
         "normalized_key": "sheep nappa leather pine green", "uom": "SQFT",
         "qty_on_hand": 6000.000, "rate": 1.75, "category": "LEATHER",
         "color": "PINE GREEN", "article_ref": "SNP-57", "is_active": True},
        {"id": inv_lining, "description": "Poly Twill Lining - Black",
         "normalized_key": "poly twill lining black", "uom": "MTR",
         "qty_on_hand": 500.000, "rate": 3.00, "category": "LINING", "color": "BLACK",
         "article_ref": "PTL-BLK", "is_active": True},
        {"id": inv_thread, "description": "Polyester Thread 40/2 - Black",
         "normalized_key": "polyester thread 40 2 black", "uom": "CONE",
         "qty_on_hand": 40.000, "rate": 2.00, "category": "TRIM", "color": "BLACK",
         "article_ref": "THR-402-BLK", "is_active": True},
        {"id": inv_buttons, "description": "Metal Snap Button 15mm - Gunmetal",
         "normalized_key": "metal snap button 15mm gunmetal", "uom": "PCS",
         "qty_on_hand": 900.000, "rate": 0.11, "category": "TRIM", "color": "GUNMETAL",
         "article_ref": "BTN-15-GM", "is_active": True},
    ],
    "inventory_check": [
        {"id": inv_check_id, "bom_id": bom_id, "status": "complete",
         "run_at": iso(today, "11:00:00"), "run_by": u_manager},
    ],
    "inventory_check_line": [
        {"id": icl_leather, "inventory_check_id": inv_check_id, "bom_item_id": bi_leather,
         "inventory_item_id": inv_leather, "required_qty": 10350.000, "on_hand_qty": 6000.000,
         "shortfall_qty": 4350.000, "status": "partial", "matched_method": "key",
         "flags": None},
        {"id": icl_lining, "inventory_check_id": inv_check_id, "bom_item_id": bi_lining,
         "inventory_item_id": inv_lining, "required_qty": 420.000, "on_hand_qty": 500.000,
         "shortfall_qty": 0.000, "status": "sufficient", "matched_method": "key",
         "flags": None},
        {"id": icl_thread, "inventory_check_id": inv_check_id, "bom_item_id": bi_thread,
         "inventory_item_id": inv_thread, "required_qty": 24.000, "on_hand_qty": 40.000,
         "shortfall_qty": 0.000, "status": "sufficient", "matched_method": "key",
         "flags": None},
        {"id": icl_buttons, "inventory_check_id": inv_check_id, "bom_item_id": bi_buttons,
         "inventory_item_id": inv_buttons, "required_qty": 1500.000, "on_hand_qty": 900.000,
         "shortfall_qty": 600.000, "status": "partial", "matched_method": "key",
         "flags": None},
    ],
    "inventory_reservation": [
        {"id": uid("ir.lining"), "inventory_item_id": inv_lining, "bom_id": bom_id,
         "inventory_check_line_id": icl_lining, "qty": 420.000, "status": "active",
         "released_at": None, "released_reason": None},
        {"id": uid("ir.thread"), "inventory_item_id": inv_thread, "bom_id": bom_id,
         "inventory_check_line_id": icl_thread, "qty": 24.000, "status": "active",
         "released_at": None, "released_reason": None},
    ],
    "material_alias": [
        {"id": uid("alias.1"), "bom_term": "sheep nappa leather",
         "inventory_key": "sheep nappa leather pine green", "is_active": True},
    ],
    "uom_conversion": [
        {"id": uid("uom.1"), "from_uom": "SQFT", "to_uom": "SQFT", "factor": 1.0},
        {"id": uid("uom.2"), "from_uom": "MTR", "to_uom": "MTR", "factor": 1.0},
    ],
}
 
# ============================================================
# SUPPLIER_PO  (app/modules/supplier_po/models.py + enums.py)
# ============================================================
supplier_leather_id = uid("supplier.hidecraft")
supplier_trims_id = uid("supplier.metalfast")
po_leather_id = uid("po.leather.CARNABY")
po_buttons_id = uid("po.buttons.CARNABY")
por_buttons_id = uid("por.buttons")
 
# Leather PO: 4350 sqft * 1.78 = 7743.00 subtotal; 9% CGST + 9% SGST (intra-state)
leather_subtotal = 4350.000 * 1.78
leather_gst = round(leather_subtotal * 0.09, 2)
leather_total = round(leather_subtotal + 2 * leather_gst, 2)
 
# Buttons PO: 600 pcs * 0.12 = 72.00 subtotal
buttons_subtotal = 600.000 * 0.12
buttons_gst = round(buttons_subtotal * 0.09, 2)
buttons_total = round(buttons_subtotal + 2 * buttons_gst, 2)
 
data["supplier_po"] = {
    "supplier": [
        {"id": supplier_leather_id, "name": "Hidecraft Tanners Pvt Ltd",
         "phone": "+91-98765-00011", "email": "sales@hidecrafttanners.test",
         "service": "Leather supply", "gstin": "27ABCDE1234F1Z5",
         "address": "Plot 12, Tannery Rd, Chennai", "currency": "INR",
         "payment_terms_days": 60, "lead_time_days": 12, "is_active": True,
         "email_status": "valid", "supplier_type": "leather", "state_code": "27",
         "whatsapp_phone": "+91-98765-00011"},
        {"id": supplier_trims_id, "name": "Metalfast Trims Co",
         "phone": "+91-98765-00022", "email": "orders@metalfasttrims.test",
         "service": "Buttons & hardware", "gstin": "33ABCDE5678F1Z2",
         "address": "SIDCO Estate, Coimbatore", "currency": "INR",
         "payment_terms_days": 45, "lead_time_days": 7, "is_active": True,
         "email_status": "valid", "supplier_type": "accessory", "state_code": "33",
         "whatsapp_phone": "+91-98765-00022"},
    ],
    "supplier_supply_history": [
        {"id": uid("ssh.1"), "supplier_id": supplier_leather_id,
         "normalized_description": "sheep nappa leather pine green",
         "raw_description": "Sheep Nappa - Pine Green", "mode": "PURCHASE", "uom": "SQFT",
         "txn_count": 14, "first_purchased_at": "2024-02-10",
         "last_purchased_at": "2026-05-20", "last_rate": 1.78, "min_rate": 1.65,
         "max_rate": 1.85},
    ],
    "purchase_order": [
        {"id": po_leather_id, "po_number": "PO-07(25-26)", "supplier_id": supplier_leather_id,
         "bom_id": bom_id, "client_order_id": order_id, "buyer_ref": "ORD-1001",
         "issue_date": str(today + timedelta(days=1)), "delivery_days": 12,
         "payment_terms_days": 60, "currency": "INR", "subtotal": round(leather_subtotal, 2),
         "cgst": leather_gst, "sgst": leather_gst, "igst": None, "gst_mode": "INTRA",
         "round_off": 0.0, "total": leather_total, "status": "sent",
         "needs_supplier": False, "no_contact_channel": False, "match_method": "key",
         "candidates": None, "revision": 1, "created_by": u_manager, "approved_by": u_admin,
         "approved_at": iso(today + timedelta(days=1), "09:00:00"), "rejected_by": None,
         "rejected_at": None, "rejection_reason": None,
         "sent_at": iso(today + timedelta(days=1), "09:30:00"), "pdf_document_id": None,
         "tracking_token": uid("tracking.po_leather")[:16], "first_opened_at": None,
         "first_clicked_at": None, "current_rung": 1,
         "next_escalation_at": iso(today + timedelta(days=4), "09:30:00"),
         "acknowledged_at": None, "acknowledged_channel": None},
        {"id": po_buttons_id, "po_number": "PO-08(25-26)", "supplier_id": supplier_trims_id,
         "bom_id": bom_id, "client_order_id": order_id, "buyer_ref": "ORD-1001",
         "issue_date": str(today + timedelta(days=1)), "delivery_days": 7,
         "payment_terms_days": 45, "currency": "INR", "subtotal": round(buttons_subtotal, 2),
         "cgst": buttons_gst, "sgst": buttons_gst, "igst": None, "gst_mode": "INTRA",
         "round_off": 0.0, "total": buttons_total, "status": "confirmed",
         "needs_supplier": False, "no_contact_channel": False, "match_method": "key",
         "candidates": None, "revision": 1, "created_by": u_manager, "approved_by": u_admin,
         "approved_at": iso(today + timedelta(days=1), "09:05:00"), "rejected_by": None,
         "rejected_at": None, "rejection_reason": None,
         "sent_at": iso(today + timedelta(days=1), "09:35:00"), "pdf_document_id": None,
         "tracking_token": uid("tracking.po_buttons")[:16],
         "first_opened_at": iso(today + timedelta(days=2), "13:50:00"),
         "first_clicked_at": iso(today + timedelta(days=2), "13:52:00"), "current_rung": 0,
         "next_escalation_at": None,
         "acknowledged_at": iso(today + timedelta(days=2), "14:00:00"),
         "acknowledged_channel": "email"},
    ],
    "po_item": [
        {"id": uid("poi.leather"), "purchase_order_id": po_leather_id, "item_no": 1,
         "description": "Sheep Nappa Leather - Pine Green", "color": "PINE GREEN",
         "uom": "SQFT", "qty": 4350.000, "unit_price": 1.78,
         "amount": round(leather_subtotal, 2), "inventory_item_id": inv_leather,
         "bom_item_id": bi_leather},
        {"id": uid("poi.buttons"), "purchase_order_id": po_buttons_id, "item_no": 1,
         "description": "Metal Snap Button 15mm - Gunmetal", "color": "GUNMETAL",
         "uom": "PCS", "qty": 600.000, "unit_price": 0.12,
         "amount": round(buttons_subtotal, 2), "inventory_item_id": inv_buttons,
         "bom_item_id": bi_buttons},
    ],
    "po_response": [
        {"id": por_buttons_id, "purchase_order_id": po_buttons_id, "channel": "email",
         "sent_at": iso(today + timedelta(days=1), "09:35:00"),
         "responded_at": iso(today + timedelta(days=2), "14:00:00"),
         "confirmed_qty": 600.000, "status": "confirmed",
         "notes": "Confirmed by return email, ships within 7 days.",
         "tracking_token": uid("tracking.po_buttons")[:16], "message_id": "msg-po-buttons-001"},
    ],
    "po_tracking_event": [
        {"id": uid("pte.1"), "purchase_order_id": po_leather_id, "po_response_id": None,
         "tracking_token": uid("tracking.po_leather")[:16], "event_type": "delivery",
         "channel": "email", "ip": None, "user_agent": None, "meta": None,
         "at": iso(today + timedelta(days=1), "09:31:00")},
        {"id": uid("pte.2"), "purchase_order_id": po_buttons_id, "po_response_id": None,
         "tracking_token": uid("tracking.po_buttons")[:16], "event_type": "open",
         "channel": "email", "ip": "103.21.244.10", "user_agent": "Mozilla/5.0",
         "meta": None, "at": iso(today + timedelta(days=2), "13:50:00")},
        {"id": uid("pte.3"), "purchase_order_id": po_buttons_id, "po_response_id": por_buttons_id,
         "tracking_token": uid("tracking.po_buttons")[:16], "event_type": "click",
         "channel": "email", "ip": "103.21.244.10", "user_agent": "Mozilla/5.0",
         "meta": None, "at": iso(today + timedelta(days=2), "13:52:00")},
    ],
    "production_tracking": [
        {"id": uid("pt.CARNABY"), "client_order_id": order_id, "style_id": style_id,
         "bom_id": bom_id, "status": "po_raised", "po_count": 2, "po_confirmed_count": 1,
         "material_ready_at": None, "released_at": None, "updated_by": u_manager},
    ],
}
 
# ============================================================
# PRODUCTION  (app/modules/production/models.py)
# ============================================================
op_cutting = uid("operation.CUTTING")
op_fusing = uid("operation.FUSING")
op_stitching = uid("operation.STITCHING")
op_packing = uid("operation.PACKING")
 
piece_1 = uid("piece.57M.1")
piece_2 = uid("piece.57M.2")
piece_3 = uid("piece.57M.3")
 
drawer_1 = uid("drawer.DRW-014")
lot_leather = uid("material_lot.pine_green_batch1")
lot_lining = uid("material_lot.poly_twill_black_batch1")
 
data["production"] = {
    "operation": [
        {"id": op_cutting, "code": "CUTTING", "label": "Cutting", "sequence": 1, "is_active": True},
        {"id": op_fusing, "code": "FUSING", "label": "Fusing", "sequence": 2, "is_active": True},
        {"id": op_stitching, "code": "STITCHING", "label": "Stitching", "sequence": 3, "is_active": True},
        {"id": op_packing, "code": "PACKING", "label": "Packing", "sequence": 4, "is_active": True},
    ],
    "operation_access": [
        {"id": uid("oa.1"), "role": "CUTTING_MANAGER", "operation_id": op_cutting},
        {"id": uid("oa.2"), "role": "STITCHING_MANAGER", "operation_id": op_stitching},
        {"id": uid("oa.3"), "role": "STITCHING_MANAGER", "operation_id": op_fusing},
        {"id": uid("oa.4"), "role": "DIRECT_MANAGER", "operation_id": op_packing},
    ],
    "style_operation": [
        {"id": uid("so.1"), "style_id": style_id, "operation_id": op_cutting},
        {"id": uid("so.2"), "style_id": style_id, "operation_id": op_fusing},
        {"id": uid("so.3"), "style_id": style_id, "operation_id": op_stitching},
        {"id": uid("so.4"), "style_id": style_id, "operation_id": op_packing},
    ],
    "piece": [
        {"id": piece_1, "code": "BG-CARNABY-57-M-1", "seq": 1, "sku_id": sku_57_M,
         "current_operation_id": op_stitching, "is_active": True, "needs_lining": True,
         "drawer_id": drawer_1},
        {"id": piece_2, "code": "BG-CARNABY-57-M-2", "seq": 2, "sku_id": sku_57_M,
         "current_operation_id": op_cutting, "is_active": True, "needs_lining": True,
         "drawer_id": None},
        {"id": piece_3, "code": "BG-CARNABY-57-M-3", "seq": 3, "sku_id": sku_57_M,
         "current_operation_id": op_cutting, "is_active": True, "needs_lining": True,
         "drawer_id": None},
    ],
    "production_event": [
        {"id": uid("pe.1"), "sku_id": sku_57_M, "operation_id": op_cutting,
         "employee_id": emp_raman, "work_date": str(today + timedelta(days=3)), "qty": 1,
         "entered_by": "Rowan Fernandes", "piece_id": piece_1,
         "leather_lot_id": lot_leather, "lining_lot_id": lot_lining, "consumption_qty": 34.5},
        {"id": uid("pe.2"), "sku_id": sku_57_M, "operation_id": op_fusing,
         "employee_id": emp_suresh, "work_date": str(today + timedelta(days=4)), "qty": 1,
         "entered_by": "Rowan Fernandes", "piece_id": piece_1,
         "leather_lot_id": None, "lining_lot_id": None, "consumption_qty": None},
        {"id": uid("pe.3"), "sku_id": sku_57_M, "operation_id": op_stitching,
         "employee_id": emp_suresh, "work_date": str(today + timedelta(days=5)), "qty": 1,
         "entered_by": "Rowan Fernandes", "piece_id": piece_1,
         "leather_lot_id": None, "lining_lot_id": None, "consumption_qty": None},
        {"id": uid("pe.4"), "sku_id": sku_57_M, "operation_id": op_cutting,
         "employee_id": emp_raman, "work_date": str(today + timedelta(days=3)), "qty": 1,
         "entered_by": "Rowan Fernandes", "piece_id": piece_2,
         "leather_lot_id": lot_leather, "lining_lot_id": lot_lining, "consumption_qty": 34.5},
        {"id": uid("pe.5"), "sku_id": sku_57_M, "operation_id": op_cutting,
         "employee_id": emp_raman, "work_date": str(today + timedelta(days=3)), "qty": 1,
         "entered_by": "Rowan Fernandes", "piece_id": piece_3,
         "leather_lot_id": lot_leather, "lining_lot_id": lot_lining, "consumption_qty": 34.5},
    ],
}
 
# ============================================================
# BARCODE  (app/modules/barcode/models.py) — drawer/material_lot live HERE
# ============================================================
mat_supplier_id = uid("material_supplier.hidecraft_local")
supplier_order_id = uid("supplier_order.leather_topup")
 
data["barcode"] = {
    "material_supplier": [
        {"id": mat_supplier_id, "name": "Hidecraft Tanners (local store)",
         "articles": "SNP-57,SNP-J24", "contact": "+91-98765-00011", "is_active": True},
    ],
    "material_lot": [
        {"id": lot_leather, "category": "LEATHER", "subtype": "NAPPA", "article": "SNP-57",
         "colour": "PINE GREEN", "thickness": "1.0-1.2mm", "size": None, "uom": "SQFT",
         "on_hand": 6000.000, "supplier_id": mat_supplier_id,
         "attributes": {"dcm": 34.5, "thickness": "1.0-1.2mm"}, "is_active": True},
        {"id": lot_lining, "category": "LINING", "subtype": "TWILL", "article": "PTL-BLK",
         "colour": "BLACK", "thickness": None, "size": None, "uom": "MTR",
         "on_hand": 500.000, "supplier_id": mat_supplier_id, "attributes": {},
         "is_active": True},
    ],
    "material_reservation": [
        {"id": uid("mr.leather"), "material_lot_id": lot_leather, "qty": 103.500,
         "status": "active", "reason": "CARNABY cutting - 3 pieces", "released_at": None},
    ],
    "material_receipt": [
        {"id": uid("mrec.1"), "material_lot_id": lot_leather,
         "supplier_order_id": supplier_order_id, "approved_qty": 4350.000,
         "rejected_qty": 15.000, "received_by": u_manager},
    ],
    "supplier_order": [
        {"id": supplier_order_id, "category": "LEATHER", "article": "SNP-57",
         "colour": "PINE GREEN", "qty": 4350.000, "uom": "SQFT", "status": "arrived",
         "supplier_id": mat_supplier_id, "ordered_by": u_manager,
         "arrived_at": iso(today + timedelta(days=13), "10:00:00")},
    ],
    # Drawer.state is a DrawerState value: waiting/holding_leather/holding_lining/
    # holding_both/merged/received/sended. Drawer 14 has both parts in -> "merged".
    "drawer": [
        {"id": drawer_1, "code": "DRW-014", "seq": 14, "state": "merged",
         "current_piece_id": piece_1, "leather_in": True, "lining_in": True,
         "received_at": iso(today + timedelta(days=3), "08:00:00"), "sended_at": None},
    ],
    "barcode_registry": [
        {"id": uid("bc.piece1"), "code": "BG-CARNABY-57-M-1", "type": "PIECE",
         "status": "active", "piece_id": piece_1, "employee_id": None, "drawer_id": None,
         "material_lot_id": None, "caption": "CARNABY \u00b7 PINE GREEN \u00b7 M \u00b7 #1",
         "retired_at": None, "retired_reason": None},
        {"id": uid("bc.piece2"), "code": "BG-CARNABY-57-M-2", "type": "PIECE",
         "status": "active", "piece_id": piece_2, "employee_id": None, "drawer_id": None,
         "material_lot_id": None, "caption": "CARNABY \u00b7 PINE GREEN \u00b7 M \u00b7 #2",
         "retired_at": None, "retired_reason": None},
        {"id": uid("bc.piece3"), "code": "BG-CARNABY-57-M-3", "type": "PIECE",
         "status": "active", "piece_id": piece_3, "employee_id": None, "drawer_id": None,
         "material_lot_id": None, "caption": "CARNABY \u00b7 PINE GREEN \u00b7 M \u00b7 #3",
         "retired_at": None, "retired_reason": None},
        {"id": uid("bc.emp.raman"), "code": "EMP-0011", "type": "EMPLOYEE",
         "status": "active", "piece_id": None, "employee_id": emp_raman, "drawer_id": None,
         "material_lot_id": None, "caption": "Raman Kumar - CUTTER",
         "retired_at": None, "retired_reason": None},
        {"id": uid("bc.emp.suresh"), "code": "EMP-0012", "type": "EMPLOYEE",
         "status": "active", "piece_id": None, "employee_id": emp_suresh, "drawer_id": None,
         "material_lot_id": None, "caption": "Suresh Babu - TAILOR",
         "retired_at": None, "retired_reason": None},
        {"id": uid("bc.drawer1"), "code": "DRW-014", "type": "DRAWER", "status": "active",
         "piece_id": None, "employee_id": None, "drawer_id": drawer_1,
         "material_lot_id": None, "caption": "Drawer 14",
         "retired_at": None, "retired_reason": None},
        {"id": uid("bc.lot.leather"), "code": "LOT-SNP57-B1", "type": "LEATHER_LOT",
         "status": "active", "piece_id": None, "employee_id": None, "drawer_id": None,
         "material_lot_id": lot_leather,
         "caption": "Sheep Nappa Pine Green - Batch 1",
         "retired_at": None, "retired_reason": None},
        {"id": uid("bc.lot.lining"), "code": "LOT-PTLBLK-B1", "type": "LINING_LOT",
         "status": "active", "piece_id": None, "employee_id": None, "drawer_id": None,
         "material_lot_id": lot_lining,
         "caption": "Poly Twill Black - Batch 1",
         "retired_at": None, "retired_reason": None},
    ],
}
 
# ============================================================
# WAGES  (app/modules/wages/models.py)
# ============================================================
rate_cutting = uid("rate.CARNABY.CUTTING")
rate_fusing = uid("rate.CARNABY.FUSING")
rate_stitching = uid("rate.CARNABY.STITCHING")
wage_run_id = uid("wage_run.2026-07")
 
data["wages"] = {
    "rate": [
        {"id": rate_cutting, "style_id": style_id, "operation_id": op_cutting,
         "rate": 45.00, "effective_from": str(today)},
        {"id": rate_fusing, "style_id": style_id, "operation_id": op_fusing,
         "rate": 30.00, "effective_from": str(today)},
        {"id": rate_stitching, "style_id": style_id, "operation_id": op_stitching,
         "rate": 120.00, "effective_from": str(today)},
    ],
    # WageRun.status is a real Enum(RunStatus) column -> stores member NAME.
    "wage_run": [
        {"id": wage_run_id, "period_start": str(today),
         "period_end": str(today + timedelta(days=6)), "status": "CLOSED",
         "recompute_count": 0, "last_recomputed_at": None, "last_recomputed_by": None},
    ],
    "wage_line_detail": [
        {"id": uid("wld.raman.cutting"), "wage_run_id": wage_run_id, "employee_id": emp_raman,
         "style_id": style_id, "operation_id": op_cutting, "pieces": 3, "rate": 45.00,
         "amount": 135.00},
        {"id": uid("wld.suresh.fusing"), "wage_run_id": wage_run_id, "employee_id": emp_suresh,
         "style_id": style_id, "operation_id": op_fusing, "pieces": 1, "rate": 30.00,
         "amount": 30.00},
        {"id": uid("wld.suresh.stitching"), "wage_run_id": wage_run_id, "employee_id": emp_suresh,
         "style_id": style_id, "operation_id": op_stitching, "pieces": 1, "rate": 120.00,
         "amount": 120.00},
    ],
    # WageLine.wage_type is String(20) snapshot of the employee's WageType MEMBER NAME.
    "wage_line": [
        {"id": uid("wl.raman"), "wage_run_id": wage_run_id, "employee_id": emp_raman,
         "wage_type": "PIECE_RATE", "pieces": 3, "amount": 135.00},
        {"id": uid("wl.suresh"), "wage_run_id": wage_run_id, "employee_id": emp_suresh,
         "wage_type": "PIECE_RATE", "pieces": 2, "amount": 150.00},
        {"id": uid("wl.lakshmi"), "wage_run_id": wage_run_id, "employee_id": emp_lakshmi,
         "wage_type": "MONTHLY", "pieces": 0, "amount": 5077.00},
    ],
}
 
# ============================================================
# WRITE OUTPUT
# ============================================================
os.makedirs("/mnt/user-data/outputs", exist_ok=True)
with open("/mnt/user-data/outputs/mock_data.json", "w") as f:
    json.dump(data, f, indent=2, default=str)
 
summary = {mod: {t: len(rows) for t, rows in tables.items()} for mod, tables in data.items()}
print(json.dumps(summary, indent=2))
print("TOTAL ROWS:", sum(len(rows) for tables in data.values() for rows in tables.values()))
