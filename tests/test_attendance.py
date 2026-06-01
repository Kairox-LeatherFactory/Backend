"""Geofence math + spec rules (no HTTP)."""
import pytest
from datetime import datetime, timedelta, timezone

from app.modules.attendance.geofence import haversine_m, within_geofence


def test_haversine_short_distance():
    # ~15 m apart in central Bangalore
    d = haversine_m(12.9716, 77.5946, 12.9717, 77.5947)
    assert 10 < d < 30


def test_within_geofence_blocks_far():
    ok, dist = within_geofence(13.0, 77.6, 12.9716, 77.5946, radius_m=100)
    assert ok is False
    assert dist > 100


def test_within_geofence_allows_close():
    ok, dist = within_geofence(12.9717, 77.5947, 12.9716, 77.5946, radius_m=100)
    assert ok is True
    assert dist <= 100
