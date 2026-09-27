"""The accessory packet scan, and the wrong-size approval that gates it.

WHAT THIS IS FOR

    An M-size button in an L-size jacket is the most expensive mistake this
    factory can make. It is invisible on the floor, it is found by the client in
    Dubai, and it is paid for in return freight plus a remade garment. Until now
    the store could not detect it even in principle: one scan on the garment
    issued every accessory line the recipe named, straight from the recipe, so no
    physical packet was ever part of the exchange.

    The store now issues accessories ONE PACKET AT A TIME, identified by the
    packet's own LOT-ACC label. That puts the packet's size and the garment's size
    in the same comparison, and a mismatch is REFUSED — not flagged. This table is
    what a refusal leaves behind for a DM/MD to answer, because the scan itself
    must fail while the question has to outlive it.

THE DATA FIX, AND WHY IT IS HERE AND NOT FORWARD-ONLY

    `style_material_spec.garment_size` scopes a recipe line to one garment size.
    It used to be GUESSED from the material's own size whenever that size read as
    a garment size — and any bare number from 30 to 70 read as one, because those
    are the EU jacket rungs. So a 60cm zip entered as size '60' was silently
    scoped to 4XL garments, and a 45cm zip to XS.

    That is not a smaller recipe for the other sizes. A line that reaches no
    garment is ABSENT: `merge_lines` drops it, `kit_required` comes back False,
    `piece_complete` collapses to leather-and-lining, and every garment of every
    other size is complete, sendable and shipped without its zip. Leaving those
    rows in place would leave live styles in exactly that state, so the guess is
    undone here: where an ACCESSORY line's garment_size is identical to a purely
    NUMERIC material size, it was the inference and not a human, and it goes back
    to NULL — every size, which is what an unscoped line has always meant.

    The release gate then asks the DM to confirm or split those lines
    (kit_rules.accessory_size_gaps / accessory_size_ambiguities), which is the
    right place for the question: it is the last moment anyone can be asked.

    ALPHA sizes are left ALONE. 'L' next to garment_size 'L' is a line somebody
    meant, and clearing it would widen a genuinely size-specific button to every
    garment — the opposite error, and this one spends stock.

Revision ID: 20260927_kit_packet_scan
Revises: 20260923_material_arrival
"""
from alembic import op
import sqlalchemy as sa

from app.core.models import GUID

revision = "20260927_kit_packet_scan"
down_revision = "20260923_material_arrival"
branch_labels = None
depends_on = None


def _spec_table():
    return sa.table(
        "style_material_spec",
        sa.column("id", GUID()),
        sa.column("category", sa.String),
        sa.column("size", sa.String),
        sa.column("garment_size", sa.String),
    )


def clear_inferred_garment_sizes(bind) -> int:
    """Undo the numeric-size inference. Returns how many lines were freed.

    A MODULE-LEVEL FUNCTION, NOT INLINE IN upgrade(), so it can be tested. The
    alembic chain cannot run on SQLite — an early revision uses create_foreign_key,
    which SQLite has no ALTER for — so a data fix written inline inside upgrade()
    is a data fix that only ever runs for the first time on production. This one is
    exercised by tests/integration/test_garment_size_backfill.py against a real
    table.

    DONE IN PYTHON, NOT SQL, because "is this string all digits" is spelled three
    different ways across Postgres and SQLite (~, REGEXP, GLOB). One SELECT and one
    UPDATE per row is affordable: these are recipe lines, in the dozens.
    """
    spec = _spec_table()
    rows = bind.execute(
        sa.select(spec.c.id, spec.c.size, spec.c.garment_size).where(
            spec.c.category == "ACCESSORY",
            spec.c.garment_size.isnot(None),
            spec.c.size.isnot(None),
        )
    ).fetchall()

    cleared = 0
    for row_id, size, garment_size in rows:
        size_token = str(size or "").strip()
        # ONLY where the two are the same string AND that string is a bare number.
        # Same-but-alpha ('L' / 'L') is a line a human meant, and clearing it would
        # widen a genuinely size-specific button to every garment — the opposite
        # error, and that one spends stock. Different values mean somebody set
        # garment_size explicitly and it is not ours to touch.
        if not size_token.isdigit():
            continue
        if size_token.upper() != str(garment_size or "").strip().upper():
            continue
        bind.execute(
            sa.update(spec).where(spec.c.id == row_id)
            .values(garment_size=None))
        cleared += 1
    return cleared


def upgrade() -> None:
    # ── the approval record ──────────────────────────────────────────────────
    op.create_table(
        "kit_substitution_request",
        sa.Column("id", GUID(), primary_key=True),
        # CASCADE on the piece and the line, unlike the issue ledger's SET NULL:
        # a pending approval for a deleted piece is not history worth keeping, it
        # is a question nobody can answer. The audit_log row beside it survives.
        sa.Column("piece_id", GUID(), nullable=False),
        sa.Column("spec_line_id", GUID(), nullable=False),
        sa.Column("material_lot_id", GUID(), nullable=True),
        # Snapshotted so the DM's queue needs no joins and still reads correctly
        # after a lot is re-articled — the same reason piece_material_issue keeps
        # its four snapshot columns.
        sa.Column("garment_size", sa.String(40), nullable=True),
        sa.Column("lot_size", sa.String(40), nullable=True),
        sa.Column("article", sa.String(120), nullable=True),
        sa.Column("colour", sa.String(80), nullable=True),
        sa.Column("subtype", sa.String(20), nullable=True),
        sa.Column("qty", sa.Numeric(14, 3), nullable=True),
        # PENDING | APPROVED | REJECTED | CONSUMED. A plain String, not a native
        # PG enum: CLAUDE.md §13 — a label cannot be dropped and the ORM persists
        # the member NAME, and this value set is owned by one module.
        sa.Column("status", sa.String(15), nullable=False,
                  server_default="PENDING"),
        # The two identities the store already splits: the card that was scanned
        # and the login that scanned it.
        sa.Column("requested_by_employee_id", GUID(), nullable=True),
        sa.Column("requested_by", sa.String(120), nullable=True),
        sa.Column("reason", sa.String(300), nullable=True),
        sa.Column("decided_by_user_id", GUID(), nullable=True),
        sa.Column("decided_by", sa.String(120), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.String(300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["piece_id"], ["piece.id"], ondelete="CASCADE",
                                name="fk_kit_substitution_piece"),
        sa.ForeignKeyConstraint(["spec_line_id"], ["style_material_spec.id"],
                                ondelete="CASCADE",
                                name="fk_kit_substitution_spec_line"),
        sa.ForeignKeyConstraint(["material_lot_id"], ["material_lot.id"],
                                ondelete="SET NULL",
                                name="fk_kit_substitution_lot"),
        sa.ForeignKeyConstraint(["requested_by_employee_id"], ["employee.id"],
                                ondelete="SET NULL",
                                name="fk_kit_substitution_employee"),
        sa.ForeignKeyConstraint(["decided_by_user_id"], ["app_user.id"],
                                ondelete="SET NULL",
                                name="fk_kit_substitution_decided_by"),
        # IDEMPOTENT ON (piece, line, lot). The protocol is "scan, get refused,
        # wait, scan again", so every re-scan must find the existing ask rather
        # than pile up identical rows for the DM to wade through.
        sa.UniqueConstraint("piece_id", "spec_line_id", "material_lot_id",
                            name="uq_kit_substitution_request"),
    )
    op.create_index("ix_kit_substitution_request_piece_id",
                    "kit_substitution_request", ["piece_id"])
    op.create_index("ix_kit_substitution_request_spec_line_id",
                    "kit_substitution_request", ["spec_line_id"])
    op.create_index("ix_kit_substitution_request_material_lot_id",
                    "kit_substitution_request", ["material_lot_id"])
    op.create_index("ix_kit_substitution_request_status",
                    "kit_substitution_request", ["status"])

    # ── undo the inferred garment_size (see the module docstring) ────────────
    cleared = clear_inferred_garment_sizes(op.get_bind())
    print(f"[20260927_kit_packet_scan] cleared inferred garment_size on "
          f"{cleared} accessory recipe line(s) — they now apply to every size, "
          f"and the release gate will ask the DM to confirm or split them.")


def downgrade() -> None:
    # THE DATA FIX IS NOT REVERSED, deliberately. Re-deriving garment_size from a
    # numeric material size would restore the very bug this migration removes —
    # and it is not recoverable anyway: after the upgrade a cleared line and a
    # line a human deliberately left unscoped are the same row.
    op.drop_index("ix_kit_substitution_request_status",
                  table_name="kit_substitution_request")
    op.drop_index("ix_kit_substitution_request_material_lot_id",
                  table_name="kit_substitution_request")
    op.drop_index("ix_kit_substitution_request_spec_line_id",
                  table_name="kit_substitution_request")
    op.drop_index("ix_kit_substitution_request_piece_id",
                  table_name="kit_substitution_request")
    op.drop_table("kit_substitution_request")
