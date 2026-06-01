"""
================================================================================
modules/employees/models.py — Shop-floor employee records
================================================================================

PURPOSE
    The people who physically make garments. Distinct from `app_user` (logins):
    an Employee is a payroll/production identity; a User is a credential. A user
    of role EMPLOYEE links to one Employee via User.employee_id.

WAGE TYPE drives the payroll fork and is a property of the PERSON, set
    explicitly — NOT inferred from designation. The source file proves it:
    'TAILOR' and 'CUTTER' appear in BOTH the monthly and piece-rate blocks.

phone / email
    Added so a login can be provisioned for each worker. The real files have no
    contact details yet, so the seed script MOCKS them deterministically
    (e.g. phone = 90000000NN). Replace with real data when available.
================================================================================
"""
from sqlalchemy import Boolean, Enum, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.core.enums import WageType
from app.core.models import TimestampMixin, UUIDMixin


class Employee(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "employee"

    name: Mapped[str] = mapped_column(String(120), index=True)
    designation: Mapped[str | None] = mapped_column(String(80))   # TAILOR, CUTTER, ...
    wage_type: Mapped[WageType] = mapped_column(
        Enum(WageType, name="wage_type"), default=WageType.PIECE_RATE
    )
    monthly_salary: Mapped[float | None] = mapped_column(Numeric(10, 2))
    daily_rate: Mapped[float | None] = mapped_column(Numeric(10, 2))   # for DAILY_WAGE workers
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Mocked contact details (used to provision logins). Nullable + unique-light.
    phone: Mapped[str | None] = mapped_column(String(30), index=True)
    email: Mapped[str | None] = mapped_column(String(160))

# Back-compat re-export: some modules import WageType from here.
__all__ = ["Employee", "WageType"]
