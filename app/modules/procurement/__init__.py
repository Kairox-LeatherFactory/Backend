"""
procurement — Stage 1 intake (the BOM Procurement Workflow front door).

Owns the upload & validation surface: the `submission` upload-batch (the pairing
surrogate that becomes the client_order link in Stage 2) and the per-client × doc-kind
validation registry `client_template`, plus the scan → sniff → validate → store
pipeline (classifier, sniffing, scanning, validator, registry, presenters, errors,
seed_templates).

────────────────────────────────────────────────────────────────────────────────
MODULE SPLIT — the former monolith is now FOUR stage modules + a shared core
────────────────────────────────────────────────────────────────────────────────
The BOM Procurement Workflow was split out of one `procurement` package into focused,
independently-ownable modules so each stage has its own router → service → repository →
models stack:

    procurement   Stage 1   upload & validation intake (this module)
    bom           Stage 2/3 BOM generation + approval/lock + in-app notifications
    inventory     Stage 4   inventory master + per-BOM stock checks + reservations
    supplier_po   Stage 5   suppliers, the supplier PO, matching, escalation, board

Cross-cutting tables that EVERY stage writes — `document`, `notification`, `audit_log`
— plus the pluggable email/storage backends live in `app/core` (FK-by-string only, so
core imports nothing from app.modules). Cross-module data flows through the owning
module's SERVICE returning plain DTOs, never another module's repository/models.

IMPORT DIRECTION (acyclic except the deliberate lazy bom↔inventory service pair):
    procurement → core, clients.service, bom.service (Stage-1 → Stage-2 generate trigger, lazy)
    bom         → core, clients.service, inventory.service   (never imports procurement)
    inventory   → core, bom.service, clients.service
    supplier_po → core, bom.service, inventory.service, clients.service, users.service
"""
