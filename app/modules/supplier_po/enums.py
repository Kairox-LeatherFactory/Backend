"""
modules/supplier_po/enums.py — Stage-5 supplier-PO value-sets (str-Enum + VARCHAR).

Domain-specific to supplier matching, the PO cross-check/approval state machine, open
tracking, and the escalation ladder. Cross-cutting Notification* vocab lives in
app/core/enums.py.

ENUM GUIDE (each str-Enum; the model stores `.value` in a VARCHAR column)
  POStatus                 the PO cross-check + send + escalation state machine.
  POResponseChannel        email / whatsapp / call — how a contact attempt was made.
  POResponseStatus         pending/opened/confirmed/no_response/failed (a contact attempt's result).
  POTrackingEventType      open/click/delivery/bounce/complaint/whatsapp/call (the engagement log).
  SupplierEmailStatus      unknown/valid/invalid — set by bounce feedback; gates email send.
  SupplierType             leather/accessory/service — drives PO template + approver routing.
  ProductionTrackingStatus the §8c production ladder (awaiting_bom → ... → completed).
"""
import enum


class POStatus(str, enum.Enum):
    """Lifecycle of a SUPPLIER purchase order (§3b).
        draft → pending_approval → approved → sent → responded → confirmed
                       └→ rejected → draft        └→ escalated → confirmed
    `cancelled` is reachable from any pre-send state."""
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    SENT = "sent"
    RESPONDED = "responded"
    CONFIRMED = "confirmed"
    ESCALATED = "escalated"
    CANCELLED = "cancelled"


class POResponseChannel(str, enum.Enum):
    EMAIL = "email"
    WHATSAPP = "whatsapp"      # §7: the first escalation rung
    CALL = "call"


class POResponseStatus(str, enum.Enum):
    PENDING = "pending"
    OPENED = "opened"
    CONFIRMED = "confirmed"
    NO_RESPONSE = "no_response"
    FAILED = "failed"          # a hard bounce / send failure (§5b)


class POTrackingEventType(str, enum.Enum):
    """The unified engagement log on `po_tracking_event` (§6c)."""
    OPEN = "open"
    CLICK = "click"
    DELIVERY = "delivery"
    BOUNCE = "bounce"
    COMPLAINT = "complaint"
    WHATSAPP = "whatsapp"
    CALL = "call"


class SupplierEmailStatus(str, enum.Enum):
    """`supplier.email_status` — set by §5b bounce feedback."""
    UNKNOWN = "unknown"
    VALID = "valid"
    INVALID = "invalid"


class SupplierType(str, enum.Enum):
    """Curated supplier type; defaults from the dominant history mode. Drives the §2 PO
    template + the §3 cross-check approver routing."""
    LEATHER = "leather"
    ACCESSORY = "accessory"
    SERVICE = "service"


class ProductionTrackingStatus(str, enum.Enum):
    """The §8c order/style production ladder (one row per style/order)."""
    AWAITING_BOM = "awaiting_bom"
    BOM_APPROVED = "bom_approved"
    INVENTORY_CHECKED = "inventory_checked"
    PO_RAISED = "po_raised"
    PO_CONFIRMED = "po_confirmed"
    MATERIAL_READY = "material_ready"
    RELEASED_TO_PRODUCTION = "released_to_production"
    IN_PRODUCTION = "in_production"
    COMPLETED = "completed"
