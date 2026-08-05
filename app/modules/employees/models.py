"""
================================================================================
modules/employees/models.py — Shop-floor employee records
================================================================================

PURPOSE
    The people who physically make garments. Distinct from `app_user` (logins):
    an Employee is a payroll/production identity; a User is a credential.

    A SHOP-FLOOR WORKER HAS NO USER ROW. Workers are not given system access, so
    there is nothing to authenticate them with and nothing for them to log into.
    They are identified on the floor by their employee barcode, which an operator
    (SECURITY / HR / MD / DM) scans to record attendance. Only STAFF — managers,
    HR, security — carry both an Employee row and a linked User (User.employee_id).

WAGE TYPE drives the payroll fork and is a property of the PERSON, set
    explicitly — NOT inferred from designation. The source file proves it:
    'TAILOR' and 'CUTTER' appear in BOTH the monthly and piece-rate blocks.

phone / email
    OPTIONAL contact details, nothing more. They were once required in order to
    provision a login per worker; now that workers get no login, an employee can
    be created with a name and a designation alone. They stay on the model (and
    are still mocked by the seed script) because STAFF created through the
    employee door do need a phone to log in with.
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
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Mocked contact details (used to provision logins). Nullable + unique-light.
    phone: Mapped[str | None] = mapped_column(String(30), index=True)
    email: Mapped[str | None] = mapped_column(String(160))

# Back-compat re-export: some modules import WageType from here.
__all__ = ["Employee", "WageType"]
