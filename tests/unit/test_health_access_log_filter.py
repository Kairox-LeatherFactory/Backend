"""
UNIT · the health-probe access-log filter — pure, no database.

WHAT THIS DEFENDS
    The compose healthcheck hits /health every 15s, so the server log was
    thousands of `GET /health 200` lines a day with the real requests buried
    between them. `HealthCheckAccessFilter` drops a SUCCESSFUL probe and nothing
    else:

      1. a 2xx /health or /ready is dropped
      2. a FAILING probe is kept — that is the line someone is looking for
      3. every other path is kept, including one that merely starts with /health
      4. a record that is not uvicorn's 5-tuple access shape passes untouched,
         so the filter can never swallow an unrelated log line
"""
import logging

import pytest

from app.main import HealthCheckAccessFilter


def _access_record(path: str, status: int) -> logging.LogRecord:
    # Exactly what uvicorn's httptools/h11 protocols log per request.
    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:42510", "GET", path, "1.1", status),
        None,
    )


@pytest.mark.parametrize("path", ["/health", "/ready"])
@pytest.mark.parametrize("status", [200, 204, 304])
def test_successful_probe_is_dropped(path, status):
    assert HealthCheckAccessFilter().filter(_access_record(path, status)) is False


@pytest.mark.parametrize("path", ["/health", "/ready"])
@pytest.mark.parametrize("status", [404, 500, 503])
def test_failing_probe_is_kept(path, status):
    assert HealthCheckAccessFilter().filter(_access_record(path, status)) is True


@pytest.mark.parametrize("path", [
    "/", "/api/v1/clients", "/api/v1/imports/commit", "/healthz", "/health?full=1",
])
def test_other_paths_are_kept(path):
    assert HealthCheckAccessFilter().filter(_access_record(path, 200)) is True


def test_non_access_record_is_untouched():
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0, "/health %s", ("ok",), None,
    )
    assert HealthCheckAccessFilter().filter(record) is True


def test_filter_is_installed_once_on_the_access_logger():
    import app.main as main

    main._configure_logging()   # a second call (reload, re-import) must not stack it
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, HealthCheckAccessFilter) for f in filters) == 1
