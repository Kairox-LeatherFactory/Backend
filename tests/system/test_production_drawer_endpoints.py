"""
SYSTEM/E2E · the production + drawer HTTP surface: status codes and body shapes.

WHY AN ENDPOINT-SHAPE FILE
    Service-level tests prove the logic; they do NOT prove the wire contract.
    Three of the bugs this file now pins were invisible below the router:

      • GET  /production/skus/{id}/pieces  answered `null` (the service's
        envelope `return` had been dropped) and 500'd on a mis-unpacked row
      • POST /production/log dropped `skill_warnings` — the service returned the
        warnings, the response model had no field for them
      • GET  /production/styles/{id}/progress answered 200 + {} across tenants

    Everything here goes through the real FastAPI app.
"""
import datetime

import pytest

from app.core.enums import DrawerPart, DrawerState, UserRole

API = "/api/v1"
TODAY = datetime.date.today().isoformat()

pytestmark = pytest.mark.asyncio


async def _cut(api_client, as_role, piece, emp, lot):
    as_role(UserRole.CUTTING_MANAGER)
    return await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(emp.id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY,
        "consumption": {"leather_lot_id": str(lot.id), "dcm": 12.0},
    })


# ══════════════════════════════════════════════════════ POST /production/log
async def test_log_returns_201_and_the_full_result_body(
    api_client, as_role, operations, pieces, cutter, leather_lot
):
    piece, _ = pieces[0]
    r = await _cut(api_client, as_role, piece, cutter[0], leather_lot)
    assert r.status_code == 201, r.text

    body = r.json()
    assert body["stage"] == "LEATHER_CUTTING"
    assert body["count_logged"] == 1 and body["logged"] == [piece.code]
    assert body["preview"] is False
    assert body["consumption_recorded"]["qty"] == 12.0
    # every bucket is present, so the frontend can render them unconditionally
    for bucket in ("rework", "not_found", "sequence_blocked", "skill_blocked",
                   "merge_blocked", "skill_warnings"):
        assert bucket in body and isinstance(body[bucket], list)


async def test_log_surfaces_skill_warnings_on_the_wire(
    api_client, as_role, operations, pieces, paster, leather_lot
):
    """GATE 2 is a warning — but a warning nobody receives is not a warning.
    The response model had no `skill_warnings` field, so it was silently
    dropped between the service and the floor."""
    piece, _ = pieces[0]
    r = await _cut(api_client, as_role, piece, paster[0], leather_lot)
    assert r.status_code == 201, r.text

    body = r.json()
    assert body["count_logged"] == 1              # logged anyway
    warning = body["skill_warnings"][0]
    assert warning["designation"] == "PASTER"
    assert warning["stage"] == "LEATHER_CUTTING"
    assert warning["piece"] == piece.code


async def test_log_preview_writes_nothing_and_echoes_preview(
    api_client, as_role, operations, pieces, cutter, leather_lot
):
    piece, _ = pieces[0]
    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_barcode": "EMP-" + str(cutter[0].id)[:6].upper()},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY, "preview": True,
        "consumption": {"leather_lot_id": str(leather_lot.id), "dcm": 12.0},
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["preview"] is True
    assert body["logged"] == [piece.code]
    assert body["consumption_recorded"] is None

    events = await api_client.get(f"{API}/production/events")
    assert events.json() == []


async def test_log_rejects_a_stage_the_role_does_not_own(
    api_client, as_role, operations, pieces, cutter, leather_lot
):
    """The piece is cut first, so PIPELINE infers FUSING — a stage the stitching
    manager does not own (CLAUDE.md §3: the cutting manager logs fusing)."""
    piece, _ = pieces[0]
    await _cut(api_client, as_role, piece, cutter[0], leather_lot)

    as_role(UserRole.STITCHING_MANAGER)
    r = await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(cutter[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY,
    })
    assert r.status_code == 403, r.text
    assert "fusing" in r.json()["detail"].lower()


async def test_an_uncut_piece_on_the_pipeline_screen_is_reported_not_fatal(
    api_client, as_role, operations, pieces, cutter
):
    """No 4xx: the piece has simply not been cut, and cutting is logged on the
    cut screen. It comes back in sequence_blocked so a batch of good pieces
    scanned alongside it still logs."""
    piece, _ = pieces[0]
    as_role(UserRole.STITCHING_MANAGER)
    r = await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(cutter[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY,
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["count_logged"] == 0
    assert body["sequence_blocked"] == [piece.code]


async def test_log_requires_an_actor_and_a_target(api_client, as_role, operations):
    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/production/log", json={
        "actor": {}, "targets": {}, "work_date": TODAY})
    assert r.status_code == 422


# ══════════════════════════════════════ GET /production/skus/{id}/pieces
async def test_the_piece_checklist_endpoint_returns_its_envelope(
    api_client, as_role, operations, pieces, order_tree, cutter, leather_lot
):
    piece, _ = pieces[0]
    await _cut(api_client, as_role, piece, cutter[0], leather_lot)

    as_role(UserRole.SUPERVISOR)
    r = await api_client.get(f"{API}/production/skus/{order_tree['sku'].id}/pieces")
    assert r.status_code == 200, r.text

    body = r.json()
    assert body is not None, "endpoint answered null — the envelope return is gone"
    assert body["total"] == 5 and body["pending"] == 5
    assert body["sku_code"] == "JP-CLERMONT-PINE-M"
    assert len(body["pieces"]) == 5
    assert body["pieces"][0]["event_stage"] == "LEATHER_CUTTING"


async def test_the_checklist_404s_an_unknown_sku(api_client, as_role):
    import uuid
    as_role(UserRole.SUPERVISOR)
    r = await api_client.get(f"{API}/production/skus/{uuid.uuid4()}/pieces")
    assert r.status_code == 404


# ══════════════════════════════════ GET /production/styles/{id}/progress
async def test_style_progress_404s_an_unknown_style(api_client, as_role):
    import uuid
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/production/styles/{uuid.uuid4()}/progress")
    assert r.status_code == 404, (
        "an unknown style answered 200 — an empty body is indistinguishable "
        "from 'no work logged yet' and confirms the id is real")


async def test_style_progress_counts_events(
    api_client, as_role, operations, pieces, order_tree, cutter, leather_lot
):
    piece, _ = pieces[0]
    await _cut(api_client, as_role, piece, cutter[0], leather_lot)
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(
        f"{API}/production/styles/{order_tree['style'].id}/progress")
    assert r.status_code == 200
    assert r.json()["LEATHER_CUTTING"] == 1


# ══════════════════════════════════════════════════════ drawers
async def test_store_scan_through_the_barcode_door(
    api_client, as_role, pieces
):
    """Scan the drawer, then the piece — codes, not ids."""
    piece, drawer = pieces[0]
    as_role(UserRole.CUTTING_MANAGER)

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LEATHER"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == DrawerState.HOLDING_LEATHER.value
    assert body["awaiting"] == ["LINING"]
    assert body["ready_for_received"] is False

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LINING"})
    assert r.json()["state"] == DrawerState.HOLDING_BOTH.value
    assert r.json()["ready_for_received"] is True


async def test_store_scan_rejects_a_bad_part_and_a_missing_door(
    api_client, as_role, pieces
):
    piece, drawer = pieces[0]
    as_role(UserRole.CUTTING_MANAGER)

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "ZIPPER"})
    assert r.status_code == 422                    # part is pattern-constrained

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "piece_barcode": piece.code, "part": "LEATHER"})
    assert r.status_code == 422                    # no drawer door given


async def test_store_scan_409s_a_piece_in_the_wrong_drawer(
    api_client, as_role, pieces
):
    piece, _ = pieces[0]
    _, other = pieces[1]
    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "drawer_barcode": other.code, "piece_barcode": piece.code,
        "part": "LEATHER"})
    assert r.status_code == 409


async def test_receive_is_dm_md_only_and_enforces_the_order(
    api_client, as_role, pieces
):
    piece, drawer = pieces[0]

    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "RECEIVED"})
    assert r.status_code == 403                    # floor cannot release the gate

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "RECEIVED"})
    assert r.status_code == 409                    # nothing in the drawer yet

    as_role(UserRole.CUTTING_MANAGER)
    for part in ("LEATHER", "LINING"):
        await api_client.post(f"{API}/drawers/store-scan", json={
            "drawer_barcode": drawer.code, "piece_barcode": piece.code,
            "part": part})

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "SENDED"})
    assert r.status_code == 409                    # SENDED before RECEIVED

    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "RECEIVED"})
    assert r.status_code == 200 and r.json()["state"] == DrawerState.RECEIVED.value
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "SENDED"})
    assert r.status_code == 200 and r.json()["state"] == DrawerState.SENDED.value


async def test_the_drawer_label_sheet(api_client, as_role, pieces):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/drawers", params={"seq_from": 1, "seq_to": 3})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3 and body["count"] == 3
    assert [i["seq"] for i in body["items"]] == [1, 2, 3]
    assert all(i["barcode"] == i["code"] for i in body["items"])

    r = await api_client.get(f"{API}/drawers", params={"state": "not_a_state"})
    assert r.status_code == 422
