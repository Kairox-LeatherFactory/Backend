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
    direction: str = Field(..., pattern="^(in|out)$")
    lat: float | None = None
    lon: float | None = None
    proxy: bool = False
    reason: str | None = Field(None, max_length=200)
    
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
    """Onboard a new daily-wage worker on the floor (Flow C).

    `phone` is optional: the worker gets no login, so there is nothing to
    authenticate with it — it is contact detail only."""
    name: str
    designation: str
    phone: str | None = None
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
    """Full config — INCLUDES fence geometry. Return only to HR/managers (F44)."""
    model_config = ConfigDict(from_attributes=True)
    shift_start: str
    shift_length_hours: float
    late_grace_minutes: int
    timezone: str
    factory_lat: float
    factory_lon: float
    radius_m: int


class ShiftConfigPublicRead(BaseModel):
    """F44: config WITHOUT the geofence geometry. Handing factory_lat/lon/radius
    to every authenticated user gives an attacker exactly what they need to forge
    a plausible in-fence coordinate. Non-privileged callers get times only."""
    model_config = ConfigDict(from_attributes=True)
    shift_start: str
    shift_length_hours: float
    late_grace_minutes: int
    timezone: str


class ShiftConfigUpdate(BaseModel):
    # F48/F109: these are applied to the config by a setattr loop and then parsed
    # on every check-in. An unvalidated shift_start like "9am" is accepted,
    # persisted, and then raises on EVERY subsequent check-in — one bad PATCH
    # bricks attendance factory-wide. Constrain them at the schema boundary.
    shift_start: str | None = Field(
        None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")          # HH:MM 24h
    shift_length_hours: float | None = Field(None, gt=0, le=24)
    late_grace_minutes: int | None = Field(None, ge=0, le=240)
    timezone: str | None = None
    factory_lat: float | None = Field(None, ge=-90, le=90)
    factory_lon: float | None = Field(None, ge=-180, le=180)
    radius_m: int | None = Field(None, ge=10, le=5000)