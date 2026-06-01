"""
================================================================================
modules/employees/schemas.py — API contract for the employees module
================================================================================
phone/email are MOCKED at seed time (the source files carry no contact data);
they are surfaced here so a manager can provision logins and contact workers.
================================================================================
"""
import uuid

from pydantic import BaseModel, ConfigDict

from app.core.enums import WageType


class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    designation: str | None
    wage_type: WageType
    monthly_salary: float | None
    is_active: bool
    phone: str | None = None
    email: str | None = None


class EmployeeCreate(BaseModel):
    name: str
    designation: str | None = None
    wage_type: WageType = WageType.PIECE_RATE
    monthly_salary: float | None = None
    phone: str | None = None
    email: str | None = None
