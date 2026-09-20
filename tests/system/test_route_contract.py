"""
================================================================================
tests/system/test_route_contract.py — every operation, checked against the route
table itself
================================================================================

WHY THIS IS PARAMETRISED OVER `app.routes` AND NOT HAND-WRITTEN

The suite grew around features, so it covers the endpoints someone thought about.
Measured on 2026-09-20: 46 of the 154 in-scope operations (30%) were not named by
any test, including `POST /wages/runs/{id}/close` and `/reopen` — money-path
writes with no HTTP-level test at all. A hand-written checklist has exactly that
failure mode: it covers what was true the day it was written.

Reading the route table instead means a route added tomorrow is covered the
moment it is registered, without anyone remembering anything.

WHAT THESE FOUR CHECKS ARE FOR — and what they are NOT

They are CONTRACT checks: does the endpoint refuse anonymous callers, refuse the
wrong roles, answer rather than explode, and declare its response type. They say
nothing about whether it returns the RIGHT answer; that is what the integration
and happy-path suites are for. Both matter, and only this one scales to "all of
them".

WHY AN EMPTY DATABASE IS THE RIGHT FIXTURE HERE

Every operation runs against a schema with no rows. A correct handler answers
"nothing found" — 404, or an empty list. A handler that raises on absent data is
broken regardless of what the data would have been, and this is the cheapest way
to find that across 154 operations at once. It is also the state a fresh
deployment is in, which is when nobody is watching.
================================================================================
"""
import datetime
import inspect
import typing
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import Base, get_db
from app.core.enums import UserRole
from app.main import app
from app.modules.users.deps import get_current_user

API_PREFIX = "/api/v1"

# The Phase-2 modules (CLAUDE.md s12). Not audited, not tested here, and their
# routes are not held to this contract.
OUT_OF_SCOPE = {"procurement", "bom", "inventory", "supplier-po", "chat"}

# `require_roles` waves these through EVERY gate (users/deps.py:69), so they can
# never be used as the "denied" role in the role-gate check.
SUPERUSERS = {UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER}


# ══════════════════════════════════════════════════════ the operations under test
def _in_scope_operations():
    """(method, path, route) for every in-scope operation, sorted and stable."""
    out = []
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"})
        if not methods or not path.startswith(API_PREFIX + "/"):
            continue
        if path.split("/")[3] in OUT_OF_SCOPE:
            continue
        for method in methods:
            out.append((method, path, route))
    return sorted(out, key=lambda x: (x[1], x[0]))


OPERATIONS = _in_scope_operations()
IDS = [f"{m} {p}" for m, p, _ in OPERATIONS]


def test_the_sweep_actually_covers_the_whole_surface():
    """A filter bug here would silently shrink the matrix and everything below
    would still be green. Pin the size so that cannot happen quietly.

    This is a floor, not an equality: routes get ADDED, and a new endpoint should
    not fail this test — it should simply be swept like the rest. If the count
    drops, either a module moved out of scope deliberately (update the number) or
    the filter broke (fix it).
    """
    assert len(OPERATIONS) >= 154, (
        f"only {len(OPERATIONS)} operations collected; the sweep covered 154 when "
        "it was written, so the route filter has probably stopped matching")


# ══════════════════════════════════════════════════════════════════════ fixtures
class _Caller:
    """Duck-typed stand-in for the User row the deps return.

    Mirrors FakeUser in test_role_guards.py. It carries phone/email because
    UserRead requires them and GET /auth/me serialises the caller straight back
    — a stub without them produces a ResponseValidationError that looks exactly
    like a broken endpoint. (It did, during the audit, and cost a false finding.)
    """

    def __init__(self, role: UserRole, employee_id=None, client_id=None):
        self.id = uuid.uuid4()
        self.name = f"SWEEP {role.value}"
        self.phone = "9990000000"
        self.email = "sweep@test.local"
        self.role = role
        self.employee_id = employee_id
        self.client_id = client_id
        self.is_active = True
        self.must_change_password = False


@pytest_asyncio.fixture
async def empty_db():
    """A schema with no rows — see the module docstring for why."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield Session
    await engine.dispose()


@pytest_asyncio.fixture
async def sweep_client(empty_db):
    """An HTTP client on the real app, bound to the empty database.

    Auth is NOT overridden here. Each test installs the caller it needs, so the
    anonymous case stays genuinely anonymous.
    """
    async def _db():
        async with empty_db() as session:
            yield session

    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://sweep") as client:
        yield client
    app.dependency_overrides.clear()


def _as(role: UserRole):
    app.dependency_overrides[get_current_user] = lambda: _Caller(role)


# ═══════════════════════════════════════════════════ synthesising a request body
_SAMPLE_BY_NAME = {
    "start": "2026-01-01", "end": "2026-12-31", "on": "2026-06-01",
    "work_date": "2026-06-01", "period_start": "2026-01-01",
    "period_end": "2026-01-31", "effective_from": "2026-01-01",
    "category": "LEATHER", "subtype": "PLAIN_LINING",
}


def _sample_for(annotation, name: str = ""):
    """A type-appropriate value for one field.

    The aim is a body that gets PAST parsing, so the handler actually runs. It
    does not need to be semantically valid — a 404 or a business 409 is a fine
    outcome and still proves the handler answered instead of raising.
    """
    if name in _SAMPLE_BY_NAME:
        return _SAMPLE_BY_NAME[name]

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin is typing.Union or (origin is not None and type(None) in args):
        inner = [a for a in args if a is not type(None)]
        return _sample_for(inner[0], name) if inner else None
    if origin in (list, set, tuple):
        return []
    if origin is dict:
        return {}
    if annotation is bool:
        return False
    if annotation in (int,):
        return 1
    if annotation in (float,):
        return 1.0
    if annotation is uuid.UUID:
        return str(uuid.uuid4())
    if annotation is datetime.date:
        return "2026-06-01"
    if annotation is datetime.datetime:
        return "2026-06-01T09:00:00Z"
    if isinstance(annotation, type) and issubclass(annotation, str):
        return "SAMPLE"
    # An enum: take its first member, which is always a legal value.
    if isinstance(annotation, type) and hasattr(annotation, "__members__"):
        first = next(iter(annotation.__members__.values()))
        return getattr(first, "value", str(first))
    if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
        return _body_for_model(annotation)
    return "SAMPLE"


def _body_for_model(model, _depth: int = 0) -> dict:
    """A minimal body: every REQUIRED field, nothing optional."""
    if _depth > 3:
        return {}
    body = {}
    for name, field in model.model_fields.items():
        if not field.is_required():
            continue
        body[name] = _sample_for(field.annotation, name)
    return body


def _request_kwargs(route) -> dict:
    """Body + query params that get a request past validation, where we can."""
    kwargs = {}
    body_field = getattr(route, "body_field", None)
    if body_field is not None:
        annotation = getattr(body_field.field_info, "annotation", None)
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            kwargs["json"] = _body_for_model(annotation)
        else:
            kwargs["json"] = {}
    params = {}
    for q in route.dependant.query_params:
        if q.name in _SAMPLE_BY_NAME:
            params[q.name] = _SAMPLE_BY_NAME[q.name]
        elif getattr(q.field_info, "is_required", lambda: False)():
            params[q.name] = _sample_for(q.field_info.annotation, q.name)
    if params:
        kwargs["params"] = params
    return kwargs


def _fill_path(path: str) -> str:
    out = []
    for segment in path.strip("/").split("/"):
        if segment.startswith("{"):
            name = segment[1:-1]
            out.append("SAMPLE" if ("code" in name or "number" in name)
                       else str(uuid.uuid4()))
        else:
            out.append(segment)
    return "/" + "/".join(out)


# ═══════════════════════════════════════════════════════ 1 · authentication
# Endpoints that are SUPPOSED to be reachable without a token.
PUBLIC = {
    ("POST", f"{API_PREFIX}/auth/login"),
}


@pytest.mark.asyncio
@pytest.mark.security
@pytest.mark.parametrize("method,path,route", OPERATIONS, ids=IDS)
async def test_every_operation_requires_authentication(
        method, path, route, sweep_client):
    """An anonymous caller must never get a success.

    This is the check that catches a route shipping without a gate. It is a
    whole-surface check on purpose: a missing `Depends` on ONE new endpoint is
    invisible everywhere else, and is exactly the kind of thing that reaches
    production because the feature it belongs to works perfectly.
    """
    if (method, path) in PUBLIC:
        pytest.skip("deliberately public")

    app.dependency_overrides.pop(get_current_user, None)   # genuinely anonymous
    response = await sweep_client.request(
        method, _fill_path(path), **_request_kwargs(route))

    assert response.status_code not in (200, 201, 202, 204), (
        f"{method} {path} answered {response.status_code} with NO credentials. "
        "Either it is missing an auth dependency, or it belongs in PUBLIC above "
        "with a comment saying why.")


# ═══════════════════════════════════════════════════════ 2 · nothing may raise
@pytest.mark.asyncio
@pytest.mark.integrity
@pytest.mark.parametrize("method,path,route", OPERATIONS, ids=IDS)
async def test_no_operation_raises_against_an_empty_database(
        method, path, route, sweep_client):
    """Answer, do not explode.

    Called as the superuser so no role gate can mask a handler error. Any 2xx or
    4xx is a pass — including 404 and 422, which are the CORRECT answers when
    the row does not exist or the synthesised body is not semantically valid. A
    5xx means the handler raised, and that is a defect whatever the data.
    """
    _as(UserRole.MANAGING_DIRECTOR)
    response = await sweep_client.request(
        method, _fill_path(path), **_request_kwargs(route))

    assert response.status_code < 500, (
        f"{method} {path} raised {response.status_code} against an empty "
        f"database: {response.text[:400]}")


# ═══════════════════════════════════════════════════════ 3 · the role gate holds
def _declared_roles(route) -> set[UserRole] | None:
    """The allow-list a route's `require_roles(...)` gate closes over.

    `require_roles(*allowed)` returns a closure, so the tuple it captured is
    readable through the function's closure cells. Deriving the matrix from the
    code is the whole point: a hand-kept table drifts the first time somebody
    widens a gate, and drifts silently.

    Returns None when no gate is readable — either the route is open, or it
    checks the role INSIDE the handler, which this cannot see. Those are listed
    in IN_HANDLER_ROLE_CHECKS below.
    """
    found: set[UserRole] = set()

    def walk(dependant, depth=0):
        if depth > 3:
            return
        for sub in dependant.dependencies:
            call = getattr(sub, "call", None)
            if call is not None:
                try:
                    allowed = inspect.getclosurevars(call).nonlocals.get("allowed")
                except (TypeError, ValueError):
                    allowed = None
                if allowed:
                    found.update(allowed)
            walk(sub, depth + 1)

    walk(route.dependant)
    return found or None


# Routes that check the role in the HANDLER rather than through a dependency, so
# the allow-list cannot be read off the route. Listed so the gap is visible and
# counted; an entry disappears when its route moves to a `Depends` gate.
IN_HANDLER_ROLE_CHECKS = {
    f"{API_PREFIX}/attendance/history",
    f"{API_PREFIX}/clients",
    f"{API_PREFIX}/clients/styles",
    f"{API_PREFIX}/employees",
}


@pytest.mark.asyncio
@pytest.mark.security
@pytest.mark.parametrize("method,path,route", OPERATIONS, ids=IDS)
async def test_a_role_outside_the_allow_list_is_refused(
        method, path, route, sweep_client):
    """Every role NOT in the gate's allow-list must get 403.

    The denied role is picked from the enum rather than hardcoded, so a role
    added later is tested against every existing gate for free.
    """
    allowed = _declared_roles(route)
    if allowed is None:
        pytest.skip("no readable allow-list — open route, or an in-handler check")

    candidates = [r for r in UserRole
                  if r not in allowed and r not in SUPERUSERS
                  and r is not UserRole.EMPLOYEE]
    if not candidates:
        pytest.skip("every non-superuser role is permitted here")

    denied = candidates[0]
    _as(denied)
    response = await sweep_client.request(
        method, _fill_path(path), **_request_kwargs(route))

    assert response.status_code in (401, 403), (
        f"{method} {path} allows '{denied.value}', which is not in its "
        f"allow-list {sorted(r.value for r in allowed)} — got "
        f"{response.status_code}")


# ═══════════════════════════════════════════════════ 4 · a declared response type
# Waived, with the reason. Shrink this list; do not grow it.
NO_RESPONSE_MODEL_WAIVED = {
    # 204 No Content: there is no body, so a response_model would be wrong.
    ("POST", f"{API_PREFIX}/auth/change-password"),
    ("DELETE", f"{API_PREFIX}/clients/{{client_id}}"),

    # 410 GONE stubs. These raise HTTPException unconditionally and never return
    # a body — they exist so an old client gets "this moved, here is where" in
    # place of a 404. There is nothing to type. Delete the route and this entry
    # together once no client calls it.
    ("POST", f"{API_PREFIX}/production/cutting"),      # -> /production/log
    ("POST", f"{API_PREFIX}/production/scan"),         # -> /production/log

    # THE SKU SCAN CHECKLIST. A paged envelope whose `pieces` rows are assembled
    # across four services (store state, lining requirement, kit, eligibility)
    # and carry a nested `store` block. Typing it is worth doing and is NOT a
    # five-minute job: `response_model` FILTERS, so a model that misses one key
    # deletes it from the scan screen silently. It goes with the analytics /
    # imports / styles contract pass, not ahead of it.
    ("GET", f"{API_PREFIX}/production/skus/{{sku_id}}/pieces"),
}

# Modules whose responses are large nested ad-hoc dicts. Typing them needs a
# contract pass WITH the frontend, not a guess: `response_model` FILTERS the
# response, so a model missing one field deletes that field from the API
# silently, with nothing failing in CI.
UNTYPED_MODULES_PENDING_CONTRACT = {"analytics", "imports", "styles"}


@pytest.mark.parametrize("method,path,route", OPERATIONS, ids=IDS)
def test_every_operation_declares_a_response_model(method, path, route):
    """Without one the OpenAPI schema has no return type and the frontend cannot
    generate a typed client, however fresh the export is."""
    if (method, path) in NO_RESPONSE_MODEL_WAIVED:
        return
    if path.split("/")[3] in UNTYPED_MODULES_PENDING_CONTRACT:
        pytest.skip("pending a schema pass with the frontend")

    assert getattr(route, "response_model", None) is not None, (
        f"{method} {path} declares no response_model")
