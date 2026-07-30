"""
================================================================================
core/enums.py — Centralised enumerations shared across every module
================================================================================

PURPOSE
    A single home for every fixed value-set the system uses. Roles, wage types,
    payroll-run statuses and ship modes were previously scattered across
    security.py and individual module models. Centralising them here means:
      - one import path everywhere: `from app.core.enums import UserRole`
      - no circular imports (this file imports nothing from the app)
      - the database, the auth layer, and the API schemas all agree on the exact
        same string values.

WHY str-Enums
    Each enum subclasses `str`, so `UserRole.MANAGER == "manager"` is True and the
    value serialises straight to JSON and stores cleanly in a Postgres column.

ROLE MODEL (maps directly onto the factory's org chart)
    MANAGING_DIRECTOR  Superuser / final authority. Approves & locks BOMs (the
                       Stage-3 gate in the BOM Procurement Workflow). Outranks the
                       Direct Manager.
    DIRECT_MANAGER     Operational lead. Stage-1 uploader (order + spec), edits
                       draft BOMs, manages users/clients, runs payroll. During the
                       MD transition still bypasses role gates (see users/deps.py).
    CUTTING_MANAGER    Logs cutting-side production operations.
    STITCHING_MANAGER  Logs fusing -> lining-stitch -> final-finish operations.
    EMPLOYEE           A shop-floor worker. Can view their own work / wages.
    CLIENT             An external client logging in to track their own orders.
    HR                 Reads employees / wages / attendance + inventory checks.
    VIEWER             Read-only office staff (accountant) — no data entry.

    Adding a role = add a line here; the rest of the app picks it up.
================================================================================
"""
import enum


class UserRole(str, enum.Enum):
    MANAGING_DIRECTOR = "managing_director"   # superuser / BOM approver (outranks DM)
    DIRECT_MANAGER = "direct_manager"
    CUTTING_MANAGER = "cutting_manager"
    LINING_MANAGER = "lining_manager"
    STITCHING_MANAGER = "stitching_manager"
    EMPLOYEE = "employee"
    CLIENT = "client"
    VIEWER = "viewer"
    SUPERVISOR = "supervisor"      # may PROXY check-in daily-wage workers + add them
    HR = "hr"                      # HR / accounts: reads people, wages, attendance

    @classmethod
    def manager_roles(cls) -> set["UserRole"]:
        """Roles allowed to enter production data (besides the superuser)."""
        return {cls.CUTTING_MANAGER, cls.STITCHING_MANAGER}


class WageType(str, enum.Enum):
    """How an employee is paid. A property of the PERSON, set explicitly —
    NOT inferred from designation (TAILOR/CUTTER appear in both pay schemes)."""
    MONTHLY = "monthly"
    PIECE_RATE = "piece_rate"      # paid per piece produced; floor workers marked via attendance


class RunStatus(str, enum.Enum):
    """Lifecycle of a payroll run. A CLOSED run is a frozen snapshot."""
    OPEN = "open"
    CLOSED = "closed"


class ShipMode(str, enum.Enum):
    """How a finished purchase order leaves the factory. Sea = profitable,
    Air = margin-eroding (the central financial risk in the workflow)."""
    SEA = "sea"
    AIR = "air"


# ══════════════════════════════════════════════════════════════════════════
# Cross-cutting value-sets (the notification + document + audit tables live in
# core/models.py because bom, inventory, and supplier_po ALL write them). Kept here
# so a module never imports another module just to emit a notification / Document /
# audit row. str-Enum + VARCHAR columns (the module convention) — no native PG ENUM.
# ══════════════════════════════════════════════════════════════════════════
class DocumentKind(str, enum.Enum):
    ORDER_SHEET = "order_sheet"
    SPEC_SHEET = "spec_sheet"
    BOM_QUOTE = "bom_quote"
    SUPPLIER_PO_PDF = "supplier_po_pdf"


class SpecType(str, enum.Enum):
    """The two real spec-sheet shapes share almost no columns. Cross-stage vocab:
    Stage-1 classifies the shape, Stage-2 (bom) reads the numbers."""
    MEASUREMENT_GRID = "measurement_grid"        # Japanese per-size grid + tolerances
    NARRATIVE_TECHPACK = "narrative_techpack"    # Jackie free-text key->value tech pack


class NotificationChannel(str, enum.Enum):
    IN_APP = "in_app"
    EMAIL = "email"
    WHATSAPP = "whatsapp"      # Stage 5 §7
    CALL = "call"


class NotificationType(str, enum.Enum):
    BOM_AWAITING_REVIEW = "bom_awaiting_review"   # Stage 3: MD review notice
    PO_AWAITING_APPROVAL = "po_awaiting_approval"  # Stage 5 §3: cross-check notice
    PO_DISPATCH = "po_dispatch"                    # Stage 5: PO email to supplier
    PO_ESCALATION_WHATSAPP = "po_escalation_whatsapp"  # Stage 5 §7: WhatsApp rung
    ESCALATION_CALL = "escalation_call"            # Stage 5: auto-call rung
    PO_ESCALATION_EXHAUSTED = "po_escalation_exhausted"  # Stage 5 §7: hand to buyer
    MATERIAL_READY = "material_ready"              # Stage 6: MD alert


class NotificationStatus(str, enum.Enum):
    PENDING = "pending"
    SENT = "sent"
    OPENED = "opened"
    RESPONDED = "responded"
    FAILED = "failed"
    
class ProductionStage(str, enum.Enum):
    CUTTING          = "CUTTING"
    FUSING           = "FUSING"
    PASTING          = "PASTING"
    SHELL_STITCHING  = "SHELL_STITCHING"
    LINE_ATTACH      = "LINE_ATTACH"
    LINE_STITCHING   = "LINE_STITCHING"
    FINAL_FINISH     = "FINAL_FINISH"
    FINAL_INSPECTION = "FINAL_INSPECTION"   # NEW — after FINAL_FINISH

    @classmethod
    def ordered(cls) -> list["ProductionStage"]:
        return [
            cls.CUTTING, cls.FUSING, cls.PASTING, cls.SHELL_STITCHING,
            cls.LINE_ATTACH, cls.LINE_STITCHING, cls.FINAL_FINISH,
            cls.FINAL_INSPECTION,
        ]

    @classmethod
    def order_index(cls) -> dict[str, int]:
        return {s.value: i for i, s in enumerate(cls.ordered())}

    @property
    def position(self) -> int:
        return ProductionStage.order_index()[self.value]

    def predecessor(self) -> "ProductionStage | None":
        seq = ProductionStage.ordered()
        i = self.position
        return seq[i - 1] if i > 0 else None


# Which manager role may LOG which stage.
# DM and MD are omitted deliberately — they are handled as bypass roles in the
# service, so adding a stage never requires remembering to grant them access.
STAGE_ROLE_ACCESS: dict[ProductionStage, set[UserRole]] = {
    ProductionStage.CUTTING:          {UserRole.CUTTING_MANAGER},
    ProductionStage.FUSING:           {UserRole.CUTTING_MANAGER},
    ProductionStage.PASTING:          {UserRole.STITCHING_MANAGER},
    ProductionStage.SHELL_STITCHING:  {UserRole.STITCHING_MANAGER},
    ProductionStage.LINE_ATTACH:      {UserRole.STITCHING_MANAGER},
    ProductionStage.LINE_STITCHING:   {UserRole.STITCHING_MANAGER},
    ProductionStage.FINAL_FINISH:     set(),   # DM/MD only
    ProductionStage.FINAL_INSPECTION: set(),   # DM/MD only
}


class Designation(str, enum.Enum):
    """Employee skill/designation. ALWAYS UPPERCASE — normalised on write.

    This is what gates 'a cutting employee cannot be logged on pasting'. It is a
    CONTROLLED VOCABULARY, not free text: the moment it is free text, 'Cutter',
    'cutter ' and 'CUTTTER' become three different skills and the gate is
    decorative.
    """
    CUTTER            = "CUTTER"
    FUSER             = "FUSER"
    PASTER            = "PASTER"
    SHELL_TAILOR      = "SHELL_TAILOR"
    LINE_ATTACHER     = "LINE_ATTACHER"
    LINE_TAILOR       = "LINE_TAILOR"
    FINISHER          = "FINISHER"
    INSPECTOR         = "INSPECTOR"
    TAILOR            = "TAILOR"      # legacy/general — see MULTI_STAGE below
    HELPER            = "HELPER"
    SUPERVISOR        = "SUPERVISOR"
    OTHER             = "OTHER"

    @classmethod
    def normalise(cls, raw: str | None) -> str | None:
        """'  shell tailor ' -> 'SHELL_TAILOR'. Unknown values are upper-cased and
        kept rather than rejected: the seeded data has designations we have not
        catalogued, and failing a create() on an unrecognised job title would
        block HR on a Friday afternoon."""
        import re
        if raw is None:
            return None
        token = re.sub(r"[\s\-/]+", "_", raw.strip()).upper()
        return token or None


# Which designations may work which stage.
# TAILOR/HELPER/SUPERVISOR are deliberately multi-stage: the source payroll files
# use 'TAILOR' for people who work several stitching stations.
STAGE_DESIGNATIONS: dict[ProductionStage, set[str]] = {
    ProductionStage.CUTTING:          {"CUTTER"},
    ProductionStage.FUSING:           {"FUSER", "CUTTER"},
    ProductionStage.PASTING:          {"PASTER"},
    ProductionStage.SHELL_STITCHING:  {"SHELL_TAILOR", "TAILOR"},
    ProductionStage.LINE_ATTACH:      {"LINE_ATTACHER", "TAILOR"},
    ProductionStage.LINE_STITCHING:   {"LINE_TAILOR", "TAILOR"},
    ProductionStage.FINAL_FINISH:     {"FINISHER"},
    ProductionStage.FINAL_INSPECTION: {"INSPECTOR", "FINISHER"},
}

# Designations that may work ANY stage — no skill gate.
MULTI_STAGE_DESIGNATIONS: frozenset[str] = frozenset({"HELPER", "SUPERVISOR", "OTHER"})

# Import barcode-specific enums LAST to avoid circular imports.
from app.core.enums_barcode import *  # noqa: E402,F401,F403