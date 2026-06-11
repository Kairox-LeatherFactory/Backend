"""
================================================================================
modules/procurement/models.py — BOM Procurement Workflow schema (Stage 0)
================================================================================

The supplier/BOM/inventory side of the workflow doc, layered on the existing
buyer-order hierarchy (clients module). One flat models file matches the repo's
per-module convention.

CONVENTIONS
    - Every table mixes in UUIDMixin + TimestampMixin and uses the portable GUID
      column type from app/core/models.py (native UUID on Postgres, CHAR(32) on
      SQLite) so the same models run in prod and in the SQLite test suite.
    - Money = Numeric(12, 2) with a sibling String(3) `currency`; fractional
      quantities (dm² / yardage) = Numeric(12, 3).
    - Status/kind columns are plain VARCHAR storing an enum `.value` (see enums.py
      for the rationale) — never native PG ENUMs.
    - Semi-structured spec data is JSONB on Postgres, JSON on SQLite (JSON_VARIANT).
    - Cross-module foreign keys (client, client_order, style, app_user) are declared
      by TABLE NAME string only — no Python import of those modules — so the
      import-linter cross-module rule is respected. Relationships are defined only
      WITHIN this module.

FK MAP (per spec §1)
    document        <- client_order.source_document_id, spec_sheet, bom, purchase_order
    supplier        -> purchase_order -> { po_item, po_response }
    bom             -> { bom_item, inventory_check -> inventory_check_line }
    inventory_item  <- inventory_check_line, po_item
================================================================================
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, TimestampMixin, UUIDMixin
from app.modules.procurement.enums import (
    BomItemCategory,
    BomStatus,
    InventoryCheckStatus,
    InventoryLineStatus,
    NotificationStatus,
    POResponseStatus,
    POStatus,
)

# JSONB on Postgres, plain JSON on SQLite (tests). Lets spec sheets store wildly
# different shapes without 200 sparse typed columns.
JSON_VARIANT = JSON().with_variant(JSONB, "postgresql")


# ══════════════════════════════════════════════════════════════════════════
# Stage 1 — inputs (uploaded artifacts + extracted spec sheets)
# ══════════════════════════════════════════════════════════════════════════
class Document(Base, UUIDMixin, TimestampMixin):
    """Every uploaded artifact (order sheet, spec sheet, BOM quote, supplier-PO
    scan). `sha256` is unique → dedupe + the cache key for LLM extraction (don't
    re-bill identical uploads)."""
    __tablename__ = "document"
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
    client_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client_order.id"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), index=True)   # DocumentKind value
    filename: Mapped[str] = mapped_column(String(300))
    mime: Mapped[str | None] = mapped_column(String(120))
    storage_url: Mapped[str | None] = mapped_column(String(600))  # object storage
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    page_count: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True, index=True
    )


class SpecSheet(Base, UUIDMixin, TimestampMixin):
    """A parsed spec sheet. The two real shapes (Japanese measurement grid vs
    Jackie narrative tech pack) share almost no columns, so the data lives in
    JSONB with a `spec_type` discriminator; only universal fields are typed."""
    __tablename__ = "spec_sheet"
    client_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("client.id"), index=True)
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style.id"), nullable=True, index=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True, index=True
    )
    spec_type: Mapped[str] = mapped_column(String(30))          # SpecType value
    season: Mapped[str | None] = mapped_column(String(20))
    customer_label: Mapped[str | None] = mapped_column(String(120))
    measurements: Mapped[dict | None] = mapped_column(JSON_VARIANT)  # grid + tolerances
    attributes: Mapped[dict | None] = mapped_column(JSON_VARIANT)    # leather/pockets/...
    instructions: Mapped[dict | None] = mapped_column(JSON_VARIANT)  # narrative steps
    extracted_by: Mapped[str | None] = mapped_column(String(20))     # ExtractionSource value
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


# ══════════════════════════════════════════════════════════════════════════
# Stage 2/3 — BOM
# ══════════════════════════════════════════════════════════════════════════
class Bom(Base, UUIDMixin, TimestampMixin):
    """The BMO-1 header. One BOM per style per order (UNIQUE). `status` drives the
    Stage-3 gate; lock + revision + approver satisfy the 'locked, full revision
    history' requirement (field-level diffs live in audit_log)."""
    __tablename__ = "bom"
    __table_args__ = (
        UniqueConstraint("client_order_id", "style_id", name="uq_bom_order_style"),
    )
    client_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("client_order.id"), index=True
    )
    style_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("style.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=BomStatus.DRAFT.value, index=True)
    currency: Mapped[str | None] = mapped_column(String(3))
    garment_fob_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    bulk_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    order_qty: Mapped[int | None] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True
    )
    items: Mapped[list["BomItem"]] = relationship(
        back_populates="bom", cascade="all, delete-orphan"
    )


class BomItem(Base, UUIDMixin, TimestampMixin):
    """One BMO-1 line: Sheep Glass 34.5 dm² @1.80, Goat Suede, lining, thread,
    buttons (TBC), cutting/stitching, packaging, FOB charge. `material_color` is
    where the multi-colour dimension lands."""
    __tablename__ = "bom_item"
    bom_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("bom.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), default=BomItemCategory.MAIN_MATERIAL.value)
    name: Mapped[str] = mapped_column(String(200))
    material_color: Mapped[str | None] = mapped_column(String(80))
    qty_per_garment: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    uom: Mapped[str | None] = mapped_column(String(20))         # dm² / pc / unit
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    bulk_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    annotation: Mapped[str | None] = mapped_column(Text)        # handwritten notes
    source_ref: Mapped[str | None] = mapped_column(String(120))
    bom: Mapped["Bom"] = relationship(back_populates="items")


# ══════════════════════════════════════════════════════════════════════════
# Stage 4 — inventory
# ══════════════════════════════════════════════════════════════════════════
class InventoryItem(Base, UUIDMixin, TimestampMixin):
    """The INVENTORY master (normalized + deduped). Live, mutable stock; the
    inventory check reads it. `normalized_key` powers BOM-item -> stock matching."""
    __tablename__ = "inventory_item"
    description: Mapped[str] = mapped_column(String(400), index=True)
    normalized_key: Mapped[str | None] = mapped_column(String(400), index=True)
    uom: Mapped[str | None] = mapped_column(String(20))         # KGS / DCM / ROLL / NOS / PCS
    qty_on_hand: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    category: Mapped[str | None] = mapped_column(String(80))
    color: Mapped[str | None] = mapped_column(String(80))
    article_ref: Mapped[str | None] = mapped_column(String(120))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class InventoryCheck(Base, UUIDMixin, TimestampMixin):
    """One stock-status report per approved BOM (Stage 4)."""
    __tablename__ = "inventory_check"
    bom_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("bom.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default=InventoryCheckStatus.RUNNING.value)
    run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    lines: Mapped[list["InventoryCheckLine"]] = relationship(
        back_populates="check", cascade="all, delete-orphan"
    )


class InventoryCheckLine(Base, UUIDMixin, TimestampMixin):
    """Per-BOM-item SUFFICIENT / PARTIAL / OUT_OF_STOCK + exact shortfall, forwarded
    to Stage 5."""
    __tablename__ = "inventory_check_line"
    inventory_check_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("inventory_check.id"), index=True
    )
    bom_item_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("bom_item.id"), index=True)
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("inventory_item.id"), nullable=True, index=True
    )
    required_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    on_hand_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    shortfall_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    status: Mapped[str] = mapped_column(String(20), default=InventoryLineStatus.OUT_OF_STOCK.value)
    check: Mapped["InventoryCheck"] = relationship(back_populates="lines")


# ══════════════════════════════════════════════════════════════════════════
# Stage 5 — suppliers + supplier PO
# ══════════════════════════════════════════════════════════════════════════
class Supplier(Base, UUIDMixin, TimestampMixin):
    """From SUPPLIERS 'Supplier contact info' + the PO-form footers (GSTIN, terms).
    The article->supplier lookup in Stage 5 resolves here."""
    __tablename__ = "supplier"
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(160))
    service: Mapped[str | None] = mapped_column(String(160))    # category / what they supply
    gstin: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(String(400))
    currency: Mapped[str | None] = mapped_column(String(3), default="INR")
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, default=60)
    lead_time_days: Mapped[int | None] = mapped_column(Integer, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="supplier")


class PurchaseOrder(Base, UUIDMixin, TimestampMixin):
    """The SUPPLIER purchase order (PAKKAR -> supplier), e.g. "PO-07(25-26)". This
    is the name freed up by renaming the buyer order to `client_order`. Mirrors the
    suppler-po-form PDFs: Bill-To supplier, CGST/SGST 2.5%, totals, 60-day terms."""
    __tablename__ = "purchase_order"
    po_number: Mapped[str] = mapped_column(String(40), index=True)   # "PO-07(25-26)"
    supplier_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("supplier.id"), index=True)
    bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom.id"), nullable=True, index=True
    )
    client_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client_order.id"), nullable=True, index=True
    )
    buyer_ref: Mapped[str | None] = mapped_column(String(120))   # "PO-JAPAN-902/904-#CRIMIE"
    issue_date: Mapped[date | None] = mapped_column(Date)
    delivery_days: Mapped[int | None] = mapped_column(Integer)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, default=60)
    currency: Mapped[str | None] = mapped_column(String(3), default="INR")
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cgst: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sgst: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    round_off: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default=POStatus.DRAFT.value, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True
    )
    supplier: Mapped["Supplier"] = relationship(back_populates="purchase_orders")
    items: Mapped[list["PoItem"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )
    responses: Mapped[list["PoResponse"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class PoItem(Base, UUIDMixin, TimestampMixin):
    """The PO-form line grid (e.g. 100 GSM Padding-Body 220 m @38). Links back to the
    shortfall line it fulfils."""
    __tablename__ = "po_item"
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("purchase_order.id"), index=True
    )
    item_no: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(String(400))
    color: Mapped[str | None] = mapped_column(String(80))
    uom: Mapped[str | None] = mapped_column(String(20))
    qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    inventory_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("inventory_item.id"), nullable=True
    )
    bom_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom_item.id"), nullable=True
    )
    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="items")


class PoResponse(Base, UUIDMixin, TimestampMixin):
    """The Stage-5 5-hour escalation: email -> (no response) -> auto-call -> record
    confirmation."""
    __tablename__ = "po_response"
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("purchase_order.id"), index=True
    )
    channel: Mapped[str] = mapped_column(String(20))            # POResponseChannel value
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    status: Mapped[str] = mapped_column(String(20), default=POResponseStatus.PENDING.value)
    notes: Mapped[str | None] = mapped_column(Text)
    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="responses")


# ══════════════════════════════════════════════════════════════════════════
# Cross-cutting (Stage 3/5/6)
# ══════════════════════════════════════════════════════════════════════════
class Notification(Base, UUIDMixin, TimestampMixin):
    """Every alert in the doc: MD review notice -> automail after 5h -> autocall
    after 5h (Stage 3); PO email + read-receipt + escalation (Stage 5);
    material-ready MD alert (Stage 6). `scheduled_for`/`opened_at` drive the timers
    and read-receipt tracking; `parent_notification_id` chains an escalation."""
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
    entity_type: Mapped[str | None] = mapped_column(String(60))  # polymorphic ref
    entity_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default=NotificationStatus.PENDING.value, index=True)
    scheduled_for: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    parent_notification_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("notification.id"), nullable=True
    )


class AuditLog(Base, UUIDMixin, TimestampMixin):
    """'Full revision history including all edits, approving identity, timestamp.'
    General-purpose trail; the field-level before/after diff lives here while `bom`
    keeps approved_by/at + revision."""
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
