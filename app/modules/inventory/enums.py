"""
modules/inventory/enums.py — Stage-4 inventory value-sets (str-Enum + VARCHAR columns).
"""
import enum


class InventoryCheckStatus(str, enum.Enum):
    RUNNING = "running"
    COMPLETE = "complete"


class InventoryLineStatus(str, enum.Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    OUT_OF_STOCK = "out_of_stock"


class ReservationStatus(str, enum.Enum):
    """Lifecycle of a soft stock allocation (stage-4 §3). `active` counts against
    `available`; `released` frees it; `consumed` = physically issued (terminal)."""
    ACTIVE = "active"
    RELEASED = "released"
    CONSUMED = "consumed"


class MatchMethod(str, enum.Enum):
    """How a BOM line matched an inventory_item (stage-4 §5). `key` = exact normalized
    key; `alias` = a curated synonym; `manual` = human-confirmed. A fuzzy candidate is
    only ever a non-applied `suggestion`, never an auto-bound method."""
    KEY = "key"
    ALIAS = "alias"
    MANUAL = "manual"
