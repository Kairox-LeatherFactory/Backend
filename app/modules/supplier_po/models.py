"""
================================================================================
modules/supplier_po/models.py — Stage-5 supplier + purchase-order schema
================================================================================

Suppliers + the supplier↔article supply-history index (§1d), the supplier PURCHASE
ORDER and its line items + responses, the open-tracking engagement log (§6c), and the
production board (§8b). Split out of the former procurement monolith. Cross-module FKs
(bom, bom_item, client_order, style, document, inventory_item, app_user) are table-name
strings only; the supplier/supply-history/PO relationships are defined within this module.

TABLE GUIDE (each class is one table; see the per-class docstring for column detail)
  Supplier               the vendor directory + send target (contact, GSTIN, type, email_status).
  SupplierSupplyHistory  the §1d matching index — pre-aggregated (supplier, article) ledger evidence.
  PurchaseOrder          the supplier PO: money + the cross-check/send/escalation state machine.
  PoItem                 one PO line; keeps the bom_item/inventory_item back-links to the shortfall.
  PoResponse             one send/contact attempt (email/whatsapp/call) + its result.
  PoTrackingEvent        the unified engagement log (open/click/bounce/whatsapp/call).
  ProductionTracking     one row per style/order advancing through the §8c production ladder.
================================================================================
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, JSON_VARIANT, TimestampMixin, UUIDMixin
from app.modules.supplier_po.enums import (
    POResponseStatus,
    POStatus,
    ProductionTrackingStatus,
    SupplierEmailStatus,
)


class Supplier(Base, UUIDMixin, TimestampMixin):
    """From SUPPLIERS 'Supplier contact info' + the PO-form footers (GSTIN, terms).
    The article→supplier lookup in Stage 5 resolves here."""
    __tablename__ = "supplier"
    name: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(160))
    service: Mapped[str | None] = mapped_column(String(160))
    gstin: Mapped[str | None] = mapped_column(String(20))
    address: Mapped[str | None] = mapped_column(String(400))
    currency: Mapped[str | None] = mapped_column(String(3), default="INR")
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, default=60)
    lead_time_days: Mapped[int | None] = mapped_column(Integer, default=10)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    email_status: Mapped[str] = mapped_column(
        String(20), default=SupplierEmailStatus.UNKNOWN.value
    )
    supplier_type: Mapped[str | None] = mapped_column(String(20))   # SupplierType value
    state_code: Mapped[str | None] = mapped_column(String(2))       # GSTIN state code
    whatsapp_phone: Mapped[str | None] = mapped_column(String(50))
    purchase_orders: Mapped[list["PurchaseOrder"]] = relationship(back_populates="supplier")
    supply_history: Mapped[list["SupplierSupplyHistory"]] = relationship(
        back_populates="supplier", cascade="all, delete-orphan"
    )


class SupplierSupplyHistory(Base, UUIDMixin, TimestampMixin):
    """The §1d matching index — pre-aggregated (supplier, article) evidence from the
    provision ledger, normalized with the SAME Stage-4 normalizer."""
    __tablename__ = "supplier_supply_history"
    __table_args__ = (
        UniqueConstraint("supplier_id", "normalized_description",
                         name="uq_supply_history_supplier_article"),
    )
    supplier_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("supplier.id"), index=True
    )
    normalized_description: Mapped[str] = mapped_column(String(400), index=True)
    raw_description: Mapped[str | None] = mapped_column(String(400))
    mode: Mapped[str | None] = mapped_column(String(20))
    uom: Mapped[str | None] = mapped_column(String(20))
    txn_count: Mapped[int] = mapped_column(Integer, default=1)
    first_purchased_at: Mapped[date | None] = mapped_column(Date)
    last_purchased_at: Mapped[date | None] = mapped_column(Date)
    last_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    supplier: Mapped["Supplier"] = relationship(back_populates="supply_history")


class PurchaseOrder(Base, UUIDMixin, TimestampMixin):
    """The SUPPLIER purchase order (PAKKAR → supplier), e.g. "PO-07(25-26)". Mirrors the
    suppler-po-form PDFs: Bill-To supplier, CGST/SGST or IGST, totals, 60-day terms."""
    __tablename__ = "purchase_order"
    po_number: Mapped[str | None] = mapped_column(String(40), index=True)   # allocated AT SEND (§2d)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("supplier.id", ondelete="SET NULL"), nullable=True, index=True
    )
    bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom.id", ondelete="SET NULL"), nullable=True, index=True
    )
    client_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client_order.id", ondelete="SET NULL"), nullable=True, index=True
    )
    buyer_ref: Mapped[str | None] = mapped_column(String(120))
    issue_date: Mapped[date | None] = mapped_column(Date)
    delivery_days: Mapped[int | None] = mapped_column(Integer)
    payment_terms_days: Mapped[int | None] = mapped_column(Integer, default=60)
    currency: Mapped[str | None] = mapped_column(String(3), default="INR")
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cgst: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    sgst: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    igst: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))      # §2c inter-state
    gst_mode: Mapped[str | None] = mapped_column(String(10))         # INTRA | INTER
    round_off: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default=POStatus.DRAFT.value, index=True)
    needs_supplier: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    no_contact_channel: Mapped[bool] = mapped_column(Boolean, default=False)
    match_method: Mapped[str | None] = mapped_column(String(20))
    candidates: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id", ondelete="SET NULL"), nullable=True
    )
    tracking_token: Mapped[str | None] = mapped_column(String(64), index=True)
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_rung: Mapped[int] = mapped_column(Integer, default=0)
    next_escalation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_channel: Mapped[str | None] = mapped_column(String(20))
    supplier: Mapped["Supplier"] = relationship(back_populates="purchase_orders")
    items: Mapped[list["PoItem"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )
    responses: Mapped[list["PoResponse"]] = relationship(
        back_populates="purchase_order", cascade="all, delete-orphan"
    )


class PoItem(Base, UUIDMixin, TimestampMixin):
    """The PO-form line grid. Links back to the shortfall line it fulfils."""
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
        GUID(), ForeignKey("inventory_item.id", ondelete="SET NULL"), nullable=True
    )
    bom_item_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom_item.id", ondelete="SET NULL"), nullable=True
    )
    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="items")


class PoResponse(Base, UUIDMixin, TimestampMixin):
    """The Stage-5 escalation: email → (no response) → WhatsApp → auto-call → record
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
    tracking_token: Mapped[str | None] = mapped_column(String(64), index=True)
    message_id: Mapped[str | None] = mapped_column(String(200))
    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="responses")


class PoTrackingEvent(Base, UUIDMixin, TimestampMixin):
    """The unified engagement log (§6c): open/click/delivery/bounce/complaint + each
    WhatsApp/call escalation event. Self-hosted read-state (no third-party SaaS)."""
    __tablename__ = "po_tracking_event"
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("purchase_order.id"), index=True
    )
    po_response_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("po_response.id", ondelete="SET NULL"), nullable=True
    )
    tracking_token: Mapped[str | None] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(20))         # POTrackingEventType value
    channel: Mapped[str | None] = mapped_column(String(20))
    ip: Mapped[str | None] = mapped_column(String(60))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    meta: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductionTracking(Base, UUIDMixin, TimestampMixin):
    """One row per style/order (§8b) advancing through the §8c status ladder. System-
    driven on each procurement stage transition + manually nudgeable by DM/Cutting/MD."""
    __tablename__ = "production_tracking"
    __table_args__ = (
        UniqueConstraint("client_order_id", "style_id",
                         name="uq_production_tracking_order_style"),
    )
    client_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("client_order.id"), index=True
    )
    style_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("style.id"), index=True)
    bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom.id", ondelete="SET NULL"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30), default=ProductionTrackingStatus.AWAITING_BOM.value, index=True
    )
    po_count: Mapped[int] = mapped_column(Integer, default=0)
    po_confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    material_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
