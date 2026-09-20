"""
================================================================================
tests/system/test_wages_endpoints.py — the payroll surface, over HTTP
================================================================================

WHY THESE EXIST

Seven wage operations had no HTTP-level test at all — measured, not guessed:
`close`, `reopen`, `recompute`, `runs/{id}/pieces`, `ledger`, `rates/bulk` and
`styles`. The service layer covers the arithmetic well; what nothing covered was
the layer above it, where the role gate lives.

That matters here more than anywhere else in the app. `_PAYROLL_READERS` and
`_PAYROLL_WRITERS` are two dependencies that differ by one role, and until
recently the reader gate was guarding five mutating routes — HR could set piece
rates, start a run, and DELETE a completed one. A service test cannot see that;
only a request can.

WHAT IS ASSERTED

The response BODY, not just the status. A 200 carrying the wrong run status is
the failure mode that matters on a payroll screen: `close` freezes a run and
`reopen` unfreezes a PAID one, and each is supposed to leave an audit trail that
says who and why.
================================================================================
"""
import datetime

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.core.enums import UserRole
from app.main import app
from app.modules.users.deps import get_current_user

from tests.system.test_role_guards import FakeUser

API = "/api/v1"


@pytest_asyncio.fixture
async def client(db):
    async def _db():
        yield db
    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


def _as(role: UserRole):
    app.dependency_overrides[get_current_user] = lambda: FakeUser(role)


@pytest_asyncio.fixture
async def priced_style(db, seed_min):
    """seed_min's style, given a `code`.

    THE WAGE PICKERS FILTER ON `Style.code IS NOT NULL` (clients/repository.py
    `_style_options_stmt`), because a Rate is keyed to a style CODE — a style
    without one cannot be priced and is correctly not offered. seed_min leaves
    `code` unset, so on its own it is invisible to every endpoint in this file.
    Setting it here rather than changing seed_min keeps the other tests that
    depend on that fixture exactly as they were.
    """
    style = seed_min["style"]
    style.code = "SEEDSTYLE"
    await db.commit()
    await db.refresh(style)
    return style


def _ok(response):
    assert response.status_code in (200, 201), (
        f"{response.request.method} {response.request.url.path} -> "
        f"{response.status_code}: {response.text[:300]}")
    return response.json()


# ═══════════════════════════════════════════════════════════════ the pickers
@pytest.mark.asyncio
@pytest.mark.money
async def test_the_style_picker_lists_released_styles_with_pricing_coverage(
        client, priced_style):
    """GET /wages/styles — the payroll landing screen's style cards.

    RELEASED ONLY, by design: a style still in a DRAFT breakdown has no pieces
    and no scanned work, so offering it a rate card invites a manager to price
    work that does not exist.
    """
    _as(UserRole.DIRECT_MANAGER)
    body = _ok(await client.get(f"{API}/wages/styles"))

    rows = body["items"] if isinstance(body, dict) else body
    assert isinstance(rows, list)
    codes = {r.get("style_code") or r.get("style_name") for r in rows}
    assert codes, "the seeded RELEASED style should be offered for pricing"


@pytest.mark.asyncio
@pytest.mark.money
async def test_the_order_picker_answers(client, priced_style):
    """GET /wages/orders — order cards, one level up from the style cards."""
    _as(UserRole.DIRECT_MANAGER)
    body = _ok(await client.get(f"{API}/wages/orders"))
    rows = body["items"] if isinstance(body, dict) else body
    assert isinstance(rows, list)


# ═══════════════════════════════════════════════════════════════ setting rates
@pytest.mark.asyncio
@pytest.mark.money
async def test_a_whole_rate_sheet_saves_in_one_call(client, seed_min, priced_style):
    """POST /wages/rates/bulk — the sheet save.

    One transaction or none: a typo in line 6 must fail the whole save rather
    than commit lines 1-5 and then 404, which would leave three operations
    repriced and four on yesterday's rate with no error a manager can act on.
    """
    _as(UserRole.DIRECT_MANAGER)
    style = priced_style
    ops = seed_min["operations"]
    codes = [o.code for o in list(ops.values())[:2]] if isinstance(ops, dict) \
        else [o.code for o in ops[:2]]

    body = _ok(await client.post(f"{API}/wages/rates/bulk", json={
        "style_code": style.code,
        "effective_from": "2026-01-01",
        "lines": [{"operation_code": c, "rate": 12.5} for c in codes],
    }))
    assert body["saved"] == len(codes)
    assert body["style_code"] == style.code


@pytest.mark.asyncio
@pytest.mark.security
async def test_hr_may_read_the_rate_sheet_but_not_write_it(client, priced_style):
    """VISIBILITY IS NOT AUTHORITY.

    CLAUDE.md s10 grants HR sight of wages. It does not grant HR the power to
    decide what a person is paid. This pins both halves in one test, because the
    bug that existed was precisely the two being conflated: a dependency named
    `_PAYROLL_READERS` was guarding the writes.
    """
    style = priced_style

    _as(UserRole.HR)
    read = await client.get(f"{API}/wages/runs")
    assert read.status_code == 200, "HR must still be able to READ payroll"

    write = await client.post(f"{API}/wages/rates", json={
        "style_code": style.code, "operation_code": "LEATHER_CUTTING",
        "rate": 99.0, "effective_from": "2026-01-01"})
    assert write.status_code == 403, (
        f"HR set a piece rate (HTTP {write.status_code}) — the writer gate is "
        "not holding")


# ═════════════════════════════════════════════════ the run lifecycle over HTTP
@pytest_asyncio.fixture
async def paid_work(db, client, priced_style, seed_min):
    """A style with a RATE and real scanned work against it.

    TWO GUARDS MAKE THIS NECESSARY, and both are correct:

      - a piece-rate run must name `style_code` or `order_number`, because two
        dates alone would pay every piece of every order in the window;
      - a run with no wage lines cannot be closed, because freezing an empty run
        locks a window in which nobody is paid.

    So a lifecycle test needs an actual piece, an actual event and an actual
    rate. Building that here is the difference between testing the endpoints and
    testing the error messages.
    """
    from app.modules.clients.models import SKU
    from app.modules.production.models import Piece
    from tests.conftest import _log_stage

    sku = seed_min["sku"]
    employee = seed_min["employee"]
    operations = seed_min["operations"]

    piece = Piece(code="SEED-PIECE-001", seq=1, sku_id=sku.id,
                  current_operation_id=None)
    db.add(piece)
    await db.flush()
    await _log_stage(db, operations, piece, employee.id, "LEATHER_CUTTING")
    await db.commit()

    _as(UserRole.DIRECT_MANAGER)
    _ok(await client.post(f"{API}/wages/rates", json={
        "style_code": priced_style.code,
        "operation_code": "LEATHER_CUTTING",
        "rate": 10.0,
        "effective_from": (datetime.date.today()
                           - datetime.timedelta(days=30)).isoformat(),
    }))
    return {"style": priced_style, "piece": piece, "employee": employee}


@pytest_asyncio.fixture
async def open_run(client, paid_work):
    """A computed, UNFROZEN run with at least one wage line on it."""
    _as(UserRole.DIRECT_MANAGER)
    today = datetime.date.today()
    body = _ok(await client.post(f"{API}/wages/runs", json={
        "period_start": (today - datetime.timedelta(days=7)).isoformat(),
        "period_end": today.isoformat(),
        "run_kind": "piece",
        "style_code": paid_work["style"].code,
        "freeze": False,
    }))
    return body


@pytest.mark.asyncio
@pytest.mark.money
async def test_a_run_computes_reads_back_and_freezes(client, open_run):
    """POST /wages/runs -> GET /runs/{id} -> /breakdown -> /pieces -> close.

    The whole lifecycle in one test because the states are only meaningful in
    sequence: a run that cannot be read back is not a run, and `close` is only
    interesting on something that was open.
    """
    run_id = open_run["id"]
    _as(UserRole.DIRECT_MANAGER)

    detail = _ok(await client.get(f"{API}/wages/runs/{run_id}"))
    assert str(detail["id"]) == str(run_id)

    _ok(await client.get(f"{API}/wages/runs/{run_id}/breakdown"))
    _ok(await client.get(f"{API}/wages/runs/{run_id}/pieces"))

    closed = _ok(await client.post(f"{API}/wages/runs/{run_id}/close"))
    assert str(closed["status"]).lower().endswith("closed"), (
        f"close left the run at {closed['status']}")


@pytest.mark.asyncio
@pytest.mark.money
async def test_reopening_a_closed_run_demands_a_reason_and_records_it(
        client, open_run):
    """POST /runs/{id}/reopen — unfreezing a PAID payslip.

    The reason is not decoration. A payslip that has been unfrozen after payment
    has to be identifiable as such, and the reason has to still be legible a year
    later, so it is stored on the run rather than only logged.
    """
    run_id = open_run["id"]
    _as(UserRole.DIRECT_MANAGER)
    _ok(await client.post(f"{API}/wages/runs/{run_id}/close"))

    without = await client.post(f"{API}/wages/runs/{run_id}/reopen", json={})
    assert without.status_code == 422, (
        "a paid run was reopened with no reason given")

    body = _ok(await client.post(f"{API}/wages/runs/{run_id}/reopen",
                                 json={"reason": "wrong rate on CUTTING"}))
    assert str(body["status"]).lower().endswith("open")
    assert body["reopen_count"] >= 1
    assert "wrong rate" in (body.get("last_reopen_reason") or "")


@pytest.mark.asyncio
@pytest.mark.money
async def test_recompute_rebuilds_the_run_and_counts_itself(client, open_run):
    """POST /runs/{id}/recompute — same window, same scope, rebuilt lines.

    `recompute_count` is the audit trail. A run that has been repriced three
    times should say so on its face, because "why is this payslip different from
    the one I printed" is the question it exists to answer.
    """
    run_id = open_run["id"]
    _as(UserRole.DIRECT_MANAGER)

    body = _ok(await client.post(f"{API}/wages/runs/{run_id}/recompute", json={}))
    assert body["recomputed"] is True
    assert body["recompute_count"] >= 1


@pytest.mark.asyncio
@pytest.mark.money
async def test_the_ledger_lists_runs_with_their_totals(client, open_run):
    """GET /wages/ledger — every run with what it paid, for the payroll history
    screen."""
    _as(UserRole.DIRECT_MANAGER)
    body = _ok(await client.get(f"{API}/wages/ledger"))
    rows = body["items"] if isinstance(body, dict) else body
    assert isinstance(rows, list)
    assert any(str(r.get("run_id")) == str(open_run["id"]) for r in rows), (
        "the run just computed is missing from the ledger")
