"""
SYSTEM/E2E · the send gate, over HTTP, exactly as the frontend drives it.

THE RULE
    A piece may not enter LINE_STITCHING until its drawer has been SENT. Not when
    the drawer is holding both parts, not when it has been received — sent. The
    send is the release, and this file proves it end to end through the real
    routes rather than at service level.

WHY IT NEEDS ITS OWN FILE
    The pieces of this were covered separately — the merge gate in one place, the
    batch send in another — so nothing asserted the WHOLE path a manager actually
    walks: scan both parts, watch the drawer receive itself, send it, then log
    line-stitching and have it accepted. A gap anywhere in that chain leaves the
    floor unable to move a garment, and every individual test still passing.
"""
import datetime

import pytest

from app.core.enums import UserRole

API = "/api/v1"
TODAY = datetime.date.today().isoformat()

pytestmark = pytest.mark.asyncio


def _emp(e):
    return f"EMP-{str(e.id)[:6].upper()}"


async def _log(api_client, as_role, role, piece, emp, screen=None, lot=None,
               dcm=None):
    as_role(role)
    body = {"actor": {"employee_id": str(emp.id)},
            "targets": {"piece_barcodes": [piece.code]},
            "work_date": TODAY}
    if screen:
        body["screen_context"] = screen
    if lot is not None:
        body["consumption"] = {"leather_lot_id": str(lot.id), "dcm": dcm or 12.0}
    return await api_client.post(f"{API}/production/log", json=body)


async def _scan_in(api_client, as_role, drawer, piece, emp, part=None):
    as_role(UserRole.CUTTING_MANAGER)
    body = {"employee_barcode": _emp(emp), "drawer_barcode": drawer.code,
            "piece_barcode": piece.code}
    if part:
        body["part"] = part
    return await api_client.post(f"{API}/drawers/store-scan", json=body)


async def _to_the_store(api_client, as_role, piece, cutter, paster, lot,
                        lining_cutter=None):
    """Run BOTH cut paths to their ends, so the piece is ready for the store.

    Both, not just the leather one: the store is where the two paths meet, and
    each half is only admitted once its own path is finished — leather after
    PASTING, lining after LINING_CUTTING. Cutting the leather and stopping was
    enough when the drawer accepted anything; it now buys a 409 on the lining
    scan.

    Pass lining_cutter=None for a genuinely leather-only garment. Do NOT log a
    lining cut for one: a cut lining is a physical fact that outranks
    needs_lining=False (core/lining_rules), so it would silently re-line the very
    piece the test is about.
    """
    r = await _log(api_client, as_role, UserRole.CUTTING_MANAGER, piece, cutter,
                   screen="LEATHER_CUT", lot=lot)
    assert r.status_code == 201, r.text
    for _ in range(2):                       # FUSING then PASTING
        r = await _log(api_client, as_role, UserRole.STITCHING_MANAGER, piece,
                       paster)
        assert r.status_code == 201, r.text
    if lining_cutter is None:
        return          # leather-only garment: there is no second path to run
    r = await _log(api_client, as_role, UserRole.LINING_MANAGER, piece,
                   lining_cutter, screen="LINING_CUT")
    assert r.status_code == 201, r.text


# ══════════════════════════════════════════════ the happy path, whole
async def test_line_stitching_opens_only_after_the_drawer_is_sent(
    api_client, as_role, operations, pieces, cutter, lining_cutter, paster, tailor,
    leather_lot
):
    piece, drawer = pieces[0]
    await _to_the_store(api_client, as_role, piece, cutter[0], paster[0],
                        leather_lot, lining_cutter[0])

    # 1 · both parts in — the drawer receives itself, and is NOT yet sent
    r = await _scan_in(api_client, as_role, drawer, piece, cutter[0], "LEATHER")
    assert r.status_code == 200, r.text
    assert r.json()["auto_received"] is False
    r = await _scan_in(api_client, as_role, drawer, piece, cutter[0], "LINING")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "received" and body["auto_received"] is True
    assert body["sent"] is False

    # 2 · received is NOT a release — line-stitching must still refuse
    r = await _log(api_client, as_role, UserRole.STITCHING_MANAGER, piece,
                   tailor[0])
    assert r.status_code == 201, r.text
    blocked = r.json()
    assert blocked["count_logged"] == 0, "RECEIVED must not open the gate"
    assert blocked["merge_blocked"] == [piece.code]
    assert drawer.code in blocked["blocked"][0]["reason"]

    # 3 · send it
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/send",
                              json={"drawer_ids": [str(drawer.id)]})
    assert r.status_code == 200, r.text
    sent = r.json()
    assert sent["count_sent"] == 1
    assert sent["pieces_released"] == [piece.code]

    # 4 · NOW line-stitching is accepted
    r = await _log(api_client, as_role, UserRole.STITCHING_MANAGER, piece,
                   tailor[0])
    assert r.status_code == 201, r.text
    logged = r.json()
    assert logged["stage"] == "LINE_STITCHING", logged
    assert logged["count_logged"] == 1
    assert logged["logged"] == [piece.code]

    # 5 · and the chain continues from there
    r = await _log(api_client, as_role, UserRole.STITCHING_MANAGER, piece,
                   tailor[0])
    assert r.json()["stage"] == "SHELL_STITCHING"


async def test_a_batch_send_releases_every_piece_in_it(
    api_client, as_role, operations, pieces, cutter, lining_cutter, paster, tailor,
    leather_lot
):
    """The whole point of the batch: many drawers, one action, all released."""
    chosen = pieces[:3]
    for piece, drawer in chosen:
        await _to_the_store(api_client, as_role, piece, cutter[0], paster[0],
                            leather_lot, lining_cutter[0])
        for part in ("LEATHER", "LINING"):
            r = await _scan_in(api_client, as_role, drawer, piece, cutter[0], part)
            assert r.status_code == 200, r.text

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/send", json={
        "drawer_ids": [str(d.id) for _, d in chosen]})
    assert r.status_code == 200, r.text
    assert r.json()["count_sent"] == 3

    for piece, _ in chosen:
        r = await _log(api_client, as_role, UserRole.STITCHING_MANAGER, piece,
                       tailor[0])
        assert r.json()["stage"] == "LINE_STITCHING", piece.code


# ══════════════════════════════════════════════ the request shape
async def test_send_takes_only_drawer_ids(api_client, as_role, pieces, cutter,
                                          lining_cutter, paster, leather_lot,
                                          operations):
    """No destination. Sending the old body must not be required, and sending an
    unexpected extra field must not break the call."""
    piece, drawer = pieces[0]
    await _to_the_store(api_client, as_role, piece, cutter[0], paster[0],
                        leather_lot, lining_cutter[0])
    for part in ("LEATHER", "LINING"):
        await _scan_in(api_client, as_role, drawer, piece, cutter[0], part)

    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/send",
                              json={"drawer_ids": [str(drawer.id)]})
    assert r.status_code == 200, r.text
    assert "destination" not in r.json()


async def test_an_empty_selection_is_refused(api_client, as_role, pieces):
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/send", json={"drawer_ids": []})
    assert r.status_code == 422


# ══════════════════════════════════════════════ the case that strands a garment
async def test_a_piece_needing_no_lining_can_still_reach_line_stitching(
    api_client, as_role, operations, pieces, cutter, lining_cutter, paster, tailor,
    leather_lot,
    db
):
    """THE ONE THAT BREAKS THE FLOOR IF IT REGRESSES.

    Auto-receive needs BOTH parts. A piece flagged needs_lining=False will never
    get a second part, so it never auto-receives — and if nothing else can move
    it, its drawer can never be sent and the garment is stuck before
    line-stitching forever. On live, most pieces currently carry that flag.

    So there must be a reachable path from 'leather in, complete, not received'
    to 'sent'.
    """
    piece, drawer = pieces[0]
    piece.needs_lining = False
    await db.commit()

    await _to_the_store(api_client, as_role, piece, cutter[0], paster[0],
                        leather_lot)          # leather side only — see the helper
    r = await _scan_in(api_client, as_role, drawer, piece, cutter[0], "LEATHER")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ready_for_received"] is True     # complete...
    assert body["auto_received"] is False         # ...but not received

    # The drawer is complete, so the send must be able to take it — otherwise the
    # garment is stranded with no action available anywhere in the UI.
    as_role(UserRole.DIRECT_MANAGER)
    r = await api_client.post(f"{API}/drawers/send",
                              json={"drawer_ids": [str(drawer.id)]})
    assert r.status_code == 200, r.text
    assert r.json()["count_sent"] == 1, (
        "a complete leather-only drawer must be sendable; otherwise nothing in "
        "the UI can move this garment and it never reaches line-stitching")

    r = await _log(api_client, as_role, UserRole.STITCHING_MANAGER, piece,
                   tailor[0])
    assert r.json()["stage"] == "LINE_STITCHING"
