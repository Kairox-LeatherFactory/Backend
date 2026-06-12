"""
modules/bom/enums.py — Stage-2/3 BOM value-sets (str-Enum + VARCHAR columns).

Domain-specific to BOM generation + approval. Cross-cutting vocab (DocumentKind,
SpecType, Notification*) lives in app/core/enums.py.
"""
import enum


class BomStatus(str, enum.Enum):
    """Stage-3 approval state machine: draft → ready_for_review → approved | rejected
    → exported. `approved`/`locked` are immutable; `rejected` is terminal until reopen."""
    DRAFT = "draft"
    READY_FOR_REVIEW = "ready_for_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    LOCKED = "locked"
    EXPORTED = "exported"


class BomItemCategory(str, enum.Enum):
    MAIN_MATERIAL = "main_material"
    SUB_MATERIAL = "sub_material"
    LINING = "lining"
    INTERLINING = "interlining"
    THREAD = "thread"
    ACCESSORY = "accessory"
    MANUFACTURING = "manufacturing"
    PACKAGING = "packaging"
    FOB_CHARGE = "fob_charge"


class DcmSource(str, enum.Enum):
    """Where a material line's DCM (dm² per garment) came from — the spine of Stage 2.
    Ordered fallback; a lower-numbered source wins; every material line is STAMPED."""
    TEMPLATE = "template"
    SIMILAR_STYLE = "similar_style"
    AI_ESTIMATE = "ai_estimate"
    MANUAL = "manual"


class ExtractionSource(str, enum.Enum):
    """Who produced an extracted value — the audit trail for the LLM policy."""
    DETERMINISTIC = "deterministic"
    GEMINI = "gemini"
    GROQ = "groq"
    MANUAL = "manual"
