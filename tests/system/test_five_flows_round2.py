"""
SYSTEM · five more end-to-end journeys, through the real HTTP app.

THE FIRST FIVE (test_five_flows_e2e.py) walked a garment. These five walk the
SERVICES — every module the factory touches in a week, in the order a real day
touches them, so that a module which only ever gets exercised by its own tests
gets driven from outside at least once.

WHY THAT MATTERS HERE MORE THAN USUAL. A FastAPI response_model silently DROPS
any key the schema does not declare. A service test asserting `out["sheet"]`
passes while the operator's screen shows nothing, because the service was right
and the contract was wrong. That class of bug is invisible below this layer and
has bitten this codebase four times. So these flows assert on the JSON THE
BROWSER RECEIVES, never on the service return value.

    FLOW  6  a new hire: card minted, first day scanned, corrected, then gone
    FLOW  7  a new client's order becomes barcodes on the floor
    FLOW  8  hides arrive, are cut on the grid, and are merged in the store
    FLOW  9  a rush order goes to an outside factory and comes back short
    FLOW 10  the mistake day — every correction surface, in one story

BETWEEN THEM THEY COVER: employees, attendance, barcode, clients, imports,
production, store, drawers (withdrawn), cutting, materials, jobwork, analytics,
dashboard and wages.
"""
import datetime
import uuid

import pytest
import pytest_asyncio

from app.core.enums import ProductionStage, UserRole
from app.modules.production.models import Operation, Piece, ProductionEvent

pytestmark = pytest.mark.asyncio
API = "/api/v1"
TODAY = datetime.date.today().isoformat()


# ══════════════════════════════════════════════════════════════ scaffolding
def _ok(r, *allowed):
    """Assert a response AND SHOW THE BODY when it is not what we expected.

    A bare `assert r.status_code == 200` against a 500 tells you nothing, and
    seeing the error the user would have seen is the entire reason for driving
    the HTTP layer instead of the service.
    """
    allowed = allowed or (200, 201)
    assert r.status_code in allowed, (
        f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:700]}")
    return r.json()


def _piece(pieces, i=0):
    return pieces[i][0] if isinstance(pieces[i], tuple) else pieces[i]


@pytest_asyncio.fixture
async def full_ops(db):
    """Every stage in the chain has an Operation row.

    The shared `operations` fixture covers the common ones; a flow that walks a
    garment past final finish needs the tail as well.
    """
    from sqlalchemy import select
    have = {c for (c,) in (await db.execute(select(Operation.code))).all()}
    for i, stage in enumerate(ProductionStage.leather_chain(), 1):
        if stage.value not in have:
            db.add(Operation(id=uuid.uuid4(), code=stage.value,
                             label=stage.value.title(), sequence=100 + i))
    if ProductionStage.LINING_CUTTING.value not in have:
        db.add(Operation(id=uuid.uuid4(), code=ProductionStage.LINING_CUTTING.value,
                         label="Lining Cutting", sequence=150))
    await db.commit()
    rows = (await db.execute(select(Operation))).scalars().all()
    return {op.code: op for op in rows}


async def _raw_event(db, ops, piece, employee_id, code, work_date=None):
    """A stage written directly — only ever used to put a garment INTO the state
    a flow is about, never to prove the thing the flow asserts."""
    db.add(ProductionEvent(
        sku_id=piece.sku_id, operation_id=ops[code].id, employee_id=employee_id,
        work_date=work_date or datetime.date.today(), qty=1, piece_id=piece.id,
        entered_by="setup"))
    await db.commit()


async def _through_the_cut_side(db, ops, piece, employee_id):
    """Cut, fused and pasted — the state a garment reaches the store in."""
    for code in ("LEATHER_CUTTING", "FUSING", "PASTING"):
        await _raw_event(db, ops, piece, employee_id, code)


# ══════════════════════════════════════════════════════════════════ FLOW 6
async def test_flow6_a_new_hire_from_first_card_to_last_day(
        api_client, as_role, db, full_ops, pieces, order_tree, leather_lot):
    """HR hires a cutter; SECURITY scans them in; they cut; they leave.

    THE INVARIANT UNDER TEST IS THE LAST STEP. When a worker leaves, the card
    stops scanning but the PERSON and everything they made stay in the database
    forever — wage lines reference them, and a production event with no worker
    is an unanswerable question six months later. You delete the scannable
    code, never the record.
    """
    # ── HR creates the worker. No login: a floor worker has none, so no phone,
    #    no email and no password are required — just a name and a designation.
    as_role(UserRole.HR)
    emp = _ok(await api_client.post(f"{API}/employees", json={
        "name": "SURESH KUMAR", "designation": "cutter",
        "wage_type": "piece_rate"}))
    emp_id = emp["id"]
    card = emp.get("employee_barcode")
    assert card, ("the card code must come back on create — it is printed "
                  "immediately and there is no second endpoint to fetch it")

    # ── the card scans, and says who it belongs to.
    as_role(UserRole.SECURITY)
    res = _ok(await api_client.get(f"{API}/barcode/resolve", params={"code": card}))
    assert res["type"] == "EMPLOYEE"

    # ── SECURITY scans them in at the gate.
    punch = _ok(await api_client.post(f"{API}/attendance/scan-check-in", json={
        "employee_barcode": card, "direction": "in"}))
    assert punch.get("employee_id") in (emp_id, None) or punch.get("employee_name")

    roster = _ok(await api_client.get(f"{API}/attendance/today"))
    rows = roster if isinstance(roster, list) else roster.get("rows", roster.get("present", []))
    assert any(str(r.get("employee_id")) == emp_id for r in rows), \
        "a worker scanned in at the gate must appear on today's roster"

    # ── they cut a garment. The presence gate is what the scan above satisfied.
    as_role(UserRole.CUTTING_MANAGER)
    piece = _piece(pieces)
    log = _ok(await api_client.post(f"{API}/production/log", json={
        "screen_context": "LEATHER_CUT",
        "actor": {"employee_barcode": card},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY,
        # The cut stage REFUSES to log without a quantity, and it is right to:
        # the material ledger and the costing are both built on this number.
        "consumption": {"leather_lot_id": str(leather_lot.id), "dcm": 400}}))
    assert log["logged"] == [piece.code]
    assert log["stage"] == "LEATHER_CUTTING"

    # ── HR notices the name was typed wrong.
    as_role(UserRole.HR)
    fixed = _ok(await api_client.patch(f"{API}/employees/{emp_id}",
                                       json={"name": "SURESH KUMARAN"}))
    assert fixed["name"] == "SURESH KUMARAN"
    assert fixed["employee_barcode"] == card, \
        "correcting a spelling must not reissue the card the worker is carrying"

    # ── the single-employee read HR's edit screen opens on.
    one = _ok(await api_client.get(f"{API}/employees/{emp_id}"))
    assert one["name"] == "SURESH KUMARAN"
    assert one["designation"] == "CUTTER", "designations are a controlled vocabulary"

    # ── the worker leaves. DM removes them; HR may edit but not remove.
    gone = await api_client.delete(f"{API}/employees/{emp_id}")
    assert gone.status_code == 403, "removing a worker is not a clerical action"

    as_role(UserRole.DIRECT_MANAGER)
    out = _ok(await api_client.delete(f"{API}/employees/{emp_id}"))
    assert out["active"] is False

    # ── AND THE POINT: the card is dead, the history is not.
    dead = await api_client.get(f"{API}/barcode/resolve", params={"code": card})
    assert dead.status_code == 410, \
        "410 GONE — a retired card is a different answer from a code that never existed"

    from sqlalchemy import func, select
    still = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.employee_id == uuid.UUID(emp_id)))
    assert still == 1, "the work they did survives them leaving"

    survivor = _ok(await api_client.get(f"{API}/employees/{emp_id}"))
    assert survivor["is_active"] is False, "the person is still readable, just inactive"


# ══════════════════════════════════════════════════════════════════ FLOW 7
async def test_flow7_a_new_clients_order_becomes_barcodes_on_the_floor(
        api_client, as_role, db, full_ops, cutter):
    """DM registers a buyer, adds an order, and the pieces reach the floor.

    AND THE DRAWER IS GONE. There are 200 physical drawers and a style releases
    more pieces than that, so the drawer was a bottleneck that earned nothing.
    This flow asserts the replacement is reachable and the old surface is not —
    a half-removed module is worse than either state, because the floor would
    have two places to look and one of them would be lying.
    """
    as_role(UserRole.DIRECT_MANAGER)

    # ── the buyer, and their first order in the same call.
    client = _ok(await api_client.post(f"{API}/clients", json={
        "name": "MAISON LEONE", "country": "IT", "order_number": "ML-2026-01"}))
    client_id = client["id"]

    # GET /clients is paged (core/pagination.py) — rows live in `items`.
    listed = _ok(await api_client.get(f"{API}/clients"))
    assert any(c["id"] == client_id for c in listed["items"])

    # ── a second order against the same buyer, with the dates freight risk reads.
    deadline = (datetime.date.today() + datetime.timedelta(days=40)).isoformat()
    order = _ok(await api_client.post(f"{API}/clients/{client_id}/orders", json={
        "order_number": "ML-2026-02", "delivery_deadline": deadline,
        "ship_mode": "sea", "currency": "EUR"}))
    assert order["order_number"] == "ML-2026-02"

    # Paged (core/pagination.py) — rows under `items`, true count under `total`.
    orders = _ok(await api_client.get(f"{API}/clients/{client_id}/orders"))
    assert {o["order_number"] for o in orders["items"]} == {"ML-2026-01", "ML-2026-02"}
    assert orders["total"] == 2

    # ── the buyer's own details are editable; the read is not write-only.
    patched = _ok(await api_client.patch(f"{API}/clients/{client_id}",
                                         json={"country": "FR"}))
    assert patched["country"] == "FR"
    assert _ok(await api_client.get(f"{API}/clients/{client_id}"))["name"] == "MAISON LEONE"

    # ── pieces minted the way breakdown upload mints them: garment, barcode,
    #    and a store state of WAITING — a piece is not in the store yet.
    from app.modules.barcode.models import BarcodeRegistry
    from app.modules.clients.models import SKU, Style
    from sqlalchemy import select
    o = (await db.execute(select(__import__(
        "app.modules.clients.models", fromlist=["ClientOrder"]).ClientOrder)
        .where(__import__("app.modules.clients.models",
                          fromlist=["ClientOrder"]).ClientOrder.order_number == "ML-2026-02")
    )).scalars().one()
    style = Style(client_order_id=o.id, name="LEONE", article="LN9",
                  production_status="RELEASED")
    db.add(style)
    await db.flush()
    sku = SKU(style_id=style.id, color_code="NERO", color_name="BLACK", size="L",
              qty_ordered=3, code="ML-LEONE-NERO-L")
    db.add(sku)
    await db.flush()
    codes = []
    for seq in range(1, 4):
        p = Piece(code=f"ML-LEONE-NERO-L-{seq:03d}", seq=seq, sku_id=sku.id)
        p.needs_lining = False
        p.store_state = "waiting"
        db.add(p)
        await db.flush()
        db.add(BarcodeRegistry(code=p.code, type="PIECE", status="ACTIVE",
                               piece_id=p.id, caption=p.code))
        codes.append(p.code)
    await db.commit()

    # ── every piece barcode resolves, and says what it is.
    for code in codes:
        r = _ok(await api_client.get(f"{API}/barcode/resolve", params={"code": code}))
        assert r["type"] == "PIECE"

    # ── the order screens the floor actually opens.
    skus = _ok(await api_client.get(f"{API}/barcode/orders/{o.id}/skus"))
    assert skus, "an order with SKUs must not come back empty"
    sheet = _ok(await api_client.get(f"{API}/barcode/orders/{o.id}/barcodes"))
    assert sheet is not None

    # ── THE DRAWER IS WITHDRAWN. Not renamed, not hidden — unrouted.
    from app.main import app
    drawer_routes = [r.path for r in app.routes
                     if getattr(r, "path", "").startswith(f"{API}/drawers")]
    assert drawer_routes == [], \
        f"the drawer module was withdrawn; these are still mounted: {drawer_routes}"

    # ── and the store is what replaced it: one scan fewer, keyed on the piece.
    as_role(UserRole.STORE_MANAGER)
    row = _ok(await api_client.get(f"{API}/store/pieces/{codes[0]}"))
    assert row["piece_code"] == codes[0]
    assert row["store_state"].upper() == "WAITING"
    assert row["needs_lining"] is False, \
        "a leather-only garment is complete on leather alone"


# ══════════════════════════════════════════════════════════════════ FLOW 8
async def test_flow8_hides_arrive_are_cut_on_the_grid_and_merge_in_the_store(
        api_client, as_role, db, full_ops, order_tree, pieces, cutter, tailor):
    """Receive hides with per-sheet barcodes → cut on the grid → merge → send.

    THE MD'S REQUIREMENT IS THE FIRST ASSERTION: a barcode for EACH leather
    sheet. One hide will not make one garment — it takes seven or more — so the
    lot-level barcode could never answer "which hides went into this jacket".

    THE LAST ASSERTION IS THE GATE. `_merge_ok` is the ONLY thing standing in
    front of LINE_STITCHING (`_sequence_ok` returns True early for it, because
    LINE_STITCHING has no chain predecessor by design). If the store state stops
    gating it, a garment could be line-stitched before it was ever cut and
    nothing in the system would contradict it.
    """
    as_role(UserRole.DIRECT_MANAGER)

    # ── a leather lot, delivered as seven hides measured one by one. A lot
    #    carries its quantity from the moment it exists — "a lot with 0 dcm" is
    #    not a thing the store can be holding.
    dcms = [44, 47, 46, 51, 43, 49, 45]
    lot = _ok(await api_client.post(f"{API}/materials/lots", json={
        "category": "LEATHER", "article": "NAPPA-77", "colour": "PINE GREEN",
        "attributes": {"thickness": "1.2mm", "dcm": float(sum(dcms))},
        "sheets": [{"dcm": d} for d in dcms]}))
    lot_id = lot["lot_id"]
    sheet_codes = [x["code"] for x in lot["sheets"]]
    assert len(sheet_codes) == 7, \
        "MD's requirement: one printable barcode per hide, not one per lot"
    assert lot["on_hand"] == float(sum(dcms))

    # ── a second delivery against the same lot, with two hides turned away at
    #    the door. Buying the same article/colour/thickness again is a TOP-UP,
    #    never a second lot row — that is what keeps the picker's promise.
    more = [48, 46, 47, 45, 44]
    recv = _ok(await api_client.post(f"{API}/materials/receive", json={
        "lot_id": lot_id, "approved_qty": float(sum(more)), "rejected_qty": 4.0,
        "sheets": [{"dcm": d} for d in more]}))
    sheet_codes += [x["code"] for x in recv["sheets"]]
    assert len(sheet_codes) == 12
    assert recv["on_hand"] == float(sum(dcms) + sum(more)), \
        "rejected hides are logged against the supplier, never added to stock"
    assert recv["rejected_logged"] == 4.0

    # ── each hide's barcode resolves to the hide AND names its parent lot.
    as_role(UserRole.CUTTING_MANAGER)
    one = _ok(await api_client.get(f"{API}/barcode/resolve",
                                   params={"code": sheet_codes[0]}))
    assert one["type"] == "LEATHER_SHEET"
    assert one.get("sheet"), \
        ("the resolved payload must carry the sheet — a response_model that "
         "drops it leaves the operator staring at a blank screen")

    # ── the grid: one row per un-cut garment, hides auto-allocated to the size.
    gen = _ok(await api_client.post(f"{API}/cutting/rows/generate", json={
        "style_id": str(order_tree["style"].id),
        "colour": "PINE GREEN", "material_lot_id": lot_id,
        "cutter_employee_id": str(cutter[0].id), "limit": 1, "work_date": TODAY}))
    assert gen["created"] == 1, "one row is ONE GARMENT"
    row = gen["rows"][0]
    row_id = row["row_id"]
    assert row["sheets"], "the allocator must have put hides on the row"
    allocated = len(row["sheets"])

    # ── the cutter calls for one more hide. Free and unaudited while DRAFT —
    #    this is the flow that was being done in Excel.
    on_row = {x["code"] for x in row["sheets"]}
    spare = next(c for c in sheet_codes if c not in on_row)
    added = _ok(await api_client.post(f"{API}/cutting/rows/{row_id}/sheets",
                                      json={"sheet_code": spare}))
    assert len(added["sheets"]) == allocated + 1
    assert float(added["total_dcm"]) > float(row["total_dcm"]), \
        "total_dcm is DERIVED from the hides on the row, never typed"

    # ── and hands one back. It returns to stock rather than vanishing.
    back = added["sheets"][0]
    drop = _ok(await api_client.delete(
        f"{API}/cutting/rows/{row_id}/sheets/{back['sheet_id']}"))
    assert len(drop["sheets"]) == allocated

    # ── the manager signs the row off. After this it is read-only.
    approved = _ok(await api_client.post(f"{API}/cutting/rows/{row_id}/approve"))
    assert approved["row"]["status"] == "APPROVED"
    assert all(x["status"] == "ISSUED" for x in approved["row"]["sheets"]),         "approval issues the hides — they are spoken for and cannot be re-allocated"
    frozen = await api_client.patch(f"{API}/cutting/rows/{row_id}",
                                    json={"size": "XXL"})
    assert frozen.status_code == 409, \
        "an approved row is an audited transition, not a soft boolean"

    # ── the cutter scans. THE OPERATOR TYPES NOTHING: article, colour, lot and
    #    dcm were entered once on the grid and signed off.
    piece = _piece(pieces)
    log = _ok(await api_client.post(f"{API}/production/log", json={
        "screen_context": "LEATHER_CUT",
        "actor": {"employee_id": str(cutter[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY}))
    assert log["logged"] == [piece.code]
    assert log.get("consumption_source") in ("CUTTING_ROW", "cutting_row"), \
        "the scan must retrieve the approved row, not ask the floor to retype it"

    # ── through to the store.
    await _raw_event(db, full_ops, piece, cutter[0].id, "FUSING")
    await _raw_event(db, full_ops, piece, cutter[0].id, "PASTING")

    # ── THE MERGE. Employee, piece, enter. One scan fewer than the drawer flow.
    as_role(UserRole.STORE_MANAGER)
    lea = _ok(await api_client.post(f"{API}/store/scan", json={
        "employee_id": str(tailor[0].id), "piece_barcode": piece.code,
        "part": "LEATHER"}))
    assert lea["leather_in"] is True
    assert lea["complete"] is False, "a lined garment is not complete on leather alone"
    assert "LINING" in " ".join(lea["awaiting"]).upper()
    assert lea["kit"] is not None, \
        "the kit checklist rides EVERY scan — its own contract says so"

    # ── the lining cannot be parked before it has been cut. The store is where
    #    a FINISHED part is put down, not where an unfinished one waits.
    early_lining = await api_client.post(f"{API}/store/scan", json={
        "employee_id": str(tailor[0].id), "piece_barcode": piece.code,
        "part": "LINING"})
    assert early_lining.status_code == 409
    assert "LINING_CUTTING has not been logged" in early_lining.text

    # the lining cut is the parallel path — a different manager, same garment.
    await _raw_event(db, full_ops, piece, cutter[0].id, "LINING_CUTTING")

    lin = _ok(await api_client.post(f"{API}/store/scan", json={
        "employee_id": str(tailor[0].id), "piece_barcode": piece.code,
        "part": "LINING"}))
    assert lin["lining_in"] is True
    assert lin["complete"] is True

    # ── line-stitching is refused until the store releases it.
    as_role(UserRole.STITCHING_MANAGER)
    early = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY}))
    assert early["logged"] == []
    assert early["merge_blocked"] == [piece.code], \
        ("_merge_ok is the ONLY gate on LINE_STITCHING — if this ever passes, "
         "a garment can be stitched before it was cut")

    # ── the store sends it, and the same scan now goes through.
    sent = _ok(await api_client.post(f"{API}/store/send",
                                     json={"piece_barcodes": [piece.code]}))
    assert sent["count_sent"] == 1

    done = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY}))
    assert done["logged"] == [piece.code]
    assert done["stage"] == "LINE_STITCHING"


# ══════════════════════════════════════════════════════════════════ FLOW 9
async def test_flow9_a_rush_order_goes_outside_and_comes_back_short(
        api_client, as_role, db, full_ops, pieces, cutter, tailor):
    """Three garments to an outside tailor; two come back, one never does.

    "EVEN WHEN WE GIVE IT TO THE OUTSIDE FACTORY WE ARE PAYING FOR EACH PIECE,
    SO WE HAVE TO TRACK THAT." The money is the point of this flow: only the
    pieces that came BACK are paid for, and SHORT is kept apart from REJECTED
    because they lead to different conversations — a missing garment is a loss
    to chase with the vendor, a badly-made one is a quality matter.
    """
    a, b, c = _piece(pieces, 0), _piece(pieces, 1), _piece(pieces, 2)
    for p in (a, b, c):
        await _through_the_cut_side(db, full_ops, p, cutter[0].id)
        p.store_state = "sended"
        p.leather_in = p.lining_in = True
        p.needs_lining = False
    await db.commit()

    as_role(UserRole.DIRECT_MANAGER)
    vendor = _ok(await api_client.post(f"{API}/jobwork/vendors", json={
        "name": "SUNRISE TAILORS", "contact": "98400 11111"}))
    vendor_id = vendor["vendor_id"]

    # registering the same name twice is not a mistake worth a 409.
    again = _ok(await api_client.post(f"{API}/jobwork/vendors",
                                      json={"name": "  sunrise tailors  "}))
    assert again["vendor_id"] == vendor_id

    # ── out they go, with a rate and a date to chase.
    due = (datetime.date.today() - datetime.timedelta(days=2)).isoformat()
    job = _ok(await api_client.post(f"{API}/jobwork/dispatch", json={
        "vendor_id": vendor_id, "stage": "LINE_STITCHING",
        "piece_ids": [str(a.id), str(b.id), str(c.id)],
        "expected_back": due, "rate_per_piece": 45.0, "currency": "INR"}))
    assert job["pieces_out"] == 3
    assert job["overdue"] is True, "a dispatch nobody chases is how garments go missing"

    late = _ok(await api_client.get(f"{API}/jobwork", params={"overdue": True}))
    assert job["job_id"] in [j["job_id"] for j in late]

    # ── GATE 7. A garment twenty miles away cannot be scanned in this building.
    as_role(UserRole.STITCHING_MANAGER)
    blocked = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [a.code]},
        "work_date": TODAY}))
    assert blocked["offsite_blocked"] == [a.code]
    reason = next(x["reason"] for x in blocked["blocked"] if x["gate"] == "offsite")
    assert "SUNRISE TAILORS" in reason, \
        "the refusal must name WHERE the garment is, or nobody can go get it"

    # ── two come back; one is still out there.
    as_role(UserRole.DIRECT_MANAGER)
    ret = _ok(await api_client.post(f"{API}/jobwork/{job['job_id']}/receive", json={
        "piece_ids": [str(a.id), str(b.id)], "work_date": TODAY}))
    assert ret["status"] == "PARTIAL", \
        '"the job is back" is a different statement from "every piece is back"'
    assert ret["pieces_back"] == 2
    assert ret["pieces_out"] == 1
    assert ret["cost"] == 90.0, "45 x 2 returned; the one still out is not paid for"

    # ── the stage the vendor performed is recorded against the VENDOR.
    from sqlalchemy import select
    ev = (await db.execute(
        select(ProductionEvent).where(
            ProductionEvent.piece_id == a.id,
            ProductionEvent.operation_id == full_ops["LINE_STITCHING"].id))
    ).scalars().first()
    assert ev is not None, "the work really happened; the garment must advance"
    assert ev.employee_id is None, \
        "nobody on our payroll earned this, so no wage line may be generated"

    # ── a returned garment scans in-house again: the block was about being
    #    away, not about having been away.
    as_role(UserRole.STITCHING_MANAGER)
    ok = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [a.code]},
        "work_date": TODAY}))
    assert ok["offsite_blocked"] == []
    assert ok["stage"] == "SHELL_STITCHING", "it advanced past what the vendor did"

    # ── and the one still out is still blocked.
    still = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [c.code]},
        "work_date": TODAY}))
    assert still["offsite_blocked"] == [c.code]

    # ── the floor's read screens survive a garment being off-site.
    as_role(UserRole.DIRECT_MANAGER)
    _ok(await api_client.get(f"{API}/analytics/pieces/{c.code}/story"))
    _ok(await api_client.get(f"{API}/dashboard/direct-manager/pieces/{c.code}"))


# ═════════════════════════════════════════════════════════════════ FLOW 10
async def test_flow10_the_mistake_day_every_correction_surface(
        api_client, as_role, db, full_ops, pieces, cutter, absent_worker, paster,
        leather_lot):
    """Everything that was entered wrong today, corrected the way the floor does it.

    THE STORY IS ONE MISTAKE WITH FOUR CONSEQUENCES. A card was swapped at the
    gate: the system says RAMESH was here and did the cutting; it was actually
    the man whose card never got scanned. Attendance is wrong, the production
    events are wrong, and the wage will be wrong — because wages are computed
    from the events, not from the attendance.

    SO THE CORRECTION IS RE-ALLOCATION, NOT DELETION, and the day's work moves
    with the name in the SAME ACTION. Leaving the events behind splits one
    mistake into two jobs, and the second is the one nobody remembers.
    """
    piece, other = _piece(pieces, 0), _piece(pieces, 1)

    # ── the day as the system wrongly recorded it.
    as_role(UserRole.CUTTING_MANAGER)
    log = _ok(await api_client.post(f"{API}/production/log", json={
        "screen_context": "LEATHER_CUT",
        "actor": {"employee_id": str(cutter[0].id)},
        "targets": {"piece_barcodes": [piece.code, other.code]},
        "work_date": TODAY,
        "consumption": {"leather_lot_id": str(leather_lot.id), "dcm": 800}}))
    assert log["count_logged"] == 2

    # ── find the punch the gate made.
    as_role(UserRole.SECURITY)
    today = _ok(await api_client.get(f"{API}/attendance/today"))
    rows = today if isinstance(today, list) else today.get("rows", today.get("present", []))
    punch = next(r for r in rows if str(r.get("employee_id")) == str(cutter[0].id))
    punch_id = punch.get("attendance_id") or punch.get("id")

    # ── DELETING IT IS REFUSED, and the refusal names the better operation.
    as_role(UserRole.HR)
    bad = await api_client.delete(f"{API}/attendance/{punch_id}",
                                  params={"reason": "he was not here"})
    assert bad.status_code == 409
    assert "Re-allocate" in bad.text, \
        ("deleting would leave two events dated to a day the worker was never "
         "present — which the presence gate would have refused to create")

    # ── a reason is not optional either. It decides whether somebody is paid.
    assert (await api_client.delete(f"{API}/attendance/{punch_id}",
                                    params={"reason": "   "})).status_code == 422

    # ── THE CORRECTION. One action, one reason, one audit row.
    as_role(UserRole.SECURITY)
    fixed = _ok(await api_client.patch(f"{API}/attendance/{punch_id}", json={
        "employee_id": str(absent_worker[0].id),
        "reason": "card swapped at the gate"}))
    assert fixed["employee_id"] == str(absent_worker[0].id)
    assert fixed["production_events_moved"] == 2, \
        "the day's work moves with the name, or the money follows the wrong man"

    from sqlalchemy import func, select
    theirs = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.employee_id == absent_worker[0].id))
    orphaned = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.employee_id == cutter[0].id))
    assert (theirs, orphaned) == (2, 0), "the day moved whole"

    # ── re-allocating onto somebody already at the gate is a 409, not a 500.
    clash = await api_client.patch(f"{API}/attendance/{punch_id}",
                                   json={"employee_id": str(paster[0].id)})
    assert clash.status_code == 409
    assert "already marked present" in clash.text

    # ── a punch with nothing behind it CAN be removed — the simple mistake.
    #    The manual door is the fallback when a card will not read; a re-tap on
    #    somebody already marked present is a no-op, not a second row.
    as_role(UserRole.HR)
    marked = _ok(await api_client.post(f"{API}/attendance/proxy/check-in", json={
        "employee_ids": [str(paster[0].id)]}), 200, 201)
    pid = marked[0]["id"]
    removed = _ok(await api_client.delete(
        f"{API}/attendance/{pid}", params={"reason": "marked the wrong person"}))
    assert removed["deleted"] is True

    # ── SECURITY may correct a punch but may NOT remove one: taking somebody
    #    off the day's roster decides whether they are paid for it.
    as_role(UserRole.SECURITY)
    assert (await api_client.delete(f"{API}/attendance/{punch_id}",
                                    params={"reason": "x"})).status_code == 403

    # ── the production record has its own correction surface: an event filed
    #    against the wrong worker is reassigned, not deleted and retyped.
    as_role(UserRole.DIRECT_MANAGER)
    events = _ok(await api_client.get(f"{API}/production/events",
                                      params={"work_date": TODAY, "limit": 10}))
    # `Page` envelope (core/pagination.py): {items, total, limit, offset, ...}.
    # The other shapes are kept because this feed used to return a bare list and
    # older deployments may still.
    evs = (events if isinstance(events, list)
           else events.get("items") or events.get("rows")
           or events.get("events") or [])
    assert evs, "the events the corrections screen lists must be readable"
    ev_id = evs[0].get("id") or evs[0].get("event_id")
    moved = _ok(await api_client.patch(
        f"{API}/production/events/{ev_id}/reassign",
        params={"employee_id": str(cutter[0].id),
                "reason": "one of the two pieces was really mine"}))
    assert str(moved["employee_id"]) == str(cutter[0].id)
    assert moved["operation"] == "LEATHER_CUTTING",         "the garment WAS cut — reassigning must not touch the stage or the stock"
    assert moved["consumption_qty"], "nor what it consumed"

    # ── and the material ledger's: an adjustment is audited, never a silent write.
    lot = _ok(await api_client.post(f"{API}/materials/lots", json={
        "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-18L",
        "colour": "BLACK", "attributes": {"size": "18L", "count": 5000}}))
    lot_id = lot.get("lot_id") or lot["id"]
    adj = _ok(await api_client.patch(f"{API}/materials/lots/{lot_id}/adjust",
                                     json={"delta": -50, "reason": "miscount at intake"}))
    assert adj is not None
    hist = _ok(await api_client.get(f"{API}/materials/lots/{lot_id}/history"))
    assert hist is not None, "every correction leaves a trail somebody can read"
