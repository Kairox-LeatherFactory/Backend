"""
================================================================================
core/enums_barcode.py — Value-sets for the barcode / per-piece traceability build
================================================================================

WHY A SEPARATE FILE (and not just appended to core/enums.py)
    core/enums.py already holds the shipped enums (UserRole, WageType, ...). This
    build adds a large, cohesive block: the production STAGE model, employee skill
    DESIGNATIONS, the role/skill/stage gate maps, and the barcode / material /
    drawer / supplier value-sets. Keeping them in one importable module makes the
    barcode feature a single reviewable unit and keeps the diff on the shipped
    file to two lines (the two re-exports noted at the bottom).

    AT MERGE TIME: paste this block into core/enums.py (or keep the module and
    `from app.core.enums_barcode import *` at the end of core/enums.py). The
    imports throughout the feature use `from app.core.enums import ...`, so the
    re-export is what makes that work — see the FINAL STEP note at the bottom.

WHAT LIVES HERE, AND WHY IT IS LAW NOT CONFIG
    The `operation` table stays the source of truth for LABELS and RATES (the MD
    edits it). But four things are BUSINESS LAW, not config, and belong in code:

      1. stage ORDER            — cutting before fusing before pasting ...
      2. the two PARALLEL cut   — leather-cut and lining-cut run at once on one
         paths                    piece; the login screen tells them apart
      3. role  → stage access   — which manager role may LOG which stage
      4. designation → stage    — which employee skill may WORK which stage

    Encoding order against Operation.sequence alone means a stray UPDATE to a
    sequence number silently reorders the factory. Codes are stable; sequences
    are not. So the ordering, the gates, and the merge rule live here.
================================================================================
"""
from __future__ import annotations

import enum
import re

from app.core.enums import UserRole


# ══════════════════════════════════════════════════════════════════════════
# NEW ROLE — the lining cutting manager
# ══════════════════════════════════════════════════════════════════════════
# UserRole is the shipped enum; str-Enums cannot be extended at runtime, so this
# member must be ADDED to UserRole in core/enums.py at merge time:
#
#     LINING_MANAGER = "lining_manager"   # logs the lining-cut path
#
# Everything below references UserRole.LINING_MANAGER. The helper resolves it
# whether or not the member exists yet, so this module imports cleanly on the
# current tree and "lights up" the moment the member is added — no import error
# during the transition.
def _role(name: str, value: str) -> UserRole:
    """Return UserRole.<name> if present, else a shim with the right .value.

    Lets this module load before UserRole.LINING_MANAGER is merged. The shim is
    only ever used for map keys / comparisons by value, never persisted.
    """
    member = getattr(UserRole, name, None)
    if member is not None:
        return member

    class _Shim(str):
        # Quacks like a str-Enum member for the two things we use: .value and ==.
        @property
        def value(self) -> str:  # noqa: D401
            return value

    shim = _Shim(value)
    return shim  # type: ignore[return-value]


LINING_MANAGER = _role("LINING_MANAGER", "lining_manager")

# ══════════════════════════════════════════════════════════════════════════
# PRODUCTION STAGES
# ══════════════════════════════════════════════════════════════════════════
class ProductionStage(str, enum.Enum):
    """The canonical production pipeline.

    TWO ENTRY STAGES, ONE JOIN.
        LEATHER_CUTTING and LINING_CUTTING are PARALLEL entry points on the same
        piece — a garment's leather and its lining are cut independently, by
        different managers, and the login screen (not a button) says which is
        which. They have NO predecessor. Everything after the drawer merge is a
        single linear chain.

        This breaks the naive "one linear predecessor()" model: the leather side
        has its own short chain (cut → fusing → pasting), the lining side is a
        single cut, and the two REJOIN at the drawer. So `predecessor()` returns
        the leather-side predecessor for the leather chain and None for the two
        cut entries; the COMPLETENESS (merge) gate — not sequence — governs the
        join into LINE_STITCHING. See MERGE_GATE_ENTRY below.

    Operation.code MUST equal one of these values for the stage machinery to
    apply. A legacy/config code that is not a member (e.g. 'FF-SMS') simply has
    its stage-order and skill gates skipped — we never invent an order for a code
    we do not recognise.
    """

    LEATHER_CUTTING = "LEATHER_CUTTING"
    LINING_CUTTING = "LINING_CUTTING"
    FUSING = "FUSING"
    PASTING = "PASTING"
    LINE_STITCHING = "LINE_STITCHING"
    SHELL_STITCHING = "SHELL_STITCHING"
    FINAL_FINISH = "FINAL_FINISH"
    FINAL_INSPECTION = "FINAL_INSPECTION"
    PACKAGE_EXPORT = "PACKAGE_EXPORT"

    # ── ordering ──────────────────────────────────────────────────────────
    # The LEATHER chain, in order. Lining is a single parallel cut that joins at
    # the merge gate, so it is not part of this linear list.
    @classmethod
    def leather_chain(cls) -> list["ProductionStage"]:
        return [
            cls.LEATHER_CUTTING, cls.FUSING, cls.PASTING,
            cls.LINE_STITCHING, cls.SHELL_STITCHING,
            cls.FINAL_FINISH, cls.FINAL_INSPECTION, cls.PACKAGE_EXPORT,
        ]

    @classmethod
    def order_index(cls) -> dict[str, int]:
        return {s.value: i for i, s in enumerate(cls.leather_chain())}

    @property
    def is_cut_entry(self) -> bool:
        return self in (ProductionStage.LEATHER_CUTTING, ProductionStage.LINING_CUTTING)

    @property
    def requires_consumption(self) -> bool:
        """Only the two cut stages capture material consumption per piece."""
        return self.is_cut_entry

    def predecessor(self) -> "ProductionStage | None":
        """The stage that must be complete before this one, on the LEATHER chain.

        Returns None for the two cut entries (they start a chain) AND for
        LINE_STITCHING — because line-stitching's precondition is not "pasting
        done" but "leather AND lining both merged in the drawer", which the
        completeness gate enforces, not the sequence gate.
        """
        if self.is_cut_entry:
            return None
        if self is ProductionStage.LINE_STITCHING:
            return None  # governed by MERGE_GATE, not sequence
        chain = self.leather_chain()
        try:
            i = chain.index(self)
        except ValueError:
            return None
        return chain[i - 1] if i > 0 else None


# The stage a piece enters ONLY after its drawer is SENDED (leather+lining merged).
MERGE_GATE_ENTRY = ProductionStage.LINE_STITCHING


# Which manager role may LOG which stage.
# DM and MD are omitted deliberately — they bypass in the service, so adding a
# stage never means remembering to grant them.
# THE CUT SIDE IS THE ONLY SPLIT. Each cut entry belongs to its own manager,
# because each is fixed by its own login screen (SCREEN_TO_STAGE). The floor
# stages downstream of the cut — FUSING through FINAL_FINISH — belong to the
# STITCHING manager, who is the one role on the PIPELINE screen and therefore
# the only manager whose scan can infer those stages at all. The last two stay
# empty on purpose: inspection and export are APPROVALS, not floor work, so only
# the DM/MD/HR bypass reaches them.
#
# FUSING lived here as {CUTTING_MANAGER} and was UNREACHABLE: ROLE_TO_SCREEN pins
# a cutting manager to LEATHER_CUT, so their scan is fixed at LEATHER_CUTTING and
# never infers FUSING. The grant could never fire; its only live effect was to
# 403 the stitching manager, the one role that CAN reach the stage. Production
# bears this out — every FUSING event in the DB was entered by the DM bypass,
# none by a manager. Moving it to STITCHING_MANAGER makes the grant reachable.
STAGE_ROLE_ACCESS: dict[ProductionStage, set] = {
    ProductionStage.LEATHER_CUTTING:  {UserRole.CUTTING_MANAGER},
    ProductionStage.LINING_CUTTING:   {UserRole.LINING_MANAGER},
    ProductionStage.FUSING:           {UserRole.STITCHING_MANAGER},
    ProductionStage.PASTING:          {UserRole.STITCHING_MANAGER},
    ProductionStage.LINE_STITCHING:   {UserRole.STITCHING_MANAGER},
    ProductionStage.SHELL_STITCHING:  {UserRole.STITCHING_MANAGER},
    ProductionStage.FINAL_FINISH:     {UserRole.STITCHING_MANAGER},
    ProductionStage.FINAL_INSPECTION: set(),   # DM/MD/HR only (approval)
    ProductionStage.PACKAGE_EXPORT:   set(),   # DM/MD/HR only (approval)
}


# The screen context the frontend declares → the cut stage it logs. This is the
# "no stage buttons" rule: the login screen fixes the cut path, the server does
# not accept a stage in the request.
class ScreenContext(str, enum.Enum):
    LEATHER_CUT = "LEATHER_CUT"     # Cutting Manager screen → LEATHER_CUTTING
    LINING_CUT = "LINING_CUT"       # Lining Manager screen  → LINING_CUTTING
    PIPELINE = "PIPELINE"           # everything after cut — stage from history


SCREEN_TO_STAGE: dict[ScreenContext, ProductionStage] = {
    ScreenContext.LEATHER_CUT: ProductionStage.LEATHER_CUTTING,
    ScreenContext.LINING_CUT: ProductionStage.LINING_CUTTING,
}

ROLE_TO_SCREEN: dict = {
    UserRole.CUTTING_MANAGER: ScreenContext.LEATHER_CUT,
    UserRole.LINING_MANAGER:  ScreenContext.LINING_CUT,
    UserRole.STITCHING_MANAGER: ScreenContext.PIPELINE,
    UserRole.SUPERVISOR: ScreenContext.PIPELINE,
    UserRole.HR: ScreenContext.PIPELINE,
}

def screen_for_role(role, override=None):
    """Resolve the screen context for a logging request.

    - DM / MD / HR: bypass roles — they may log any stage, so they PASS an
      explicit screen override (required for the two parallel cut stages,
      LEATHER_CUT / LINING_CUT); default PIPELINE when none is given.
    - CUTTING/LINING/STITCHING managers, supervisor: pinned by role (role wins,
      any sent value is ignored).
    - Anyone else not in ROLE_TO_SCREEN: may pass an override; default PIPELINE.
    """
    from app.core.enums import UserRole  # local import to avoid cycles
    if role in (UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR):
        return override or ScreenContext.PIPELINE
    pinned = ROLE_TO_SCREEN.get(role)
    if pinned is not None:
        return pinned                     # role wins; sent value ignored
    return override or ScreenContext.PIPELINE

# Which role each cut SCREEN expects — the screen↔role cross-check. A leather
# cutter scanned on the lining screen (or the reverse) raises a warning, not a
# hard block: the work still logs, but the mismatch is surfaced.
SCREEN_EXPECTED_ROLE: dict[ScreenContext, set] = {
    ScreenContext.LEATHER_CUT: {UserRole.CUTTING_MANAGER},
    ScreenContext.LINING_CUT: {LINING_MANAGER},
}


# ══════════════════════════════════════════════════════════════════════════
# EMPLOYEE DESIGNATIONS (skill) — always uppercase, controlled vocabulary
# ══════════════════════════════════════════════════════════════════════════
class Designation(str, enum.Enum):
    """Employee skill. ALWAYS UPPERCASE, normalised on write.

    This is what gates 'a cutting employee cannot be logged on pasting'. It is a
    CONTROLLED VOCABULARY, not free text: the moment it is free text, 'Cutter',
    'cutter ' and 'CUTTTER' become three skills and the gate is decorative.
    """
    CUTTER = "CUTTER"
    LINING_CUTTER = "LINING_CUTTER"
    FUSER = "FUSER"
    PASTER = "PASTER"
    LINE_TAILOR = "LINE_TAILOR"
    SHELL_TAILOR = "SHELL_TAILOR"
    FINISHER = "FINISHER"
    INSPECTOR = "INSPECTOR"
    PACKER = "PACKER"
    TAILOR = "TAILOR"        # general stitching hand — multi-station
    HELPER = "HELPER"
    SUPERVISOR = "SUPERVISOR"
    TRIMMER = "TRIMMER"
    CHEMICAL_TECHNICIAN = "CHEMICAL_TECHNICIAN"
    SECURITY = "SECURITY"
    MERCHANDISER = "MERCHANDISER"
    STITCHING_INSTRUCTOR = "STITCHING_INSTRUCTOR"
    QC_INSPECTOR = "QC_INSPECTOR"


    @classmethod
    def normalise(cls, raw: str | None) -> str | None:
        """'  shell tailor ' / 'Shell-Tailor' -> 'SHELL_TAILOR'.

        Unknown values are upper-cased and KEPT rather than rejected: the seeded
        data carries job titles we have not catalogued, and failing create() on
        an unrecognised title would block HR. The skill gate treats an
        uncatalogued designation as unknown → it passes (fail-open), tightening
        as HR backfills the vocabulary.
        """
        if raw is None:
            return None
        token = re.sub(r"[\s\-/]+", "_", raw.strip()).upper()
        return token or None


# Which designations may work which stage.
STAGE_DESIGNATIONS: dict[ProductionStage, set[str]] = {
    ProductionStage.LEATHER_CUTTING:  {"CUTTER"},
    ProductionStage.LINING_CUTTING:   {"LINING_CUTTER"},
    ProductionStage.FUSING:           {"FUSER"},
    ProductionStage.PASTING:          {"PASTER"},
    ProductionStage.LINE_STITCHING:   {"LINE_TAILOR", "TAILOR"},
    ProductionStage.SHELL_STITCHING:  {"SHELL_TAILOR", "TAILOR"},
    ProductionStage.FINAL_FINISH:     {"FINISHER", "TAILOR"},
    ProductionStage.FINAL_INSPECTION: {"INSPECTOR", "FINISHER"},
    ProductionStage.PACKAGE_EXPORT:   {"PACKER", "FINISHER"},
}

# Designations that may work ANY stage — no skill gate.
MULTI_STAGE_DESIGNATIONS: frozenset[str] = frozenset({"HELPER", "SUPERVISOR"})


# ══════════════════════════════════════════════════════════════════════════
# BARCODE REGISTRY
# ══════════════════════════════════════════════════════════════════════════
class BarcodeType(str, enum.Enum):
    PIECE = "PIECE"                 # parent — permanent garment identity
    LEATHER_LOT = "LEATHER_LOT"     # child 1
    LINING_LOT = "LINING_LOT"       # child 2
    ACCESSORY_LOT = "ACCESSORY_LOT"  # buttons / zips / thread / other
    EMPLOYEE = "EMPLOYEE"           # editable / reassignable / retirable
    DRAWER = "DRAWER"               # static, recycling


class BarcodeStatus(str, enum.Enum):
    ACTIVE = "active"
    RETIRED = "retired"             # employee left / label reissued — resolve → 410


# ══════════════════════════════════════════════════════════════════════════
# MATERIALS & INVENTORY  (human-driven — separate from BOM-driven inventory)
# ══════════════════════════════════════════════════════════════════════════
class MaterialCategory(str, enum.Enum):
    LEATHER = "LEATHER"
    LINING = "LINING"
    ACCESSORY = "ACCESSORY"


class MaterialSubtype(str, enum.Enum):
    """Only meaningful for LINING and ACCESSORY; NULL for LEATHER."""
    # LINING
    RIBS = "RIBS"            # kg / stripes
    KNIT = "KNIT"            # number of pieces
    PLAIN_LINING = "PLAIN_LINING"
    # ACCESSORY
    BUTTON = "BUTTON"        # count, size
    ZIP = "ZIP"             # count, size
    THREAD = "THREAD"        # mtrs, thickness
    OTHER = "OTHER"          # description, count


# The unit of measure per (category, subtype). Drives stock arithmetic + display.
def uom_for(category: str, subtype: str | None) -> str:
    c = (category or "").upper()
    s = (subtype or "").upper() or None
    if c == "LEATHER":
        return "dcm"
    if c == "LINING":
        if s == "RIBS":
            return "kg"
        if s == "KNIT":
            return "pcs"
        return "mtrs"
    if c == "ACCESSORY":
        if s in ("BUTTON", "ZIP"):
            return "pcs"
        if s == "THREAD":
            return "mtrs"
        return "pcs"
    return "unit"


# ══════════════════════════════════════════════════════════════════════════
# MATERIAL SPEC — the exact "Add New" field list per (category, subtype)
# ══════════════════════════════════════════════════════════════════════════
# This is the single source of truth for BOTH the strict create-lot validation
# AND the stock-filter fields. Each spec says:
#   required  — attribute keys a lot of this kind MUST carry (else 422)
#   qty_field — which attribute holds the stock quantity (added to on_hand)
#   qty_uom   — the unit that quantity is in
#   filters   — the fields the DM may search/filter that kind of stock by
#
# Keyed by (CATEGORY, SUBTYPE or None). Leather has no subtype; lining/accessory
# resolve by subtype. A (category, subtype) not in this table is rejected — we do
# not invent a shape for a material class we were not told about.
#
# Mapping straight from the product spec:
#   Leather : article, colour, thickness, dcm
#   Lining/plain : article, colour, thickness, mtrs
#   Lining/ribs  : article, colour, kg
#   Lining/knit  : article, colour, piece-count
#   Button  : article, colour, size, count
#   Zip     : article, colour, size, count
#   Thread  : article, colour, thickness, mtrs
#   Other   : article, colour, description, count
# (article + colour are enforced on the top-level lot fields, not in attributes —
#  see MaterialService.create_lot — so `required` below lists only the ATTRIBUTE
#  keys beyond article/colour.)
MATERIAL_SPEC: dict[tuple[str, str | None], dict] = {
    ("LEATHER", None): {
        "required": {"thickness", "dcm"}, "qty_field": "dcm", "qty_uom": "dcm",
        "filters": ["article", "colour", "thickness"],
    },
    ("LINING", "PLAIN_LINING"): {
        "required": {"thickness", "mtrs"}, "qty_field": "mtrs", "qty_uom": "mtrs",
        "filters": ["article", "colour", "thickness"],
    },
    ("LINING", "RIBS"): {
        "required": {"kg"}, "qty_field": "kg", "qty_uom": "kg",
        "filters": ["article", "colour"],
    },
    ("LINING", "KNIT"): {
        "required": {"pcs"}, "qty_field": "pcs", "qty_uom": "pcs",
        "filters": ["article", "colour"],
    },
    ("ACCESSORY", "BUTTON"): {
        "required": {"size", "count"}, "qty_field": "count", "qty_uom": "pcs",
        "filters": ["article", "colour", "size"],
    },
    ("ACCESSORY", "ZIP"): {
        "required": {"size", "count"}, "qty_field": "count", "qty_uom": "pcs",
        "filters": ["article", "colour", "size"],
    },
    ("ACCESSORY", "THREAD"): {
        "required": {"thickness", "mtrs"}, "qty_field": "mtrs", "qty_uom": "mtrs",
        "filters": ["article", "colour", "thickness"],
    },
    ("ACCESSORY", "OTHER"): {
        "required": {"description", "count"}, "qty_field": "count", "qty_uom": "pcs",
        "filters": ["article", "colour"],
    },
}


def resolve_spec(category: str, subtype: str | None) -> dict | None:
    """The MATERIAL_SPEC row for a (category, subtype), or None if unknown.

    LEATHER ignores subtype. LINING with no subtype defaults to PLAIN_LINING (the
    common case: 'article, colour, thickness, mtrs'). ACCESSORY always needs a
    subtype (button/zip/thread/other) — no default, because there is no generic
    accessory quantity.
    """
    c = (category or "").upper()
    s = (subtype or "").upper() or None
    if c == "LEATHER":
        return MATERIAL_SPEC.get(("LEATHER", None))
    if c == "LINING":
        return MATERIAL_SPEC.get(("LINING", s or "PLAIN_LINING"))
    if c == "ACCESSORY":
        return MATERIAL_SPEC.get(("ACCESSORY", s)) if s else None
    return None


class ReceiptStatus(str, enum.Enum):
    """The receiving verdict split — approved adds stock, rejected is logged."""
    APPROVED = "approved"
    REJECTED = "rejected"


# ══════════════════════════════════════════════════════════════════════════
# SUPPLIER ORDERS  (manual, two-state)
# ══════════════════════════════════════════════════════════════════════════
class SupplierOrderStatus(str, enum.Enum):
    ORDERED = "ordered"
    ARRIVED = "arrived"


# ══════════════════════════════════════════════════════════════════════════
# DRAWERS — static, recycling state machine
# ══════════════════════════════════════════════════════════════════════════
class DrawerState(str, enum.Enum):
    """One drawer's lifecycle. It RECYCLES: after a piece ships, back to WAITING.

        WAITING            not merged to anything
        MERGED             assigned to a piece (sku+seq) at breakdown upload
        HOLDING_LEATHER    leather part stored
        HOLDING_BOTH       leather + lining both stored (complete if lining needed)
        RECEIVED           DM confirmed complete
        SENDED             DM released to line-stitching
        (then → WAITING once the piece clears final inspection and ships)
    """
    WAITING = "waiting"
    MERGED = "merged"
    HOLDING_LEATHER = "holding_leather"
    HOLDING_LINING = "holding_lining"   # F07: lining stored, leather not yet
    HOLDING_BOTH = "holding_both"
    RECEIVED = "received"
    SENDED = "sended"


class DrawerPart(str, enum.Enum):
    LEATHER = "LEATHER"
    LINING = "LINING"


# ══════════════════════════════════════════════════════════════════════════
# AUDIT ACTIONS (written to core.AuditLog for the hard transitions)
# ══════════════════════════════════════════════════════════════════════════
class BarcodeAuditAction(str, enum.Enum):
    DRAWER_RECEIVED = "DRAWER_RECEIVED"
    DRAWER_SENDED = "DRAWER_SENDED"
    EMPLOYEE_BARCODE_REISSUE = "EMPLOYEE_BARCODE_REISSUE"
    EMPLOYEE_BARCODE_DEACTIVATE = "EMPLOYEE_BARCODE_DEACTIVATE"
    MATERIAL_RECEIVED = "MATERIAL_RECEIVED"


# ══════════════════════════════════════════════════════════════════════════
# FINAL STEP (do at merge, one time):
#   In core/enums.py:
#     1. add   LINING_MANAGER = "lining_manager"   to class UserRole
#     2. add at end of file:   from app.core.enums_barcode import *   # noqa
#   Then every `from app.core.enums import ProductionStage` etc. resolves, and
#   the _role() shim above becomes a no-op (the real member is found).
# ══════════════════════════════════════════════════════════════════════════