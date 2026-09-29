"""Create and seed the accessory kind catalogue independently of SKU cleanup.

Revision ID: 20260929_acc_type_catalog
Revises: 20260929_missing_deferred_fks
"""
from __future__ import annotations

from datetime import datetime, timezone
import uuid

from alembic import op
import sqlalchemy as sa

from app.core.models import GUID

revision = "20260929_acc_type_catalog"
down_revision = "20260929_missing_deferred_fks"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(
    sa.dialects.postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def _catalogue_table():
    return sa.table(
        "accessory_type",
        sa.column("id", GUID()),
        sa.column("code", sa.String),
        sa.column("label", sa.String),
        sa.column("qty_field", sa.String),
        sa.column("qty_uom", sa.String),
        sa.column("requires", _JSON),
        sa.column("filters", _JSON),
        sa.column("size_varies_by_sku", sa.Boolean),
        sa.column("is_active", sa.Boolean),
        sa.column("first_seen_at", sa.DateTime(timezone=True)),
        sa.column("created_by", sa.String),
        sa.column("note", sa.String),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def _seed_accessory_types(bind) -> int:
    from app.modules.materials.accessory_catalog import SEED_TYPES

    table = _catalogue_table()
    existing = {row[0] for row in bind.execute(sa.select(table.c.code)).fetchall()}
    now = datetime.now(timezone.utc)
    added = 0
    for item in SEED_TYPES:
        if item["code"] in existing:
            continue
        bind.execute(table.insert().values(
            id=uuid.uuid4(), code=item["code"], label=item["label"],
            qty_field=item["qty_field"], qty_uom=item["qty_uom"],
            requires=item["requires"], filters=item["filters"],
            size_varies_by_sku=item["size_varies_by_sku"], is_active=True,
            first_seen_at=now, created_by="seed",
            note="Seeded with the accessory catalogue.",
            created_at=now, updated_at=now))
        added += 1
    return added


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if not inspector.has_table("accessory_type"):
        op.create_table(
            "accessory_type",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("code", sa.String(20), nullable=False),
            sa.Column("label", sa.String(80), nullable=True),
            sa.Column("qty_field", sa.String(20), nullable=False,
                      server_default="count"),
            sa.Column("qty_uom", sa.String(20), nullable=False,
                      server_default="pcs"),
            sa.Column("requires", _JSON, nullable=True),
            sa.Column("filters", _JSON, nullable=True),
            sa.Column("size_varies_by_sku", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("is_active", sa.Boolean(), nullable=False,
                      server_default=sa.true()),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_by", sa.String(120), nullable=True),
            sa.Column("note", sa.String(300), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("code", name="uq_accessory_type_code"),
        )

    index_names = {
        index["name"] for index in sa.inspect(bind).get_indexes("accessory_type")
    }
    if "ix_accessory_type_code" not in index_names:
        op.create_index("ix_accessory_type_code", "accessory_type", ["code"])

    seeded = _seed_accessory_types(bind)
    print(f"[20260929_acc_type_catalog] seeded {seeded} missing accessory kind(s).")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("accessory_type"):
        return

    index_names = {
        index["name"] for index in inspector.get_indexes("accessory_type")
    }
    if "ix_accessory_type_code" in index_names:
        op.drop_index("ix_accessory_type_code", table_name="accessory_type")
    op.drop_table("accessory_type")
