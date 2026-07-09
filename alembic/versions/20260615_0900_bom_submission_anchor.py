"""bom anchored on submission (order/style nullable, order_identity snapshot)

A Stage-2 BOM is now generated from the order + spec sheets alone, anchored on the
procurement submission. The Client->Order->Style->SKU breakdown is created only at MD
approval, so client_order_id/style_id become nullable and a submission_id + client_id +
order_identity snapshot are added.

Revision ID: b1f3a2c9e7d4
Revises: 3e54e129d9aa
Create Date: 2026-06-15 09:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import UUID

revision = 'b1f3a2c9e7d4'
down_revision = '3e54e129d9aa'
branch_labels = None
depends_on = None


def upgrade():
    # order/style are populated only when the breakdown is materialised at approval
    op.alter_column('bom', 'client_order_id',
                    existing_type=UUID(), nullable=True)
    op.alter_column('bom', 'style_id',
                    existing_type=UUID(), nullable=True)

    op.add_column('bom', sa.Column('submission_id', UUID(), nullable=True))
    op.add_column('bom', sa.Column('client_id', UUID(), nullable=True))
    op.add_column('bom', sa.Column(
        'order_identity',
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'),
        nullable=True))

    op.create_index(op.f('ix_bom_submission_id'), 'bom', ['submission_id'], unique=False)
    op.create_index(op.f('ix_bom_client_id'), 'bom', ['client_id'], unique=False)
    op.create_unique_constraint('uq_bom_submission', 'bom', ['submission_id'])
    op.create_foreign_key(op.f('fk_bom_submission_id_submission'), 'bom', 'submission',
                          ['submission_id'], ['id'])
    op.create_foreign_key(op.f('fk_bom_client_id_client'), 'bom', 'client',
                          ['client_id'], ['id'])


def downgrade():
    op.drop_constraint(op.f('fk_bom_client_id_client'), 'bom', type_='foreignkey')
    op.drop_constraint(op.f('fk_bom_submission_id_submission'), 'bom', type_='foreignkey')
    op.drop_constraint('uq_bom_submission', 'bom', type_='unique')
    op.drop_index(op.f('ix_bom_client_id'), table_name='bom')
    op.drop_index(op.f('ix_bom_submission_id'), table_name='bom')
    op.drop_column('bom', 'order_identity')
    op.drop_column('bom', 'client_id')
    op.drop_column('bom', 'submission_id')
    op.alter_column('bom', 'style_id',
                    existing_type=UUID(), nullable=False)
    op.alter_column('bom', 'client_order_id',
                    existing_type=UUID(), nullable=False)
