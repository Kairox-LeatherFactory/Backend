"""
INTEGRATION · MaterialService, method by method, against a real session.

THE MONEY PATHS FIRST (CLAUDE.md §14). Everything in this file either moves
stock or decides whether stock may move:

    create_lot      strict per-category fields, one-lot-per-spec, child barcode,
                    the opening receipt, and the hides
    receive         approved/rejected, PO matching, DM/MD substitution
    _decrement      the one place stock comes off a lot — warn, never block —
                    and the reservation release that used to have no caller
    adjust / retire the two audited corrections
    stock / lots    what the DM and the cut screen read before they act

WHY THE SERVICE AND NOT THE ROUTER. These are business rules, and asserting them
over HTTP would mean re-proving the role gate on every one. The role gate has
its own file (tests/system/test_materials_endpoints_full.py); this one is about
what the rules DO once a legitimate caller is through.
"""
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import BarcodeStatus, SheetStatus, SupplierOrderStatus, UserRole
from app.modules.barcode.models import (
    BarcodeRegistry, MaterialLot, MaterialReceipt, MaterialReservation,
    MaterialSheet, MaterialSupplier, SupplierOrder,
)
from app.modules.materials import schemas
from app.modules.materials.service import MaterialService

pytestmark = pytest.mark.integrity

ACTOR = uuid.uuid4()


# ══════════════════════════════════════════════════════════════ helpers
def lot_body(**kw):
    """A valid LEATHER lot body; override anything per test."""
    base = dict(category="LEATHER", subtype=None, article="SUEDE-A32",
                colour="PINE", attributes={"thickness": "1.2mm", "dcm": 400})
    base.update(kw)
    return schemas.LotCreate(**base)


async def make_lot(db, **kw):
    """Create a lot through the SERVICE, so it gets its barcode and receipt."""
    return await MaterialService(db).create_lot(lot_body(**kw))


async def make_supplier(db, name="TANNERY SRL", articles="SUEDE-A32,NAP-11"):
    sup = MaterialSupplier(name=name, articles=articles, is_active=True)
    db.add(sup)
    await db.commit()
    await db.refresh(sup)
    return sup


# ══════════════════════════════════════════════════════════════ create_lot
class TestCreateLot:
    async def test_a_leather_lot_enters_stock_and_gets_a_child_barcode(self, db):
        res = await make_lot(db)
        assert res["on_hand"] == 400.0 and res["available"] == 400.0
        assert res["uom"] == "dcm"
        assert res["lot_barcode"].startswith("LOT-LEA-")

        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.material_lot_id == res["lot_id"]))
        assert row.status == BarcodeStatus.ACTIVE.value
        assert row.type == "LEATHER_LOT"

    async def test_the_create_writes_its_own_opening_receipt(self, db):
        """BUG #26: `received` undercounted by exactly the opening quantity. The
        material physically arrived when the lot was created."""
        res = await make_lot(db)
        detail = await MaterialService(db).get_lot(res["lot_id"])
        assert detail["received"] == 400.0
        assert detail["deliveries"] == 1

    @pytest.mark.parametrize("category,subtype,attrs,uom,prefix", [
        ("LINING", "PLAIN_LINING", {"thickness": "0.4mm", "mtrs": 50}, "mtrs", "LOT-LIN-"),
        ("LINING", "RIBS", {"kg": 12}, "kg", "LOT-LIN-"),
        ("LINING", "KNIT", {"pcs": 30}, "pcs", "LOT-LIN-"),
        ("ACCESSORY", "BUTTON", {"size": "18L", "count": 500}, "pcs", "LOT-ACC-"),
        ("ACCESSORY", "ZIP", {"size": "60CM", "count": 200}, "pcs", "LOT-ACC-"),
        ("ACCESSORY", "THREAD", {"thickness": "40", "mtrs": 900}, "mtrs", "LOT-ACC-"),
        ("ACCESSORY", "OTHER", {"description": "HANG TAG", "count": 99}, "pcs", "LOT-ACC-"),
    ])
    async def test_every_category_mints_its_own_barcode_type_and_unit(
            self, db, category, subtype, attrs, uom, prefix):
        res = await MaterialService(db).create_lot(lot_body(
            category=category, subtype=subtype, article=f"ART-{subtype}",
            attributes=attrs))
        assert res["uom"] == uom
        assert res["lot_barcode"].startswith(prefix)

    async def test_an_unknown_category_is_rejected(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(category="TIMBER"))
        assert e.value.status_code == 422 and "TIMBER" in e.value.detail

    async def test_an_accessory_without_a_subtype_names_the_four_it_accepts(self, db):
        """There is no generic accessory quantity, so the subtype is required."""
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(
                category="ACCESSORY", subtype=None, attributes={"count": 5}))
        assert e.value.status_code == 422
        assert "BUTTON" in e.value.detail and "ZIP" in e.value.detail

    async def test_an_unknown_lining_subtype_is_rejected(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(
                category="LINING", subtype="FLEECE", attributes={"mtrs": 10}))
        assert e.value.status_code == 422 and "FLEECE" in e.value.detail

    @pytest.mark.parametrize("field", ["article", "colour"])
    async def test_article_and_colour_are_mandatory_for_every_material(
            self, db, field):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(**{field: "   "}))
        assert e.value.status_code == 422
        assert f"{field} is required" in e.value.detail

    async def test_a_missing_required_attribute_names_what_it_wants(self, db):
        """STRICT per-category fields — the whole point of CLAUDE.md §5."""
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(attributes={"dcm": 10}))
        assert e.value.status_code == 422
        assert "thickness" in e.value.detail
        assert "supplied: dcm" in e.value.detail

    async def test_a_lot_with_no_attributes_at_all_says_so(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(attributes={}))
        assert "supplied: none" in e.value.detail

    async def test_blank_attribute_values_count_as_missing(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(
                attributes={"thickness": "  ", "dcm": 400}))
        assert e.value.status_code == 422 and "thickness" in e.value.detail

    async def test_a_non_numeric_quantity_is_rejected(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(
                attributes={"thickness": "1.2mm", "dcm": "lots"}))
        assert e.value.status_code == 422 and "must be a number" in e.value.detail

    @pytest.mark.parametrize("qty", [0, -5])
    async def test_a_non_positive_quantity_is_rejected(self, db, qty):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_lot(lot_body(
                attributes={"thickness": "1.2mm", "dcm": qty}))
        assert e.value.status_code == 422 and "must be > 0" in e.value.detail

    async def test_the_same_spec_twice_is_a_409_that_points_at_receive(self, db):
        """ONE LOT PER MATERIAL SPEC. A second row would split one material's
        stock and give the picker two rows a cutter cannot tell apart."""
        first = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await make_lot(db)
        assert e.value.status_code == 409
        assert "one lot per spec" in e.value.detail
        assert str(first["lot_id"]) in e.value.detail
        assert "/materials/receive" in e.value.detail

    async def test_a_different_thickness_is_a_different_material(self, db):
        await make_lot(db)
        res = await make_lot(db, attributes={"thickness": "0.8mm", "dcm": 100})
        assert res["on_hand"] == 100.0

    async def test_the_caption_is_printed_onto_the_registry_row(self, db):
        res = await make_lot(db)
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.material_lot_id == res["lot_id"]))
        assert row.caption == "SUEDE-A32 · PINE · 1.2mm · 400 dcm"

    async def test_a_supplier_id_is_carried_onto_the_lot(self, db):
        sup = await make_supplier(db)
        res = await make_lot(db, supplier_id=sup.id)
        lot = await db.get(MaterialLot, res["lot_id"])
        assert lot.supplier_id == sup.id


# ══════════════════════════════════════════════════════════════ the hides
class TestSheets:
    async def test_a_sheeted_delivery_mints_one_label_per_hide(self, db):
        res = await MaterialService(db).create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 90},
            sheets=[{"dcm": 43}, {"dcm": 47}]))
        assert len(res["sheets"]) == 2
        assert {s["status"] for s in res["sheets"]} == {SheetStatus.IN_STOCK.value}
        codes = [s["code"] for s in res["sheets"]]
        assert all(c.startswith("LS-") for c in codes)

        rows = (await db.execute(select(BarcodeRegistry).where(
            BarcodeRegistry.code.in_(codes)))).scalars().all()
        assert len(rows) == 2
        assert all(r.type == "LEATHER_SHEET" for r in rows)
        # A hide's label must answer BOTH "which hide" and "what article".
        assert all(r.material_sheet_id and r.material_lot_id for r in rows)

    async def test_hides_that_do_not_add_up_warn_but_the_delivery_is_kept(self, db):
        """Two decimetres of measurement slop must not refuse a receipt — the
        floor would stop sheeting. The gap is surfaced instead."""
        svc = MaterialService(db)
        res = await svc.create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 420},
            sheets=[{"dcm": 209}, {"dcm": 209}]))
        assert res["on_hand"] == 420.0          # the delivery still stands
        warning = svc.decrement_warnings[-1]
        assert warning["kind"] == "sheet_sum_mismatch"
        assert warning["difference"] == 2.0

    async def test_hides_that_add_up_raise_no_warning(self, db):
        svc = MaterialService(db)
        await svc.create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 90},
            sheets=[{"dcm": 43}, {"dcm": 47}]))
        assert svc.decrement_warnings == []

    async def test_only_leather_is_tracked_hide_by_hide(self, db):
        """A metre of lining is any other metre. Sheeting it would print labels
        to learn nothing the packet count already says."""
        res = await MaterialService(db).create_lot(lot_body(
            category="LINING", subtype="RIBS", article="RIB-1",
            attributes={"kg": 10}))
        lot = await db.get(MaterialLot, res["lot_id"])
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).mint_sheets_nocommit(lot, [{"dcm": 5}])
        assert e.value.status_code == 422
        assert "Only LEATHER" in e.value.detail

    @pytest.mark.parametrize("bad", [0, -3])
    async def test_a_hide_with_no_measurement_is_refused(self, db, bad):
        res = await make_lot(db)
        lot = await db.get(MaterialLot, res["lot_id"])
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).mint_sheets_nocommit(lot, [{"dcm": bad}])
        assert e.value.status_code == 422
        assert "greater than 0" in e.value.detail

    async def test_a_non_numeric_hide_measurement_is_refused(self, db):
        res = await make_lot(db)
        lot = await db.get(MaterialLot, res["lot_id"])
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).mint_sheets_nocommit(lot, [{"dcm": "big"}])
        assert e.value.status_code == 422 and "must be a number" in e.value.detail

    async def test_sheet_codes_keep_counting_up_across_lots(self, db):
        svc = MaterialService(db)
        first = await svc.create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 43}, sheets=[{"dcm": 43}]))
        second = await svc.create_lot(lot_body(
            article="NAP-11", attributes={"thickness": "1.2mm", "dcm": 47},
            sheets=[{"dcm": 47}]))
        assert first["sheets"][0]["code"] != second["sheets"][0]["code"]

    async def test_reconciliation_reports_the_gap_without_enforcing_it(self, db):
        svc = MaterialService(db)
        res = await svc.create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 100},
            sheets=[{"dcm": 40}, {"dcm": 50}]))
        lot = await db.get(MaterialLot, res["lot_id"])
        rec = await svc.sheet_reconciliation(lot)
        assert rec["sheets_total"] == 2
        assert rec["sheet_dcm_in_store"] == 90.0
        assert rec["difference"] == 10.0
        assert rec["reconciled"] is False

    async def test_a_lot_with_no_hides_is_not_a_mismatch(self, db):
        """A lot received before sheet tracking existed. Reporting it as a
        mismatch would make every legacy lot look broken."""
        svc = MaterialService(db)
        res = await svc.create_lot(lot_body())
        lot = await db.get(MaterialLot, res["lot_id"])
        rec = await svc.sheet_reconciliation(lot)
        assert rec["sheets_total"] == 0 and rec["reconciled"] is True

    async def test_a_lot_whose_hides_match_reconciles(self, db):
        svc = MaterialService(db)
        res = await svc.create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 90},
            sheets=[{"dcm": 43}, {"dcm": 47}]))
        lot = await db.get(MaterialLot, res["lot_id"])
        assert (await svc.sheet_reconciliation(lot))["reconciled"] is True

    async def test_a_note_on_a_hide_is_stored(self, db):
        res = await MaterialService(db).create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 43},
            sheets=[{"dcm": 43, "note": "scar on the shoulder"}]))
        sheet = await db.get(MaterialSheet, res["sheets"][0]["sheet_id"])
        assert sheet.note == "scar on the shoulder"


# ══════════════════════════════════════════════════════════════ get_lot
class TestGetLot:
    async def test_the_detail_carries_the_three_stock_numbers_and_the_barcode(
            self, db):
        res = await make_lot(db)
        detail = await MaterialService(db).get_lot(res["lot_id"])
        assert detail["on_hand"] == 400.0
        assert detail["reserved"] == 0.0
        assert detail["available"] == 400.0
        assert detail["barcode"] == res["lot_barcode"]
        assert detail["is_active"] is True

    async def test_it_echoes_the_fields_the_edit_form_may_show(self, db):
        res = await make_lot(db)
        detail = await MaterialService(db).get_lot(res["lot_id"])
        assert "article" in detail["editable_fields"]
        assert "thickness" in detail["editable_fields"]
        assert detail["required_attributes"] == ["dcm", "thickness"]

    async def test_a_reservation_lowers_available_but_not_on_hand(self, db):
        res = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=res["lot_id"],
                                   qty=Decimal("50"), status="active"))
        await db.commit()
        detail = await MaterialService(db).get_lot(res["lot_id"])
        assert detail["on_hand"] == 400.0
        assert detail["reserved"] == 50.0
        assert detail["available"] == 350.0

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).get_lot(uuid.uuid4())
        assert e.value.status_code == 404


class TestLotHistory:
    async def test_every_delivery_is_listed_newest_first(self, db):
        svc = MaterialService(db)
        res = await svc.create_lot(lot_body())
        await svc.receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=100, rejected_qty=5),
            actor_id=ACTOR)
        history = await svc.lot_history(res["lot_id"])
        assert len(history["receipts"]) == 2
        assert history["received"] == 500.0     # 400 opening + 100
        assert history["rejected"] == 5.0
        assert history["on_hand"] == 500.0

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).lot_history(uuid.uuid4())
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════════════ update_lot
class TestUpdateLot:
    async def test_an_identity_field_may_be_corrected(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).update_lot(
            res["lot_id"], {"article": "SUEDE-A33"})
        assert out["article"] == "SUEDE-A33"

    async def test_a_promoted_column_is_mirrored_into_the_json_attributes(self, db):
        """Or the printed caption and the filter columns tell different stories."""
        res = await make_lot(db)
        await MaterialService(db).update_lot(res["lot_id"], {"thickness": "0.8mm"})
        lot = await db.get(MaterialLot, res["lot_id"])
        assert lot.attributes["thickness"] == "0.8mm"

    @pytest.mark.parametrize("blocked,value", [
        ("category", "LINING"), ("subtype", "RIBS"),
        ("on_hand", 999), ("uom", "mtrs")])
    async def test_the_four_unpatchable_fields_are_refused_by_name(
            self, db, blocked, value):
        """on_hand is a LEDGER, and category decides the UOM. Either would make
        the stock and its movement history disagree with no record."""
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_lot(res["lot_id"], {blocked: value})
        assert e.value.status_code == 422
        assert f"'{blocked}' cannot be patched" in e.value.detail

    async def test_an_empty_patch_is_refused(self, db):
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_lot(res["lot_id"], {})
        assert e.value.status_code == 422 and "Nothing to update" in e.value.detail

    async def test_renaming_a_lot_onto_another_lots_spec_is_a_409(self, db):
        """Two rows a cutting manager cannot tell apart, with one material's
        stock split across both."""
        first = await make_lot(db)
        second = await make_lot(db, article="NAP-11")
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_lot(
                second["lot_id"], {"article": "SUEDE-A32"})
        assert e.value.status_code == 409
        assert str(first["lot_id"]) in e.value.detail

    async def test_patching_a_lot_onto_its_own_spec_is_allowed(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).update_lot(
            res["lot_id"], {"article": "SUEDE-A32", "colour": "FOREST"})
        assert out["colour"] == "FOREST"

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).update_lot(uuid.uuid4(), {"article": "X"})
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════════════ adjust_lot
class TestAdjustLot:
    async def test_a_counted_correction_moves_stock_and_writes_an_audit_row(
            self, db):
        from app.core.models import AuditLog
        res = await make_lot(db)
        out = await MaterialService(db).adjust_lot(
            res["lot_id"], delta=-40, reason="counted short at stocktake",
            actor_id=ACTOR)
        assert out["on_hand"] == 360.0

        row = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_STOCK_ADJUSTED"))
        assert row.after["before"] == 400.0 and row.after["after"] == 360.0
        assert row.after["reason"] == "counted short at stocktake"

    async def test_a_positive_delta_adds(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).adjust_lot(
            res["lot_id"], delta=12.5, reason="found a roll")
        assert out["on_hand"] == 412.5

    @pytest.mark.parametrize("reason", ["", "  ", "ab"])
    async def test_a_correction_without_a_reason_is_refused(self, db, reason):
        """The reason is the only record of why the count and the system
        disagreed."""
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).adjust_lot(
                res["lot_id"], delta=-1, reason=reason)
        assert e.value.status_code == 422 and "Give a reason" in e.value.detail

    async def test_a_zero_delta_is_refused(self, db):
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).adjust_lot(
                res["lot_id"], delta=0, reason="nothing changed")
        assert e.value.status_code == 422 and "non-zero" in e.value.detail

    async def test_a_non_numeric_delta_is_refused(self, db):
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).adjust_lot(
                res["lot_id"], delta="lots", reason="typed by hand")
        assert e.value.status_code == 422 and "delta must be a number" in e.value.detail

    async def test_stock_cannot_be_driven_negative(self, db):
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).adjust_lot(
                res["lot_id"], delta=-500, reason="wrong by a lot")
        assert e.value.status_code == 422 and "cannot go negative" in e.value.detail

    async def test_stock_cannot_fall_below_what_is_already_reserved(self, db):
        """That stock is committed to a cut; a negative available is not a
        number anyone can act on."""
        res = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=res["lot_id"],
                                   qty=Decimal("300"), status="active"))
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).adjust_lot(
                res["lot_id"], delta=-200, reason="stocktake")
        assert e.value.status_code == 409 and "reserved for a" in e.value.detail

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).adjust_lot(
                uuid.uuid4(), delta=1, reason="does not exist")
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════════════ retire_lot
# WAS AN OPEN DEFECT, NOW FIXED — keep these tests, they are the regression.
# service.py called `self.barcodes.retire_lot_code_nocommit(...)` where
# `self.barcodes` is a BarcodeRepository and the method lives on BarcodeService,
# so DELETE /materials/lots/{id} was an unconditional AttributeError -> 500 and
# a lot could never be retired through the API.
#
# THE MISTAKE IS RECURRING, WHICH IS WHY THIS NOTE STAYS. The docstring on
# `display_stock` (materials/service.py) records the identical shape: a SERVICE
# method reached through a REPOSITORY handle, invisible until the line runs.
# Anything new on `self.barcodes` belongs on BarcodeRepository; anything that
# needs BarcodeService imports it in the method.


class TestRetireLot:
    async def test_retiring_deactivates_the_lot_and_its_label(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).retire_lot(res["lot_id"], actor_id=ACTOR)
        assert out["is_active"] is False
        assert out["barcode_retired"] is True
        assert out["history_preserved"] is True

        lot = await db.get(MaterialLot, res["lot_id"])
        assert lot.is_active is False
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.material_lot_id == res["lot_id"]))
        assert row.status == BarcodeStatus.RETIRED.value
        assert row.retired_reason == "lot_retired"

    async def test_the_row_survives_so_the_consumption_history_is_not_orphaned(
            self, db):
        res = await make_lot(db)
        await MaterialService(db).retire_lot(res["lot_id"])
        assert await db.get(MaterialLot, res["lot_id"]) is not None

    async def test_a_retired_lot_leaves_the_picker(self, db):
        res = await make_lot(db)
        await MaterialService(db).retire_lot(res["lot_id"])
        listing = await MaterialService(db).list_lots(category="LEATHER")
        assert listing["count"] == 0

    async def test_retiring_is_refused_while_stock_is_reserved(self, db):
        res = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=res["lot_id"],
                                   qty=Decimal("10"), status="active"))
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).retire_lot(res["lot_id"])
        assert e.value.status_code == 409 and "still reserved" in e.value.detail

    async def test_a_lot_with_no_label_still_retires(self, db):
        lot = MaterialLot(category="LEATHER", article="ORPHAN", uom="dcm",
                          on_hand=Decimal("1"), is_active=True)
        db.add(lot)
        await db.commit()
        out = await MaterialService(db).retire_lot(lot.id)
        assert out["barcode_retired"] is False and out["is_active"] is False

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).retire_lot(uuid.uuid4())
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════════════ the picker
class TestListLots:
    async def test_the_picker_hands_the_cut_screen_a_lot_id(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).list_lots(category="LEATHER")
        assert out["count"] == 1
        assert out["lots"][0]["lot_id"] == res["lot_id"]
        assert out["lots"][0]["barcode"] == res["lot_barcode"]

    async def test_the_options_block_fills_the_cascading_dropdowns(self, db):
        await make_lot(db)
        await make_lot(db, colour="FOREST")
        out = await MaterialService(db).list_lots(category="LEATHER")
        assert out["options"]["article"] == ["SUEDE-A32"]
        assert out["options"]["colour"] == ["FOREST", "PINE"]

    @pytest.mark.parametrize("filters,expect", [
        ({"article": "SUEDE-A32"}, 1),
        ({"colour": "FOREST"}, 0),
        ({"thickness": "1.2mm"}, 1),
        ({"thickness": "9mm"}, 0),
        ({"subtype": "RIBS"}, 0),
        ({"size": "18L"}, 0),
    ])
    async def test_each_filter_narrows_the_list(self, db, filters, expect):
        await make_lot(db)
        out = await MaterialService(db).list_lots(category="LEATHER", **filters)
        assert out["count"] == expect

    async def test_required_flags_which_lots_can_cover_the_batch(self, db):
        """So a lot that cannot cover the cut is greyed out BEFORE it, rather
        than warned about after."""
        await make_lot(db)
        covers = await MaterialService(db).list_lots(
            category="LEATHER", required=100)
        short = await MaterialService(db).list_lots(
            category="LEATHER", required=10_000)
        assert covers["lots"][0]["covers_required"] is True
        assert short["lots"][0]["covers_required"] is False

    async def test_covers_required_is_null_when_the_caller_did_not_say(self, db):
        await make_lot(db)
        out = await MaterialService(db).list_lots(category="LEATHER")
        assert out["lots"][0]["covers_required"] is None
        assert out["required"] is None

    async def test_an_exhausted_lot_is_returned_not_hidden(self, db):
        """A manager searching for a lot they know exists must find it, with
        available: 0 explaining itself."""
        res = await make_lot(db)
        await MaterialService(db).decrement_for_cut_nocommit(res["lot_id"], 400)
        await db.commit()
        out = await MaterialService(db).list_lots(category="LEATHER")
        assert out["count"] == 1
        assert out["lots"][0]["available"] == 0.0

    async def test_the_last_lot_this_sku_was_cut_from_is_pre_selected(
            self, db, order_tree, pieces, operations):
        """Derived live from production_event, so it cannot go stale."""
        from app.modules.production.models import ProductionEvent
        res = await make_lot(db)
        piece, _ = pieces[0]
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=piece.id,
            operation_id=operations["LEATHER_CUTTING"].id,
            work_date=date.today(), leather_lot_id=res["lot_id"],
            consumption_qty=Decimal("12.5")))
        await db.commit()

        out = await MaterialService(db).list_lots(
            category="LEATHER", sku_id=order_tree["sku"].id)
        assert out["suggested_lot_id"] == res["lot_id"]
        assert out["lots"][0]["last_used_for_sku"] is True

    async def test_the_lining_suggestion_reads_the_lining_column(
            self, db, order_tree, pieces, operations):
        from app.modules.production.models import ProductionEvent
        lining = await make_lot(db, category="LINING", subtype="PLAIN_LINING",
                                article="POLY-1",
                                attributes={"thickness": "0.4mm", "mtrs": 50})
        piece, _ = pieces[0]
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=piece.id,
            operation_id=operations["LINING_CUTTING"].id,
            work_date=date.today(), lining_lot_id=lining["lot_id"]))
        await db.commit()

        out = await MaterialService(db).list_lots(
            category="LINING", sku_id=order_tree["sku"].id)
        assert out["suggested_lot_id"] == lining["lot_id"]

    async def test_the_suggested_lot_is_sorted_to_the_top(
            self, db, order_tree, pieces, operations):
        from app.modules.production.models import ProductionEvent
        await make_lot(db, article="AAA-FIRST")
        target = await make_lot(db, article="ZZZ-LAST")
        piece, _ = pieces[0]
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=piece.id,
            operation_id=operations["LEATHER_CUTTING"].id,
            work_date=date.today(), leather_lot_id=target["lot_id"],
            consumption_qty=Decimal("1")))
        await db.commit()
        out = await MaterialService(db).list_lots(
            category="LEATHER", sku_id=order_tree["sku"].id)
        assert out["lots"][0]["lot_id"] == target["lot_id"]

    async def test_an_empty_picker_returns_empty_options_not_an_error(self, db):
        out = await MaterialService(db).list_lots(category="LEATHER")
        assert out["count"] == 0 and out["options"]["article"] == []


# ══════════════════════════════════════════════════════════════ the stock check
class TestStock:
    async def test_stock_sums_every_matching_lot(self, db):
        await make_lot(db)
        await make_lot(db, colour="FOREST", attributes={"thickness": "1.2mm",
                                                        "dcm": 100})
        out = await MaterialService(db).stock(category="LEATHER")
        assert out["on_hand"] == 500.0
        assert out["lot_count"] == 2
        assert out["uom"] == "dcm"

    async def test_consumption_shows_as_used_while_received_stays_whole(self, db):
        res = await make_lot(db)
        await MaterialService(db).decrement_for_cut_nocommit(res["lot_id"], 150)
        await db.commit()
        out = await MaterialService(db).stock(category="LEATHER")
        assert out["used"] == 150.0
        assert out["on_hand"] == 400.0          # received, not remaining
        assert out["available"] == 250.0

    async def test_a_shortfall_is_computed_and_a_supplier_suggested(self, db):
        sup = await make_supplier(db)
        await make_lot(db)
        out = await MaterialService(db).stock(
            category="LEATHER", article="SUEDE-A32", required=600)
        assert out["short_by"] == 200.0
        assert out["suggested_supplier"]["id"] == str(sup.id)

    async def test_no_shortfall_means_no_supplier_suggestion(self, db):
        await make_supplier(db)
        await make_lot(db)
        out = await MaterialService(db).stock(
            category="LEATHER", article="SUEDE-A32", required=100)
        assert out["short_by"] == 0.0
        assert "suggested_supplier" not in out

    async def test_a_shortfall_with_no_supplier_on_file_reports_none(self, db):
        await make_lot(db)
        out = await MaterialService(db).stock(
            category="LEATHER", article="SUEDE-A32", required=9999)
        assert out["suggested_supplier"] is None

    async def test_a_shortfall_with_no_article_skips_the_lookup(self, db):
        await make_lot(db)
        out = await MaterialService(db).stock(category="LEATHER", required=9999)
        assert out["short_by"] > 0 and "suggested_supplier" not in out

    async def test_stock_for_a_material_that_does_not_exist_falls_back_to_its_uom(
            self, db):
        out = await MaterialService(db).stock(category="ACCESSORY",
                                              subtype="BUTTON")
        assert out["lot_count"] == 0 and out["uom"] == "pcs"

    async def test_an_active_reservation_is_subtracted_from_available(self, db):
        res = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=res["lot_id"],
                                   qty=Decimal("100"), status="active"))
        await db.commit()
        out = await MaterialService(db).stock(category="LEATHER")
        assert out["reserved"] == 100.0 and out["available"] == 300.0


class TestAvailableForLot:
    async def test_it_reports_on_hand_minus_reservations(self, db):
        res = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=res["lot_id"],
                                   qty=Decimal("40"), status="active"))
        await db.commit()
        assert await MaterialService(db).available_for_lot(res["lot_id"]) == 360.0

    async def test_an_unknown_lot_reports_zero_rather_than_raising(self, db):
        assert await MaterialService(db).available_for_lot(uuid.uuid4()) == 0.0


# ══════════════════════════════════════════════════════════════ receive
class TestReceive:
    async def test_an_approved_quantity_is_added_to_the_lot(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=100), actor_id=ACTOR)
        assert out["on_hand"] == 500.0
        assert out["substituted"] is False

    async def test_a_rejected_quantity_is_logged_without_entering_stock(self, db):
        """The supplier quality history — see repo.rejected_history."""
        res = await make_lot(db)
        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=90, rejected_qty=10),
            actor_id=ACTOR)
        assert out["on_hand"] == 490.0
        assert out["rejected_logged"] == 10.0

    async def test_a_receiving_reservation_is_recorded(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=100, reserve_for_required=60),
            actor_id=ACTOR)
        assert out["reserved"] == 60.0
        assert out["available"] == 440.0

    async def test_an_unknown_lot_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).receive(schemas.ReceiveRequest(
                lot_id=uuid.uuid4(), approved_qty=1), actor_id=ACTOR)
        assert e.value.status_code == 404 and e.value.detail == "Lot not found."

    async def test_an_unknown_supplier_order_is_a_404(self, db):
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).receive(schemas.ReceiveRequest(
                lot_id=res["lot_id"], approved_qty=1,
                supplier_order_id=uuid.uuid4()), actor_id=ACTOR)
        assert e.value.status_code == 404
        assert "Supplier order not found" in e.value.detail

    async def test_negative_quantities_are_refused(self, db):
        """The schema blocks this at the wire; the service blocks it for every
        other caller."""
        res = await make_lot(db)
        body = schemas.ReceiveRequest(lot_id=res["lot_id"], approved_qty=0)
        object.__setattr__(body, "approved_qty", -1)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).receive(body, actor_id=ACTOR)
        assert e.value.status_code == 422 and "negative" in e.value.detail

    async def test_receiving_against_a_matching_order_flips_it_to_arrived(self, db):
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="SUEDE-A32",
                              colour="PINE", thickness="1.2mm",
                              qty=Decimal("100"), uom="dcm",
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()

        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=100,
            supplier_order_id=order.id), actor_id=ACTOR)
        assert out["supplier_order_status"] == SupplierOrderStatus.ARRIVED.value
        assert out["mismatch_fields"] is None

    async def test_an_already_arrived_order_keeps_its_status(self, db):
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="SUEDE-A32",
                              qty=Decimal("10"), uom="dcm",
                              status=SupplierOrderStatus.ARRIVED.value)
        db.add(order)
        await db.commit()
        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=10,
            supplier_order_id=order.id), actor_id=ACTOR)
        assert out["supplier_order_status"] == SupplierOrderStatus.ARRIVED.value

    async def test_a_mismatched_delivery_is_refused_and_nothing_enters_stock(
            self, db):
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("100"), uom="dcm",
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()

        with pytest.raises(HTTPException) as e:
            await MaterialService(db).receive(schemas.ReceiveRequest(
                lot_id=res["lot_id"], approved_qty=100,
                supplier_order_id=order.id), actor_id=ACTOR)
        assert e.value.status_code == 409
        assert "article" in e.value.detail
        assert "approve_mismatch=true" in e.value.detail

        lot = await db.get(MaterialLot, res["lot_id"])
        assert lot.on_hand == Decimal("400.000")

    async def test_a_non_dm_cannot_force_a_mismatch_through(self, db):
        """The substitution is a COSTING decision, so widening the receiving
        door to HR does not widen this."""
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("100"), uom="dcm",
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).receive(schemas.ReceiveRequest(
                lot_id=res["lot_id"], approved_qty=100,
                supplier_order_id=order.id, approve_mismatch=True),
                actor_id=ACTOR, actor_role=UserRole.HR)
        assert e.value.status_code == 409

    @pytest.mark.parametrize("role", [UserRole.DIRECT_MANAGER,
                                      UserRole.MANAGING_DIRECTOR])
    async def test_a_dm_substitution_receives_into_a_new_lot(self, db, role):
        """The originally-targeted lot is never topped up with the wrong
        material."""
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("100"), uom="dcm",
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()

        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=100,
            supplier_order_id=order.id, approve_mismatch=True),
            actor_id=ACTOR, actor_role=role)

        assert out["substituted"] is True
        assert out["lot_id"] != res["lot_id"]
        assert out["on_hand"] == 100.0
        assert out["mismatch_fields"] == ["article"]

        original = await db.get(MaterialLot, res["lot_id"])
        assert original.on_hand == Decimal("400.000")

    async def test_a_substitution_mints_its_own_label_and_audit_row(self, db):
        from app.core.models import AuditLog
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("50"), uom="dcm",
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()

        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=50,
            supplier_order_id=order.id, approve_mismatch=True),
            actor_id=ACTOR, actor_role=UserRole.DIRECT_MANAGER)

        label = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.material_lot_id == out["lot_id"]))
        assert label.caption.startswith("SUBSTITUTE · ")

        audit = await db.scalar(select(AuditLog).where(
            AuditLog.action == "MATERIAL_RECEIVED_SUBSTITUTE"))
        assert audit.after["original_lot_id"] == str(res["lot_id"])

    @pytest.mark.parametrize("category,subtype,attrs", [
        ("LINING", "RIBS", {"kg": 10}),
        ("ACCESSORY", "BUTTON", {"size": "18L", "count": 100}),
    ])
    async def test_a_substitution_works_for_every_category(
            self, db, category, subtype, attrs):
        res = await MaterialService(db).create_lot(lot_body(
            category=category, subtype=subtype, article="SUB-ART",
            attributes=attrs))
        order = SupplierOrder(category=category, article="SOMETHING-ELSE",
                              qty=Decimal("5"), uom="x",
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()
        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=5,
            supplier_order_id=order.id, approve_mismatch=True),
            actor_id=ACTOR, actor_role=UserRole.MANAGING_DIRECTOR)
        assert out["substituted"] is True

    async def test_hides_in_a_substituted_delivery_follow_the_substitute_lot(
            self, db):
        """Sheeting them to the ordered lot would file real hides under an
        article nobody received."""
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="NAP-11",
                              qty=Decimal("90"), uom="dcm",
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()

        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=90,
            supplier_order_id=order.id, approve_mismatch=True,
            sheets=[{"dcm": 43}, {"dcm": 47}]),
            actor_id=ACTOR, actor_role=UserRole.DIRECT_MANAGER)

        sheet = await db.get(MaterialSheet, out["sheets"][0]["sheet_id"])
        assert sheet.material_lot_id == out["lot_id"] != res["lot_id"]

    async def test_a_sheeted_receipt_returns_its_reconciliation(self, db):
        res = await make_lot(db)
        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=90,
            sheets=[{"dcm": 43}, {"dcm": 47}]), actor_id=ACTOR)
        assert out["sheet_reconciliation"]["sheets_total"] == 2
        assert len(out["sheets"]) == 2

    async def test_an_unsheeted_receipt_reports_no_reconciliation_block(self, db):
        """A zeroed block would read as a mismatch rather than as an absence."""
        res = await make_lot(db)
        out = await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=10), actor_id=ACTOR)
        assert out["sheet_reconciliation"] is None
        assert out["sheets"] == []

    async def test_every_receipt_leaves_a_row_for_the_purchase_history(self, db):
        res = await make_lot(db)
        await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=10, rejected_qty=2),
            actor_id=ACTOR)
        rows = (await db.execute(select(MaterialReceipt).where(
            MaterialReceipt.material_lot_id == res["lot_id"]))).scalars().all()
        assert len(rows) == 2                      # opening + this delivery
        assert any(r.received_by == ACTOR for r in rows)


# ══════════════════════════════════════════════ the decrement (the money path)
class TestDecrement:
    async def test_a_cut_takes_stock_off_the_lot_and_records_what_was_used(
            self, db):
        res = await make_lot(db)
        svc = MaterialService(db)
        available = await svc.decrement_for_cut_nocommit(res["lot_id"], 12.5)
        await db.commit()

        lot = await db.get(MaterialLot, res["lot_id"])
        assert lot.on_hand == Decimal("387.500")
        assert lot.used == Decimal("12.500")
        assert available == 387.5
        assert svc.last_decrement_warning is None

    async def test_an_issue_uses_the_same_arithmetic_with_its_own_vocabulary(
            self, db):
        res = await make_lot(db)
        svc = MaterialService(db)
        await svc.decrement_for_issue_nocommit(res["lot_id"], 400.5)
        assert svc.last_decrement_warning["note"].startswith("Issued ")

    async def test_cutting_more_than_is_on_hand_warns_but_records_the_cut(
            self, db):
        """The garment is physically on the table. Refusing the log would lose
        the production record to protect a number."""
        res = await make_lot(db)
        svc = MaterialService(db)
        await svc.decrement_for_cut_nocommit(res["lot_id"], 500)
        await db.commit()

        warning = svc.last_decrement_warning
        assert warning["short_by"] == 100.0
        assert warning["available_before"] == 400.0
        assert warning["on_hand_after"] == -100.0
        assert "Cut 500.0 dcm of SUEDE-A32" in warning["note"]
        assert "The cut WAS recorded" in warning["note"]

        lot = await db.get(MaterialLot, res["lot_id"])
        assert lot.on_hand == Decimal("-100.000")

    async def test_every_warning_on_one_instance_is_accumulated(self, db):
        """A kit spends several lots in one scan, so a single 'last' warning
        cannot describe it."""
        a = await make_lot(db)
        b = await make_lot(db, article="NAP-11")
        svc = MaterialService(db)
        await svc.decrement_for_issue_nocommit(a["lot_id"], 500)
        await svc.decrement_for_issue_nocommit(b["lot_id"], 500)
        assert len(svc.decrement_warnings) == 2

    async def test_consumption_releases_the_reservation_it_satisfied(self, db):
        """Nothing called consume_reservations before: every reservation stayed
        active forever and `available` fell monotonically."""
        res = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=res["lot_id"],
                                   qty=Decimal("50"), status="active"))
        await db.commit()

        svc = MaterialService(db)
        available = await svc.decrement_for_cut_nocommit(res["lot_id"], 50)
        await db.commit()

        row = await db.scalar(select(MaterialReservation).where(
            MaterialReservation.material_lot_id == res["lot_id"]))
        assert row.status == "consumed" and row.qty == Decimal("0.000")
        assert row.released_at is not None
        assert available == 350.0

    async def test_a_partial_consumption_leaves_the_rest_of_the_reservation(
            self, db):
        res = await make_lot(db)
        db.add(MaterialReservation(material_lot_id=res["lot_id"],
                                   qty=Decimal("50"), status="active"))
        await db.commit()
        await MaterialService(db).decrement_for_cut_nocommit(res["lot_id"], 20)
        await db.commit()
        row = await db.scalar(select(MaterialReservation).where(
            MaterialReservation.material_lot_id == res["lot_id"]))
        assert row.status == "active" and row.qty == Decimal("30.000")

    async def test_reservations_are_consumed_oldest_first(self, db):
        """created_at is the ordering key, so the timestamps are set
        explicitly: SQLite stores whole seconds, and two rows written in the
        same second would be ordered by the uuid tie-break instead."""
        from datetime import datetime, timedelta, timezone
        res = await make_lot(db)
        base = datetime.now(timezone.utc)
        for qty, offset in ((10, 0), (20, 60)):
            db.add(MaterialReservation(
                material_lot_id=res["lot_id"], qty=Decimal(qty), status="active",
                reason=f"r{qty}", created_at=base + timedelta(seconds=offset)))
        await db.commit()

        await MaterialService(db).decrement_for_cut_nocommit(res["lot_id"], 15)
        await db.commit()

        rows = (await db.execute(select(MaterialReservation).order_by(
            MaterialReservation.created_at))).scalars().all()
        assert rows[0].status == "consumed" and rows[0].qty == Decimal("0.000")
        assert rows[1].status == "active" and rows[1].qty == Decimal("15.000")

    @pytest.mark.parametrize("qty", [0, -1])
    async def test_a_non_positive_consumption_is_refused(self, db, qty):
        res = await make_lot(db)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).decrement_for_cut_nocommit(res["lot_id"], qty)
        assert e.value.status_code == 422
        assert "at cutting" in e.value.detail

    async def test_a_missing_lot_names_the_operation_that_wanted_it(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).decrement_for_cut_nocommit(uuid.uuid4(), 1)
        assert e.value.detail == "Consumption lot not found."
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).decrement_for_issue_nocommit(uuid.uuid4(), 1)
        assert e.value.detail == "Issue lot not found."


# ══════════════════════════════════════════════════════════════ supplier orders
class TestSupplierOrders:
    async def test_an_order_is_raised_in_the_ordered_state(self, db):
        out = await MaterialService(db).create_order(schemas.SupplierOrderCreate(
            category="LEATHER", article="SUEDE-A32", qty=400), actor_id=ACTOR)
        assert out["status"] == SupplierOrderStatus.ORDERED.value
        assert out["uom"] == "dcm"
        assert out["supplier"] is None

    async def test_a_supplier_is_suggested_from_the_article(self, db):
        sup = await make_supplier(db)
        out = await MaterialService(db).create_order(schemas.SupplierOrderCreate(
            category="LEATHER", article="SUEDE-A32", qty=10), actor_id=ACTOR)
        assert out["supplier"]["name"] == sup.name

    async def test_an_explicit_supplier_that_does_not_carry_the_article_is_refused(
            self, db):
        """Do not create an order the supplier cannot fill."""
        sup = await make_supplier(db, articles="BTN-4H")
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_order(schemas.SupplierOrderCreate(
                category="LEATHER", article="SUEDE-A32", qty=10,
                supplier_id=sup.id), actor_id=ACTOR)
        assert e.value.status_code == 422
        assert "does not supply" in e.value.detail

    async def test_a_supplier_with_no_catalog_at_all_is_refused(self, db):
        sup = await make_supplier(db, articles=None)
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_order(schemas.SupplierOrderCreate(
                category="LEATHER", article="SUEDE-A32", qty=10,
                supplier_id=sup.id), actor_id=ACTOR)
        assert e.value.status_code == 422

    async def test_an_unknown_supplier_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).create_order(schemas.SupplierOrderCreate(
                category="LEATHER", article="X", qty=1,
                supplier_id=uuid.uuid4()), actor_id=ACTOR)
        assert e.value.status_code == 404

    async def test_the_full_leather_spec_is_stored_on_the_order(self, db):
        out = await MaterialService(db).create_order(schemas.SupplierOrderCreate(
            category="leather", article="SUEDE-A32", colour="PINE",
            thickness="1.2mm", dcm=400, qty=10), actor_id=ACTOR)
        order = await db.get(SupplierOrder, out["order_id"])
        assert order.category == "LEATHER"
        assert order.thickness == "1.2mm" and order.dcm == Decimal("400.000")

    async def test_mark_arrived_flips_the_state_and_stamps_the_time(self, db):
        out = await MaterialService(db).create_order(schemas.SupplierOrderCreate(
            category="LEATHER", article="X", qty=1), actor_id=ACTOR)
        res = await MaterialService(db).mark_arrived(out["order_id"])
        assert res["status"] == SupplierOrderStatus.ARRIVED.value
        assert res["arrived_at"] is not None

    async def test_mark_arrived_is_idempotent(self, db):
        out = await MaterialService(db).create_order(schemas.SupplierOrderCreate(
            category="LEATHER", article="X", qty=1), actor_id=ACTOR)
        first = await MaterialService(db).mark_arrived(out["order_id"])
        second = await MaterialService(db).mark_arrived(out["order_id"])
        assert first["arrived_at"] == second["arrived_at"]

    async def test_mark_arrived_on_an_unknown_order_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).mark_arrived(uuid.uuid4())
        assert e.value.status_code == 404

    async def test_an_ordered_spec_may_be_corrected_with_an_audit_trail(self, db):
        from app.core.models import AuditLog
        created = await MaterialService(db).create_order(
            schemas.SupplierOrderCreate(category="LEATHER", article="SUEDE-A32",
                                        qty=400), actor_id=ACTOR)
        await MaterialService(db).edit_order_spec(
            created["order_id"],
            schemas.OrderSpecPatch(article="NAP-11", colour="NAVY",
                                   thickness="0.8mm", dcm=350, qty=380),
            actor_id=ACTOR)
        order = await db.get(SupplierOrder, created["order_id"])
        assert order.article == "NAP-11" and order.qty == Decimal("380.000")

        audit = await db.scalar(select(AuditLog).where(
            AuditLog.action == "SUPPLIER_ORDER_SPEC_EDIT"))
        assert audit.after["before"]["article"] == "SUEDE-A32"
        assert audit.after["after"]["qty"] == 380.0

    async def test_an_empty_patch_leaves_the_order_untouched(self, db):
        created = await MaterialService(db).create_order(
            schemas.SupplierOrderCreate(category="LEATHER", article="SUEDE-A32",
                                        qty=400), actor_id=ACTOR)
        await MaterialService(db).edit_order_spec(
            created["order_id"], schemas.OrderSpecPatch(), actor_id=ACTOR)
        order = await db.get(SupplierOrder, created["order_id"])
        assert order.article == "SUEDE-A32"

    async def test_an_arrived_order_may_no_longer_be_edited(self, db):
        created = await MaterialService(db).create_order(
            schemas.SupplierOrderCreate(category="LEATHER", article="X", qty=1),
            actor_id=ACTOR)
        await MaterialService(db).mark_arrived(created["order_id"])
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).edit_order_spec(
                created["order_id"], schemas.OrderSpecPatch(article="Y"),
                actor_id=ACTOR)
        assert e.value.status_code == 409 and "ORDERED" in e.value.detail

    async def test_editing_an_unknown_order_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await MaterialService(db).edit_order_spec(
                uuid.uuid4(), schemas.OrderSpecPatch(article="Y"), actor_id=ACTOR)
        assert e.value.status_code == 404


# ══════════════════════════════════════════════════════════════ the reports
class TestConsumptionReports:
    @pytest.fixture
    async def cut_history(self, db, order_tree, pieces, operations):
        """Two garments cut from one lot, the second one a rework."""
        from app.modules.production.models import ProductionEvent
        lot = await make_lot(db)
        for (piece, _), qty, rework in ((pieces[0], 12.5, False),
                                        (pieces[1], 7.5, True)):
            db.add(ProductionEvent(
                sku_id=order_tree["sku"].id, piece_id=piece.id,
                operation_id=operations["LEATHER_CUTTING"].id,
                work_date=date.today(), leather_lot_id=lot["lot_id"],
                consumption_qty=Decimal(str(qty)), is_rework=rework))
        await db.commit()
        return lot

    async def test_leather_by_style_splits_the_cost_by_rework(
            self, db, cut_history, order_tree):
        """'This style cost X, of which Y was defects' — averaging the two hides
        how much the floor is losing."""
        rows = await MaterialService(db).leather_by_style(
            style_id=order_tree["style"].id)
        assert len(rows) == 1
        row = rows[0]
        assert row["consumed"] == 20.0
        assert row["consumed_rework"] == 7.5
        assert row["consumed_original"] == 12.5
        assert row["pieces"] == 2
        assert row["per_piece"] == 10.0

    async def test_the_report_reads_arrived_and_available_from_the_lot(
            self, db, cut_history):
        row = (await MaterialService(db).leather_by_style())[0]
        assert row["arrived"] == 400.0
        assert row["on_hand"] == 400.0 and row["available"] == 400.0

    async def test_a_reservation_shows_against_the_style(self, db, cut_history):
        db.add(MaterialReservation(material_lot_id=cut_history["lot_id"],
                                   qty=Decimal("25"), status="active"))
        await db.commit()
        row = (await MaterialService(db).leather_by_style())[0]
        assert row["reserved"] == 25.0 and row["available"] == 375.0

    async def test_the_paged_form_windows_the_rows_before_the_lot_lookups(
            self, db, cut_history):
        from app.core.pagination import PageParams
        rows, total = await MaterialService(db).page_leather_by_style(
            PageParams(limit=50, offset=0))
        assert total == 1 and len(rows) == 1

    async def test_a_style_cut_from_a_lot_that_no_longer_matches_reports_zeroes(
            self, db, cut_history):
        """The report joins on (article, colour), so a lot deactivated by hand
        drops out of `find_lots` and the arrived/on-hand columns go to zero
        while the consumption history stays. Deactivated directly rather than
        through retire_lot — see the OPEN DEFECT note above TestRetireLot."""
        lot = await db.get(MaterialLot, cut_history["lot_id"])
        lot.is_active = False
        await db.commit()
        row = (await MaterialService(db).leather_by_style())[0]
        assert row["arrived"] == 0.0 and row["on_hand"] == 0.0
        assert row["consumed"] == 20.0

    async def test_piece_consumption_lists_every_event_for_one_garment(
            self, db, cut_history, pieces):
        out = await MaterialService(db).piece_consumption(pieces[1][0].id)
        assert out["total"] == 7.5
        assert out["rework"] == 7.5
        assert out["original"] == 0.0
        assert out["events"][0]["stage"] == "LEATHER_CUTTING"
        assert out["events"][0]["article"] == "SUEDE-A32"

    async def test_a_garment_cut_the_old_way_lists_no_hides(
            self, db, cut_history, pieces):
        """The honest answer: nobody recorded which hides those were."""
        out = await MaterialService(db).piece_consumption(pieces[0][0].id)
        assert out["sheets"] == []

    async def test_a_garment_nobody_cut_reports_zero(self, db, pieces):
        out = await MaterialService(db).piece_consumption(pieces[4][0].id)
        assert out["total"] == 0 and out["events"] == []


# ══════════════════════════════════════════════════════════════ the repository
class TestRepositoryEdges:
    async def test_the_batched_reads_short_circuit_on_an_empty_list(self, db):
        repo = MaterialService(db).repo
        assert await repo.received_totals([]) == {}
        assert await repo.reserved_by_lot([]) == {}
        assert await repo.barcodes_by_lot([]) == {}

    async def test_consumption_by_style_narrows_by_article(
            self, db, order_tree, pieces, operations):
        """The report is also asked per-article — "what has SUEDE-A32 cost us
        across every style" — which is a different question from per-style."""
        from app.modules.production.models import ProductionEvent
        lot = await make_lot(db)
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=pieces[0][0].id,
            operation_id=operations["LEATHER_CUTTING"].id,
            work_date=date.today(), leather_lot_id=lot["lot_id"],
            consumption_qty=Decimal("9")))
        await db.commit()
        repo = MaterialService(db).repo
        assert len(await repo.consumption_by_style(article="SUEDE-A32")) == 1
        assert await repo.consumption_by_style(article="NAP-11") == []

    async def test_consumption_stops_once_every_reservation_is_satisfied(self, db):
        """The oldest-first loop breaks as soon as `remaining` reaches zero, so
        a later reservation is left whole rather than partially consumed."""
        from datetime import datetime, timedelta, timezone
        res = await make_lot(db)
        base = datetime.now(timezone.utc)
        for qty, offset in ((10, 0), (20, 60)):
            db.add(MaterialReservation(
                material_lot_id=res["lot_id"], qty=Decimal(qty), status="active",
                created_at=base + timedelta(seconds=offset)))
        await db.commit()

        await MaterialService(db).decrement_for_cut_nocommit(res["lot_id"], 10)
        await db.commit()
        rows = (await db.execute(select(MaterialReservation).order_by(
            MaterialReservation.created_at))).scalars().all()
        assert rows[0].status == "consumed"
        assert rows[1].status == "active" and rows[1].qty == Decimal("20.000")

    async def test_the_repository_owns_the_commit_its_callers_delegate(self, db):
        """`MaterialRepository.commit` exists so a service never reaches past
        the repository for its transaction boundary (CLAUDE.md §15)."""
        res = await make_lot(db)
        lot = await db.get(MaterialLot, res["lot_id"])
        lot.colour = "FOREST"
        await MaterialService(db).repo.commit()
        await db.refresh(lot)
        assert lot.colour == "FOREST"

    async def test_supplier_matching_is_a_token_match_not_a_substring_accident(
            self, db):
        repo = MaterialService(db).repo
        sup = await make_supplier(db, articles="SUEDE-A32, NAP-11")
        assert await repo.supplier_supplies(sup, "nap-11") is True
        assert await repo.supplier_supplies(sup, "SUEDE") is True   # token contains
        assert await repo.supplier_supplies(sup, "BTN-4H") is False
        assert await repo.supplier_supplies(None, "SUEDE-A32") is False

    async def test_an_empty_article_suggests_nobody(self, db):
        await make_supplier(db)
        assert await MaterialService(db).repo.suggest_supplier("") is None

    async def test_rejected_history_sums_a_suppliers_quality_record(self, db):
        sup = await make_supplier(db)
        res = await make_lot(db)
        order = SupplierOrder(category="LEATHER", article="SUEDE-A32",
                              qty=Decimal("100"), uom="dcm", supplier_id=sup.id,
                              status=SupplierOrderStatus.ORDERED.value)
        db.add(order)
        await db.commit()
        await MaterialService(db).receive(schemas.ReceiveRequest(
            lot_id=res["lot_id"], approved_qty=90, rejected_qty=10,
            supplier_order_id=order.id), actor_id=ACTOR)
        assert await MaterialService(db).repo.rejected_history(sup.id) \
            == Decimal("10.000")

    async def test_a_sheet_is_findable_by_its_printed_code(self, db):
        res = await MaterialService(db).create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 43}, sheets=[{"dcm": 43}]))
        repo = MaterialService(db).repo
        code = res["sheets"][0]["code"]
        assert (await repo.get_sheet_by_code(code)).dcm == Decimal("43.000")
        assert (await repo.get_sheet_by_code("NOPE")) is None
        assert (await repo.get_sheet(res["sheets"][0]["sheet_id"])) is not None

    async def test_only_in_stock_hides_are_offerable_to_a_cutting_row(self, db):
        res = await MaterialService(db).create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 90},
            sheets=[{"dcm": 43}, {"dcm": 47}]))
        repo = MaterialService(db).repo
        sheet = await repo.get_sheet(res["sheets"][0]["sheet_id"])
        sheet.status = SheetStatus.CONSUMED.value
        await db.commit()

        assert len(await repo.allocatable_sheets(res["lot_id"])) == 1
        assert await repo.sheet_dcm_in_store(res["lot_id"]) == Decimal("47.000")
        counts = await repo.sheet_counts_by_status(res["lot_id"])
        assert counts[SheetStatus.CONSUMED.value]["count"] == 1

    async def test_sheets_for_a_lot_can_be_narrowed_by_status(self, db):
        res = await MaterialService(db).create_lot(lot_body(
            attributes={"thickness": "1.2mm", "dcm": 90},
            sheets=[{"dcm": 43}, {"dcm": 47}]))
        repo = MaterialService(db).repo
        assert len(await repo.sheets_for_lot(res["lot_id"])) == 2
        assert len(await repo.sheets_for_lot(
            res["lot_id"], statuses=[SheetStatus.ISSUED.value])) == 0

    async def test_a_cutting_row_with_no_hides_returns_an_empty_list(self, db):
        repo = MaterialService(db).repo
        assert await repo.sheets_for_row(uuid.uuid4()) == []
        assert await repo.sheets_for_row_by_piece(uuid.uuid4()) == []

    async def test_last_lot_for_sku_prefers_the_later_work_date(
            self, db, order_tree, pieces, operations):
        """work_date is the business fact; created_at only breaks ties."""
        from app.modules.production.models import ProductionEvent
        old = await make_lot(db)
        new = await make_lot(db, article="NAP-11")
        for lot_id, when in ((new["lot_id"], date.today() - timedelta(days=2)),
                             (old["lot_id"], date.today())):
            db.add(ProductionEvent(
                sku_id=order_tree["sku"].id, piece_id=pieces[0][0].id,
                operation_id=operations["LEATHER_CUTTING"].id,
                work_date=when, leather_lot_id=lot_id,
                consumption_qty=Decimal("1")))
        await db.commit()
        assert await MaterialService(db).repo.last_lot_for_sku(
            order_tree["sku"].id) == old["lot_id"]
