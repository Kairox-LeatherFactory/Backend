"""Cutting V2 — per-hide leather tracking and the sheet-wise cutting record

Revision ID: 20260901_cutting_v2
Revises: b7af20f6f3bb
Create Date: 2026-09-01

WHAT THIS IS FOR
──────────────────────────────────────────────────────────────────────────────
The cutting manager's real record lived in Excel: which hides arrived, what each
one measured, who they went to, the sheet the cutter handed back, the extra one
he needed. The ERP only ever saw the total, typed once at the end, so it could
not answer how much leather a garment actually took — the question the whole
material ledger exists for.

Two tables close that:

    material_sheet   ONE physical hide. Individually measured, individually
                     barcoded, issued to one cutter for one garment.
    cutting_row      ONE garment's cutting record — the Excel row — which the
                     hides attach themselves to.

Plus one column, `barcode_registry.material_sheet_id`, so a hide's label resolves
through the same single front door every other code does.

WHY A SHEET IS NOT A LOT
    Material is ONE LOT PER SPEC: a second lot for the same article/colour/
    thickness is refused with a 409 pointing at the first
    (MaterialService.create_lot). That rule is what stops stock fragmenting
    across duplicate rows. So ten hides of SUEDE-A32 NAVY are ten rows of one
    lot, not ten lots — hence a child table.

WHY `material_lot.on_hand` IS UNTOUCHED
    It stays the leather stock figure in dcm and stays the only thing
    MaterialService._decrement_nocommit moves, so every existing consumption
    path, shortfall warning and analytics read keeps working with no change.
    Sheets are the detail beneath that number. The two are reconciled by a
    REPORTED check, never a constraint: a delivery nobody sheeted must stay
    receivable, and a mismatch must be visible rather than fatal.

NO BACKFILL, AND THAT IS THE BACK-COMPATIBILITY ARGUMENT
    Every lot that predates this migration has zero sheets, and a piece with no
    cutting_row takes exactly the path it takes today — the cutting manager types
    a dcm and a lot, as before. Nothing already on the floor changes behaviour,
    and the two systems run side by side until a style is cut the new way.

PORTABILITY (CLAUDE.md §13)
    No JSONB. No ON CONFLICT. No gen_random_uuid() — ids come from UUIDMixin's
    uuid4 in Python. And no native PG enums: `status` on both tables is VARCHAR,
    so adding a sheet state or a row state never needs an ALTER TYPE. That is the
    trap that broke three deploys (lining_manager, security, merchandiser).

    Every constraint is named explicitly to match NAMING_CONVENTION in
    core/database.py — letting the database invent them is what makes a later
    downgrade unable to refer to them.

DELETE RULES
    Both tables' nullable FKs are born with ON DELETE SET NULL here, inline, so
    a live database gets the right rule from the CREATE rather than from a later
    sweep. They are registered in 20260818_fk_setnull_all's
    _NULLABLE_FKS_BORN_WITH_RULE so tests/unit/test_fk_delete_rules.py can still
    prove the models and the schema agree.

    The two exceptions are deliberate:
      material_sheet.material_lot_id  CASCADE  — a hide has no meaning without
                                                 its lot; it is ownership, not
                                                 reference.
      material_sheet.cutting_row_id   SET NULL — deleting a cutting row must
                                                 return its hides to stock,
                                                 never delete the hides.
"""
from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects.postgresql import UUID as GUID   # the project's portable UUID type

revision = "20260901_cutting_v2"
down_revision = "b7af20f6f3bb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    true_ = sa.text("1" if is_sqlite else "true")

    # ── the garment's cutting record ─────────────────────────────────────────
    # Created FIRST: material_sheet carries a foreign key to it.
    op.create_table(
        "cutting_row",
        sa.Column("id", GUID(), nullable=False),
        # SET NULL on all four parents, like production_event.piece_id: a cutting
        # record of work that physically happened must survive the administrative
        # deletion of a mis-imported piece or a retired style. The snapshot
        # columns below keep the orphaned row readable.
        sa.Column("piece_id", GUID(), nullable=True),
        sa.Column("style_id", GUID(), nullable=True),
        sa.Column("sku_id", GUID(), nullable=True),
        sa.Column("cutter_employee_id", GUID(), nullable=True),
        # THE SNAPSHOT. article/colour are patchable on the lot they came from
        # (MaterialService.update_lot), so reading them back through a FK would
        # let an edit today rewrite what was cut last month. Same rule as
        # piece_material_issue's snapshot columns.
        sa.Column("article", sa.String(length=120), nullable=True),
        sa.Column("colour", sa.String(length=80), nullable=True),
        sa.Column("size", sa.String(length=40), nullable=True),
        sa.Column("rc_no", sa.String(length=40), nullable=True),
        sa.Column("work_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="DRAFT"),
        # What the allocator aimed at, and whose number it was — "style_spec"
        # (a measurement someone signed off) or "size_baseline" (this system's
        # estimate, core/leather_norms.py). Kept beside what the row actually
        # got, so an over-allocation is diagnosable instead of merely visible.
        sa.Column("target_dcm", sa.Numeric(12, 3), nullable=True),
        sa.Column("target_source", sa.String(length=20), nullable=True),
        # DERIVED from the row's sheets, frozen at approval. Nullable while DRAFT
        # so it cannot be mistaken for a promise before one is made.
        sa.Column("total_dcm", sa.Numeric(12, 3), nullable=True),
        # An app_user.id — the LOGIN — never the scanned employee.id. Passing a
        # worker's id into an actor column is what 500'd the store scan.
        sa.Column("approved_by", GUID(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("logged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_cutting_row"),
        # ONE OPEN ROW PER GARMENT. Two rows for one jacket would each claim
        # hides for it and the ledger would double-count the leather.
        sa.UniqueConstraint("piece_id", name="uq_cutting_row_piece"),
        sa.ForeignKeyConstraint(["piece_id"], ["piece.id"], ondelete="SET NULL",
                                name="fk_cutting_row_piece_id_piece"),
        sa.ForeignKeyConstraint(["style_id"], ["style.id"], ondelete="SET NULL",
                                name="fk_cutting_row_style_id_style"),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], ondelete="SET NULL",
                                name="fk_cutting_row_sku_id_sku"),
        sa.ForeignKeyConstraint(
            ["cutter_employee_id"], ["employee.id"], ondelete="SET NULL",
            name="fk_cutting_row_cutter_employee_id_employee"),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["app_user.id"], ondelete="SET NULL",
            name="fk_cutting_row_approved_by_app_user"),
    )
    op.create_index("ix_cutting_row_piece_id", "cutting_row", ["piece_id"])
    op.create_index("ix_cutting_row_style_id", "cutting_row", ["style_id"])
    op.create_index("ix_cutting_row_sku_id", "cutting_row", ["sku_id"])
    op.create_index("ix_cutting_row_cutter_employee_id", "cutting_row",
                    ["cutter_employee_id"])
    op.create_index("ix_cutting_row_work_date", "cutting_row", ["work_date"])
    op.create_index("ix_cutting_row_status", "cutting_row", ["status"])
    # The grid's own query: this style, these rows, by state.
    op.create_index("ix_cutting_row_style_status", "cutting_row",
                    ["style_id", "status"])

    # ── one physical hide ────────────────────────────────────────────────────
    op.create_table(
        "material_sheet",
        sa.Column("id", GUID(), nullable=False),
        sa.Column("code", sa.String(length=60), nullable=False),
        # CASCADE — ownership, not reference. A hide has no meaning without the
        # lot whose article and colour describe it.
        sa.Column("material_lot_id", GUID(), nullable=False),
        # (12,3) matches production_event.consumption_qty, so a sum of sheets and
        # a logged consumption are the same scale and never need rounding to
        # compare.
        sa.Column("dcm", sa.Numeric(12, 3), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="IN_STOCK"),
        sa.Column("cutting_row_id", GUID(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_material_sheet"),
        sa.UniqueConstraint("code", name="uq_material_sheet_code"),
        sa.ForeignKeyConstraint(
            ["material_lot_id"], ["material_lot.id"], ondelete="CASCADE",
            name="fk_material_sheet_material_lot_id_material_lot"),
        sa.ForeignKeyConstraint(
            ["cutting_row_id"], ["cutting_row.id"], ondelete="SET NULL",
            name="fk_material_sheet_cutting_row_id_cutting_row"),
    )
    op.create_index("ix_material_sheet_code", "material_sheet", ["code"])
    op.create_index("ix_material_sheet_material_lot_id", "material_sheet",
                    ["material_lot_id"])
    op.create_index("ix_material_sheet_status", "material_sheet", ["status"])
    op.create_index("ix_material_sheet_cutting_row_id", "material_sheet",
                    ["cutting_row_id"])
    # The allocator's hot query: free hides of this lot.
    op.create_index("ix_material_sheet_lot_status", "material_sheet",
                    ["material_lot_id", "status"])

    # ── the hide's label resolves through the one front door ─────────────────
    # A LEATHER_SHEET registry row carries BOTH material_sheet_id and
    # material_lot_id: a scan has to answer "which hide" and "what article and
    # colour is it" in one read, and the lot is the only place the second half
    # lives. `type` still says which link is the subject.
    op.add_column("barcode_registry",
                  sa.Column("material_sheet_id", GUID(), nullable=True))
    op.create_index("ix_barcode_registry_material_sheet_id", "barcode_registry",
                    ["material_sheet_id"])
    op.create_foreign_key(
        "fk_barcode_registry_material_sheet_id_material_sheet",
        "barcode_registry", "material_sheet",
        ["material_sheet_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint("fk_barcode_registry_material_sheet_id_material_sheet",
                       "barcode_registry", type_="foreignkey")
    op.drop_index("ix_barcode_registry_material_sheet_id",
                  table_name="barcode_registry")
    op.drop_column("barcode_registry", "material_sheet_id")

    # material_sheet first — it references cutting_row.
    op.drop_index("ix_material_sheet_lot_status", table_name="material_sheet")
    op.drop_index("ix_material_sheet_cutting_row_id", table_name="material_sheet")
    op.drop_index("ix_material_sheet_status", table_name="material_sheet")
    op.drop_index("ix_material_sheet_material_lot_id", table_name="material_sheet")
    op.drop_index("ix_material_sheet_code", table_name="material_sheet")
    op.drop_table("material_sheet")

    op.drop_index("ix_cutting_row_style_status", table_name="cutting_row")
    op.drop_index("ix_cutting_row_status", table_name="cutting_row")
    op.drop_index("ix_cutting_row_work_date", table_name="cutting_row")
    op.drop_index("ix_cutting_row_cutter_employee_id", table_name="cutting_row")
    op.drop_index("ix_cutting_row_sku_id", table_name="cutting_row")
    op.drop_index("ix_cutting_row_style_id", table_name="cutting_row")
    op.drop_index("ix_cutting_row_piece_id", table_name="cutting_row")
    op.drop_table("cutting_row")
