"""
================================================================================
modules/procurement/enums.py — Value-sets for the BOM Procurement Workflow
================================================================================

These live in the procurement module (not core/enums.py) on purpose: they are
domain-specific to the supplier/BOM/inventory flow, and keeping them here lets
the module be lifted into its own service later without dragging procurement
vocabulary into the shared core.

WHY str-Enums + STRING columns
    Each enum subclasses `str` so `.value` serialises to JSON and stores cleanly.
    The DB columns that hold these are plain VARCHAR storing the enum *value*
    (mirroring the existing `client_order.ship_mode` pattern), NOT native Postgres
    ENUM types. Native enums mean a new PG type per status + an irreversible
    `ALTER TYPE ... ADD VALUE` every time a state is added; plain strings stay
    portable to the SQLite test DB and keep migrations reversible. Validation
    happens at the Pydantic/service layer.
================================================================================
"""
import enum


class DocumentKind(str, enum.Enum):
    ORDER_SHEET = "order_sheet"
    SPEC_SHEET = "spec_sheet"
    BOM_QUOTE = "bom_quote"
    SUPPLIER_PO_PDF = "supplier_po_pdf"


class SpecType(str, enum.Enum):
    """The two real spec-sheet shapes share almost no columns."""
    MEASUREMENT_GRID = "measurement_grid"        # Japanese per-size grid + tolerances
    NARRATIVE_TECHPACK = "narrative_techpack"    # Jackie free-text key->value tech pack


class ExtractionSource(str, enum.Enum):
    """Who produced an extracted value — the audit trail for the LLM policy."""
    DETERMINISTIC = "deterministic"   # rules/regex/normalised string match, no LLM
    GEMINI = "gemini"                 # primary extraction provider
    GROQ = "groq"                     # fallback provider
    MANUAL = "manual"                 # human keyed it (extraction declined to guess)


class BomStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    LOCKED = "locked"


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


class InventoryCheckStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETE = "complete"


class InventoryLineStatus(str, enum.Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    OUT_OF_STOCK = "out_of_stock"


class POStatus(str, enum.Enum):
    """Lifecycle of a SUPPLIER purchase order (Stage 5)."""
    DRAFT = "draft"
    SENT = "sent"
    RESPONDED = "responded"
    CONFIRMED = "confirmed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class POResponseChannel(str, enum.Enum):
    EMAIL = "email"
    CALL = "call"


class POResponseStatus(str, enum.Enum):
    PENDING = "pending"
    OPENED = "opened"
    CONFIRMED = "confirmed"
    NO_RESPONSE = "no_response"


class NotificationChannel(str, enum.Enum):
    IN_APP = "in_app"
    EMAIL = "email"
    CALL = "call"


class NotificationType(str, enum.Enum):
    BOM_AWAITING_REVIEW = "bom_awaiting_review"   # Stage 3: MD review notice
    PO_DISPATCH = "po_dispatch"                    # Stage 5: PO email to supplier
    ESCALATION_CALL = "escalation_call"            # Stage 5: 5-hour auto-call
    MATERIAL_READY = "material_ready"              # Stage 6: MD alert


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    OPENED = "opened"
    RESPONDED = "responded"
    FAILED = "failed"


class SizeSystem(str, enum.Enum):
    """Stored on `client.default_size_system`; orders mix systems per row regardless."""
    LETTER = "letter"   # S, M, L, XL, XXL (Beau Geste)
    EU = "eu"           # 38-62 numeric (Jackie)
    MIXED = "mixed"     # KJ: 25-31 and 46-62 in one order
