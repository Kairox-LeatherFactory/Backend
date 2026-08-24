"""Per-piece material spec + the accessory issue ledger.

WHAT THIS IS FOR
    Until now the only material the factory could spend automatically was
    leather, and its quantity was typed by the cutting manager on every scan.
    Accessories — buttons, zips, thread — could be stocked, barcoded and received
    but were never decremented, so accessory `on_hand` only ever went up and the
    floor assembled each kit from memory.

    Two tables and four columns fix that: a style declares what one garment takes
    BEFORE it is released, and the store spends that recipe when it kits a drawer.

        style_material_spec    the recipe   — per style, optionally per SKU
        piece_material_issue   the ledger   — what was actually issued, per piece
        drawer.accessories_in  the third bucket, beside leather_in / lining_in
        style.material_spec_*  the header: who confirmed the recipe, and whether
                               they declared the style takes no accessories

NO BACKFILL, AND THAT IS THE BACK-COMPATIBILITY ARGUMENT.
    Every style that predates this migration has no spec lines, so
    `kit_required` is false for it, so the drawer completeness predicate
    (core/kit_rules.drawer_complete) collapses to exactly the two clauses it had
    before — and every drawer, RECEIVED transition and send on the floor behaves
    bit-identically. Nothing is retroactively required of work already in flight.

    `material_spec_confirmed_at` is NULLABLE and its NULL is load-bearing in the
    same way `style.needs_lining`'s is: it means NOBODY HAS BEEN ASKED. A NOT NULL
    default would state that a recipe had been signed off when none exists.

PORTABILITY (CLAUDE.md §13)
    No JSONB — nothing here is a JSON column. No ON CONFLICT — the kit's
    idempotency is a READ (see StyleSpecService.issue_kit_nocommit), not an
    upsert. No gen_random_uuid() — ids come from UUIDMixin's uuid4 in Python.
    And no native PG enums: category / subtype / source / uom are all VARCHAR, so
    adding a material kind or an issue source never needs an ALTER TYPE. That is
    the trap that broke three deploys (lining_manager, security, merchandiser).

    The only dialect-sensitive line is the boolean server_default, spelled the
    same way 20260819 spells it. Fully transactional either way.

Revision ID: 20260821_style_spec
Revises: 20260819_drw_wage
"""
from alembic import op
import sqlalchemy as sa

from sqlalchemy.dialects.postgresql import UUID as GUID   # the project's portable UUID type

revision = "20260821_style_spec"
down_revision = "20260819_drw_wage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    false_ = sa.text("0" if is_sqlite else "false")
    true_ = sa.text("1" if is_sqlite else "true")

    # ── the recipe ───────────────────────────────────────────────────────────
    # Every constraint is named EXPLICITLY to match NAMING_CONVENTION in
    # core/database.py. Letting the database invent them is what makes a later
    # downgrade or rename unable to refer to them.
    op.create_table(
        "style_material_spec",
        sa.Column("id", GUID(), nullable=False),
        # CASCADE on both parents — ownership, not reference. A recipe line has
        # no meaning without its style, and `sku_id` must not SET NULL: NULL
        # there MEANS "style-wide default", so nulling an override would promote
        # one colourway's line into everyone's. See _MEANINGFUL_NULL_FKS in
        # tests/unit/test_fk_delete_rules.py.
        sa.Column("style_id", GUID(), nullable=False),
        sa.Column("sku_id", GUID(), nullable=True),
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("subtype", sa.String(length=20), nullable=True),
        sa.Column("article", sa.String(length=120), nullable=False),
        sa.Column("colour", sa.String(length=80), nullable=True),
        sa.Column("thickness", sa.String(length=40), nullable=True),
        sa.Column("size", sa.String(length=40), nullable=True),
        # (12,3) is the PER-PIECE scale, matching production_event.consumption_qty.
        sa.Column("qty_per_piece", sa.Numeric(12, 3), nullable=False),
        sa.Column("uom", sa.String(length=20), nullable=False),
        sa.Column("material_lot_id", GUID(), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=true_),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_style_material_spec"),
        sa.ForeignKeyConstraint(["style_id"], ["style.id"], ondelete="CASCADE",
                                name="fk_style_material_spec_style_id_style"),
        sa.ForeignKeyConstraint(["sku_id"], ["sku.id"], ondelete="CASCADE",
                                name="fk_style_material_spec_sku_id_sku"),
        sa.ForeignKeyConstraint(
            ["material_lot_id"], ["material_lot.id"], ondelete="SET NULL",
            name="fk_style_material_spec_material_lot_id_material_lot"),
        # A BACKSTOP, NOT THE DEDUPE. Four of these columns are nullable and
        # NULLs compare distinct in a unique index on both Postgres and SQLite,
        # so two lines that both leave colour blank do NOT collide here.
        # StyleSpecRepository.find_duplicate_line is the real check.
        sa.UniqueConstraint("style_id", "sku_id", "category", "subtype",
                            "article", "colour", "thickness", "size",
                            name="uq_style_material_spec_line"),
    )
    op.create_index("ix_style_material_spec_style_active", "style_material_spec",
                    ["style_id", "is_active"])
    op.create_index("ix_style_material_spec_category", "style_material_spec",
                    ["category"])
    op.create_index("ix_style_material_spec_article", "style_material_spec",
                    ["article"])
    op.create_index("ix_style_material_spec_sku_id", "style_material_spec",
                    ["sku_id"])
    op.create_index("ix_style_material_spec_material_lot_id",
                    "style_material_spec", ["material_lot_id"])

    # ── the ledger ───────────────────────────────────────────────────────────
    # Created AFTER the recipe: it carries a foreign key to it.
    op.create_table(
        "piece_material_issue",
        sa.Column("id", GUID(), nullable=False),
        # ALL FOUR PARENT LINKS ARE SET NULL, nullable. A stock movement that
        # physically happened must survive the administrative deletion of a
        # mis-imported piece or a retired lot — the same rule
        # production_event.piece_id follows — and the four snapshot columns below
        # keep the orphaned row readable.
        sa.Column("piece_id", GUID(), nullable=True),
        sa.Column("drawer_id", GUID(), nullable=True),
        sa.Column("spec_line_id", GUID(), nullable=True),
        sa.Column("material_lot_id", GUID(), nullable=True),
        # THE SNAPSHOT. material_lot.article and .colour are PATCHABLE through
        # MaterialService.update_lot, so reading them back through the FK would
        # let an edit today rewrite what a garment was issued last month.
        sa.Column("category", sa.String(length=20), nullable=False),
        sa.Column("subtype", sa.String(length=20), nullable=True),
        sa.Column("article", sa.String(length=120), nullable=False),
        sa.Column("colour", sa.String(length=80), nullable=True),
        # (14,3) — a STOCK quantity, taken off material_lot.on_hand.
        sa.Column("qty", sa.Numeric(14, 3), nullable=False),
        sa.Column("uom", sa.String(length=20), nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False),
        sa.Column("issued_by_employee_id", GUID(), nullable=True),
        sa.Column("entered_by", sa.String(length=120), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_piece_material_issue"),
        sa.ForeignKeyConstraint(["piece_id"], ["piece.id"], ondelete="SET NULL",
                                name="fk_piece_material_issue_piece_id_piece"),
        sa.ForeignKeyConstraint(["drawer_id"], ["drawer.id"], ondelete="SET NULL",
                                name="fk_piece_material_issue_drawer_id_drawer"),
        sa.ForeignKeyConstraint(
            ["spec_line_id"], ["style_material_spec.id"],
            ondelete="SET NULL",
            name="fk_piece_material_issue_spec_line_id_style_material_spec"),
        sa.ForeignKeyConstraint(
            ["material_lot_id"], ["material_lot.id"], ondelete="SET NULL",
            name="fk_piece_material_issue_material_lot_id_material_lot"),
        sa.ForeignKeyConstraint(
            ["issued_by_employee_id"], ["employee.id"], ondelete="SET NULL",
            name="fk_piece_material_issue_issued_by_employee_id_employee"),
        # THE IDEMPOTENCY BACKSTOP. One row per (piece, recipe line), so a
        # concurrent double-scan loses one side to an IntegrityError that rolls
        # back its own decrement with it — stock cannot go out twice. The normal
        # double-tap never reaches here; it is caught by the outstanding read.
        sa.UniqueConstraint("piece_id", "spec_line_id",
                            name="uq_piece_material_issue_line"),
    )
    op.create_index("ix_piece_material_issue_piece_source",
                    "piece_material_issue", ["piece_id", "source"])
    op.create_index("ix_piece_material_issue_piece_id",
                    "piece_material_issue", ["piece_id"])
    op.create_index("ix_piece_material_issue_drawer_id",
                    "piece_material_issue", ["drawer_id"])
    op.create_index("ix_piece_material_issue_spec_line_id",
                    "piece_material_issue", ["spec_line_id"])
    op.create_index("ix_piece_material_issue_material_lot_id",
                    "piece_material_issue", ["material_lot_id"])
    op.create_index("ix_piece_material_issue_source",
                    "piece_material_issue", ["source"])

    # ── the third drawer bucket ──────────────────────────────────────────────
    # NOT NULL with a false default, so every drawer in the building reads
    # "unkitted" — which, combined with kit_required being false for every style
    # that has no spec, leaves the completeness predicate computing exactly what
    # it computed yesterday.
    op.add_column("drawer", sa.Column(
        "accessories_in", sa.Boolean(), nullable=False, server_default=false_))

    # ── the recipe header, on style ──────────────────────────────────────────
    # ALL NULLABLE, NO BACKFILL. A NULL confirmed_at means NOBODY HAS BEEN ASKED,
    # exactly as it does for style.needs_lining. Stamping the existing rows as
    # confirmed would claim a sign-off that never happened; stamping them
    # unconfirmed is harmless because the release gate only ever runs on DRAFT
    # styles and these are already RELEASED.
    op.add_column("style", sa.Column(
        "material_spec_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("style", sa.Column(
        "material_spec_confirmed_by", sa.String(length=120), nullable=True))
    # THREE-STATE: NULL nobody asked / True takes none / False takes some.
    op.add_column("style", sa.Column(
        "material_spec_no_accessories", sa.Boolean(), nullable=True))


def downgrade() -> None:
    """Reverse order: the header columns, the bucket, then the two tables.

    piece_material_issue drops FIRST because it holds a foreign key into
    style_material_spec.
    """
    op.drop_column("style", "material_spec_no_accessories")
    op.drop_column("style", "material_spec_confirmed_by")
    op.drop_column("style", "material_spec_confirmed_at")
    op.drop_column("drawer", "accessories_in")

    for name in ("ix_piece_material_issue_source",
                 "ix_piece_material_issue_material_lot_id",
                 "ix_piece_material_issue_spec_line_id",
                 "ix_piece_material_issue_drawer_id",
                 "ix_piece_material_issue_piece_id",
                 "ix_piece_material_issue_piece_source"):
        op.drop_index(name, table_name="piece_material_issue")
    op.drop_table("piece_material_issue")

    for name in ("ix_style_material_spec_material_lot_id",
                 "ix_style_material_spec_sku_id",
                 "ix_style_material_spec_article",
                 "ix_style_material_spec_category",
                 "ix_style_material_spec_style_active"):
        op.drop_index(name, table_name="style_material_spec")
    op.drop_table("style_material_spec")
