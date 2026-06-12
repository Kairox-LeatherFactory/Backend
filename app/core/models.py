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
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from app.core.database import Base

# JSONB on Postgres, plain JSON on SQLite (tests) — shared by every module's models.
JSON_VARIANT = JSON().with_variant(JSONB, "postgresql")


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


# ══════════════════════════════════════════════════════════════════════════
# Cross-cutting tables (document / notification / audit_log)
# ──────────────────────────────────────────────────────────────────────────
# These three are referenced/written by EVERY procurement-side module (Stage-1
# intake stores Documents; bom export + supplier PO render Documents; bom approval
# + supplier escalation write Notifications; all stages write AuditLog). Hosting
# them in core lets a module persist one without importing another module's models
# — the strict cross-module boundary. Cross-table FKs are TABLE-NAME STRINGS only
# (no Python import of app.modules), so core still imports nothing from modules.
# Status/kind columns store an enum `.value`; defaults are string literals here so
# core needs no module-owned enum import.
# ══════════════════════════════════════════════════════════════════════════
class Document(Base, UUIDMixin, TimestampMixin):
    """Every uploaded/rendered artifact (order sheet, spec sheet, BOM quote, supplier
    PO PDF). `sha256` unique → dedupe + the LLM-extraction cache key. It deliberately
    carries NO `client_order_id` — the order is derived from the order sheet in Stage 2,
    so the link runs the other way (`client_order.source_document_id` → document)."""
    __tablename__ = "document"
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), index=True)   # DocumentKind value
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str | None] = mapped_column(String(120))
    storage_url: Mapped[str | None] = mapped_column(String(600))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True, index=True
    )
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("submission.id"), nullable=True, index=True
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(
        String(25), default="pending", index=True            # ValidationStatus.PENDING
    )
    classified_kind: Mapped[str | None] = mapped_column(String(30))
    classified_spec_type: Mapped[str | None] = mapped_column(String(30))
    classification_method: Mapped[str | None] = mapped_column(String(20))
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    client_match_code: Mapped[str | None] = mapped_column(String(60))
    scan_status: Mapped[str] = mapped_column(String(20), default="skipped")  # ScanStatus.SKIPPED
    validation_signals: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    submission: Mapped["object | None"] = relationship(  # → procurement.Submission (by name)
        "Submission", back_populates="documents", foreign_keys="Document.submission_id",
    )


class Notification(Base, UUIDMixin, TimestampMixin):
    """Every alert: MD BOM-review notice + 2-hour email escalation (Stage 3); PO dispatch
    + read-receipt + WhatsApp/call escalation (Stage 5); material-ready MD alert (Stage 6).
    `scheduled_for`/`opened_at` drive the timers; `parent_notification_id` chains an
    escalation. `supplier_id` FKs the supplier_po table by name."""
    __tablename__ = "notification"
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True, index=True
    )
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("supplier.id"), nullable=True, index=True
    )
    channel: Mapped[str] = mapped_column(String(20))            # NotificationChannel value
    type: Mapped[str] = mapped_column(String(40), index=True)   # NotificationType value
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # NotificationStatus.PENDING
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_notification_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("notification.id"), nullable=True
    )


class AuditLog(Base, UUIDMixin, TimestampMixin):
    """'Full revision history including all edits, approving identity, timestamp.'
    General-purpose trail; the field-level before/after diff lives here while each
    domain row keeps its own approved_by/at + revision."""
    __tablename__ = "audit_log"
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(40), index=True)  # BOM_APPROVE, PO_SEND, ...
    entity_type: Mapped[str | None] = mapped_column(String(60), index=True)
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    before: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    after: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
