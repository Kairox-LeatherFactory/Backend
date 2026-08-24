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
from datetime import date, datetime
from sqlalchemy import DateTime

from sqlalchemy import (
    JSON, Boolean, Date, Enum, ForeignKey, Integer, Numeric, String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.core.enums import RunStatus, WageRunKind
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
    """One payroll window.

    OPEN = DRAFT (recompute freely), CLOSED = FROZEN (the document paid against).
    See RunStatus in core/enums.py for why rewriting a CLOSED run requires an
    explicit REOPEN rather than happening in place.
    """

    __tablename__ = "wage_run"
    period_start: Mapped[date] = mapped_column(Date, index=True)
    period_end: Mapped[date] = mapped_column(Date, index=True)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus, name="run_status"), default=RunStatus.OPEN
    )
    # ── SCOPE (change-list item 3) ─────────────────────────────────────────
    # A run may be narrowed to one order or one style. Stored as the human CODE,
    # not an id, for the reason the whole wages surface uses codes: a run reprinted
    # after a style is renamed must still say which style it paid for, and the
    # code is what appears on the printed traveler. NULL = the whole factory.
    #
    # It is stored (rather than passed again at recompute time) so a recompute
    # reproduces the SAME scope. A recompute that silently widened from one style
    # to the whole factory would pay everyone twice for the window.
    scope_order_number: Mapped[str | None] = mapped_column(String(50), index=True)
    scope_style_code: Mapped[str | None] = mapped_column(String(200), index=True)
    # ── WHICH PAYROLL THIS RUN IS (WageRunKind) ────────────────────────────
    # STORED AS A PLAIN STRING, deliberately, and not as Enum(..., name=...).
    # A native PG enum type would need its labels ALTER TYPE-d in before any
    # INSERT could name them, and this project has been bitten three times by
    # exactly that (see CLAUDE.md §13 — lining_manager, security, merchandiser
    # all shipped with a label the ORM never emitted). A VARCHAR needs no type
    # surgery, degrades identically on SQLite, and the values are validated by
    # WageRunKind at the schema boundary where a bad one becomes a 422 instead
    # of a mid-transaction DataError.
    #
    # DEFAULTS TO "combined" for the rows that pre-date the fork: those runs
    # genuinely paid both populations, and calling them PIECE or MONTHLY would
    # under-report what they already paid to the overlap guard.
    run_kind: Mapped[str] = mapped_column(
        String(20), default=WageRunKind.COMBINED.value,
        server_default=WageRunKind.COMBINED.value, index=True)
    # TRUE when scope_order_number / scope_style_code are DECORATION, not a
    # filter — i.e. on a MONTHLY run. The distinction has to be stored, not
    # re-derived from run_kind at read time, because it is what the overlap
    # guard consults: two monthly runs over the same fortnight pay the same
    # salaries twice no matter which style each one is labelled with, so a label
    # must never be allowed to look like a narrowing.
    scope_is_label: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0", nullable=False)
    # ── THE UNPAID-WORK WARNING, FROZEN WITH THE RUN ───────────────────────
    # compute returned `unrated_operations` once and then lost it: every later
    # read (GET /runs/{id}, GET /runs) hardcoded []. A manager reopening the
    # payslip screen therefore saw an empty warning list and no way to tell it
    # apart from "everything was rated". Snapshotted here so the warning is a
    # property of the run, not of the one HTTP response that computed it.
    unrated_snapshot: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # ── recompute / reopen audit ───────────────────────────────────────────
    # A closed run is still a snapshot; these make it a VERSIONED one. A payslip
    # printed at recompute_count=0 and one printed at 2 are different documents
    # and must be distinguishable on paper.
    recompute_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_recomputed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_recomputed_by: Mapped[str | None] = mapped_column(String(120))
    # Unfreezing is rarer and more serious than recomputing, so it is counted
    # separately and carries a REASON. "recomputed twice" and "unfrozen twice
    # after payment" are different facts about a payslip.
    reopen_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_reopened_by: Mapped[str | None] = mapped_column(String(120))
    last_reopen_reason: Mapped[str | None] = mapped_column(String(500))

    lines: Mapped[list["WageLine"]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )


class WageLineDetailRow(Base, UUIDMixin):
    """Per-(employee, style, operation) breakdown behind one WageLine.

    WHY A TABLE AND NOT A RECOMPUTE-ON-READ
        The whole point of freezing a run is that reading it never re-derives
        anything. Rebuilding the breakdown from production_event at read time
        would give a DIFFERENT answer the moment anyone edits a historical event
        — which is exactly the failure mode wage_line exists to prevent. So the
        breakdown freezes alongside the lines.
    """
    __tablename__ = "wage_line_detail"
    __table_args__ = (
        UniqueConstraint("wage_run_id", "employee_id", "style_id", "operation_id",
                         name="uq_wage_line_detail"),
    )
    wage_run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("wage_run.id", ondelete="CASCADE"), index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("employee.id"), index=True)
    style_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("style.id"))
    operation_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("operation.id"))
    pieces: Mapped[int] = mapped_column(Integer, default=0)
    rate: Mapped[float] = mapped_column(Numeric(10, 2))     # per-piece rate applied
    amount: Mapped[float] = mapped_column(Numeric(12, 2))   # pieces * rate


class WageLine(Base, UUIDMixin):
    """One frozen payroll line for one employee in one run.

    wage_type is a SNAPSHOT of the employee's type at run time, not a foreign key
    to it: moving a worker from piece-rate to monthly next year must not rewrite
    what last year's payslip says he was.

    B5: ONE LINE PER EMPLOYEE PER RUN is an invariant, so it lives in the
    database, not in a docstring. PIECE_RATE and MONTHLY are mutually exclusive
    branches of _populate_run; without this constraint a partial re-run, a retry,
    or two concurrent computes can each add a second line for the same worker and
    the run total silently doubles.
    """

    __tablename__ = "wage_line"
    __table_args__ = (
        UniqueConstraint("wage_run_id", "employee_id", name="uq_wage_line_run_emp"),
    )
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
