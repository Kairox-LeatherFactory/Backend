"""
================================================================================
modules/dashboard/schemas.py — Manager Dashboard response models
================================================================================
Pydantic v2 (ConfigDict, not class Config). These are RESPONSE shapes only — the
dashboards take no request bodies; all inputs are query params on the router.

FOUR DASHBOARDS, ONE MODULE
    Cutting   (leather cut side)      — original, unchanged.
    Lining    (lining cut side)       — mirrors Cutting on LINING_CUTTING + lining lots.
    Stitching (pre/post-store funnel) — PASTING/FUSING → STORE → LINE/SHELL/FINAL.
    Store     (drawer movement)       — drawer-centric; STORE is a drawer state,
                                         never a ProductionEvent.

HONEST CONTRACT (shared across all four)
    Every "damage_*" field, every "expected_consumption"/"variance"/"waste"
    field, every "daily_target"/"photo" field is present in the contract but
    returns null/0 with a one-line reason in `meta.unsupported`, because the
    current schema stores no damage state, no expected-consumption (BOM) baseline,
    and no employee target/photo column. This keeps the frontend contract STABLE:
    when the PieceDamage model + BOM baseline land, these fields populate with no
    shape change on the client. (CTO decisions — see module notes.)
================================================================================
"""
from __future__ import annotations

import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


# ══════════════════════════════════════════════════════════════════════════
# SHARED
# ══════════════════════════════════════════════════════════════════════════
class DashboardMeta(BaseModel):
    generated_for: date
    scope: str                        # "all_clients" | "client:<id>"
    unsupported: dict[str, str] = Field(default_factory=dict)


# ══════════════════════════════════════════════════════════════════════════
# CUTTING  (original — preserved verbatim)
# ══════════════════════════════════════════════════════════════════════════
class ProductionKPIs(BaseModel):
    total_order_pieces: int
    minted_pieces: int
    assigned_pieces: int
    assigned_today: int
    completed_today: int
    overall_completed: int
    pending_today: int
    overall_pending: int
    damage_pieces: int = 0            # unsupported → 0 (see meta)
    damage_today: int = 0             # unsupported → 0
    rework_pieces: int
    rework_today: int


class LeatherKPIs(BaseModel):
    total_available_leather: float
    allocated_leather: float | None = None   # materials reservation ledger (flagged)
    consumed_leather: float
    remaining_leather: float
    total_leather_waste: float | None = None  # no waste column (flagged)


class CurrentStyle(BaseModel):
    style_id: uuid.UUID
    style_name: str
    article: str | None
    thickness: str | None
    minted: int
    completed: int
    pending: int
    stages: dict[str, int]


class CurrentOrder(BaseModel):
    order_id: uuid.UUID
    order_number: str
    client: str | None
    order_date: date | None
    delivery_deadline: date | None
    total_pieces: int
    completed: int
    pending: int
    styles: list[CurrentStyle]


class CutterRow(BaseModel):
    employee_id: uuid.UUID
    name: str
    designation: str | None
    assigned_pieces: int
    assigned_today: int
    rework_today: int
    consumed_leather: float
    daily_target: int | None = None   # no target column (flagged)
    damage_pieces: int = 0            # unsupported


class LeatherLotRow(BaseModel):
    lot_id: uuid.UUID
    article: str
    colour: str | None
    thickness: str | None
    leather_type: str | None          # subtype
    uom: str
    available: float
    consumed: float
    pieces_cut: int
    remaining: float


class OrderProgressRow(BaseModel):
    order_id: uuid.UUID
    order_number: str
    style_id: uuid.UUID
    style_name: str
    article: str | None
    order_date: date | None
    delivery_deadline: date | None
    total_ordered: int
    minted: int
    completed: int
    pending: int
    completion_pct: float
    delay_status: str                 # ON_TRACK | AT_RISK | LATE | NO_DEADLINE


class DailyRow(BaseModel):
    work_date: date
    assigned: int
    completed: int
    events: int


class PieceConsumptionRow(BaseModel):
    piece_code: str
    work_date: date
    employee: str
    stage: str
    actual_consumption: float | None
    expected_consumption: float | None = None   # BOM baseline (flagged)
    variance: float | None = None               # derived from expected (flagged)
    leather_article: str | None
    colour: str | None
    thickness: str | None
    style: str | None
    order_number: str | None
    size: str | None


class EmployeePieceRow(BaseModel):
    piece_code: str
    seq: int
    size: str | None
    colour: str | None
    style: str | None
    current_stage: str | None
    last_worked: date | None


class CuttingDashboard(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    meta: DashboardMeta
    production_kpis: ProductionKPIs
    leather_kpis: LeatherKPIs
    current_order: CurrentOrder | None
    cutters: list[CutterRow]
    leather_lots: list[LeatherLotRow]
    order_progress: list[OrderProgressRow]
    daily_production: list[DailyRow]


# ══════════════════════════════════════════════════════════════════════════
# LINING  (mirrors Cutting on the LINING_CUTTING stage + lining lots)
# ══════════════════════════════════════════════════════════════════════════
class LiningProductionKPIs(BaseModel):
    total_order_pieces: int
    lining_required_pieces: int       # minted pieces whose needs_lining is True
    assigned_pieces: int              # distinct pieces with a lining-cut event
    assigned_today: int
    completed_today: int              # lining-cut events today
    overall_completed: int            # distinct pieces with a lining-cut event
    pending_today: int                # remaining lining-required not yet cut (see meta)
    overall_pending: int              # lining_required − overall_completed
    damage_pieces: int = 0            # unsupported → 0
    damage_today: int = 0             # unsupported → 0
    rework_pieces: int
    rework_today: int


class LiningMaterialKPIs(BaseModel):
    total_available_lining: float
    allocated_lining: float | None = None    # reservation ledger (flagged)
    used_lining: float
    remaining_lining: float
    total_lining_waste: float | None = None   # no waste column (flagged)


class LiningLotRow(BaseModel):
    lot_id: uuid.UUID
    lining_type: str | None           # subtype (KNIT / NYLON / ...)
    article: str
    colour: str | None
    thickness: str | None
    uom: str
    available: float
    allocated: float | None = None    # reservation ledger (flagged)
    used: float
    pieces_lined: int
    remaining: float


class LiningEmployeeRow(BaseModel):
    employee_id: uuid.UUID
    name: str
    designation: str | None
    assigned_pieces: int
    assigned_today: int
    completed_today: int
    rework_today: int
    used_lining: float
    daily_target: int | None = None   # no target column (flagged)
    damage_pieces: int = 0            # unsupported
    photo: str | None = None          # no photo column (flagged)


class UpcomingPieceRow(BaseModel):
    """Section 14 — pieces cut on the leather side but not yet lining-cut, i.e.
    lining work that is coming up. Cutting status is 'DONE' (leather cut exists)
    vs 'PENDING'; lining status is 'PENDING' by definition of this list."""
    order_id: uuid.UUID
    order_number: str | None
    style_id: uuid.UUID
    style: str | None
    article: str | None
    colour: str | None
    thickness: str | None
    size: str | None
    expected_qty: int
    target_date: date | None
    cutting_status: str               # DONE | PENDING
    lining_status: str                # PENDING


class LiningDashboard(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    meta: DashboardMeta
    production_kpis: LiningProductionKPIs
    material_kpis: LiningMaterialKPIs
    current_order: CurrentOrder | None
    employees: list[LiningEmployeeRow]
    lining_lots: list[LiningLotRow]
    order_progress: list[OrderProgressRow]
    daily_production: list[DailyRow]
    upcoming: list[UpcomingPieceRow]


# ══════════════════════════════════════════════════════════════════════════
# STITCHING  (pre/post-store funnel)
# ══════════════════════════════════════════════════════════════════════════
class StitchingKPIs(BaseModel):
    overall_pieces: int               # qty ordered in scope
    assigned_pieces: int              # distinct pieces with any stitching-side event
    completed_today: int              # distinct pieces reaching FINAL_FINISH today
    overall_completed: int            # distinct pieces with a FINAL_FINISH event
    pending_today: int
    overall_pending: int
    damage_pieces: int = 0            # unsupported → 0
    rework_pieces: int
    ready_for_store: int              # cleared cut side, drawer holding, not yet line
    in_store: int                     # drawer RECEIVED/SENDED, not yet line-stitched
    ready_for_inspection: int         # FINAL_FINISH done, no FINAL_INSPECTION yet
    target_date: date | None


class StageBlock(BaseModel):
    """One post-cut stitching stage (Pasting/Fusing/Line/Shell/Final)."""
    stage: str
    label: str
    section: str                      # PRE_STORE | POST_STORE
    total_received: int               # predecessor complete (or merge-gate for line)
    assigned_pieces: int              # pool handed to the stage (== received)
    completed_pieces: int             # distinct pieces with an event at this stage
    pending_pieces: int               # received − completed
    damage_pieces: int = 0            # unsupported → 0
    rework_pieces: int
    daily_target: int | None = None   # no target column (flagged)
    daily_completed: int              # events at this stage today
    achievement_pct: float | None = None  # null while daily_target is unsupported


class StoreHandoff(BaseModel):
    ready_for_store: int              # cleared cut side / holding, not received
    in_store: int                     # drawers holding/received but not sent
    sent_to_store: int                # drawers SENDED (released to line)
    in_drawer: int                    # drawers currently holding a merged piece
    ready_for_stitching: int          # drawers SENDED with no line event yet
    store_pending: int                # cleared cut side but drawer not yet sent


class StitchingEmployeeRow(BaseModel):
    employee_id: uuid.UUID
    name: str
    designation: str | None
    stage: str
    section: str                      # PRE_STORE | POST_STORE
    assigned_pieces: int
    completed_pieces: int
    assigned_today: int
    completed_today: int
    rework_today: int
    daily_target: int | None = None   # flagged
    damage_pieces: int = 0            # unsupported
    photo: str | None = None          # flagged


class StitchingStyleStage(BaseModel):
    stage: str
    label: str
    section: str
    count: int


class StitchingCurrentStyle(BaseModel):
    style_id: uuid.UUID
    style: str
    article: str | None
    order_id: uuid.UUID
    order_number: str | None
    total_pieces: int
    stages: list[StitchingStyleStage]
    ready_for_inspection: int


class StageDailyRow(BaseModel):
    work_date: date
    stage: str
    completed: int
    events: int


class StitchingDashboard(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    meta: DashboardMeta
    kpis: StitchingKPIs
    stages: list[StageBlock]
    store_handoff: StoreHandoff
    current_style: StitchingCurrentStyle | None
    employees: list[StitchingEmployeeRow]
    daily_production: list[StageDailyRow]
    order_progress: list[OrderProgressRow]


class PieceStageHistoryRow(BaseModel):
    """Section 21 traceability — one row per real production event of a piece,
    in pipeline order, plus the derived STORE overlay row when applicable."""
    stage: str
    label: str
    employee: str | None
    work_date: date | None
    is_store_overlay: bool = False
    store_status: str | None = None


class PieceTrace(BaseModel):
    piece_code: str
    style: str | None
    order_number: str | None
    colour: str | None
    size: str | None
    display_stage: str | None
    in_store: bool
    store_label: str | None
    history: list[PieceStageHistoryRow]


# ══════════════════════════════════════════════════════════════════════════
# STORE  (drawer movement — STORE is a drawer state, not an event)
# ══════════════════════════════════════════════════════════════════════════
class StoreKPIs(BaseModel):
    total_drawers: int
    drawers_in_store: int             # not yet SENDED and holding something
    drawers_sent: int                 # state == SENDED
    empty_drawers: int                # nothing held (leather_in/lining_in both false)
    held_drawers: int                 # RECEIVED (DM confirmed, awaiting send)
    leather_drawers: int              # leather_in only
    lining_drawers: int               # lining_in only
    leather_lining_drawers: int       # both


class DrawerRow(BaseModel):
    drawer_id: uuid.UUID
    drawer_code: str
    seq: int
    state: str
    status_label: str                 # In Store | Held | Ready to Send | Sent | Empty
    contents: str                     # HOLDING LEATHER | LINING | BOTH | EMPTY
    material_type: str                # LEATHER | LINING | LEATHER+LINING | NONE
    leather_in: bool
    lining_in: bool
    piece_id: uuid.UUID | None
    piece_code: str | None
    style_id: uuid.UUID | None
    style: str | None
    order_id: uuid.UUID | None
    order_number: str | None
    colour: str | None
    size: str | None
    received_at: datetime | None
    sended_at: datetime | None
    target_date: date | None


class StoreCurrentStyleRow(BaseModel):
    style_id: uuid.UUID
    style: str
    order_id: uuid.UUID | None
    order_number: str | None
    drawers: int
    leather_drawers: int
    lining_drawers: int
    both_drawers: int
    ready_to_send: int
    target_date: date | None


class DrawerCutterRow(BaseModel):
    material_type: str                # LEATHER | LINING
    employee_id: uuid.UUID | None
    employee: str | None
    work_date: date | None


class DrawerDetail(BaseModel):
    drawer_id: uuid.UUID
    drawer_code: str
    seq: int
    state: str
    status_label: str
    contents: str
    piece_id: uuid.UUID | None
    piece_code: str | None
    style: str | None
    order_number: str | None
    colour: str | None
    size: str | None
    leather_in: bool
    lining_in: bool
    date_received: datetime | None
    date_sended: datetime | None
    created_at: datetime | None
    cutters: list[DrawerCutterRow]    # who cut the leather / lining in this drawer


class DrawerMovementRow(BaseModel):
    action: str
    at: datetime | None
    actor_user_id: uuid.UUID | None


class EmptyDrawerRow(BaseModel):
    drawer_id: uuid.UUID
    drawer_code: str
    seq: int
    last_style: str | None
    last_material: str | None
    last_sent_date: datetime | None
    availability: str                 # "Yes"


class MaterialCutterTrace(BaseModel):
    piece_code: str
    material_type: str                # LEATHER | LINING
    employee_id: uuid.UUID | None
    employee: str | None
    style: str | None
    order_number: str | None
    colour: str | None
    size: str | None
    cutting_date: date | None
    drawer_code: str | None
    photo: str | None = None          # flagged


class StoreDashboard(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    meta: DashboardMeta
    kpis: StoreKPIs
    current_styles: list[StoreCurrentStyleRow]
    drawers: list[DrawerRow]
    held_drawers: list[DrawerRow]
    empty_drawers: list[EmptyDrawerRow]
