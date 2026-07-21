"""
================================================================================
modules/employees/schemas.py — API contract for the employees module
================================================================================
phone/email are MOCKED at seed time (the source files carry no contact data);
they are surfaced here so a manager can provision logins and contact workers.
================================================================================
"""
import uuid

from pydantic import BaseModel, ConfigDict, model_validator

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
    password: str | None = None

    @model_validator(mode="after")
    def _login_fields(self):
        if self.wage_type is WageType.MONTHLY:
            missing = [f for f in ("phone", "password") if not getattr(self, f)]
            if missing:
                raise ValueError(f"{', '.join(missing)} required for MONTHLY employees")
        elif self.password:
            raise ValueError("password is only accepted for MONTHLY employees")
        return self


class EmployeeCreateRead(EmployeeRead):
    user_created: bool = False
    login_phone: str | None = None