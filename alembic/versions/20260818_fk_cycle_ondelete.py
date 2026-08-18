"""circular foreign keys: give every edge in a cycle an ON DELETE rule

Revision ID: 20260818_fk_cycles
Revises: 20260818_style_lining
Create Date: 2026-08-18

THE BUG THIS FIXES
──────────────────────────────────────────────────────────────────────────────

    Unable to delete rows as one of them is currently referenced by a foreign
    key constraint from the table `piece`.
      DETAIL: Key (id)=(4cbf23f5-…) is still referenced from table piece.

    Unable to delete rows as one of them is currently referenced by a foreign
    key constraint from the table `drawer`.
      DETAIL: Key (id)=(960511a2-…) is still referenced from table drawer.

Those two errors are ONE bug seen from both ends. `piece.drawer_id` references
`drawer.id` and `drawer.current_piece_id` references `piece.id`, so the two rows
each hold the other hostage: Postgres refuses the drawer delete because a piece
points at it, and refuses the piece delete because a drawer points at it. There
is no order that resolves — that is what a circular foreign key means, and no
amount of retrying or reordering in the UI can get past it. The only exits are a
delete rule on the constraint, deferring the constraint, or manually NULLing one
pointer first. This migration installs the delete rule.

WHY `SET NULL` AND NOT `CASCADE`, EVERYWHERE IN THIS FILE
    CASCADE on a cycle is a data-loss machine: deleting one drawer would delete
    its piece, which would delete... it also runs straight into "history is
    sacred" (CLAUDE.md §6). Every column repointed below is ALREADY NULLABLE and
    every NULL already means something the application handles:

        piece.drawer_id IS NULL            → the piece is on the drawer waiting
                                             list (imports/premint.
                                             allocate_waiting_pieces; the
                                             `no_drawer` store bucket)
        drawer.current_piece_id IS NULL    → the drawer is back in the WAITING
                                             pool (drawers.service.release_nocommit
                                             writes exactly this)
        client_order.source_document_id …  → provenance pointer; the order's own
                                             extracted data is intact without it
        submission.order/spec_document_id  → an empty intake slot
        document.submission_id             → a document not filed in a submission
        style.base_style_id                → a style with no declared base
        notification.parent_notification_id→ an escalation with no parent notice

    So SET NULL never invents a state; it moves the row into a state the code
    already knows how to read. That is the whole reason it is safe here.

THE FOUR CYCLES REPAIRED
    1. piece ↔ drawer                                   (mutual, 2 tables)
    2. document → submission → client_order → document  (3 tables)
       + document ↔ submission                          (the inner 2-table cycle)
    3. style → style                                    (self-referential)
    4. notification → notification                      (self-referential)

    Cycles 1–2 were found by walking Base.metadata's foreign keys and running
    Tarjan's SCC over the table graph; 3–4 are the self-loops from the same walk.
    Nothing else in the schema is cyclic.

WHY `use_alter` WAS NOT ALREADY ENOUGH
    Cycle 2 already carried `use_alter=True` on three of its edges. That flag is
    a CREATE-time instruction only — it tells SQLAlchemy to emit the constraint
    as a separate ALTER TABLE so `create_all` can order the CREATE TABLEs. It has
    no effect whatsoever on DELETE. Cycle 2 therefore created cleanly and still
    deadlocked on delete, which is why the model change adds `ondelete` there too
    rather than assuming `use_alter` covered it.

WHY THE CONSTRAINT NAME IS LOOKED UP AND NOT HARD-CODED
    Postgres has no "ALTER CONSTRAINT … SET ON DELETE"; the only way to change a
    delete rule is DROP + ADD. That makes the exact existing name load-bearing,
    and this schema has two sources for it: the metadata naming convention
    (`fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`) and three
    hand-named constraints on the document cycle. Rather than trust either, the
    helper asks the live database which constraint sits on that column and drops
    that one by its real name, then recreates it under the canonical name. A DB
    that was built before the naming convention landed converges here instead of
    erroring on a name that does not exist.

POSTGRES ONLY, BY DESIGN
    SQLite cannot ALTER a constraint at all (the test harness builds its schema
    from `Base.metadata.create_all`, which already carries the new `ondelete`
    from the model change), so this migration is a no-op off Postgres.

LOCKING
    Each ADD CONSTRAINT takes a SHARE ROW EXCLUSIVE lock on both tables and scans
    the child table to validate. At this schema's size (~10^3–10^4 rows) that is
    milliseconds. If `piece` ever grows past a few million rows, split each step
    into `ADD CONSTRAINT … NOT VALID` followed by a separate
    `VALIDATE CONSTRAINT` so the scan runs without blocking writes.

PORTABILITY (Phase-3 Oracle note)
    ON DELETE SET NULL is standard SQL and exists on Oracle. ON DELETE CASCADE
    would have been portable too — it is rejected above on data-safety grounds,
    not portability grounds.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260818_fk_cycles"
down_revision = "20260818_style_lining"
branch_labels = None
depends_on = None


# (table, column, referred_table, canonical constraint name)
#
# Every one of these is an edge that participates in a cycle. Plain
# (non-cyclic) foreign keys are deliberately NOT touched: a blocked delete on a
# non-cyclic FK is a normal, resolvable "delete the children first", and turning
# those into SET NULL would quietly orphan rows for no benefit.
_CYCLE_EDGES = [
    # ── cycle 1: piece ↔ drawer ──────────────────────────────────────────────
    ("piece", "drawer_id", "drawer", "fk_piece_drawer_id_drawer"),
    ("drawer", "current_piece_id", "piece", "fk_drawer_current_piece_id_piece"),
    # ── cycle 2: document → submission → client_order → document ─────────────
    ("client_order", "source_document_id", "document",
     "fk_client_order_source_document"),
    ("submission", "order_document_id", "document",
     "fk_submission_order_document"),
    ("submission", "spec_document_id", "document",
     "fk_submission_spec_document"),
    ("submission", "client_order_id", "client_order",
     "fk_submission_client_order_id_client_order"),
    ("document", "submission_id", "submission",
     "fk_document_submission_id_submission"),
    # ── cycles 3 & 4: the self-referential ones ──────────────────────────────
    ("style", "base_style_id", "style", "fk_style_base_style_id_style"),
    ("notification", "parent_notification_id", "notification",
     "fk_notification_parent_notification_id_notification"),
]


def _existing_fk_names(inspector, table: str, column: str, referred: str) -> list[str]:
    """Every FK on `table` that is exactly (column) → referred(id).

    Returns a list because a database that has been through a rename can carry a
    duplicate constraint on the same column; all of them get dropped and one
    canonical constraint is put back.
    """
    out = []
    for fk in inspector.get_foreign_keys(table):
        if (fk.get("constrained_columns") == [column]
                and fk.get("referred_table") == referred
                and fk.get("name")):
            out.append(fk["name"])
    return out


def _repoint(table: str, column: str, referred: str, name: str,
             ondelete: str | None) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return                                   # table not deployed yet
    cols = {c["name"] for c in inspector.get_columns(table)}
    if column not in cols:
        return                                   # column not deployed yet

    for existing in _existing_fk_names(inspector, table, column, referred):
        op.drop_constraint(existing, table, type_="foreignkey")

    op.create_foreign_key(
        name, table, referred, [column], ["id"], ondelete=ondelete,
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column, referred, name in _CYCLE_EDGES:
        _repoint(table, column, referred, name, ondelete="SET NULL")


def downgrade() -> None:
    """Put the constraints back with no delete rule — i.e. re-deadlock them.

    Kept honest rather than kept useful: the pre-migration state genuinely was
    "no ON DELETE", and a downgrade that silently left SET NULL in place would
    make the migration non-reversible in fact while claiming to be reversible.
    """
    if op.get_bind().dialect.name != "postgresql":
        return
    for table, column, referred, name in _CYCLE_EDGES:
        _repoint(table, column, referred, name, ondelete=None)
