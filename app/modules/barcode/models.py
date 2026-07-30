"""
================================================================================
modules/barcode/models.py — The barcode registry + the new physical entities
================================================================================

ONE REGISTRY, EVERY CODE.
    BarcodeRegistry is the single table every scan resolves through. Each printed
    code — a piece, a material lot, a drawer, an employee card — has exactly one
    row here, carrying its type, its status, and a nullable FK to the domain row
    it names. `resolve(code)` is therefore one indexed lookup, and there is one
    answer to "what is this code", never a guess.

WHY THE FK IS NULLABLE + POLYMORPHIC-BY-TYPE (not a hard relationship per type)
    A registry row points at exactly one of {piece, employee, drawer, material
    lot}. Modelling that as four nullable FKs keeps every link a real, indexed
    foreign key (so a deleted piece can't orphan a code) while letting one table
    serve all types. The `type` column says which FK is populated.

EMPLOYEE BARCODE IS EDITABLE; THE PIECE BARCODE IS NOT.
    A piece code is a permanent identity (it is Piece.code, minted at breakdown
    upload, printed on the garment). An employee code is a credential on a card:
    it can be reissued and it is RETIRED when the worker leaves. Retiring flips
    `status` to RETIRED — resolve() then returns 410 — and NEVER touches the
    employee row or any production/wage history. That is the whole point: you
    delete the scannable code, not the person.
================================================================================
"""
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, JSON_VARIANT, TimestampMixin, UUIDMixin


class BarcodeRegistry(Base, UUIDMixin, TimestampMixin):
    """Every printed/scannable code. resolve() reads this and nothing else first."""
    __tablename__ = "barcode_registry"
    __table_args__ = (
        UniqueConstraint("code", name="uq_barcode_code"),
    )
    code: Mapped[str] = mapped_column(String(120), index=True)   # normalised upper
    type: Mapped[str] = mapped_column(String(20), index=True)    # BarcodeType value
    status: Mapped[str] = mapped_column(String(15), index=True, default="active")

    # Exactly one of these is set, per `type`. All indexed, all real FKs.
    piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id"), nullable=True, index=True)
    employee_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("employee.id"), nullable=True, index=True)
    drawer_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("drawer.id"), nullable=True, index=True)
    material_lot_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("material_lot.id"), nullable=True, index=True)

    # Freeform caption for the label (STYLE · COLOUR · SIZE · #seq, etc.)
    caption: Mapped[str | None] = mapped_column(String(200))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_reason: Mapped[str | None] = mapped_column(String(120))


class Drawer(Base, UUIDMixin, TimestampMixin):
    """A physical storage drawer. STATIC code, RECYCLING state.

    seq is the human number on the drawer; code is the scannable label. A drawer
    is merged to exactly one (sku_id, piece_seq) at a time, holds that piece's
    parts, and returns to WAITING after the piece ships — so `current_piece_id`
    moves over the drawer's life while `code` never changes.
    """
    __tablename__ = "drawer"
    __table_args__ = (
        UniqueConstraint("code", name="uq_drawer_code"),
    )
    code: Mapped[str] = mapped_column(String(60), index=True)     # "DRW-014"
    seq: Mapped[int] = mapped_column(Integer, index=True)
    state: Mapped[str] = mapped_column(String(20), default="waiting", index=True)
    current_piece_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("piece.id"), nullable=True, index=True)
    leather_in: Mapped[bool] = mapped_column(Boolean, default=False)
    lining_in: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaterialLot(Base, UUIDMixin, TimestampMixin):
    """A batch of stock. Creating a lot registers a child barcode AND adds stock.

    STOCK IS THREE NUMBERS, computed the same way inventory has always meant it:
        on_hand   — physically in the building
        reserved  — committed to a required quantity (can't be spent elsewhere)
        available — on_hand − reserved   (a derived read, not a stored column, so
                    the two can never drift; see MaterialService.stock)

    Category-specific attributes live in `attributes` (JSON) rather than 20 mostly
    -null columns: leather needs dcm+thickness, thread needs mtrs+thickness,
    buttons need size, 'other' needs a description. The typed ones the queries
    filter on (article, colour, thickness, size) are promoted to real columns so
    the stock-check filter is indexable.
    """
    __tablename__ = "material_lot"
    category: Mapped[str] = mapped_column(String(20), index=True)   # MaterialCategory
    subtype: Mapped[str | None] = mapped_column(String(20), index=True)
    article: Mapped[str] = mapped_column(String(120), index=True)
    colour: Mapped[str | None] = mapped_column(String(80), index=True)
    thickness: Mapped[str | None] = mapped_column(String(40), index=True)
    size: Mapped[str | None] = mapped_column(String(40), index=True)
    uom: Mapped[str] = mapped_column(String(20))
    on_hand: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("supplier.id"), nullable=True, index=True)
    attributes: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class MaterialReservation(Base, UUIDMixin, TimestampMixin):
    """A soft allocation against a lot. available = on_hand − Σ active reservations.

    Never mutates on_hand — mirrors the shipped inventory_reservation pattern so a
    reserved requirement can't be silently spent, and releasing frees it without
    disturbing physical stock.
    """
    __tablename__ = "material_reservation"
    material_lot_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("material_lot.id"), index=True)
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    status: Mapped[str] = mapped_column(String(15), default="active", index=True)
    reason: Mapped[str | None] = mapped_column(String(120))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MaterialReceipt(Base, UUIDMixin, TimestampMixin):
    """One receiving record: approved qty added to a lot, rejected qty logged.

    Rejected quantity is kept for the supplier's quality history — it is the only
    place the factory learns which suppliers ship rejects.
    """
    __tablename__ = "material_receipt"
    material_lot_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("material_lot.id"), index=True)
    supplier_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("supplier_order.id"), nullable=True, index=True)
    approved_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    rejected_qty: Mapped[Decimal] = mapped_column(Numeric(14, 3), default=0)
    received_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True)


class Supplier(Base, UUIDMixin, TimestampMixin):
    """A material supplier. Minimal for v1 — the article→supplier suggestion reads
    `articles` (a comma list) as a simple lookup, NOT the AI procurement
    classifier (that is separate August-20 scope)."""
    __tablename__ = "supplier"
    name: Mapped[str] = mapped_column(String(200), index=True)
    articles: Mapped[str | None] = mapped_column(String(600))   # "SUEDE-A32,NAP-11"
    contact: Mapped[str | None] = mapped_column(String(200))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class SupplierOrder(Base, UUIDMixin, TimestampMixin):
    """A manual supplier order. Two states only: ORDERED → ARRIVED."""
    __tablename__ = "supplier_order"
    category: Mapped[str] = mapped_column(String(20))
    article: Mapped[str] = mapped_column(String(120), index=True)
    colour: Mapped[str | None] = mapped_column(String(80))
    qty: Mapped[Decimal] = mapped_column(Numeric(14, 3))
    uom: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(15), default="ordered", index=True)
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("supplier.id"), nullable=True, index=True)
    ordered_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True)
    arrived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))