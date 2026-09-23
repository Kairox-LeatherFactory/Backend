"""
SYSTEM · five end-to-end journeys, through the real HTTP app.

WHY THESE AND NOT MORE UNIT TESTS. Everything below is covered somewhere at the
service layer already. What these add is the layer where the service tests
cannot fail: routing, role gates, Pydantic serialisation of the response models,
and the interaction between modules that were each verified alone.

A crash here is the crash a user would actually see.

    FLOW 1  a garment from leather delivery to export, cut through the new grid
    FLOW 2  a defect found late: reject, DM approval, and the re-walk
    FLOW 3  work sent to an outside factory and brought back
    FLOW 4  correcting a production record, and the closed-payroll wall
    FLOW 5  materials: sized accessories, the kit, and the money reconciling
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
@pytest_asyncio.fixture
async def full_ops(db):
    """Every stage in the chain has an Operation row.

    The shared `operations` fixture covers the common ones; a flow that walks a
    garment all the way to export needs the tail as well.
    """
    from sqlalchemy import select
    have = {c for (c,) in (await db.execute(select(Operation.code))).all()}
    made = {}
    for i, stage in enumerate(ProductionStage.leather_chain(), 1):
        if stage.value not in have:
            op = Operation(id=uuid.uuid4(), code=stage.value,
                           label=stage.value.title(), sequence=100 + i)
            db.add(op)
    for stage in (ProductionStage.LINING_CUTTING,):
        if stage.value not in have:
            db.add(Operation(id=uuid.uuid4(), code=stage.value,
                             label="Lining Cutting", sequence=150))
    await db.commit()
    rows = (await db.execute(select(Operation))).scalars().all()
    for op in rows:
        made[op.code] = op
    return made


async def _log_raw(db, ops, piece, employee_id, code):
    """A stage event written directly — used only to put a garment INTO the
    state a flow is about, never to test the thing the flow asserts."""
    db.add(ProductionEvent(
        sku_id=piece.sku_id, operation_id=ops[code].id, employee_id=employee_id,
        work_date=datetime.date.today(), qty=1, piece_id=piece.id,
        entered_by="setup"))
    await db.commit()


def _piece(pieces, i=0):
    return pieces[i]


def _ok(r, *allowed):
    """Assert a response and SHOW THE BODY when it is not what we expected.

    A bare `assert r.status_code == 200` on a 500 tells you nothing; the whole
    point of driving the HTTP layer is to see the error the user would get.
    """
    allowed = allowed or (200, 201)
    assert r.status_code in allowed, f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:600]}"
    return r.json()


# ══════════════════════════════════════════════════════════════════ FLOW 1
async def test_flow1_leather_delivery_to_a_cut_garment(
        api_client, as_role, db, full_ops, order_tree, pieces, cutter):
    """Receive hides → generate the grid → edit it → approve → the cutter scans.

    THE POINT OF THE WHOLE CUTTING FEATURE is the last step: the operator types
    nothing. Article, colour, lot and dcm were entered once in the grid and
    signed off, and the scan pulls them in.
    """
    as_role(UserRole.DIRECT_MANAGER)
    style = order_tree["style"]

    # ── 1 · the delivery, sheet by sheet ────────────────────────────────────
    lot = _ok(await api_client.post(f"{API}/materials/lots", json={
        "category": "LEATHER", "article": style.article, "colour": "PINE GREEN",
        "attributes": {"thickness": "1.2", "dcm": 552},
        "sheets": [{"dcm": d} for d in (43, 47, 47, 40, 39, 57, 53, 39, 42, 57, 51, 37)],
    }))
    assert len(lot["sheets"]) == 12, "one barcode per hide"
    assert lot["sheets"][0]["code"].startswith("LS-")

    detail = _ok(await api_client.get(f"{API}/materials/lots/{lot['lot_id']}"))
    assert detail["received"] == 552.0, "bug #26: received must not be 0"
    assert detail["sheets_total"] == 12

    # a hide resolves through the one front door
    scan = _ok(await api_client.get(
        f"{API}/barcode/resolve", params={"code": lot["sheets"][2]["code"]}))
    assert scan["type"] == "LEATHER_SHEET"
    assert scan["sheet"]["dcm"] == lot["sheets"][2]["dcm"]
    assert scan["lot"]["article"] == style.article
    assert scan["next_expected_scan"] == "PIECE"

    # ── 2 · the grid ────────────────────────────────────────────────────────
    as_role(UserRole.CUTTING_MANAGER)
    gen = _ok(await api_client.post(f"{API}/cutting/rows/generate", json={
        "style_id": str(style.id), "colour": "PINE GREEN"}))
    assert gen["created"] >= 1
    row = gen["rows"][0]
    assert row["status"] == "DRAFT"
    assert row["sheet_count"] >= 1, "the row must not open empty"
    assert row["target_source"] in ("style_spec", "size_baseline")

    grid = _ok(await api_client.get(f"{API}/cutting/grid", params={
        "style_id": str(style.id), "colour": "PINE GREEN"}))
    assert grid["style_name"] == style.name
    assert any(r["row_id"] == row["row_id"] for r in grid["rows"])

    # ── 3 · the cutter needed one more hide, then handed one back ───────────
    # The third door: the delivery is fully allocated across the generated rows,
    # so there is no hide on the shelf to scan — this is the "the delivery was
    # never sheeted, create one now" case.
    added = _ok(await api_client.post(
        f"{API}/cutting/rows/{row['row_id']}/sheets",
        json={"dcm": 44.0, "material_lot_id": lot["lot_id"],
              "create_if_missing": True}))
    assert added["sheet_count"] == row["sheet_count"] + 1

    dropped = added["sheets"][0]
    back = _ok(await api_client.delete(
        f"{API}/cutting/rows/{row['row_id']}/sheets/{dropped['sheet_id']}"))
    assert back["sheet_count"] == added["sheet_count"] - 1
    assert back["total_dcm"] == pytest.approx(added["total_dcm"] - dropped["dcm"])

    # a misread measurement
    fixed = _ok(await api_client.patch(
        f"{API}/cutting/rows/{row['row_id']}/sheets/{back['sheets'][0]['sheet_id']}",
        json={"dcm": 41.5}))
    _ok(await api_client.patch(f"{API}/cutting/rows/{row['row_id']}",
                               json={"rc_no": "1072"}))

    # ── 4 · approve, and the row freezes ────────────────────────────────────
    # THE CUTTER IS NAMED HERE, per garment — generate takes none, because one
    # id there put the same worker on every row of the style.
    appr = _ok(await api_client.post(
        f"{API}/cutting/rows/{row['row_id']}/approve",
        json={"cutter_employee_id": str(cutter[0].id)}))
    assert appr["row"]["status"] == "APPROVED"
    assert {s["status"] for s in appr["row"]["sheets"]} == {"ISSUED"}

    frozen = await api_client.patch(f"{API}/cutting/rows/{row['row_id']}",
                                    json={"rc_no": "9999"})
    assert frozen.status_code == 409, frozen.text

    # re-approving is a no-op, not an error
    again = _ok(await api_client.post(f"{API}/cutting/rows/{row['row_id']}/approve"))
    assert "already approved" in again["message"].lower()

    # ── 5 · the cutter scans. NOTHING is typed. ─────────────────────────────
    piece = await db.get(Piece, uuid.UUID(row["piece_id"]))
    before = _ok(await api_client.get(f"{API}/materials/lots/{lot['lot_id']}"))["on_hand"]

    logged = _ok(await api_client.post(f"{API}/production/log", json={
        "screen_context": "LEATHER_CUT",
        "actor": {"employee_id": str(cutter[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY,
    }), 201)
    assert logged["logged"] == [piece.code]
    assert logged["consumption_source"] == "cutting_row"
    assert logged["consumption_recorded"]["qty"] == appr["row"]["total_dcm"]

    after = _ok(await api_client.get(f"{API}/materials/lots/{lot['lot_id']}"))["on_hand"]
    assert after == pytest.approx(before - appr["row"]["total_dcm"]), "spent once"

    # the row closed and its hides are gone
    final = _ok(await api_client.get(f"{API}/cutting/grid", params={
        "style_id": str(style.id), "colour": "PINE GREEN"}))
    mine = next(r for r in final["rows"] if r["row_id"] == row["row_id"])
    assert mine["status"] == "LOGGED"
    assert {s["status"] for s in mine["sheets"]} == {"CONSUMED"}


# ══════════════════════════════════════════════════════════════════ FLOW 2
async def test_flow2_a_defect_found_late_goes_back_and_re_walks(
        api_client, as_role, db, full_ops, pieces, cutter, paster, tailor):
    """Rejected at LINE_STITCHING, sent back to FUSING, and the stages done on
    the bad panel are done again — in order."""
    piece = _piece(pieces)
    for code in ("LEATHER_CUTTING", "FUSING", "PASTING", "LINE_STITCHING"):
        await _log_raw(db, full_ops, piece, cutter[0].id, code)
    piece.store_state = "sended"
    piece.leather_in = True
    piece.lining_in = True
    piece.needs_lining = False
    await db.commit()

    # ── the stitching manager raises it, naming who is answerable ───────────
    as_role(UserRole.STITCHING_MANAGER)
    insp = _ok(await api_client.post(f"{API}/inspections", json={
        "piece_barcode": piece.code,
        "found_at_stage": "LINE_STITCHING",
        "verdict": "REJECT", "action": "REDO", "return_to_stage": "FUSING",
        "defect_type": "WORKMANSHIP",
        "responsible_employee_id": str(cutter[0].id),
        "responsible_stage": "FUSING",
        "reason": "fusing lifted at the seam",
    }), 201)
    assert insp["status"] == "PENDING"

    # blaming somebody for a bad hide is refused
    bad = await api_client.post(f"{API}/inspections", json={
        "piece_barcode": piece.code, "found_at_stage": "LINE_STITCHING",
        "verdict": "REJECT", "action": "FIX", "defect_type": "PRODUCT_DAMAGE",
        "responsible_employee_id": str(cutter[0].id)})
    assert bad.status_code in (409, 422), bad.text

    # ── while it is pending the garment does not move ───────────────────────
    held = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [piece.code]},
        "work_date": TODAY}), 201)
    assert held["logged"] == []
    assert held["rejected_blocked"] == [piece.code]

    # the stitching manager cannot approve his own rejection
    denied = await api_client.post(f"{API}/inspections/{insp['inspection_id']}/approve")
    assert denied.status_code == 403, denied.text

    # ── the DM approves, and the re-walk begins ─────────────────────────────
    as_role(UserRole.DIRECT_MANAGER)
    ok = _ok(await api_client.post(
        f"{API}/inspections/{insp['inspection_id']}/approve",
        json={"note": "agreed, re-fuse it"}))
    assert ok["status"] == "APPROVED"

    walked = []
    for _ in range(10):
        out = _ok(await api_client.post(f"{API}/production/log", json={
            "actor": {"employee_id": str(tailor[0].id)},
            "targets": {"piece_barcodes": [piece.code]},
            "work_date": TODAY}), 201)
        if not out["logged"]:
            break
        walked.append(out["stage"])
    assert walked[:3] == ["FUSING", "PASTING", "LINE_STITCHING"], walked

    # ── and the defect is countable against the worker ──────────────────────
    report = _ok(await api_client.get(f"{API}/inspections/responsibility"))
    assert any(r["employee"] == cutter[0].name and r["stage"] == "FUSING"
               and r["rejections"] == 1 for r in report), report


# ══════════════════════════════════════════════════════════════════ FLOW 3
async def test_flow3_work_goes_to_an_outside_factory_and_comes_back(
        api_client, as_role, db, full_ops, pieces, cutter, tailor):
    """Dispatch, the in-house block, the return that logs the vendor's stage,
    and a cost that counts only what actually came back."""
    a, b = _piece(pieces, 0), _piece(pieces, 1)
    for p in (a, b):
        for code in ("LEATHER_CUTTING", "FUSING", "PASTING"):
            await _log_raw(db, full_ops, p, cutter[0].id, code)
        p.store_state = "sended"
        p.leather_in = True
        p.lining_in = True
        p.needs_lining = False
    await db.commit()

    as_role(UserRole.DIRECT_MANAGER)
    vendor = _ok(await api_client.post(f"{API}/jobwork/vendors", json={
        "name": "ABC Tailors", "contact": "98000 00000"}), 201)

    job = _ok(await api_client.post(f"{API}/jobwork/dispatch", json={
        "vendor_id": vendor["vendor_id"], "stage": "LINE_STITCHING",
        "piece_ids": [str(a.id), str(b.id)],
        "expected_back": (datetime.date.today() - datetime.timedelta(days=1)).isoformat(),
        "rate_per_piece": 45.0, "currency": "INR"}), 201)
    assert job["pieces_out"] == 2
    assert job["overdue"] is True, "a dispatch nobody chases is how pieces vanish"

    # ── they cannot be worked here while they are there ─────────────────────
    blocked = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [a.code, b.code]},
        "work_date": TODAY}), 201)
    assert blocked["logged"] == []
    assert set(blocked["offsite_blocked"]) == {a.code, b.code}
    assert "ABC Tailors" in next(
        x["reason"] for x in blocked["blocked"] if x["gate"] == "offsite")

    overdue = _ok(await api_client.get(f"{API}/jobwork", params={"overdue": True}))
    assert [j["job_id"] for j in overdue] == [job["job_id"]]

    # ── one comes back good, one badly done ─────────────────────────────────
    got = _ok(await api_client.post(f"{API}/jobwork/{job['job_id']}/receive",
                                    json={"rejected_ids": [str(b.id)]}))
    assert got["pieces_back"] == 1
    assert got["pieces_rejected"] == 1
    assert got["cost"] == 45.0, "only the piece that came back is paid for"

    # the returned one advances; its stage was logged against the VENDOR
    moved = _ok(await api_client.post(f"{API}/production/log", json={
        "actor": {"employee_id": str(tailor[0].id)},
        "targets": {"piece_barcodes": [a.code]},
        "work_date": TODAY}), 201)
    assert moved["offsite_blocked"] == []
    assert moved["stage"] == "SHELL_STITCHING", "it moved past what the vendor did"


# ══════════════════════════════════════════════════════════════════ FLOW 4
async def test_flow4_correcting_a_record_and_the_closed_payroll_wall(
        api_client, as_role, db, full_ops, pieces, cutter, paster, leather_lot):
    """The wrong worker is reassigned; a bogus record is deleted and its stock
    returned; and neither is possible once payroll has been closed on it."""
    piece = _piece(pieces)
    ev = ProductionEvent(
        sku_id=piece.sku_id, operation_id=full_ops["LEATHER_CUTTING"].id,
        employee_id=cutter[0].id, work_date=datetime.date.today(), qty=1,
        piece_id=piece.id, entered_by="setup",
        leather_lot_id=leather_lot.id, consumption_qty=12.0)
    db.add(ev)
    await db.commit()
    await db.refresh(ev)
    before = float(leather_lot.on_hand)

    # ── the wrong name: stock must NOT move, the hide really was cut ────────
    as_role(UserRole.CUTTING_MANAGER)
    fixed = _ok(await api_client.patch(
        f"{API}/production/events/{ev.id}/reassign",
        params={"employee_id": str(paster[0].id), "reason": "scanned wrong card"}))
    assert fixed["employee_id"] == str(paster[0].id)
    await db.refresh(leather_lot)
    assert float(leather_lot.on_hand) == before

    # ── a cutting manager may not DELETE ────────────────────────────────────
    nope = await api_client.delete(f"{API}/production/events/{ev.id}",
                                   params={"reason": "oops"})
    assert nope.status_code == 403, nope.text

    # ── the DM deletes a bogus record and the hide comes back ───────────────
    as_role(UserRole.DIRECT_MANAGER)
    no_reason = await api_client.delete(f"{API}/production/events/{ev.id}")
    assert no_reason.status_code == 422, "a deletion must say why"

    gone = _ok(await api_client.delete(f"{API}/production/events/{ev.id}",
                                       params={"reason": "scanned the wrong piece"}))
    assert gone["stock_returned"] == 12.0
    await db.refresh(leather_lot)
    assert float(leather_lot.on_hand) == before + 12.0

    # ── once payroll is closed on it, nothing may be touched ────────────────
    from app.core.enums import RunStatus
    from app.modules.wages.models import WageRun
    second = ProductionEvent(
        sku_id=piece.sku_id, operation_id=full_ops["FUSING"].id,
        employee_id=cutter[0].id, work_date=datetime.date.today(), qty=1,
        piece_id=piece.id, entered_by="setup")
    db.add(second)
    db.add(WageRun(id=uuid.uuid4(), period_start=datetime.date.today(),
                   period_end=datetime.date.today(), status=RunStatus.CLOSED))
    await db.commit()
    await db.refresh(second)

    walled = await api_client.patch(
        f"{API}/production/events/{second.id}/reassign",
        params={"employee_id": str(paster[0].id)})
    assert walled.status_code == 409, walled.text
    assert "CLOSED payroll run" in walled.text


# ══════════════════════════════════════════════════════════════════ FLOW 5
async def test_flow5_sized_accessories_the_kit_and_the_money(
        api_client, as_role, db, full_ops, order_tree, cutter, paster):
    """A sized accessory reaches only its own size; the store issues the kit and
    spends stock; and received / consumed / available reconcile."""
    from app.modules.clients.models import SKU
    as_role(UserRole.DIRECT_MANAGER)
    style = order_tree["style"]

    # three sizes of one style
    skus = {order_tree["sku"].size: order_tree["sku"]}
    for size in ("S", "L"):
        k = SKU(id=uuid.uuid4(), style_id=style.id, color_code="PINE",
                color_name="PINE GREEN", size=size, qty_ordered=1,
                code=f"JP-CLERMONT-PINE-{size}")
        db.add(k)
        skus[size] = k
    await db.commit()

    made = {}
    for size, sku in skus.items():
        p = Piece(id=uuid.uuid4(), code=f"FLOW5-{size}", seq=90, sku_id=sku.id,
                  needs_lining=False)
        db.add(p)
        made[size] = p
    await db.commit()

    # ── the accessory stock, and a recipe with one line per size ────────────
    for size in ("S", "M", "L"):
        _ok(await api_client.post(f"{API}/materials/lots", json={
            "category": "ACCESSORY", "subtype": "ZIP", "article": "ZIP-N",
            "colour": "PINE GREEN",
            "attributes": {"size": size, "count": 100}}))

    # order_tree's style is RELEASED, so the LEATHER line is frozen — and must
    # be: it is what the garments were cut and costed against. ACCESSORIES are
    # correctable after release (fix #12), which is the path used here.
    for size in ("S", "M", "L"):
        _ok(await api_client.post(
            f"{API}/styles/{style.id}/material-spec/lines",
            json={"category": "ACCESSORY", "subtype": "ZIP", "article": "ZIP-N",
                  "colour": "PINE GREEN", "size": size, "qty_per_piece": 1}),
            200, 201)

    lines = _ok(await api_client.get(f"{API}/styles/{style.id}/material-spec"))
    zips = [l for l in lines["lines"] if l["article"] == "ZIP-N"]
    assert len(zips) == 3, "three sizes are three lines, not one overwritten"
    assert {z["garment_size"] for z in zips} == {"S", "M", "L"}, zips

    # ── the requirement must not order three zips per garment ───────────────
    # `requirement` is a STYLE-WIDE purchase report, so it lists all three lines
    # — that is correct. What must NOT happen is each sized line being multiplied
    # by the whole style: a purchase is raised from this number, so the error
    # would have been bought.
    req = _ok(await api_client.get(
        f"{API}/styles/{style.id}/material-spec/requirement"))
    zip_lines = [l for l in req["lines"] if l["article"] == "ZIP-N"]
    assert len(zip_lines) == 3
    # A requirement is a PURCHASE report, so it is measured in garments ORDERED
    # (SKU.qty_ordered), not in pieces minted so far.
    ordered = {sz: skus[sz].qty_ordered for sz in skus}
    for line in zip_lines:
        want = ordered[line["garment_size"]]
        assert line["pieces"] == want, (
            f"the {line['garment_size']} zip line is costed for "
            f"{line['pieces']} garments, not {want}")
    assert sum(l["pieces"] for l in zip_lines) == sum(ordered.values()), (
        "one zip per garment in total, not one per garment per size")

    # ── the store kits one garment and the stock moves ──────────────────────
    piece = made["M"]
    for code in ("LEATHER_CUTTING", "FUSING", "PASTING"):
        await _log_raw(db, full_ops, piece, cutter[0].id, code)

    as_role(UserRole.STORE_MANAGER)
    leather_in = _ok(await api_client.post(f"{API}/store/scan", json={
        "employee_id": str(cutter[0].id), "piece_id": str(piece.id),
        "part": "LEATHER"}), 201)
    assert leather_in["store_state"] == "holding_leather"
    assert leather_in["kit"] is not None, "the kit rides every scan"

    kitted = _ok(await api_client.post(f"{API}/store/scan", json={
        "employee_id": str(cutter[0].id), "piece_id": str(piece.id),
        "part": "ACCESSORY"}), 201)
    assert kitted["accessories_in"] is True
    assert "ACCESSORIES" not in kitted["awaiting"]

    # By id: these pieces were created directly by the fixture and never went
    # through premint, so they carry no printed barcode. Both doors are supported.
    sent = _ok(await api_client.post(f"{API}/store/send",
                                     json={"piece_ids": [str(piece.id)]}))
    assert sent["sent"] == [piece.code], sent

    # ── and the money reconciles ────────────────────────────────────────────
    as_role(UserRole.DIRECT_MANAGER)
    lots = _ok(await api_client.get(f"{API}/materials/lots",
                                    params={"category": "ACCESSORY"}))
    m_zip = next(l for l in lots["lots"] if l["size"] == "M")
    detail = _ok(await api_client.get(f"{API}/materials/lots/{m_zip['lot_id']}"))
    assert detail["received"] == 100.0
    assert detail["on_hand"] == 99.0, "one zip went into the garment"
    assert detail["received"] - detail["on_hand"] == 1.0

    # the M zip was spent; the S and L ones were not
    for size in ("S", "L"):
        other = next(l for l in lots["lots"] if l["size"] == size)
        od = _ok(await api_client.get(f"{API}/materials/lots/{other['lot_id']}"))
        assert od["on_hand"] == 100.0, f"the {size} zip must not have been spent"
