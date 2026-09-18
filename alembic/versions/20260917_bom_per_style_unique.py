"""Drop uq_bom_submission — one BOM per STYLE per submission, not one per submission

Revision ID: 20260917_bom_per_style
Revises: 20260911_pom_status
Create Date: 2026-09-17

UPDATED 2026-09-17 (Hamthan): bom carried two overlapping unique constraints,
uq_bom_submission (submission_id) and uq_bom_submission_style (submission_id,
style_signature). models.py annotated the composite one "replaces
uq_bom_submission", but the single-column constraint was never dropped — not in
the model, not in a migration — so both stayed live in Postgres.

That made the per-style BOM flow generate exactly ONE BOM per order sheet. A
submission breaks down into many styles (55e27857-3e0d-4b0c-a261-8218d76b1345
has eight), and once any one of them had a BOM, every other style's INSERT
failed with

    duplicate key value violates unique constraint "uq_bom_submission"

inside the Celery worker, long after POST /order-styles/{id}/generate-bom had
already returned its 202 — so the operator saw a queued task that silently
never produced a BOM.

uq_bom_submission_style keeps the rule that was actually intended: a style may
be generated once per submission, and a re-run replays the existing BOM instead
of inserting a second one.

SAFETY: this only ever RELAXES the schema, so no data can conflict with it.
downgrade() re-adds the old constraint and will legitimately fail on any
submission that has since generated more than one style's BOM — that is the
point, those rows are exactly what the constraint used to forbid.
"""
from alembic import op

revision = "20260917_bom_per_style"
down_revision = "20260911_pom_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF EXISTS: databases created from the current models.py metadata (tests,
    # fresh dev boxes via create_all) never had the constraint to begin with.
    op.execute("ALTER TABLE bom DROP CONSTRAINT IF EXISTS uq_bom_submission")


def downgrade() -> None:
    op.create_unique_constraint("uq_bom_submission", "bom", ["submission_id"])
