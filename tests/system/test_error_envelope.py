"""
================================================================================
tests/system/test_error_envelope.py — one error shape, every failure
================================================================================
An unhandled 500 already carried a `request_id` the caller could quote to
support. Every DELIBERATE failure did not: a 403 on the payroll gate, a 401 on a
missing token, a 422 on a malformed body all came back as a bare
`{"detail": ...}` with nothing traceable in it.

Those are the errors the floor actually hits. A store operator who gets a 409
scanning a piece into the wrong drawer cannot tell support anything except what
the message said, and support cannot find the request in the logs.

The handlers only ADD `request_id`. Status codes, `detail`, the 422 `errors`
payload and the 401 `WWW-Authenticate` header are all unchanged — anything
reading `detail` today keeps working. These tests hold both halves of that: the
id is present, and nothing else moved.
================================================================================
"""
import uuid

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.main import app

API = "/api/v1"


@pytest.fixture
async def anon(db):
    """A client with NO auth override — so auth failures are real."""
    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _is_uuid(value) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


@pytest.mark.asyncio
@pytest.mark.security
async def test_an_unauthenticated_request_carries_a_request_id(anon):
    r = await anon.get(f"{API}/wages/runs")
    assert r.status_code == 401
    body = r.json()
    assert "detail" in body, "the message a user reads must not change"
    assert _is_uuid(body.get("request_id")), (
        f"401 has no traceable request_id: {body}")


@pytest.mark.asyncio
@pytest.mark.security
async def test_the_401_keeps_its_www_authenticate_header(anon):
    """The header drives the client's auth flow. Rebuilding the response must
    not drop it — that is the easiest thing to lose when you replace a handler."""
    r = await anon.get(f"{API}/wages/runs")
    assert r.status_code == 401
    assert "www-authenticate" in {k.lower() for k in r.headers}


@pytest.mark.asyncio
async def test_a_404_carries_a_request_id(anon):
    r = await anon.get(f"{API}/no-such-endpoint-anywhere")
    assert r.status_code == 404
    assert _is_uuid(r.json().get("request_id"))


@pytest.mark.asyncio
async def test_a_validation_error_keeps_its_field_errors(anon):
    """422 must still say WHICH field is wrong.

    `detail` on a validation error is a LIST of per-field errors, not a string —
    the import and material forms read it to highlight the offending box. The
    envelope adds an id beside it; it does not replace it with a sentence.
    """
    r = await anon.post(f"{API}/auth/login", json={"not_a_field": 1})
    assert r.status_code == 422
    body = r.json()
    assert _is_uuid(body.get("request_id"))
    assert isinstance(body.get("detail"), list) and body["detail"], (
        f"per-field errors were flattened away: {body}")
    assert any("loc" in e for e in body["detail"]), (
        "a field error with no `loc` cannot be pointed at a form field")
