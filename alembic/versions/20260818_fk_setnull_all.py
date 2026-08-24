"""every NULLABLE foreign key gets ON DELETE SET NULL

Revision ID: 20260818_fk_setnull
Revises: 20260818_fk_cycles
Create Date: 2026-08-18

WHAT THIS IS
──────────────────────────────────────────────────────────────────────────────

The predecessor migration (`20260818_fk_cycles`) gave a delete rule to the nine
foreign keys sitting inside a CYCLE, because those were the ones with no
workaround at all — a cyclic reference has no "delete the children first" order.
This migration finishes the job for the other 76: **every nullable foreign key
in the schema now declares ON DELETE SET NULL.**

The 85 constraints listed below are the complete set, and a strict superset of
the nine the previous migration touched. Re-applying SET NULL to a constraint
that already has it is a no-op in effect, so this migration is correct whether or
not `20260818_fk_cycles` has already been applied to the target database. That
redundancy is the whole reason these are two files rather than an edit to one
that may already be stamped.

THE RULE, AND WHY NULLABILITY IS EXACTLY THE RIGHT LINE TO DRAW
──────────────────────────────────────────────────────────────────────────────

A nullable foreign key is a column whose schema already says, in the only way a
schema can, "this link is optional; the row means something without it". Every
one of them can lose its parent and remain a valid row. ON DELETE SET NULL only
makes the database do what the nullability already promised, instead of pinning
the parent in place forever.

    Delete a supplier   → its lots keep article, colour and on_hand; they just
                          no longer name a supplier.
    Delete a user       → the audit_log row keeps action / before / after; it
                          just no longer names an actor.
    Delete a document   → the order keeps every field extracted from it at
                          upload time; it just loses the provenance pointer.

Note what is NOT in the list: the 39 NOT NULL foreign keys. SET NULL is illegal
on those — Postgres rejects the constraint outright — and it would be wrong even
if it were legal. A `sku` with no `style_id`, a `wage_line` with no
`wage_run_id`, a `production_event` with no `employee_id` are not degraded rows,
they are corrupt ones. Those keep the default RESTRICT, which is the right
answer: to delete a style you must first deal with its SKUs, deliberately. Two
already opt into CASCADE where the child is genuinely owned
(`sku_order_line.sku_id`, `wage_line_detail.wage_run_id`); that stays.

CASCADE WAS CONSIDERED AND REJECTED AS THE DEFAULT
    CASCADE turns one DELETE into an unbounded, invisible one. On a schema whose
    stated first principle is that production history and wage lines survive
    everything (CLAUDE.md §6, §10), a rule letting the removal of one client
    silently erase a year of production events is not a convenience — it is the
    failure mode. SET NULL loses a pointer; CASCADE loses the record.

THE ONE PLACE TO WATCH: barcode_registry
    A registry row exists in order to name a domain row, so nulling its FK leaves
    an ACTIVE code that resolves to nothing. `resolve()` guards every branch on
    the FK being present (`type == PIECE and piece_id`), so this degrades to
    "known code, no payload" rather than crashing the scan screen — but widowed
    rows still count toward the per-order `minted` / `balance` figures the
    factory reconciles against. After any administrative delete of a piece,
    drawer or employee, sweep barcode_registry for rows whose type-appropriate FK
    went NULL. The long comment on BarcodeRegistry in
    app/modules/barcode/models.py carries the full reasoning.

HOW THE CONSTRAINT NAME IS RESOLVED
──────────────────────────────────────────────────────────────────────────────

Postgres has no "ALTER CONSTRAINT … SET ON DELETE"; changing a delete rule means
DROP + ADD, which makes the existing name load-bearing. This migration does not
trust the names below. It asks the live database which constraint sits on that
(table, column) → referred pair, drops THAT one, and recreates it under the same
name — so a delete rule never renames a constraint as a side effect. The listed
name is the fallback used only when no constraint is found at all.

    Two listed names carry a hash suffix:
        fk_inventory_reservation_inventory_check_line_id_invent_a929
        fk_pattern_reference_resolved_template_id_style_consump_e74f
    Those are not typos. The naming convention would produce 69-character
    identifiers; SQLAlchemy truncates to Postgres' 63-char NAMEDATALEN with a
    deterministic 4-hex-digit suffix. The names here are the ones actually in the
    database, read from the dialect's identifier preparer rather than from the
    raw convention string.

POSTGRES ONLY
    SQLite cannot ALTER a constraint. Its schema is built by
    `Base.metadata.create_all`, which already carries these rules straight from
    the model definitions, so this is a no-op off Postgres.

LOCKING
    85 × (DROP + ADD). Each ADD takes a SHARE ROW EXCLUSIVE lock on both tables
    and scans the child table to validate. At this schema's row counts that is
    seconds in total. If a table later grows past a few million rows, split its
    step into `ADD CONSTRAINT … NOT VALID` plus a separate `VALIDATE CONSTRAINT`
    so the scan does not block writes.

PORTABILITY (Phase-3 Oracle note)
    ON DELETE SET NULL is standard SQL and present on Oracle. Nothing here is
    Postgres-specific beyond the identifier-length note above.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260818_fk_setnull"
down_revision = "20260818_fk_cycles"
branch_labels = None
depends_on = None


# (table, column, referred_table, expected constraint name)
#
# Generated from Base.metadata: every foreign key whose child column is NULLABLE.
# The name is a FALLBACK only — see "HOW THE CONSTRAINT NAME IS RESOLVED" above.
# Adding a nullable FK to a model means adding it here too; the guard test
# tests/unit/test_fk_delete_rules.py fails until the model side is done.
_NULLABLE_FKS = [
    # ── app_user ──────────────────────────────────────────────
    ("app_user", "client_id", "client", "fk_app_user_client_id_client"),
    ("app_user", "employee_id", "employee", "fk_app_user_employee_id_employee"),
    # ── attendance_log ────────────────────────────────────────
    ("attendance_log", "recorded_by_user_id", "app_user",
     "fk_attendance_log_recorded_by_user_id_app_user"),
    # ── audit_log ─────────────────────────────────────────────
    ("audit_log", "actor_user_id", "app_user",
     "fk_audit_log_actor_user_id_app_user"),
    # ── barcode_registry ──────────────────────────────────────
    ("barcode_registry", "drawer_id", "drawer",
     "fk_barcode_registry_drawer_id_drawer"),
    ("barcode_registry", "employee_id", "employee",
     "fk_barcode_registry_employee_id_employee"),
    ("barcode_registry", "material_lot_id", "material_lot",
     "fk_barcode_registry_material_lot_id_material_lot"),
    ("barcode_registry", "order_id", "client_order",
     "fk_barcode_registry_order_id_client_order"),
    ("barcode_registry", "piece_id", "piece", "fk_barcode_registry_piece_id_piece"),
    ("barcode_registry", "sku_id", "sku", "fk_barcode_registry_sku_id_sku"),
    ("barcode_registry", "style_id", "style", "fk_barcode_registry_style_id_style"),
    # ── bom ───────────────────────────────────────────────────
    ("bom", "approved_by", "app_user", "fk_bom_approved_by_app_user"),
    ("bom", "client_id", "client", "fk_bom_client_id_client"),
    ("bom", "client_order_id", "client_order",
     "fk_bom_client_order_id_client_order"),
    ("bom", "cutting_confirmed_by", "app_user",
     "fk_bom_cutting_confirmed_by_app_user"),
    ("bom", "export_document_id", "document", "fk_bom_export_document_id_document"),
    ("bom", "garment_type_id", "garment_type",
     "fk_bom_garment_type_id_garment_type"),
    ("bom", "rejected_by", "app_user", "fk_bom_rejected_by_app_user"),
    ("bom", "source_document_id", "document", "fk_bom_source_document_id_document"),
    ("bom", "style_id", "style", "fk_bom_style_id_style"),
    ("bom", "submission_id", "submission", "fk_bom_submission_id_submission"),
    # ── client_order ──────────────────────────────────────────
    ("client_order", "source_document_id", "document",
     "fk_client_order_source_document"),
    # ── document ──────────────────────────────────────────────
    ("document", "client_id", "client", "fk_document_client_id_client"),
    ("document", "submission_id", "submission",
     "fk_document_submission_id_submission"),
    ("document", "uploaded_by", "app_user", "fk_document_uploaded_by_app_user"),
    # ── drawer ────────────────────────────────────────────────
    ("drawer", "current_piece_id", "piece", "fk_drawer_current_piece_id_piece"),
    # ── dxf_yield_observation ─────────────────────────────────
    ("dxf_yield_observation", "confirmed_by", "app_user",
     "fk_dxf_yield_observation_confirmed_by_app_user"),
    ("dxf_yield_observation", "source_bom_id", "bom",
     "fk_dxf_yield_observation_source_bom_id_bom"),
    # ── inventory_check ───────────────────────────────────────
    ("inventory_check", "run_by", "app_user", "fk_inventory_check_run_by_app_user"),
    # ── inventory_check_line ──────────────────────────────────
    ("inventory_check_line", "inventory_item_id", "inventory_item",
     "fk_inventory_check_line_inventory_item_id_inventory_item"),
    # ── inventory_reservation ─────────────────────────────────
    ("inventory_reservation", "inventory_check_line_id", "inventory_check_line",
     "fk_inventory_reservation_inventory_check_line_id_invent_a929"),
    # ── material_lot ──────────────────────────────────────────
    ("material_lot", "supplier_id", "material_supplier",
     "fk_material_lot_supplier_id_material_supplier"),
    # ── material_receipt ──────────────────────────────────────
    ("material_receipt", "received_by", "app_user",
     "fk_material_receipt_received_by_app_user"),
    ("material_receipt", "supplier_order_id", "supplier_order",
     "fk_material_receipt_supplier_order_id_supplier_order"),
    # ── notification ──────────────────────────────────────────
    ("notification", "parent_notification_id", "notification",
     "fk_notification_parent_notification_id_notification"),
    ("notification", "recipient_user_id", "app_user",
     "fk_notification_recipient_user_id_app_user"),
    ("notification", "supplier_id", "supplier",
     "fk_notification_supplier_id_supplier"),
    # ── order_extraction ──────────────────────────────────────
    ("order_extraction", "promoted_to_bom_id", "bom",
     "fk_order_extraction_promoted_to_bom_id_bom"),
    ("order_extraction", "source_document_id", "document",
     "fk_order_extraction_source_document_id_document"),
    # ── order_style ───────────────────────────────────────────
    ("order_style", "bom_id", "bom", "fk_order_style_bom_id_bom"),
    ("order_style", "client_id", "client", "fk_order_style_client_id_client"),
    ("order_style", "pattern_reference_id", "pattern_reference",
     "fk_order_style_pattern_reference_id_pattern_reference"),
    ("order_style", "spec_document_id", "document",
     "fk_order_style_spec_document_id_document"),
    # ── pattern_extraction ────────────────────────────────────
    ("pattern_extraction", "client_id", "client",
     "fk_pattern_extraction_client_id_client"),
    ("pattern_extraction", "garment_type_id", "garment_type",
     "fk_pattern_extraction_garment_type_id_garment_type"),
    ("pattern_extraction", "source_document_id", "document",
     "fk_pattern_extraction_source_document_id_document"),
    # ── pattern_reference ─────────────────────────────────────
    ("pattern_reference", "client_id", "client",
     "fk_pattern_reference_client_id_client"),
    ("pattern_reference", "resolved_style_id", "style",
     "fk_pattern_reference_resolved_style_id_style"),
    ("pattern_reference", "resolved_template_id", "style_consumption_template",
     "fk_pattern_reference_resolved_template_id_style_consump_e74f"),
    ("pattern_reference", "spec_sheet_id", "spec_sheet",
     "fk_pattern_reference_spec_sheet_id_spec_sheet"),
    # ── piece ─────────────────────────────────────────────────
    ("piece", "current_operation_id", "operation",
     "fk_piece_current_operation_id_operation"),
    ("piece", "drawer_id", "drawer", "fk_piece_drawer_id_drawer"),
    # ── piece_material_issue ──────────────────────────────────
    # The accessory-kit ledger (20260821_style_spec). Listed here because this
    # list is the REGISTRY of nullable FKs the models declare — the test in
    # tests/unit/test_fk_delete_rules.py reads it as such — even though these
    # constraints are created with SET NULL inline by their own migration and so
    # need no repointing. _repoint() no-ops on a table that is not deployed yet,
    # which is exactly the case at this revision.
    ("piece_material_issue", "drawer_id", "drawer",
     "fk_piece_material_issue_drawer_id_drawer"),
    ("piece_material_issue", "issued_by_employee_id", "employee",
     "fk_piece_material_issue_issued_by_employee_id_employee"),
    ("piece_material_issue", "material_lot_id", "material_lot",
     "fk_piece_material_issue_material_lot_id_material_lot"),
    ("piece_material_issue", "piece_id", "piece",
     "fk_piece_material_issue_piece_id_piece"),
    ("piece_material_issue", "spec_line_id", "style_material_spec",
     "fk_piece_material_issue_spec_line_id_style_material_spec"),
    # ── po_item ───────────────────────────────────────────────
    ("po_item", "bom_item_id", "bom_item", "fk_po_item_bom_item_id_bom_item"),
    ("po_item", "inventory_item_id", "inventory_item",
     "fk_po_item_inventory_item_id_inventory_item"),
    # ── po_tracking_event ─────────────────────────────────────
    ("po_tracking_event", "po_response_id", "po_response",
     "fk_po_tracking_event_po_response_id_po_response"),
    # ── pom_dictionary ────────────────────────────────────────
    ("pom_dictionary", "garment_type_id", "garment_type",
     "fk_pom_dictionary_garment_type_id_garment_type"),
    # ── production_event ──────────────────────────────────────
    ("production_event", "leather_lot_id", "material_lot",
     "fk_production_event_leather_lot_id_material_lot"),
    ("production_event", "lining_lot_id", "material_lot",
     "fk_production_event_lining_lot_id_material_lot"),
    ("production_event", "piece_id", "piece", "fk_production_event_piece_id_piece"),
    # ── production_tracking ───────────────────────────────────
    ("production_tracking", "bom_id", "bom", "fk_production_tracking_bom_id_bom"),
    ("production_tracking", "updated_by", "app_user",
     "fk_production_tracking_updated_by_app_user"),
    # ── purchase_order ────────────────────────────────────────
    ("purchase_order", "approved_by", "app_user",
     "fk_purchase_order_approved_by_app_user"),
    ("purchase_order", "bom_id", "bom", "fk_purchase_order_bom_id_bom"),
    ("purchase_order", "client_order_id", "client_order",
     "fk_purchase_order_client_order_id_client_order"),
    ("purchase_order", "created_by", "app_user",
     "fk_purchase_order_created_by_app_user"),
    ("purchase_order", "pdf_document_id", "document",
     "fk_purchase_order_pdf_document_id_document"),
    ("purchase_order", "rejected_by", "app_user",
     "fk_purchase_order_rejected_by_app_user"),
    ("purchase_order", "supplier_id", "supplier",
     "fk_purchase_order_supplier_id_supplier"),
    # ── spec_extraction ───────────────────────────────────────
    ("spec_extraction", "promoted_to_spec_sheet_id", "spec_sheet",
     "fk_spec_extraction_promoted_to_spec_sheet_id_spec_sheet"),
    ("spec_extraction", "source_document_id", "document",
     "fk_spec_extraction_source_document_id_document"),
    # ── spec_sheet ────────────────────────────────────────────
    ("spec_sheet", "client_id", "client", "fk_spec_sheet_client_id_client"),
    ("spec_sheet", "source_document_id", "document",
     "fk_spec_sheet_source_document_id_document"),
    ("spec_sheet", "style_id", "style", "fk_spec_sheet_style_id_style"),
    # ── style ─────────────────────────────────────────────────
    ("style", "base_style_id", "style", "fk_style_base_style_id_style"),
    # ── style_material_spec ───────────────────────────────────
    # Only material_lot_id is listed. `sku_id` is nullable but CASCADEs, and is
    # a DOCUMENTED EXCEPTION to the SET NULL rule — NULL there means "this line
    # is the style-wide default", so SET NULL would promote one colourway's
    # override into everyone's default instead of clearing a link. See
    # _MEANINGFUL_NULL_FKS in tests/unit/test_fk_delete_rules.py.
    ("style_material_spec", "material_lot_id", "material_lot",
     "fk_style_material_spec_material_lot_id_material_lot"),
    # ── style_consumption_template ────────────────────────────
    ("style_consumption_template", "client_id", "client",
     "fk_style_consumption_template_client_id_client"),
    ("style_consumption_template", "confirmed_by", "app_user",
     "fk_style_consumption_template_confirmed_by_app_user"),
    ("style_consumption_template", "garment_type_id", "garment_type",
     "fk_style_consumption_template_garment_type_id_garment_type"),
    ("style_consumption_template", "source_bom_id", "bom",
     "fk_style_consumption_template_source_bom_id_bom"),
    # ── submission ────────────────────────────────────────────
    ("submission", "client_id", "client", "fk_submission_client_id_client"),
    ("submission", "client_order_id", "client_order",
     "fk_submission_client_order_id_client_order"),
    ("submission", "created_by", "app_user", "fk_submission_created_by_app_user"),
    ("submission", "order_document_id", "document", "fk_submission_order_document"),
    ("submission", "spec_document_id", "document", "fk_submission_spec_document"),
    # ── supplier_order ────────────────────────────────────────
    ("supplier_order", "ordered_by", "app_user",
     "fk_supplier_order_ordered_by_app_user"),
    ("supplier_order", "supplier_id", "material_supplier",
     "fk_supplier_order_supplier_id_material_supplier"),
]


def _existing_fk_names(inspector, table: str, column: str, referred: str) -> list[str]:
    """Every FK on `table` that is exactly (column) → referred(id).

    A list, not a single name: a database that has been through a rename can
    carry two constraints on the same column. All are dropped and exactly one is
    put back.
    """
    return [
        fk["name"] for fk in inspector.get_foreign_keys(table)
        if fk.get("constrained_columns") == [column]
        and fk.get("referred_table") == referred
        and fk.get("name")
    ]


def _repoint(table: str, column: str, referred: str, fallback_name: str,
             ondelete: str | None) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return                                    # table not deployed yet
    if column not in {c["name"] for c in inspector.get_columns(table)}:
        return                                    # column not deployed yet

    existing = _existing_fk_names(inspector, table, column, referred)
    for name in existing:
        op.drop_constraint(name, table, type_="foreignkey")

    # Keep whatever the database already calls it, so changing a delete rule
    # never renames a constraint as a side effect.
    op.create_foreign_key(
        existing[0] if existing else fallback_name,
        table, referred, [column], ["id"], ondelete=ondelete,
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column, referred, name in _NULLABLE_FKS:
        _repoint(table, column, referred, name, ondelete="SET NULL")


def downgrade() -> None:
    """Put every constraint back with no delete rule.

    Honest rather than useful: the pre-migration state really was "no ON DELETE",
    and a downgrade that quietly left SET NULL in place would make this migration
    irreversible in fact while advertising itself as reversible.
    """
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column, referred, name in _NULLABLE_FKS:
        _repoint(table, column, referred, name, ondelete=None)
