"""stage 5 — supplier PO: matching index, cross-check approval, tracking, escalation

Adds the Stage-5 (supplier purchase-order) schema on top of Stage 4 (stage-5 spec §10).
The base tables `supplier` / `purchase_order` / `po_item` / `po_response` already exist
(c1a1); this adds the deltas + three new tables:

  1. supplier_supply_history   the §1d matching index — pre-aggregated (supplier, article)
                               evidence from the provision ledger, normalized like Stage 4.
  2. po_tracking_event         the §6c unified engagement log (open/click/bounce/whatsapp/call).
  3. production_tracking       the §8b one-row-per-style production board.
  + supplier  deltas: email_status, supplier_type, state_code, whatsapp_phone.
  + purchase_order deltas: igst/gst_mode, needs_supplier/no_contact_channel/match_method/
    candidates, revision + the cross-check stamps, the tracking stamps, the escalation
    state, created_by; supplier_id + po_number relaxed to NULLable (§1c unresolved draft,
    §2d allocate-at-send).
  + po_response deltas: tracking_token, message_id.

No enum migration: POStatus/POResponseChannel/NotificationType/ProductionTrackingStatus are
plain VARCHARs storing the enum `.value` (the module convention), so no native ALTER TYPE.
New audit actions (PO_GENERATE/…/SUPPLIER_*) are strings — no schema change to audit_log.

Reversible: downgrade drops the columns/tables. Plain DDL only — the supplier import +
po_templates seed live in the importer/seed, never in Alembic.

Revision ID: c1a7_stage5_supplier_po
Revises: c1a6_stage4_inventory
Create Date: 2026-06-12
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "c1a7_stage5_supplier_po"
down_revision = "c1a6_stage4_inventory"
branch_labels = None
depends_on = None


def _ts_cols():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
    )


def upgrade():
    # ── 1. supplier_supply_history (the §1d matching index) ───────────────────
    op.create_table(
        "supplier_supply_history",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("supplier_id", UUID(as_uuid=True),
                  sa.ForeignKey("supplier.id"), nullable=False),
        sa.Column("normalized_description", sa.String(400), nullable=False),
        sa.Column("raw_description", sa.String(400), nullable=True),
        sa.Column("mode", sa.String(20), nullable=True),
        sa.Column("uom", sa.String(20), nullable=True),
        sa.Column("txn_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_purchased_at", sa.Date(), nullable=True),
        sa.Column("last_purchased_at", sa.Date(), nullable=True),
        sa.Column("last_rate", sa.Numeric(12, 2), nullable=True),
        sa.Column("min_rate", sa.Numeric(12, 2), nullable=True),
        sa.Column("max_rate", sa.Numeric(12, 2), nullable=True),
        *_ts_cols(),
        sa.UniqueConstraint("supplier_id", "normalized_description",
                            name="uq_supply_history_supplier_article"),
    )
    op.create_index("ix_supply_history_supplier_id",
                    "supplier_supply_history", ["supplier_id"])
    op.create_index("ix_supply_history_normalized_description",
                    "supplier_supply_history", ["normalized_description"])

    # ── 2. po_tracking_event (the §6c engagement log) ─────────────────────────
    op.create_table(
        "po_tracking_event",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("purchase_order_id", UUID(as_uuid=True),
                  sa.ForeignKey("purchase_order.id"), nullable=False),
        sa.Column("po_response_id", UUID(as_uuid=True),
                  sa.ForeignKey("po_response.id"), nullable=True),
        sa.Column("tracking_token", sa.String(64), nullable=True),
        sa.Column("event_type", sa.String(20), nullable=False),
        sa.Column("channel", sa.String(20), nullable=True),
        sa.Column("ip", sa.String(60), nullable=True),
        sa.Column("user_agent", sa.String(400), nullable=True),
        sa.Column("meta", JSONB(), nullable=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=True),
        *_ts_cols(),
    )
    op.create_index("ix_po_tracking_event_purchase_order_id",
                    "po_tracking_event", ["purchase_order_id"])
    op.create_index("ix_po_tracking_event_tracking_token",
                    "po_tracking_event", ["tracking_token"])

    # ── 3. production_tracking (the §8b board) ────────────────────────────────
    op.create_table(
        "production_tracking",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("client_order_id", UUID(as_uuid=True),
                  sa.ForeignKey("client_order.id"), nullable=False),
        sa.Column("style_id", UUID(as_uuid=True), sa.ForeignKey("style.id"), nullable=False),
        sa.Column("bom_id", UUID(as_uuid=True), sa.ForeignKey("bom.id"), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="awaiting_bom"),
        sa.Column("po_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("po_confirmed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("material_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_by", UUID(as_uuid=True),
                  sa.ForeignKey("app_user.id"), nullable=True),
        *_ts_cols(),
        sa.UniqueConstraint("client_order_id", "style_id",
                            name="uq_production_tracking_order_style"),
    )
    op.create_index("ix_production_tracking_client_order_id",
                    "production_tracking", ["client_order_id"])
    op.create_index("ix_production_tracking_style_id", "production_tracking", ["style_id"])
    op.create_index("ix_production_tracking_status", "production_tracking", ["status"])

    # ── 4. supplier deltas (§10) ──────────────────────────────────────────────
    op.add_column("supplier", sa.Column("email_status", sa.String(20),
                  nullable=False, server_default="unknown"))
    op.add_column("supplier", sa.Column("supplier_type", sa.String(20), nullable=True))
    op.add_column("supplier", sa.Column("state_code", sa.String(2), nullable=True))
    op.add_column("supplier", sa.Column("whatsapp_phone", sa.String(50), nullable=True))

    # ── 5. purchase_order deltas (§10) ────────────────────────────────────────
    op.alter_column("purchase_order", "supplier_id", existing_type=UUID(as_uuid=True),
                    nullable=True)
    op.alter_column("purchase_order", "po_number", existing_type=sa.String(40),
                    nullable=True)
    op.add_column("purchase_order", sa.Column("igst", sa.Numeric(12, 2), nullable=True))
    op.add_column("purchase_order", sa.Column("gst_mode", sa.String(10), nullable=True))
    op.add_column("purchase_order", sa.Column("needs_supplier", sa.Boolean(),
                  nullable=False, server_default=sa.false()))
    op.add_column("purchase_order", sa.Column("no_contact_channel", sa.Boolean(),
                  nullable=False, server_default=sa.false()))
    op.add_column("purchase_order", sa.Column("match_method", sa.String(20), nullable=True))
    op.add_column("purchase_order", sa.Column("candidates", JSONB(), nullable=True))
    op.add_column("purchase_order", sa.Column("revision", sa.Integer(),
                  nullable=False, server_default="1"))
    op.add_column("purchase_order", sa.Column("created_by", UUID(as_uuid=True),
                  sa.ForeignKey("app_user.id"), nullable=True))
    op.add_column("purchase_order", sa.Column("approved_by", UUID(as_uuid=True),
                  sa.ForeignKey("app_user.id"), nullable=True))
    op.add_column("purchase_order", sa.Column("approved_at", sa.DateTime(timezone=True),
                  nullable=True))
    op.add_column("purchase_order", sa.Column("rejected_by", UUID(as_uuid=True),
                  sa.ForeignKey("app_user.id"), nullable=True))
    op.add_column("purchase_order", sa.Column("rejected_at", sa.DateTime(timezone=True),
                  nullable=True))
    op.add_column("purchase_order", sa.Column("rejection_reason", sa.Text(), nullable=True))
    op.add_column("purchase_order", sa.Column("tracking_token", sa.String(64), nullable=True))
    op.add_column("purchase_order", sa.Column("first_opened_at", sa.DateTime(timezone=True),
                  nullable=True))
    op.add_column("purchase_order", sa.Column("first_clicked_at", sa.DateTime(timezone=True),
                  nullable=True))
    op.add_column("purchase_order", sa.Column("current_rung", sa.Integer(),
                  nullable=False, server_default="0"))
    op.add_column("purchase_order", sa.Column("next_escalation_at", sa.DateTime(timezone=True),
                  nullable=True))
    op.add_column("purchase_order", sa.Column("acknowledged_at", sa.DateTime(timezone=True),
                  nullable=True))
    op.add_column("purchase_order", sa.Column("acknowledged_channel", sa.String(20),
                  nullable=True))
    op.create_index("ix_purchase_order_needs_supplier", "purchase_order", ["needs_supplier"])
    op.create_index("ix_purchase_order_tracking_token", "purchase_order", ["tracking_token"])

    # ── 6. po_response deltas (§10) ───────────────────────────────────────────
    op.add_column("po_response", sa.Column("tracking_token", sa.String(64), nullable=True))
    op.add_column("po_response", sa.Column("message_id", sa.String(200), nullable=True))
    op.create_index("ix_po_response_tracking_token", "po_response", ["tracking_token"])


def downgrade():
    op.drop_index("ix_po_response_tracking_token", table_name="po_response")
    op.drop_column("po_response", "message_id")
    op.drop_column("po_response", "tracking_token")

    op.drop_index("ix_purchase_order_tracking_token", table_name="purchase_order")
    op.drop_index("ix_purchase_order_needs_supplier", table_name="purchase_order")
    for col in ("acknowledged_channel", "acknowledged_at", "next_escalation_at",
                "current_rung", "first_clicked_at", "first_opened_at", "tracking_token",
                "rejection_reason", "rejected_at", "rejected_by", "approved_at",
                "approved_by", "created_by", "revision", "candidates", "match_method",
                "no_contact_channel", "needs_supplier", "gst_mode", "igst"):
        op.drop_column("purchase_order", col)
    op.alter_column("purchase_order", "po_number", existing_type=sa.String(40),
                    nullable=False)
    op.alter_column("purchase_order", "supplier_id", existing_type=UUID(as_uuid=True),
                    nullable=False)

    for col in ("whatsapp_phone", "state_code", "supplier_type", "email_status"):
        op.drop_column("supplier", col)

    op.drop_index("ix_production_tracking_status", table_name="production_tracking")
    op.drop_index("ix_production_tracking_style_id", table_name="production_tracking")
    op.drop_index("ix_production_tracking_client_order_id", table_name="production_tracking")
    op.drop_table("production_tracking")

    op.drop_index("ix_po_tracking_event_tracking_token", table_name="po_tracking_event")
    op.drop_index("ix_po_tracking_event_purchase_order_id", table_name="po_tracking_event")
    op.drop_table("po_tracking_event")

    op.drop_index("ix_supply_history_normalized_description",
                  table_name="supplier_supply_history")
    op.drop_index("ix_supply_history_supplier_id", table_name="supplier_supply_history")
    op.drop_table("supplier_supply_history")
