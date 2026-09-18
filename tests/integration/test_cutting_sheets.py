"""
INTEGRATION · Cutting V2 — per-hide leather tracking and the sheet-wise grid.

THE MONEY PATH THIS PROTECTS. Leather is the expensive material and the whole
costing rests on it, but until now the ERP only saw one number typed at the end of
cutting. Kumar kept the real record — which hides, what each measured, the one the
cutter handed back — in Excel, so "how much leather did this jacket take" was
unanswerable from the system.

These tests pin the three properties that make the new record trustworthy:

  · a hide can be spent ONCE. It is claimed by exactly one garment, and the
    status is what makes a second claim impossible rather than merely discouraged.
  · the total is DERIVED. Add a hide, drop a hide, correct a measurement — the
    row's total follows its parts and cannot be typed over.
  · approval FREEZES it, and the freeze is audited. Past approval the numbers are
    what the production log will spend, so an edit underneath them would rewrite a
    signed-off figure with nothing to say it happened.

The numbers are the factory's own: hides of 37-57 dcm, ~9 per size-L garment.
"""
import uuid
from datetime import date

import pytest
import pytest_asyncio

from app.core.enums import CuttingRowStatus, ScreenContext, SheetStatus
from app.modules.cutting.schemas import (
    GenerateRequest, RowPatch, SheetAdd, SheetPatch,
)
from app.modules.cutting.service import CuttingService
from app.modules.materials.schemas import LotCreate, ReceiveRequest, SheetIn
from app.modules.materials.service import MaterialService
from app.modules.production.models import Piece

pytestmark = pytest.mark.asyncio

# Twelve hides off Kumar's own screen. Enough for one size-L garment (430 dcm
# baseline) with a couple to spare, which is what makes the "spare" tests real.
HIDES = [43, 47, 47, 40, 39, 57, 53, 39, 42, 57, 51, 37]

# conftest's `cutter` fixture hands back (employee, barcode) and has already
# written today's attendance row, so these tests take cutter[0] and never need
# to mark presence themselves.


async def _lot(db, dcms=HIDES, article="CL1", colour="PINE GREEN"):
    return await MaterialService(db).create_lot(LotCreate(
        category="LEATHER", article=article, colour=colour,
        attributes={"thickness": "1.2", "dcm": sum(dcms)},
        sheets=[SheetIn(dcm=d) for d in dcms]))


@pytest_asyncio.fixture
async def garments(db, order_tree):
    """Two un-cut pieces — the state a cutting grid is generated from."""
    sku = order_tree["sku"]
    out = []
    for seq in (1, 2):
        p = Piece(code=f"CL-PINE-L-{seq:03d}", seq=seq, sku_id=sku.id)
        db.add(p)
        out.append(p)
    await db.commit()
    for p in out:
        await db.refresh(p)
    return out


# ══════════════════════════════════════════════════════ 1A · hides in stock
async def test_receiving_leather_mints_one_barcode_per_hide(db):
    """MD's requirement: every skin carries its own label.

    A lot barcode names a SPEC and its quantity is fungible. A hide is not — it is
    individually measured and issued to one cutter for one garment — so it is the
    only material here whose individual identity carries information.
    """
    lot = await _lot(db)
    assert len(lot["sheets"]) == len(HIDES)
    codes = [s["code"] for s in lot["sheets"]]
    assert len(set(codes)) == len(codes), "every hide needs its own code"
    assert all(s["status"] == SheetStatus.IN_STOCK.value for s in lot["sheets"])
    assert float(lot["on_hand"]) == float(sum(HIDES))


async def test_a_hide_resolves_through_the_one_front_door(db):
    """Scanning a hide must say WHICH hide, not just what article it came from.

    A LEATHER_SHEET registry row carries material_lot_id too, so a generic lot
    branch would answer "552 dcm of CL1" — true of the lot and useless about the
    skin in the operator's hand.
    """
    from app.modules.barcode.service import BarcodeService
    lot = await _lot(db)
    target = lot["sheets"][2]
    payload = await BarcodeService(db).resolve(target["code"])
    assert payload["type"] == "LEATHER_SHEET"
    assert payload["sheet"]["code"] == target["code"]
    assert payload["sheet"]["dcm"] == target["dcm"]
    assert payload["lot"]["article"] == "CL1", "the lot still travels with it"


async def test_only_leather_is_tracked_sheet_by_sheet(db):
    """A 5,000-button packet is one barcode and a count, not 5,000 labels."""
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        await MaterialService(db).create_lot(LotCreate(
            category="ACCESSORY", subtype="BUTTON", article="BTN-18L",
            colour="NAVY", attributes={"size": "18L", "count": 5000},
            sheets=[SheetIn(dcm=10)]))
    assert exc.value.status_code == 422
    assert "LEATHER" in str(exc.value.detail)


async def test_hides_that_do_not_add_up_warn_but_still_receive(db):
    """Two decimetres of measurement slop must not refuse a delivery.

    Blocking the receipt over it is how a store learns to stop sheeting
    altogether; the discrepancy is reported instead, where a human can see it.
    """
    svc = MaterialService(db)
    lot = await svc.create_lot(LotCreate(
        category="LEATHER", article="CL1", colour="PINE GREEN",
        attributes={"thickness": "1.2", "dcm": 100},
        sheets=[SheetIn(dcm=40), SheetIn(dcm=40)]))
    assert float(lot["on_hand"]) == 100.0, "the stock figure follows the delivery"
    kinds = [w.get("kind") for w in svc.decrement_warnings]
    assert "sheet_sum_mismatch" in kinds


async def test_a_later_delivery_adds_hides_to_the_same_lot(db):
    """Material is one lot per spec, so a second delivery is more hides, not a
    second lot — and the two must still reconcile."""
    lot = await _lot(db, [43, 47])
    svc = MaterialService(db)
    out = await svc.receive(ReceiveRequest(
        lot_id=lot["lot_id"], approved_qty=90, rejected_qty=0,
        sheets=[SheetIn(dcm=45), SheetIn(dcm=45)]), actor_id=None)
    assert len(out["sheets"]) == 2
    assert out["sheet_reconciliation"]["reconciled"] is True
    assert out["sheet_reconciliation"]["sheets_total"] == 4


# ══════════════════════════════════════════════════════ 1B · the grid
async def test_the_grid_allocates_hides_sized_to_the_garment(db, order_tree,
                                                             garments, cutter):
    """The row opens with hides on it, or the manager is back to picking ten.

    Nobody supplies per-piece dcm for most styles, so the allocator aims at the
    size baseline and is expected to be edited. What it must not do is open empty.
    """
    await _lot(db)
    gen = await CuttingService(db).generate(GenerateRequest(
        style_id=order_tree["style"].id, colour="PINE GREEN",
        cutter_employee_id=cutter[0].id))
    assert gen["created"] == 2
    first = gen["rows"][0]
    assert first["target_source"] == "size_baseline"
    assert first["sheet_count"] >= 1
    # It overshoots rather than undershoots: a sheet too many is handed back, a
    # sheet too few stops a cutter mid-garment.
    assert first["total_dcm"] >= first["target_dcm"] or gen["warnings"]


async def test_generating_twice_creates_nothing_the_second_time(db, order_tree,
                                                                garments, cutter):
    """A manager who is not sure the button worked will press it again."""
    await _lot(db)
    svc = CuttingService(db)
    body = GenerateRequest(style_id=order_tree["style"].id,
                           colour="PINE GREEN", cutter_employee_id=cutter[0].id)
    first = await svc.generate(body)
    second = await svc.generate(body)
    assert first["created"] == 2
    assert second["created"] == 0


async def test_one_hide_can_never_be_claimed_by_two_garments(db, order_tree,
                                                             garments, cutter):
    """The expensive failure: two cutters sent for one skin.

    Allocation is a STATE on the hide, so a second claim is refused rather than
    silently overwriting the first.
    """
    from fastapi import HTTPException
    lot = await _lot(db)
    svc = CuttingService(db)
    gen = await svc.generate(GenerateRequest(
        style_id=order_tree["style"].id, colour="PINE GREEN",
        cutter_employee_id=cutter[0].id))
    taken = gen["rows"][0]["sheets"][0]["code"]
    other_row = gen["rows"][1]["row_id"]
    with pytest.raises(HTTPException) as exc:
        await svc.add_sheet(other_row, SheetAdd(sheet_code=taken))
    assert exc.value.status_code == 409
    assert "ALLOCATED" in str(exc.value.detail)


async def test_a_returned_hide_goes_back_on_the_shelf(db, order_tree,
                                                      garments, cutter):
    """'Only 9 were needed' — the hide returns to stock, not to nowhere.

    RETURNED rather than IN_STOCK: both are allocatable, but the status keeps the
    hide's history honest about having been out and come back.
    """
    await _lot(db)
    svc = CuttingService(db)
    gen = await svc.generate(GenerateRequest(
        style_id=order_tree["style"].id, colour="PINE GREEN",
        cutter_employee_id=cutter[0].id))
    row = gen["rows"][0]
    before_total, before_count = row["total_dcm"], row["sheet_count"]
    dropped = row["sheets"][0]

    after = await svc.remove_sheet(row["row_id"], dropped["sheet_id"])
    assert after["sheet_count"] == before_count - 1
    assert after["total_dcm"] == pytest.approx(before_total - dropped["dcm"])

    sheet = await MaterialService(db).repo.get_sheet(dropped["sheet_id"])
    assert sheet.status == SheetStatus.RETURNED.value
    assert sheet.cutting_row_id is None


async def test_the_total_follows_the_hides_and_is_never_typed(db, order_tree,
                                                              garments, cutter):
    """Correcting a misread measurement recomputes the row.

    A typed total is a number that can disagree with its own parts — the class of
    bug the breakdown importer's reconciliation exists to catch.
    """
    await _lot(db)
    svc = CuttingService(db)
    gen = await svc.generate(GenerateRequest(
        style_id=order_tree["style"].id, colour="PINE GREEN",
        cutter_employee_id=cutter[0].id))
    row = gen["rows"][0]
    first = row["sheets"][0]
    delta = 41.5 - first["dcm"]
    after = await svc.patch_sheet(row["row_id"], first["sheet_id"],
                                  SheetPatch(dcm=41.5))
    assert after["total_dcm"] == pytest.approx(row["total_dcm"] + delta)


# ══════════════════════════════════════════════════════ the approval gate
async def _approved(db, order_tree, cutter):
    svc = CuttingService(db)
    gen = await svc.generate(GenerateRequest(
        style_id=order_tree["style"].id, colour="PINE GREEN",
        cutter_employee_id=cutter[0].id))
    row = gen["rows"][0]
    actor = uuid.uuid4()
    out = await svc.approve(row["row_id"], actor_user_id=None, actor_name="KUMAR")
    return svc, row, out


async def test_approval_freezes_the_row_and_issues_its_hides(db, order_tree,
                                                             garments, cutter):
    await _lot(db)
    svc, row, out = await _approved(db, order_tree, cutter)
    assert out["row"]["status"] == CuttingRowStatus.APPROVED.value
    assert {c["status"] for c in out["row"]["sheets"]} == {SheetStatus.ISSUED.value}
    assert out["row"]["total_dcm"] == row["total_dcm"]


async def test_an_approved_row_refuses_every_edit_and_says_why(db, order_tree,
                                                               garments, cutter):
    """Past approval the numbers are what the log will spend."""
    from fastapi import HTTPException
    await _lot(db)
    svc, row, _ = await _approved(db, order_tree, cutter)
    with pytest.raises(HTTPException) as exc:
        await svc.patch_row(row["row_id"], RowPatch(rc_no="9999"))
    assert exc.value.status_code == 409
    assert "frozen" in str(exc.value.detail).lower()


async def test_approving_twice_is_a_no_op_not_an_error(db, order_tree,
                                                       garments, cutter):
    """The manager could not tell whether the first tap landed."""
    await _lot(db)
    svc, row, _ = await _approved(db, order_tree, cutter)
    again = await svc.approve(row["row_id"], actor_user_id=None)
    assert "already approved" in again["message"].lower()
    assert again["row"]["status"] == CuttingRowStatus.APPROVED.value


async def test_a_row_with_no_cutter_cannot_be_approved(db, order_tree, garments):
    """The cutting log is what a wage is paid from.

    Filling the cutter in afterwards would mean rewriting a signed-off row.
    """
    from fastapi import HTTPException
    await _lot(db)
    svc = CuttingService(db)
    gen = await svc.generate(GenerateRequest(
        style_id=order_tree["style"].id, colour="PINE GREEN"))
    with pytest.raises(HTTPException) as exc:
        await svc.approve(gen["rows"][0]["row_id"], actor_user_id=None)
    assert exc.value.status_code == 409
    assert "cutter" in str(exc.value.detail).lower()


async def test_a_row_with_no_hides_cannot_be_approved(db, order_tree,
                                                      garments, cutter):
    """There is nothing to cut from, so there is nothing to sign off."""
    from fastapi import HTTPException
    svc = CuttingService(db)
    gen = await svc.generate(GenerateRequest(          # no lot -> no hides
        style_id=order_tree["style"].id, colour="PINE GREEN",
        cutter_employee_id=cutter[0].id))
    with pytest.raises(HTTPException) as exc:
        await svc.approve(gen["rows"][0]["row_id"], actor_user_id=None)
    assert exc.value.status_code == 409
    assert "no sheets" in str(exc.value.detail).lower()


async def test_approval_writes_an_audit_row(db, order_tree, garments, cutter):
    """A hard, audited transition — never a soft boolean (CLAUDE.md §15)."""
    from sqlalchemy import select
    from app.core.models import AuditLog
    await _lot(db)
    svc, row, _ = await _approved(db, order_tree, cutter)
    logs = (await db.execute(
        select(AuditLog).where(AuditLog.entity_type == "cutting_row"))
    ).scalars().all()
    assert len(logs) == 1
    assert logs[0].action == "CUTTING_ROW_APPROVED"
    assert logs[0].after["total_dcm"] == row["total_dcm"]
    assert logs[0].after["sheets"], "the hides spent are named in the trail"


async def test_reopening_returns_the_hides_to_the_row_and_is_audited(
        db, order_tree, garments, cutter):
    await _lot(db)
    svc, row, _ = await _approved(db, order_tree, cutter)
    after = await svc.reopen(row["row_id"], actor_user_id=None, reason="miscount")
    assert after["status"] == CuttingRowStatus.DRAFT.value
    assert {c["status"] for c in after["sheets"]} == {SheetStatus.ALLOCATED.value}
    # editable again
    assert (await svc.patch_row(row["row_id"], RowPatch(rc_no="1072")))["rc_no"] == "1072"


# ══════════════════════════════════════════════════════ 1C · the logger
async def test_the_logger_reads_the_approved_row_and_spends_it_once(
        db, order_tree, garments, cutter, operations, cutting_mgr):
    """THE POINT OF THE WHOLE FEATURE: the cutter types nothing.

    Article, colour, lot and dcm were all entered once in the grid and signed off.
    Re-asking for them at the scan gun is the duplicate work this deletes — and
    re-typing is where the wrong article got recorded (bugs #23/#27).
    """
    from app.modules.production.router import _approved_cutting
    from app.modules.production.service import ProductionService
    lot = await _lot(db)
    svc, row, _ = await _approved(db, order_tree, cutter)

    approved, lot_id, dcm, _w = await _approved_cutting(
        db, [garments[0].id], ScreenContext.LEATHER_CUT)
    assert lot_id is not None and dcm == row["total_dcm"]

    before = float((await MaterialService(db).repo.get_lot(lot["lot_id"])).on_hand)
    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[garments[0].id],
        work_date=date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=lot_id, consumption_qty=dcm,
        consumption_source="cutting_row", cutting_rows=approved)

    assert res["logged"] == [garments[0].code]
    assert res["consumption_source"] == "cutting_row"
    after = float((await MaterialService(db).repo.get_lot(lot["lot_id"])).on_hand)
    assert after == pytest.approx(before - dcm), "spent exactly once"

    final = await svc.get_row_payload(row["row_id"])
    assert final["status"] == CuttingRowStatus.LOGGED.value
    assert {c["status"] for c in final["sheets"]} == {SheetStatus.CONSUMED.value}


async def test_a_piece_with_no_approved_row_keeps_the_typed_path(
        db, order_tree, garments, cutter):
    """Nothing on the floor may change until a style is cut the new way."""
    from app.modules.production.router import _approved_cutting
    await _lot(db)
    approved, lot_id, dcm, warns = await _approved_cutting(
        db, [garments[0].id], ScreenContext.LEATHER_CUT)
    assert approved == {} and lot_id is None and dcm is None


async def test_a_lining_cut_never_reads_a_cutting_row(db, order_tree,
                                                      garments, cutter):
    """A cutting row is a list of hides; lining is cut by the metre."""
    from app.modules.production.router import _approved_cutting
    await _lot(db)
    await _approved(db, order_tree, cutter)
    approved, lot_id, dcm, _ = await _approved_cutting(
        db, [garments[0].id], ScreenContext.LINING_CUT)
    assert (approved, lot_id, dcm) == ({}, None, None)


async def test_garments_approved_with_different_totals_are_not_averaged(
        db, order_tree, garments, cutter):
    """One scan spends one number, and these two took different leather.

    Averaging or picking one would charge a garment what another took — a wrong
    number in the ledger the factory reconciles against, and invisible.
    """
    from fastapi import HTTPException
    from app.modules.production.router import _approved_cutting
    await _lot(db)
    svc = CuttingService(db)
    gen = await svc.generate(GenerateRequest(
        style_id=order_tree["style"].id, colour="PINE GREEN",
        cutter_employee_id=cutter[0].id))
    for r in gen["rows"]:
        await svc.approve(r["row_id"], actor_user_id=None)
    totals = {r["total_dcm"] for r in gen["rows"]}
    if len(totals) == 1:
        pytest.skip("both rows happened to take the same dcm")
    with pytest.raises(HTTPException) as exc:
        await _approved_cutting(db, [g.id for g in garments],
                                ScreenContext.LEATHER_CUT)
    assert exc.value.status_code == 409
