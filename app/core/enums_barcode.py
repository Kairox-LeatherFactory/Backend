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


def next_chain_stage(completed_codes) -> "ProductionStage | None":
    """The next leather-chain stage after everything in `completed_codes`.

    PURE — takes a set of operation codes, touches no database. It exists so the
    WRITE path (ProductionService._infer_stage_for_piece, which decides what a
    scan logs) and the READ paths (/barcode/resolve, /production/piece-state,
    which tell the screen what is coming) cannot answer the question differently.
    Two copies of this loop would drift, and the drift is invisible until a screen
    offers a stage the log then refuses.

    "Furthest completed, plus one" rather than "first not completed": a piece that
    was reworked or logged out of order should advance from how far it has
    actually got, not stall at the earliest gap.

    Returns None when the piece has finished the chain.
    """
    done = {(c or "").strip().upper() for c in (completed_codes or ())}
    chain = ProductionStage.leather_chain()
    furthest = -1
    for i, stage in enumerate(chain):
        if stage.value in done:
            furthest = i
    nxt = furthest + 1
    return chain[nxt] if 0 <= nxt < len(chain) else None


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
#
# THE FLOOR IS CROSS-TRAINED, and this matrix says so (Hamthan, 2026-09-20).
# A CUTTER also works FUSING and the LINING cut; a TAILOR also pastes. Those
# three pairs used to be absent, so every time a cutter fused a garment the log
# carried a skill warning that was not an anomaly at all — and a warning that
# fires on normal work is one nobody reads, which costs the gate its whole
# value.
#
# THIS GATE WARNS, IT DOES NOT BLOCK (production/service.py, GATE 2: the piece
# is logged either way, with no `continue`). So an entry here is not permission
# to do the work — anyone can be recorded at any stage — it is a statement about
# which pairings are ORDINARY. Add a designation when the floor genuinely
# cross-trains it, not to silence a warning somebody found annoying.
STAGE_DESIGNATIONS: dict[ProductionStage, set[str]] = {
    ProductionStage.LEATHER_CUTTING:  {"CUTTER"},
    # A cutter cuts both sides: leather and lining are parallel entries to the
    # pipeline, and the same person often does both.
    ProductionStage.LINING_CUTTING:   {"LINING_CUTTER", "CUTTER"},
    ProductionStage.FUSING:           {"FUSER", "CUTTER"},
    ProductionStage.PASTING:          {"PASTER", "TAILOR"},
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
    LEATHER_SHEET = "LEATHER_SHEET"  # ONE physical hide — see below
    EMPLOYEE = "EMPLOYEE"           # editable / reassignable / retirable
    DRAWER = "DRAWER"               # static, recycling


# WHY A SHEET GETS ITS OWN CODE AND A BUTTON DOES NOT.
#
#   A LOT barcode names a SPEC — "SUEDE-A32 · NAVY · 1.2mm" — and the quantity
#   behind it is fungible: one metre of that lining is any other metre, and one
#   button out of a 5,000-button packet is any other button. One code for the
#   packet, and a count that goes down, says everything true about it.
#
#   A LEATHER SHEET IS NOT FUNGIBLE. It is one hide, individually measured
#   (43, 47, 40 dcm — no two alike), individually expensive, and it is issued to
#   ONE named cutter for ONE garment. "How much leather did this jacket take" is
#   not answerable from a lot-level number; it is the sum of the specific hides
#   that went into it. So the sheet is the unit that carries the code.
#
#   That is also why there is no ACCESSORY_UNIT here and there must not be: it
#   would mean printing five thousand labels to learn nothing the packet count
#   does not already say.
class SheetStatus(str, enum.Enum):
    """One hide's life. It does NOT recycle — a cut sheet is gone.

        IN_STOCK    received and measured, nothing has claimed it
        ALLOCATED   put on a DRAFT cutting row; no longer offerable to another row
        ISSUED      the row was approved and the hide handed to the cutter
        CONSUMED    the cutting event was logged; its dcm is spent
        RETURNED    the cutter did not need it — back to IN_STOCK's pool
        SCRAPPED    damaged/unusable; leaves stock without ever being cut

    ALLOCATED EXISTS TO STOP TWO ROWS CLAIMING ONE HIDE. Without it the allocator
    would hand the same 47-dcm sheet to two garments the moment two rows are
    generated in one session, and only the second cutter would find out.
    """
    IN_STOCK = "IN_STOCK"
    ALLOCATED = "ALLOCATED"
    ISSUED = "ISSUED"
    CONSUMED = "CONSUMED"
    RETURNED = "RETURNED"
    SCRAPPED = "SCRAPPED"


class CuttingRowStatus(str, enum.Enum):
    """One cutting row's life. APPROVED is the freeze line.

        DRAFT      the manager is still editing; sheets may be added or removed
                   freely and nothing has been promised to anyone
        APPROVED   the cutter has confirmed the hides and the manager signed off;
                   the numbers are now what the Production Logger will spend
        LOGGED     the LEATHER_CUTTING event exists; the hides are consumed
        CANCELLED  abandoned; its hides went back to stock

    WHY NOT A BOOLEAN `is_approved`. Approval here moves stock and decides what a
    garment cost, so it needs a state name, a timestamp, an actor and an audit_log
    row — the same rule the drawer RECEIVED/SENDED and the breakdown release
    already follow (CLAUDE.md §15).
    """
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    LOGGED = "LOGGED"
    CANCELLED = "CANCELLED"


# A row whose hides are spoken for. Used to decide whether deleting/reopening a
# row has to hand sheets back.
CUTTING_ROW_HOLDS_SHEETS = frozenset({
    CuttingRowStatus.DRAFT.value, CuttingRowStatus.APPROVED.value})


# The statuses that still count as physically in the store. RETURNED is here
# because a returned hide is back on the shelf; the status is kept distinct from
# IN_STOCK only so the row's history reads honestly.
SHEET_IN_STORE = frozenset({SheetStatus.IN_STOCK.value, SheetStatus.RETURNED.value})

# The statuses a sheet can be allocated FROM. Deliberately not SHEET_IN_STORE +
# ALLOCATED: re-allocating an already-allocated hide is the double-claim bug.
SHEET_ALLOCATABLE = SHEET_IN_STORE


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


class IntakeStatus(str, enum.Enum):
    """How far a DELIVERY has been entered. Not what the verdict was.

    A different question from ReceiptStatus above, which is about the material
    (approved or rejected). This is about the PAPERWORK, and it exists because
    the two halves of a delivery are entered at two different moments:

        PENDING     the van came, somebody typed article + colour + total, and
                    the material is in the building and cuttable. The approved /
                    rejected split and the per-hide measurements are not in yet.
        COMPLETED   the QC split is entered and, for leather, the hides are
                    measured. on_hand has been corrected to the approved figure.

    UPPERCASE VALUES, unlike ReceiptStatus. Both are plain String columns rather
    than native PG enums, but this one is written by the arrival flow and read
    back by a query filter, so the value and the member name are kept identical —
    the mismatch between the two is exactly what CLAUDE.md §13 documents going
    wrong on the native enums.
    """
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"


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


class JobWorkStatus(str, enum.Enum):
    """One dispatch's life.

        OUT       the garments are at the vendor
        PARTIAL   some have come back, some have not
        RETURNED  every piece is accounted for
        CANCELLED the dispatch was abandoned; the pieces came straight back
    """
    OUT = "OUT"
    PARTIAL = "PARTIAL"
    RETURNED = "RETURNED"
    CANCELLED = "CANCELLED"


class JobWorkPieceStatus(str, enum.Enum):
    """One garment on one dispatch.

    SHORT and REJECTED are separate on purpose: a piece that never came back is
    a loss to chase with the vendor, and a piece that came back badly done is a
    quality matter. Collapsing them would hide which conversation to have — and
    neither is paid for.
    """
    OUT = "OUT"
    BACK = "BACK"
    SHORT = "SHORT"
    REJECTED = "REJECTED"


# The statuses in which a garment is still physically outside the factory.
JOBWORK_PIECE_AWAY = frozenset({JobWorkPieceStatus.OUT.value})


class InspectionVerdict(str, enum.Enum):
    """Did this garment pass at the stage it was inspected at?"""
    PASS = "PASS"
    REJECT = "REJECT"


class ReworkAction(str, enum.Enum):
    """What a rejected garment needs.

        FIX   repaired in place. The piece does not move: whoever can mend it
              does so at the stage it is already at, no earlier stage is redone
              and no new material is cut.
        REDO  an EARLIER stage is performed again. The piece goes back to the
              stage named on the rejection and walks forward from there.

    The difference is not cosmetic: a FIX leaves the chain intact, while a REDO
    re-opens a stage the sequence gate already considers complete.
    """
    FIX = "FIX"
    REDO = "REDO"


class DefectType(str, enum.Enum):
    """Whose fault is this, and it is asked because the factory asks it.

        PRODUCT_DAMAGE  the material itself was bad — a flaw in the hide, a
                        fault that arrived with the delivery. Nobody on the
                        floor is answerable for it.
        WORKMANSHIP     a stage was not done properly. The worker who did THAT
                        stage is answerable for this piece.

    WHY THE DISTINCTION IS STORED RATHER THAN LEFT IN THE REASON TEXT. "That
    employee is responsible for that piece" is a fact somebody will be asked to
    produce later — for a wage conversation, for a supplier claim, for a pattern
    across a month. A sentence in a free-text box cannot be counted; a column
    can. And the two point at different remedies: workmanship is a training or
    pay matter, product damage is a supplier matter.
    """
    PRODUCT_DAMAGE = "PRODUCT_DAMAGE"
    WORKMANSHIP = "WORKMANSHIP"


class InspectionStatus(str, enum.Enum):
    """A rejection is RAISED by the floor and APPROVED by the DM.

    Any manager or HR can raise one — they are the people who see the defect —
    but sending a garment backwards re-opens a completed stage, re-orders work
    and may cost material, so the DM signs it off before the piece actually
    moves. Until then the rejection is a report, not a movement.

        PENDING   raised, waiting on the DM
        APPROVED  the DM agreed; the piece has moved back (or is being fixed)
        DECLINED  the DM disagreed; the piece stays where it was
        RESOLVED  the rework is done and the piece has been re-logged forward
    """
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    DECLINED = "DECLINED"
    RESOLVED = "RESOLVED"


# Who may RAISE a rejection: everyone who stands at a stage and can see a defect.
# Approval is narrower — see INSPECTION_APPROVER_ROLES.
INSPECTION_RAISER_ROLES = frozenset({
    "managing_director", "direct_manager", "hr", "cutting_manager",
    "lining_manager", "stitching_manager", "store_manager", "supervisor",
})
# Who may SEND A GARMENT BACKWARDS. The DM, and the MD who outranks everyone.
INSPECTION_APPROVER_ROLES = frozenset({"direct_manager", "managing_director"})


class StoreState(str, enum.Enum):
    """Where a garment stands IN THE STORE. Lives on the PIECE, not on a drawer.

    WHY THE DRAWER WENT AWAY. There were 200 physical drawers. A style releases
    100+ garments, stalls mid-chain, and the next 50 have nowhere to go; the DM
    then had to re-allocate by hand, which is complicated enough that it did not
    happen. So the drawer was a BOTTLENECK that recorded nothing the piece could
    not record itself: every fact the old DrawerState carried — leather in,
    lining in, kit issued, received, sent — is a fact about the GARMENT.

        WAITING          not in the store yet (still on the cut side)
        MERGED           expected in the store; nothing has arrived
        HOLDING_LEATHER  leather part stored
        HOLDING_LINING   lining stored, leather not yet
        HOLDING_BOTH     both parts stored
        RECEIVED         confirmed complete (leather + lining + kit as required)
        SENDED           released to line-stitching

    THE VALUES ARE THE OLD DrawerState VALUES, DELIBERATELY. The migration copies
    drawer.state straight across, so a garment mid-store keeps its exact position
    and every label table, dashboard filter and analytics bucket keeps working
    without a translation layer nobody would maintain.

    IT NO LONGER RECYCLES. A drawer went back to WAITING for the next garment
    because the drawer was reused; a piece ships once, so PACKAGE_EXPORT simply
    clears its store flags and the piece leaves the store for good.
    """
    WAITING = "waiting"
    MERGED = "merged"
    HOLDING_LEATHER = "holding_leather"
    HOLDING_LINING = "holding_lining"
    HOLDING_BOTH = "holding_both"
    RECEIVED = "received"
    SENDED = "sended"


# The states in which a garment is physically parked in the store. Drives the
# STORE display overlay and the store dashboard's buckets.
STORE_HOLDING_STATES = frozenset({
    StoreState.HOLDING_LEATHER.value, StoreState.HOLDING_LINING.value,
    StoreState.HOLDING_BOTH.value, StoreState.RECEIVED.value,
    StoreState.SENDED.value,
})


class StorePart(str, enum.Enum):
    """What is being scanned into the store — the old DrawerPart, unchanged.

    ACCESSORY IS NEVER INFERRED, AND THAT IS A SAFETY RULE, NOT A CONVENIENCE.
    Inferring LEATHER vs LINING from a piece's history can at worst set the wrong
    boolean, which a human can undo. An inferred ACCESSORY would SPEND STOCK —
    it decrements every accessory lot on the style's spec — so a mis-inference
    would move money nothing on the floor asked to move.
    """
    LEATHER = "LEATHER"
    LINING = "LINING"
    ACCESSORY = "ACCESSORY"


class DrawerPart(str, enum.Enum):
    """What is being scanned into a drawer.

    ACCESSORY IS NEVER INFERRED, AND THAT IS A SAFETY RULE, NOT A CONVENIENCE ONE.
        `StoreService.infer_part` reads a piece's history to decide whether a
        scan is the leather or the lining arriving. The worst a wrong guess can
        do there is set the wrong boolean, which a human can undo. An inferred
        ACCESSORY would *spend stock* — it decrements every accessory lot on the
        style's spec — so a mis-inference would move money that nothing on the
        floor asked to move. The kit is therefore explicit-only: `infer_part`
        still returns LEATHER or LINING and nothing else, and `part_inferred`
        stays honest.
    """
    LEATHER = "LEATHER"
    LINING = "LINING"
    ACCESSORY = "ACCESSORY"     # NEVER inferred — see above


class MaterialIssueSource(str, enum.Enum):
    """How a `piece_material_issue` row came to exist.

    CUT is RESERVED, not used. Leather and lining consumption lives on
    ProductionEvent (`leather_lot_id` / `lining_lot_id` / `consumption_qty`) —
    the lot link belongs to the ACT OF CUTTING, never to the piece (CLAUDE.md
    §8). The member exists so a future decision to unify the two ledgers has a
    name already minted rather than a migration to add one.
    """
    STORE_KIT = "STORE_KIT"     # the drawer kit scan — the normal path
    CUT = "CUT"                 # reserved; see above
    MANUAL = "MANUAL"           # off-spec correction, POST /materials/issues


class KitStatus(str, enum.Enum):
    """Where a piece stands against its accessory spec.

    NOT_REQUIRED is load-bearing: it is what every style that predates the spec
    feature reports, and it tells the screen to HIDE the checklist rather than
    render an empty one. "No accessories declared" and "accessories declared but
    none issued" are different facts and must not collapse into one.
    """
    NOT_REQUIRED = "NOT_REQUIRED"   # the style declares no accessories
    PENDING = "PENDING"             # declared, nothing issued
    PARTIAL = "PARTIAL"             # some issued, or some line unresolved
    ISSUED = "ISSUED"               # every line issued in full


# ══════════════════════════════════════════════════════════════════════════
# AUDIT ACTIONS (written to core.AuditLog for the hard transitions)
# ══════════════════════════════════════════════════════════════════════════
class BarcodeAuditAction(str, enum.Enum):
    DRAWER_RECEIVED = "DRAWER_RECEIVED"
    DRAWER_SENDED = "DRAWER_SENDED"
    EMPLOYEE_BARCODE_REISSUE = "EMPLOYEE_BARCODE_REISSUE"
    EMPLOYEE_BARCODE_DEACTIVATE = "EMPLOYEE_BARCODE_DEACTIVATE"
    MATERIAL_RECEIVED = "MATERIAL_RECEIVED"
    # The kit scan spends stock, so it is a hard transition like the two drawer
    # ones above and gets the same audit row.
    MATERIAL_KIT_ISSUED = "MATERIAL_KIT_ISSUED"
    MATERIAL_ISSUED_MANUAL = "MATERIAL_ISSUED_MANUAL"
    # Confirming a spec is what unlocks release, so who confirmed it and what
    # they confirmed has to be recoverable months later.
    STYLE_MATERIAL_SPEC_CONFIRMED = "STYLE_MATERIAL_SPEC_CONFIRMED"
    STYLE_MATERIAL_SPEC_AMENDED = "STYLE_MATERIAL_SPEC_AMENDED"
    # Cutting V2 — the approval that turns an editable grid into a spend.
    CUTTING_ROW_APPROVED = "CUTTING_ROW_APPROVED"
    CUTTING_ROW_REOPENED = "CUTTING_ROW_REOPENED"
    CUTTING_SHEET_RETURNED = "CUTTING_SHEET_RETURNED"
    # Stage-wise reject & rework.
    PIECE_REJECTED = "PIECE_REJECTED"
    PIECE_REWORK_APPROVED = "PIECE_REWORK_APPROVED"
    PIECE_REWORK_DECLINED = "PIECE_REWORK_DECLINED"
    # Work sent outside the factory.
    JOB_WORK_DISPATCHED = "JOB_WORK_DISPATCHED"
    JOB_WORK_RETURNED = "JOB_WORK_RETURNED"





# ══════════════════════════════════════════════════════════════════════════
# FINAL STEP (do at merge, one time):
#   In core/enums.py:
#     1. add   LINING_MANAGER = "lining_manager"   to class UserRole
#     2. add at end of file:   from app.core.enums_barcode import *   # noqa
#   Then every `from app.core.enums import ProductionStage` etc. resolves, and
#   the _role() shim above becomes a no-op (the real member is found).
# ══════════════════════════════════════════════════════════════════════════