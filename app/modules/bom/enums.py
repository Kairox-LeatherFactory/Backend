"""
modules/bom/enums.py â€” Stage-2/3 BOM value-sets (str-Enum + VARCHAR columns).

Domain-specific to BOM generation + approval. Cross-cutting vocab (DocumentKind,
SpecType, Notification*) lives in app/core/enums.py.

ENUM GUIDE (each is a str-Enum; the model stores `.value` in a VARCHAR column)
  BomStatus         the Stage-3 state machine â€” read by every edit/approve guard.
  BomItemCategory   the 9 BMO-1 line types; MAIN/SUB/LINING/INTERLINING get DCM-resolved,
                    MANUFACTURING/FOB_CHARGE are excluded from the inventory check.
  DcmSource         provenance of a material line's DCM (template/similar/ai_estimate/manual)
                    â€” keys the dcm.CONFIDENCE map and the Â§10 re-gate logic.
  ExtractionSource  who produced an extracted value (deterministic/gemini/groq/manual) â€”
                    the audit trail the LLM policy requires.
"""
import enum


class BomStatus(str, enum.Enum):
    """Stage-3 approval state machine: draft â†’ ready_for_review â†’ approved | rejected
    â†’ exported. `approved`/`locked` are immutable; `rejected` is terminal until reopen."""
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
    DXF = "dxf"
    SIMILAR_STYLE = "similar_style"
    AI_ESTIMATE = "ai_estimate"          # heuristic POM formula (rename to HEURISTIC later)
    PROVISIONAL = "provisional"          # NEW: no source resolved; qty 0, flagged, awaiting confirm
    MANUAL = "manual"
    
class ExtractionSource(str, enum.Enum):
    GEMINI = "gemini"
    GROQ = "groq"
    MANUAL = "manual"