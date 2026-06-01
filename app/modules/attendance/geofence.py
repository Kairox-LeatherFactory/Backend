"""
================================================================================
modules/attendance/geofence.py — Haversine distance + the 100-m rule
================================================================================
The Haversine formula gives the great-circle distance between two lat/lon
points on Earth — exact enough for a 100-meter geofence (sub-metre error).
Pure function, no I/O, trivially unit-testable with known coordinates.
================================================================================
"""
import math


EARTH_RADIUS_M = 6_371_000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres between two (lat, lon) points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlam / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


def within_geofence(device_lat: float, device_lon: float,
                    factory_lat: float, factory_lon: float,
                    radius_m: int) -> tuple[bool, float]:
    """Returns (allowed, distance_metres). Spec rule: distance <= radius."""
    d = haversine_m(device_lat, device_lon, factory_lat, factory_lon)
    return d <= radius_m, round(d, 2)
