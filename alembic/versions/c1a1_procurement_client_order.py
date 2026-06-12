"""rename buyer order -> client_order; add BOM-procurement schema

Stage 0 — BOM Procurement Workflow. This is the schema half of the spec:

  1. ATOMIC RENAME (spec §3, "reversible but high-risk"):
       purchase_order            -> client_order   (the BUYER order)
       style.purchase_order_id   -> client_order_id
       client_order.po_number    -> order_number
     Done FIRST so the name `purchase_order` is freed for the SUPPLIER PO created
     in step 4, and so `client_order` exists before document/bom FK to it.

  2. New columns on existing tables (client / client_order / style / sku) for the
     cross-client variance (currency, size system, refs, multi-colour dims, ...).

  3. style_component child table (combined / add-on styles: "CLERMONT + VEST").

  4. The 13 procurement tables (document, spec_sheet, bom, bom_item,
     inventory_item, inventory_check, inventory_check_line, supplier,
     purchase_order [SUPPLIER PO], po_item, po_response, notification, audit_log),
     created in FK-dependency order.

Reversible: `downgrade` drops the new tables/columns and renames the buyer order
back. Plain DDL only — no data backfill (per spec §3, backfills stay in the
importer/seed, never in Alembic).

Revision ID: c1a1_procurement_client_order
Revises: c1a0_add_md_hr_roles
Create Date: 2026-06-11
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c1a1_procurement_client_order"
down_revision = "c1a0_add_md_hr_roles"
branch_labels = None
depends_on = None


def _ts_cols():
    """created_at / updated_at, matching TimestampMixin."""
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )


def upgrade():
    # ── 1. ATOMIC RENAME: buyer purchase_order -> client_order ────────────────
    op.drop_index("ix_purchase_order_po_number", table_name="purchase_order")
    op.drop_index("ix_purchase_order_client_id", table_name="purchase_order")
    op.drop_index("ix_style_purchase_order_id", table_name="style")

    op.rename_table("purchase_order", "client_order")
    op.alter_column("client_order", "po_number", new_column_name="order_number")
    op.alter_column("style", "purchase_order_id", new_column_name="client_order_id")

    op.create_index("ix_client_order_order_number", "client_order", ["order_number"])
    op.create_index("ix_client_order_client_id", "client_order", ["client_id"])
    op.create_index("ix_style_client_order_id", "style", ["client_order_id"])

    # ── 2a. New columns on `client` (existing rows -> server defaults) ────────
    op.add_column("client", sa.Column("code", sa.String(40), nullable=True))
    op.add_column("client", sa.Column("currency", sa.String(3), nullable=True))
    op.add_column("client", sa.Column("default_size_system", sa.String(10), nullable=True))
    op.add_column("client", sa.Column("brand", sa.String(120), nullable=True))
    op.add_column("client", sa.Column("label", sa.String(120), nullable=True))
    op.add_column("client", sa.Column("contact_email", sa.String(160), nullable=True))
    op.add_column("client", sa.Column("contact_phone", sa.String(50), nullable=True))
    op.add_column("client", sa.Column("address", sa.String(400), nullable=True))
    op.add_column("client", sa.Column("is_active", sa.Boolean(),
                                      server_default=sa.true(), nullable=False))
    op.create_index("ix_client_code", "client", ["code"], unique=True)

    # ── 2b. New columns on `style` (incl. self-FK base_style_id) ─────────────
    op.add_column("style", sa.Column("season", sa.String(20), nullable=True))
    op.add_column("style", sa.Column("customer_ref", sa.String(80), nullable=True))
    op.add_column("style", sa.Column("internal_ref", sa.String(80), nullable=True))
    op.add_column("style", sa.Column("unit_price", sa.Numeric(12, 2), nullable=True))
    op.add_column("style", sa.Column("currency", sa.String(3), nullable=True))
    op.add_column("style", sa.Column("base_style_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_style_base_style_id", "style", ["base_style_id"])
    op.create_foreign_key("fk_style_base_style_id_style", "style", "style",
                          ["base_style_id"], ["id"])

    # ── 2c. New columns on `sku` ─────────────────────────────────────────────
    op.add_column("sku", sa.Column("nylon_color", sa.String(80), nullable=True))
    op.add_column("sku", sa.Column("knit_color", sa.String(80), nullable=True))

    # ── 3. style_component ───────────────────────────────────────────────────
    op.create_table(
        "style_component",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("style_id", UUID(as_uuid=True), nullable=False),
        sa.Column("component_name", sa.String(80), nullable=False),
        sa.Column("add_on_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(3), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["style_id"], ["style.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_style_component_style_id", "style_component", ["style_id"])

    # ── 4. Procurement tables — created in FK-dependency order ───────────────
    # supplier / inventory_item / document have no FK into the new set.
    op.create_table(
        "supplier",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("email", sa.String(160), nullable=True),
        sa.Column("service", sa.String(160), nullable=True),
        sa.Column("gstin", sa.String(20), nullable=True),
        sa.Column("address", sa.String(400), nullable=True),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), server_default="60", nullable=True),
        sa.Column("lead_time_days", sa.Integer(), server_default="10", nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_ts_cols(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_supplier_name", "supplier", ["name"], unique=True)

    op.create_table(
        "inventory_item",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("description", sa.String(400), nullable=False),
        sa.Column("normalized_key", sa.String(400), nullable=True),
        sa.Column("uom", sa.String(20), nullable=True),
        sa.Column("qty_on_hand", sa.Numeric(12, 3), server_default="0", nullable=False),
        sa.Column("rate", sa.Numeric(12, 2), nullable=True),
        sa.Column("category", sa.String(80), nullable=True),
        sa.Column("color", sa.String(80), nullable=True),
        sa.Column("article_ref", sa.String(120), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_ts_cols(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_item_description", "inventory_item", ["description"])
    op.create_index("ix_inventory_item_normalized_key", "inventory_item", ["normalized_key"])

    op.create_table(
        "document",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", UUID(as_uuid=True), nullable=True),
        sa.Column("client_order_id", UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("filename", sa.String(300), nullable=False),
        sa.Column("mime", sa.String(120), nullable=True),
        sa.Column("storage_url", sa.String(600), nullable=True),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("uploaded_by", UUID(as_uuid=True), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"]),
        sa.ForeignKeyConstraint(["client_order_id"], ["client_order.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_document_client_id", "document", ["client_id"])
    op.create_index("ix_document_client_order_id", "document", ["client_order_id"])
    op.create_index("ix_document_kind", "document", ["kind"])
    op.create_index("ix_document_sha256", "document", ["sha256"], unique=True)
    op.create_index("ix_document_uploaded_by", "document", ["uploaded_by"])

    # client_order.source_document_id now that `document` exists.
    op.add_column("client_order", sa.Column("currency", sa.String(3), nullable=True))
    op.add_column("client_order", sa.Column("agent", sa.String(120), nullable=True))
    op.add_column("client_order", sa.Column("line", sa.String(120), nullable=True))
    op.add_column("client_order", sa.Column("source_document_id", UUID(as_uuid=True), nullable=True))
    op.create_index("ix_client_order_source_document_id", "client_order", ["source_document_id"])
    op.create_foreign_key("fk_client_order_source_document_id_document",
                          "client_order", "document", ["source_document_id"], ["id"])

    op.create_table(
        "spec_sheet",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", UUID(as_uuid=True), nullable=False),
        sa.Column("style_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_document_id", UUID(as_uuid=True), nullable=True),
        sa.Column("spec_type", sa.String(30), nullable=False),
        sa.Column("season", sa.String(20), nullable=True),
        sa.Column("customer_label", sa.String(120), nullable=True),
        sa.Column("measurements", JSONB(), nullable=True),
        sa.Column("attributes", JSONB(), nullable=True),
        sa.Column("instructions", JSONB(), nullable=True),
        sa.Column("extracted_by", sa.String(20), nullable=True),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["client_id"], ["client.id"]),
        sa.ForeignKeyConstraint(["style_id"], ["style.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_spec_sheet_client_id", "spec_sheet", ["client_id"])
    op.create_index("ix_spec_sheet_style_id", "spec_sheet", ["style_id"])
    op.create_index("ix_spec_sheet_source_document_id", "spec_sheet", ["source_document_id"])

    op.create_table(
        "bom",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("client_order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("style_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("currency", sa.String(3), nullable=True),
        sa.Column("garment_fob_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("bulk_total", sa.Numeric(12, 2), nullable=True),
        sa.Column("order_qty", sa.Integer(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("approved_by", UUID(as_uuid=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_document_id", UUID(as_uuid=True), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["client_order_id"], ["client_order.id"]),
        sa.ForeignKeyConstraint(["style_id"], ["style.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["source_document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("client_order_id", "style_id", name="uq_bom_order_style"),
    )
    op.create_index("ix_bom_client_order_id", "bom", ["client_order_id"])
    op.create_index("ix_bom_style_id", "bom", ["style_id"])
    op.create_index("ix_bom_status", "bom", ["status"])

    op.create_table(
        "bom_item",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("bom_id", UUID(as_uuid=True), nullable=False),
        sa.Column("category", sa.String(20), server_default="main_material", nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("material_color", sa.String(80), nullable=True),
        sa.Column("qty_per_garment", sa.Numeric(12, 3), nullable=True),
        sa.Column("uom", sa.String(20), nullable=True),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("bulk_qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("total_cost", sa.Numeric(12, 2), nullable=True),
        sa.Column("annotation", sa.Text(), nullable=True),
        sa.Column("source_ref", sa.String(120), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["bom_id"], ["bom.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_bom_item_bom_id", "bom_item", ["bom_id"])

    op.create_table(
        "inventory_check",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("bom_id", UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), server_default="running", nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("run_by", UUID(as_uuid=True), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["bom_id"], ["bom.id"]),
        sa.ForeignKeyConstraint(["run_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_check_bom_id", "inventory_check", ["bom_id"])

    op.create_table(
        "inventory_check_line",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("inventory_check_id", UUID(as_uuid=True), nullable=False),
        sa.Column("bom_item_id", UUID(as_uuid=True), nullable=False),
        sa.Column("inventory_item_id", UUID(as_uuid=True), nullable=True),
        sa.Column("required_qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("on_hand_qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("shortfall_qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("status", sa.String(20), server_default="out_of_stock", nullable=False),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["inventory_check_id"], ["inventory_check.id"]),
        sa.ForeignKeyConstraint(["bom_item_id"], ["bom_item.id"]),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_item.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_check_line_inventory_check_id", "inventory_check_line", ["inventory_check_id"])
    op.create_index("ix_inventory_check_line_bom_item_id", "inventory_check_line", ["bom_item_id"])
    op.create_index("ix_inventory_check_line_inventory_item_id", "inventory_check_line", ["inventory_item_id"])

    # The SUPPLIER purchase_order — the name freed by the rename in step 1.
    op.create_table(
        "purchase_order",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("po_number", sa.String(40), nullable=False),
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=False),
        sa.Column("bom_id", UUID(as_uuid=True), nullable=True),
        sa.Column("client_order_id", UUID(as_uuid=True), nullable=True),
        sa.Column("buyer_ref", sa.String(120), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=True),
        sa.Column("delivery_days", sa.Integer(), nullable=True),
        sa.Column("payment_terms_days", sa.Integer(), server_default="60", nullable=True),
        sa.Column("currency", sa.String(3), server_default="INR", nullable=True),
        sa.Column("subtotal", sa.Numeric(12, 2), nullable=True),
        sa.Column("cgst", sa.Numeric(12, 2), nullable=True),
        sa.Column("sgst", sa.Numeric(12, 2), nullable=True),
        sa.Column("round_off", sa.Numeric(12, 2), nullable=True),
        sa.Column("total", sa.Numeric(12, 2), nullable=True),
        sa.Column("status", sa.String(20), server_default="draft", nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pdf_document_id", UUID(as_uuid=True), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["supplier_id"], ["supplier.id"]),
        sa.ForeignKeyConstraint(["bom_id"], ["bom.id"]),
        sa.ForeignKeyConstraint(["client_order_id"], ["client_order.id"]),
        sa.ForeignKeyConstraint(["pdf_document_id"], ["document.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_purchase_order_po_number", "purchase_order", ["po_number"])
    op.create_index("ix_purchase_order_supplier_id", "purchase_order", ["supplier_id"])
    op.create_index("ix_purchase_order_bom_id", "purchase_order", ["bom_id"])
    op.create_index("ix_purchase_order_client_order_id", "purchase_order", ["client_order_id"])
    op.create_index("ix_purchase_order_status", "purchase_order", ["status"])

    op.create_table(
        "po_item",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("item_no", sa.Integer(), nullable=True),
        sa.Column("description", sa.String(400), nullable=False),
        sa.Column("color", sa.String(80), nullable=True),
        sa.Column("uom", sa.String(20), nullable=True),
        sa.Column("qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=True),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("inventory_item_id", UUID(as_uuid=True), nullable=True),
        sa.Column("bom_item_id", UUID(as_uuid=True), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.ForeignKeyConstraint(["inventory_item_id"], ["inventory_item.id"]),
        sa.ForeignKeyConstraint(["bom_item_id"], ["bom_item.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_po_item_purchase_order_id", "po_item", ["purchase_order_id"])

    op.create_table(
        "po_response",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("purchase_order_id", UUID(as_uuid=True), nullable=False),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmed_qty", sa.Numeric(12, 3), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["purchase_order_id"], ["purchase_order.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_po_response_purchase_order_id", "po_response", ["purchase_order_id"])

    op.create_table(
        "notification",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("recipient_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("supplier_id", UUID(as_uuid=True), nullable=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("subject", sa.String(300), nullable=True),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(20), server_default="pending", nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("parent_notification_id", UUID(as_uuid=True), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["recipient_user_id"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["supplier.id"]),
        sa.ForeignKeyConstraint(["parent_notification_id"], ["notification.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notification_recipient_user_id", "notification", ["recipient_user_id"])
    op.create_index("ix_notification_supplier_id", "notification", ["supplier_id"])
    op.create_index("ix_notification_type", "notification", ["type"])
    op.create_index("ix_notification_status", "notification", ["status"])

    op.create_table(
        "audit_log",
        sa.Column("id", UUID(as_uuid=True), nullable=False),
        sa.Column("actor_user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("entity_type", sa.String(60), nullable=True),
        sa.Column("entity_id", UUID(as_uuid=True), nullable=True),
        sa.Column("before", JSONB(), nullable=True),
        sa.Column("after", JSONB(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=True),
        *_ts_cols(),
        sa.ForeignKeyConstraint(["actor_user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])
    op.create_index("ix_audit_log_action", "audit_log", ["action"])
    op.create_index("ix_audit_log_entity_type", "audit_log", ["entity_type"])
    op.create_index("ix_audit_log_entity_id", "audit_log", ["entity_id"])


def downgrade():
    # Drop procurement tables in reverse FK-dependency order.
    op.drop_table("audit_log")
    op.drop_table("notification")
    op.drop_table("po_response")
    op.drop_table("po_item")
    op.drop_table("purchase_order")          # SUPPLIER PO
    op.drop_table("inventory_check_line")
    op.drop_table("inventory_check")
    op.drop_table("bom_item")
    op.drop_table("bom")
    op.drop_table("spec_sheet")

    # Remove client_order columns that FK into `document` before dropping document.
    op.drop_constraint("fk_client_order_source_document_id_document",
                       "client_order", type_="foreignkey")
    op.drop_index("ix_client_order_source_document_id", table_name="client_order")
    op.drop_column("client_order", "source_document_id")
    op.drop_column("client_order", "line")
    op.drop_column("client_order", "agent")
    op.drop_column("client_order", "currency")

    op.drop_table("document")
    op.drop_table("inventory_item")
    op.drop_table("supplier")
    op.drop_table("style_component")

    # sku / style / client column drops.
    op.drop_column("sku", "knit_color")
    op.drop_column("sku", "nylon_color")

    op.drop_constraint("fk_style_base_style_id_style", "style", type_="foreignkey")
    op.drop_index("ix_style_base_style_id", table_name="style")
    op.drop_column("style", "base_style_id")
    op.drop_column("style", "currency")
    op.drop_column("style", "unit_price")
    op.drop_column("style", "internal_ref")
    op.drop_column("style", "customer_ref")
    op.drop_column("style", "season")

    op.drop_index("ix_client_code", table_name="client")
    op.drop_column("client", "is_active")
    op.drop_column("client", "address")
    op.drop_column("client", "contact_phone")
    op.drop_column("client", "contact_email")
    op.drop_column("client", "label")
    op.drop_column("client", "brand")
    op.drop_column("client", "default_size_system")
    op.drop_column("client", "currency")
    op.drop_column("client", "code")

    # ── Reverse the atomic rename: client_order -> purchase_order ────────────
    op.drop_index("ix_style_client_order_id", table_name="style")
    op.drop_index("ix_client_order_client_id", table_name="client_order")
    op.drop_index("ix_client_order_order_number", table_name="client_order")

    op.alter_column("style", "client_order_id", new_column_name="purchase_order_id")
    op.alter_column("client_order", "order_number", new_column_name="po_number")
    op.rename_table("client_order", "purchase_order")

    op.create_index("ix_style_purchase_order_id", "style", ["purchase_order_id"])
    op.create_index("ix_purchase_order_client_id", "purchase_order", ["client_id"])
    op.create_index("ix_purchase_order_po_number", "purchase_order", ["po_number"])
