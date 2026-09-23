"""
INTEGRATION · BarcodeService — the one front door every scan resolves through.

WHAT THIS FILE IS ABOUT. `resolve(code)` is called the instant any scanner or
camera produces a string, and it has exactly one job: return the code's TYPE and
a live payload for it, or say honestly that it cannot. The three outcomes are
load-bearing and each has its own section here:

    a known, active code   → type + payload, never a guess
    an unknown code        → 404
    a RETIRED code         → 410 Gone, for EVERY type (F18), so the UI can say
                             "this card was deactivated" rather than "invalid"

Beyond resolve, this covers the screens that hang off the registry: the print
run, the employee-card lifecycle (history is sacred — CLAUDE.md §6), the
material-label reprint list, the order picker and its analytics, and the
filterable history table.
"""
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import (
    BarcodeStatus, BarcodeType, SheetStatus, StoreState,
)
from app.core.pagination import PageParams
from app.modules.barcode.models import (
    BarcodeRegistry, MaterialLot, MaterialSheet,
)
from app.modules.barcode.repository import encode_short
from app.modules.barcode.service import BarcodeService
from app.modules.employees.models import Employee
from app.modules.production.models import ProductionEvent

pytestmark = pytest.mark.integrity

ACTOR = uuid.uuid4()


# ══════════════════════════════════════════════════════════════ helpers
async def register(db, **kw):
    kw.setdefault("status", BarcodeStatus.ACTIVE.value)
    row = BarcodeRegistry(**kw)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def make_lot(db, **kw):
    base = dict(category="LEATHER", article="SUEDE-A32", colour="PINE",
                thickness="1.2mm", uom="dcm", on_hand=Decimal("400"),
                is_active=True)
    base.update(kw)
    lot = MaterialLot(**base)
    db.add(lot)
    await db.flush()
    type_ = {"LEATHER": BarcodeType.LEATHER_LOT, "LINING": BarcodeType.LINING_LOT,
             "ACCESSORY": BarcodeType.ACCESSORY_LOT}[lot.category]
    prefix = {"LEATHER": "LOT-LEA", "LINING": "LOT-LIN",
              "ACCESSORY": "LOT-ACC"}[lot.category]
    code = f"{prefix}-{str(lot.id)[:6].upper()}"
    await register(db, code=code, type=type_.value,
                   material_lot_id=lot.id, caption=lot.article)
    await db.refresh(lot)
    return lot, code


@pytest.fixture
async def order_barcodes(db, order_tree, pieces):
    """The pieces fixture as the ORDER PICKER sees them: registry rows carrying
    order_id / style_id / sku_id, which is what premint writes and what every
    /barcode/orders* read filters on."""
    codes = []
    for i, piece in enumerate(pieces, start=1):
        # The fixture's long code becomes the LEGACY ALIAS, which is what the
        # compact-code switch actually did to it (bug #19): the printed label
        # keeps resolving, but it is no longer the code anything prints or
        # counts. Leaving it primary would make this fixture model a state the
        # importer never produces.
        legacy = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == piece.code))
        legacy.is_alias = True
        code = encode_short(i)
        await register(db, code=code, type=BarcodeType.PIECE.value,
                       piece_id=piece.id, caption=piece.code,
                       order_id=order_tree["order"].id,
                       style_id=order_tree["style"].id,
                       sku_id=order_tree["sku"].id)
        codes.append(code)
    await db.commit()
    return codes


# ══════════════════════════════════════════════════════════════ resolve
class TestResolve:
    async def test_a_piece_code_returns_the_whole_garment_card(self, db, pieces):
        piece = pieces[0]
        out = await BarcodeService(db).resolve(piece.code)

        assert out["type"] == BarcodeType.PIECE.value
        assert out["active"] is True
        assert out["is_alias"] is False
        card = out["piece"]
        assert card["piece_id"] == str(piece.id)
        assert card["style_name"] == "CLERMONT"
        assert card["article"] == "CL1"
        assert card["colour"] == "PINE GREEN"
        assert card["size"] == "M"
        assert card["seq"] == 1
        assert card["serial"] == "001"
        assert card["order_number"] == "JP-PO"
        assert card["client"] == "John Peter"
        assert card["needs_lining"] is True

    async def test_the_piece_payload_pre_joins_the_sticker_text(self, db, pieces):
        out = await BarcodeService(db).resolve(pieces[0].code)
        assert out["piece"]["label_line"] == \
            "JP-PO · CLERMONT · CL1 · PINE GREEN · M · 001"

    async def test_the_store_travels_with_every_payload_that_names_a_piece(
            self, db, pieces):
        """BUG #12 — so an operator on any stage can see where the garment
        stands without opening the Store hub.

        This used to carry a DRAWER: an id and the code of the box to walk to,
        and null for any garment the 200-slot pool had no room for. The garment
        carries its own standing, so every piece has an answer.
        """
        piece = pieces[0]
        out = await BarcodeService(db).resolve(piece.code)
        store = out["piece"]["store"]
        assert store["state"] == piece.store_state
        assert store["leather_in"] is False
        assert store["holding"] is not None
        assert out["piece"]["store_state"] == piece.store_state

    async def test_a_piece_minted_before_the_compact_switch_has_no_short_code(
            self, db, pieces):
        """null means 'not backfilled yet' — the long code still scans, so this
        is a gap to fill rather than a failure."""
        assert (await BarcodeService(db).resolve(
            pieces[0].code))["piece"]["short_code"] is None

    async def test_a_backfilled_piece_reports_its_compact_code(
            self, db, pieces, order_barcodes):
        out = await BarcodeService(db).resolve(pieces[0].code)
        assert out["piece"]["short_code"] == order_barcodes[0]

    async def test_the_cut_consumption_is_carried_on_the_card(
            self, db, pieces, order_tree, operations):
        piece = pieces[0]
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=piece.id,
            operation_id=operations["LEATHER_CUTTING"].id,
            work_date=date.today(), consumption_qty=Decimal("12.5")))
        await db.commit()
        out = await BarcodeService(db).resolve(piece.code)
        assert out["piece"]["leather_consumption_dcm"] == 12.5

    async def test_an_employee_card_returns_the_worker(self, db, cutter):
        emp, bc = cutter
        out = await BarcodeService(db).resolve(bc.code)
        assert out["type"] == BarcodeType.EMPLOYEE.value
        assert out["employee"]["name"] == "RAMESH"
        assert out["employee"]["designation"] == "CUTTER"
        assert out["employee"]["is_active"] is True
        assert out["next_expected_scan"] == "PIECE"

    # THE DRAWER CODE IS GONE. There used to be a test here that resolved a
    # DRW- label to a box's state and holding label. No drawer barcode is minted
    # any more, so there is nothing to scan and nothing to assert — what the
    # label was read FOR is on the piece payload, above.

    async def test_a_lot_label_returns_its_stock(self, db):
        lot, code = await make_lot(db)
        out = await BarcodeService(db).resolve(code)
        assert out["type"] == BarcodeType.LEATHER_LOT.value
        assert out["lot"]["on_hand"] == 400.0
        assert out["lot"]["available"] == 400.0
        assert out["lot"]["uom"] == "dcm"
        assert out["next_expected_scan"] == "PIECE"

    async def test_a_hide_answers_about_the_hide_and_its_lot(self, db):
        """A LEATHER_SHEET row carries material_lot_id too. Answering "552 dcm
        of SUEDE-A32" is true of the LOT and useless about the skin in the
        operator's hand — so BOTH come back, the hide first."""
        lot, _ = await make_lot(db)
        sheet = MaterialSheet(code="LS-000001", material_lot_id=lot.id,
                              dcm=Decimal("43"), status=SheetStatus.IN_STOCK.value)
        db.add(sheet)
        await db.flush()
        await register(db, code=sheet.code, type=BarcodeType.LEATHER_SHEET.value,
                       material_sheet_id=sheet.id, material_lot_id=lot.id)

        out = await BarcodeService(db).resolve("LS-000001")
        assert out["sheet"]["dcm"] == 43.0
        assert out["sheet"]["status"] == SheetStatus.IN_STOCK.value
        assert out["sheet"]["cutting_row_id"] is None
        assert out["lot"]["lot_id"] == str(lot.id)
        assert out["next_expected_scan"] == "PIECE"

    async def test_an_unknown_code_is_a_404_that_echoes_what_was_scanned(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve("NOT-A-CODE")
        assert e.value.status_code == 404 and "NOT-A-CODE" in e.value.detail

    async def test_resolution_is_case_and_whitespace_insensitive(self, db, pieces):
        code = pieces[0].code
        out = await BarcodeService(db).resolve(f"  {code.lower()} ")
        assert out["code"] == code

    @pytest.mark.parametrize("type_,field", [
        (BarcodeType.PIECE, "piece_id"),
        (BarcodeType.EMPLOYEE, "employee_id"),
        (BarcodeType.LEATHER_SHEET, "material_sheet_id"),
    ])
    async def test_a_retired_code_of_any_type_is_410_not_404(
            self, db, pieces, cutter, type_, field):
        """F18: retirement is a lifecycle state on the REGISTRY, not an
        employee-only concept. 'never existed' and 'was deactivated' are
        different facts and the UI must be able to tell them apart.

        DRAWER was the third case here and is gone with the drawer barcode. The
        rule it was standing in for — "retirement applies to EVERY type, not just
        EMPLOYEE" — needs a third type to be worth parametrising, so the hide
        takes its place: it is the newest label in the registry and the one most
        likely to be retired next.
        """
        piece = pieces[0]
        target = {"piece_id": piece.id, "employee_id": cutter[0].id,
                  "material_sheet_id": uuid.uuid4()}[field]
        await register(db, code=f"RETIRED-{type_.value}", type=type_.value,
                       status=BarcodeStatus.RETIRED.value, **{field: target})
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve(f"RETIRED-{type_.value}")
        assert e.value.status_code == 410
        assert "history" in e.value.detail

    async def test_a_registry_row_pointing_at_nothing_still_answers(self, db):
        """A dangling row must degrade to the code + type, never a 500."""
        await register(db, code="ORPHAN-PIECE", type=BarcodeType.PIECE.value,
                       piece_id=uuid.uuid4())
        out = await BarcodeService(db).resolve("ORPHAN-PIECE")
        assert out["type"] == BarcodeType.PIECE.value
        assert out["piece"]["piece_id"] is not None
        assert "code" not in out["piece"]

    async def test_a_dangling_employee_and_lot_row_degrade_gracefully(self, db):
        await register(db, code="ORPHAN-EMP", type=BarcodeType.EMPLOYEE.value,
                       employee_id=uuid.uuid4())
        await register(db, code="ORPHAN-LOT", type=BarcodeType.LEATHER_LOT.value,
                       material_lot_id=uuid.uuid4())
        svc = BarcodeService(db)
        assert (await svc.resolve("ORPHAN-EMP"))["employee"].keys() == {"employee_id"}
        assert (await svc.resolve("ORPHAN-LOT"))["lot"].keys() == {"lot_id"}

    async def test_a_legacy_drawer_row_answers_with_its_code_and_nothing_else(
            self, db):
        """THE HISTORICAL ROWS ARE STILL THERE, and a scan of one must not 500.

        The drawer tables are kept for audit, so DRW- registry rows survive in
        databases that had them. Nothing resolves a drawer any more, so the
        payload has no `drawer` block — the scan degrades to "known code, this
        type", which is the same graceful floor every dangling row gets.
        """
        await register(db, code="LEGACY-DRW", type=BarcodeType.DRAWER.value)
        out = await BarcodeService(db).resolve("LEGACY-DRW")
        assert out["type"] == BarcodeType.DRAWER.value
        assert "drawer" not in out
        assert out["next_expected_scan"] is None

    async def test_a_sheet_row_pointing_at_no_hide_returns_an_empty_block(self, db):
        await register(db, code="ORPHAN-SHEET",
                       type=BarcodeType.LEATHER_SHEET.value,
                       material_sheet_id=uuid.uuid4())
        out = await BarcodeService(db).resolve("ORPHAN-SHEET")
        assert out["sheet"] == {}

    async def test_a_legacy_long_code_reports_itself_as_an_alias(self, db, pieces):
        """The scan works; the UI can nudge the operator to reprint the label
        with the small code."""
        piece = pieces[0]
        await register(db, code="OLD-LONG-CODE-001", type=BarcodeType.PIECE.value,
                       piece_id=piece.id, is_alias=True)
        assert (await BarcodeService(db).resolve("OLD-LONG-CODE-001"))["is_alias"]


# ══════════════════════════════════════════════ what to do next
class TestNextStep:
    async def test_a_freshly_minted_piece_is_due_at_its_cut_screen(
            self, db, pieces):
        """A piece with no cut event yet is reached from a cut SCREEN, never by
        pipeline inference — say so rather than implying a pipeline scan."""
        out = await BarcodeService(db).resolve(pieces[0].code)
        assert out["next_stage"] == "LEATHER_CUTTING"
        assert out["next_stage_label"] == "Leather Cutting"
        assert "cut screen" in out["next_stage_blocked_reason"]

    async def test_line_stitching_is_blocked_until_the_store_has_released_it(
            self, db, pieces, order_tree, operations):
        """The SOFT half of the merge gate. It has to agree with the hard gate
        in ProductionService._merge_ok, or the screen promises a stage the log
        then refuses."""
        piece = pieces[0]
        for code in ("LEATHER_CUTTING", "FUSING", "PASTING", "LINING_CUTTING"):
            db.add(ProductionEvent(
                sku_id=order_tree["sku"].id, piece_id=piece.id,
                operation_id=operations[code].id, work_date=date.today()))
        await db.commit()

        out = await BarcodeService(db).resolve(piece.code)
        assert out["next_stage"] == "LINE_STITCHING"
        assert "store" in out["next_stage_blocked_reason"]

    async def test_a_released_garment_is_no_longer_blocked(
            self, db, pieces, order_tree, operations):
        piece = pieces[0]
        for code in ("LEATHER_CUTTING", "FUSING", "PASTING", "LINING_CUTTING"):
            db.add(ProductionEvent(
                sku_id=order_tree["sku"].id, piece_id=piece.id,
                operation_id=operations[code].id, work_date=date.today()))
        piece.store_state = StoreState.SENDED.value
        await db.commit()

        out = await BarcodeService(db).resolve(piece.code)
        assert out["next_stage"] == "LINE_STITCHING"
        assert out["next_stage_blocked_reason"] is None

    async def test_a_finished_garment_says_so_rather_than_returning_nothing(
            self, db, pieces, order_tree, operations):
        piece = pieces[0]
        for code in operations:
            db.add(ProductionEvent(
                sku_id=order_tree["sku"].id, piece_id=piece.id,
                operation_id=operations[code].id, work_date=date.today()))
        await db.commit()
        out = await BarcodeService(db).resolve(piece.code)
        assert out["next_stage"] is None
        assert out["next_stage_label"] == "Finished — nothing left to log"

    async def test_a_non_piece_code_carries_the_three_keys_as_nulls(self, db, cutter):
        out = await BarcodeService(db).resolve(cutter[1].code)
        assert out["next_stage"] is None
        assert out["next_stage_label"] is None
        assert out["next_stage_blocked_reason"] is None


# ══════════════════════════════════════════════ the narrowed resolvers
class TestNarrowResolvers:
    async def test_each_resolver_returns_its_own_id(self, db, pieces, cutter):
        piece = pieces[0]
        lot, lot_code = await make_lot(db)
        svc = BarcodeService(db)
        assert await svc.resolve_piece_id(piece.code) == piece.id
        assert await svc.resolve_employee_id(cutter[1].code) == cutter[0].id
        assert await svc.resolve_lot_id(lot_code) == lot.id

    @pytest.mark.parametrize("method,message", [
        ("resolve_piece_id", "not a known piece barcode"),
        ("resolve_employee_id", "not a known employee barcode"),
        ("resolve_lot_id", "not a known material-lot barcode"),
    ])
    async def test_a_code_of_the_wrong_type_is_a_404_naming_the_type_wanted(
            self, db, cutter, method, message):
        svc = BarcodeService(db)
        code = cutter[1].code if method != "resolve_employee_id" else "NOPE"
        if method == "resolve_employee_id":
            await register(db, code="NOPE", type=BarcodeType.LEATHER_LOT.value,
                           material_lot_id=uuid.uuid4())
        with pytest.raises(HTTPException) as e:
            await getattr(svc, method)(code)
        assert e.value.status_code == 404 and message in e.value.detail

    async def test_every_narrow_resolver_honours_the_410(self, db, pieces):
        piece = pieces[0]
        await register(db, code="DEAD-PIECE", type=BarcodeType.PIECE.value,
                       piece_id=piece.id, status=BarcodeStatus.RETIRED.value)
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve_piece_id("DEAD-PIECE")
        assert e.value.status_code == 410


class TestResolveActor:
    async def test_a_scanned_card_alone_identifies_the_worker(self, db, cutter):
        assert await BarcodeService(db).resolve_actor(
            employee_barcode=cutter[1].code, employee_id=None) == cutter[0].id

    async def test_an_id_alone_identifies_the_worker(self, db, cutter):
        assert await BarcodeService(db).resolve_actor(
            employee_barcode=None, employee_id=cutter[0].id) == cutter[0].id

    async def test_both_doors_agreeing_is_accepted(self, db, cutter):
        assert await BarcodeService(db).resolve_actor(
            employee_barcode=cutter[1].code,
            employee_id=cutter[0].id) == cutter[0].id

    async def test_two_different_people_in_one_request_is_refused_by_name(
            self, db, cutter, tailor):
        """Preferring one and discarding the other silently is how a scan of one
        card gets logged against somebody else."""
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve_actor(
                employee_barcode=cutter[1].code, employee_id=tailor[0].id)
        assert e.value.status_code == 422
        assert "RAMESH" in e.value.detail and "TARA" in e.value.detail

    async def test_a_conflict_against_an_unknown_id_still_names_the_scanned_card(
            self, db, cutter):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve_actor(
                employee_barcode=cutter[1].code, employee_id=uuid.uuid4())
        assert "RAMESH" in e.value.detail
        assert "an unknown record" in e.value.detail

    async def test_neither_door_is_a_422(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve_actor(
                employee_barcode=None, employee_id=None)
        assert e.value.status_code == 422
        assert "Provide employee_barcode or employee_id" in e.value.detail

    async def test_an_id_from_the_wrong_table_is_diagnosed_not_just_refused(
            self, db, cutter):
        """The most common way to get this wrong is passing a barcode ROW's id
        where an employee.id belongs — the two look identical."""
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve_actor(
                employee_barcode=None, employee_id=cutter[1].id)
        assert e.value.status_code == 404
        assert "the id of a barcode row is a different value" in e.value.detail


# ══════════════════════════════════════════════ the employee card lifecycle
class TestEmployeeCardLifecycle:
    async def test_a_new_card_is_minted_and_returned_for_printing(self, db):
        emp = Employee(name="NEWBIE", designation="CUTTER", is_active=True)
        db.add(emp)
        await db.commit()
        code = await BarcodeService(db).issue_employee_barcode_nocommit(
            emp.id, "NEWBIE")
        await db.commit()
        assert code.startswith("EMP-")
        assert await BarcodeService(db).resolve_employee_id(code) == emp.id

    async def test_a_reissue_retires_the_old_card_and_mints_a_new_one(
            self, db, cutter):
        """Lost/damaged card. History untouched (CLAUDE.md §6)."""
        emp, old = cutter
        out = await BarcodeService(db).reissue_employee_barcode(
            emp.id, actor_id=ACTOR)

        assert out["active"] is True and out["history_preserved"] is True
        assert out["employee_barcode"] != old.code

        await db.refresh(old)
        assert old.status == BarcodeStatus.RETIRED.value
        assert old.retired_reason == "reissued"
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve(old.code)
        assert e.value.status_code == 410

    async def test_the_new_card_keeps_the_old_caption(self, db, cutter):
        emp, old = cutter
        out = await BarcodeService(db).reissue_employee_barcode(emp.id, None)
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == out["employee_barcode"]))
        assert row.caption == old.caption

    async def test_a_reissue_for_a_worker_with_no_card_simply_mints_one(self, db):
        emp = Employee(name="CARDLESS", designation="CUTTER", is_active=True)
        db.add(emp)
        await db.commit()
        out = await BarcodeService(db).reissue_employee_barcode(emp.id, None)
        assert out["employee_barcode"].startswith("EMP-")

    async def test_deactivation_retires_the_code_and_keeps_the_person(
            self, db, cutter, order_tree, pieces, operations):
        """You delete the scannable code, never the person or their record."""
        emp, bc = cutter
        db.add(ProductionEvent(
            sku_id=order_tree["sku"].id, piece_id=pieces[0].id,
            operation_id=operations["LEATHER_CUTTING"].id,
            employee_id=emp.id, work_date=date.today()))
        await db.commit()

        out = await BarcodeService(db).deactivate_employee_barcode(
            emp.id, actor_id=ACTOR)
        assert out["active"] is False and out["history_preserved"] is True

        await db.refresh(bc)
        assert bc.status == BarcodeStatus.RETIRED.value
        assert bc.retired_reason == "left_company"
        assert await db.get(Employee, emp.id) is not None
        events = (await db.execute(select(ProductionEvent).where(
            ProductionEvent.employee_id == emp.id))).scalars().all()
        assert len(events) == 1

    async def test_deactivating_a_worker_with_no_active_card_is_a_404(
            self, db, cutter):
        emp, _ = cutter
        await BarcodeService(db).deactivate_employee_barcode(emp.id, None)
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).deactivate_employee_barcode(emp.id, None)
        assert e.value.status_code == 404
        assert "No active barcode" in e.value.detail

    @pytest.mark.parametrize("action,expected", [
        ("reissue", "EMPLOYEE_BARCODE_REISSUE"),
        ("deactivate", "EMPLOYEE_BARCODE_DEACTIVATE"),
    ])
    async def test_both_transitions_are_audited_in_the_same_transaction(
            self, db, cutter, action, expected):
        from app.core.models import AuditLog
        emp, _ = cutter
        svc = BarcodeService(db)
        if action == "reissue":
            await svc.reissue_employee_barcode(emp.id, actor_id=ACTOR)
        else:
            await svc.deactivate_employee_barcode(emp.id, actor_id=ACTOR)
        row = await db.scalar(select(AuditLog).where(AuditLog.action == expected))
        assert row is not None and row.actor_user_id == ACTOR

    async def test_the_roster_reads_every_card_in_one_call(self, db, cutter, tailor):
        codes = await BarcodeService(db).employee_codes(
            [cutter[0].id, tailor[0].id])
        assert codes[cutter[0].id] == cutter[1].code
        assert codes[tailor[0].id] == tailor[1].code

    async def test_a_retired_card_is_hidden_from_the_roster_unless_asked_for(
            self, db, cutter):
        emp, bc = cutter
        await BarcodeService(db).deactivate_employee_barcode(emp.id, None)
        assert await BarcodeService(db).employee_codes([emp.id]) == {}
        assert await BarcodeService(db).employee_codes(
            [emp.id], active_only=False) == {emp.id: bc.code}

    async def test_an_empty_roster_costs_no_query(self, db):
        assert await BarcodeService(db).employee_codes([]) == {}


# ══════════════════════════════════════════════════════════════ the print run
class TestPrintPayload:
    async def test_a_label_carries_the_code_the_caption_and_the_typeset_line(
            self, db, pieces):
        piece = pieces[0]
        out = await BarcodeService(db).print_payload(codes=[piece.code])
        label = out["labels"][0]
        assert label["symbology"] == "code128"
        assert label["known"] is True
        assert label["details"]["serial"] == "001"
        assert label["details"]["article"] == "CL1"
        assert label["label_line"] == \
            "JP-PO · CLERMONT · CL1 · PINE GREEN · M · 001"

    async def test_an_unknown_code_is_returned_as_unknown_rather_than_dropped(
            self, db):
        out = await BarcodeService(db).print_payload(codes=["ghost"])
        label = out["labels"][0]
        assert label["known"] is False
        assert label["code"] == "GHOST"
        assert label["caption"] == "ghost"
        assert label["details"] is None and label["label_line"] is None

    async def test_a_non_piece_label_has_no_garment_line_to_print(self, db, cutter):
        out = await BarcodeService(db).print_payload(codes=[cutter[1].code])
        assert out["labels"][0]["known"] is True
        assert out["labels"][0]["details"] is None

    async def test_printing_a_whole_sku_expands_to_its_pieces(
            self, db, order_tree, order_barcodes):
        out = await BarcodeService(db).print_payload(sku_id=order_tree["sku"].id)
        assert len(out["labels"]) == 5
        assert {l["code"] for l in out["labels"]} == set(order_barcodes)

    async def test_printing_a_whole_order_expands_to_its_pieces(
            self, db, order_tree, order_barcodes):
        out = await BarcodeService(db).print_payload(
            order_id=order_tree["order"].id)
        assert len(out["labels"]) == 5

    async def test_the_expansion_prints_the_compact_code_not_the_long_one(
            self, db, order_tree, order_barcodes, pieces):
        """Selecting Piece.code here would print a sheet of exactly the labels
        the compact-code change set out to shrink."""
        out = await BarcodeService(db).print_payload(sku_id=order_tree["sku"].id)
        assert pieces[0].code not in {l["code"] for l in out["labels"]}

    async def test_a_retired_label_is_not_part_of_a_reprint_run(
            self, db, order_tree, order_barcodes):
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == order_barcodes[0]))
        row.status = BarcodeStatus.RETIRED.value
        await db.commit()
        out = await BarcodeService(db).print_payload(sku_id=order_tree["sku"].id)
        assert len(out["labels"]) == 4

    async def test_an_empty_request_returns_an_empty_sheet(self, db):
        assert await BarcodeService(db).print_payload(codes=[]) == {"labels": []}


# ══════════════════════════════════════════════ the material-label screen
class TestLotBarcodeScreen:
    async def test_every_lot_label_is_listed_for_reprinting(self, db):
        await make_lot(db)
        rows = await BarcodeService(db).list_lot_barcodes()
        assert len(rows) == 1
        assert rows[0]["category"] == "LEATHER"
        assert rows[0]["label_line"].startswith("SUEDE-A32 · PINE")

    async def test_the_list_narrows_by_category(self, db):
        await make_lot(db)
        await make_lot(db, category="LINING", subtype="RIBS", article="RIB-1",
                       uom="kg")
        svc = BarcodeService(db)
        # The filter is on the LOT's category, and it upper-cases what it is
        # given — the screen's category box sends whatever the user picked.
        assert len(await svc.list_lot_barcodes(category="leather")) == 1
        assert len(await svc.list_lot_barcodes(category="LINING")) == 1
        assert len(await svc.list_lot_barcodes(category="ACCESSORY")) == 0

    async def test_retired_labels_are_hidden_unless_asked_for(self, db):
        lot, code = await make_lot(db)
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == code))
        row.status = BarcodeStatus.RETIRED.value
        await db.commit()
        svc = BarcodeService(db)
        assert await svc.list_lot_barcodes() == []
        greyed = await svc.list_lot_barcodes(active_only=False)
        assert greyed[0]["status"] == BarcodeStatus.RETIRED.value

    async def test_the_paged_form_describes_the_same_rows_as_the_list(self, db):
        await make_lot(db)
        await make_lot(db, article="NAP-11")
        rows, total = await BarcodeService(db).page_lot_barcodes(
            PageParams(limit=1, offset=0))
        assert total == 2 and len(rows) == 1

    async def test_a_lot_barcode_may_be_retired_and_then_resolves_410(self, db):
        lot, code = await make_lot(db)
        assert await BarcodeService(db).retire_lot_code_nocommit(lot.id) is True
        await db.commit()
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve(code)
        assert e.value.status_code == 410

    async def test_retiring_a_lot_with_no_label_reports_false(self, db):
        assert await BarcodeService(db).retire_lot_code_nocommit(
            uuid.uuid4()) is False


# ══════════════════════════════════════════════ the order picker + analytics
class TestOrderScreens:
    async def test_the_picker_lists_orders_that_have_minted_barcodes(
            self, db, order_tree, order_barcodes):
        rows = await BarcodeService(db).list_orders()
        assert len(rows) == 1
        assert rows[0]["order_number"] == "JP-PO"
        assert rows[0]["client_name"] == "John Peter"
        assert rows[0]["minted"] == 5
        assert rows[0]["first_generated_at"] is not None

    async def test_an_order_with_no_barcodes_is_not_in_the_picker(
            self, db, order_tree):
        assert await BarcodeService(db).list_orders() == []

    async def test_the_paged_picker_reports_the_number_of_orders_not_labels(
            self, db, order_tree, order_barcodes):
        """GROUP BY means the count is over the GROUPS — five labels are one
        order, not five rows."""
        rows, total = await BarcodeService(db).page_orders(
            PageParams(limit=10, offset=0))
        assert total == 1 and rows[0]["minted"] == 5

    async def test_analytics_reconciles_planned_against_generated(
            self, db, order_tree, order_barcodes):
        out = await BarcodeService(db).order_analytics(order_tree["order"].id)
        total = out["order_total"]
        assert total["planned"] == 5
        assert total["generated"] == 5
        assert total["balance"] == 0
        assert total["active"] == 5 and total["retired"] == 0
        assert total["duplicates"] == 0
        assert total["fully_generated"] is True
        assert total["half_minted"] is False

    async def test_a_half_minted_order_is_flagged_rather_than_silently_short(
            self, db, order_tree, order_barcodes):
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == order_barcodes[0]))
        await db.delete(row)
        await db.commit()
        total = (await BarcodeService(db).order_analytics(
            order_tree["order"].id))["order_total"]
        assert total["balance"] == 1 and total["half_minted"] is True
        assert total["fully_generated"] is False

    async def test_a_retired_label_is_counted_as_minted_but_not_active(
            self, db, order_tree, order_barcodes):
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == order_barcodes[0]))
        row.status = BarcodeStatus.RETIRED.value
        await db.commit()
        total = (await BarcodeService(db).order_analytics(
            order_tree["order"].id))["order_total"]
        assert total["generated"] == 5 and total["active"] == 4
        assert total["retired"] == 1

    async def test_an_alias_never_double_counts_a_garment(
            self, db, order_tree, order_barcodes, pieces):
        """Since bug #19 a piece can hold two registry rows. Counting both would
        drive `balance` negative and make the duplicates=0 proof read broken."""
        await register(db, code="LEGACY-LONG-1", type=BarcodeType.PIECE.value,
                       piece_id=pieces[0].id, is_alias=True,
                       order_id=order_tree["order"].id,
                       style_id=order_tree["style"].id,
                       sku_id=order_tree["sku"].id)
        total = (await BarcodeService(db).order_analytics(
            order_tree["order"].id))["order_total"]
        assert total["generated"] == 5 and total["duplicates"] == 0

    async def test_the_per_style_breakdown_names_every_style(
            self, db, order_tree, order_barcodes):
        out = await BarcodeService(db).order_analytics(order_tree["order"].id)
        assert out["by_style"][0]["style_name"] == "CLERMONT"
        assert out["by_style"][0]["planned"] == 5
        assert out["by_style"][0]["minted"] == 5
        assert out["by_style"][0]["balance"] == 0

    async def test_analytics_for_an_unknown_order_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).order_analytics(uuid.uuid4())
        assert e.value.status_code == 404

    async def test_the_sku_options_fill_the_filter_dropdowns(
            self, db, order_tree, order_barcodes):
        rows = await BarcodeService(db).list_order_skus(order_tree["order"].id)
        assert rows[0]["sku_code"] == "JP-CLERMONT-PINE-M"
        assert rows[0]["colour"] == "PINE GREEN" and rows[0]["size"] == "M"
        assert rows[0]["style_name"] == "CLERMONT"

    async def test_sku_options_for_an_unknown_order_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).list_order_skus(uuid.uuid4())
        assert e.value.status_code == 404

    async def test_an_order_number_resolves_to_its_id(self, db, order_tree):
        svc = BarcodeService(db)
        assert await svc.resolve_order_id("JP-PO") == order_tree["order"].id
        assert await svc.resolve_order_id(str(order_tree["order"].id)) \
            == order_tree["order"].id
        assert await svc.resolve_order_id(order_tree["order"].id) \
            == order_tree["order"].id

    async def test_an_unknown_order_number_is_a_404_that_echoes_it(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve_order_id("NO-SUCH-PO")
        assert e.value.status_code == 404 and "NO-SUCH-PO" in e.value.detail

    async def test_a_well_formed_uuid_for_a_missing_order_is_still_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).resolve_order_id(str(uuid.uuid4()))
        assert e.value.status_code == 404


# ══════════════════════════════════════════════ the filterable history table
class TestHistory:
    async def test_the_history_lists_one_row_per_garment(
            self, db, order_tree, order_barcodes):
        out = await BarcodeService(db).list_history(order_tree["order"].id)
        assert out["total"] == 5 and out["pages"] == 1
        row = out["items"][0]
        assert row["style_name"] == "CLERMONT"
        assert row["article"] == "CL1"
        assert row["colour"] == "PINE GREEN"
        assert row["serial"] is not None
        assert row["piece_code"].startswith("JP-CLERMONT-PINE-M-")

    async def test_the_current_stage_travels_with_the_row(
            self, db, order_tree, order_barcodes, pieces, operations):
        piece = pieces[0]
        piece.current_operation_id = operations["FUSING"].id
        await db.commit()
        out = await BarcodeService(db).list_history(order_tree["order"].id)
        assert "FUSING" in {r["current_stage"] for r in out["items"]}

    @pytest.mark.parametrize("key", ["sku_id", "style_id"])
    async def test_it_narrows_by_sku_and_by_style(
            self, db, order_tree, order_barcodes, key):
        target = {"sku_id": order_tree["sku"].id,
                  "style_id": order_tree["style"].id}[key]
        out = await BarcodeService(db).list_history(
            order_tree["order"].id, **{key: target})
        assert out["total"] == 5
        miss = await BarcodeService(db).list_history(
            order_tree["order"].id, **{key: uuid.uuid4()})
        assert miss["total"] == 0

    async def test_it_narrows_by_garment_size(
            self, db, order_tree, order_barcodes):
        svc = BarcodeService(db)
        assert (await svc.list_history(
            order_tree["order"].id, size=" m "))["total"] == 5
        assert (await svc.list_history(
            order_tree["order"].id, size="XXL"))["total"] == 0

    async def test_it_narrows_by_status(self, db, order_tree, order_barcodes):
        row = await db.scalar(select(BarcodeRegistry).where(
            BarcodeRegistry.code == order_barcodes[0]))
        row.status = BarcodeStatus.RETIRED.value
        await db.commit()
        svc = BarcodeService(db)
        assert (await svc.list_history(
            order_tree["order"].id, status_filter="active"))["total"] == 4
        assert (await svc.list_history(
            order_tree["order"].id, status_filter="retired"))["total"] == 1

    async def test_it_narrows_by_generated_date_range(
            self, db, order_tree, order_barcodes):
        svc = BarcodeService(db)
        now = datetime.now(timezone.utc)
        assert (await svc.list_history(
            order_tree["order"].id,
            date_from=now - timedelta(days=1)))["total"] == 5
        assert (await svc.list_history(
            order_tree["order"].id,
            date_to=now - timedelta(days=1)))["total"] == 0

    async def test_the_page_size_is_capped_so_a_big_order_cannot_be_dumped(
            self, db, order_tree, order_barcodes):
        out = await BarcodeService(db).list_history(
            order_tree["order"].id, page_size=5000)
        assert out["page_size"] == 200

    @pytest.mark.parametrize("page,size,expect", [(1, 2, 2), (3, 2, 1), (9, 2, 0)])
    async def test_paging_walks_the_whole_order(
            self, db, order_tree, order_barcodes, page, size, expect):
        out = await BarcodeService(db).list_history(
            order_tree["order"].id, page=page, page_size=size)
        assert len(out["items"]) == expect
        assert out["pages"] == 3 and out["total"] == 5

    async def test_a_page_below_one_is_clamped(self, db, order_tree, order_barcodes):
        out = await BarcodeService(db).list_history(order_tree["order"].id, page=0)
        assert out["page"] == 1

    async def test_history_for_an_unknown_order_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).list_history(uuid.uuid4())
        assert e.value.status_code == 404


class TestBarcodeDetail:
    async def test_the_click_through_returns_the_same_payload_as_a_scan(
            self, db, pieces):
        code = pieces[0].code
        svc = BarcodeService(db)
        assert await svc.barcode_detail(code) == await svc.resolve(code)

    async def test_detail_for_an_unknown_code_is_a_404(self, db):
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).barcode_detail("NOPE")
        assert e.value.status_code == 404

    async def test_detail_for_a_retired_code_is_a_410(self, db, cutter):
        emp, bc = cutter
        await BarcodeService(db).deactivate_employee_barcode(emp.id, None)
        with pytest.raises(HTTPException) as e:
            await BarcodeService(db).barcode_detail(bc.code)
        assert e.value.status_code == 410


# ══════════════════════════════════════════════════════════ the code minter
class TestCodeMinting:
    async def test_codes_count_up_within_a_prefix(self, db):
        repo = BarcodeService(db).repo
        emp = Employee(name="A", designation="CUTTER", is_active=True)
        db.add(emp)
        await db.flush()
        first = await repo.mint_employee_code_nocommit(emp.id, None)
        await db.commit()
        second = await repo.mint_employee_code_nocommit(emp.id, None)
        await db.commit()
        assert int(second.code.split("-")[1]) == int(first.code.split("-")[1]) + 1

    async def test_a_non_numeric_code_in_the_namespace_does_not_jam_the_counter(
            self, db):
        """F16/F79/F99. One fixture-style row (EMP-<hex>) used to pin the
        counter at 0 permanently: every subsequent mint returned EMP-000001 and
        died on the unique index — an unhandled 500 on every employee create."""
        emp = Employee(name="B", designation="CUTTER", is_active=True)
        db.add(emp)
        await db.flush()
        await register(db, code="EMP-ZZZZZZ", type=BarcodeType.EMPLOYEE.value,
                       employee_id=emp.id)
        await register(db, code="EMP-000007", type=BarcodeType.EMPLOYEE.value,
                       employee_id=emp.id)

        row = await BarcodeService(db).repo.mint_employee_code_nocommit(emp.id, None)
        await db.commit()
        assert row.code == "EMP-000008"

    async def test_an_entirely_non_numeric_namespace_starts_from_one(self, db):
        emp = Employee(name="C", designation="CUTTER", is_active=True)
        db.add(emp)
        await db.flush()
        await register(db, code="EMP-ZZZZZZ", type=BarcodeType.EMPLOYEE.value,
                       employee_id=emp.id)
        row = await BarcodeService(db).repo.mint_employee_code_nocommit(emp.id, None)
        await db.commit()
        assert row.code == "EMP-000001"

    # `mint_drawer_code_nocommit` IS GONE, and with it the test that DRW-0014
    # was derived from seq 14. Nothing mints a drawer label: the store is a state
    # on the garment, and a state has no barcode.

    async def test_a_single_piece_short_code_continues_the_import_sequence(
            self, db, pieces, order_barcodes):
        repo = BarcodeService(db).repo
        assert await repo.max_short_code_counter() == 5
        row = await repo.mint_piece_short_code_nocommit(pieces[0].id)
        await db.commit()
        assert row.code == encode_short(6)

    async def test_an_empty_registry_starts_the_short_code_counter_at_zero(
            self, db):
        assert await BarcodeService(db).repo.max_short_code_counter() == 0

    async def test_short_codes_for_pieces_short_circuits_on_an_empty_list(self, db):
        assert await BarcodeService(db).repo.short_codes_for_pieces([]) == {}

    async def test_a_long_primary_code_is_never_reported_as_a_short_code(
            self, db, pieces):
        """A piece minted before the switch has one row — its long code, primary
        and active. Returning that as `short_code` is precisely what it is not."""
        codes = await BarcodeService(db).repo.short_codes_for_pieces(
            [pieces[0].id])
        assert codes == {}

    async def test_the_batched_print_reads_short_circuit_on_an_empty_list(self, db):
        repo = BarcodeService(db).repo
        assert await repo.captions_for_codes([]) == {}
        assert await repo.label_details_for_codes([]) == {}
