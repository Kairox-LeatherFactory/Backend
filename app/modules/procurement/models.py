"""
================================================================================
modules/procurement/models.py — Stage-1 intake schema
================================================================================

The upload front door: the `submission` upload-batch (the pairing surrogate that
becomes the client_order link in Stage 2) and the per-client × doc-kind validation
registry `client_template`.

The cross-cutting `document` / `notification` / `audit_log` tables moved to
app/core/models.py when the procurement monolith was split — they are written by
bom, inventory, and supplier_po too, so hosting them in core keeps a module from
importing another just to persist one. `Submission.documents` still relates to the
core `Document` by class-name string (the shared registry resolves it); no Python
import of core models is needed for the FK.
================================================================================
"""
import uuid

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, JSON_VARIANT, TimestampMixin, UUIDMixin
from app.modules.procurement.enums import SubmissionStatus, ValidationStatus  # noqa: F401


class Submission(Base, UUIDMixin, TimestampMixin):
    """The upload-batch that PAIRS an order sheet and a spec sheet (Stage-1 §2). The two
    `*_document_id` columns are the CURRENT slot pointers (a corrective re-upload
    supersedes the old doc and repoints the slot); `Document.submission_id` is the
    membership FK that keeps every version. `use_alter=True` on the slot FKs breaks the
    circular dependency with `document` so Alembic/create_all can order the CREATEs."""
    __tablename__ = "submission"
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(20), default=SubmissionStatus.OPEN.value, index=True
    )
    order_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id", use_alter=True, name="fk_submission_order_document"),
        nullable=True,
    )
    spec_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id", use_alter=True, name="fk_submission_spec_document"),
        nullable=True,
    )
    client_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client_order.id"), nullable=True, index=True
    )
    # Every document ever uploaded into this submission (incl. superseded), via the core
    # Document.submission_id membership FK. foreign_keys is pinned so SQLAlchemy doesn't
    # confuse this back-ref with the two slot-pointer FKs above.
    documents: Mapped[list["object"]] = relationship(
        "Document",
        back_populates="submission",
        foreign_keys="Document.submission_id",
    )


class ClientTemplate(Base, UUIDMixin, TimestampMixin):
    """A per-client × doc-kind validation profile (Stage-1 §4). Seeded idempotently from
    config/client_templates.yaml; the validator reads these rows at request time so
    onboarding a new client is one registry entry, no code change."""
    __tablename__ = "client_template"
    __table_args__ = (
        UniqueConstraint("client_code", "doc_kind", name="uq_client_template_code_kind"),
    )
    client_code: Mapped[str] = mapped_column(String(60), index=True)
    display_name: Mapped[str | None] = mapped_column(String(160))
    doc_kind: Mapped[str] = mapped_column(String(30), index=True)
    language: Mapped[str | None] = mapped_column(String(10))
    size_system: Mapped[str | None] = mapped_column(String(10))
    currency: Mapped[str | None] = mapped_column(String(3))
    expected_layout: Mapped[str | None] = mapped_column(String(20))
    spec_type_hint: Mapped[str | None] = mapped_column(String(30))
    accepted_mime: Mapped[list | None] = mapped_column(JSON_VARIANT)
    anchors: Mapped[list | None] = mapped_column(JSON_VARIANT)
    fingerprints: Mapped[list | None] = mapped_column(JSON_VARIANT)
    grid_signals: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    thresholds: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
