"""
================================================================================
modules/production/service.py — Production logic + role->operation access (async)
================================================================================

CORE RULE (unchanged from the original design, now async)
    log_event() validates the referenced SKU/employee/operation exist, enforces
    that the current user's ROLE is permitted to log that operation (the direct
    manager bypasses), then records the event. It deliberately does NOT check the
    quantity against the previous stage — pieces don't conserve across operations
    (Cutting 152, Pasting 155 is normal); the spread is a metric we surface in
    analytics, never an error we reject here.

Cross-module access goes through ClientService / EmployeeService, never their
repositories, preserving module boundaries.
================================================================================
"""
import uuid
from datetime import date

from fastapi import HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import UserRole
from app.modules.clients.service import ClientService
from app.modules.employees.service import EmployeeService
from app.modules.production.models import Operation, ProductionEvent
from app.modules.production.repository import ProductionRepository
from app.modules.users.models import User


class ProductionService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ProductionRepository(db)
        self.clients = ClientService(db)
        self.employees = EmployeeService(db)

    async def list_operations(self) -> list[Operation]:
        return await self.repo.list_operations()

    async def log_event(self, user: User, sku_id: uuid.UUID, operation_id: uuid.UUID,
                        employee_id: uuid.UUID, work_date: date, qty: int,
                        bundle_ref: str | None = None) -> ProductionEvent:
        # 1. Validate referenced entities exist (no orphan events).
        if not await self.clients.get_sku(sku_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "SKU not found")
        if not await self.employees.get(employee_id):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Employee not found")
        op = await self.repo.get_operation(operation_id)
        if not op:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Operation not found")

        # 2. Enforce role->operation access. Direct manager bypasses.
        if user.role != UserRole.DIRECT_MANAGER:
            allowed = await self.repo.operations_for_role(user.role.value)
            if operation_id not in allowed:
                raise HTTPException(
                    status.HTTP_403_FORBIDDEN,
                    f"Your role may not log operation '{op.code}'",
                )

        # 3. Attendance cross-check (TODAY only — historical events left alone).
        # A worker who hasn't checked in TODAY cannot have produced TODAY. This
        # blocks fraud and data-entry mistakes without touching past records.
        if work_date == date.today():
            from app.modules.attendance.service import AttendanceService
            if not await AttendanceService(self.db).is_present_today(employee_id):
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Employee is not checked in today — cannot log production. "
                    "Mark attendance first.",
                )

        # 4. Record. No cross-stage quantity check by design.
        return await self.repo.add_event(
            sku_id=sku_id, operation_id=operation_id, employee_id=employee_id,
            work_date=work_date, qty=qty, bundle_ref=bundle_ref,
            entered_by=user.name,
        )

    async def list_events(self, **filters) -> list[ProductionEvent]:
        return await self.repo.list_events(**filters)

    async def style_progress(self, style_id: uuid.UUID) -> dict[str, int]:
        return await self.repo.stage_totals_for_style(style_id)

    # Public interface for the wages module:
    async def piece_counts(self, start: date, end: date):
        return await self.repo.piece_counts_by_employee_style_op(start, end)
