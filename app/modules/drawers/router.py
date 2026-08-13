"""
================================================================================
modules/drawers/router.py — Drawers, the Drawers List & the merge gate (async)
================================================================================
GET  /drawers                    the Drawers List: every drawer + barcode + piece
GET  /drawers/{id}               open one drawer: contents, garment, can it send
POST /drawers/store-scan         scan employee → drawer → piece; part is inferred
POST /drawers/send               send MANY drawers to LINING / STITCHING
POST /drawers/{id}/receive       DEPRECATED — completeness now auto-receives

THE FLOW THIS ROUTER NOW SERVES (bugs #13/#14/#15/#18)
    scan a part in  →  drawer auto-reaches RECEIVED once it holds everything the
    garment needs  →  the store manager opens the Drawers List, ticks the drawers
    that are ready, and sends them as ONE batch. Sending to STITCHING is what
    releases that whole bunch of pieces into line-stitching.

    The old rhythm — press RECEIVED on a drawer, then press SEND on the same
    drawer, one at a time — is gone. RECEIVED asserted a fact the server had
    already computed, so it is automatic; SEND is a decision, so it stayed, and
    it became plural because the store never releases one drawer at a time.

store-scan resolves the barcode door (employee/drawer/piece barcodes) or the
manual door (ids) to ids, then delegates.
================================================================================
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import DrawerPart, DrawerState, UserRole
from app.modules.barcode.service import BarcodeService
from app.modules.employees.models import Employee
from app.modules.drawers import schemas
from app.modules.drawers.service import DrawerService
from app.modules.users.deps import get_current_user, require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/drawers", tags=["Drawers"])

# STORE_MANAGER (bug #16) belongs here and only here: the store hub is its whole
# job. It is deliberately absent from ROLE_TO_SCREEN and STAGE_ROLE_ACCESS, so
# the same login cannot log production stages.
_FLOOR = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
    UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER, UserRole.SUPERVISOR,
    UserRole.STORE_MANAGER)

# Releasing a batch of garments into the next stage is a store decision — the
# store manager and the DM/MD make it. A cutting or stitching manager may read
# the list and scan parts in, but must not decide what leaves the store.
_SENDERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
    UserRole.STORE_MANAGER)


@router.get("", response_model=schemas.DrawerLabelPage)
async def list_drawers(
    state: str | None = Query(None, description="Filter by DrawerState, e.g. waiting"),
    seq_from: int | None = Query(None, ge=1, description="Print a range: first seq."),
    seq_to: int | None = Query(None, ge=1, description="Print a range: last seq."),
    has_piece: bool | None = Query(None, description="Only drawers holding a garment."),
    sendable: bool | None = Query(
        None, description="Only drawers ready to send (state=received) — the send queue."),
    limit: int = Query(500, ge=1, le=2000),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR),
):
    """The Drawers List (bug #13): every drawer, its scannable barcode, what it
    is holding and which garment is inside it.

    Ordered by `seq`, so DRW-0001…DRW-0200 come out in drawer order. `limit`
    defaults to 500 (a 200-drawer pool lists in one call); page with `offset`
    beyond that. `total` counts the filter, not the page.

    `sendable=true` is the send queue: drawers that reached RECEIVED on their own
    once both parts were scanned in, waiting for someone to tick and send them.

    Encode `barcode` on a label. A row with `barcode: null` is a drawer with no
    registry code — it cannot be scanned, so print nothing for it and re-run
    `python -m scripts.gen_drawer_barcodes` to mint the missing code."""
    if state is not None:
        valid = {s.value for s in DrawerState}
        if state not in valid:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                f"state must be one of {sorted(valid)}.")
    if seq_from is not None and seq_to is not None and seq_from > seq_to:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "seq_from must not exceed seq_to.")
    return await DrawerService(db).list_labels(
        state=state, seq_from=seq_from, seq_to=seq_to, has_piece=has_piece,
        sendable=sendable, limit=limit, offset=offset)


@router.post("/send", response_model=schemas.DrawerSendResult)
async def send_drawers(
    body: schemas.DrawerSendRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_SENDERS),
):
    """Send one or many drawers — and the pieces in them — onward (bugs #13/#14).

    `destination=STITCHING` sets each drawer to SENDED, which is exactly what the
    production merge gate reads: the whole selected bunch of pieces becomes
    eligible for LINE_STITCHING in one action. `destination=LINING` records the
    routing and leaves that gate shut.

    PARTIAL ACCEPT: a drawer that is not yet RECEIVED comes back in `not_ready`
    with the reason; the rest are still sent. Check `count_sent`, not the HTTP
    status, to know how many moved.

    NOTE the route is declared before `/{drawer_id}` on purpose — otherwise
    "send" would be parsed as a drawer id and 422 on the UUID conversion.
    """
    return await DrawerService(db).send_batch(
        drawer_ids=body.drawer_ids, destination=body.destination,
        actor_id=user.id)


@router.get("/{drawer_id}", response_model=schemas.DrawerDetail)
async def drawer_detail(
    drawer_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR),
):
    """Open one drawer from the list (bug #13): what it holds, which garment is
    inside, what it is still waiting for, and whether it can be sent."""
    return await DrawerService(db).drawer_detail(drawer_id)


@router.post("/store-scan", response_model=schemas.StoreScanResult)
async def store_scan(
    body: schemas.StoreScanRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_FLOOR),
):
    """Record a leather or lining part arriving in its drawer.

    THE ORDER IS EMPLOYEE → DRAWER → PIECE. The employee scan is mandatory (bug
    #2) and enforced here, not just in the UI. The drawer comes before the piece
    because the merge map is the authority — a piece scanned into the wrong
    drawer is a 409, not a re-assignment.

    `part` is optional (bug #18): the system decides LEATHER vs LINING from the
    piece's own cut history and what the drawer is still missing. Send it only to
    override that.

    H2: floor staff only. This write feeds the merge gate that releases a piece
    into line-stitching — CLIENT and VIEWER tokens must not reach it."""
    barcodes = BarcodeService(db)
    drawers = DrawerService(db)

    # Employee FIRST — resolving it before anything else means an unknown or
    # retired card fails the request before any drawer state is touched.
    #
    # resolve_employee_id already 404s an unknown code and 410s a retired one, but
    # it answers from the BARCODE REGISTRY: it proves the label is known, not that
    # the worker it names still exists. A registry row whose employee row was
    # removed would sail through here and fail much later, as a foreign-key error
    # with no useful message. So the row itself is checked, once, up front.
    if body.employee_id:
        employee_id = body.employee_id
    else:
        employee_id = await barcodes.resolve_employee_id(body.employee_barcode)

    employee = await db.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"No employee record for this card ({employee_id}). The barcode is "
            f"registered but the worker it names no longer exists — reissue the "
            f"card, or scan a different one.")
    if not employee.is_active:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{employee.name} is not an active employee, so work cannot be "
            f"recorded against them.")

    if body.drawer_id:
        drawer_id = body.drawer_id
    elif body.drawer_barcode:
        drawer_id = await barcodes.resolve_drawer_id(body.drawer_barcode)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Provide drawer_barcode or drawer_id.")

    if body.piece_id:
        piece_id = body.piece_id
    elif body.piece_barcode:
        piece_id = await barcodes.resolve_piece_id(body.piece_barcode)
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            "Provide piece_barcode or piece_id.")

    return await drawers.store_scan(
        drawer_id=drawer_id, piece_id=piece_id,
        part=DrawerPart(body.part) if body.part else None,
        # actor = the LOGIN (app_user); employee = the WORKER whose card was
        # scanned. Passing the worker as the actor is what wrote an employee id
        # into audit_log.actor_user_id and broke this endpoint — see store_scan.
        actor_id=user.id,
        employee_id=employee_id)


@router.post("/{drawer_id}/receive", response_model=schemas.DrawerTransitionResult,
             deprecated=True)
async def transition(
    drawer_id: uuid.UUID,
    body: schemas.DrawerTransition,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.DIRECT_MANAGER, UserRole.MANAGING_DIRECTOR,
        UserRole.STORE_MANAGER)),
):
    """DEPRECATED — kept working for one release so a frontend mid-deploy does not
    break (the same courtesy /production/cutting was given).

    RECEIVED is now reached automatically the moment a drawer holds everything its
    garment needs, and SENDED is done in bulk through `POST /drawers/send`. Move
    to those two; this single-drawer route will be removed.
    """
    return await DrawerService(db).transition(
        drawer_id, body.transition, actor_id=user.id)
