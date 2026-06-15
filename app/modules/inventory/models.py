"""
================================================================================
modules/inventory/models.py — Stage-4 inventory schema
================================================================================

The inventory master, the per-BOM stock check + its lines, the soft reservation
ledger, and the alias / UOM reference tables. Split out of the former procurement
monolith. Cross-module FKs (bom, bom_item, app_user) are table-name strings only.

TABLE GUIDE (each class is one table; see the per-class docstring for column detail)
  InventoryItem         the normalized, deduped stock master (live, mutable). normalized_key matches.
  InventoryCheck        one stock-status report per approved BOM (running → complete).
  InventoryCheckLine    per-BOM-item result (required/on_hand/shortfall/status + flags).
  InventoryReservation  a SOFT allocation — available = qty_on_hand − Σ active; never mutates stock.
  MaterialAlias         curated BOM-term → inventory-key synonym (seeded; promote-by-config).
  UomConversion         stock-UOM → BOM-UOM factor (seeded; identity rows included).
================================================================================
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.models import GUID, JSON_VARIANT, TimestampMixin, UUIDMixin
from app.modules.inventory.enums import (
    InventoryCheckStatus,
    InventoryLineStatus,
    ReservationStatus,
)


class InventoryItem(Base, UUIDMixin, TimestampMixin):
    """The INVENTORY master (normalized + deduped). Live, mutable stock; the inventory
    check reads it. `normalized_key` powers BOM-item → stock matching."""
    __tablename__ = "inventory_item"
    description: Mapped[str] = mapped_column(String(400), index=True)
    normalized_key: Mapped[str | None] = mapped_column(String(400), index=True)
    uom: Mapped[str | None] = mapped_column(String(20))
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
    """Per-BOM-item SUFFICIENT / PARTIAL / OUT_OF_STOCK + exact shortfall, forwarded to
    Stage 5."""
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
    matched_method: Mapped[str | None] = mapped_column(String(20))
    flags: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    check: Mapped["InventoryCheck"] = relationship(back_populates="lines")


class InventoryReservation(Base, UUIDMixin, TimestampMixin):
    """A SOFT stock allocation (stage-4 §3). available = qty_on_hand − Σ active
    reservations; never mutates qty_on_hand, so a re-sync can snapshot-replace stock
    without destroying a commitment. Released on BOM cancel/reopen/re-run."""
    __tablename__ = "inventory_reservation"
    inventory_item_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("inventory_item.id"), index=True
    )
    bom_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("bom.id"), index=True)
    inventory_check_line_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("inventory_check_line.id"), nullable=True
    )
    qty: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    status: Mapped[str] = mapped_column(
        String(20), default=ReservationStatus.ACTIVE.value, index=True
    )
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_reason: Mapped[str | None] = mapped_column(String(120))


class MaterialAlias(Base, UUIDMixin, TimestampMixin):
    """A curated BOM-term → inventory-key synonym (stage-4 §5.2). Seeded from
    config/material_aliases.yaml. Confirming a fuzzy suggestion writes one of these."""
    __tablename__ = "material_alias"
    __table_args__ = (
        UniqueConstraint("bom_term", name="uq_material_alias_term"),
    )
    bom_term: Mapped[str] = mapped_column(String(200), index=True)
    inventory_key: Mapped[str] = mapped_column(String(200), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UomConversion(Base, UUIDMixin, TimestampMixin):
    """Unit reconciliation (stage-4 §6.2). Converts a matched stock UOM into the BOM
    line's UOM. Identity rows seeded; an unconvertible mismatch is a flagged conservative
    out_of_stock. Seeded from config/uom_conversions.yaml."""
    __tablename__ = "uom_conversion"
    __table_args__ = (
        UniqueConstraint("from_uom", "to_uom", name="uq_uom_conversion_pair"),
    )
    from_uom: Mapped[str] = mapped_column(String(20), index=True)
    to_uom: Mapped[str] = mapped_column(String(20), index=True)
    factor: Mapped[Decimal] = mapped_column(Numeric(16, 6), default=1)
