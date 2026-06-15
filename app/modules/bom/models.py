"""
================================================================================
modules/bom/models.py — Stage-2/3 BOM schema
================================================================================

The BMO-1 BOM + its line items, plus the Stage-2 extraction support tables (POM
dictionary/measurements, the DCM consumption memory, garment types, pattern refs)
and the parsed spec sheet. Split out of the former procurement monolith.

CONVENTIONS (unchanged): UUIDMixin + TimestampMixin + portable GUID; money =
Numeric(12,2); fractional qty = Numeric(12,3); status/kind = VARCHAR storing an
enum `.value`; semi-structured data = JSON_VARIANT. Cross-module FKs (client,
client_order, style, app_user, document) are TABLE-NAME STRINGS only — no Python
import of those modules. Relationships are defined only WITHIN this module.

TABLE GUIDE (each class is one table; see the per-class docstring for column detail)
  SpecSheet                 a parsed spec sheet (typed cols + JSON behind a spec_type discriminator).
  Bom                       the BMO-1 header + the Stage-3 state machine. UNIQUE (client_order_id, style_id).
  BomItem                   one BOM line; qty_per_garment IS the DCM for material lines.
  GarmentType               per-type config (required POMs + Source-3 area formula + wastage).
  PomDictionary             native term → standard pom_code map (seeded).
  PomMeasurement            one standardized POM per spec sheet per size.
  StyleConsumptionTemplate  the DCM memory — cross-order-stable key; back-filled by the cutting gate.
  PatternReference          "follow pattern X in size Y" → resolved base style / DCM template.
================================================================================
"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
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
from app.modules.bom.enums import BomItemCategory, BomStatus


class SpecSheet(Base, UUIDMixin, TimestampMixin):
    """A parsed spec sheet. The two real shapes (Japanese measurement grid vs Jackie
    narrative tech pack) share almost no columns, so the data lives in JSON with a
    `spec_type` discriminator; only universal fields are typed."""
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
    measurements: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    attributes: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    instructions: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    extracted_by: Mapped[str | None] = mapped_column(String(20))     # ExtractionSource value
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


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
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    export_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True
    )
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cutting_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    cutting_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    garment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("garment_type.id"), nullable=True
    )
    dcm_base_size: Mapped[str | None] = mapped_column(String(10))
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True
    )
    items: Mapped[list["BomItem"]] = relationship(
        back_populates="bom", cascade="all, delete-orphan"
    )


class BomItem(Base, UUIDMixin, TimestampMixin):
    """One BMO-1 line: Sheep Glass 34.5 dm² @1.80, Goat Suede, lining, thread, buttons
    (TBC), cutting/stitching, packaging, FOB charge. `material_color` is where the
    multi-colour dimension lands."""
    __tablename__ = "bom_item"
    bom_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("bom.id"), index=True)
    category: Mapped[str] = mapped_column(String(20), default=BomItemCategory.MAIN_MATERIAL.value)
    name: Mapped[str] = mapped_column(String(200))
    material_color: Mapped[str | None] = mapped_column(String(80))
    qty_per_garment: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    uom: Mapped[str | None] = mapped_column(String(20))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    bulk_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    total_cost: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    annotation: Mapped[str | None] = mapped_column(Text)
    source_ref: Mapped[str | None] = mapped_column(String(120))
    dcm_source: Mapped[str | None] = mapped_column(String(20))   # DcmSource value
    dcm_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    bom: Mapped["Bom"] = relationship(back_populates="items")


class GarmentType(Base, UUIDMixin, TimestampMixin):
    """Per-type config (§3e): which POMs to expect + the Source-3 area heuristic +
    wastage used ONLY as the last-resort DCM estimate. Seeded from config/garment_types.yaml."""
    __tablename__ = "garment_type"
    code: Mapped[str] = mapped_column(String(60), unique=True, index=True)
    label: Mapped[str | None] = mapped_column(String(120))
    required_poms: Mapped[list | None] = mapped_column(JSON_VARIANT)
    area_formula: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    default_wastage_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class PomDictionary(Base, UUIDMixin, TimestampMixin):
    """Maps every client/native term (渡り幅, inseam, girovita) to a STANDARD pom_code
    (§3a). Adding a new client term is a row. Seeded from config/pom_dictionary.yaml."""
    __tablename__ = "pom_dictionary"
    __table_args__ = (
        UniqueConstraint("language", "source_term", "garment_type_id",
                         name="uq_pom_dictionary_term"),
    )
    language: Mapped[str] = mapped_column(String(10), index=True)
    source_term: Mapped[str] = mapped_column(String(160), index=True)
    pom_code: Mapped[str] = mapped_column(String(40), index=True)
    garment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("garment_type.id"), nullable=True
    )
    weight: Mapped[int] = mapped_column(Integer, default=1)


class PomMeasurement(Base, UUIDMixin, TimestampMixin):
    """One standardized POM, per spec sheet, per size (§3b). The Beau Geste grid yields
    14 POMs × 5 sizes = 70 rows; Jackiee (no grid) yields none."""
    __tablename__ = "pom_measurement"
    __table_args__ = (
        UniqueConstraint("spec_sheet_id", "size", "pom_code",
                         name="uq_pom_measurement_identity"),
    )
    spec_sheet_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("spec_sheet.id"), index=True
    )
    size: Mapped[str] = mapped_column(String(10))
    pom_code: Mapped[str] = mapped_column(String(40))
    value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    pitch: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    tolerance: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    source_term: Mapped[str | None] = mapped_column(String(160))
    extracted_by: Mapped[str | None] = mapped_column(String(20))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


class StyleConsumptionTemplate(Base, UUIDMixin, TimestampMixin):
    """The DCM memory (§3c) — a learning store of CONFIRMED consumption. Keys on a
    cross-order-stable `style_signature` (NOT style_id, which is order-scoped) so the
    SECOND order of a style is a Source-1 template hit. Back-filled by the §10 gate."""
    __tablename__ = "style_consumption_template"
    __table_args__ = (
        UniqueConstraint("client_id", "style_signature", "garment_type_id",
                         "material_category", "size",
                         name="uq_style_consumption_identity"),
    )
    client_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("client.id"), index=True)
    style_signature: Mapped[str] = mapped_column(String(120), index=True)
    garment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("garment_type.id"), nullable=True
    )
    material_category: Mapped[str] = mapped_column(String(20))
    size: Mapped[str] = mapped_column(String(10))
    dcm_value: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    uom: Mapped[str | None] = mapped_column(String(20))
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom.id"), nullable=True
    )


class PatternReference(Base, UUIDMixin, TimestampMixin):
    """"Follow existing pattern X in size Y" (§3d). Resolved to a base style / DCM
    template, NOT parsed as new POMs."""
    __tablename__ = "pattern_reference"
    client_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("client.id"), index=True)
    spec_sheet_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("spec_sheet.id"), nullable=True, index=True
    )
    pattern_code: Mapped[str] = mapped_column(String(120), index=True)
    base_size: Mapped[str | None] = mapped_column(String(10))
    resolved_style_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style.id"), nullable=True
    )
    resolved_template_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style_consumption_template.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
