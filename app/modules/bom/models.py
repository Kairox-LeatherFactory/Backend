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
  
  
Two new staging tables that capture the RAW extraction output before promotion
into the operational tables (spec_sheet / pom_measurement / bom). Same column
conventions as the rest of the module — UUIDMixin + TimestampMixin, GUID PKs,
JSON_VARIANT for free-shape payloads, VARCHAR for enum values.
Paste this block AFTER the PatternReference class. No other changes to the file.
The matching Alembic migration is in the next file.
  
  THREE NEW TABLES, all bom-owned (LAYERING: bom never writes another module's tables):

  PatternExtraction   one parsed DXF per (style, file). Header + the derived area /
                      fabric matrices + the resolved fabric->role map, stored as JSON
                      (same JSON_VARIANT posture as SpecSheet.attributes). This is what
                      the DCM resolver reads — it does NOT re-parse the DXF at generate
                      time. Keyed cross-order-stable on style_signature (like the DCM
                      memory) so any order of the style finds its pattern.
  PatternPiece        one row per cut piece (75 for this file = 15 pieces x 5 sizes).
                      Full fidelity for audit / re-derivation; cascade-deleted with the
                      parent. The resolver doesn't need these — the matrices on the
                      parent are the hot path — but "store the data, persistently" means
                      the raw pattern is kept, not just the rollup.
  DxfYieldObservation the learning loop. Each CONFIRMED order writes one row per leather
                      material: net_qty_sf (from the pattern) + confirmed_dcm_sf (cutting
                      manager) -> implied_yield. The resolver averages these per species
                      to derive the per-factory DXF yield empirically, instead of trusting
                      the seeds.yaml bootstrap forever.

UNITS: square feet (sf) everywhere, consistent with the rest of the DCM subsystem.
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
    Boolean,
    Float,
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
    # Nullable: a submission-anchored spec sheet is parsed BEFORE a client is resolved
    # (the client comes off the order sheet at MD approval). Mirrors Bom.client_id.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
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
        UniqueConstraint("submission_id", name="uq_bom_submission"),
    )
    # A Stage-2 BOM is now born from the order + spec sheets ALONE, anchored on the
    # submission. The Client→Order→Style→SKU breakdown is created (and back-linked into
    # client_order_id/style_id) only after the MD approves — so both stay NULL until then.
    # uq_bom_order_style still guarantees one BOM per (order, style) once populated;
    # NULL/NULL rows are distinct on both Postgres and SQLite, so unmaterialised BOMs
    # never collide. submission_id is the opaque link back to procurement (FK by table
    # name only — no Python import, no layering break).
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("submission.id"), nullable=True, index=True
    )
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
    client_order_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client_order.id"), nullable=True, index=True
    )
    style_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style.id"), nullable=True, index=True
    )
    # The parsed order-sheet snapshot (order_number, refs, per-size qty, colour lines,
    # season, price). What _resolve_identity + breakdown materialisation read while
    # style_id is NULL — there are no SKUs yet to read the quantities off.
    order_identity: Mapped[dict | None] = mapped_column(JSON_VARIANT)
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
    # Nullable: the DCM memory can be back-filled from a still-client-less BOM (the
    # client is resolved at approval). Mirrors Bom.client_id / SpecSheet.client_id.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
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
    # Nullable: a pattern reference can be recorded on a still-client-less BOM (the
    # client is resolved at approval). Mirrors Bom.client_id / SpecSheet.client_id.
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
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
    
# (imports at the top of the file already cover everything we need:
#  UUIDMixin, TimestampMixin, GUID, JSON_VARIANT, Numeric, String, Integer,
#  DateTime, ForeignKey, Mapped, mapped_column)


class SpecExtraction(Base, UUIDMixin, TimestampMixin):
    """The RAW spec-extraction output, before promotion into spec_sheet +
    pom_measurement. One row per extraction attempt (including failed/manual
    ones). The full Pydantic dump lives in raw_payload; the hot columns are
    denormalized copies so dashboards can group/filter without JSON parsing.

    Lifecycle: created at the start of generate_bom; promoted_at + promoted_to_*
    are stamped once the operational tables are written. A row with promoted_at
    NULL is an extraction the service hasn't (yet) committed to the operational
    schema — either still in flight, failed mid-promotion, or a future async/
    preview flow that extracts without promoting.

    The retention story is up to ops: orphan rows (promoted_at NULL, older than
    N days) can be GC'd; promoted rows are kept for audit per BOM retention policy."""
    __tablename__ = "spec_extraction"

    # The source upload this came from. Nullable for the rare flow where the
    # bytes were passed in without a corresponding Document row (tests).
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True, index=True,
    )
    # Engine that produced raw_payload. Matches ExtractedSpec.extracted_by
    # ('gemini' | 'groq' | 'manual'). Kept as String(20) for back-compat with old
    # rows that may carry 'heuristic' or 'deterministic'.
    extracted_by: Mapped[str] = mapped_column(String(20), index=True)
    confidence_overall: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))

    # The FULL Pydantic dump — the audit truth. Includes warnings, accessories,
    # technical_details, etc. — anything model_dump(mode='json') produced.
    raw_payload: Mapped[dict] = mapped_column(JSON_VARIANT)

    # Denormalized hot columns for dashboards / analytics. Nullable because a
    # failed extraction has none of these. NEVER read these for business logic —
    # always read from the operational tables (spec_sheet etc.) for that. These
    # are query-fast copies only.
    style_no: Mapped[str | None] = mapped_column(String(120), index=True)
    client_name: Mapped[str | None] = mapped_column(String(120))
    garment_type_guess: Mapped[str | None] = mapped_column(String(60))
    num_measurements: Mapped[int] = mapped_column(Integer, default=0)
    num_accessories: Mapped[int] = mapped_column(Integer, default=0)
    num_warnings: Mapped[int] = mapped_column(Integer, default=0)
    manual_entry_required: Mapped[bool] = mapped_column(default=False, index=True)

    # Promotion stamps. NULL until the service has written through to spec_sheet
    # (and pom_measurement). Two columns so we can audit "extracted but not
    # promoted" (orphan) and "extracted AND promoted to X" cases.
    promoted_to_spec_sheet_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("spec_sheet.id"), nullable=True, index=True,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OrderExtraction(Base, UUIDMixin, TimestampMixin):
    """The RAW order-extraction output, before promotion into bom (and, at
    approval, the Client→Order→Style→SKU tree via clients.service). Same
    lifecycle pattern as SpecExtraction — raw_payload is the audit truth, hot
    columns are denormalized for query speed."""
    __tablename__ = "order_extraction"

    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True, index=True,
    )
    extracted_by: Mapped[str] = mapped_column(String(20), index=True)
    confidence_overall: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))

    raw_payload: Mapped[dict] = mapped_column(JSON_VARIANT)

    # Denormalized hot columns
    order_number: Mapped[str | None] = mapped_column(String(120), index=True)
    style_no: Mapped[str | None] = mapped_column(String(120), index=True)
    client_name: Mapped[str | None] = mapped_column(String(120))
    season: Mapped[str | None] = mapped_column(String(20))
    currency: Mapped[str | None] = mapped_column(String(3))
    order_qty: Mapped[int] = mapped_column(Integer, default=0)
    num_lines: Mapped[int] = mapped_column(Integer, default=0)
    num_warnings: Mapped[int] = mapped_column(Integer, default=0)
    manual_entry_required: Mapped[bool] = mapped_column(default=False, index=True)

    # Promoted into a Bom (and eventually a client_order at MD approval — but
    # bom.id is the immediate target). NULL until the service writes the Bom row.
    promoted_to_bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom.id"), nullable=True, index=True,
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    
    
class PatternExtraction(Base, UUIDMixin, TimestampMixin):
    """A parsed pattern DXF for a style. The cut-area data the DCM resolver consumes.

    Keyed on the SAME cross-order-stable style_signature the DCM memory uses (NOT
    style_id, which is order-scoped), so the pattern outlives any single order and is
    found by every future order of the same physical style. `sha256` makes a re-upload
    of the identical file idempotent; a genuinely new export supersedes via is_current.
    """
    __tablename__ = "pattern_extraction"
    __table_args__ = (
        UniqueConstraint("style_signature", "sha256", name="uq_pattern_extraction_file"),
    )
    # Nullable client_id mirrors Bom/SpecSheet — a pattern can land before the client is
    # resolved (client comes off the order sheet at approval).
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
    )
    style_signature: Mapped[str] = mapped_column(String(120), index=True)
    garment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("garment_type.id"), nullable=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True
    )
    # parser provenance / detected facts
    source_system: Mapped[str | None] = mapped_column(String(20))     # creacompo|lectra|gerber|unknown
    parser_version: Mapped[str | None] = mapped_column(String(40))
    unit: Mapped[str | None] = mapped_column(String(4))               # mm|cm|in|m (detected)
    master_size: Mapped[str | None] = mapped_column(String(10))
    n_pieces: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    storage_key: Mapped[str | None] = mapped_column(String(300))      # where the raw .dxf lives
    is_current: Mapped[bool] = mapped_column(default=True, index=True)
    # derived data the resolver reads (sf). Stored, not recomputed.
    area_matrix: Mapped[dict | None] = mapped_column(JSON_VARIANT)     # {size:{n_pieces,net_sf,net_qty_sf}}
    fabric_matrix: Mapped[dict | None] = mapped_column(JSON_VARIANT)   # {size:{fabric: net_qty_sf}}
    fabric_roles: Mapped[dict | None] = mapped_column(JSON_VARIANT)    # {fabric:{role,category,is_leather}}
    warnings: Mapped[list | None] = mapped_column(JSON_VARIANT)        # parser warnings + unknown fabrics

    pieces: Mapped[list["PatternPiece"]] = relationship(
        back_populates="pattern", cascade="all, delete-orphan"
    )


class PatternPiece(Base, UUIDMixin, TimestampMixin):
    """One cut piece instance from the DXF (full fidelity for audit / re-derivation)."""
    __tablename__ = "pattern_piece"
    pattern_extraction_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("pattern_extraction.id"), index=True
    )
    block: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str | None] = mapped_column(String(200))     # cp932-recovered PIECE name
    fabric: Mapped[str | None] = mapped_column(String(120))   # raw FABRIC label
    size: Mapped[str | None] = mapped_column(String(10))
    qty: Mapped[int] = mapped_column(Integer, default=1)
    net_area_sf: Mapped[Decimal | None] = mapped_column(Numeric(10, 3))
    longest_cm: Mapped[Decimal | None] = mapped_column(Numeric(8, 1))
    pattern: Mapped["PatternExtraction"] = relationship(back_populates="pieces")


class DxfYieldObservation(Base, UUIDMixin, TimestampMixin):
    """A confirmed (net pattern area -> real DCM) pair: the per-factory cutting/hide-waste
    yield, MEASURED. Written by the cutting gate for each confirmed leather line; the
    resolver averages by species to learn the DXF yield empirically (replacing the
    seeds.yaml bootstrap as data accrues)."""
    __tablename__ = "dxf_yield_observation"
    style_signature: Mapped[str] = mapped_column(String(120), index=True)
    species: Mapped[str] = mapped_column(String(20), index=True)   # sheep|goat|_default
    size: Mapped[str | None] = mapped_column(String(10))
    net_qty_sf: Mapped[Decimal] = mapped_column(Numeric(10, 3))
    confirmed_dcm_sf: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    implied_yield: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    source_bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom.id"), nullable=True
    )
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DxfYield(Base, UUIDMixin, TimestampMixin):
    """Per-species cutting-yield multiplier (net pattern sf → leather DCM).
    Overrides the built-in default; `_default` is a valid species key."""
    __tablename__ = "dxf_yield"
    species: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    factor: Mapped[Decimal] = mapped_column(Numeric(6, 3))
    note: Mapped[str | None] = mapped_column(Text)


class FabricRoleRow(Base, UUIDMixin, TimestampMixin):
    """CAD fabric label → BOM role/category/leather-flag. Language-agnostic:
    a Japanese, Italian, or English label is just a row."""
    __tablename__ = "fabric_role"
    label: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    role: Mapped[str] = mapped_column(String(30))
    category: Mapped[str] = mapped_column(String(30))
    is_leather: Mapped[bool] = mapped_column(Boolean, default=False)
    
class CostCatalogLine(Base, UUIDMixin, TimestampMixin):
    """A default non-material BOM cost line (§5b). Keyed by garment_type.code (UPPER)
    or the literal '_default'. Seeded from cost_catalog.yaml; editable at runtime."""
    __tablename__ = "cost_catalog_line"
    __table_args__ = (
        UniqueConstraint("garment_code", "category", "name", name="uq_cost_line"),
    )
    garment_code: Mapped[str] = mapped_column(String(40), index=True)
    category: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    uom: Mapped[str | None] = mapped_column(String(20))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    qty_per_garment: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), default=1)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    
class ClientCheckRule(Base, UUIDMixin, TimestampMixin):
    """One per-client BOM cross-check (§8). `kind` dispatches a generic engine
    primitive; the rest is that rule's config. Replaces the last hardcoded clients."""
    __tablename__ = "client_check_rule"
    __table_args__ = (
        UniqueConstraint("client_code", "rule_id", name="uq_check_rule"),
    )
    client_code: Mapped[str] = mapped_column(String(60), index=True)
    rule_id: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(40))
    severity: Mapped[str] = mapped_column(String(10), default="warn")
    field: Mapped[str | None] = mapped_column(String(60))
    range_lo: Mapped[float | None] = mapped_column(Float)
    range_hi: Mapped[float | None] = mapped_column(Float)
    params: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)