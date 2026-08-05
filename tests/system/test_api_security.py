"""
SYSTEM · the security properties of the wire contract itself.

Not "can role X reach endpoint Y" — `test_role_guards.py` owns that. This file
asks what the API does with HOSTILE INPUT once the caller is already through the
door: extra fields, ids where codes belong, junk in path parameters, and what
leaks back out in an error body.

WHY THESE ARE SYSTEM-LAYER AND NOT UNIT
    Every one of them is a property of the request/response boundary. Mass
    assignment is invisible to a service test because the service is handed a
    validated model; a stack trace in a 500 body is invisible to anything that
    does not read the raw HTTP response.
"""
import uuid

import pytest

from app.core.enums import UserRole
from app.modules.employees.models import Employee

API = "/api/v1"

pytestmark = pytest.mark.security


# ══════════════════════════════════════════════════════════ mass assignment
@pytest.mark.asyncio
async def test_extra_fields_on_employee_create_are_ignored_not_applied(
        api_client, as_role, db):
    """The classic privilege-escalation shape: post fields the schema never
    declared and see whether they reach the row.

    `EmployeeCreate` (employees/schemas.py:36-44) declares seven fields. Pydantic
    v2's default is to IGNORE unknown keys, so `id` and `is_active` below should
    vanish at the boundary. If they did not, a caller could pin an employee's
    primary key — and a chosen id is a foothold for overwriting an existing
    worker's wage lines by reference.
    """
    from sqlalchemy import select

    hostile_id = uuid.uuid4()
    as_role(UserRole.HR)
    r = await api_client.post(f"{API}/employees", json={
        "name": "MASSASSIGN", "designation": "cutter",
        "wage_type": "piece_rate",
        # none of these are on the schema:
        "id": str(hostile_id),
        "is_active": False,
        "monthly_salary_override": 999999,
        "role": "managing_director",
    })
    assert r.status_code == 201, r.text

    emp = (await db.execute(
        select(Employee).where(Employee.name == "MASSASSIGN"))).scalars().first()
    assert emp is not None
    assert emp.id != hostile_id, "caller-supplied primary key was accepted"
    assert emp.is_active is True, "caller overrode a field the schema does not expose"


@pytest.mark.asyncio
async def test_a_client_cannot_set_its_own_id_on_create(api_client, as_role, db):
    """Same shape on the tenancy root. A caller-chosen client id would let a
    later request claim an id that a real client will one day be issued."""
    from sqlalchemy import select
    from app.modules.clients.models import Client

    hostile_id = uuid.uuid4()
    as_role(UserRole.DIRECT_MANAGER)
    # `ClientCreate` (clients/schemas.py:85-88) bundles the first order, so
    # order_number is mandatory — a client never exists without one.
    r = await api_client.post(f"{API}/clients", json={
        "name": "IDPICKER", "country": "IT", "order_number": "IDPICK-PO",
        "id": str(hostile_id)})
    assert r.status_code in (200, 201), r.text

    row = (await db.execute(
        select(Client).where(Client.name == "IDPICKER"))).scalars().first()
    assert row is not None and row.id != hostile_id


# ═══════════════════════════════════════════════ codes on the wire, not UUIDs
@pytest.mark.asyncio
async def test_the_rate_sheet_refuses_a_uuid_where_a_style_code_belongs(
        api_client, as_role, order_tree):
    """CLAUDE.md / wages/service.py:23-28: "CODES IN, IDS NEVER OUT." The rate
    screens take `style_code`, and `_resolve_style` upper-cases and looks it up
    (service.py:100-117).

    Posting a raw UUID must 404 with the code echoed back, not silently resolve.
    If a UUID were accepted here, the whole "no id is ever typed on a rate
    screen" contract would be advisory — and the frontend teams would start
    sending ids, which is how the next id-enumeration bug gets written.
    """
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/wages/rate-sheet",
                             params={"style_code": str(order_tree["style"].id)})
    assert r.status_code == 404
    assert "no style with code" in r.text.lower()


@pytest.mark.asyncio
async def test_an_unknown_operation_code_names_the_known_ones(
        api_client, as_role, db, operations):
    """A 404 that lists the valid vocabulary (wages/service.py:134-139) is the
    difference between a frontend dev fixing a typo in a minute and filing a
    ticket. Asserted because it is easy to "tidy" such a message away.

    A style is created here rather than reusing `order_tree`, because
    `_resolve_style` looks up `Style.code` and the shared fixture sets only
    `name`/`article` — so order_tree's style is unreachable by code and would
    404 on the STYLE before the operation code was ever examined.
    """
    from app.modules.clients.models import Client, ClientOrder, Style

    client = Client(name="OPCODE CO", country="IT")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number="OPCODE-PO")
    db.add(order)
    await db.flush()
    db.add(Style(client_order_id=order.id, name="OPSTYLE", article="OP1",
                 code="OPSTYLE"))
    await db.commit()

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/wages/rate-history", params={
        "style_code": "OPSTYLE", "operation_code": "SEWING"})
    assert r.status_code == 404
    assert "LEATHER_CUTTING" in r.text, "the error does not name the real operations"


@pytest.mark.asyncio
async def test_an_empty_style_code_is_a_422_not_a_500(api_client, as_role):
    """wages/service.py:108-111. A blank query parameter is a user error."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/wages/rate-sheet", params={"style_code": "   "})
    assert r.status_code == 422


# ═════════════════════════════════════════════════ input validation on the wire
@pytest.mark.asyncio
@pytest.mark.parametrize("junk", [
    "not-a-uuid",
    "1 OR 1=1",
    "<script>alert(1)</script>",
    "00000000-0000-0000-0000-00000000000",          # one digit short
    "%00",
])
async def test_junk_in_a_uuid_path_parameter_is_rejected_cleanly(
        api_client, as_role, junk):
    """FastAPI coerces path params before the handler runs, so a non-UUID should
    never reach a query.

    422 (bad shape for this route) and 404 (matched no route at all) are both
    clean refusals; 500 is not, because it would mean the string got far enough
    to break something. The reflection check is the second half: an error body
    that echoed the payload back verbatim would make this endpoint a reflected-
    XSS vector for anything rendering the message.
    """
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/production/styles/{junk}/progress")
    assert r.status_code in (404, 422), f"{junk!r} produced HTTP {r.status_code}"
    assert "<script>" not in r.text, "the error body reflected raw markup"


@pytest.mark.asyncio
async def test_a_traversal_style_path_parameter_matches_no_route(api_client,
                                                                 as_role):
    """`../../etc/passwd` contains slashes, so it changes the PATH SHAPE rather
    than the parameter value — the request reaches no route and 404s before any
    handler exists to be confused. Separated from the case above because the
    mechanism is different, and asserting 422 here would be asserting the wrong
    thing."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/production/styles/../../etc/passwd/progress")
    assert r.status_code == 404
    assert "passwd" not in r.text.lower() or "detail" in r.text.lower()


@pytest.mark.asyncio
async def test_a_payroll_window_typed_backwards_is_refused_at_the_api(
        api_client, as_role):
    """The money guard, through the router rather than the service
    (wages/service.py:285-289)."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/wages/runs", json={
        "period_start": "2026-07-14", "period_end": "2026-07-01"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_a_future_payroll_window_is_refused_at_the_api(api_client, as_role):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/wages/runs", json={
        "period_start": "2030-01-01", "period_end": "2030-01-14"})
    assert r.status_code == 422
    assert "future" in r.text.lower()


# ══════════════════════════════════════════════ no internals in an error body
@pytest.mark.asyncio
@pytest.mark.parametrize("path,payload", [
    (f"{API}/wages/runs", {"period_start": "2026-07-14", "period_end": "2026-07-01"}),
    (f"{API}/materials/lots", {"category": "FABRIC", "article": "A", "colour": "B"}),
])
async def test_an_error_body_never_carries_a_traceback(api_client, as_role,
                                                       path, payload):
    """A 4xx body is shown to the operator. It must carry the business reason and
    nothing about the machine: a file path or a frame tells an attacker the
    library versions and the layout on disk."""
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(path, json=payload)
    assert r.status_code >= 400
    body = r.text.lower()
    for leak in ("traceback", "site-packages", "sqlalchemy.exc",
                 "\\app\\modules", "/app/modules", ".py\", line"):
        assert leak not in body, f"{path} leaked {leak!r} in its error body"


@pytest.mark.asyncio
async def test_a_404_does_not_echo_the_database_error(api_client, as_role):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/wages/runs/{uuid.uuid4()}")
    assert r.status_code == 404
    assert "select" not in r.text.lower(), "a SQL statement reached the client"


# ═══════════════════════════════════════════════════ unsafe delete / retirement
@pytest.mark.asyncio
async def test_there_is_no_employee_delete_route(api_client, as_role, cutter):
    """CLAUDE.md §6: "You delete the scannable code, never the person or their
    record." A DELETE that removed an employee would orphan closed wage lines —
    documents the factory already paid against.

    Asserted as 404/405 (no such route) rather than 403, because the correct
    design is that the verb does not exist, not that it is guarded.
    """
    emp, _ = cutter
    as_role(UserRole.MANAGING_DIRECTOR)
    r = await api_client.delete(f"{API}/employees/{emp.id}")
    assert r.status_code in (404, 405), (
        f"an employee delete route exists (HTTP {r.status_code}) — retirement "
        "must go through barcode deactivation, which preserves history")


@pytest.mark.asyncio
async def test_there_is_no_wage_run_delete_route(api_client, as_role):
    """A closed run is a frozen snapshot (CLAUDE.md §10). Deleting one erases the
    record of money that left the building."""
    as_role(UserRole.MANAGING_DIRECTOR)
    r = await api_client.delete(f"{API}/wages/runs/{uuid.uuid4()}")
    assert r.status_code in (404, 405)


# ══════════════════════════════════════════════════ authentication is required
@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    f"{API}/production/skus",
    f"{API}/production/operations",
    f"{API}/analytics/explorer",
    f"{API}/wages/rate-sheet?style_code=X",
])
async def test_the_tenancy_scoped_reads_still_demand_a_token(api_client, path):
    """`client_scope` depends on `get_current_user`, so these endpoints are
    authenticated even though they carry no `require_roles`. Pinning that: if the
    dependency were ever swapped for an optional one to "make the picker work",
    these would open to the public."""
    from app.main import app
    from app.modules.users.deps import get_current_user
    app.dependency_overrides.pop(get_current_user, None)

    r = await api_client.get(path)
    assert r.status_code == 401, f"{path} served an anonymous caller"
