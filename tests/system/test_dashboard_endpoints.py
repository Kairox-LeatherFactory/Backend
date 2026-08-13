"""
SYSTEM/E2E · the dashboard HTTP surface.

WHY THIS FILE EXISTS AT ALL
    The dashboard module had NO tests and was not registered in main.py — its
    routes were unreachable in this repo. Registering it is half the fix; the
    other half is a test that fails the moment it is un-registered again.

WHAT IT PINS
    • the module is mounted, under /api/v1, behind the manager-only guard
    • piece tracking is reachable from EVERY dashboard, not just stitching, and
      every alias returns the same body (#5)
    • the consumption grids never mix stages, over the wire (#4)
    • STORE_MANAGER reaches the store dashboard; a viewer reaches nothing
"""
import datetime

import pytest

from app.core.enums import UserRole

API = "/api/v1"
TODAY = datetime.date.today().isoformat()

pytestmark = pytest.mark.asyncio


async def _cut(api_client, as_role, piece, emp, lot, dcm=12.0):
    as_role(UserRole.CUTTING_MANAGER)
    return await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(emp.id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY,
        "consumption": {"leather_lot_id": str(lot.id), "dcm": dcm},
    })


# ══════════════════════════════════════════════ the module is wired in
async def test_the_dashboards_are_reachable(api_client, as_role, pieces):
    """THE REGISTRATION GUARD. Every one of these 404s if main.py stops including
    the router — which is the state this module was found in."""
    as_role(UserRole.DIRECT_MANAGER)
    for path in ("/dashboard/cutting", "/dashboard/lining",
                 "/dashboard/stitching", "/dashboard/store",
                 "/dashboard/direct-manager"):
        r = await api_client.get(f"{API}{path}")
        assert r.status_code == 200, f"{path} -> {r.status_code} {r.text[:200]}"


async def test_the_direct_manager_panel_returns_every_block(
    api_client, as_role, pieces
):
    as_role(UserRole.DIRECT_MANAGER)
    body = (await api_client.get(f"{API}/dashboard/direct-manager")).json()
    for block in ("meta", "overall", "departments", "pipeline", "bottleneck",
                  "production_rate", "quality", "attendance", "store",
                  "order_progress", "daily_production"):
        assert block in body, f"{block} missing"
    assert len(body["departments"]) == 6
    # the gaps are declared, not silently zeroed
    assert body["quality"]["rejected"] is None
    assert "quality_rejection" in body["meta"]["unsupported"]


async def test_the_dm_drilldowns_answer_over_http(
    api_client, as_role, pieces, order_tree
):
    as_role(UserRole.DIRECT_MANAGER)
    oid, sid = order_tree["order"].id, order_tree["style"].id

    r = await api_client.get(f"{API}/dashboard/direct-manager/orders/{oid}")
    assert r.status_code == 200 and r.json()["order_number"] == "JP-PO"

    r = await api_client.get(f"{API}/dashboard/direct-manager/styles/{sid}")
    assert r.status_code == 200 and r.json()["style"] == "CLERMONT"

    import uuid as _uuid
    ghost = _uuid.uuid4()
    assert (await api_client.get(
        f"{API}/dashboard/direct-manager/orders/{ghost}")).status_code == 404
    assert (await api_client.get(
        f"{API}/dashboard/direct-manager/styles/{ghost}")).status_code == 404


async def test_the_lining_dashboard_explains_its_own_numbers(
    api_client, as_role, pieces
):
    as_role(UserRole.DIRECT_MANAGER)
    k = (await api_client.get(f"{API}/dashboard/lining")).json()["production_kpis"]
    # every population is named on the wire, not just internally
    for field in ("total_order_pieces", "minted_pieces", "lining_required_pieces",
                  "lining_required_derived", "lining_flag_stale",
                  "lining_flag_undercount", "orders_without_pieces",
                  "pending_basis"):
        assert field in k, f"{field} missing from the response"
    assert k["pending_basis"] == "lining_required_pieces"


# ══════════════════════════════════════════════ #5 — piece tracking everywhere
async def test_every_dashboard_can_follow_one_piece(
    api_client, as_role, operations, pieces, cutter, leather_lot
):
    piece, _ = pieces[0]
    assert (await _cut(api_client, as_role, piece, cutter[0], leather_lot)).status_code == 201

    as_role(UserRole.DIRECT_MANAGER)
    bodies = []
    for path in (f"/dashboard/pieces/{piece.code}",
                 f"/dashboard/cutting/pieces/{piece.code}",
                 f"/dashboard/lining/pieces/{piece.code}",
                 f"/dashboard/store/pieces/{piece.code}",
                 f"/dashboard/stitching/pieces/{piece.code}",
                 f"/dashboard/direct-manager/pieces/{piece.code}"):
        r = await api_client.get(f"{API}{path}")
        assert r.status_code == 200, f"{path} -> {r.status_code}"
        bodies.append(r.json())

    # ONE handler behind five URLs — if they ever differ, they have drifted.
    assert all(b == bodies[0] for b in bodies), (
        "the per-stage aliases must return the same trace; separate "
        "implementations are exactly what this avoids")
    assert bodies[0]["piece_code"] == piece.code
    assert bodies[0]["serial"] == "001"
    assert bodies[0]["article"] == "CL1"


async def test_an_unknown_piece_code_is_a_404_that_says_what_to_do(
    api_client, as_role, pieces
):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/dashboard/pieces/NO-SUCH-CODE")
    assert r.status_code == 404
    assert "drawer or employee card" in r.text


# ══════════════════════════════════════════════ #4 — stage isolation on the wire
async def test_the_consumption_grids_do_not_mix_stages_over_http(
    api_client, as_role, operations, pieces, cutter, leather_lot
):
    piece, _ = pieces[0]
    await _cut(api_client, as_role, piece, cutter[0], leather_lot)

    as_role(UserRole.DIRECT_MANAGER)
    cutting = (await api_client.get(f"{API}/dashboard/cutting/consumption")).json()
    lining = (await api_client.get(f"{API}/dashboard/lining/consumption")).json()

    assert {r["stage"] for r in cutting} == {"LEATHER_CUTTING"}
    assert lining == []          # no lining cuts exist
    assert cutting[0]["material_article"] == leather_lot.article
    assert cutting[0]["leather_article"] == leather_lot.article   # kept alias


async def test_a_non_cut_stage_is_rejected_with_422(api_client, as_role, pieces):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/dashboard/cutting/consumption",
                             params={"stage": "FUSING"})
    assert r.status_code == 422
    assert "cut stages" in r.text


async def test_the_stage_parameter_works_over_http(
    api_client, as_role, operations, pieces, cutter, leather_lot
):
    piece, _ = pieces[0]
    await _cut(api_client, as_role, piece, cutter[0], leather_lot)

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/dashboard/lining/consumption",
                             params={"stage": "LEATHER_CUTTING"})
    assert r.status_code == 200
    assert {row["stage"] for row in r.json()} == {"LEATHER_CUTTING"}


# ══════════════════════════════════════════════ role boundaries
async def test_the_store_manager_reads_the_store_dashboard(api_client, as_role, pieces):
    as_role(UserRole.STORE_MANAGER)
    assert (await api_client.get(f"{API}/dashboard/store")).status_code == 200


async def test_a_viewer_reads_no_dashboard(api_client, as_role, pieces):
    """These aggregate worker names and floor data — office read-only staff and
    clients have no business in them."""
    as_role(UserRole.VIEWER)
    for path in ("/dashboard/cutting", "/dashboard/lining",
                 "/dashboard/stitching", "/dashboard/store"):
        assert (await api_client.get(f"{API}{path}")).status_code == 403, path
