"""
================================================================================
scripts/docgen/responses.py — hand-written shapes for the routes that declare none
================================================================================
WHY THIS FILE EXISTS

    Most routes here declare a `response_model`, so FastAPI publishes the exact
    response schema and the API-reference document is generated from it. About
    forty per cent do not: they return a plain `dict` the service builds. For
    those, OpenAPI publishes `{}` — literally nothing — and a reference that
    says "no body" is WRONG, because there very much is a body and the frontend
    has to render it.

    So the shapes below are read out of the service code and written down here.
    They are the one hand-maintained part of the reference, and the documents
    label them as such so nobody mistakes them for machine-checked truth.

    The real fix is a `response_model` on the route. Every entry that disappears
    from this file because someone added one is a win — the generator will then
    take the shape from the app instead.

FORMAT
    "METHOD /api/v1/path/{param}": {
        "note":    one sentence about what comes back,
        "fields":  [[field path, type, meaning], ...],   # optional
        "example": <the actual JSON, as Python>,
    }

    Use the same dotted/`[]` field paths the generator uses elsewhere
    (`rows[].piece_code`) so both kinds of table read the same.
================================================================================
"""
from __future__ import annotations

_PREVIEW_SUMMARY = {
    "clients": {
        "BOGGI": {
            "order_lines": 12,
            "pieces_ordered": 840,
            "styles": ["CLERMONT", "CLERMONT + VEST"],
            "by_style": {
                "CLERMONT": {
                    "sizes": {"46": 40, "48": 53, "50": 52, "52": 7},
                    "pieces_ordered": 152,
                    "unit_price": 148.5,
                    "currency": "EUR",
                    "delivery_date": "2026-11-30",
                    "size_confidence": "HIGH",
                },
            },
            "warnings": [
                "[BOGGI-GARMENT ORDER] row 41: size band could not be proved "
                "against the printed total",
            ],
        },
    },
    "sheets": [
        {"sheet": "BOGGI-GARMENT ORDER", "type": "ORDER", "client": "BOGGI"},
        {"sheet": "COSTING", "type": "UNKNOWN", "client": "COSTING"},
    ],
    "total_warnings": 1,
}

_STOCK_BLOCK = {
    "arrived": 1200.0, "used": 348.0, "balance": 852.0,
    "reserved": 120.0, "available": 732.0, "on_hand": 852.0,
}

_SPEC_LINE = {
    "line_id": "c1a20033-0000-4a11-9f00-000000000001",
    "scope": "STYLE",
    "sku_id": None,
    "category": "LEATHER", "subtype": None,
    "article": "GOAT SUEDE", "colour": "DARK BROWN",
    "thickness": "0.8-1.0", "size": None,
    "garment_size": None,
    "qty_per_piece": 14.5, "uom": "dcm",
    "material_lot_id": None,
    "note": None, "is_active": True,
    "resolution": "MATCHED",
    "candidate_lot_ids": [],
    "lot": {"lot_id": "d2b30044-0000-4a11-9f00-000000000001",
            "article": "GOAT SUEDE", "colour": "DARK BROWN", "uom": "dcm",
            **_STOCK_BLOCK},
}

_SPEC_LINE_FIELDS = [
    ["lines[].line_id", "string (uuid)", "The recipe line."],
    ["lines[].scope", "string", "`STYLE` (every garment) or `SKU` (one colourway "
                                "only)."],
    ["lines[].sku_id", "string (uuid) or null", "Set only when `scope` is SKU."],
    ["lines[].category", "string", "`LEATHER` | `LINING` | `ACCESSORY`."],
    ["lines[].subtype", "string or null", "`RIBS`/`KNIT`, or "
                                          "`BUTTON`/`ZIP`/`THREAD`/`OTHER`."],
    ["lines[].article", "string", "The material's article."],
    ["lines[].colour", "string or null", "Its colour."],
    ["lines[].thickness", "string or null", "Its thickness."],
    ["lines[].size", "string or null", "THE MATERIAL's size — a 60 cm zip."],
    ["lines[].garment_size", "string or null",
     "WHICH GARMENTS this line is for. Do not confuse it with `size`: `size` is "
     "the material's, this one is the jacket's."],
    ["lines[].qty_per_piece", "number", "How much ONE garment takes."],
    ["lines[].uom", "string", "`dcm` | `mtrs` | `kg` | `pcs`."],
    ["lines[].material_lot_id", "string (uuid) or null",
     "Set when the line is pinned to one exact lot."],
    ["lines[].note", "string or null", "Free text."],
    ["lines[].is_active", "boolean", "`false` = removed (soft delete)."],
    ["lines[].resolution", "string",
     "How the line finds its stock: `PINNED` (names an exact lot), `MATCHED` "
     "(exactly one lot matches), `NONE` (no lot matches — receive stock first), "
     "`AMBIGUOUS` (several match — the line is not specific enough)."],
    ["lines[].candidate_lot_ids", "array of string",
     "Filled only when `resolution` is AMBIGUOUS — up to 5 lots to choose from."],
    ["lines[].lot", "object or null", "The resolved lot and its stock."],
    ["lines[].lot.arrived", "number", "Everything that ever came in."],
    ["lines[].lot.used", "number", "Cut or issued out of it."],
    ["lines[].lot.balance", "number", "What is on the shelf now."],
    ["lines[].lot.reserved", "number", "Committed to a requirement, not yet spent."],
    ["lines[].lot.available", "number", "`balance − reserved`."],
    ["lines[].lot.on_hand", "number", "The legacy key. Same as `balance`."],
]

# ─────────────────────────────────────────────────────────────────────────────
# PROCUREMENT building blocks (the same envelopes recur on many routes)
# ─────────────────────────────────────────────────────────────────────────────
_SUB_ID = "5b6c7d80-0000-4a11-9f00-000000000001"
_BOM_ID = "b0b0b0b0-0000-4a11-9f00-000000000001"
_PO_ID = "90a0b0c0-0000-4a11-9f00-000000000001"
_DOC_ID = "d7a90099-0000-4a11-9f00-000000000001"

_DOCUMENT_BLOCK = {
    "id": _DOC_ID,
    "kind": "ORDER_SHEET",
    "filename": "ORDER BOGGI MAIN SS27.xlsx",
    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "sha256": "9f2c…",
    "size_bytes": 84213,
    "page_count": None,
    "storage_url": "s3://kairox/submissions/5b6c…/order.xlsx",
    "validation": {
        "status": "accepted",
        "classified_as": "ORDER_SHEET",
        "spec_type": None,
        "client_match": "BOGGI",
        "confidence": 0.94,
        "method": "heuristic",
        "llm_label": None,
        "llm_model": None,
        "signals_matched": ["order_number", "size_grid", "client_name"],
        "signals_expected": ["order_number", "size_grid"],
        "signals_found": ["order_number", "size_grid", "client_name"],
        "reason_code": None,
        "suggested_fix": None,
    },
    "scan_status": "clean",
}

_SUBMISSION_BLOCK = {
    "order_sheet": {"present": True, "validation_status": "accepted"},
    "spec_sheet": {"present": False, "validation_status": None},
    "complete": False,
    "ready_for_stage_2": False,
    "blocking": ["spec_sheet missing"],
}

_SUBMISSION_FIELDS = [
    ["order_sheet.present", "boolean", "The slot holds an ACCEPTED document."],
    ["order_sheet.validation_status", "string or null",
     "`accepted` | `rejected` | `pending` | `needs_manual_review` | null when nothing has been uploaded."],
    ["spec_sheet.present", "boolean", "Same, for the specification sheet."],
    ["spec_sheet.validation_status", "string or null", "Same."],
    ["complete", "boolean", "Both slots are `accepted`."],
    ["ready_for_stage_2", "boolean",
     "Both slots `accepted` **and** neither has a blocking virus-scan status. "
     "This is the gate — the BOM cannot be generated until it is true."],
    ["blocking", "array of string",
     "Whole phrases saying what is in the way, ready to display."],
]

_UPLOAD_ENVELOPE = {"document": _DOCUMENT_BLOCK, "submission": _SUBMISSION_BLOCK}

_UPLOAD_FIELDS = [
    ["document.id", "string (uuid)", "The stored document."],
    ["document.kind", "string", "`ORDER_SHEET` | `SPEC_SHEET` — what it was "
                                "classified as, not what you called it."],
    ["document.filename", "string", "As uploaded."],
    ["document.mime", "string", "Detected media type."],
    ["document.sha256", "string",
     "Content hash. Re-uploading the same bytes replays the cached verdict."],
    ["document.size_bytes", "integer", "Size."],
    ["document.page_count", "integer or null", "For a PDF."],
    ["document.storage_url", "string or null", "Where it was stored."],
    ["document.validation.status", "string", "`accepted` | `rejected` | `needs_manual_review`."],
    ["document.validation.classified_as", "string or null", "What it looks like."],
    ["document.validation.spec_type", "string or null", "Spec-sheet variant."],
    ["document.validation.client_match", "string or null",
     "Which client's profile it matched."],
    ["document.validation.confidence", "number or null", "0–1."],
    ["document.validation.method", "string or null", "`heuristic`, `llm` or `manual`."],
    ["document.validation.llm_label", "string or null", "The model's label."],
    ["document.validation.llm_model", "string or null", "Which model."],
    ["document.validation.signals_matched", "array of string", "What it found "
                                                               "AND expected."],
    ["document.validation.signals_expected", "array of string",
     "What this kind of document must contain."],
    ["document.validation.signals_found", "array of string",
     "What the file actually contained."],
    ["document.validation.reason_code", "string or null",
     "On a rejection — why, as a code."],
    ["document.validation.suggested_fix", "string or null",
     "On a rejection — what to do, in words. Show this."],
    ["document.scan_status", "string",
     "`clean` | `skipped` pass. `infected` or `error` blocks Stage 2."],
    ["submission", "object", "The submission block described above."],
]

_BOM_VIEW = {
    "id": _BOM_ID, "status": "draft", "revision": 3, "currency": "INR",
    "order_qty": 152,
    "garment_fob_price": 148.5, "bulk_total": 512340.0,
    "cutting_confirmed_at": None, "approved_at": None,
    "rejection_reason": None, "export_document_id": None, "exported_at": None,
    "items": [{
        "id": "f0e10001-0000-4a11-9f00-000000000001",
        "category": "LEATHER", "name": "GOAT SUEDE", "material_color": "DARK BROWN",
        "qty_per_garment": 14.5, "uom": "dcm", "unit_price": 3.4,
        "bulk_qty": 2204.0, "total_cost": 7493.6,
        "dcm_source": "dxf", "dcm_confidence": 0.88,
        "annotation": None,
    }],
}

_BOM_FIELDS = [
    ["id", "string (uuid)", "The BOM."],
    ["status", "string",
     "`draft` | `ready_for_review` | `approved` | `locked` | `rejected` | `exported`."],
    ["revision", "integer",
     "The optimistic lock. A PATCH must send this back as `base_revision`."],
    ["currency", "string", "Costing currency."],
    ["order_qty", "integer", "Garments the BOM was costed for."],
    ["garment_fob_price", "number or null", "Price per garment."],
    ["bulk_total", "number or null", "Total material cost for the order."],
    ["cutting_confirmed_at", "string (date-time) or null",
     "When the cutting manager confirmed the consumption figures."],
    ["approved_at", "string (date-time) or null", "When the MD approved it."],
    ["rejection_reason", "string or null", "Why it was rejected."],
    ["export_document_id", "string (uuid) or null", "The exported file."],
    ["exported_at", "string (date-time) or null", "When it was exported."],
    ["items[].id", "string (uuid)", "The line."],
    ["items[].category", "string", "`LEATHER` | `LINING` | `ACCESSORY` …"],
    ["items[].name", "string", "The material."],
    ["items[].material_color", "string or null", "Its colour."],
    ["items[].qty_per_garment", "number or null", "Per garment."],
    ["items[].uom", "string", "Unit."],
    ["items[].unit_price", "number or null", "Per unit."],
    ["items[].bulk_qty", "number or null", "`qty_per_garment × order_qty`."],
    ["items[].total_cost", "number or null", "`bulk_qty × unit_price`."],
    ["items[].dcm_source", "string or null",
     "Where the consumption came from — for example the DXF pattern."],
    ["items[].dcm_confidence", "number or null",
     "0–1. A low number is the line to check first."],
    ["items[].annotation", "string or null", "Free note on the line."],
]

_INV_PREVIEW = {
    "raw_count": 412, "kept": 388, "dropped": 24,
    "warnings": ["row 91: no UOM, defaulted to pcs"],
    "rows": [{"normalized_key": "goat suede dark brown 1.0mm",
              "description": "GOAT SUEDE DARK BROWN 1.0MM", "uom": "dcm",
              "qty_on_hand": 18400.0, "rate": 3.4, "color": "DARK BROWN",
              "lots": 3}],
}

_INV_PREVIEW_FIELDS = [
    ["raw_count", "integer", "Rows read from the file."],
    ["kept", "integer", "Rows that will be / were written."],
    ["dropped", "integer", "Rows ignored — see `warnings`."],
    ["warnings", "array of string", "One phrase per problem, with the row."],
    ["rows[].normalized_key", "string", "The cleaned key the matcher uses."],
    ["rows[].description", "string", "As the warehouse wrote it."],
    ["rows[].uom", "string", "Unit of measure."],
    ["rows[].qty_on_hand", "number or null", "Quantity on the shelf."],
    ["rows[].rate", "number or null", "Unit rate."],
    ["rows[].color", "string or null", "Colour."],
    ["rows[].lots", "integer or null", "How many source rows merged into this."],
]

_CHECK_VIEW = {
    "inventory_check_id": "aa330044-0000-4a11-9f00-000000000001",
    "bom_id": _BOM_ID, "status": "complete",
    "run_at": "2026-09-23T10:40:00Z",
    "summary": {"badge": "partial", "lines_total": 12, "sufficient": 9,
                "partial": 2, "out_of_stock": 1,
                "flags": {"unmatched": 1, "uom_mismatch": 0},
                "shortfall_value": 18420.50, "currency": "INR"},
    "lines": [{
        "bom_item_id": "f0e10001-0000-4a11-9f00-000000000001",
        "category": "LEATHER", "name": "GOAT SUEDE",
        "material_color": "DARK BROWN", "required_qty": 2204.0, "uom": "dcm",
        "matched": {"inventory_item_id": "c0ffee11-0000-4a11-9f00-000000000001",
                    "description": "GOAT SUEDE DARK BROWN 1.0MM",
                    "method": "alias", "uom": "dcm"},
        "on_hand_qty": 18400.0, "available_qty": 1800.0,
        "reserved_for_this_bom": 1800.0, "shortfall_qty": 404.0,
        "status": "partial", "flags": [], "suggestion": None,
    }],
    "excluded": [{"bom_item_id": "f0e10001-0000-4a11-9f00-000000000009",
                  "name": "THREAD 40s", "category": "ACCESSORY"}],
}

_CHECK_FIELDS = [
    ["inventory_check_id", "string (uuid)", "This check."],
    ["bom_id", "string (uuid)", "The BOM it was run against."],
    ["status", "string", "The check's own state."],
    ["run_at", "string (date-time)", "When it ran."],
    ["summary.badge", "string",
     "`sufficient` | `partial` | `out_of_stock` — the **worst** line. This is "
     "the badge for the whole BOM."],
    ["summary.lines_total", "integer", "Lines checked."],
    ["summary.sufficient", "integer", "Fully covered."],
    ["summary.partial", "integer", "Partly covered."],
    ["summary.out_of_stock", "integer", "Nothing available."],
    ["summary.flags.unmatched", "integer",
     "Lines whose material could not be matched to any inventory item."],
    ["summary.flags.uom_mismatch", "integer",
     "Matched, but the units disagree — check before trusting the number."],
    ["summary.shortfall_value", "number", "Shortfall × rate, summed."],
    ["summary.currency", "string", "Currency of that value."],
    ["lines[].bom_item_id", "string (uuid)", "The BOM line."],
    ["lines[].category", "string", "Its category."],
    ["lines[].name", "string", "The material."],
    ["lines[].material_color", "string or null", "Its colour."],
    ["lines[].required_qty", "number or null", "What the BOM needs."],
    ["lines[].uom", "string or null", "Unit."],
    ["lines[].matched", "object or null",
     "The inventory item it matched, and how. `null` means unmatched."],
    ["lines[].matched.inventory_item_id", "string (uuid)", "The stock item."],
    ["lines[].matched.description", "string", "Its description."],
    ["lines[].matched.method", "string", "How it matched — exact, alias, fuzzy."],
    ["lines[].matched.uom", "string", "The stock item's unit."],
    ["lines[].on_hand_qty", "number or null", "On the shelf."],
    ["lines[].available_qty", "number or null", "Free to promise."],
    ["lines[].reserved_for_this_bom", "number or null", "Held for this BOM."],
    ["lines[].shortfall_qty", "number or null", "What must be bought."],
    ["lines[].status", "string", "`sufficient` | `partial` | `out_of_stock`."],
    ["lines[].flags", "array of string", "`unmatched`, `uom_mismatch`."],
    ["lines[].suggestion", "string or null", "What the matcher suggests instead."],
    ["excluded[].bom_item_id", "string (uuid)", "A line not checked."],
    ["excluded[].name", "string", "Its material."],
    ["excluded[].category", "string", "Its category."],
]

_PO_VIEW = {
    "id": _PO_ID, "po_number": "PO-2026-0041", "status": "draft", "revision": 1,
    "supplier_id": "e3c40055-0000-4a11-9f00-000000000001",
    "bom_id": _BOM_ID,
    "client_order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
    "buyer_ref": "NB-B-1", "issue_date": "2026-09-23",
    "delivery_days": 10, "payment_terms_days": 60,
    "currency": "INR", "gst_mode": "intra",
    "subtotal": 43220.0, "cgst": 3889.8, "sgst": 3889.8, "igst": 0.0,
    "round_off": 0.4, "total": 51000.0,
    "needs_supplier": False, "no_contact_channel": False,
    "match_method": "history", "candidates": None,
    "approved_at": None, "rejected_at": None, "rejection_reason": None,
    "sent_at": None, "pdf_document_id": None,
    "first_opened_at": None, "first_clicked_at": None,
    "current_rung": 0, "next_escalation_at": None,
    "acknowledged_at": None, "acknowledged_channel": None,
    "items": [{"id": "77880011-0000-4a11-9f00-000000000001", "item_no": 1,
               "description": "GOAT SUEDE DARK BROWN 1.0MM", "color": "DARK BROWN",
               "uom": "dcm", "qty": 404.0, "unit_price": 3.4, "amount": 1373.6,
               "inventory_item_id": "c0ffee11-0000-4a11-9f00-000000000001",
               "bom_item_id": "f0e10001-0000-4a11-9f00-000000000001"}],
    "supplier": {"id": "e3c40055-0000-4a11-9f00-000000000001",
                 "name": "S.N. TRADERS", "email": "sales@sntraders.example",
                 "phone": "+919000000000", "gstin": "33AAAAA0000A1Z5",
                 "address": "Chennai", "supplier_type": "leather",
                 "email_status": "verified"},
}

_PO_FIELDS = [
    ["id", "string (uuid)", "The purchase order."],
    ["po_number", "string", "Its human number."],
    ["status", "string",
     "`draft` → `pending_approval` → `approved` → `sent` → `responded` / "
     "`escalated` → `confirmed`. Also `rejected` (back to draft) and "
     "`cancelled` (from any pre-send state)."],
    ["revision", "integer", "The optimistic lock for `PATCH …/items`."],
    ["supplier_id", "string (uuid) or null", "Who it is addressed to."],
    ["bom_id", "string (uuid) or null", "The BOM it came from."],
    ["client_order_id", "string (uuid) or null", "The order behind it."],
    ["buyer_ref", "string or null", "Our reference on the document."],
    ["issue_date", "string (date) or null", "Printed date."],
    ["delivery_days", "integer or null", "Agreed lead time."],
    ["payment_terms_days", "integer or null", "Agreed terms."],
    ["currency", "string", "Currency."],
    ["gst_mode", "string", "`intra` (CGST+SGST) or `inter` (IGST)."],
    ["subtotal", "number or null", "Before tax."],
    ["cgst", "number or null", "Central GST."],
    ["sgst", "number or null", "State GST."],
    ["igst", "number or null", "Integrated GST."],
    ["round_off", "number or null", "Rounding adjustment."],
    ["total", "number or null", "What the supplier will invoice."],
    ["needs_supplier", "boolean",
     "**true = nobody is on this PO yet.** The match could not pick a supplier; "
     "somebody must choose one before it can be sent."],
    ["no_contact_channel", "boolean",
     "The supplier has neither an email nor a phone — it cannot be sent."],
    ["match_method", "string or null",
     "How the supplier was chosen — for example from purchase history."],
    ["candidates", "array or null",
     "The other suppliers considered, when the match was not certain."],
    ["approved_at", "string (date-time) or null", "When approved."],
    ["rejected_at", "string (date-time) or null", "When rejected."],
    ["rejection_reason", "string or null", "Why."],
    ["sent_at", "string (date-time) or null", "When it was emailed."],
    ["pdf_document_id", "string (uuid) or null", "The generated PDF."],
    ["first_opened_at", "string (date-time) or null",
     "When the supplier first opened the email (tracking pixel)."],
    ["first_clicked_at", "string (date-time) or null",
     "When they first clicked a link in it."],
    ["current_rung", "integer",
     "How far up the escalation ladder this PO has climbed."],
    ["next_escalation_at", "string (date-time) or null",
     "When the sweeper will chase it next."],
    ["acknowledged_at", "string (date-time) or null",
     "When the supplier confirmed. **This stops the ladder.**"],
    ["acknowledged_channel", "string or null", "email / whatsapp / voice."],
    ["items[].id", "string (uuid)", "The line."],
    ["items[].item_no", "integer", "Its number on the printed PO."],
    ["items[].description", "string", "What is being bought."],
    ["items[].color", "string or null", "Colour."],
    ["items[].uom", "string", "Unit."],
    ["items[].qty", "number or null", "Quantity."],
    ["items[].unit_price", "number or null", "Rate."],
    ["items[].amount", "number or null", "`qty × unit_price`."],
    ["items[].inventory_item_id", "string (uuid) or null", "The stock item."],
    ["items[].bom_item_id", "string (uuid) or null", "The BOM line it covers."],
    ["supplier", "object or null",
     "The supplier's contact block, when one is attached."],
]

_SUPPLIER_BLOCK = {
    "id": "e3c40055-0000-4a11-9f00-000000000001", "name": "S.N. TRADERS",
    "phone": "+919000000000", "email": "sales@sntraders.example",
    "service": "leather", "gstin": "33AAAAA0000A1Z5", "address": "Chennai",
    "currency": "INR", "payment_terms_days": 60, "lead_time_days": 10,
    "is_active": True, "email_status": "verified", "supplier_type": "leather",
    "state_code": "33", "whatsapp_phone": "+919000000000", "has_contact": True,
}

_SUPPLIER_IMPORT = {
    "contact_rows": 126, "provision_rows": 4180, "suppliers": 126,
    "suppliers_with_phone": 118, "suppliers_with_email": 97,
    "history_rows": 4180,
    "warnings": ["row 214: supplier has neither phone nor email"],
    "sample_history": [{"supplier": "S.N. TRADERS",
                        "article": "goat suede dark brown", "mode": "purchase",
                        "txn_count": 14, "last_rate": 3.4}],
}

_SUPPLIER_IMPORT_FIELDS = [
    ["contact_rows", "integer", "Contact rows read from the sheet."],
    ["provision_rows", "integer", "Purchase-history rows read."],
    ["suppliers", "integer", "Distinct suppliers found."],
    ["suppliers_with_phone", "integer", "How many have a phone."],
    ["suppliers_with_email", "integer", "How many have an email."],
    ["history_rows", "integer", "History rows parsed."],
    ["warnings", "array of string", "One phrase per problem, with the row."],
    ["sample_history[].supplier", "string", "Supplier name."],
    ["sample_history[].article", "string", "The article, normalised."],
    ["sample_history[].mode", "string or null", "How it was supplied."],
    ["sample_history[].txn_count", "integer", "How many transactions."],
    ["sample_history[].last_rate", "number or null", "Last rate paid."],
]

_SUPPLIER_FIELDS = [
    ["suppliers[].id", "string (uuid)", "The supplier."],
    ["suppliers[].name", "string", "Their name. Unique."],
    ["suppliers[].phone", "string or null", "Phone."],
    ["suppliers[].email", "string or null", "Email."],
    ["suppliers[].service", "string or null", "What they supply."],
    ["suppliers[].gstin", "string or null", "GST number."],
    ["suppliers[].address", "string or null", "Address."],
    ["suppliers[].currency", "string", "Default currency."],
    ["suppliers[].payment_terms_days", "integer", "Default terms."],
    ["suppliers[].lead_time_days", "integer", "Default lead time."],
    ["suppliers[].is_active", "boolean", "`false` = deactivated, not deleted."],
    ["suppliers[].email_status", "string or null",
     "Whether the address has been proved to work."],
    ["suppliers[].supplier_type", "string or null", "Category."],
    ["suppliers[].state_code", "string or null", "Drives intra/inter GST."],
    ["suppliers[].whatsapp_phone", "string or null", "For the WhatsApp chase."],
    ["suppliers[].has_contact", "boolean",
     "There is an email **or** a phone. Without one, a PO cannot be sent."],
]


RESPONSES: dict[str, dict] = {

    # ═════════════════════════════════════════════════════════════════════════
    # IMPORT — breakdown upload
    # ═════════════════════════════════════════════════════════════════════════
    "POST /api/v1/imports/preview": {
        "note": "The parsed workbook. **Nothing is written.** `clients` is keyed "
                "by the client prefix of the sheet name; `sheets` says what each "
                "tab was recognised as; `total_warnings` is the number the "
                "screen should show next to the Commit button.",
        "fields": [
            ["clients", "object", "One entry per client found in the workbook, "
                                  "keyed by the client key (for example `BOGGI`)."],
            ["clients.<KEY>.order_lines", "integer", "How many order rows were read."],
            ["clients.<KEY>.pieces_ordered", "integer", "Total garments on the sheet."],
            ["clients.<KEY>.styles", "array of string", "Style names found."],
            ["clients.<KEY>.by_style", "object", "Per style: size split, totals "
                                                 "and the commercial facts."],
            ["clients.<KEY>.by_style.<STYLE>.sizes", "object",
             "`{size: quantity}` — the per-size split."],
            ["clients.<KEY>.by_style.<STYLE>.pieces_ordered", "integer",
             "Sum of that style's sizes."],
            ["clients.<KEY>.by_style.<STYLE>.unit_price", "number or null",
             "First price found for the style (the commit uses the same rule)."],
            ["clients.<KEY>.by_style.<STYLE>.currency", "string or null",
             "Currency of that price."],
            ["clients.<KEY>.by_style.<STYLE>.delivery_date", "string or null",
             "Earliest delivery date found for the style."],
            ["clients.<KEY>.by_style.<STYLE>.size_confidence", "string",
             "`HIGH` or `MEDIUM`. MEDIUM means the size band could not be proved "
             "against a printed total — the reason is in `warnings`."],
            ["clients.<KEY>.warnings", "array of string",
             "Every problem found, already prefixed with the sheet name."],
            ["sheets", "array of object", "One row per worksheet."],
            ["sheets[].sheet", "string", "The tab name."],
            ["sheets[].type", "string", "`ORDER` (imported) or `UNKNOWN` (skipped)."],
            ["sheets[].client", "string", "Client key the tab was grouped under."],
            ["total_warnings", "integer", "Sum of all clients' warnings."],
        ],
        "example": _PREVIEW_SUMMARY,
    },

    "POST /api/v1/imports/commit": {
        "note": "The same parse as preview, plus what was written. **No barcodes "
                "are minted here** — `release_required` is always `true` and "
                "`pieces_minted` is always `0`. The mint happens on release.",
        "fields": [
            ["summary", "object", "Exactly the preview body described above."],
            ["written", "object", "What the database write did."],
            ["written.order_number", "string", "The order the rows went into."],
            ["written.styles", "integer", "Styles touched."],
            ["written.skus_created", "integer", "New SKU rows."],
            ["written.skus_updated", "integer", "Existing SKU rows updated "
                                                "(a re-upload is idempotent)."],
            ["written.operations", "integer", "Always 0 — the importer no longer "
                                              "creates operations."],
            ["written.rates", "integer", "Always 0 — wage rates are entered in "
                                         "the wages module."],
            ["written.release_required", "boolean", "Always `true`. Nothing is on "
                                                    "the floor until release."],
            ["written.pieces_minted", "integer", "Always 0 here."],
        ],
        "example": {
            "summary": _PREVIEW_SUMMARY,
            "written": {"order_number": "NB-B-1", "styles": 2,
                        "skus_created": 18, "skus_updated": 0,
                        "operations": 0, "rates": 0,
                        "release_required": True, "pieces_minted": 0},
        },
    },

    "GET /api/v1/imports/orders": {
        "note": "The order index the breakdown screen opens from. Newest activity "
                "first. Click a row and call its `breakdown_url`.",
        "fields": [
            ["total", "integer", "Orders matching the filters, ignoring paging."],
            ["count", "integer", "Rows in this page."],
            ["items[].order_id", "string (uuid)", "The order."],
            ["items[].order_number", "string", "What the client calls it."],
            ["items[].client_id", "string (uuid)", "Buyer."],
            ["items[].client_name", "string", "Buyer name."],
            ["items[].client_code", "string or null", "Buyer short code."],
            ["items[].order_date", "string (date) or null", "The client's date."],
            ["items[].delivery_deadline", "string (date) or null", "Ship-by date."],
            ["items[].ship_mode", "string", "`sea` or `air`."],
            ["items[].currency", "string or null", "Order currency."],
            ["items[].breakdown_status", "string",
             "`NOT_UPLOADED` | `DRAFT` | `PARTIALLY_RELEASED` | `RELEASED` | "
             "`CANCELLED` — rolled up from the order's styles."],
            ["items[].style_count", "integer", "Styles on the order."],
            ["items[].released_styles", "integer", "How many are released."],
            ["items[].cancelled_styles", "integer", "How many were withdrawn."],
            ["items[].sku_count", "integer", "Colour/size lines."],
            ["items[].qty_ordered", "integer", "Garments ordered."],
            ["items[].pieces_minted", "integer",
             "Real barcoded garments behind the order. 0 on a RELEASED row means "
             "it released and produced nothing."],
            ["items[].last_uploaded_at", "string (date-time) or null",
             "NULL until a breakdown sheet is committed."],
            ["items[].last_activity_at", "string (date-time)", "Used for sorting."],
            ["items[].created_at", "string (date-time)", "When the order was raised."],
            ["items[].breakdown_url", "string",
             "The exact call to make when the row is clicked — already encoded."],
        ],
        "example": {
            "total": 42, "count": 1,
            "items": [{
                "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
                "order_number": "NB-B-1",
                "client_id": "3f8c1b2a-0000-4a11-9f00-000000000001",
                "client_name": "BOGGI", "client_code": "BOG",
                "order_date": "2026-08-01", "delivery_deadline": "2026-11-30",
                "ship_mode": "sea", "currency": "EUR",
                "breakdown_status": "PARTIALLY_RELEASED",
                "style_count": 2, "released_styles": 1, "cancelled_styles": 0,
                "sku_count": 18, "qty_ordered": 840, "pieces_minted": 152,
                "last_uploaded_at": "2026-09-20T11:04:00Z",
                "last_activity_at": "2026-09-22T08:31:00Z",
                "created_at": "2026-08-01T09:00:00Z",
                "breakdown_url": "/api/v1/imports/breakdown/NB-B-1",
            }],
        },
    },

    "GET /api/v1/imports/breakdown/{order_number}": {
        "note": "The breakdown as a table: every style with its SKUs, its release "
                "state and its lining answer. This is the screen between upload "
                "and production.",
        "fields": [
            ["order_id", "string (uuid)", "The order."],
            ["order_number", "string", "The order number you asked for."],
            ["styles[].style_id", "string (uuid)", "The style."],
            ["styles[].style_code", "string", "Deterministic style code."],
            ["styles[].style_name", "string", "For example `CLERMONT`."],
            ["styles[].article", "string or null", "Leather article."],
            ["styles[].production_status", "string",
             "`DRAFT` | `RELEASED` | `CANCELLED`."],
            ["styles[].released_at", "string (date-time) or null", "When released."],
            ["styles[].released_by", "string or null", "Who released it."],
            ["styles[].editable", "boolean",
             "`true` only while DRAFT. Render the row read-only when `false`."],
            ["styles[].needs_lining", "boolean or null",
             "The DM's ANSWER. `null` means nobody has been asked — render it as "
             "an unanswered question, never as 'no'."],
            ["styles[].needs_lining_suggested", "boolean",
             "What the system guesses from the style name. A default to "
             "pre-select, never a verdict."],
            ["styles[].lining_answered", "boolean", "Has anyone answered yet."],
            ["styles[].sku_count", "integer", "Colour/size lines on this style."],
            ["styles[].qty_ordered", "integer", "Garments ordered for this style."],
            ["styles[].minted_pieces", "integer", "Barcoded garments produced."],
            ["styles[].skus[].sku_id", "string (uuid)", "The SKU."],
            ["styles[].skus[].sku_code", "string", "Deterministic SKU code."],
            ["styles[].skus[].colour", "string or null", "Colour name, else code."],
            ["styles[].skus[].color_code", "string or null", "Client's colour code."],
            ["styles[].skus[].size", "string", "Size label."],
            ["styles[].skus[].qty_ordered", "integer", "Garments for this line."],
            ["styles[].skus[].knit_color", "string or null", "Knit trim colour."],
            ["styles[].skus[].nylon_color", "string or null", "Lining colour."],
            ["totals.styles", "integer", "Styles on the order."],
            ["totals.styles_draft", "integer", "Still editable."],
            ["totals.styles_released", "integer", "In production."],
            ["totals.qty_ordered", "integer", "Garments ordered in total."],
            ["totals.qty_draft", "integer", "Garments on DRAFT styles."],
            ["totals.minted_pieces", "integer", "Barcodes minted so far."],
            ["totals.styles_awaiting_lining_answer", "integer",
             "DRAFT styles with no lining answer. Badge this on the release "
             "screen — those styles cannot be released."],
        ],
        "example": {
            "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
            "order_number": "NB-B-1",
            "styles": [{
                "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
                "style_name": "CLERMONT", "article": "GOAT SUEDE",
                "production_status": "DRAFT",
                "released_at": None, "released_by": None, "editable": True,
                "needs_lining": None, "needs_lining_suggested": True,
                "lining_answered": False,
                "sku_count": 1, "qty_ordered": 40, "minted_pieces": 0,
                "skus": [{
                    "sku_id": "b4d10022-0000-4a11-9f00-000000000001",
                    "sku_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46",
                    "colour": "DARK BROWN", "color_code": "DB",
                    "size": "46", "qty_ordered": 40,
                    "knit_color": None, "nylon_color": "BLACK",
                }],
            }],
            "totals": {"styles": 1, "styles_draft": 1, "styles_released": 0,
                       "qty_ordered": 40, "qty_draft": 40, "minted_pieces": 0,
                       "styles_awaiting_lining_answer": 1},
        },
    },

    "PATCH /api/v1/imports/breakdown/skus/{sku_id}": {
        "note": "The corrected line, and its parent style when you sent a `style` "
                "block. `changed` holds only the fields that actually moved.",
        "fields": [
            ["sku_id", "string (uuid)", "The line you edited."],
            ["sku_code", "string", "Its code (unchanged by an edit)."],
            ["qty_ordered", "integer", "Quantity after the edit."],
            ["colour", "string or null", "Colour name, else colour code."],
            ["size", "string", "Size after the edit."],
            ["changed", "object", "`{field: new value}` for the SKU."],
            ["style.style_id", "string (uuid)", "The parent style."],
            ["style.style_code", "string or null", "Its code."],
            ["style.style_name", "string", "Its name."],
            ["style.article", "string or null", "Its article."],
            ["style.needs_lining", "boolean or null", "The lining answer."],
            ["style.changed", "object", "`{field: new value}` for the style. "
                                        "`{}` when you sent no `style` block."],
        ],
        "example": {
            "sku_id": "b4d10022-0000-4a11-9f00-000000000001",
            "sku_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46",
            "qty_ordered": 44, "colour": "DARK BROWN", "size": "46",
            "changed": {"qty_ordered": 44},
            "style": {
                "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
                "style_name": "CLERMONT", "article": "GOAT SUEDE",
                "needs_lining": True, "changed": {"needs_lining": True},
            },
        },
    },

    "PATCH /api/v1/imports/breakdown/styles/{style_id}": {
        "note": "The style after the edit. `changed` holds only what moved.",
        "fields": [
            ["style_id", "string (uuid)", "The style."],
            ["style_code", "string or null", "Its code — unique across styles."],
            ["style_name", "string", "Its name."],
            ["article", "string or null", "Leather article."],
            ["thickness", "string or null", "Leather thickness."],
            ["season", "string or null", "Season."],
            ["unit_price", "number or null", "Price per garment."],
            ["currency", "string or null", "Price currency."],
            ["needs_lining", "boolean or null", "The lining answer (`null` = unasked)."],
            ["lining_answered", "boolean", "Whether anyone has answered."],
            ["production_status", "string", "`DRAFT` | `RELEASED` | `CANCELLED`."],
            ["changed", "object", "`{field: new value}`."],
        ],
        "example": {
            "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
            "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
            "style_name": "CLERMONT", "article": "GOAT SUEDE",
            "thickness": "0.8-1.0", "season": "SS27",
            "unit_price": 148.5, "currency": "EUR",
            "needs_lining": True, "lining_answered": True,
            "production_status": "DRAFT",
            "changed": {"thickness": "0.8-1.0", "needs_lining": True},
        },
    },

    "DELETE /api/v1/imports/breakdown/skus/{sku_id}": {
        "note": "Confirmation that the DRAFT line is gone.",
        "fields": [
            ["deleted", "boolean", "Always `true` when you get a 200."],
            ["sku_id", "string (uuid)", "The line that was removed."],
            ["sku_code", "string or null", "Its code, for the toast message."],
        ],
        "example": {"deleted": True,
                    "sku_id": "b4d10022-0000-4a11-9f00-000000000001",
                    "sku_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46"},
    },

    "POST /api/v1/imports/breakdown/{order_number}/cancel": {
        "note": "Partial accept: a style that could not be cancelled never stops "
                "the others. Read the two lists, not the status code.",
        "fields": [
            ["cancelled[].style_id", "string (uuid)", "Withdrawn."],
            ["cancelled[].style_code", "string or null", "Its code."],
            ["rejected[].style_id", "string (uuid)", "Not withdrawn."],
            ["rejected[].style_code", "string or null", "Its code, when known."],
            ["rejected[].reason", "string", "Plain sentence — show it as-is."],
        ],
        "example": {
            "cancelled": [{"style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                           "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE"}],
            "rejected": [{"style_id": "9c2f0011-0000-4a11-9f00-000000000002",
                          "style_code": "NB-B-1-VEST",
                          "reason": "VEST is RELEASED — only a DRAFT style can "
                                    "be cancelled."}],
        },
    },

    "POST /api/v1/imports/breakdown/{order_number}/release": {
        "note": "THE MINT. Partial accept — read `minted` and `rejected`, not the "
                "HTTP status. `minted` is `{}` when nothing was released.",
        "fields": [
            ["order_number", "string", "The order."],
            ["released[].style_id", "string (uuid)", "Style now in production."],
            ["released[].style_code", "string or null", "Its code."],
            ["released[].style_name", "string", "Its name."],
            ["released[].production_status", "string", "`RELEASED`."],
            ["released[].needs_lining", "boolean or null",
             "The lining requirement stamped on the style and copied to every "
             "piece it minted. It can never be changed after this."],
            ["released[].lining_declared", "boolean",
             "`true` when a human answered. `false` means it was GUESSED from the "
             "style name — show that difference on the screen."],
            ["rejected[].style_id", "string (uuid)", "Not released."],
            ["rejected[].style_code", "string or null", "Its code."],
            ["rejected[].reason", "string", "One sentence, ready to display."],
            ["rejected[].blockers", "array of string",
             "Present when the material spec blocked it — one sentence per "
             "problem, so the screen can list them beside the row."],
            ["minted.pieces_minted", "integer", "Barcoded garments created."],
            ["minted.pieces_needing_lining", "integer",
             "How many of them must hold both parts before line-stitching."],
            ["minted.sample_barcodes", "array of string",
             "A few of the codes just created, so the screen can prove it worked."],
            ["styles_released_without_lining_answer", "array of string",
             "Style codes released on a guess. Empty is what you want."],
            ["message", "string",
             "A ready-made sentence for the toast, including the warning when "
             "any style released without a lining answer."],
        ],
        "example": {
            "order_number": "NB-B-1",
            "released": [{
                "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
                "style_name": "CLERMONT", "production_status": "RELEASED",
                "needs_lining": True, "lining_declared": True,
            }],
            "rejected": [{
                "style_id": "9c2f0011-0000-4a11-9f00-000000000002",
                "style_code": "NB-B-1-VEST",
                "reason": "VEST cannot be released yet. VEST's material spec has "
                          "not been confirmed. Enter the per-piece consumption "
                          "(PUT /styles/{id}/material-spec) and confirm it.",
                "blockers": ["VEST's material spec has not been confirmed. Enter "
                             "the per-piece consumption "
                             "(PUT /styles/{id}/material-spec) and confirm it."],
            }],
            "minted": {"pieces_minted": 40, "pieces_needing_lining": 40,
                       "sample_barcodes": ["PC-0001A7", "PC-0001A8", "PC-0001A9"]},
            "styles_released_without_lining_answer": [],
            "message": "Released 1 style(s); 40 piece barcode(s) minted. 1 "
                       "style(s) take a lining — the store must hold BOTH parts "
                       "before line-stitching.",
        },
    },

    # ═════════════════════════════════════════════════════════════════════════
    # PROCUREMENT — Stage 1 intake
    # ═════════════════════════════════════════════════════════════════════════
    "POST /api/v1/procurement/submissions": {
        "note": "Opens an empty submission — the folder the order sheet and the "
                "spec sheet are uploaded into. Keep the `submission_id`.",
        "fields": [
            ["submission_id", "string (uuid)", "Use it on every later Stage-1 call."],
            ["client_id", "string (uuid) or null", "Echoed from the request."],
            ["status", "string", "`open` on creation."],
        ],
        "example": {"submission_id": _SUB_ID,
                    "client_id": "3f8c1b2a-0000-4a11-9f00-000000000001",
                    "status": "open"},
    },

    "GET /api/v1/procurement/submissions/{submission_id}": {
        "note": "**The Stage-2 gate.** `ready_for_stage_2` is true only when BOTH "
                "slots are ACCEPTED and neither has a blocking virus-scan status. "
                "`blocking[]` says, in words, what is missing.",
        "fields": _SUBMISSION_FIELDS,
        "example": _SUBMISSION_BLOCK,
    },

    "POST /api/v1/procurement/submissions/{submission_id}/order-sheet": {
        "note": "Uploads the order sheet into this submission. The reply is the "
                "document **and** the submission's new readiness. A document that "
                "does not look like an order sheet comes back **422** with the "
                "signals it expected, the signals it found, and a suggested fix.",
        "fields": _UPLOAD_FIELDS,
        "example": _UPLOAD_ENVELOPE,
    },

    "POST /api/v1/procurement/submissions/{submission_id}/spec-sheet": {
        "note": "Same as the order-sheet upload, for the specification sheet.",
        "fields": _UPLOAD_FIELDS,
        "example": _UPLOAD_ENVELOPE,
    },

    "POST /api/v1/procurement/upload/order-sheet": {
        "note": "The **one-shot** door: opens a submission and uploads the order "
                "sheet in one call. Same body as the two-step version.",
        "fields": _UPLOAD_FIELDS,
        "example": _UPLOAD_ENVELOPE,
    },

    "POST /api/v1/procurement/upload/spec-sheet": {
        "note": "The one-shot door for the spec sheet.",
        "fields": _UPLOAD_FIELDS,
        "example": _UPLOAD_ENVELOPE,
    },

    "GET /api/v1/procurement/submissions/{submission_id}/documents/{document_id}": {
        "note": "One uploaded document, with the full classification verdict — "
                "the same `document` block the upload returned.",
        "fields": [[f[0].replace("document.", ""), f[1], f[2]]
                   for f in _UPLOAD_FIELDS if f[0].startswith("document.")],
        "example": _DOCUMENT_BLOCK,
    },

    # ═════════════════════════════════════════════════════════════════════════
    # PROCUREMENT — Stage 2/3 BOM
    # ═════════════════════════════════════════════════════════════════════════
    "GET /api/v1/procurement/boms/{bom_id}": {
        "note": "The whole BOM: its status, its revision, its costing totals and "
                "every line.",
        "fields": _BOM_FIELDS,
        "example": _BOM_VIEW,
    },

    "GET /api/v1/procurement/boms/{bom_id}/items": {
        "note": "The BOM's lines with just enough header to edit them. "
                "**`revision` is what a PATCH must send back** as "
                "`base_revision` — that is the optimistic lock.",
        "fields": [
            ["bom_id", "string (uuid)", "The BOM."],
            ["status", "string", "Its status."],
            ["revision", "integer",
             "Send this as `base_revision` on a PATCH. A stale number is a 409."],
            ["items[]", "array of object", "The same line shape as above."],
        ],
        "example": {"bom_id": _BOM_ID, "status": "draft", "revision": 3,
                    "items": _BOM_VIEW["items"]},
    },

    "PATCH /api/v1/procurement/boms/{bom_id}/items": {
        "note": "The edited BOM, in the same shape as `GET /boms/{bom_id}`, with "
                "`revision` incremented. A `base_revision` that is not the "
                "current one is a **409** carrying `current_revision` — reload "
                "and re-apply.",
        "fields": _BOM_FIELDS,
        "example": dict(_BOM_VIEW, revision=4),
    },

    "POST /api/v1/procurement/boms/{bom_id}/confirm-cutting": {
        "note": "The cutting manager confirms the consumption figures. The BOM "
                "comes back with `cutting_confirmed_at` set.",
        "fields": _BOM_FIELDS,
        "example": dict(_BOM_VIEW, status="ready_for_review",
                        cutting_confirmed_at="2026-09-23T09:15:00Z"),
    },

    "POST /api/v1/procurement/boms/{bom_id}/approve": {
        "note": "**MD only** — the Direct Manager is refused here on purpose "
                "(separation of duties). `lock: true` freezes the BOM as well as "
                "approving it.",
        "fields": _BOM_FIELDS,
        "example": dict(_BOM_VIEW, status="approved",
                        approved_at="2026-09-23T10:02:00Z"),
    },

    "POST /api/v1/procurement/boms/{bom_id}/reject": {
        "note": "MD only. `reason` is required and comes back on the BOM.",
        "fields": _BOM_FIELDS,
        "example": dict(_BOM_VIEW, status="rejected",
                        rejection_reason="Lining consumption looks doubled."),
    },

    "POST /api/v1/procurement/boms/{bom_id}/reopen": {
        "note": "Puts an approved or rejected BOM back to DRAFT so it can be "
                "edited again.",
        "fields": _BOM_FIELDS,
        "example": dict(_BOM_VIEW, status="draft", approved_at=None),
    },

    "POST /api/v1/procurement/boms/{bom_id}/export": {
        "note": "MD only. Renders the BOM to a document. **This is NOT the BOM "
                "shape** — it is a short receipt for the generated file. Calling "
                "it twice does not export twice: the second call replays the "
                "same document with `replay: true` and the ORIGINAL "
                "`exported_at`.",
        "fields": [
            ["bom_id", "string (uuid)", "The BOM."],
            ["status", "string", "Its status — `exported` after a first export."],
            ["export_document_id", "string (uuid)", "The generated file."],
            ["sha256", "string", "Its content hash."],
            ["mime", "string", "Its media type."],
            ["storage_url", "string or null", "Where it was stored."],
            ["replay", "boolean",
             "`true` = nothing new was produced; you are seeing the earlier "
             "export again. Do not report it as a second export."],
            ["exported_at", "string (date-time) or null",
             "On a replay this is the FIRST export's timestamp, unchanged."],
        ],
        "example": {"bom_id": _BOM_ID, "status": "exported",
                    "export_document_id": _DOC_ID, "sha256": "4a1b…",
                    "mime": "application/pdf",
                    "storage_url": "s3://kairox/boms/b0b0…/bom.pdf",
                    "replay": False, "exported_at": "2026-09-23T10:20:00Z"},
    },

    "GET /api/v1/procurement/order-styles/{order_style_id}/bom": {
        "note": "Poll this after `POST …/generate-bom`. While the job is still "
                "running, `status` is `not_started` (or a queue state) and `bom` "
                "is `null`.",
        "fields": [
            ["order_style_id", "string (uuid)", "The style you asked about."],
            ["status", "string", "`not_started` | `ready` (or a queue state)."],
            ["bom", "object or null", "The BOM, once it is ready."],
        ],
        "example": {"order_style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                    "status": "ready", "bom": _BOM_VIEW},
    },

    "GET /api/v1/procurement/notifications": {
        "note": "This login's notifications, newest first. `unread_only=true` "
                "narrows it to the ones nobody has opened.",
        "fields": [
            ["[].id", "string (uuid)", "The notification."],
            ["[].kind", "string", "What it is about — for example a BOM review."],
            ["[].bom_id", "string (uuid) or null", "The BOM it points at."],
            ["[].message", "string", "Ready to display."],
            ["[].created_at", "string (date-time)", "When it was raised."],
            ["[].opened_at", "string (date-time) or null",
             "`null` means unread. An unread notice is what the escalation "
             "sweeper chases."],
            ["[].escalated_at", "string (date-time) or null",
             "Set once it has been escalated by email."],
        ],
        "example": [{"id": "e1f10101-0000-4a11-9f00-000000000001",
                     "kind": "BOM_REVIEW", "bom_id": _BOM_ID,
                     "message": "CLERMONT's BOM is waiting for your approval.",
                     "created_at": "2026-09-23T09:20:00Z",
                     "opened_at": None, "escalated_at": None}],
    },

    "GET /api/v1/procurement/notifications/stream": {
        "note": "**Not JSON.** A Server-Sent-Events stream "
                "(`text/event-stream`) that pushes a line whenever a "
                "notification is raised for this login. Read it with "
                "`EventSource`; each `data:` line is one notification in the "
                "shape above.",
        "example": {
            "_format": "text/event-stream — the line below is literal",
            "_lines": [
                "data: {\"id\": \"e1f1…\", \"kind\": \"BOM_REVIEW\", "
                "\"bom_id\": \"b0b0…\", \"message\": \"…\"}",
            ],
        },
    },

    "POST /api/v1/procurement/notifications/{notification_id}/open": {
        "note": "Marks it read, which is what stops the escalation sweeper "
                "chasing it.",
        "fields": [
            ["id", "string (uuid)", "The notification."],
            ["opened_at", "string (date-time)", "Now."],
        ],
        "example": {"id": "e1f10101-0000-4a11-9f00-000000000001",
                    "opened_at": "2026-09-23T09:31:00Z"},
    },

    # ═════════════════════════════════════════════════════════════════════════
    # PROCUREMENT — Stage 4 inventory
    # ═════════════════════════════════════════════════════════════════════════
    "POST /api/v1/procurement/inventory/preview": {
        "note": "Parses the warehouse spreadsheet and says what WOULD be kept, "
                "dropped and merged. **Nothing is written.**",
        "fields": _INV_PREVIEW_FIELDS,
        "example": _INV_PREVIEW,
    },

    "POST /api/v1/procurement/inventory/commit": {
        "note": "The same parse, written to the inventory master. Same body as "
                "the preview.",
        "fields": _INV_PREVIEW_FIELDS,
        "example": _INV_PREVIEW,
    },

    "GET /api/v1/procurement/inventory/items": {
        "note": "The inventory master — what the check is run against.",
        "fields": [
            ["items[].id", "string (uuid)", "The item."],
            ["items[].description", "string", "As the warehouse writes it."],
            ["items[].normalized_key", "string",
             "The cleaned key the matcher uses."],
            ["items[].uom", "string", "Unit of measure."],
            ["items[].qty_on_hand", "number or null", "On the shelf."],
            ["items[].rate", "number or null", "Unit rate, used for shortfall value."],
            ["items[].color", "string or null", "Colour."],
            ["items[].is_active", "boolean", "Inactive items are not matched."],
            ["count", "integer", "Rows returned."],
        ],
        "example": {"items": [{"id": "c0ffee11-0000-4a11-9f00-000000000001",
                               "description": "GOAT SUEDE DARK BROWN 1.0MM",
                               "normalized_key": "goat suede dark brown 1.0mm",
                               "uom": "dcm", "qty_on_hand": 18400.0,
                               "rate": 3.4, "color": "DARK BROWN",
                               "is_active": True}],
                    "count": 1},
    },

    "POST /api/v1/procurement/boms/{bom_id}/inventory-check": {
        "note": "**Runs the check** — matches every BOM line against the "
                "inventory master and reserves what it can. Returns the whole "
                "result.",
        "fields": _CHECK_FIELDS,
        "example": _CHECK_VIEW,
    },

    "GET /api/v1/procurement/boms/{bom_id}/inventory-check": {
        "note": "Re-reads the LAST check for this BOM. **Nothing is "
                "recomputed** — this is what the check said when it ran.",
        "fields": _CHECK_FIELDS,
        "example": _CHECK_VIEW,
    },

    "GET /api/v1/procurement/inventory-checks/{check_id}": {
        "note": "One stored check by its own id. Nothing is recomputed.",
        "fields": _CHECK_FIELDS,
        "example": _CHECK_VIEW,
    },

    "GET /api/v1/procurement/inventory-checks": {
        "note": "The Stage-4 dashboard: every checked BOM grouped "
                "client → order → style, each with its worst-line badge.",
        "fields": [
            ["clients[].client_id", "string (uuid) or null", "The buyer."],
            ["clients[].client_name", "string", "Their name."],
            ["clients[].orders[].client_order_id", "string (uuid)", "The order."],
            ["clients[].orders[].order_number", "string", "Its number."],
            ["clients[].orders[].styles[].style_id", "string (uuid)", "The style."],
            ["clients[].orders[].styles[].style_name", "string", "Its name."],
            ["clients[].orders[].styles[].bom_id", "string (uuid)", "Its BOM."],
            ["clients[].orders[].styles[].inventory_check_id", "string (uuid)",
             "The check to open."],
            ["clients[].orders[].styles[].badge", "string",
             "`sufficient` | `partial` | `out_of_stock` — the WORST line."],
            ["clients[].orders[].styles[].shortfall_lines", "integer",
             "How many lines are short."],
            ["clients[].orders[].styles[].checked_at", "string (date-time)",
             "When it was run."],
            ["totals.boms_checked", "integer", "BOMs with a check."],
            ["totals.fully_sufficient", "integer", "Fully covered by stock."],
            ["totals.with_shortfall", "integer", "Need a purchase order."],
        ],
        "example": {
            "clients": [{
                "client_id": "3f8c1b2a-0000-4a11-9f00-000000000001",
                "client_name": "BOGGI",
                "orders": [{
                    "client_order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
                    "order_number": "NB-B-1",
                    "styles": [{
                        "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                        "style_name": "CLERMONT", "bom_id": _BOM_ID,
                        "inventory_check_id":
                            "aa330044-0000-4a11-9f00-000000000001",
                        "badge": "partial", "shortfall_lines": 2,
                        "checked_at": "2026-09-23T10:40:00Z"}],
                }],
            }],
            "totals": {"boms_checked": 6, "fully_sufficient": 4,
                       "with_shortfall": 2},
        },
    },

    # ═════════════════════════════════════════════════════════════════════════
    # PROCUREMENT — Stage 5 supplier PO
    # ═════════════════════════════════════════════════════════════════════════
    "GET /api/v1/procurement/pos": {
        "note": "Purchase orders, filterable by `status`, `needs_supplier`, "
                "`bom_id` and `supplier_id`.",
        "fields": [["purchase_orders[]", "array of object",
                    "Each one is the full PO shape below."],
                   ["count", "integer", "Rows returned."]],
        "example": {"purchase_orders": [_PO_VIEW], "count": 1},
    },

    "GET /api/v1/procurement/pos/{po_id}": {
        "note": "One purchase order in full, with its lines, its supplier and "
                "its delivery tracking.",
        "fields": _PO_FIELDS,
        "example": _PO_VIEW,
    },

    "PATCH /api/v1/procurement/pos/{po_id}/items": {
        "note": "The edited PO, same shape, `revision` incremented. A stale "
                "`base_revision` is a **409** carrying `current_revision`; a PO "
                "that has already been sent is a **409** `po_locked` — cancel and "
                "re-issue.",
        "fields": _PO_FIELDS,
        "example": dict(_PO_VIEW, revision=2),
    },
    "POST /api/v1/procurement/pos/{po_id}/submit": {
        "note": "Sends the PO for approval. Same shape as `GET /pos/{po_id}`.",
        "fields": _PO_FIELDS,
        "example": dict(_PO_VIEW, status="pending_approval"),
    },
    "POST /api/v1/procurement/pos/{po_id}/approve": {
        "note": "Approves it. Same shape, `approved_at` set.",
        "fields": _PO_FIELDS,
        "example": dict(_PO_VIEW, status="approved",
                        approved_at="2026-09-23T11:00:00Z"),
    },
    "POST /api/v1/procurement/pos/{po_id}/reject": {
        "note": "Rejects it. `reason` is required and comes back on the PO.",
        "fields": _PO_FIELDS,
        "example": dict(_PO_VIEW, status="rejected",
                        rejected_at="2026-09-23T11:05:00Z",
                        rejection_reason="Rate is above the last quote."),
    },
    "POST /api/v1/procurement/pos/{po_id}/send": {
        "note": "Emails the PO to the supplier and starts the escalation clock. "
                "`sent_at`, `next_escalation_at` and `pdf_document_id` are set.",
        "fields": _PO_FIELDS,
        "example": dict(_PO_VIEW, status="sent",
                        sent_at="2026-09-23T11:10:00Z",
                        next_escalation_at="2026-09-24T11:10:00Z",
                        pdf_document_id="d7a90099-0000-4a11-9f00-000000000002"),
    },
    "POST /api/v1/procurement/pos/{po_id}/cancel": {
        "note": "Cancels it. A sent PO is cancelled and re-issued rather than "
                "edited.",
        "fields": _PO_FIELDS,
        "example": dict(_PO_VIEW, status="cancelled"),
    },
    "POST /api/v1/procurement/pos/{po_id}/acknowledge": {
        "note": "Records the supplier's confirmation — which channel it came "
                "through, and when. This is what stops the escalation ladder.",
        "fields": _PO_FIELDS,
        "example": dict(_PO_VIEW, status="confirmed",
                        acknowledged_at="2026-09-24T08:02:00Z",
                        acknowledged_channel="whatsapp"),
    },
    "POST /api/v1/procurement/boms/{bom_id}/generate-pos": {
        "note": "Turns the BOM's shortfall into draft purchase orders, one per "
                "matched supplier. A line whose supplier could not be matched "
                "produces a PO with `needs_supplier: true`.",
        "fields": [["purchase_orders[]", "array of object", "The POs created."],
                   ["count", "integer", "How many."]],
        "example": {"purchase_orders": [dict(_PO_VIEW, status="draft")],
                    "count": 1},
    },

    "GET /api/v1/procurement/suppliers": {
        "note": "The supplier directory.",
        "fields": _SUPPLIER_FIELDS + [["count", "integer", "Rows returned."]],
        "example": {"suppliers": [_SUPPLIER_BLOCK], "count": 1},
    },
    "GET /api/v1/procurement/suppliers/{supplier_id}": {
        "note": "One supplier, **plus what they have historically sold us** and "
                "their open POs. `supply_history` is what the automatic "
                "supplier match is built on.",
        "fields": [[f[0].replace("suppliers[].", ""), f[1], f[2]]
                   for f in _SUPPLIER_FIELDS] + [
            ["supply_history[].normalized_description", "string",
             "The article, normalised."],
            ["supply_history[].raw_description", "string", "As they wrote it."],
            ["supply_history[].mode", "string", "How it was supplied."],
            ["supply_history[].uom", "string", "Unit."],
            ["supply_history[].txn_count", "integer", "How many times."],
            ["supply_history[].last_purchased_at", "string (date-time) or null",
             "Most recent purchase."],
            ["supply_history[].last_rate", "number or null", "Last rate paid."],
            ["supply_history[].min_rate", "number or null", "Cheapest ever."],
            ["supply_history[].max_rate", "number or null", "Dearest ever."],
            ["open_pos[].id", "string (uuid)", "An open PO."],
            ["open_pos[].po_number", "string", "Its number."],
            ["open_pos[].status", "string", "Its state."],
            ["open_pos[].total", "number or null", "Its value."],
        ],
        "example": dict(
            _SUPPLIER_BLOCK,
            supply_history=[{"normalized_description": "goat suede dark brown",
                             "raw_description": "GOAT SUEDE D/BROWN",
                             "mode": "purchase", "uom": "dcm", "txn_count": 14,
                             "last_purchased_at": "2026-08-12T00:00:00Z",
                             "last_rate": 3.4, "min_rate": 3.1,
                             "max_rate": 3.8}],
            open_pos=[{"id": _PO_ID, "po_number": "PO-2026-0041",
                       "status": "sent", "total": 51000.0}]),
    },
    "POST /api/v1/procurement/suppliers": {
        "note": "Creates a supplier. A duplicate name is a **409**.",
        "fields": [[f[0].replace("suppliers[].", ""), f[1], f[2]]
                   for f in _SUPPLIER_FIELDS],
        "example": _SUPPLIER_BLOCK,
    },
    "PATCH /api/v1/procurement/suppliers/{supplier_id}": {
        "note": "The supplier after the edit.",
        "fields": [[f[0].replace("suppliers[].", ""), f[1], f[2]]
                   for f in _SUPPLIER_FIELDS],
        "example": _SUPPLIER_BLOCK,
    },
    "DELETE /api/v1/procurement/suppliers/{supplier_id}": {
        "note": "**Deactivates** — never deletes. Purchase history hangs off the "
                "row. `is_active` comes back `false`.",
        "fields": [[f[0].replace("suppliers[].", ""), f[1], f[2]]
                   for f in _SUPPLIER_FIELDS],
        "example": dict(_SUPPLIER_BLOCK, is_active=False),
    },
    "POST /api/v1/procurement/suppliers/{supplier_id}/reactivate": {
        "note": "Puts a deactivated supplier back in service.",
        "fields": [[f[0].replace("suppliers[].", ""), f[1], f[2]]
                   for f in _SUPPLIER_FIELDS],
        "example": _SUPPLIER_BLOCK,
    },

    "POST /api/v1/procurement/webhooks/ses": {
        "note": "Called by **Amazon SES**, not by your frontend. Always answers "
                "`{\"ok\": true}` so the provider does not retry.",
        "example": {"ok": True},
    },
    "POST /api/v1/procurement/webhooks/twilio/whatsapp": {
        "note": "Called by **Twilio**, not by your frontend. A supplier's "
                "WhatsApp reply arrives here and can acknowledge a PO.",
        "example": {"ok": True},
    },
    "POST /api/v1/procurement/webhooks/twilio/voice": {
        "note": "Called by **Twilio**, not by your frontend.",
        "example": {"ok": True},
    },

    # ═════════════════════════════════════════════════════════════════════════
    # PROCUREMENT — Stage 5 supplier import, tracking board, patterns, admin
    # ═════════════════════════════════════════════════════════════════════════
    "POST /api/v1/procurement/suppliers/import/preview": {
        "note": "Parses the supplier workbook and says what it found. "
                "**Nothing is written.**",
        "fields": _SUPPLIER_IMPORT_FIELDS,
        "example": _SUPPLIER_IMPORT,
    },
    "POST /api/v1/procurement/suppliers/import/commit": {
        "note": "The same parse, written. Adds three fields to the preview body.",
        "fields": _SUPPLIER_IMPORT_FIELDS + [
            ["committed", "integer", "Suppliers created or updated."],
            ["history_rows", "integer", "Purchase-history rows written."],
            ["deactivated", "integer",
             "Suppliers absent from the sheet that were switched off."],
        ],
        "example": dict(_SUPPLIER_IMPORT, committed=126, history_rows=4180,
                        deactivated=3),
    },

    "GET /api/v1/procurement/production-tracking": {
        "note": "**The production board** — one row per style, tying Stage 5 back "
                "to the floor: how many POs it needs, how many the suppliers have "
                "confirmed, and whether the material is ready.",
        "fields": [
            ["trackers[].id", "string (uuid)", "The tracker row."],
            ["trackers[].client_order_id", "string (uuid)", "The order."],
            ["trackers[].order_number", "string or null", "Its number."],
            ["trackers[].client_name", "string or null", "The buyer."],
            ["trackers[].style_id", "string (uuid)", "The style."],
            ["trackers[].style_name", "string or null", "Its name."],
            ["trackers[].bom_id", "string (uuid) or null", "Its BOM."],
            ["trackers[].status", "string", "Where this style stands."],
            ["trackers[].po_count", "integer", "Purchase orders raised."],
            ["trackers[].po_confirmed_count", "integer",
             "How many suppliers have acknowledged. When this equals `po_count` "
             "the material is on its way."],
            ["trackers[].material_ready_at", "string (date-time) or null",
             "When everything needed had arrived."],
            ["trackers[].released_at", "string (date-time) or null",
             "When it was released to the floor."],
            ["count", "integer", "Rows returned."],
        ],
        "example": {
            "trackers": [{
                "id": "cc110022-0000-4a11-9f00-000000000001",
                "client_order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
                "order_number": "NB-B-1", "client_name": "BOGGI",
                "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                "style_name": "CLERMONT", "bom_id": _BOM_ID,
                "status": "AWAITING_MATERIAL", "po_count": 3,
                "po_confirmed_count": 2,
                "material_ready_at": None, "released_at": None}],
            "count": 1,
        },
    },
    "POST /api/v1/procurement/production-tracking/{tracking_id}/transition": {
        "note": "Moves one board row to its next state. The reply is just the "
                "row's id and its new status.",
        "fields": [["id", "string (uuid)", "The tracker row."],
                   ["status", "string", "Its new status."]],
        "example": {"id": "cc110022-0000-4a11-9f00-000000000001",
                    "status": "MATERIAL_READY"},
    },

    "POST /api/v1/procurement/patterns": {
        "note": "Uploads a DXF pattern. **Answers 202, not 200** — the parse runs "
                "as a background job. Poll the BOM, or listen on `channel`.",
        "fields": [
            ["job_id", "string", "The background job."],
            ["channel", "string",
             "`pattern:<style_signature>` — the channel the job publishes to."],
        ],
        "example": {"job_id": "f81d4fae-7dec-11d0-a765-00a0c91e6bf6",
                    "channel": "pattern:CLERMONT-GOAT_SUEDE"},
    },
    "GET /api/v1/procurement/patterns": {
        "note": "The stored DXF patterns. `is_current` marks the one a BOM "
                "generation would use.",
        "fields": [
            ["[].id", "string (uuid)", "The pattern."],
            ["[].style_signature", "string", "What it is the pattern for."],
            ["[].client_id", "string (uuid) or null", "Scoped to a client, if any."],
            ["[].is_current", "boolean", "The version in use."],
            ["[].n_pieces", "integer or null", "Pattern pieces found in the file."],
            ["[].sha256", "string", "Content hash — a re-upload is recognised."],
            ["[].created_at", "string (date-time)", "When it was uploaded."],
        ],
        "example": [{"id": "ab120033-0000-4a11-9f00-000000000001",
                     "style_signature": "CLERMONT-GOAT_SUEDE",
                     "client_id": "3f8c1b2a-0000-4a11-9f00-000000000001",
                     "is_current": True, "n_pieces": 34, "sha256": "7c1a…",
                     "created_at": "2026-09-20T12:00:00Z"}],
    },

    "GET /api/v1/procurement/admin/cost-catalog": {
        "note": "The standing cost catalogue, keyed by garment code. The key "
                "`_default` is the fallback used when a garment has no entry.",
        "fields": [["<garment_code>", "array of object",
                    "The cost lines for that garment: category, name, uom, and "
                    "the standing rate."]],
        "example": {"_default": [{"category": "ACCESSORY", "name": "THREAD",
                                  "uom": "mtrs", "unit_price": 0.4}],
                    "JACKET": [{"category": "LEATHER", "name": "GOAT SUEDE",
                                "uom": "dcm", "unit_price": 3.4}]},
    },
    "PUT /api/v1/procurement/admin/cost-catalog/{garment_code}": {
        "note": "Replaces that garment's cost lines. Confirms what was written.",
        "fields": [["garment_code", "string", "Upper-cased."],
                   ["lines", "integer", "How many lines were stored."]],
        "example": {"garment_code": "JACKET", "lines": 7},
    },
    "GET /api/v1/procurement/admin/checks": {
        "note": "The per-client BOM validation rules. `run_checks` filters this "
                "list by `client_code`.",
        "fields": [["[].client_code", "string", "Which client the rules are for."],
                   ["[].checks", "array of object",
                    "The rules: `id`, `kind`, `severity`, `field`, `range` …"]],
        "example": [{"client_code": "BOGGI",
                     "checks": [{"id": "dcm_band", "kind": "range",
                                 "severity": "warn", "field": "qty_per_garment",
                                 "range": [8, 22]}]}],
    },
    "PUT /api/v1/procurement/admin/checks/{client_code}": {
        "note": "Replaces that client's rules. Confirms what was written.",
        "fields": [["client_code", "string", "The client."],
                   ["rules", "integer", "How many rules were stored."]],
        "example": {"client_code": "BOGGI", "rules": 4},
    },
    "PUT /api/v1/procurement/admin/dxf-yields/{species}": {
        "note": "Sets the yield factor used when consumption is derived from a "
                "DXF pattern for that leather species.",
        "fields": [["species", "string", "The leather species."],
                   ["factor", "number", "The stored factor."]],
        "example": {"species": "GOAT", "factor": 1.18},
    },
    "POST /api/v1/procurement/admin/fabric-roles": {
        "note": "Upserts one fabric-role mapping. **Echoes back exactly what you "
                "sent**, now stored.",
        "fields": [["label", "string", "The term as it appears on a spec sheet."],
                   ["role", "string", "What that term means — shell, lining …"],
                   ["category", "string", "Which BOM category it belongs to."],
                   ["is_leather", "boolean", "Whether it is leather."]],
        "example": {"label": "NYLON LINING", "role": "lining",
                    "category": "LINING", "is_leather": False},
    },
    "GET /api/v1/procurement/admin/pom-dictionary": {
        "note": "The measurement-term dictionary. **Read `status`** — it is how "
                "you tell an LLM suggestion still awaiting review from a "
                "confirmed mapping.",
        "fields": [
            ["[].source_term", "string", "The term as it appears on a spec sheet."],
            ["[].pom_code", "string", "The canonical measurement code."],
            ["[].language", "string", "Language of the source term."],
            ["[].garment_type_id", "string (uuid) or null",
             "Scoped to one garment type, if any."],
            ["[].weight", "integer or null", "Match weight."],
            ["[].status", "string",
             "Confirmed, or LLM-suggested and awaiting review."],
            ["[].confidence", "number or null", "0–1, for a suggestion."],
        ],
        "example": [{"source_term": "lunghezza manica", "pom_code": "SLV_LEN",
                     "language": "it", "garment_type_id": None, "weight": 1,
                     "status": "CONFIRMED", "confidence": None}],
    },
    "POST /api/v1/procurement/admin/pom-dictionary": {
        "note": "Adds or updates one mapping. Confirms what was stored.",
        "fields": [["source_term", "string", "The term."],
                   ["pom_code", "string", "The code it maps to."],
                   ["language", "string", "Its language."],
                   ["garment_type_id", "string (uuid) or null", "Scope."]],
        "example": {"source_term": "lunghezza manica", "pom_code": "SLV_LEN",
                    "language": "it", "garment_type_id": None},
    },

    "GET /api/v1/procurement/t/o/{token}.gif": {
        "note": "**Not for your frontend.** A 1×1 tracking pixel embedded in the "
                "PO email. Fetching it stamps `first_opened_at` on the PO. It "
                "returns an `image/gif`, not JSON, and always succeeds — an "
                "unknown token still returns the pixel, so a supplier's mail "
                "client never shows a broken image.",
        "example": {"_format": "image/gif — a 1×1 transparent pixel, no JSON"},
    },
    "GET /api/v1/procurement/t/c/{token}": {
        "note": "**Not for your frontend.** The click-tracking link in the PO "
                "email. It stamps `first_clicked_at` and answers **302**, "
                "redirecting the supplier to the real destination.",
        "example": {"_format": "302 redirect — the Location header carries the "
                               "real URL; there is no body"},
    },

    # ═════════════════════════════════════════════════════════════════════════
    # SYSTEM
    # ═════════════════════════════════════════════════════════════════════════
    "GET /": {
        "note": "A pointer, nothing more. No token needed.",
        "fields": [["message", "string", "Welcome line."],
                   ["docs", "string", "Where the interactive docs are."],
                   ["health", "string", "Where the liveness probe is."]],
        "example": {"message": "Welcome to KairoX", "docs": "/docs",
                    "health": "/health"},
    },
    "GET /health": {
        "note": "**Liveness.** Is the process up? It checks nothing external — "
                "deliberately, so a transient database blip does not make an "
                "orchestrator restart a healthy process. No token needed.",
        "fields": [["status", "string", "Always `healthy` when it answers."],
                   ["app", "string", "The application name."],
                   ["version", "string", "The application version."]],
        "example": {"status": "healthy", "app": "KairoX", "version": "1.0.0"},
    },
    "GET /ready": {
        "note": "**Readiness.** Can this replica actually serve? It runs a "
                "trivial query, so a pool-exhausted or database-unreachable "
                "replica answers **503** `{\"status\": \"not_ready\"}` and is "
                "taken out of rotation instead of looking healthy. No token "
                "needed.",
        "fields": [["status", "string", "`ready`, or `not_ready` with a 503."]],
        "example": {"status": "ready"},
    },

    "POST /api/v1/chat/stream": {
        "note": "**Not JSON.** This is a Server-Sent-Events stream "
                "(`text/event-stream`) for a live-typing UI. Read it with "
                "`EventSource` or a streaming fetch, one `data:` line at a time. "
                "Every line but the last carries a `delta`; the last carries "
                "`done: true` plus the same `tool` and `data` the non-streaming "
                "`POST /chat` would have returned.",
        "fields": [
            ["delta", "string", "The next chunk of the answer. Append it."],
            ["done", "boolean", "Present only on the final line."],
            ["tool", "string or null", "On the final line — which tool answered."],
            ["data", "object or null",
             "On the final line — the structured result behind the answer."],
        ],
        "example": {
            "_format": "text/event-stream — the lines below are literal",
            "_lines": [
                "data: {\"delta\": \"152 \"}",
                "data: {\"delta\": \"pieces \"}",
                "data: {\"delta\": \"are \"}",
                "data: {\"delta\": \"in \"}",
                "data: {\"delta\": \"the \"}",
                "data: {\"delta\": \"store. \"}",
                "data: {\"done\": true, \"tool\": \"store_summary\", "
                "\"data\": {\"in_store\": 152}}",
            ],
        },
    },

    # ═════════════════════════════════════════════════════════════════════════
    # ANALYTICS  (read-only; this module owns no tables)
    # ═════════════════════════════════════════════════════════════════════════
    "GET /api/v1/analytics/overview": {
        "note": "**REMOVED.** Always **410 Gone**. Factory-wide figures live on "
                "the role dashboards: `GET /dashboard/direct-manager` (whole "
                "factory), or `/dashboard/cutting` | `/lining` | `/stitching` | "
                "`/store`.",
        "example": {
            "detail": "The Analytics Overview screen has been removed. "
                      "Factory-wide figures live on the role dashboards: "
                      "GET /api/v1/dashboard/direct-manager (whole factory), or "
                      "/dashboard/cutting | /lining | /stitching | /store.",
            "request_id": "0f2c7f0e-2b7a-4f1b-9a5a-9f0b3f0c7e11",
        },
    },

    "GET /api/v1/analytics/alerts/stage-spread": {
        "note": "**REMOVED.** Always **410 Gone**. Use `GET /dashboard/alerts`, "
                "which every manager role may read.",
        "example": {
            "detail": "The standalone Risk Alerts screen has been removed. "
                      "Bottleneck and alerts are now on "
                      "GET /api/v1/dashboard/alerts, which every manager role "
                      "may read.",
            "request_id": "0f2c7f0e-2b7a-4f1b-9a5a-9f0b3f0c7e11",
        },
    },

    "GET /api/v1/analytics/alerts/freight-risk": {
        "note": "**REMOVED.** Always **410 Gone**. Use `GET /dashboard/alerts`.",
        "example": {
            "detail": "The standalone Risk Alerts screen has been removed. "
                      "Bottleneck and alerts are now on "
                      "GET /api/v1/dashboard/alerts, which every manager role "
                      "may read.",
            "request_id": "0f2c7f0e-2b7a-4f1b-9a5a-9f0b3f0c7e11",
        },
    },

    "GET /api/v1/analytics/explorer": {
        "note": "The left-panel navigation tree: client → order → style → piece. "
                "**The piece leaves are capped** at `pieces_per_style`; the "
                "style's true `piece_count` is always reported.",
        "fields": [
            ["clients[].client_id", "string (uuid)", "The buyer."],
            ["clients[].client_name", "string", "Their name."],
            ["clients[].order_count", "integer", "Orders under them."],
            ["clients[].orders[].order_id", "string (uuid)", "The order."],
            ["clients[].orders[].order_number", "string", "Its number."],
            ["clients[].orders[].piece_count", "integer",
             "Garments across its styles."],
            ["clients[].orders[].styles[].style_id", "string (uuid)", "The style."],
            ["clients[].orders[].styles[].style_name", "string", "Its name."],
            ["clients[].orders[].styles[].article", "string or null", "Its article."],
            ["clients[].orders[].styles[].piece_count", "integer",
             "The style's REAL piece count, not the number of leaves below."],
            ["clients[].orders[].styles[].pieces_truncated", "boolean",
             "`true` when leaves were cut off by `pieces_per_style`."],
            ["clients[].orders[].styles[].pieces[]", "array of object",
             "Up to `pieces_per_style` leaves. Absent when "
             "`include_pieces=false`."],
        ],
        "example": {
            "clients": [{
                "client_id": "3f8c1b2a-0000-4a11-9f00-000000000001",
                "client_name": "BOGGI", "order_count": 1,
                "orders": [{
                    "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
                    "order_number": "NB-B-1", "piece_count": 152,
                    "styles": [{
                        "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                        "style_name": "CLERMONT", "article": "GOAT SUEDE",
                        "piece_count": 152, "pieces_truncated": True,
                        "pieces": [{
                            "piece_id": "f5e10077-0000-4a11-9f00-000000000001",
                            "piece_code":
                                "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-005",
                            "seq": 5, "current_stage": "FUSING"}],
                    }],
                }],
            }],
        },
    },

    "GET /api/v1/analytics/orders/{order_id}/tree": {
        "note": "The ORDER level of the stage spreadsheet: its totals, its "
                "per-stage progress, where its garments are in the store, and "
                "the style list to drill into next.",
        "fields": [
            ["order_id", "string (uuid)", "The order."],
            ["order_number", "string", "Its number."],
            ["client", "string", "The buyer."],
            ["delivery_deadline", "string (date) or null", "Ship-by date."],
            ["totals.qty_ordered", "integer", "Garments ordered."],
            ["totals.minted_pieces", "integer", "Barcodes created."],
            ["totals.completed", "integer", "Through the final stage."],
            ["totals.balance", "integer",
             "`qty_ordered − completed`. Measured against what was ORDERED, not "
             "what was minted — a style nobody has released yet is still owed."],
            ["totals.completion_pct", "number", "Percent complete."],
            ["totals.not_minted", "integer", "Ordered but never released."],
            ["stages[].stage", "string", "Stage code."],
            ["stages[].label", "string", "Readable label."],
            ["stages[].total", "integer", "The denominator — the whole scope."],
            ["stages[].completed", "integer", "Pieces that have cleared it."],
            ["stages[].pending", "integer",
             "`total − completed` — the BALANCE at that stage, which is what the "
             "floor means. Deliberately not queue depth."],
            ["stages[].pct", "number", "Percent cleared."],
            ["store.buckets[]", "array of object", "`{state, label, pieces}`."],
            ["store.holding_leather", "integer", "Holding leather only."],
            ["store.holding_lining", "integer", "Holding lining only."],
            ["store.holding_both", "integer", "Holding both."],
            ["store.received", "integer", "Confirmed complete."],
            ["store.sended", "integer", "Released to line-stitching."],
            ["store.in_store", "integer", "Holding anything, plus received."],
            ["store.awaiting_parts", "integer", "Expected but nothing arrived."],
            ["store.no_drawer", "integer",
             "Always 0. There are no drawers; kept so nothing reading the key "
             "breaks."],
            ["style_count", "integer", "Styles on the order."],
            ["styles[].style_id", "string (uuid)", "The style — what to fetch next."],
            ["styles[].style_code", "string or null", "What a human recognises."],
            ["styles[].style_name", "string", "Its name."],
            ["styles[].article", "string or null", "Its article."],
            ["styles[].production_status", "string", "DRAFT / RELEASED / CANCELLED."],
            ["styles[].needs_lining", "boolean or null", "Its lining answer."],
            ["styles[].qty_ordered", "integer", "Ordered."],
            ["styles[].completed", "integer", "Finished."],
            ["styles[].balance", "integer", "Still owed."],
            ["styles[].completion_pct", "number", "Percent."],
            ["styles[].piece_count", "integer", "Barcodes minted."],
            ["styles[].stage_counts", "object", "`{stage: pieces}` right now."],
        ],
        "example": {
            "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
            "order_number": "NB-B-1", "client": "BOGGI",
            "delivery_deadline": "2026-11-30",
            "totals": {"qty_ordered": 840, "minted_pieces": 152,
                       "completed": 40, "balance": 800,
                       "completion_pct": 4.8, "not_minted": 688},
            "stages": [{"stage": "LEATHER_CUTTING", "label": "Leather Cutting",
                        "total": 152, "completed": 120, "pending": 32,
                        "pct": 78.9}],
            "store": {"buckets": [{"state": "holding_both",
                                   "label": "Holding both", "pieces": 12}],
                      "holding_leather": 4, "holding_lining": 2,
                      "holding_both": 12, "received": 6, "sended": 40,
                      "in_store": 24, "awaiting_parts": 3, "no_drawer": 0},
            "style_count": 1,
            "styles": [{"style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                        "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
                        "style_name": "CLERMONT", "article": "GOAT SUEDE",
                        "production_status": "RELEASED", "needs_lining": True,
                        "qty_ordered": 152, "completed": 40, "balance": 112,
                        "completion_pct": 26.3, "piece_count": 152,
                        "stage_counts": {"FUSING": 60, "PASTING": 52}}],
        },
    },

    "GET /api/v1/analytics/styles/{style_id}/detail": {
        "note": "One style, with its pieces and each piece's full stage history. "
                "**PAGED** — events are fetched only for the pieces on the page, "
                "while `totals`, `stages` and `store` stay whole-style.",
        "fields": [
            ["style_id", "string (uuid)", "The style."],
            ["style_code", "string or null", "Its code."],
            ["style_name", "string", "Its name."],
            ["article", "string or null", "Its article."],
            ["order_id", "string (uuid)", "Its order."],
            ["order_number", "string", "The order number."],
            ["client", "string", "The buyer."],
            ["production_status", "string", "DRAFT / RELEASED / CANCELLED."],
            ["needs_lining", "boolean or null", "Its lining answer."],
            ["totals", "object", "Same block as the order tree."],
            ["stages", "array of object", "Same block as the order tree."],
            ["store", "object", "Same block as the order tree."],
            ["piece_count", "integer",
             "The STYLE's piece count, not the page's."],
            ["pieces[]", "array of object",
             "This page's garments, each with its stage history."],
            ["limit", "integer or null", "Page size."],
            ["offset", "integer or null", "Rows skipped."],
            ["count", "integer", "Pieces in this page."],
            ["has_more", "boolean", "Another page follows."],
        ],
        "example": {
            "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
            "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
            "style_name": "CLERMONT", "article": "GOAT SUEDE",
            "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
            "order_number": "NB-B-1", "client": "BOGGI",
            "production_status": "RELEASED", "needs_lining": True,
            "totals": {"qty_ordered": 152, "minted_pieces": 152,
                       "completed": 40, "balance": 112,
                       "completion_pct": 26.3, "not_minted": 0},
            "stages": [{"stage": "FUSING", "label": "Fusing", "total": 152,
                        "completed": 60, "pending": 92, "pct": 39.5}],
            "store": {"buckets": [], "holding_leather": 4, "holding_lining": 2,
                      "holding_both": 12, "received": 6, "sended": 40,
                      "in_store": 24, "awaiting_parts": 3, "no_drawer": 0},
            "piece_count": 152,
            "pieces": [{
                "piece_id": "f5e10077-0000-4a11-9f00-000000000001",
                "piece_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-005",
                "seq": 5, "current_stage": "FUSING",
                "history": [{"stage": "LEATHER_CUTTING",
                             "employee_name": "Ramesh",
                             "work_date": "2026-09-21",
                             "leather_consumption_dcm": 412.0}],
            }],
            "limit": 50, "offset": 0, "count": 50, "has_more": True,
        },
    },

    "GET /api/v1/analytics/pieces/detail": {
        "note": "ONE garment, by `piece_code` **or** by (`sku_code` + `seq`): "
                "header, where it stands in the store, and a stage checklist.",
        "fields": [
            ["piece_id", "string (uuid)", "The garment."],
            ["piece_code", "string", "Its long identity."],
            ["bundle_id", "string", "Same as `piece_code` — a legacy alias."],
            ["seq", "integer", "Its number within the SKU."],
            ["serial", "string or null", "`seq` zero-padded."],
            ["sku_code", "string", "Its colour/size line."],
            ["sku_label", "string", "A readable label for that line."],
            ["colour", "string or null", "Colour."],
            ["size", "string", "Size."],
            ["style_id", "string (uuid)", "Its style."],
            ["style_code", "string or null", "Style code."],
            ["style_name", "string", "Style name."],
            ["article", "string or null", "Article."],
            ["order_id", "string (uuid)", "Its order."],
            ["order_number", "string", "Order number."],
            ["client", "string", "Buyer."],
            ["current_stage", "string or null", "Last stage LOGGED."],
            ["display_stage", "string or null",
             "What the screen should show. May be `STORE`."],
            ["display_label", "string or null", "Readable version."],
            ["in_store", "boolean", "It is in the store."],
            ["needs_lining", "boolean", "The effective requirement."],
            ["lining_reason", "string or null", "Why, in words."],
            ["lining_declared_on_style", "boolean or null",
             "Whether the style declared it (rather than it being inferred)."],
            ["completed", "boolean", "Past the final stage."],
            ["store.state", "string or null", "Store state."],
            ["store.holding", "string", "What it holds, in words."],
            ["store.leather_in", "boolean", "Leather is in."],
            ["store.lining_in", "boolean", "Lining is in."],
            ["store.accessories_in", "boolean", "Accessories are in."],
            ["store.received_at", "string (date-time) or null", "When received."],
            ["store.sended_at", "string (date-time) or null", "When released."],
            ["store.awaiting", "array of string",
             "`LEATHER` / `LINING` — what is still missing."],
            ["checklist[].stage", "string", "Stage code."],
            ["checklist[].state", "string", "`done` or `pending`."],
            ["checklist[].employee_name", "string or null", "Who did it."],
            ["checklist[].work_date", "string (date) or null", "When."],
        ],
        "example": {
            "piece_id": "f5e10077-0000-4a11-9f00-000000000001",
            "piece_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-005",
            "bundle_id": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-005",
            "seq": 5, "serial": "005",
            "sku_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46",
            "sku_label": "CLERMONT · DARK BROWN · 46",
            "colour": "DARK BROWN", "size": "46",
            "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
            "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
            "style_name": "CLERMONT", "article": "GOAT SUEDE",
            "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
            "order_number": "NB-B-1", "client": "BOGGI",
            "current_stage": "PASTING", "display_stage": "STORE",
            "display_label": "In store · holding leather · awaiting lining",
            "in_store": True, "needs_lining": True,
            "lining_reason": "declared on the style at release",
            "lining_declared_on_style": True, "completed": False,
            "store": {"state": "holding_leather", "holding": "leather",
                      "leather_in": True, "lining_in": False,
                      "accessories_in": False, "received_at": None,
                      "sended_at": None, "awaiting": ["LINING"]},
            "checklist": [{"stage": "LEATHER_CUTTING", "state": "done",
                           "employee_name": "Ramesh",
                           "work_date": "2026-09-21"},
                          {"stage": "LINING_CUTTING", "state": "pending",
                           "employee_name": None, "work_date": None}],
        },
    },

    "GET /api/v1/analytics/pieces/{piece_code}/story": {
        "note": "**The piece life story** — every stage this garment passed, who "
                "did it, when, whether it was a rework, and the leather recorded "
                "at the cut.",
        "fields": [
            ["piece_code", "string", "The garment."],
            ["style_name", "string", "Its style."],
            ["colour", "string or null", "Colour."],
            ["size", "string", "Size."],
            ["order_number", "string", "Its order."],
            ["client", "string", "The buyer."],
            ["current_stage", "string or null", "The last stage logged."],
            ["store_state", "string or null", "Where it stands in the store."],
            ["awaiting", "string or null",
             "One sentence: what it is waiting for — for example `in the store, "
             "awaiting lining`. `null` when it is not waiting on anything named."],
            ["stages[].stage", "string", "Stage code."],
            ["stages[].stage_label", "string", "Readable label."],
            ["stages[].employee_name", "string or null",
             "Who did it. `null` for work done by an outside factory."],
            ["stages[].work_date", "string (date) or null", "The work date."],
            ["stages[].logged_at", "string (date-time) or null",
             "When it was recorded — which can be later than the work date."],
            ["stages[].entered_by", "string or null", "The login that recorded it."],
            ["stages[].is_rework", "boolean",
             "`true` when this stage had already been logged for this garment."],
            ["stages[].leather_consumption_dcm", "number or null",
             "Recorded at the cut. `null` everywhere else, and on a lining cut "
             "logged without a measurement."],
        ],
        "example": {
            "piece_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-005",
            "style_name": "CLERMONT", "colour": "DARK BROWN", "size": "46",
            "order_number": "NB-B-1", "client": "BOGGI",
            "current_stage": "PASTING", "store_state": "holding_leather",
            "awaiting": "in the store, awaiting lining",
            "stages": [{
                "stage": "LEATHER_CUTTING", "stage_label": "Leather cutting",
                "employee_name": "Ramesh", "work_date": "2026-09-21",
                "logged_at": "2026-09-21T11:04:00Z", "entered_by": "Kumar (CM)",
                "is_rework": False, "leather_consumption_dcm": 412.0,
            }],
        },
    },

    "GET /api/v1/analytics/consumption": {
        "note": "Leather consumed per style, **summed from the cut events "
                "themselves**. Filter by `order_id` or `style_id`.",
        "fields": [
            ["styles[].style_id", "string (uuid)", "The style."],
            ["styles[].style_name", "string", "Its name."],
            ["styles[].pieces_cut", "integer",
             "Distinct garments with a leather cut logged."],
            ["styles[].leather_consumed_dcm", "number", "Total dcm recorded."],
            ["total_consumed_dcm", "number", "Sum across the styles listed."],
        ],
        "example": {
            "styles": [{"style_id": "9c2f0011-0000-4a11-9f00-000000000001",
                        "style_name": "CLERMONT", "pieces_cut": 120,
                        "leather_consumed_dcm": 48260.0}],
            "total_consumed_dcm": 48260.0,
        },
    },

    "GET /api/v1/analytics/employee-rates": {
        "note": "A **LIVE ESTIMATE** of per-employee pieces and earnings from "
                "production events and current rates. It is NOT payroll — the "
                "payroll of record is the frozen wage run. The two legitimately "
                "differ mid-period.",
        "fields": [
            ["start", "string (date)", "Window start (required)."],
            ["end", "string (date)", "Window end (required)."],
            ["employees[].employee_id", "string (uuid)", "The worker."],
            ["employees[].name", "string", "Their name."],
            ["employees[].total_pieces", "integer", "Pieces in the window."],
            ["employees[].total_amount", "number", "Estimated earnings."],
            ["employees[].unrated_pieces", "integer",
             "Pieces at an operation with no rate — they earned nothing."],
            ["employees[].lines[]", "array of object",
             "Per style × stage: pieces, rate and amount."],
            ["total_pieces", "integer", "Across every employee."],
            ["total_amount", "number", "Across every employee."],
            ["note", "string",
             "Says in words that this is an estimate and points at "
             "`GET /wages/runs/{id}`."],
        ],
        "example": {
            "start": "2026-09-01", "end": "2026-09-15",
            "employees": [{
                "employee_id": "aa220088-0000-4a11-9f00-000000000001",
                "name": "Ramesh", "total_pieces": 240, "total_amount": 3000.0,
                "unrated_pieces": 0,
                "lines": [{"style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
                           "stage": "LEATHER_CUTTING", "pieces": 240,
                           "rate": 12.5, "amount": 3000.0,
                           "unrated_pieces": 0}],
            }],
            "total_pieces": 240, "total_amount": 3000.0,
            "note": "Live estimate from production events and current rates. "
                    "Payroll of record is GET /wages/runs/{id}.",
        },
    },

    # ═════════════════════════════════════════════════════════════════════════
    # PRODUCTION
    # ═════════════════════════════════════════════════════════════════════════
    "GET /api/v1/production/skus/{sku_id}/pieces": {
        "note": "The scan checklist for one SKU: every piece, the stage it is at, "
                "and whether it may be scanned now. The header counts are "
                "**SKU-wide**, except `blocked`, which is a page figure and says "
                "so in `blocked_scope`.",
        "fields": [
            ["sku_id", "string (uuid)", "The colour/size line."],
            ["sku_code", "string", "Its code."],
            ["colour", "string or null", "Colour name, else colour code."],
            ["size", "string", "Size."],
            ["order_id", "string (uuid) or null", "The order it belongs to."],
            ["operation_id", "string (uuid) or null",
             "The operation you filtered by, if any."],
            ["operation_code", "string or null", "That operation's code."],
            ["total", "integer", "Pieces in this SKU (whole SKU)."],
            ["done", "integer", "Done at that operation (whole SKU)."],
            ["pending", "integer", "`total − done` (whole SKU)."],
            ["blocked", "integer", "Pieces on THIS PAGE that cannot be scanned."],
            ["blocked_scope", "string", "`page` or `sku` — what `blocked` counted."],
            ["limit", "integer or null", "Page size."],
            ["offset", "integer or null", "Rows skipped."],
            ["count", "integer", "Pieces in this page."],
            ["has_more", "boolean", "Another page follows."],
            ["closed", "boolean",
             "Every piece of the SKU is done at the named operation. The style "
             "must stop being offered for scanning. Always `false` when no "
             "operation was named."],
            ["pieces[].piece_id", "string (uuid)", "The garment."],
            ["pieces[].code", "string", "Its long identity."],
            ["pieces[].seq", "integer", "Its number within the SKU."],
            ["pieces[].serial", "string or null", "`seq` zero-padded — `\"005\"`."],
            ["pieces[].event_stage", "string or null",
             "The raw last-logged stage code."],
            ["pieces[].event_stage_label", "string or null", "Its readable label."],
            ["pieces[].current_stage", "string or null",
             "What the screen should SHOW. May be `STORE`."],
            ["pieces[].current_stage_label", "string or null", "Readable version."],
            ["pieces[].in_store", "boolean", "The garment is in the store."],
            ["pieces[].store_status", "string or null", "Its store wording."],
            ["pieces[].needs_lining", "boolean",
             "The EFFECTIVE requirement (the style's declaration wins), so the "
             "lining column can be greyed out for a leather-only style."],
            ["pieces[].store.state", "string or null", "Store state."],
            ["pieces[].store.holding", "string", "What the store holds, in words."],
            ["pieces[].store.leather_in", "boolean", "Leather is in."],
            ["pieces[].store.lining_in", "boolean", "Lining is in."],
            ["pieces[].store.accessories_in", "boolean", "Accessories are in."],
            ["pieces[].done_at_op", "boolean", "Already logged at that operation."],
            ["pieces[].eligible", "boolean", "May be scanned right now."],
            ["pieces[].blocked_reason", "string or null", "Why not, in words."],
        ],
        "example": {
            "sku_id": "b4d10022-0000-4a11-9f00-000000000001",
            "sku_code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46",
            "colour": "DARK BROWN", "size": "46",
            "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
            "operation_id": "aa110066-0000-4a11-9f00-000000000001",
            "operation_code": "FUSING",
            "total": 40, "done": 12, "pending": 28,
            "blocked": 3, "blocked_scope": "page",
            "limit": 50, "offset": 0, "count": 40, "has_more": False,
            "closed": False,
            "pieces": [{
                "piece_id": "f5e10077-0000-4a11-9f00-000000000001",
                "code": "NB-B-1-CLERMONT-GOAT_SUEDE-DARK_BROWN-46-005",
                "seq": 5, "serial": "005",
                "order_id": "7a1c0e55-0000-4a11-9f00-000000000001",
                "event_stage": "LEATHER_CUTTING",
                "event_stage_label": "Leather cutting",
                "current_stage": "LEATHER_CUTTING",
                "current_stage_label": "Leather cutting",
                "in_store": False, "store_status": None,
                "needs_lining": True,
                "store": {"state": "waiting", "holding": "nothing",
                          "leather_in": False, "lining_in": False,
                          "accessories_in": False},
                "done_at_op": False, "eligible": True, "blocked_reason": None,
            }],
        },
    },

    "POST /api/v1/production/cutting": {
        "note": "**REMOVED.** This route always answers **410 Gone**. Pieces are "
                "minted at breakdown upload, so there is nothing for it to do. "
                "Log a cut through `POST /production/log` with "
                "`screen_context=LEATHER_CUT`.",
        "example": {
            "detail": "POST /production/cutting is removed. Pieces mint at "
                      "breakdown upload; log cutting via POST /production/log "
                      "with screen_context=LEATHER_CUT.",
            "request_id": "0f2c7f0e-2b7a-4f1b-9a5a-9f0b3f0c7e11",
        },
    },

    "POST /api/v1/production/scan": {
        "note": "**REMOVED.** This route always answers **410 Gone**. Use "
                "`POST /production/log`, which is the single two-door logging "
                "surface.",
        "example": {
            "detail": "POST /production/scan is replaced by POST /production/log "
                      "(two-door).",
            "request_id": "0f2c7f0e-2b7a-4f1b-9a5a-9f0b3f0c7e11",
        },
    },

    # ═════════════════════════════════════════════════════════════════════════
    # MATERIAL — the per-style material spec (the recipe)
    # ═════════════════════════════════════════════════════════════════════════
    "GET /api/v1/styles/{style_id}/material-spec": {
        "note": "The authoring grid: every recipe line, how each one resolves to "
                "real stock, and what still blocks release.",
        "fields": [
            ["style_id", "string (uuid)", "The style."],
            ["style_code", "string or null", "Its code."],
            ["style_name", "string", "Its name."],
            ["production_status", "string", "`DRAFT` | `RELEASED` | `CANCELLED`."],
            ["editable", "boolean",
             "Whether the grid may still be edited. Leather and lining freeze at "
             "release; accessories stay correctable."],
            ["confirmed", "boolean", "Has the recipe been signed off."],
            ["confirmed_at", "string (date-time) or null", "When."],
            ["confirmed_by", "string or null", "Who."],
            ["no_accessories_declared", "boolean or null",
             "THREE STATES. `true` = somebody declared this garment takes none. "
             "`null` = nobody was asked — that does **not** pass the release gate."],
            ["release_blockers", "array of string",
             "Whole sentences, ready to display. Empty means release will pass."],
            ["sku_overrides_count", "integer", "How many lines are colourway-specific."],
        ] + _SPEC_LINE_FIELDS,
        "example": {
            "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
            "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
            "style_name": "CLERMONT", "production_status": "DRAFT",
            "editable": True, "confirmed": False,
            "confirmed_at": None, "confirmed_by": None,
            "no_accessories_declared": None,
            "release_blockers": [
                "CLERMONT's material spec has not been confirmed. Enter the "
                "per-piece consumption (PUT /styles/{id}/material-spec) and "
                "confirm it."],
            "sku_overrides_count": 0,
            "lines": [_SPEC_LINE],
        },
    },

    "PUT /api/v1/styles/{style_id}/material-spec": {
        "note": "Exactly the same body as `GET .../material-spec`, plus a "
                "`message`. Re-posting the same grid is a no-op.",
        "fields": [
            ["message", "string", "For example `Saved 4 line(s) for CLERMONT; "
                                  "1 removed.` Everything else is the GET body."],
        ],
        "example": {
            "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
            "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
            "style_name": "CLERMONT", "production_status": "DRAFT",
            "editable": True, "confirmed": False, "confirmed_at": None,
            "confirmed_by": None, "no_accessories_declared": None,
            "release_blockers": [], "sku_overrides_count": 0,
            "lines": [_SPEC_LINE],
            "message": "Saved 4 line(s) for CLERMONT; 1 removed.",
        },
    },

    "POST /api/v1/styles/{style_id}/material-spec/lines": {
        "note": "The one line you just added, in the same shape as a `lines[]` "
                "entry above (no `lines[].` prefix — it is the object itself).",
        "example": _SPEC_LINE,
    },

    "PATCH /api/v1/styles/{style_id}/material-spec/lines/{line_id}": {
        "note": "The line after the edit, in the same shape as a `lines[]` entry.",
        "example": _SPEC_LINE,
    },

    "DELETE /api/v1/styles/{style_id}/material-spec/lines/{line_id}": {
        "note": "A SOFT delete — the issue ledger still points at the row, so it "
                "is deactivated, not removed.",
        "fields": [
            ["deactivated", "boolean", "Always `true`."],
            ["line_id", "string (uuid)", "The line."],
            ["article", "string", "Its article, for the toast message."],
            ["message", "string", "Ready-made sentence."],
        ],
        "example": {"deactivated": True,
                    "line_id": "c1a20033-0000-4a11-9f00-000000000001",
                    "article": "YKK ZIP 60",
                    "message": "YKK ZIP 60 removed from CLERMONT's recipe."},
    },

    "POST /api/v1/styles/{style_id}/material-spec/confirm": {
        "note": "The sign-off. Read `release_blockers` — confirming does not by "
                "itself mean the style can be released.",
        "fields": [
            ["style_id", "string (uuid)", "The style."],
            ["style_code", "string or null", "Its code."],
            ["confirmed", "boolean", "Always `true` here."],
            ["confirmed_at", "string (date-time)", "Now."],
            ["confirmed_by", "string", "The caller's name."],
            ["no_accessories_declared", "boolean", "What you declared."],
            ["line_count", "integer", "Lines on the recipe."],
            ["accessory_line_count", "integer", "How many are accessories."],
            ["release_blockers", "array of string",
             "Empty means the style may now be released."],
            ["warnings", "array of string",
             "NOT blockers. For example: no LINING line, so the lining cut "
             "screen will not prefill a quantity."],
            ["message", "string", "Ready-made sentence for the toast."],
        ],
        "example": {
            "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
            "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
            "confirmed": True, "confirmed_at": "2026-09-23T09:15:00Z",
            "confirmed_by": "Anita (DM)", "no_accessories_declared": False,
            "line_count": 4, "accessory_line_count": 2,
            "release_blockers": [], "warnings": [],
            "message": "CLERMONT's material spec is confirmed. It may now be "
                       "released.",
        },
    },

    "POST /api/v1/styles/{style_id}/material-spec/copy-from": {
        "note": "Seeds the recipe from another style. **It does not confirm it** "
                "— somebody still has to look at the numbers for this style.",
        "fields": [
            ["copied", "integer", "Lines copied."],
            ["skipped", "integer", "Lines not copied."],
            ["skipped_detail[].article", "string", "Which material."],
            ["skipped_detail[].scope", "string", "`SKU` when it was an override."],
            ["skipped_detail[].reason", "string",
             "Why — already on this style, or no matching colour/size for the "
             "override."],
            ["source_style", "string", "Where it was copied from."],
            ["message", "string", "Ready-made sentence."],
        ],
        "example": {
            "copied": 4, "skipped": 1,
            "skipped_detail": [{"article": "PINE GREEN KNIT", "scope": "SKU",
                                "reason": "CLERMONT has no matching colour/size "
                                          "for this override."}],
            "source_style": "CLERMONT SS26",
            "message": "Copied 4 line(s) from CLERMONT SS26. Review the "
                       "quantities, then confirm the spec.",
        },
    },

    "GET /api/v1/styles/{style_id}/material-spec/requirement": {
        "note": "**The screen that should stop an order.** Ordered quantity × "
                "per-piece against what is actually on the shelf, line by line. "
                "Read it BEFORE release — afterwards a shortfall is a stoppage "
                "instead of a purchase order.",
        "fields": [
            ["style_id", "string (uuid)", "The style."],
            ["style_code", "string or null", "Its code."],
            ["style_name", "string", "Its name."],
            ["qty_ordered", "integer", "Garments ordered across every SKU."],
            ["confirmed", "boolean", "Is the recipe signed off."],
            ["release_blockers", "array of string", "What still blocks release."],
            ["short_lines", "integer", "How many lines cannot be covered."],
            ["message", "string", "Ready-made summary sentence."],
            ["lines[].pieces", "integer",
             "How many garments THIS line is needed by. A style-wide line covers "
             "every garment minus those with their own override; a sized line "
             "covers only garments of that size."],
            ["lines[].total_required", "number", "`pieces × qty_per_piece`."],
            ["lines[].short_by", "number", "`total_required − available`, or 0."],
            ["lines[].suggested_supplier", "object or null",
             "`{id, name}` when short — feeds straight into "
             "`POST /suppliers/orders`."],
        ] + _SPEC_LINE_FIELDS,
        "example": {
            "style_id": "9c2f0011-0000-4a11-9f00-000000000001",
            "style_code": "NB-B-1-CLERMONT-GOAT_SUEDE",
            "style_name": "CLERMONT", "qty_ordered": 152,
            "confirmed": True, "release_blockers": [],
            "lines": [dict(_SPEC_LINE, pieces=152, total_required=2204.0,
                           short_by=1472.0,
                           suggested_supplier={
                               "id": "e3c40055-0000-4a11-9f00-000000000001",
                               "name": "S.N. TRADERS"})],
            "short_lines": 1,
            "message": "1 of 4 material line(s) are short for 152 piece(s). "
                       "Raise a supplier order before releasing.",
        },
    },
}
