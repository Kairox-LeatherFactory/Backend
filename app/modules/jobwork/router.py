"""
================================================================================
modules/jobwork/router.py — HTTP only. Garments that leave the building.
================================================================================
POST /jobwork/vendors          register an outside factory
GET  /jobwork/vendors          the vendor list
POST /jobwork/dispatch         send garments out for a stage
POST /jobwork/{id}/receive     book them back in — logs the stage they did
GET  /jobwork                  what is out, and what is overdue

WHO. Sending garments out of the building and agreeing what is paid for them is
a DM/MD decision. Everyone else can read where things are.
================================================================================
"""
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.enums import UserRole
from app.modules.jobwork.service import JobWorkService
from app.modules.users.deps import require_roles
from app.modules.users.models import User

router = APIRouter(prefix="/jobwork", tags=["job work"])

_SENDERS = require_roles(UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER)
_READERS = require_roles(
    UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER, UserRole.HR,
    UserRole.CUTTING_MANAGER, UserRole.LINING_MANAGER,
    UserRole.STITCHING_MANAGER, UserRole.STORE_MANAGER, UserRole.SUPERVISOR)


class VendorIn(BaseModel):
    name: str
    contact: str | None = None
    note: str | None = None


class DispatchIn(BaseModel):
    vendor_id: uuid.UUID
    stage: str
    piece_ids: list[uuid.UUID] = Field(default_factory=list)
    expected_back: date | None = None
    # OPTIONAL. Some work is quoted per piece and some is settled another way;
    # demanding a number nobody has yet would mean the dispatch does not get
    # recorded at all.
    rate_per_piece: float | None = None
    currency: str | None = None
    note: str | None = None


class ReceiveIn(BaseModel):
    """Which garments came back, and which did not.

    Omit `piece_ids` and everything still out is treated as returned — the
    ordinary case. `rejected_ids` and `short_ids` are the exceptions, and they
    are kept apart because they lead to different conversations: badly-done work
    is a quality matter, a missing garment is a loss to chase. Neither is paid
    for.
    """
    piece_ids: list[uuid.UUID] | None = None
    rejected_ids: list[uuid.UUID] = Field(default_factory=list)
    short_ids: list[uuid.UUID] = Field(default_factory=list)
    work_date: date | None = None


# ── responses ────────────────────────────────────────────────────────────────
# Declared so the OpenAPI schema carries a return type and the frontend can
# generate one. NOTE FOR ANYONE EDITING THESE: `response_model` FILTERS the
# response — a field the service returns and the model does not declare is
# silently dropped on the way out, with no error anywhere. `JobWorkService.
# payload` splices caller-supplied `**extra` into its dict, so every extra key
# any caller passes has to be declared here too (`skipped`, from dispatch).
class VendorOut(BaseModel):
    vendor_id: uuid.UUID
    name: str
    contact: str | None = None
    note: str | None = None
    is_active: bool


class JobWorkOut(BaseModel):
    job_id: uuid.UUID
    vendor_id: uuid.UUID | None = None
    vendor: str | None = None
    stage: str
    status: str
    dispatched_at: datetime | None = None
    expected_back: date | None = None
    returned_at: datetime | None = None
    rate_per_piece: float | None = None
    currency: str | None = None
    pieces_out: int
    pieces_back: int
    pieces_rejected: int
    pieces_short: int
    # Derived, and only over what came back: a garment that never returned was
    # never work delivered, so it is never paid for.
    cost: float | None = None
    overdue: bool
    note: str | None = None
    # dispatch only — pieces it declined to send (already out, or not found).
    skipped: list[str] | None = None


@router.post("/vendors", status_code=status.HTTP_201_CREATED,
             response_model=VendorOut)
async def create_vendor(
    body: VendorIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_SENDERS),
):
    """Register an outside factory. Registering the same name twice returns the
    original rather than a 409 — a duplicate is not a mistake worth blocking."""
    return await JobWorkService(db).create_vendor(
        name=body.name, contact=body.contact, note=body.note)


@router.get("/vendors", response_model=list[VendorOut])
async def list_vendors(
    active_only: bool = Query(default=True),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_READERS),
):
    return await JobWorkService(db).list_vendors(active_only=active_only)


@router.post("/dispatch", status_code=status.HTTP_201_CREATED,
             response_model=JobWorkOut)
async def dispatch(
    body: DispatchIn,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_SENDERS),
):
    """Send garments out. They stop being scannable in-house until they return.

    A garment already out at another vendor is skipped and reported rather than
    silently double-booked: two open records claiming one piece would each look
    satisfied by the other's return.
    """
    return await JobWorkService(db).dispatch(
        vendor_id=body.vendor_id, stage=body.stage, piece_ids=body.piece_ids,
        expected_back=body.expected_back, rate_per_piece=body.rate_per_piece,
        currency=body.currency, note=body.note,
        actor_user_id=user.id, actor_name=user.name)


@router.post("/{job_id}/receive", response_model=JobWorkOut)
async def receive(
    job_id: uuid.UUID,
    body: ReceiveIn | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_SENDERS),
):
    """Book garments back in, and LOG the stage the vendor performed.

    The work really happened, so the garment advances and the progress counts
    include it — but it is logged against the VENDOR, not an employee, so no
    wage line is generated for work nobody on our payroll did.
    """
    b = body or ReceiveIn()
    return await JobWorkService(db).receive(
        job_id, piece_ids=b.piece_ids, rejected_ids=b.rejected_ids,
        short_ids=b.short_ids, work_date=b.work_date,
        actor_user_id=user.id, actor_name=user.name)


@router.get("", response_model=list[JobWorkOut])
async def list_jobs(
    status_filter: str | None = Query(default=None, alias="status"),
    vendor_id: uuid.UUID | None = Query(default=None),
    overdue: bool = Query(default=False),
    limit: int = Query(default=200, le=1000),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(_READERS),
):
    """What is out, what it will cost, and what is late back."""
    return await JobWorkService(db).list_jobs(
        status_filter=status_filter, vendor_id=vendor_id,
        overdue_only=overdue, limit=limit)
