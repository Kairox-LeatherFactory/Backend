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

# Releasing a batch of garments into the next stage is a store decision.
#
# WIDENED (change-list item 11): the STITCHING_MANAGER is now a sender.
# THIS IS A DELIBERATE SCOPE CHANGE, NOT A TIDY-UP — record the reasoning.
#   • Sending is what releases a batch into LINE_STITCHING. The person waiting on
#     that batch is the stitching manager, and on the floor they are the one who
#     walks to the store and takes the drawers. Requiring a DM/MD/store login to
#     press the button made the store a bottleneck on a decision the stitching
#     manager was already making physically.
#   • It is a ROLE GRANT on the store surface, NOT a production permission. The
#     stitching manager still cannot log a stage they do not own: STAGE_ROLE_ACCESS
#     is untouched, and STORE_MANAGER remains absent from it, so store access and
#     stage access stay two separate questions.
#   • CUTTING/LINING managers are still excluded. They put parts IN; they do not
#     decide what leaves.
_SENDERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
    UserRole.STORE_MANAGER, UserRole.STITCHING_MANAGER)


@router.get("", response_model=schemas.DrawerLabelPage)
async def list_drawers(
    state: str | None = Query(None, description="Filter by DrawerState, e.g. waiting"),
    code: str | None = Query(
        None, min_length=1, max_length=40,
        description="Search by drawer code — case-insensitive CONTAINS, so '42' "
                    "and 'drw-004' both work."),
    seq_from: int | None = Query(None, ge=1, description="Print a range: first seq."),
    seq_to: int | None = Query(None, ge=1, description="Print a range: last seq."),
    has_piece: bool | None = Query(None, description="Only drawers holding a garment."),
    sendable: bool | None = Query(
        None, description="Only drawers ready to send — the send queue."),
    sort: str = Query(
        "seq", pattern="^(seq|recent)$",
        description="seq = drawer order, for printing labels and finding a "
                    "drawer in the rack (default). recent = most recently "
                    "acted-on first, for the store screen's latest-drawers view."),
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

    `sendable=true` is the send queue: drawers that hold everything their garment
    needs, waiting for someone to tick and send them.

    `code=` is the store screen's search box (change-list item 6): the production
    view shows the 10 most recent drawers (`?sort=recent&limit=10`) and
    everything else is reached by typing a code, rather than paging 430 rows.
    `sort=recent` orders by the newest of sended_at / received_at / created_at —
    "latest" has to mean most recently WORKED ON, because a bootstrapped pool
    shares one creation timestamp and would otherwise return the same arbitrary
    ten rows forever.

    `needs_lining` on every row is the EFFECTIVE requirement, resolved from the
    style/SKU/cut history — NOT the stored `piece.needs_lining` flag, which is
    written once at upload and is wrong for most of a live order. `can_send`
    applies the same rule POST /drawers/send enforces, so the queue can never
    offer a row the server refuses.

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
        state=state, code=code, seq_from=seq_from, seq_to=seq_to,
        has_piece=has_piece, sendable=sendable, sort=sort,
        limit=limit, offset=offset)


# ── THE DRAWER POOL (change-list item 9) ─────────────────────────────────────
# Declared before /{drawer_id} — "pool" would otherwise be parsed as a UUID.
@router.get("/pool")
async def drawer_pool(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR),
):
    """Pool size, free drawers, and how many minted pieces have NO drawer.

    The pool is FIXED at 200 and only grows when a DM/MD says so. A release that
    outruns it mints the pieces anyway — they keep their barcodes — but leaves
    them unmerged. Those are `pieces_waiting_for_drawer`, and until they get a
    drawer they cannot be stored and therefore cannot pass the merge gate.

    `shortfall` is what a DM would have to add right now to clear the list.
    """
    from app.modules.imports.premint import drawer_pool_status
    from app.core.database import SessionLocal
    from starlette.concurrency import run_in_threadpool

    def _read() -> dict:
        s = SessionLocal()
        try:
            return drawer_pool_status(s)
        finally:
            s.close()
    return await run_in_threadpool(_read)


@router.post("/pool", status_code=201)
async def grow_pool(
    body: schemas.DrawerPoolGrow,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_roles(
        UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER)),
):
    """Add N NEW PERMANENT barcoded drawers, then drain the waiting list into them.

    DM/MD ONLY, and it is one-way: the pool never shrinks. Each new drawer is
    permanent from this moment and recycles like any other once its piece ships.

    Print the returned codes — a drawer with no printed label cannot be scanned,
    so it is a drawer that does not exist as far as the floor is concerned.
    """
    from app.modules.imports.premint import (allocate_waiting_pieces,
                                             drawer_pool_status,
                                             grow_drawer_pool)
    from app.core.database import SessionLocal
    from starlette.concurrency import run_in_threadpool

    def _grow() -> dict:
        s = SessionLocal()
        try:
            grown = grow_drawer_pool(s, body.add)
            # Growing the pool with pieces still waiting and NOT merging them
            # would leave the DM staring at empty drawers beside a waiting list.
            drained = allocate_waiting_pieces(s)
            s.commit()
            return {**grown, **drained, "status": drawer_pool_status(s)}
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    result = await run_in_threadpool(_grow)
    result["by"] = user.name
    return result


@router.post("/allocate-waiting")
async def allocate_waiting(
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_SENDERS),
):
    """Merge drawer-less pieces into whatever drawers are currently free.

    The waiting list drains itself as garments ship (PACKAGE_EXPORT recycles a
    drawer to WAITING), but nothing merges the next waiting piece into it
    automatically. This is that step — safe to call repeatedly and a no-op when
    there is nothing to place.
    """
    from app.modules.imports.premint import (allocate_waiting_pieces,
                                             drawer_pool_status)
    from app.core.database import SessionLocal
    from starlette.concurrency import run_in_threadpool

    def _run() -> dict:
        s = SessionLocal()
        try:
            out = allocate_waiting_pieces(s)
            s.commit()
            return {**out, "status": drawer_pool_status(s)}
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
    return await run_in_threadpool(_run)


@router.get("/by-code/{code}", response_model=schemas.DrawerDetail)
async def drawer_by_code(
    code: str,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(_FLOOR),
):
    """One drawer by its printed/scanned code — the store search box's target.

    Same body as GET /drawers/{drawer_id}, so the row the search returns opens
    exactly like a row clicked in the list (change-list item 6: the Send button
    lives inside the drawer as well as outside it)."""
    drawer = await DrawerService(db).get_by_code(code)
    if drawer is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"No drawer with code '{code}'.")
    return await DrawerService(db).drawer_detail(drawer.id)


@router.post("/send", response_model=schemas.DrawerSendResult)
async def send_drawers(
    body: schemas.DrawerSendRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_SENDERS),
):
    """Send one or many drawers — and the pieces in them — onward.

    Sets each drawer to SENDED, which is exactly what the production merge gate
    reads: the whole selected bunch of pieces becomes eligible for LINE_STITCHING
    in one action, and then follows the chain on to shell stitching and final
    finish.

    THERE IS NO DESTINATION TO PICK. Lining is upstream of the store — the lining
    part is cut and then scanned INTO the drawer — so a merged drawer has exactly
    one way forward. Send the ids and nothing else.

    PARTIAL ACCEPT: a drawer that is not yet RECEIVED comes back in `not_ready`
    with the reason; the rest are still sent. Check `count_sent`, not the HTTP
    status, to know how many moved.

    NOTE the route is declared before `/{drawer_id}` on purpose — otherwise
    "send" would be parsed as a drawer id and 422 on the UUID conversion.
    """
    return await DrawerService(db).send_batch(
        drawer_ids=body.drawer_ids, actor_id=user.id)


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
    employee_id = await barcodes.resolve_actor(
        employee_barcode=body.employee_barcode, employee_id=body.employee_id)

    employee = await db.get(Employee, employee_id)
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
    user: User = Depends(_SENDERS),
):
    """DEPRECATED — kept working for one release so a frontend mid-deploy does not
    break (the same courtesy /production/cutting was given).

    RECEIVED is now reached automatically the moment a drawer holds everything its
    garment needs, and SENDED is done in bulk through `POST /drawers/send`. Move
    to those two; this single-drawer route will be removed.
    """
    return await DrawerService(db).transition(
        drawer_id, body.transition, actor_id=user.id)
