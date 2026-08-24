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
    """The piece is cut first, so PIPELINE infers FUSING — a stage the SUPERVISOR
    does not own.

    This used to use the stitching manager, back when FUSING was granted to the
    cutting manager. That grant was unreachable (the cutting manager's screen is
    pinned to LEATHER_CUT), so FUSING now belongs to the stitching manager and it
    is no longer a role that can demonstrate the gate. The supervisor is: they
    sit on the PIPELINE screen, so their scan reaches the stage, and they appear
    in no STAGE_ROLE_ACCESS set at all."""
    piece, _ = pieces[0]
    await _cut(api_client, as_role, piece, cutter[0], leather_lot)

    as_role(UserRole.SUPERVISOR)
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
def _emp_code(emp) -> str:
    """The employee card the conftest mints (tests/conftest.py:171)."""
    return f"EMP-{str(emp.id)[:6].upper()}"


async def test_store_scan_through_the_barcode_door(
    api_client, as_role, cut_pieces, cutter
):
    """Scan the employee, then the drawer, then the piece — codes, not ids."""
    piece, drawer = cut_pieces[0]
    as_role(UserRole.CUTTING_MANAGER)

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LEATHER"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == DrawerState.HOLDING_LEATHER.value
    assert body["awaiting"] == ["LINING"]
    assert body["ready_for_received"] is False
    # BUG #15: the scan logs the part into the drawer and nothing more.
    assert body["sent"] is False

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LINING"})
    body = r.json()
    # Complete → auto-RECEIVED (bug #13); contents still report BOTH.
    assert body["state"] == DrawerState.RECEIVED.value
    assert body["holding"] == "HOLDING BOTH"
    assert body["ready_for_received"] is True
    assert body["sent"] is False


async def test_store_scan_requires_the_employee_barcode_first(
    api_client, as_role, pieces
):
    """BUG #2 — the employee scan is mandatory, and enforced HERE.

    The UI was asked to keep the other inputs disabled until an employee is
    scanned. A rule that lives only in the UI is not a rule: this endpoint used
    to accept a drawer fill from nobody at all, so "who put this here" was
    unanswerable for the one write on the floor that has no other actor.
    """
    piece, drawer = pieces[0]
    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LEATHER"})
    assert r.status_code == 422
    assert "employee" in r.text.lower()


async def test_store_scan_infers_the_hold_bucket(api_client, as_role, pieces, cutter,
                                                ready_for_store):
    """BUG #18 — no Hold Leather / Hold Lining button. `part` is omitted and the
    server decides, reporting which bucket it chose.

    Each side is finished just before its own scan, which is both the real floor
    order and the only setup that pins the answer: once BOTH cuts are done the
    empty drawer is genuinely ambiguous and the server answers LINING (it reads
    the lining cut first)."""
    piece, drawer = pieces[0]
    as_role(UserRole.CUTTING_MANAGER)

    await ready_for_store(piece, lining=False)          # leather side finished
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["part_inferred"] is True
    assert body["part"] == "LEATHER"        # only the leather is ready to store
    assert body["holding"] == "HOLDING LEATHER"

    # Second scan fills the other side without being told either.
    await ready_for_store(piece, leather=False)         # …now the lining is cut
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code})
    body = r.json()
    assert body["part"] == "LINING" and body["part_inferred"] is True
    assert body["holding"] == "HOLDING BOTH"

    # Nothing left to scan in — a third scan is a 409, not a silent no-op.
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code})
    assert r.status_code == 409


async def test_store_scan_rejects_a_bad_part_and_a_missing_door(
    api_client, as_role, pieces, cutter
):
    piece, drawer = pieces[0]
    as_role(UserRole.CUTTING_MANAGER)

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "ZIPPER"})
    assert r.status_code == 422                    # part is pattern-constrained

    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "piece_barcode": piece.code, "part": "LEATHER"})
    assert r.status_code == 422                    # no drawer door given


async def test_store_scan_409s_a_piece_in_the_wrong_drawer(
    api_client, as_role, pieces, cutter
):
    piece, _ = pieces[0]
    _, other = pieces[1]
    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": other.code, "piece_barcode": piece.code,
        "part": "LEATHER"})
    assert r.status_code == 409


async def test_batch_send_releases_many_drawers_at_once(
    api_client, as_role, cut_pieces, cutter
):
    """BUGS #13/#14/#15 — completeness auto-receives; SEND is the manual step,
    it takes many drawers, and it partially accepts."""
    ready, not_ready = cut_pieces[0], cut_pieces[1]

    as_role(UserRole.CUTTING_MANAGER)
    for part in ("LEATHER", "LINING"):
        r = await api_client.post(f"{API}/drawers/store-scan", json={
            "employee_barcode": _emp_code(cutter[0]),
            "drawer_barcode": ready[1].code, "piece_barcode": ready[0].code,
            "part": part})
        assert r.status_code == 200, r.text
    # the second drawer gets leather only — still awaiting its lining
    await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": not_ready[1].code, "piece_barcode": not_ready[0].code,
        "part": "LEATHER"})

    # the send queue shows exactly the one that is ready
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/drawers", params={"sendable": True})
    assert [i["code"] for i in r.json()["items"]] == [ready[1].code]

    # a floor role may scan parts in but may not decide what leaves the store
    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/drawers/send", json={
        "drawer_ids": [str(ready[1].id)]})
    assert r.status_code == 403

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/send", json={
        "drawer_ids": [str(ready[1].id), str(not_ready[1].id)],
        })
    assert r.status_code == 200, r.text
    body = r.json()
    # PARTIAL ACCEPT: the good drawer goes, the incomplete one is reported.
    assert body["count_sent"] == 1
    assert body["sent"][0]["drawer_code"] == ready[1].code
    assert body["pieces_released"] == [ready[0].code]
    assert len(body["not_ready"]) == 1
    assert body["not_ready"][0]["drawer_code"] == not_ready[1].code


async def test_drawer_detail_opens_a_row_from_the_list(
    api_client, as_role, cut_pieces, cutter
):
    """BUG #13 — the Drawers List row must open into real detail."""
    piece, drawer = cut_pieces[0]
    as_role(UserRole.CUTTING_MANAGER)
    await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LEATHER"})

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.get(f"{API}/drawers/{drawer.id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == drawer.code
    assert body["holding"] == "HOLDING LEATHER"
    assert body["awaiting"] == ["LINING"]
    assert body["complete"] is False and body["can_send"] is False
    # the garment itself, with the fields bug #7 asked for
    assert body["piece"]["code"] == piece.code
    assert body["piece"]["serial"] == "001"


async def test_receive_is_dm_md_only_and_enforces_the_order(
    api_client, as_role, cut_pieces, cutter
):
    """The DEPRECATED single-drawer route still behaves, for one release."""
    piece, drawer = cut_pieces[0]

    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "RECEIVED"})
    assert r.status_code == 403                    # floor cannot release the gate

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "RECEIVED"})
    assert r.status_code == 409                    # nothing in the drawer yet

    as_role(UserRole.CUTTING_MANAGER)
    r = await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LEATHER"})
    assert r.status_code == 200, r.text

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "SENDED"})
    assert r.status_code == 409                    # SENDED before RECEIVED

    # completing it auto-receives, so the explicit RECEIVED is now a no-op that
    # still answers 200 — the deprecated route must not start failing.
    as_role(UserRole.CUTTING_MANAGER)
    await api_client.post(f"{API}/drawers/store-scan", json={
        "employee_barcode": _emp_code(cutter[0]),
        "drawer_barcode": drawer.code, "piece_barcode": piece.code,
        "part": "LINING"})

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "RECEIVED"})
    assert r.status_code == 200 and r.json()["state"] == DrawerState.RECEIVED.value
    r = await api_client.post(f"{API}/drawers/{drawer.id}/receive",
                              json={"transition": "SENDED"})
    assert r.status_code == 200 and r.json()["state"] == DrawerState.SENDED.value


async def test_the_drawer_label_sheet(api_client, as_role, pieces):
    as_role(UserRole.DIRECT_MANAGER)
    # sort=seq EXPLICITLY. The default is "recent", which orders by
    # coalesce(last_activity_at, updated_at, created_at) DESC and only falls back
    # to seq on a TIE. SQLite's CURRENT_TIMESTAMP has one-second resolution, so
    # the five drawers this fixture creates tie — until the loop happens to
    # straddle a second boundary, at which point they come back newest-first and
    # this assertion fails for reasons that have nothing to do with the endpoint.
    # The label SHEET is a seq-ordered question, so ask for seq order.
    r = await api_client.get(f"{API}/drawers",
                             params={"seq_from": 1, "seq_to": 3, "sort": "seq"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3 and body["count"] == 3
    assert [i["seq"] for i in body["items"]] == [1, 2, 3]
    assert all(i["barcode"] == i["code"] for i in body["items"])

    r = await api_client.get(f"{API}/drawers", params={"state": "not_a_state"})
    assert r.status_code == 422
