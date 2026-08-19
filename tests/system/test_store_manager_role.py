"""
SYSTEM/E2E · the Store Manager login — bug #16.

THE SEPARATION THAT MAKES THE ROLE WORTH ADDING
    The client asked for a dedicated Store Management login, "maintained
    separately from Production Logger access". A role that could do both would
    have satisfied the letter of that and none of its point, so STORE_MANAGER is
    deliberately scoped:

        CAN   read the Drawers List, open a drawer, scan parts in, send batches
        CANNOT log a production stage

    That cannot is not enforced by a deny-list. STORE_MANAGER is simply absent
    from STAGE_ROLE_ACCESS and from ROLE_TO_SCREEN, so there is no stage its scan
    could ever resolve to — the same mechanism that keeps every other role inside
    its own lane. This file pins the boundary in both directions, because a role
    test that only proves the allowed half is how privilege quietly widens.
"""
import datetime

import pytest

from app.core.enums import UserRole

API = "/api/v1"
TODAY = datetime.date.today().isoformat()

pytestmark = pytest.mark.security


def _emp_code(emp) -> str:
    return f"EMP-{str(emp.id)[:6].upper()}"


# ══════════════════════════════════════════════ what the store manager CAN do
@pytest.mark.asyncio
async def test_the_store_manager_runs_the_whole_store_hub(
    api_client, as_role, cut_pieces, cutter
):
    piece, drawer = cut_pieces[0]
    as_role(UserRole.STORE_MANAGER)

    # the list
    r = await api_client.get(f"{API}/drawers")
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 5

    # the detail
    r = await api_client.get(f"{API}/drawers/{drawer.id}")
    assert r.status_code == 200, r.text
    assert r.json()["code"] == drawer.code

    # scanning parts in
    for _ in range(2):
        r = await api_client.post(f"{API}/drawers/store-scan", json={
            "employee_barcode": _emp_code(cutter[0]),
            "drawer_barcode": drawer.code, "piece_barcode": piece.code})
        assert r.status_code == 200, r.text
    assert r.json()["state"] == "received"      # auto, on completeness

    # and releasing a batch — the decision the role exists to own
    r = await api_client.post(f"{API}/drawers/send", json={
        "drawer_ids": [str(drawer.id)], })
    assert r.status_code == 200, r.text
    assert r.json()["count_sent"] == 1


# ══════════════════════════════════════════════ what it CANNOT do
@pytest.mark.asyncio
async def test_the_store_manager_cannot_log_production(
    api_client, as_role, operations, pieces, cutter, leather_lot
):
    """Store functions only. A store login that could also log cutting would make
    the separation the client asked for cosmetic."""
    piece, _ = pieces[0]
    as_role(UserRole.STORE_MANAGER)

    r = await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(cutter[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY,
        "consumption": {"leather_lot_id": str(leather_lot.id), "dcm": 12.0},
    })
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_the_store_manager_cannot_reach_the_production_reads(
    api_client, as_role, operations, pieces
):
    piece, _ = pieces[0]
    as_role(UserRole.STORE_MANAGER)
    for path in (f"{API}/production/operations",
                 f"{API}/production/events",
                 f"{API}/production/piece-state?code={piece.code}"):
        r = await api_client.get(path)
        assert r.status_code == 403, f"{path} -> {r.status_code}"


@pytest.mark.asyncio
async def test_a_floor_manager_may_scan_but_may_not_release(
    api_client, as_role, cut_pieces, cutter
):
    """The other side of the boundary: the cutting manager fills drawers and
    reads the list, but does not decide what leaves the store."""
    piece, drawer = cut_pieces[0]
    as_role(UserRole.CUTTING_MANAGER)

    assert (await api_client.get(f"{API}/drawers")).status_code == 200
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code})
    assert r.status_code == 200, r.text

    r = await api_client.post(f"{API}/drawers/send", json={
        "drawer_ids": [str(drawer.id)], })
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_a_viewer_reaches_none_of_the_store(api_client, as_role, pieces):
    _, drawer = pieces[0]
    as_role(UserRole.VIEWER)
    assert (await api_client.get(f"{API}/drawers")).status_code == 403
    assert (await api_client.get(f"{API}/drawers/{drawer.id}")).status_code == 403
    r = await api_client.post(f"{API}/drawers/send", json={
        "drawer_ids": [str(drawer.id)], })
    assert r.status_code == 403


# ══════════════════════════════════════════════ the role is a real login role
def test_store_manager_may_hold_a_login_and_is_grantable():
    """A role that cannot be granted cannot be used. MD grants every login role;
    DM and HR are given it explicitly so store staff can be onboarded without
    escalating to the MD."""
    from app.modules.users.service import UserService

    assert UserRole.STORE_MANAGER in UserRole.login_roles()
    for granter in (UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER,
                    UserRole.HR):
        assert UserRole.STORE_MANAGER in UserService._GRANTABLE[granter], granter


def test_store_manager_owns_no_production_stage_and_no_screen():
    """The mechanism behind the 403s above: there is no stage it may log and no
    screen its scan resolves to. If either map ever gains STORE_MANAGER, the
    separation the client asked for is gone — so assert on the maps, not just on
    the status codes."""
    from app.core.enums import (
        ROLE_TO_SCREEN, STAGE_ROLE_ACCESS, ScreenContext, screen_for_role,
    )

    assert UserRole.STORE_MANAGER not in ROLE_TO_SCREEN
    for stage, roles in STAGE_ROLE_ACCESS.items():
        assert UserRole.STORE_MANAGER not in roles, stage
    # It falls through to PIPELINE like any unmapped role, and PIPELINE is
    # exactly where GATE 1 then refuses it.
    assert screen_for_role(UserRole.STORE_MANAGER) is ScreenContext.PIPELINE
