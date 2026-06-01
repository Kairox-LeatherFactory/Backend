"""API contract for attendance."""
import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.attendance.models import AttendanceSource


class GpsPoint(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class CheckInRequest(GpsPoint):
    """SELF check-in by the worker themselves (Flow A)."""
    pass


class CheckOutRequest(GpsPoint):
    pass


class ProxyMarkRequest(BaseModel):
    """Supervisor marks daily-wage workers present (Flow B). The GPS pinged is
    the SUPERVISOR's device — that's the spec."""
    employee_ids: list[uuid.UUID]
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)


class AddDailyWorkerRequest(BaseModel):
    """Supervisor onboards a new daily-wage worker on the floor (Flow C)."""
    name: str
    phone: str
    designation: str
    daily_rate: float | None = None


class AttendanceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    check_in_at: datetime
    check_out_at: datetime | None
    source: AttendanceSource
    is_late: bool
    is_short: bool
    is_overtime: bool
    distance_m: float | None


class ShiftConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    shift_start: str
    shift_length_hours: float
    late_grace_minutes: int
    factory_lat: float
    factory_lon: float
    radius_m: int


class ShiftConfigUpdate(BaseModel):
    shift_start: str | None = None
    shift_length_hours: float | None = None
    late_grace_minutes: int | None = None
    factory_lat: float | None = None
    factory_lon: float | None = None
    radius_m: int | None = None
