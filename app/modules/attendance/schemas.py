"""API contract for attendance."""
import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.attendance.models import AttendanceSource


class GpsPoint(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)

class ScanCheckIn(BaseModel):
    employee_barcode: str
    direction: str = Field(pattern="^(in|out)$")
    lat: float | None = None
    lon: float | None = None
    proxy: bool = False

class CheckInRequest(GpsPoint):
    """SELF check-in by the worker themselves (Flow A).

    Frontend sends ONLY the device GPS — the worker's identity comes from the
    auth token (user.employee_id), and the timestamp is set server-side to
    block client-side clock manipulation. Payload: {"lat": ..., "lon": ...}.
    """
    pass


class CheckOutRequest(GpsPoint):
    """SELF check-out (Flow A). Same payload as check-in: {"lat", "lon"}."""
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


class ShiftStatus(BaseModel):
    """Server-anchored payload for the frontend live countdown.

    All timestamps are UTC. The frontend renders them in `timezone` and ticks
    the remaining time toward `shift_end_at` using a server-clock offset
    (server_now - device_now) so a wrong device clock can't skew the display.
    This is display-only; the backend recomputes hours at check-out.
    """
    server_now: datetime          # UTC 'now' — anchor for the live timer
    timezone: str                 # IANA factory tz, for local rendering
    shift_length_hours: float
    checked_in: bool              # has an open/closed log today
    checked_out: bool             # already checked out today
    check_in_at: datetime | None  # UTC
    shift_end_at: datetime | None # UTC = check_in_at + shift_length_hours
    remaining_seconds: int | None # max(0, shift_end_at - server_now)


class ShiftConfigRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    shift_start: str
    shift_length_hours: float
    late_grace_minutes: int
    timezone: str
    factory_lat: float
    factory_lon: float
    radius_m: int


class ShiftConfigUpdate(BaseModel):
    shift_start: str | None = None
    shift_length_hours: float | None = None
    late_grace_minutes: int | None = None
    timezone: str | None = None
    factory_lat: float | None = None
    factory_lon: float | None = None
    radius_m: int | None = None

