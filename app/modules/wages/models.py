"""Wages: rate table (per style x operation, effective-dated) and frozen wage runs.

WHY effective_from: when you reprint April payroll in June, you must use the rate
that was active in April — not today's rate. We pick the latest rate <= work_date.

WHY wage_run/wage_line: a closed run is a SNAPSHOT. We never recompute a closed
period, so editing an old production event can't silently rewrite past payroll.
"""
import uuid
from datetime import date
from sqlalchemy import String, Numeric, ForeignKey, Date, Integer, Enum, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.core.database import Base
from app.core.enums import RunStatus
from app.core.models import UUIDMixin, TimestampMixin, GUID


class Rate(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "rate"
    __table_args__ = (
        UniqueConstraint("style_id", "operation_id", "effective_from", name="uq_rate"),
    )
    style_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("style.id"), index=True)
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"), index=True)
    rate: Mapped[float] = mapped_column(Numeric(10, 2))     # rupees per piece
    effective_from: Mapped[date] = mapped_column(Date)



class WageRun(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "wage_run"
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.OPEN
    )
    lines: Mapped[list["WageLine"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class WageLine(Base, UUIDMixin):
    """One frozen payroll line for one employee in one run."""
    __tablename__ = "wage_line"
    wage_run_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("wage_run.id"), index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employee.id"), index=True)
    wage_type: Mapped[str] = mapped_column(String(20))      # snapshot of type at run time
    pieces: Mapped[int] = mapped_column(Integer, default=0)  # 0 for monthly staff
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    run: Mapped["WageRun"] = relationship(back_populates="lines")