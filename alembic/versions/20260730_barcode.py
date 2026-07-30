"""barcode feature: registry, drawers, materials, suppliers, piece/event columns

Revision ID: 20260730_barcode
Revises: <SET TO CURRENT HEAD — likely the July-28 fixes revision>
Create Date: 2026-07-30

WHAT THIS ADDS
    New tables : barcode_registry, drawer, material_lot, material_reservation,
                 material_receipt, supplier, supplier_order
    New columns: piece.needs_lining, piece.drawer_id
                 production_event.leather_lot_id / lining_lot_id / consumption_qty
    New enum   : UserRole gains 'lining_manager' — added in the Python enum, NOT a
                 DB enum type (UserRole is stored as a String value in this schema,
                 so no ALTER TYPE is needed; confirm your app_user.role column is a
                 String and not a native PG enum before running).

ORACLE-PORTABILITY FLAGS (log for the Phase-3 migration file):
    - server_default "1" for a boolean is SQLite/PG-friendly; on Oracle use '1'
      with a NUMBER(1) or a CHAR check. GUID() already abstracts UUID vs CHAR(32).
    - No gen_random_uuid() here — ids come from the app (uuid4 default), which is
      portable. Keep it that way.
"""
from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects.postgresql import UUID as GUID   # the project's portable UUID type

revision = "20260730_barcode"
down_revision = "20260724_01_july28_fixes"   # ← set to your actual current head
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── NEW ROLE VALUE on the native PG enum ────────────────────────────────
    # app_user.role is Enum(UserRole, name="user_role") — a native PG type — so
    # the new member must be added to the DB type, not just the Python enum.
    # No-op on SQLite (tests), where there is no native enum type.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'lining_manager'")

    # ── supplier (referenced by material_lot + supplier_order) ──────────────
    op.create_table(
        "supplier",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("articles", sa.String(600)),
        sa.Column("contact", sa.String(200)),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_supplier_name", "supplier", ["name"])

    # ── material_lot ────────────────────────────────────────────────────────
    op.create_table(
        "material_lot",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("subtype", sa.String(20)),
        sa.Column("article", sa.String(120), nullable=False),
        sa.Column("colour", sa.String(80)),
        sa.Column("thickness", sa.String(40)),
        sa.Column("size", sa.String(40)),
        sa.Column("uom", sa.String(20), nullable=False),
        sa.Column("on_hand", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("supplier_id", GUID(), sa.ForeignKey("supplier.id"), nullable=True),
        sa.Column("attributes", sa.JSON()),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    for col in ("category", "subtype", "article", "colour", "thickness", "size"):
        op.create_index(f"ix_material_lot_{col}", "material_lot", [col])

    # ── material_reservation ────────────────────────────────────────────────
    op.create_table(
        "material_reservation",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("material_lot_id", GUID(),
                  sa.ForeignKey("material_lot.id"), nullable=False),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("status", sa.String(15), nullable=False, server_default="active"),
        sa.Column("reason", sa.String(120)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_material_reservation_lot", "material_reservation",
                    ["material_lot_id"])

    # ── supplier_order ──────────────────────────────────────────────────────
    op.create_table(
        "supplier_order",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("article", sa.String(120), nullable=False),
        sa.Column("colour", sa.String(80)),
        sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("uom", sa.String(20), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="ordered"),
        sa.Column("supplier_id", GUID(), sa.ForeignKey("supplier.id"), nullable=True),
        sa.Column("ordered_by", GUID(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("arrived_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_supplier_order_status", "supplier_order", ["status"])
    op.create_index("ix_supplier_order_article", "supplier_order", ["article"])

    # ── material_receipt (FKs supplier_order) ───────────────────────────────
    op.create_table(
        "material_receipt",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("material_lot_id", GUID(),
                  sa.ForeignKey("material_lot.id"), nullable=False),
        sa.Column("supplier_order_id", GUID(),
                  sa.ForeignKey("supplier_order.id"), nullable=True),
        sa.Column("approved_qty", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("rejected_qty", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("received_by", GUID(), sa.ForeignKey("app_user.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_material_receipt_lot", "material_receipt", ["material_lot_id"])

    # ── drawer (FKs piece; piece FKs drawer — create drawer first, add the
    #    piece.drawer_id FK after piece column exists) ─────────────────────────
    op.create_table(
        "drawer",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("code", sa.String(60), nullable=False),
        sa.Column("seq", sa.Integer, nullable=False),
        sa.Column("state", sa.String(20), nullable=False, server_default="waiting"),
        sa.Column("current_piece_id", GUID(),
                  sa.ForeignKey("piece.id"), nullable=True),
        sa.Column("leather_in", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("lining_in", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("sended_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_drawer_code"),
    )
    op.create_index("ix_drawer_code", "drawer", ["code"])
    op.create_index("ix_drawer_seq", "drawer", ["seq"])
    op.create_index("ix_drawer_state", "drawer", ["state"])

    # ── piece: new columns ──────────────────────────────────────────────────
    op.add_column("piece", sa.Column(
        "needs_lining", sa.Boolean, nullable=False, server_default=sa.true()))
    op.add_column("piece", sa.Column(
        "drawer_id", GUID(), sa.ForeignKey("drawer.id"), nullable=True))
    op.create_index("ix_piece_drawer_id", "piece", ["drawer_id"])

    # ── production_event: consumption columns ───────────────────────────────
    op.add_column("production_event", sa.Column(
        "leather_lot_id", GUID(), sa.ForeignKey("material_lot.id"), nullable=True))
    op.add_column("production_event", sa.Column(
        "lining_lot_id", GUID(), sa.ForeignKey("material_lot.id"), nullable=True))
    op.add_column("production_event", sa.Column(
        "consumption_qty", sa.Numeric(12, 3), nullable=True))

    # ── barcode_registry (FKs piece, employee, drawer, material_lot) ────────
    op.create_table(
        "barcode_registry",
        sa.Column("id", GUID(), primary_key=True),
        sa.Column("code", sa.String(120), nullable=False),
        sa.Column("type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(15), nullable=False, server_default="active"),
        sa.Column("piece_id", GUID(), sa.ForeignKey("piece.id"), nullable=True),
        sa.Column("employee_id", GUID(), sa.ForeignKey("employee.id"), nullable=True),
        sa.Column("drawer_id", GUID(), sa.ForeignKey("drawer.id"), nullable=True),
        sa.Column("material_lot_id", GUID(),
                  sa.ForeignKey("material_lot.id"), nullable=True),
        sa.Column("caption", sa.String(200)),
        sa.Column("retired_at", sa.DateTime(timezone=True)),
        sa.Column("retired_reason", sa.String(120)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("code", name="uq_barcode_code"),
    )
    op.create_index("ix_barcode_code", "barcode_registry", ["code"])
    op.create_index("ix_barcode_type", "barcode_registry", ["type"])
    op.create_index("ix_barcode_status", "barcode_registry", ["status"])
    op.create_index("ix_barcode_employee", "barcode_registry", ["employee_id"])
    op.create_index("ix_barcode_piece", "barcode_registry", ["piece_id"])


def downgrade() -> None:
    op.drop_table("barcode_registry")
    op.drop_column("production_event", "consumption_qty")
    op.drop_column("production_event", "lining_lot_id")
    op.drop_column("production_event", "leather_lot_id")
    op.drop_index("ix_piece_drawer_id", table_name="piece")
    op.drop_column("piece", "drawer_id")
    op.drop_column("piece", "needs_lining")
    op.drop_table("drawer")
    op.drop_table("material_receipt")
    op.drop_table("supplier_order")
    op.drop_table("material_reservation")
    op.drop_table("material_lot")
    op.drop_table("supplier")