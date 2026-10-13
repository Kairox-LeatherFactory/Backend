"""API contract for attendance.

LOCATION TRACKING IS REMOVED. There is no factory position and no device
position anywhere on the write path, so nothing below is location-gated.

`lat` / `lon` survive as OPTIONAL, IGNORED fields rather than being deleted:
an older frontend build still posts them, and a hard 422 on an extra key would
break check-in for anyone who had not redeployed. They are never read. The
geofence pieces of the shift config are commented out below, next to the fields
that replaced them.
"""
import uuid
from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from app.modules.attendance.models import AttendanceSource


class GpsPoint(BaseModel):
    """LOCATION REMOVED — both fields are optional and ignored.

    Was: `lat: float = Field(..., ge=-90, le=90)` / `lon: float = Field(...)`,
    i.e. REQUIRED. They are kept (nullable) purely so a client that still sends
    coordinates gets a 201, not a 422.
    """
    lat: float | None = None      # ignored
    lon: float | None = None      # ignored


class ScanCheckIn(BaseModel):
    employee_barcode: str
    direction: str = Field(..., pattern="^(in|out)$")
    lat: float | None = None      # ignored (location removed)
    lon: float | None = None      # ignored (location removed)
    proxy: bool = False
    # REMOVED 2026-09-19 (Hamthan): `reason`. It existed to excuse a scan taken
    # with no GPS fix, back when the geofence could refuse one. Location
    # tracking is gone, so no scan is ever refused for a missing fix and there
    # is nothing left to excuse — the service has been ignoring the value for
    # some time already. A field the API accepts and discards is worse than no
    # field: the gate operator is asked to type something that changes nothing.
    # The correction trail (attendance/corrections.py) is where an attendance
    # mistake is explained, and that reason IS recorded.


class CheckInRequest(GpsPoint):
    """An operator records their OWN arrival.

    The body is now EMPTY — identity comes from the auth token
    (user.employee_id) and the timestamp is set server-side to block
    client-side clock manipulation. `POST` with `{}` or with no body at all.
    """
    pass


class CheckOutRequest(GpsPoint):
    """Operator's own check-out. Empty body, same as check-in."""
    pass


class ProxyMarkRequest(BaseModel):
    """Operator marks employees present by typing them in (the manual door).

    Only `employee_ids` is required now. The supervisor's device position used
    to be mandatory here (`lat`/`lon` were `Field(...)`); it is no longer asked
    for or recorded.
    """
    employee_ids: list[uuid.UUID]
    lat: float | None = None      # ignored (location removed)
    lon: float | None = None      # ignored (location removed)


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
    name: str
    work_date: date
    check_in_at: datetime
    check_out_at: datetime | None
    source: AttendanceSource
    # WHO RECORDED THIS ROW. A shop-floor worker has no login, so every
    # attendance row is written FOR them by an operator (SECURITY / HR / MD / DM).
    # The column has always been on the model and has always been populated —
    # it just was not on this schema, so the API never told anyone who marked a
    # worker present. That made the operator trail real in the database and
    # invisible through the API, which is the half that matters in a dispute.
    # None only for the legacy rows written before the operator model landed.
    recorded_by_user_id: uuid.UUID | None = None
    is_late: bool
    is_short: bool
    is_overtime: bool
    # LOCATION REMOVED — always None on rows written from now on. Kept in the
    # response so a frontend reading the key does not break; historical rows
    # still carry the distance they were recorded with.
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
    """Full config for HR/managers.

    LOCATION REMOVED — this used to be the privileged variant that also carried
    the fence geometry, which is why F44 split it from ShiftConfigPublicRead.
    With no fence there is no geometry to withhold, so the two shapes are now
    identical; both are kept so neither response contract changes name.
    """
    model_config = ConfigDict(from_attributes=True)
    shift_start: str
    shift_length_hours: float
    late_grace_minutes: int
    timezone: str
    # factory_lat: float
    # factory_lon: float
    # radius_m: int


class ShiftConfigPublicRead(BaseModel):
    """Config for non-privileged callers — shift times only.

    F44 originally existed to keep factory_lat/lon/radius away from a client or
    employee login, since those three values are exactly what you need to forge
    a plausible in-fence coordinate. The fence is gone, so this shape now
    matches ShiftConfigRead; it stays as its own model so the split can come
    back with the fence.
    """
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
    # LOCATION REMOVED — the fence is not configurable because there is no
    # fence. Nothing reads these columns any more; PATCHing them would only
    # write dead data, so they are off the update contract.
    # factory_lat: float | None = Field(None, ge=-90, le=90)
    # factory_lon: float | None = Field(None, ge=-180, le=180)
    # radius_m: int | None = Field(None, ge=10, le=5000)


class AttendanceCorrection(BaseModel):
    """Correct a punch. Every field optional; omitted means unchanged.

    `employee_id` IS NOT AN ORDINARY FIELD EDIT. It means "this was the wrong
    person", and the day's production events move with it — the work happened,
    it was simply filed under the wrong name. The response says how many moved.
    """
    employee_id: uuid.UUID | None = None
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    reason: str | None = None


class AttendanceCorrectionResult(BaseModel):
    attendance_id: uuid.UUID
    employee_id: uuid.UUID
    work_date: date
    check_in_at: datetime | None = None
    check_out_at: datetime | None = None
    is_late: bool = False
    is_short: bool = False
    is_overtime: bool = False
    production_events_moved: int = 0
    message: str


class AttendanceDeleteResult(BaseModel):
    attendance_id: uuid.UUID
    deleted: bool
    message: str


class DailyWorkerCreated(BaseModel):
    """A casual worker added at the gate: identity + how they are paid."""
    id: str
    name: str
    wage_type: str


class ScanResult(BaseModel):
    """What the gate terminal shows after a card is scanned.

    Times are ISO strings rather than datetimes: the terminal renders them
    straight back and this payload has always been strings.
    """
    employee_id: str
    employee_name: str
    work_date: str
    check_in_at: str | None = None
    check_out_at: str | None = None
    is_late: bool
    present_today: bool
    # GEOFENCE DISABLED. Always True, kept so a frontend still reading this key
    # does not crash. It no longer means "we could not verify the position":
    # nothing is verified.
    location_unverified: bool = True
