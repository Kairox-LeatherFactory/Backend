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
    ProductionTrackingStatus,
    ReservationStatus,
    ScanStatus,
    SubmissionStatus,
    SupplierEmailStatus,
    ValidationStatus,
)

# JSONB on Postgres, plain JSON on SQLite (tests). Lets spec sheets store wildly
# different shapes without 200 sparse typed columns.
JSON_VARIANT = JSON().with_variant(JSONB, "postgresql")


# ══════════════════════════════════════════════════════════════════════════
# Stage 1 — upload batch (the pairing key) + per-client validation registry
# ══════════════════════════════════════════════════════════════════════════
class Submission(Base, UUIDMixin, TimestampMixin):
    """The upload-batch that PAIRS an order sheet and a spec sheet (Stage-1 §2).

    The two documents arrive as separate uploads but gate Stage 2 together. The
    first upload mints a `submission_id`; the second references it. We pair on the
    submission, NOT on an order id, because the `client_order` does not exist yet
    at upload time — it is derived from the order sheet in Stage 2. That order link
    is resolved later, once, via `client_order_id`. This is the clean home for the
    Stage-0 note that `Document` deliberately carries no `client_order_id`.

    The two `*_document_id` columns are the CURRENT slot pointers (a corrective
    re-upload supersedes the old doc and repoints the slot); `Document.submission_id`
    is the membership FK that keeps every version — including superseded ones — for
    audit. `use_alter=True` on the slot FKs breaks the circular dependency with
    `document` so Alembic/create_all can order the CREATE TABLEs."""
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
    # Every document ever uploaded into this submission (incl. superseded), via
    # Document.submission_id. foreign_keys is pinned so SQLAlchemy doesn't confuse
    # this back-ref with the two slot-pointer FKs above.
    documents: Mapped[list["Document"]] = relationship(
        back_populates="submission",
        foreign_keys="Document.submission_id",
    )


class ClientTemplate(Base, UUIDMixin, TimestampMixin):
    """A per-client × doc-kind validation profile (Stage-1 §4). Seeded idempotently
    from `config/client_templates.yaml`; the validator reads these rows at request
    time so onboarding a new client is one registry entry, no code change. The
    weighted `anchors`/`fingerprints`/`thresholds` live in JSONB because their shape
    varies per client and language."""
    __tablename__ = "client_template"
    __table_args__ = (
        UniqueConstraint("client_code", "doc_kind", name="uq_client_template_code_kind"),
    )
    client_code: Mapped[str] = mapped_column(String(60), index=True)   # beau_geste, _generic
    display_name: Mapped[str | None] = mapped_column(String(160))
    doc_kind: Mapped[str] = mapped_column(String(30), index=True)      # DocumentKind value
    language: Mapped[str | None] = mapped_column(String(10))
    size_system: Mapped[str | None] = mapped_column(String(10))        # SizeSystem value
    currency: Mapped[str | None] = mapped_column(String(3))
    expected_layout: Mapped[str | None] = mapped_column(String(20))    # ExpectedLayout value
    spec_type_hint: Mapped[str | None] = mapped_column(String(30))     # SpecType value
    accepted_mime: Mapped[list | None] = mapped_column(JSON_VARIANT)
    anchors: Mapped[list | None] = mapped_column(JSON_VARIANT)
    fingerprints: Mapped[list | None] = mapped_column(JSON_VARIANT)
    grid_signals: Mapped[dict | None] = mapped_column(JSON_VARIANT)    # min_cols, has_tolerance_column, ...
    thresholds: Mapped[dict | None] = mapped_column(JSON_VARIANT)      # {accept_high, reject_low}
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)


# ══════════════════════════════════════════════════════════════════════════
# Stage 1 — inputs (uploaded artifacts + extracted spec sheets)
# ══════════════════════════════════════════════════════════════════════════
class Document(Base, UUIDMixin, TimestampMixin):
    """Every uploaded artifact (order sheet, spec sheet, BOM quote, supplier-PO
    scan). `sha256` is unique → dedupe + the cache key for LLM extraction (don't
    re-bill identical uploads).

    OWNERSHIP DIRECTION (per the workflow doc): the client emails the order +
    spec sheets, the MD/DM uploads them (`uploaded_by` = MD/DM). A document is
    *about* a `client` (nullable — tagged on upload or filled by Stage-2
    extraction). It deliberately has NO `client_order_id`: the order doesn't
    exist yet at upload time — it is *derived from* the order sheet in Stage 2 —
    so the link runs the other way, `client_order.source_document_id` → document.
    That also avoids a circular FK between `document` and `client_order`."""
    __tablename__ = "document"
    client_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("client.id"), nullable=True, index=True
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

    # ── Stage 1 additions (upload & validation) ──────────────────────────────
    # The upload-batch this document belongs to (membership; the pairing lives on
    # `submission`). foreign_keys is pinned on the back-ref to disambiguate from
    # submission's two slot-pointer FKs.
    submission_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("submission.id"), nullable=True, index=True
    )
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    validation_status: Mapped[str] = mapped_column(
        String(25), default=ValidationStatus.PENDING.value, index=True
    )
    classified_kind: Mapped[str | None] = mapped_column(String(30))        # DocumentKind value
    classified_spec_type: Mapped[str | None] = mapped_column(String(30))   # SpecType value
    classification_method: Mapped[str | None] = mapped_column(String(20))  # ClassificationMethod value
    classification_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    client_match_code: Mapped[str | None] = mapped_column(String(60))      # client_template.client_code
    scan_status: Mapped[str] = mapped_column(
        String(20), default=ScanStatus.SKIPPED.value
    )
    # The matched / missing / expected signals returned in the API envelope, plus
    # the cached LLM classification (keyed by this row's sha256 per stage-0 §4 —
    # a byte-identical re-upload is never re-classified or re-billed).
    validation_signals: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    submission: Mapped["Submission | None"] = relationship(
        back_populates="documents", foreign_keys="Document.submission_id",
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
    # ── Stage 3: rejection + PDF export (stage-3 spec §1c) ────────────────────
    # rejection is MD-only, with a mandatory reason (also copied into the BOM_REJECT
    # audit `after`). The export is the rendered BMO-1 quotation, stored as a
    # Document(kind=bom_quote) and linked here — DISTINCT from source_document_id
    # (the INPUT quote). exported_at stamps the current export.
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    export_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True
    )
    exported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ── Stage 2: the cutting-manager confirmation gate (§10) ──────────────────
    # A mandatory gate between Stage-2 generation and Stage-3 MD approval, set
    # regardless of dcm_source. The Stage-3 approve endpoint REFUSES a BOM whose
    # cutting_confirmed_at is null; a post-confirmation DCM edit clears these (§9).
    cutting_confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    cutting_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ── DCM-memory provenance: the garment type + representative size the DCM was
    # resolved at (§2). Persisted so the §10 confirm back-fills the
    # style_consumption_template keyed IDENTICALLY to how generation looked it up —
    # otherwise the second order of a style would miss its own confirmed value (§11.5).
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
    # ── Stage 2: DCM provenance (§2, §3f) — null on non-material lines ─────────
    # Every MATERIAL line stamps where its qty_per_garment (DCM) came from and how
    # confident we are. ai_estimate lines are FLAGGED and block finalization until
    # the cutting manager confirms (§10). Manufacturing/packaging/FOB leave null.
    dcm_source: Mapped[str | None] = mapped_column(String(20))   # DcmSource value
    dcm_confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    bom: Mapped["Bom"] = relationship(back_populates="items")


# ══════════════════════════════════════════════════════════════════════════
# Stage 2 — POM extraction, the DCM memory, garment types, pattern refs (§3)
# ══════════════════════════════════════════════════════════════════════════
class GarmentType(Base, UUIDMixin, TimestampMixin):
    """Per-type config (§3e): which POMs to expect (a completeness check) and the
    Source-3 area heuristic + wastage used ONLY as the last-resort DCM estimate.
    Seeded idempotently from config/garment_types.yaml."""
    __tablename__ = "garment_type"
    code: Mapped[str] = mapped_column(String(60), unique=True, index=True)  # TRACK_PANT
    label: Mapped[str | None] = mapped_column(String(120))
    required_poms: Mapped[list | None] = mapped_column(JSON_VARIANT)   # [pom_code, ...]
    area_formula: Mapped[dict | None] = mapped_column(JSON_VARIANT)    # Source-3 expression
    default_wastage_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))


class PomDictionary(Base, UUIDMixin, TimestampMixin):
    """Maps every client/native term (渡り幅, inseam, girovita) to a STANDARD
    pom_code (§3a). Adding a new client term is a row, not a code change. Seeded
    idempotently from config/pom_dictionary.yaml."""
    __tablename__ = "pom_dictionary"
    __table_args__ = (
        UniqueConstraint("language", "source_term", "garment_type_id",
                         name="uq_pom_dictionary_term"),
    )
    language: Mapped[str] = mapped_column(String(10), index=True)   # ja / it / en
    source_term: Mapped[str] = mapped_column(String(160), index=True)  # 渡り幅, inseam
    pom_code: Mapped[str] = mapped_column(String(40), index=True)   # THIGH_WIDTH
    garment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("garment_type.id"), nullable=True
    )
    weight: Mapped[int] = mapped_column(Integer, default=1)         # tie-break on collide


class PomMeasurement(Base, UUIDMixin, TimestampMixin):
    """One standardized POM, per spec sheet, per size (§3b). The Beau Geste grid
    yields 14 POMs × 5 sizes = 70 rows; Jackiee (no grid) yields none. `source_term`
    preserves the ORIGINAL label so a reviewer can verify the mapping."""
    __tablename__ = "pom_measurement"
    __table_args__ = (
        # queryable per (sheet, size, pom) — the §2 Source-3 area read joins here.
        UniqueConstraint("spec_sheet_id", "size", "pom_code",
                         name="uq_pom_measurement_identity"),
    )
    spec_sheet_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("spec_sheet.id"), index=True
    )
    size: Mapped[str] = mapped_column(String(10))           # "M", "42" — string by design
    pom_code: Mapped[str] = mapped_column(String(40))       # standardized
    value: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    pitch: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))   # grading step (ピッチ)
    tolerance: Mapped[dict | None] = mapped_column(JSON_VARIANT)   # ± band when present
    source_term: Mapped[str | None] = mapped_column(String(160))  # audit — original label
    extracted_by: Mapped[str | None] = mapped_column(String(20))  # ExtractionSource value
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))


class StyleConsumptionTemplate(Base, UUIDMixin, TimestampMixin):
    """The DCM memory (§3c) — a learning store of CONFIRMED consumption.

    ⚠ CROSS-ORDER KEYING. `Style` rows are order-scoped (style.client_order_id), so
    the same physical style in two orders is two Style rows with two ids. This memory
    MUST NOT key on style_id — it keys on a cross-order-stable `style_signature`
    (prefer style.customer_ref → internal_ref → slug(name)) so the SECOND order of a
    style is a Source-1 template hit, not a re-estimate. Back-filled by the §10 gate."""
    __tablename__ = "style_consumption_template"
    __table_args__ = (
        UniqueConstraint("client_id", "style_signature", "garment_type_id",
                         "material_category", "size",
                         name="uq_style_consumption_identity"),
    )
    client_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("client.id"), index=True)
    style_signature: Mapped[str] = mapped_column(String(120), index=True)  # CR1-02F5-PL02
    garment_type_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("garment_type.id"), nullable=True
    )
    material_category: Mapped[str] = mapped_column(String(20))   # BomItemCategory value
    size: Mapped[str] = mapped_column(String(10))
    dcm_value: Mapped[Decimal] = mapped_column(Numeric(12, 3))   # the confirmed consumption
    uom: Mapped[str | None] = mapped_column(String(20))          # dm² / pc
    confirmed_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_bom_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("bom.id"), nullable=True
    )


class PatternReference(Base, UUIDMixin, TimestampMixin):
    """"Follow existing pattern X in size Y" (§3d). Both real clients carry one —
    Jackiee explicitly (MIMI-28-09-22-ZZ, size 42), Beau Geste embedded in 特記事項
    (CR1-O2B5-PL02). Resolved to a base style / DCM template, NOT parsed as new POMs."""
    __tablename__ = "pattern_reference"
    client_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("client.id"), index=True)
    spec_sheet_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("spec_sheet.id"), nullable=True, index=True
    )
    pattern_code: Mapped[str] = mapped_column(String(120), index=True)  # MIMI-28-09-22-ZZ
    base_size: Mapped[str | None] = mapped_column(String(10))           # "42"
    resolved_style_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style.id"), nullable=True
    )
    resolved_template_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("style_consumption_template.id"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text)   # the source phrase, for audit


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
    # ── Stage 4 (§5/§10): how the line matched + non-blocking flags ───────────
    # matched_method ∈ MatchMethod (key/alias/manual; null when unmatched).
    # flags carries the non-blocking diagnostics the report surfaces:
    # `unmatched` (no stock row), `uom_mismatch` (units unconvertible → conservative
    # out_of_stock), `suggestion` (an advisory fuzzy candidate, never auto-applied).
    matched_method: Mapped[str | None] = mapped_column(String(20))
    flags: Mapped[dict | None] = mapped_column(JSON_VARIANT)
    check: Mapped["InventoryCheck"] = relationship(back_populates="lines")


class InventoryReservation(Base, UUIDMixin, TimestampMixin):
    """A SOFT stock allocation (stage-4 spec §3). The Stage-4 check reserves
    min(required, available) of a matched article against an approved BOM. Reservations
    live HERE, never in `inventory_item.qty_on_hand` — so `available = qty_on_hand −
    Σ(active reservations)` and a re-sync of the master can snapshot-replace qty_on_hand
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
    """A curated BOM-term → inventory-key synonym (stage-4 spec §5.2). The BOM says
    `SHEEP GLASS`; the master says `SHEEP NAPPA …`. Adding a synonym is a row (seeded
    idempotently from config/material_aliases.yaml), not a code change. Confirming a
    fuzzy suggestion writes one of these → promotes the pair to the free exact path."""
    __tablename__ = "material_alias"
    __table_args__ = (
        UniqueConstraint("bom_term", name="uq_material_alias_term"),
    )
    bom_term: Mapped[str] = mapped_column(String(200), index=True)      # normalized BOM term
    inventory_key: Mapped[str] = mapped_column(String(200), index=True)  # normalized stock token
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class UomConversion(Base, UUIDMixin, TimestampMixin):
    """Unit reconciliation (stage-4 spec §6.2). Converts a matched stock UOM into the
    BOM line's UOM (`on_hand_in_bom_uom = on_hand × factor`). Identity rows (dm²↔DCM,
    pc↔NOS) are seeded; an unconvertible mismatch is a flagged conservative out_of_stock,
    never a false 'sufficient'. Seeded from config/uom_conversions.yaml."""
    __tablename__ = "uom_conversion"
    __table_args__ = (
        UniqueConstraint("from_uom", "to_uom", name="uq_uom_conversion_pair"),
    )
    from_uom: Mapped[str] = mapped_column(String(20), index=True)   # the stock UOM
    to_uom: Mapped[str] = mapped_column(String(20), index=True)     # the BOM-line UOM
    factor: Mapped[Decimal] = mapped_column(Numeric(16, 6), default=1)


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
    # ── Stage 5 deltas (§10) ──────────────────────────────────────────────────
    # email_status set by §5b SES bounce feedback (a dead address is flagged, not
    # silently retried); supplier_type (curated leather/accessory/service, defaulting
    # from the dominant history mode) drives the §2 template + §3 approver routing;
    # state_code (the GSTIN state) drives the §2c intra/inter-GST decision; whatsapp_phone
    # for the §7 escalation rung (often == phone).
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
    """The §1d matching index — the pre-aggregated `(supplier, article)` evidence from
    the `Suppliers provision` ledger. Built by the §9 importer, refreshed on re-import.
    `normalized_description` is run through the SAME Stage-4 normalizer used on inventory
    + BOM lines, so a BOM term, an inventory row, and a historical buy all collapse to
    one comparable key — the matcher's index probe (§1b)."""
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
    mode: Mapped[str | None] = mapped_column(String(20))    # LEATHER/MATERIALS/SERVICE/JOB WORK
    uom: Mapped[str | None] = mapped_column(String(20))     # provision NOM → PO line UOM default
    txn_count: Mapped[int] = mapped_column(Integer, default=1)
    first_purchased_at: Mapped[date | None] = mapped_column(Date)
    last_purchased_at: Mapped[date | None] = mapped_column(Date)
    last_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    min_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    max_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    supplier: Mapped["Supplier"] = relationship(back_populates="supply_history")


class PurchaseOrder(Base, UUIDMixin, TimestampMixin):
    """The SUPPLIER purchase order (PAKKAR -> supplier), e.g. "PO-07(25-26)". This
    is the name freed up by renaming the buyer order to `client_order`. Mirrors the
    suppler-po-form PDFs: Bill-To supplier, CGST/SGST 2.5%, totals, 60-day terms."""
    __tablename__ = "purchase_order"
    po_number: Mapped[str | None] = mapped_column(String(40), index=True)   # "PO-07(25-26)" — allocated AT SEND (§2d)
    # Stage 5 (§1c): an UNRESOLVED shortfall is held as a draft PO with no supplier and
    # needs_supplier=True, so supplier_id is nullable (a costed order is never bound to a
    # guessed vendor). A resolved PO carries the supplier.
    supplier_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("supplier.id"), nullable=True, index=True
    )
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
    igst: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))      # §2c inter-state
    gst_mode: Mapped[str | None] = mapped_column(String(10))         # INTRA | INTER
    round_off: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    total: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20), default=POStatus.DRAFT.value, index=True)
    # ── Stage 5: matching/contactability flags (§1c) ──────────────────────────
    needs_supplier: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    no_contact_channel: Mapped[bool] = mapped_column(Boolean, default=False)
    match_method: Mapped[str | None] = mapped_column(String(20))   # ledger/alias/category/manual
    candidates: Mapped[dict | None] = mapped_column(JSON_VARIANT)   # ranked shortlist (§1c)
    # ── Stage 5: editable contract + cross-check approval (§3/§4) ─────────────
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rejection_reason: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pdf_document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("document.id"), nullable=True
    )
    # ── Stage 5: open tracking (§6c) ──────────────────────────────────────────
    tracking_token: Mapped[str | None] = mapped_column(String(64), index=True)
    first_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_clicked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # ── Stage 5: escalation state machine (§7c) ───────────────────────────────
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
    # ── Stage 5 (§5c/§6): correlate inbound SNS/pixel events back to THIS send ──
    tracking_token: Mapped[str | None] = mapped_column(String(64), index=True)
    message_id: Mapped[str | None] = mapped_column(String(200))   # SES/SMTP Message-ID
    purchase_order: Mapped["PurchaseOrder"] = relationship(back_populates="responses")


class PoTrackingEvent(Base, UUIDMixin, TimestampMixin):
    """The unified engagement log (§6c): every open/click/delivery/bounce/complaint +
    each WhatsApp/call escalation event for a PO. Self-hosted read-state (no third-party
    SaaS, §6a) — the escalation sweeper (§7) and the dashboard read straight from here."""
    __tablename__ = "po_tracking_event"
    purchase_order_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("purchase_order.id"), index=True
    )
    po_response_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("po_response.id"), nullable=True
    )
    tracking_token: Mapped[str | None] = mapped_column(String(64), index=True)
    event_type: Mapped[str] = mapped_column(String(20))         # POTrackingEventType value
    channel: Mapped[str | None] = mapped_column(String(20))     # email/whatsapp/call
    ip: Mapped[str | None] = mapped_column(String(60))
    user_agent: Mapped[str | None] = mapped_column(String(400))
    meta: Mapped[dict | None] = mapped_column(JSON_VARIANT)     # SNS/Twilio payload refs
    at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProductionTracking(Base, UUIDMixin, TimestampMixin):
    """One row per style/order (§8b) advancing through the §8c status ladder. System-
    driven on each procurement stage transition + manually nudgeable by DM/Cutting/MD.
    Cross-module style/order identity is resolved via clients.service (CLAUDE.md §3.2)."""
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
        GUID(), ForeignKey("bom.id"), nullable=True
    )
    status: Mapped[str] = mapped_column(
        String(30), default=ProductionTrackingStatus.AWAITING_BOM.value, index=True
    )
    po_count: Mapped[int] = mapped_column(Integer, default=0)
    po_confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    material_ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_by: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id"), nullable=True
    )


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
