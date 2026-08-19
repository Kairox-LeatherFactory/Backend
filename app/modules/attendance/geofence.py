"""
================================================================================
modules/attendance/geofence.py — DISABLED (location tracking removed)
================================================================================
THE GEOFENCE FEATURE IS OFF.

The factory no longer records a factory position and no longer asks the device
for a live position. Check-in and check-out succeed on identity alone (an
operator scans the card, or types the employee in) — there is no distance test
anywhere on the attendance write path.

The implementation below is COMMENTED OUT, not deleted, so the 100-metre rule
can be switched back on by uncommenting this module plus the four call sites
marked "GEOFENCE DISABLED" in modules/attendance/service.py.

WHAT ELSE IS STILL IN THE SCHEMA (deliberately)
    ShiftConfig.factory_lat / factory_lon / radius_m and AttendanceLog.distance_m
    are still MAPPED columns. They are NOT NULL in Postgres, so unmapping them
    would break the next ShiftConfig insert; they are simply never read or
    written now. Dropping them is a migration, not a comment-out.
================================================================================
"""

# import math
#
#
# EARTH_RADIUS_M = 6_371_000.0
#
#
# def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
#     """Distance in metres between two (lat, lon) points."""
#     p1, p2 = math.radians(lat1), math.radians(lat2)
#     dphi = math.radians(lat2 - lat1)
#     dlam = math.radians(lon2 - lon1)
#     a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
#     return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))
#
#
# def within_geofence(device_lat: float, device_lon: float,
#                     factory_lat: float, factory_lon: float,
#                     radius_m: int) -> tuple[bool, float]:
#     """Returns (allowed, distance_metres). Spec rule: distance <= radius."""
#     d = haversine_m(device_lat, device_lon, factory_lat, factory_lon)
#     return d <= radius_m, round(d, 2)
