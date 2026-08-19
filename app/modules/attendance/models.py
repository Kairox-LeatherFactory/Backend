"""
================================================================================
modules/attendance/models.py — Attendance domain tables
================================================================================

PURPOSE
    Tables that make the requirement spec real:
      ShiftConfig       Factory-wide shift policy. ONE row (singleton). Stored
                        in DB so HR can change it without redeploying — start
                        time, length, grace minutes, timezone. The geofence
                        columns are still here but the FEATURE IS REMOVED; see
                        the note on them below.
      AttendanceLog     One row per (employee, work_date). check_in_at,
                        check_out_at, source (SELF / PROXY), is_late, is_short,
                        is_overtime, distance_m (now always NULL — location
                        tracking removed), recorded_by_user
                        (the supervisor for proxy entries — accountability).

WHY DATES NOT JUST TIMESTAMPS
    work_date is a separate column so the unique-per-day rule
    ("one attendance row per employee per day") is enforced by the DATABASE,
    not the application. UniqueConstraint on (employee_id, work_date) makes a
    duplicate check-in physically impossible.

WHY recorded_by_user
    Spec Flow B says supervisors mark daily-wage workers. We MUST know which
    supervisor did each proxy entry (accountability + fraud trail). NULL means
    the worker checked themselves in (Flow A).
================================================================================
"""
import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.models import GUID, TimestampMixin, UUIDMixin

import enum


class AttendanceSource(str, enum.Enum):
    SELF = "self"          # Flow A: worker themself
    PROXY = "proxy"        # Flow B: supervisor marked them


class ShiftConfig(Base, UUIDMixin, TimestampMixin):
    """Singleton row holding factory-wide shift + geofence policy. HR can
    update via API; no redeploy needed."""
    __tablename__ = "shift_config"

    # Shift policy
    shift_start: Mapped[str] = mapped_column(String(5), default="09:00")    # "HH:MM" (factory wall-clock)
    shift_length_hours: Mapped[float] = mapped_column(Numeric(4, 2), default=8.0)
    late_grace_minutes: Mapped[int] = mapped_column(default=15)

    # Factory timezone (IANA name, e.g. "Asia/Kolkata"). SINGLE source of truth
    # for interpreting wall-clock policy (shift_start -> is_late) and for which
    # calendar day a punch belongs to (work_date). Storage stays UTC; this is
    # only applied at the business-logic + display boundaries.
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Kolkata")

    # ── Geofence (the 100-metre rule) — FEATURE REMOVED, COLUMNS RETAINED ──
    # Nothing reads or writes these any more: attendance no longer knows where
    # the factory is and never asks a device where it is. They stay MAPPED on
    # purpose — they are NOT NULL in Postgres with no server default, so
    # commenting the mapping out would make the next ShiftConfig insert fail.
    # Removing them for real is a migration, not a comment-out.
    factory_lat: Mapped[float] = mapped_column(Numeric(10, 7), default=0.0)   # unused
    factory_lon: Mapped[float] = mapped_column(Numeric(10, 7), default=0.0)   # unused
    radius_m: Mapped[int] = mapped_column(default=100)                        # unused


class AttendanceLog(Base, UUIDMixin, TimestampMixin):
    """One row per (employee, work_date). The unique constraint guarantees
    'one attendance per day'."""
    __tablename__ = "attendance_log"
    __table_args__ = (UniqueConstraint("employee_id", "work_date", name="uq_att_emp_day"),)

    employee_id: Mapped[uuid.UUID] = mapped_column(GUID(), ForeignKey("employee.id"), index=True)
    work_date: Mapped[date] = mapped_column(Date, index=True)

    check_in_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    check_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source: Mapped[AttendanceSource] = mapped_column(
        Enum(AttendanceSource, name="attendance_source"), default=AttendanceSource.SELF
    )
    # Supervisor (User) who recorded a PROXY entry. NULL for SELF entries.
    recorded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("app_user.id", ondelete="SET NULL"))

    # Computed at write time so dashboards never recompute on read.
    is_late: Mapped[bool] = mapped_column(Boolean, default=False)
    is_short: Mapped[bool] = mapped_column(Boolean, default=False)
    is_overtime: Mapped[bool] = mapped_column(Boolean, default=False)
    # Distance from the factory at check-in. FEATURE REMOVED — always NULL on
    # rows written from now on. Nullable already, and historical rows keep the
    # value they were recorded with, so the column simply goes quiet.
    distance_m: Mapped[float | None] = mapped_column(Numeric(8, 2))   # unused
