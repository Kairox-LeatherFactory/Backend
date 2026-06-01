"""
================================================================================
core/models.py — Shared model mixins + a portable UUID column type
================================================================================

PURPOSE
    Reusable building blocks every table mixes in:
      UUIDMixin       a UUID primary key (`id`)
      TimestampMixin  auto-managed created_at / updated_at columns

WHY A CUSTOM GUID TYPE
    The previous version imported postgresql.UUID directly, which makes the
    models Postgres-only and breaks the SQLite-backed test suite. `GUID` below
    stores as native UUID on Postgres and as CHAR(32) on SQLite, so the SAME
    models run in production (Postgres) and in tests (SQLite) unchanged.
================================================================================
"""
import uuid
from datetime import datetime

from sqlalchemy import CHAR, DateTime, func
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


class GUID(TypeDecorator):
    """Platform-independent UUID: native on Postgres, CHAR(32) elsewhere."""
    impl = CHAR
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        return value.hex if isinstance(value, uuid.UUID) else uuid.UUID(str(value)).hex

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


class UUIDMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        GUID(), primary_key=True, default=uuid.uuid4
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
