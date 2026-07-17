"""Wages: rate table (per style x operation, effective-dated) and frozen wage runs.

WHY effective_from: when you reprint April payroll in June, you must use the rate
that was active in April — not today's rate. We pick the latest rate <= work_date.

WHY wage_run/wage_line: a closed run is a SNAPSHOT. We never recompute a closed
period, so editing an old production event can't silently rewrite past payroll.

WHY Rate has no sku_id: a rate is per STYLE x OPERATION. Every colour and size of
a style is cut and stitched at the same price — confirmed against the production
cards in johnpeter.xlsx, which price per style title. If XXL stitching ever needs
to pay more than S, this table needs a sku_id and uq_rate needs to grow with it.
"""
import uuid
from datetime import date

from sqlalchemy import (
    Date, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.enums import RunStatus
from app.core.models import GUID, TimestampMixin, UUIDMixin


class Rate(Base, UUIDMixin, TimestampMixin):
    """Rupees per piece for one operation on one style, from one date onward."""

    __tablename__ = "rate"
    __table_args__ = (
        UniqueConstraint("style_id", "operation_id", "effective_from", name="uq_rate"),
    )
    style_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("style.id"), index=True
    )
    operation_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("operation.id"), index=True
    )
    rate: Mapped[float] = mapped_column(Numeric(10, 2))  # rupees per piece
    effective_from: Mapped[date] = mapped_column(Date)


class WageRun(Base, UUIDMixin, TimestampMixin):
    """One payroll window. period_start/period_end are entered by the manager.

    A CLOSED run is immutable. compute_run refuses any window that intersects an
    existing CLOSED run — see WageRepository.overlapping_closed_run.
    """

    __tablename__ = "wage_run"
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.OPEN
    )
    lines: Mapped[list["WageLine"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class WageLine(Base, UUIDMixin):
    """One frozen payroll line for one employee in one run.

    wage_type is a SNAPSHOT of the employee's type at run time, not a foreign key
    to it: moving a worker from piece-rate to monthly next year must not rewrite
    what last year's payslip says he was.
    """

    __tablename__ = "wage_line"
    wage_run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("wage_run.id"), index=True
    )
    employee_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employee.id"), index=True
    )
    wage_type: Mapped[str] = mapped_column(String(20))
    pieces: Mapped[int] = mapped_column(Integer, default=0)  # always 0 for monthly
    amount: Mapped[float] = mapped_column(Numeric(12, 2))
    run: Mapped["WageRun"] = relationship(back_populates="lines")
