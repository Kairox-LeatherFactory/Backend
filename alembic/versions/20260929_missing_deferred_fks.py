"""Add the four deferred (use_alter) foreign keys the pre-squash chain never created.

Revision ID: 20260929_missing_deferred_fks
Revises: 20260929_accessory_sku_scope
Create Date: 2026-09-29

WHAT THIS REPAIRS
    Four columns carry no foreign key at all on a database that was built by the
    OLD chain, before the 2026-09-28 squash:

        drawer.current_piece_id        -> piece.id
        submission.order_document_id   -> document.id
        submission.spec_document_id    -> document.id
        client_order.source_document_id-> document.id

    All four are declared `use_alter=True` in the models and in the squashed
    baseline (20260928_1013_initial_migration), because each one closes a CIRCULAR
    dependency between two tables — neither can be created first with the FK
    inline, so SQLAlchemy emits it as a separate ALTER after both tables exist.
    A database created from the baseline therefore HAS them.

    A database created by the chain the squash replaced does not, and no future
    `upgrade head` would ever add them: the only revision that declares them is the
    baseline, which such a database is stamped past. So the drift is permanent and
    silent — `compare_metadata` reports it on every run and nothing ever fixes it.

WHY IT MATTERS RATHER THAN BEING COSMETIC
    `ondelete='SET NULL'` is the point of three of the four. Without the
    constraint there is no ON DELETE behaviour at all, so deleting a `document`
    leaves `submission.order_document_id` and `client_order.source_document_id`
    pointing at a row that is gone. Stage-1 intake reads those ids to re-render a
    document's validation verdict (procurement/service.get_document_report), and a
    dangling id reads as a 404 on a submission that looks complete.

IDEMPOTENT BY CONSTRUCTION — this is the part to keep if you edit it.
    On a FRESH database the baseline has already created all four, so this
    revision must add NOTHING. It checks pg_constraint by name before each ALTER
    rather than assuming which kind of database it is running against. That is the
    same posture as the `ALTER TYPE ... ADD VALUE IF NOT EXISTS` guards elsewhere
    in this repo, and it is what makes the revision safe to run on both a repaired
    production database and a brand-new one.

    Guarded to PostgreSQL. SQLite has no pg_constraint, and cannot
    ALTER TABLE ... ADD CONSTRAINT at all, so on SQLite this is a no-op — which
    keeps the test suite (CLAUDE.md §14) running the chain unchanged.

THE ORPHAN CHECK WAS DONE, AND IT MATTERS
    An ALTER TABLE ... ADD FOREIGN KEY fails outright if any existing row points
    at a row that is not there. All four columns were verified to hold NO non-null
    values before this was written, so there is nothing to clean first. If that is
    ever untrue on another environment, this revision will fail LOUDLY with the
    offending constraint named — which is the correct outcome. Do not add NOT VALID
    to make it pass; an unvalidated constraint is drift wearing a constraint's name.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260929_missing_deferred_fks"
down_revision = "20260929_accessory_sku_scope"
branch_labels = None
depends_on = None


# (constraint name, table, local column, referenced table, referenced column)
_DEFERRED_FKS = [
    ("fk_drawer_current_piece_id_piece", "drawer", "current_piece_id", "piece", "id"),
    ("fk_submission_order_document", "submission", "order_document_id", "document", "id"),
    ("fk_submission_spec_document", "submission", "spec_document_id", "document", "id"),
    ("fk_client_order_source_document", "client_order", "source_document_id",
     "document", "id"),
]


def _existing(bind, names: list[str]) -> set[str]:
    """Which of these constraint names Postgres already holds."""
    rows = bind.execute(
        sa.text("select conname from pg_constraint where conname = any(:names)"),
        {"names": names},
    ).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    have = _existing(bind, [f[0] for f in _DEFERRED_FKS])
    added = []
    for name, table, column, ref_table, ref_column in _DEFERRED_FKS:
        if name in have:
            continue
        op.create_foreign_key(
            name, table, ref_table, [column], [ref_column], ondelete="SET NULL",
        )
        added.append(name)

    if added:
        print(f"[20260929_missing_deferred_fks] added {len(added)} deferred foreign "
              f"key(s) the pre-squash chain never created: {', '.join(added)}")
    else:
        print("[20260929_missing_deferred_fks] all four deferred foreign keys were "
              "already present — nothing to do (this is the fresh-database case).")


def downgrade() -> None:
    """Drop only what this revision could have added.

    Guarded the same way as the upgrade, so downgrading a fresh database — where
    the BASELINE created these four, not this revision — does not strip constraints
    that the baseline is still responsible for re-creating. Downgrading past the
    baseline drops the tables anyway.
    """
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    have = _existing(bind, [f[0] for f in _DEFERRED_FKS])
    for name, table, _column, _ref_table, _ref_column in _DEFERRED_FKS:
        if name in have:
            op.drop_constraint(name, table, type_="foreignkey")
