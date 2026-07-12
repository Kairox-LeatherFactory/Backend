"""
================================================================================
modules/production/service.py — Production logic + role->operation access (async)
================================================================================

CORE FLOW (per-piece)
    cut()   mints N pieces for a SKU at the CUTTING stage (N Piece rows + N
            CUTTING events, qty=1), one transaction, and returns their codes to
            print on traveler cards.
    scan()  takes a BATCH of manually-typed piece codes for one operation /
            worker / day. Existing pieces get one qty=1 event each and their
            current stage advances; unknown codes are reported, never written
            (a typo can't corrupt the ledger); a piece already seen at this op is
            still logged and flagged as REWORK (permissive — pieces can loop
            back; we surface, we don't reject). One commit for the whole batch.

    Role->operation access is enforced (direct manager bypasses). We deliberately
    do NOT compare qty against a previous stage — under per-piece the only
    integrity rule is "the piece must exist", handled in scan().

Cross-module access goes through ClientService / EmployeeService, never their
repositories, preserving module boundaries.
================================================================================
"""
import re
import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.modules.clients.service import ClientService
from app.modules.employees.service import EmployeeService
from app.modules.production.models import Operation, Piece, ProductionEvent
from app.modules.production.repository import ProductionRepository
from app.modules.users.models import User

CUTTING_CODE = "CUTTING"


def _norm(code: str) -> str:
    """Normalise a manually-typed code so lookups always match what we stored."""
    return (code or "").strip().upper()


def _slug(s: str | None) -> str:
    """Compact, typeable token for a code segment (alnum, upper, no spaces)."""
    return re.sub(r"[^A-Za-z0-9]", "", (s or "")).upper() or "NA"


class ProductionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ProductionRepository(db)
        self.clients = ClientService(db)
        self.employees = EmployeeService(db)

    async def list_operations(self) -> list[Operation]:
        return await self.repo.list_operations()

    # ------------------------------------------------------------------ helpers
    async def _assert_can_log(self, user: User, op: Operation) -> None:
        """Role->operation access. Direct manager bypasses."""
        if user.role != UserRole.DIRECT_MANAGER:
            allowed = await self.repo.operations_for_role(user.role.value)
            if op.id not in allowed:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Your role may not log operation '{op.code}'",
                )

    async def _assert_present(self, employee_id: uuid.UUID, work_date: date) -> None:
        """A worker who hasn't checked in TODAY cannot have produced TODAY.
        Historical events are left alone."""
        if work_date == date.today():
            from app.modules.attendance.service import AttendanceService
            if not await AttendanceService(self.db).is_present_today(employee_id):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Employee is not checked in today — cannot log production. "
                    "Mark attendance first.",
                )

    # -------------------------------------------------------------------- cut
    async def cut(self, *, user: User, sku_id: uuid.UUID, employee_id: uuid.UUID,
                  work_date: date, count: int) -> list[Piece]:
        """Mint `count` pieces for a SKU at the CUTTING stage."""
        if count < 1:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "count must be >= 1")

        ctx = await self.clients.sku_label_context(sku_id)
        if not ctx:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")
        if not await self.employees.get(employee_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")

        op = await self.repo.get_operation_by_code(CUTTING_CODE)
        if not op:
            raise HTTPException(
                status.HTTP_500_INTERNAL_SERVER_ERROR,
                f"No '{CUTTING_CODE}' operation configured",
            )
        await self._assert_can_log(user, op)
        await self._assert_present(employee_id, work_date)

        prefix = "-".join((
            _slug(ctx["order_number"]),
            _slug(ctx["color_code"]),
            _slug(ctx["size"]),
        ))
        return await self.repo.mint_pieces(
            sku_id=sku_id, operation=op, employee_id=employee_id,
            work_date=work_date, count=count, entered_by=user.name, prefix=prefix,
        )

    # ------------------------------------------------------------------- scan
    async def scan(self, *, user: User, operation_id: uuid.UUID,
                   employee_id: uuid.UUID, work_date: date,
                   piece_codes: list[str]) -> dict:
        """Log a batch of typed piece codes at one operation. Partial-accept."""
        op = await self.repo.get_operation(operation_id)
        if not op:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")
        if not await self.employees.get(employee_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
        await self._assert_can_log(user, op)
        await self._assert_present(employee_id, work_date)

        logged: list[str] = []
        rework: list[str] = []
        not_found: list[str] = []
        seen: set[str] = set()

        for raw in piece_codes:
            code = _norm(raw)
            if not code or code in seen:
                continue
            seen.add(code)
            piece = await self.repo.get_piece_by_code(code)
            if not piece:
                not_found.append(raw)
                continue
            if await self.repo.has_event_at_op(piece.id, op.id):
                rework.append(piece.code)
            self.repo.stage_piece_nocommit(
                piece=piece, operation_id=op.id, employee_id=employee_id,
                work_date=work_date, entered_by=user.name,
            )
            logged.append(piece.code)

        await self.repo.commit()
        return {
            "operation": op.code,
            "count_logged": len(logged),
            "logged": logged,
            "rework": rework,
            "not_found": not_found,
        }

    # --------------------------------------------------- legacy generic event
    async def log_event(self, user: User, sku_id: uuid.UUID, operation_id: uuid.UUID,
                        employee_id: uuid.UUID, work_date: date, qty: int,
                        bundle_ref: str | None = None) -> ProductionEvent:
        """Back-compat qty-based entry (no piece linkage). Prefer cut()/scan()."""
        if not await self.clients.get_sku(sku_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")
        if not await self.employees.get(employee_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
        op = await self.repo.get_operation(operation_id)
        if not op:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")
        await self._assert_can_log(user, op)
        await self._assert_present(employee_id, work_date)
        return await self.repo.add_event(
            sku_id=sku_id, operation_id=operation_id, employee_id=employee_id,
            work_date=work_date, qty=qty, bundle_ref=bundle_ref,
            entered_by=user.name,
        )

    async def list_events(self, **filters) -> list[ProductionEvent]:
        return await self.repo.list_events(**filters)

    async def style_progress(self, style_id: uuid.UUID) -> dict[str, int]:
        return await self.repo.stage_totals_for_style(style_id)

    async def list_sku_options(self, *, order_id: uuid.UUID | None = None,
                               style_id: uuid.UUID | None = None) -> list[dict]:
        """Human-friendly SKU picker for the log screens (never shows a UUID)."""
        return await self.clients.list_sku_options(order_id=order_id, style_id=style_id)

    # Public interface for the wages module:
    async def piece_counts(self, start: date, end: date):
        return await self.repo.piece_counts_by_employee_style_op(start, end)