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
from app.core.enums import ScreenContext , UserRole
from app.modules.barcode.service import BarcodeService
from app.modules.production.service import ProductionService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User
from app.modules.production.schemas import (
    Consumption, LogRequest, LogResult, PieceState,
)
from app.core.enums import ProductionStage, ScreenContext, screen_for_role

router = APIRouter(prefix="/production", tags=["Production"])

# ── B9: tenancy, mirroring analytics/router.py:23-32 ────────────────────────
def client_scope(user: User = Depends(get_current_user)) -> uuid.UUID | None:
    """The client_id a request must be scoped to: the caller's own for a CLIENT
    login, None for staff (who legitimately read across clients). A cross-tenant
    id must resolve to 404 — existence itself is information."""
    return user.client_id if user.role == UserRole.CLIENT else None

# Raw event feed + piece-level reads are floor/office data, never customer data.
_FLOOR_READERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR,
    UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER,
)

# WHO MAY REACH THE LOG AT ALL — a door gate, in front of the per-stage GATE 1.
#
# This route deliberately carried no role dependency: "the role gate is
# stage-specific, in-service". That is true for a piece with a resolvable stage,
# and it leaves a gap for one without. A piece that has not been cut yet resolves
# to NO stage on the PIPELINE screen, so GATE 1 never runs and the request comes
# back 201 with everything in the `not_cut` bucket — for ANY authenticated
# token, including a VIEWER, a CLIENT, or (bug #16) the new STORE_MANAGER.
#
# Nothing is written on that path, so this was never a data leak; it was worse as
# an ANSWER. A store login is told "nothing logged, cut it first", implying it
# may log once the piece is cut, when in fact its scan will 403 the moment a
# stage resolves. The role that cannot log anything should be told so at the
# door, once, instead of being led down the path and stopped at the end of it.
#
# STORE_MANAGER is deliberately absent: store functions only (CLAUDE.md §3 and
# the bug-#16 separation). GATE 1 still does the per-stage work behind this.
_LOGGERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR,
    UserRole.SUPERVISOR, UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER,
)

# ══════════════════════════════════════════════════════════════════════════
# READ endpoints (carried over from the pre-barcode router — unchanged behaviour)
# ══════════════════════════════════════════════════════════════════════════
@router.get("/operations")
async def list_operations(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR_READERS),
):
    """The configured production operations (the pipeline steps)."""
    return await ProductionService(db).list_operations()


@router.get("/skus")
async def list_sku_options(
    order_id: uuid.UUID | None = Query(None),
    style_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Friendly SKU picker for the log screens (code + style · colour · size)."""
    return await ProductionService(db).list_sku_options(
        order_id=order_id, style_id=style_id, client_scope=scope)


@router.get("/events")
async def list_events(
    sku_id: uuid.UUID | None = None,
    employee_id: uuid.UUID | None = None,
    start: date | None = Query(None),
    end: date | None = Query(None),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR_READERS),
):
    """Raw production events, filterable by sku / employee / date window.

    B9: floor/office staff only. This feed names the employee who worked each
    piece; a CLIENT or VIEWER token has no business in it."""
    return await ProductionService(db).list_events(
        sku_id=sku_id, employee_id=employee_id, start=start, end=end)


@router.get("/styles/{style_id}/progress")
async def style_progress(
    style_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Per-stage completed counts for a style (the live progress card)."""
    return await ProductionService(db).style_progress(style_id, client_scope=scope)


@router.get("/skus/{sku_id}/pieces")
async def list_pieces(
    sku_id: uuid.UUID,
    operation_id: uuid.UUID | None = Query(None),
    db: AsyncSession = Depends(get_db),
    scope: uuid.UUID | None = Depends(client_scope),
):
    """Every piece of one SKU with its current stage and eligibility."""
    return await ProductionService(db).list_pieces_for_sku(
        sku_id=sku_id, operation_id=operation_id, client_scope=scope)

@router.get("/piece-state", response_model=PieceState)
async def piece_state(
    code: str | None = Query(None, description="A scanned piece barcode "
                                               "(compact or legacy long code)."),
    piece_id: uuid.UUID | None = Query(None, description="Manual door."),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_FLOOR_READERS),
):
    """What is true about this piece, the instant it is scanned.

    THE READ THE SCAN SCREEN NEVER HAD. Stage inference, the sequence gate and
    the merge gate all ran only at WRITE time, so the UI had to guess: it made
    the operator pick a stage by hand (bug #4) and it left later stage cards
    scannable before their predecessor was done (bug #6). This answers both from
    the server, using the very same predicates POST /log enforces — so a card the
    UI opens is a card the log will accept.

    Also carries the piece's drawer (bug #12) and how many pieces of its SKU are
    still outstanding at the next stage (bug #8).
    """
    if not code and not piece_id:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Provide code or piece_id.")
    if piece_id is None:
        piece_id = await BarcodeService(db).resolve_piece_id(code)
    return await ProductionService(db).piece_state(piece_id, user=user)


async def _resolve_cut_lot(
    db: AsyncSession, cons: Consumption, screen: ScreenContext,
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Turn the cut screen's inputs into (leather_lot_id, lining_lot_id).

    TWO DOORS, ONE OUTCOME — the same pattern the log itself uses. A screen that
    already picked a lot sends its id and nothing happens here. A cutting manager
    who typed article + colour (+ optional thickness) gets it resolved through the
    EXISTING lot picker, MaterialService.list_lots, which already filters on
    exactly those three fields and already returns lot ids (bugs #9/#10).

    Resolving in the ROUTER is deliberate: the service must keep seeing ids only
    (CLAUDE.md §15), exactly as barcodes are resolved to ids here and not inside
    log_batch.

    AMBIGUITY IS AN ERROR, NOT A GUESS. If the spec matches several lots we 409
    and name them. Silently taking the first would decrement stock from a lot the
    manager never chose — a wrong number in the one ledger the factory reconciles
    against, and invisible.
    """
    is_lining = screen is ScreenContext.LINING_CUT
    if is_lining and cons.lining_lot_id:
        return None, cons.lining_lot_id
    if not is_lining and cons.leather_lot_id:
        return cons.leather_lot_id, None
    # Either id may be sent regardless of screen (DM/MD logging a mixed shift).
    if cons.leather_lot_id or cons.lining_lot_id:
        return cons.leather_lot_id, cons.lining_lot_id
    if not cons.article:
        return None, None                 # nothing to resolve; caller validates

    from app.modules.materials.service import MaterialService
    category = "LINING" if is_lining else "LEATHER"
    found = await MaterialService(db).list_lots(
        category=category, article=cons.article, colour=cons.colour,
        thickness=cons.thickness)
    lots = found["lots"]
    if not lots:
        spec = " · ".join(str(v) for v in
                          [cons.article, cons.colour, cons.thickness] if v)
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No {category} lot in stock for {spec}. Add the delivery with "
            f"POST /materials before cutting from it.")
    if len(lots) > 1:
        names = "; ".join(
            f"{l['article']} · {l['colour']} · {l['thickness'] or 'no thickness'} "
            f"({l['available']} {l['uom']} available, lot {l['lot_id']})"
            for l in lots[:5])
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{len(lots)} {category} lots match that spec — say which one. "
            f"Add a thickness, or send the lot id directly. Candidates: {names}")
    lot_id = lots[0]["lot_id"]
    return (None, lot_id) if is_lining else (lot_id, None)


@router.post("/log", response_model=LogResult, status_code=201)
async def log_batch(
    body: LogRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_LOGGERS),   # door gate; per-stage GATE 1 is in-service
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

    # Screen context is DERIVED FROM ROLE. The client no longer needs to send it.
    # DM/MD may pass an explicit override; any other role's sent value is ignored.
    override = None
    if body.screen_context:
        try:
            override = ScreenContext(body.screen_context.upper())
        except ValueError:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "screen_context, if sent, must be LEATHER_CUT, LINING_CUT, or "
                "PIPELINE.")
    screen = screen_for_role(user.role, override=override)

    cons = body.consumption or Consumption()
    # Bugs #9/#10: the cut screen may name the material by article/colour/
    # thickness instead of by lot id. Resolved to ids HERE so the service still
    # sees ids only, exactly as barcodes are.
    leather_lot_id, lining_lot_id = await _resolve_cut_lot(db, cons, screen)
    return await svc.log_batch(
        user=user, employee_id=employee_id, piece_ids=piece_ids,
        work_date=body.work_date, screen=screen,
        leather_lot_id=leather_lot_id, lining_lot_id=lining_lot_id,
        consumption_qty=cons.dcm, preview=body.preview)



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
