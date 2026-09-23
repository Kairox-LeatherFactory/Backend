"""
INTEGRATION · the correction paths — hide CRUD and arrival CRUD.

WHAT THESE COVER AND WHY THEY ARE A MONEY PATH. Both surfaces exist because the
two most mistake-prone entries in the module could not be corrected:

    a HIDE   is typed by somebody reading a number written on a skin. A hide
             entered as 45 when the skin says 4.5 was wrong forever, and a fifth
             hide typed by accident stayed in the count forever.
    an ARRIVAL is typed at the gate, from a delivery note, and it puts stock
             straight onto the cuttable shelf. A van entered twice was a lot's
             worth of stock nobody could take back out.

THE RULE UNDER EVERY TEST HERE is that the STATE is the permission. Untouched
means correctable; acted-upon means history, and history is not edited. The 409
branches matter more than the happy paths, because they are what stops a
correction from rewriting what a garment was cut from.
"""
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import BarcodeStatus, IntakeStatus, SheetStatus
from app.modules.barcode.models import (
    BarcodeRegistry, MaterialLot, MaterialReceipt, MaterialSheet,
)
from app.modules.materials import schemas
from app.modules.materials.service import MaterialService

pytestmark = pytest.mark.integrity

ACTOR = uuid.uuid4()


# ══════════════════════════════════════════════════════════════ helpers
def lot_body(**kw):
    base = dict(category="LEATHER", subtype=None, article="SHEEP GLESS",
                colour="BLACK", attributes={"thickness": "0.7", "dcm": 3400})
    base.update(kw)
    return schemas.LotCreate(**base)


@pytest.fixture
async def sheeted(db):
    """The exact shape the bug report used: one lot, four hides."""
    res = await MaterialService(db).create_lot(lot_body(
        sheets=[{"dcm": 45}, {"dcm": 54}, {"dcm": 40}, {"dcm": 30}]))
    return res


async def arrival(db, **kw):
    body = schemas.ArrivalCreate(**{
        "article": "SHEEP GLESS", "colour": "BLACK", "total_qty": 3400,
        "category": "LEATHER", **kw})
    return await MaterialService(db).arrive(body, actor_id=ACTOR)


# ══════════════════════════════════════════════════════════════ read
class TestListSheets:
    async def test_every_hide_of_a_lot_is_listed_smallest_first(self, db, sheeted):
        """Smallest first is the allocator's order — offcuts before a big skin."""
        out = await MaterialService(db).list_sheets(sheeted["lot_id"])
        assert out["count"] == 4
        assert [s["dcm"] for s in out["sheets"]] == [30.0, 40.0, 45.0, 54.0]
        assert out["article"] == "SHEEP GLESS"

    async def test_the_roll_up_and_reconciliation_ride_along(self, db, sheeted):
        out = await MaterialService(db).list_sheets(sheeted["lot_id"])
        assert out["sheets_by_status"][SheetStatus.IN_STOCK.value]["count"] == 4
        assert out["reconciliation"]["sheet_dcm_in_store"] == 169.0
        assert out["reconciliation"]["lot_on_hand"] == 3400.0
        assert out["reconciliation"]["reconciled"] is False

    async def test_it_narrows_by_status(self, db, sheeted):
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        sheet.status = SheetStatus.CONSUMED.value
        await db.commit()
        assert (await svc.list_sheets(
            sheeted["lot_id"], status_filter="IN_STOCK"))["count"] == 3
        assert (await svc.list_sheets(
            sheeted["lot_id"], status_filter="CONSUMED,SCRAPPED"))["count"] == 1

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).list_sheets(uuid.uuid4())
        assert e.value.status_code == 404

    async def test_a_lot_with_no_hides_lists_none_without_failing(self, db):
        res = await MaterialService(db).create_lot(lot_body(article="UNSHEETED"))
        out = await MaterialService(db).list_sheets(res["lot_id"])
        assert out["count"] == 0 and out["sheets"] == []
        # Zero hides is not a mismatch — it is a delivery nobody sheeted.
        assert out["reconciliation"]["reconciled"] is True


class TestGetSheet:
    async def test_one_hide_carries_its_lot_identity_and_label(self, db, sheeted):
        out = await MaterialService(db).get_sheet(sheeted["sheets"][0]["sheet_id"])
        assert out["dcm"] == 45.0
        assert out["article"] == "SHEEP GLESS" and out["colour"] == "BLACK"
        assert out["thickness"] == "0.7"
        # REGRESSION: a hide's registry row carries material_lot_id too, so
        # `barcodes_by_lot` used to hand back one of the HIDES' codes as the
        # lot's label — here, on the lot picker and on the lot detail page.
        assert out["lot_barcode"] == sheeted["lot_barcode"]
        assert out["lot_barcode"].startswith("LOT-LEA-")
        assert out["editable"] is True

    async def test_a_sheeted_lot_still_reports_its_own_label_everywhere(
            self, db, sheeted):
        """The other two readers of `barcodes_by_lot`, pinned against the same
        bug: neither may return an LS- code."""
        svc = MaterialService(db)
        detail = await svc.get_lot(sheeted["lot_id"])
        assert detail["barcode"] == sheeted["lot_barcode"]
        picker = await svc.list_lots(category="LEATHER")
        assert picker["lots"][0]["barcode"] == sheeted["lot_barcode"]

    async def test_a_hide_on_a_cutting_row_reports_itself_as_locked(
            self, db, sheeted):
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        sheet.cutting_row_id = uuid.uuid4()
        await db.commit()
        assert (await svc.get_sheet(sheet.id))["editable"] is False

    async def test_a_consumed_hide_reports_itself_as_locked(self, db, sheeted):
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        sheet.status = SheetStatus.CONSUMED.value
        await db.commit()
        assert (await svc.get_sheet(sheet.id))["editable"] is False

    async def test_an_unknown_hide_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).get_sheet(uuid.uuid4())
        assert e.value.status_code == 404 and e.value.detail == "Hide not found."


# ══════════════════════════════════════════════════════════════ add
class TestAddSheets:
    async def test_a_missed_hide_can_be_added_later_with_its_own_label(
            self, db, sheeted):
        out = await MaterialService(db).add_sheets(
            sheeted["lot_id"], [{"dcm": 61, "note": "found under the bundle"}],
            actor_id=ACTOR)
        assert len(out["added"]) == 1
        added = out["added"][0]
        assert added["dcm"] == 61.0 and added["status"] == SheetStatus.IN_STOCK.value

        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.material_sheet_id == added["sheet_id"]))
        assert row.type == "LEATHER_SHEET"
        assert row.status == BarcodeStatus.ACTIVE.value

    async def test_adding_a_hide_does_not_invent_stock(self, db, sheeted):
        """`on_hand` is what the delivery note said arrived. Typing the hide
        somebody forgot does not mean more leather walked in."""
        before = (await db.get(MaterialLot, sheeted["lot_id"])).on_hand
        out = await MaterialService(db).add_sheets(
            sheeted["lot_id"], [{"dcm": 61}], actor_id=ACTOR)
        after = (await db.get(MaterialLot, sheeted["lot_id"])).on_hand
        assert before == after == Decimal("3400.000")
        assert out["reconciliation"]["sheet_dcm_in_store"] == 230.0

    async def test_the_addition_is_audited(self, db, sheeted):
        from app.core.models import AuditLog
        await MaterialService(db).add_sheets(
            sheeted["lot_id"], [{"dcm": 61}], actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_SHEETS_ADDED"))
        assert row.after["added"][0]["dcm"] == 61.0

    async def test_an_empty_list_is_refused(self, db, sheeted):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).add_sheets(sheeted["lot_id"], [])
        assert e.value.status_code == 422

    async def test_only_leather_is_tracked_hide_by_hide(self, db):
        res = await MaterialService(db).create_lot(lot_body(
            category="LINING", subtype="RIBS", article="RIB-1",
            attributes={"kg": 10}))
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).add_sheets(res["lot_id"], [{"dcm": 5}])
        assert e.value.status_code == 422 and "Only LEATHER" in e.value.detail

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).add_sheets(uuid.uuid4(), [{"dcm": 5}])
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════════════ update
class TestUpdateSheet:
    async def test_a_mistyped_measurement_is_corrected(self, db, sheeted):
        """45 where the skin says 4.5 — the reported case."""
        sheet_id = sheeted["sheets"][0]["sheet_id"]
        out = await MaterialService(db).update_sheet(
            sheet_id, {"dcm": 4.5}, actor_id=ACTOR)
        assert out["dcm"] == 4.5
        assert out["reconciliation"]["sheet_dcm_in_store"] == 128.5

    async def test_the_note_alone_may_be_corrected(self, db, sheeted):
        out = await MaterialService(db).update_sheet(
            sheeted["sheets"][0]["sheet_id"], {"note": "scar on the shoulder"})
        assert out["note"] == "scar on the shoulder"
        assert out["dcm"] == 45.0

    async def test_the_correction_records_what_it_changed(self, db, sheeted):
        from app.core.models import AuditLog
        await MaterialService(db).update_sheet(
            sheeted["sheets"][0]["sheet_id"], {"dcm": 4.5}, actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_SHEET_UPDATED"))
        assert row.after["before"]["dcm"] == 45.0
        assert row.after["after"]["dcm"] == 4.5

    async def test_an_empty_patch_is_refused(self, db, sheeted):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_sheet(
                sheeted["sheets"][0]["sheet_id"], {})
        assert e.value.status_code == 422 and "Nothing to update" in e.value.detail

    @pytest.mark.parametrize("bad", [0, -3])
    async def test_a_non_positive_measurement_is_refused(self, db, sheeted, bad):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_sheet(
                sheeted["sheets"][0]["sheet_id"], {"dcm": bad})
        assert e.value.status_code == 422 and "greater than 0" in e.value.detail

    async def test_a_non_numeric_measurement_is_refused(self, db, sheeted):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_sheet(
                sheeted["sheets"][0]["sheet_id"], {"dcm": "big"})
        assert e.value.status_code == 422 and "must be a number" in e.value.detail

    async def test_a_hide_on_a_cutting_row_names_the_row(self, db, sheeted):
        """Changing it underneath the cutter would rewrite what a garment cost."""
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        row_id = uuid.uuid4()
        sheet.cutting_row_id = row_id
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await svc.update_sheet(sheet.id, {"dcm": 4.5})
        assert e.value.status_code == 409
        assert str(row_id) in e.value.detail

    @pytest.mark.parametrize("state", [SheetStatus.ALLOCATED.value,
                                       SheetStatus.ISSUED.value,
                                       SheetStatus.CONSUMED.value,
                                       SheetStatus.SCRAPPED.value])
    async def test_an_acted_upon_hide_is_refused_and_points_at_adjust(
            self, db, sheeted, state):
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        sheet.status = state
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await svc.update_sheet(sheet.id, {"dcm": 4.5})
        assert e.value.status_code == 409
        assert "/adjust" in e.value.detail

    async def test_a_returned_hide_is_back_on_the_shelf_and_editable(
            self, db, sheeted):
        """RETURNED means the cutter did not need it — it is stock again."""
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        sheet.status = SheetStatus.RETURNED.value
        await db.commit()
        assert (await svc.update_sheet(sheet.id, {"dcm": 44}))["dcm"] == 44.0

    async def test_an_unknown_hide_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_sheet(uuid.uuid4(), {"dcm": 1})
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════════════ delete
class TestDeleteSheet:
    async def test_a_hide_entered_by_mistake_is_removed(self, db, sheeted):
        sheet_id = sheeted["sheets"][3]["sheet_id"]
        out = await MaterialService(db).delete_sheet(sheet_id, actor_id=ACTOR)
        assert out["deleted"] is True and out["code"] == "LS-000004"
        assert await db.get(MaterialSheet, sheet_id) is None
        assert (await MaterialService(db).list_sheets(
            sheeted["lot_id"]))["count"] == 3

    async def test_its_label_is_retired_not_deleted(self, db, sheeted):
        """A label may already be stuck on something: that scan must read
        410 Gone, never "unknown code"."""
        from app.modules.barcode.service import BarcodeService
        sheet = sheeted["sheets"][3]
        out = await MaterialService(db).delete_sheet(sheet["sheet_id"])
        assert out["barcode_retired"] is True

        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == sheet["code"]))
        assert row is not None
        assert row.status == BarcodeStatus.RETIRED.value
        assert row.retired_reason == "sheet_deleted"
        assert row.material_sheet_id is None

        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve(sheet["code"])
        assert e.value.status_code == 410

    async def test_the_lots_stock_figure_is_untouched(self, db, sheeted):
        """A hide nobody had claimed was never part of it."""
        out = await MaterialService(db).delete_sheet(
            sheeted["sheets"][3]["sheet_id"])
        lot = await db.get(MaterialLot, sheeted["lot_id"])
        assert lot.on_hand == Decimal("3400.000")
        assert out["reconciliation"]["sheet_dcm_in_store"] == 139.0

    async def test_the_deletion_is_audited_with_what_was_removed(
            self, db, sheeted):
        from app.core.models import AuditLog
        await MaterialService(db).delete_sheet(
            sheeted["sheets"][3]["sheet_id"], actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_SHEET_DELETED"))
        assert row.after["dcm"] == 30.0 and row.after["code"] == "LS-000004"

    async def test_a_hide_a_cutter_has_been_given_is_refused(self, db, sheeted):
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        sheet.cutting_row_id = uuid.uuid4()
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await svc.delete_sheet(sheet.id)
        assert e.value.status_code == 409
        assert await db.get(MaterialSheet, sheet.id) is not None

    async def test_a_consumed_hide_is_refused_because_it_is_history(
            self, db, sheeted):
        svc = MaterialService(db)
        sheet = await svc.repo.get_sheet(sheeted["sheets"][0]["sheet_id"])
        sheet.status = SheetStatus.CONSUMED.value
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await svc.delete_sheet(sheet.id)
        assert e.value.status_code == 409
        assert "cut from" in e.value.detail

    async def test_a_hide_with_no_label_still_deletes(self, db, sheeted):
        sheet_id = sheeted["sheets"][3]["sheet_id"]
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.material_sheet_id == sheet_id))
        await db.delete(row)
        await db.commit()
        out = await MaterialService(db).delete_sheet(sheet_id)
        assert out["deleted"] is True and out["barcode_retired"] is False

    async def test_an_unknown_hide_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).delete_sheet(uuid.uuid4())
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════ arrival CRUD
class TestGetArrival:
    async def test_one_arrival_reads_back_in_the_queues_shape(self, db):
        made = await arrival(db, sheet_count=4, note="van 2")
        out = await MaterialService(db).get_arrival(made["receipt_id"])
        assert out["status"] == IntakeStatus.PENDING.value
        assert out["declared_qty"] == 3400.0
        assert out["declared_sheet_count"] == 4
        assert out["note"] == "van 2"
        assert out["outstanding"]          # it is still owed its QC split

    async def test_an_unknown_arrival_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).get_arrival(uuid.uuid4())
        assert e.value.status_code == 404


class TestUpdateArrival:
    async def test_correcting_the_quantity_moves_the_stock_it_put_on_the_floor(
            self, db):
        """3400 typed for 340: the shelf must stop claiming 3060 that never
        arrived, or the correction is cosmetic."""
        made = await arrival(db)
        out = await MaterialService(db).update_arrival(
            made["receipt_id"], {"declared_qty": 340}, actor_id=ACTOR)
        assert out["declared_qty"] == 340.0
        assert out["stock_delta"] == -3060.0
        lot = await db.get(MaterialLot, made["lot_id"])
        assert lot.on_hand == Decimal("340.000")

    async def test_the_receipt_total_follows_so_received_stays_true(self, db):
        made = await arrival(db)
        await MaterialService(db).update_arrival(
            made["receipt_id"], {"declared_qty": 340})
        receipt = await db.get(MaterialReceipt, made["receipt_id"])
        assert receipt.approved_qty == Decimal("340.000")
        history = await MaterialService(db).lot_history(made["lot_id"])
        assert history["received"] == 340.0

    async def test_the_correction_is_a_delta_so_a_cut_in_between_survives(
            self, db):
        made = await arrival(db)
        await MaterialService(db).decrement_for_cut_nocommit(made["lot_id"], 400)
        await db.commit()
        await MaterialService(db).update_arrival(
            made["receipt_id"], {"declared_qty": 3000})
        lot = await db.get(MaterialLot, made["lot_id"])
        # 3400 − 400 cut = 3000, then −400 for the correction = 2600.
        assert lot.on_hand == Decimal("2600.000")

    async def test_the_bundle_count_and_note_may_be_corrected_alone(self, db):
        made = await arrival(db, sheet_count=4)
        out = await MaterialService(db).update_arrival(
            made["receipt_id"], {"declared_sheet_count": 12, "note": "recount"})
        assert out["declared_sheet_count"] == 12 and out["note"] == "recount"
        assert out["declared_qty"] == 3400.0
        assert out["stock_delta"] == 0.0

    async def test_a_correction_that_would_go_negative_is_refused(self, db):
        made = await arrival(db)
        await MaterialService(db).decrement_for_cut_nocommit(made["lot_id"], 3300)
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_arrival(
                made["receipt_id"], {"declared_qty": 50})
        assert e.value.status_code == 409
        assert "below" in e.value.detail or "negative" in e.value.detail.lower()

    @pytest.mark.parametrize("bad", [0, -1])
    async def test_a_non_positive_quantity_is_refused(self, db, bad):
        made = await arrival(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_arrival(
                made["receipt_id"], {"declared_qty": bad})
        assert e.value.status_code == 422 and "void it instead" in e.value.detail

    async def test_a_non_numeric_quantity_is_refused(self, db):
        made = await arrival(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_arrival(
                made["receipt_id"], {"declared_qty": "lots"})
        assert e.value.status_code == 422

    async def test_a_completed_arrival_may_not_be_corrected(self, db):
        """It is the QC record of a delivery, not a draft."""
        made = await arrival(db)
        await MaterialService(db).complete_arrival(
            made["receipt_id"],
            schemas.ArrivalComplete(approved_qty=3400, rejected_qty=0),
            actor_id=ACTOR)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_arrival(
                made["receipt_id"], {"declared_qty": 340})
        assert e.value.status_code == 409 and "/adjust" in e.value.detail

    async def test_the_correction_is_audited(self, db):
        from app.core.models import AuditLog
        made = await arrival(db)
        await MaterialService(db).update_arrival(
            made["receipt_id"], {"declared_qty": 340}, actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_ARRIVAL_UPDATED"))
        assert row.after["before"]["declared_qty"] == 3400.0
        assert row.after["stock_delta"] == -3060.0

    async def test_an_unknown_arrival_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_arrival(
                uuid.uuid4(), {"declared_qty": 1})
        assert e.value.status_code == 404


class TestDeleteArrival:
    async def test_the_van_entered_twice_is_voided_and_its_stock_comes_back_out(
            self, db):
        first = await arrival(db)
        second = await arrival(db)
        lot = await db.get(MaterialLot, second["lot_id"])
        await db.refresh(lot)
        assert lot.on_hand == Decimal("6800.000")

        out = await MaterialService(db).delete_arrival(
            second["receipt_id"], actor_id=ACTOR)
        assert out["voided"] is True and out["qty_removed"] == 3400.0
        await db.refresh(lot)
        assert lot.on_hand == Decimal("3400.000")
        assert await db.get(MaterialReceipt, second["receipt_id"]) is None
        assert await db.get(MaterialReceipt, first["receipt_id"]) is not None

    async def test_the_lot_survives_even_when_that_was_its_only_delivery(
            self, db):
        """Its barcode may be printed and a recipe may point at it. A lot at
        zero is an empty shelf, which is a true statement."""
        made = await arrival(db)
        out = await MaterialService(db).delete_arrival(made["receipt_id"])
        assert out["lot_retained"] is True
        lot = await db.get(MaterialLot, made["lot_id"])
        assert lot is not None and lot.on_hand == Decimal("0.000")
        assert lot.is_active is True

    async def test_voiding_material_that_has_been_cut_is_refused(self, db):
        """Some of it was real — the correction is a quantity edit, not a void."""
        made = await arrival(db)
        await MaterialService(db).decrement_for_cut_nocommit(made["lot_id"], 100)
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).delete_arrival(made["receipt_id"])
        assert e.value.status_code == 409
        assert "already been cut" in e.value.detail
        assert await db.get(MaterialReceipt, made["receipt_id"]) is not None

    async def test_a_completed_arrival_may_not_be_voided(self, db):
        made = await arrival(db)
        await MaterialService(db).complete_arrival(
            made["receipt_id"],
            schemas.ArrivalComplete(approved_qty=3400, rejected_qty=0),
            actor_id=ACTOR)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).delete_arrival(made["receipt_id"])
        assert e.value.status_code == 409

    async def test_the_void_is_audited(self, db):
        from app.core.models import AuditLog
        made = await arrival(db)
        await MaterialService(db).delete_arrival(made["receipt_id"],
                                                 actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_ARRIVAL_VOIDED"))
        assert row.after["declared_qty"] == 3400.0

    async def test_the_arrival_leaves_the_pending_queue(self, db):
        made = await arrival(db)
        assert (await MaterialService(db).list_arrivals())["total"] == 1
        await MaterialService(db).delete_arrival(made["receipt_id"])
        assert (await MaterialService(db).list_arrivals())["total"] == 0

    async def test_an_unknown_arrival_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).delete_arrival(uuid.uuid4())
        assert e.value.status_code == 404
