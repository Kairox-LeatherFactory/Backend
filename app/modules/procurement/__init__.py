"""
procurement — the BOM Procurement Workflow domain (supplier side of the doc).

Owns the schema for: uploaded documents + extracted spec sheets (Stage 1),
the BOM and its line items (Stage 2/3), the inventory master + per-BOM stock
checks (Stage 4), suppliers + the supplier PURCHASE ORDER and its escalation
responses (Stage 5), plus cross-cutting notifications and an audit log
(Stage 3/5/6).

NAMING: the name `purchase_order` belongs HERE (the supplier PO). The buyer's
order is `client_order` in the clients module. Domain value-sets live in
`enums.py`; the tables live in `models.py`.

Stage 0 establishes only the schema. Stage 1 adds the upload & validation surface
(submission pairing, identity validation, virus scan, storage). The Gemini->Groq
extraction service and the BOM/inventory/supplier logic arrive in later stages.

────────────────────────────────────────────────────────────────────────────────
MODULE STRUCTURE & BOUNDARIES — why this is ONE module (and how it grows)
────────────────────────────────────────────────────────────────────────────────
DECISION: procurement is ONE module = ONE deployable = ONE import-linter container.
It is NOT split into separate modules or microservices, on purpose.

WHY ONE: the 13 tables are a single, tightly-FK-coupled bounded context — a single
workflow run threads through nearly all of them in ONE transaction (`submission` →
`document` → `bom` → `bom_item` → `inventory_check`/`inventory_check_line` →
`purchase_order` → `po_item` → `po_response`, with `notification`/`audit_log`
cross-cutting). `document` alone is referenced by 5 FKs, `bom` by 3. Splitting that
into network services would replace in-process referential integrity with a
distributed saga (eventual consistency + retries + compensation) for no current
gain (one team, one DB, no independent scaling need). The platform is a *modular
monolith, microservice-READY* (CLAUDE.md §3.1–3.2) — ready, not pre-split.

FUTURE INTERNAL LAYOUT (organise by STAGE as each gains real logic — NOT before,
to avoid empty shells). Keep the shared `models.py` + `enums.py` at the module root;
group the per-stage behavior into sub-packages:
    procurement/intake/     Stage 1 upload & validation  (today's service, router,
                            pipeline, validator, registry, classifier, sniffing,
                            scanning, storage, presenters, seed_templates, errors)
    procurement/bom/        Stage 2/3 extraction + BOM approval/lock
    procurement/inventory/  Stage 4 stock checks
    procurement/suppliers/  Stage 5 supplier PO + escalation
(As of now only Stage 1 has logic, so the files live flat at the module root; move
them into `intake/` only when a second stage's package is introduced.)


PROMOTE-TO-SERVICE CRITERIA (when to revisit the monolith decision): a stage develops
an independent scaling profile, a separate ownership/team boundary, or a genuinely
async/queue-driven workload. The move is then "lift the sub-package, swap the
in-process service call for HTTP" (CLAUDE.md §3.2) — nothing else changes.
"""
