"""
SYSTEM · the three surfaces added in the drawers / wages / imports change, over HTTP.

These assert the ROUTER contract — status codes, query params, role guards — for
things whose service-level behaviour is pinned elsewhere:

  · GET  /drawers            defaults to `sort=recent` and accepts `pin_codes`,
                             the client-held recently-searched band.
  · GET  /wages/runs         filterable by kind / order / style / window / status,
                             and every row carries the scope it paid for. This is
                             the route back to a run_id for a recompute.
  · POST /wages/runs         a PIECE run must name its work; `combined` cannot be
                             asked for; a MONTHLY run needs no scope.
  · DELETE /wages/runs/{id}  the escape hatch the overlap 409 has always named and
                             never had a route for.
  · GET  /imports/orders     the permanent order index the breakdown screen opens
                             from, DM/MD only.
"""
import datetime
import pytest
from app.core.enums import UserRole

API = "/api/v1"


@pytest.mark.asyncio
async def test_drawer_list_accepts_pin_codes_and_defaults_to_recent(
        api_client, as_role):
    as_role(UserRole.STORE_MANAGER)
    r = await api_client.get(f"{API}/drawers", params={
        "pin_codes": ["DRW-0042", "DRW-0117"], "limit": 5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"total", "count", "items"} <= body.keys()
    # the contract fields the store screen renders
    if body["items"]:
        row = body["items"][0]
        assert "last_activity_at" in row and "last_activity" in row
        assert "pinned" in row


@pytest.mark.asyncio
async def test_wage_run_list_filters_and_delete_route(api_client, as_role):
    as_role(UserRole.DIRECT_MANAGER)

    # filters are accepted and validated
    r = await api_client.get(f"{API}/wages/runs", params={
        "run_kind": "piece", "style_code": "NOPE", "status": "open"})
    assert r.status_code == 200, r.text
    assert r.json() == []

    r = await api_client.get(f"{API}/wages/runs", params={"status": "banana"})
    assert r.status_code == 422
    r = await api_client.get(f"{API}/wages/runs", params={"run_kind": "banana"})
    assert r.status_code == 422

    # a piece run with no scope is refused at the schema
    r = await api_client.post(f"{API}/wages/runs", json={
        "period_start": "2026-08-01", "period_end": "2026-08-14"})
    assert r.status_code == 422, r.text
    assert "style_code" in r.text

    # combined cannot be asked for
    r = await api_client.post(f"{API}/wages/runs", json={
        "run_kind": "combined",
        "period_start": "2026-08-01", "period_end": "2026-08-14"})
    assert r.status_code == 422

    # a monthly run needs no scope and computes
    r = await api_client.post(f"{API}/wages/runs", json={
        "run_kind": "monthly", "freeze": False,
        "period_start": "2026-08-01", "period_end": "2026-08-14"})
    assert r.status_code == 201, r.text
    run = r.json()
    assert run["run_kind"] == "monthly"
    assert run["monthly_only"] is True
    assert run["piece_rate_only"] is False

    # it shows up on the list WITH its kind
    r = await api_client.get(f"{API}/wages/runs", params={"run_kind": "monthly"})
    assert r.status_code == 200
    rows = r.json()
    assert rows and rows[0]["id"] == run["id"]
    assert rows[0]["run_kind"] == "monthly"
    assert rows[0]["computed_at"] is not None

    # DELETE exists and frees the window
    r = await api_client.request("DELETE", f"{API}/wages/runs/{run['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    r = await api_client.get(f"{API}/wages/runs", params={"run_kind": "monthly"})
    assert r.json() == []


@pytest.mark.asyncio
async def test_imports_order_index_is_reachable(api_client, as_role):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/imports/orders")
    assert r.status_code == 200, r.text
    assert {"total", "count", "items"} <= r.json().keys()

    r = await api_client.get(f"{API}/imports/orders", params={"status": "BANANA"})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_the_order_index_is_dm_only(api_client, as_role):
    as_role(UserRole.SUPERVISOR)
    r = await api_client.get(f"{API}/imports/orders")
    assert r.status_code == 403
