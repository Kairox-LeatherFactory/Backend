"""
================================================================================
scripts/docgen/services.py — THE ONE service registry
================================================================================
Which OpenAPI tags belong to which SERVICE, in the factory's words rather than
the router's. Everything generated per-service reads this list:

    scripts/make_postman_per_service.py   -> postman/<stem>.postman_collection.json
    scripts/make_service_docs.py          -> docs/service/<STEM>_API_REFERENCE.docx
                                          -> docs/service/<STEM>_SYSTEM_GUIDE.docx

WHY ONE LIST AND NOT TWO. A tag that is missing from the registry gets no
collection and no document, silently — the route simply is not there for anyone
downstream. With two copies of the list, adding a tag to one and forgetting the
other is invisible. `check_coverage()` below turns that into a loud failure, and
both generators call it.
================================================================================
"""
from __future__ import annotations

# stem, tags, one-line blurb (used as the Postman description + doc subtitle)
SERVICES: list[tuple[str, list[str], str]] = [
    ("user", ["Authentication", "Users"],
     "Login and the logins themselves. START HERE: run login, paste "
     "access_token into the collection's `token` variable, and every other "
     "collection is authorised."),
    ("client", ["Clients"],
     "Buyers, their orders, and the styles under them."),
    ("import", ["Imports"],
     "Breakdown upload: preview, commit, edit, then RELEASE. Release is the "
     "mint — it creates the pieces and their barcodes and cannot be undone."),
    ("barcode", ["Barcode"],
     "GET /barcode/resolve is every scan's front door: unknown code 404, retired "
     "card 410 Gone, otherwise the code's type and a live payload for it."),
    ("employee", ["Employees"],
     "The roster and one worker. Shop-floor workers get NO login — creating one "
     "needs only a name, a designation and a wage type, and the response carries "
     "the barcode so the card can be printed immediately."),
    ("attendance", ["Attendance"],
     "Two doors — the card scan at the gate and the manual fallback — plus the "
     "corrections. PATCH with a new employee_id RE-ALLOCATES the day and moves "
     "that day's production events with it."),
    ("material", ["Materials", "MaterialSuppliers", "Style material spec"],
     "Lots, stock, receiving (with per-hide leather sheets), the supplier "
     "directory, and the style's material recipe."),
    ("cutting", ["cutting"],
     "The grid that replaced the cutting manager's spreadsheet. One row is ONE "
     "garment; hides are allocated to it, edited freely while DRAFT, then frozen "
     "by an audited approval."),
    ("production", ["Production"],
     "POST /production/log is the whole floor's logging surface — the caller "
     "sends an ACTOR and TARGETS and never a stage. Also the two correction "
     "endpoints for a record that named the wrong worker."),
    ("store", ["store"],
     "Scan employee + piece — two scans, not three. Send releases a batch into "
     "line-stitching, which is the ONLY gate LINE_STITCHING has."),
    ("inspection", ["inspections"],
     "Reject and rework, recorded against the stage RESPONSIBLE for the defect "
     "rather than the stage it was found at. The DM approves the re-walk."),
    ("jobwork", ["job work"],
     "Garments sent to an outside factory. A dispatched piece cannot be scanned "
     "in-house; the return logs the stage against the VENDOR, so it advances the "
     "garment without generating a wage."),
    ("wage", ["Wages"],
     "Rates, runs and the ledger. A CLOSED run is a frozen snapshot and is never "
     "recomputed. Visible to HR, DM and MD only."),
    ("dashboard", ["Dashboard"],
     "The per-role manager screens: cutting, lining, stitching, store and the "
     "Direct Manager's own. Every one is a read."),
    ("analytics", ["Analytics"],
     "Factory overview, the order/style explorer, stage spread, freight-risk "
     "alerts and one garment's whole life story. READ-ONLY — analytics owns no "
     "tables and never writes."),
    ("procurement", ["Procurement — Stage 1 intake",
                     "Procurement — Stage 2/3 BOM",
                     "Procurement — Stage 4 inventory",
                     "Procurement — Stage 5 supplier PO"],
     "PHASE 2 — the auto-generation pipeline, in the five stages it actually "
     "runs in: a client submission comes in, a BOM is generated and approved, "
     "stock is checked against it, and the shortfall becomes supplier POs. "
     "Folders are ordered by stage, not alphabetically, because the stages only "
     "work in order — you cannot check inventory against a BOM you have not "
     "generated."),
    ("system", ["Health", "Root", "Chatbot"],
     "Liveness, readiness and the chatbot. `/health` answers without touching "
     "the database and `/ready` does not — that difference is the point of "
     "having both, so a load balancer can tell a slow database from a dead "
     "process. Neither is under /api/v1, and neither needs a token."),
]

# Endpoints that belong to a service's SCREEN but carry another module's tag.
# `PATCH /employees/{id}/barcode` is tagged Barcode because the barcode module
# owns the code; the person who needs it is on the employee screen, reissuing a
# card somebody lost. It appears in both places rather than neither.
EXTRAS: dict[str, set[str]] = {
    "employee": {"/api/v1/employees/{employee_id}/barcode"},
    "store": {"/api/v1/materials/issues"},
    "cutting": {"/api/v1/materials/lots", "/api/v1/materials/receive"},
}

# Display names for the document covers — what the factory calls the service.
TITLES: dict[str, str] = {
    "user": "Users & Authentication",
    "client": "Clients & Orders",
    "import": "Breakdown Import",
    "barcode": "Barcode",
    "employee": "Employees",
    "attendance": "Attendance",
    "material": "Materials & Stock",
    "cutting": "Cutting Grid",
    "production": "Production Logging",
    "store": "Store (the merge)",
    "inspection": "Inspection — Reject & Rework",
    "jobwork": "Job Work (outsourcing)",
    "wage": "Wages & Payroll",
    "dashboard": "Manager Dashboards",
    "analytics": "Analytics",
    "procurement": "Procurement (Phase 2)",
    "system": "System, Health & Chatbot",
}

# The drawer module is withdrawn; it is not a tag any more. It still gets a
# Postman file (see make_postman_per_service._drawer_collection) that says so.
DRAWER_HISTORY_PATHS = {
    "/api/v1/dashboard/store/garments/{piece_id}",
    "/api/v1/dashboard/store/garments/{piece_id}/movement",
}


def all_tags() -> set[str]:
    return {tag for _, tags, _ in SERVICES for tag in tags}


def check_coverage(spec: dict) -> list[str]:
    """Every tag the app publishes must belong to a service. Returns the orphans.

    A tag missing here produces no Postman collection and no documentation, and
    nothing complains — which is exactly how a route goes undocumented for a
    release. Callers print the orphans and exit non-zero.
    """
    published: set[str] = set()
    for _, ops in spec["paths"].items():
        for method, op in ops.items():
            if method in ("get", "post", "put", "patch", "delete"):
                published.update(op.get("tags") or ["(untagged)"])
    return sorted(published - all_tags())
