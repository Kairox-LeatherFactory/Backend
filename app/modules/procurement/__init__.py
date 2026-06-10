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

Stage 0 establishes only the schema. Repository / service / router layers and
the Gemini->Groq extraction service arrive in later stages.
"""
