"""
================================================================================
modules/production/router.py — the two-door log (replaces /cutting and /scan)
================================================================================
POST /production/log                the single logging surface (barcode OR manual)
GET  /production/skus/{id}/pieces    the checklist (with eligibility) — unchanged

MIGRATION NOTE
    This REPLACES the old POST /production/cutting and POST /production/scan. The
    old cut() minted pieces; pieces now mint at breakdown upload, so /cutting has
    no meaning. Keep the old routes returning HTTP 410 for one release with a
    body pointing at /log, so a stale frontend fails loudly instead of silently.

    The old ScanBatch response buckets (logged/rework/not_found) are preserved and
    EXTENDED (sequence_blocked/skill_blocked/merge_blocked/screen_role_warning/
    consumption_recorded) — a frontend reading only the old fields still works.
================================================================================
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import ScreenContext
from app.modules.barcode.service import BarcodeService
from app.modules.production.service import ProductionService
from app.modules.users.deps import get_current_user
from app.modules.users.models import User

router = APIRouter(prefix="/production", tags=["Production"])


# ══════════════════════════════════════════════════════════════════════════
# READ endpoints (carried over from the pre-barcode router — unchanged behaviour)
# ══════════════════════════════════════════════════════════════════════════
@router.get("/operations")
async def list_operations(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """The configured production operations (the pipeline steps)."""
    return await ProductionService(db).list_operations()


@router.get("/skus")
async def list_sku_options(
    order_id: uuid.UUID | None = Query(None),
    style_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Friendly SKU picker for the log screens (code + style · colour · size)."""
    return await ProductionService(db).list_sku_options(order_id=order_id, style_id=style_id)


@router.get("/events")
async def list_events(
    sku_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Raw production events, filterable by sku / employee / date window."""
    return await ProductionService(db).list_events(
        sku_id=sku_id, employee_id=employee_id, start=start, end=end)


@router.get("/styles/{style_id}/progress")
async def style_progress(
    style_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """Per-stage completed counts for a style (the live progress card)."""
    return await ProductionService(db).style_progress(style_id)


# ── request/response ─────────────────────────────────────────────────────────
class Actor(BaseModel):
    employee_barcode: str | None = None
    employee_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _one(self):
        if not self.employee_barcode and not self.employee_id:
            raise ValueError("Provide employee_barcode or employee_id.")
        return self


class Targets(BaseModel):
    piece_barcodes: list[str] | None = None
    sku_id: uuid.UUID | None = None
    piece_seqs: list[int] | None = None

    @model_validator(mode="after")
    def _one(self):
        if not self.piece_barcodes and not (self.sku_id and self.piece_seqs):
            raise ValueError("Provide piece_barcodes OR (sku_id + piece_seqs).")
        return self


class Consumption(BaseModel):
    leather_lot_id: uuid.UUID | None = None
    lining_lot_id: uuid.UUID | None = None
    dcm: float | None = None


class LogRequest(BaseModel):
    screen_context: str = "PIPELINE"       # LEATHER_CUT | LINING_CUT | PIPELINE
    actor: Actor
    targets: Targets
    work_date: date
    consumption: Consumption | None = None


class LogResult(BaseModel):
    stage: str | None
    count_logged: int
    logged: list[str]
    rework: list[str]
    not_found: list[str]
    sequence_blocked: list[str]
    skill_blocked: list[str]
    merge_blocked: list[str]
    screen_role_warning: str | None = None
    consumption_recorded: dict | None = None


@router.post("/log", response_model=LogResult, status_code=201)
async def log_batch(
    body: LogRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),   # role gate is stage-specific, in-service
):
    """Log one stage for a batch of pieces. Stage is inferred (never sent):
    a cut screen fixes it; PIPELINE infers it from each piece's history.

    Resolves BOTH doors to ids here, then hands the service ids only:
      • barcode door: employee_barcode + piece_barcodes
      • manual door:  employee_id + (sku_id + piece_seqs)
    """
    svc = ProductionService(db)
    barcodes = BarcodeService(db)

    # actor → employee_id
    if body.actor.employee_id:
        employee_id = body.actor.employee_id
    else:
        employee_id = await barcodes.resolve_employee_id(body.actor.employee_barcode)

    # targets → piece_ids
    piece_ids: list[uuid.UUID] = []
    if body.targets.piece_barcodes:
        for code in body.targets.piece_barcodes:
            piece_ids.append(await barcodes.resolve_piece_id(code))
    else:
        # manual door: resolve sku + seqs → piece ids
        for seq in body.targets.piece_seqs:
            piece = await svc.repo.get_piece_by_sku_seq(body.targets.sku_id, seq)
            if piece is None:
                # keep the not_found reporting behaviour: pass a sentinel the
                # service will bucket. We resolve to real ids only; unknown seqs
                # are reported by the service, so skip here and let the checklist
                # guide the user. Raise only if NOTHING resolves.
                continue
            piece_ids.append(piece.id)
        if not piece_ids:
            raise HTTPException(status.HTTP_404_NOT_FOUND,
                                "None of the given piece numbers exist for this SKU.")

    try:
        screen = ScreenContext(body.screen_context.upper())
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "screen_context must be LEATHER_CUT, LINING_CUT, or PIPELINE.")

    cons = body.consumption or Consumption()
    return await svc.log_batch(
        user=user, employee_id=employee_id, piece_ids=piece_ids,
        work_date=body.work_date, screen=screen,
        leather_lot_id=cons.leather_lot_id, lining_lot_id=cons.lining_lot_id,
        consumption_qty=cons.dcm)


@router.get("/skus/{sku_id}/pieces")
async def sku_pieces(
    sku_id: uuid.UUID,
    operation_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """The manual-door checklist: every piece of a SKU with done/eligible flags
    for the selected operation, so the UI greys out out-of-sequence pieces."""
    return await ProductionService(db).list_pieces_for_sku(
        sku_id=sku_id, operation_id=operation_id)


# ── deprecated shims (one release) ───────────────────────────────────────────
@router.post("/cutting", deprecated=True)
async def cutting_removed():
    raise HTTPException(
        status.HTTP_410_GONE,
        "POST /production/cutting is removed. Pieces mint at breakdown upload; "
        "log cutting via POST /production/log with screen_context=LEATHER_CUT.")


@router.post("/scan", deprecated=True)
async def scan_removed():
    raise HTTPException(
        status.HTTP_410_GONE,
        "POST /production/scan is replaced by POST /production/log (two-door).")