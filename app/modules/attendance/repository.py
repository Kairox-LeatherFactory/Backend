"""Async data access for attendance + the shift-config singleton."""
import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.attendance.models import AttendanceLog, ShiftConfig


class AttendanceRepository:
    def __init__(self, db: AsyncSession):
        self.db = db

    # ── ShiftConfig (singleton) ──────────────────────────────────────────
    async def get_config(self) -> ShiftConfig:
        cfg = (await self.db.execute(select(ShiftConfig).limit(1))).scalar_one_or_none()
        if cfg is None:
            cfg = ShiftConfig()                # create with defaults on first read
            self.db.add(cfg)
            await self.db.commit()
            await self.db.refresh(cfg)
        return cfg

    async def save(self, obj) -> None:
        await self.db.commit()
        await self.db.refresh(obj)

    # ── Attendance log ───────────────────────────────────────────────────
    async def find(self, employee_id: uuid.UUID, work_date: date) -> AttendanceLog | None:
        res = await self.db.execute(select(AttendanceLog).where(
            AttendanceLog.employee_id == employee_id,
            AttendanceLog.work_date == work_date))
        return res.scalar_one_or_none()

    async def add(self, log: AttendanceLog) -> AttendanceLog:
        self.db.add(log)
        await self.db.commit()
        await self.db.refresh(log)
        return log

    async def for_employee(self, employee_id: uuid.UUID,
                           start: date, end: date) -> list[AttendanceLog]:
        res = await self.db.execute(
            select(AttendanceLog).where(
                AttendanceLog.employee_id == employee_id,
                AttendanceLog.work_date >= start,
                AttendanceLog.work_date <= end,
            ).order_by(AttendanceLog.work_date)
        )
        return list(res.scalars())

    async def by_day(self, work_date: date) -> list[AttendanceLog]:
        res = await self.db.execute(
            select(AttendanceLog).where(AttendanceLog.work_date == work_date))
        return list(res.scalars())
