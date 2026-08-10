"""
INTEGRATION · The lot picker, unique lots, derived auto-fill, stock validation.

WHAT THIS COVERS (the four decisions made for the cut screen)
    1. OPTION A — one lot per material spec. Re-supply tops the existing lot up
       through /materials/receive; create_lot refuses to mint a second row. This
       is what makes "filter article + colour + thickness → one lot" a rule the
       picker can rely on rather than an accident of the seed data.
    2. THE PICKER — GET /materials/lots hands the frontend the lot_id that
       /materials/spec (a form definition) and /materials/stock (aggregate
       totals) both cannot.
    3. DERIVED AUTO-FILL — "the lot this SKU was last cut from" is a QUERY over
       production_event, not a stored preference. Keyed on the SKU because the
       SKU carries the COLOUR; keying it on the style would hand a FOREST lot to
       a WHISKY garment of the same style.
    4. STOCK VALIDATION — cutting more than is on hand WARNS and still records
       the cut. The garment is physically cut; refusing the log would lose the
       production record to protect a number. What is not acceptable is doing it
       silently, which is what happened before.
"""
import datetime
import uuid

import pytest
from fastapi import HTTPException

from app.core.enums import BarcodeStatus, BarcodeType, ScreenContext
from app.modules.barcode.models import BarcodeRegistry, MaterialLot
from app.modules.materials.service import MaterialService
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService


# ── helpers ─────────────────────────────────────────────────────────────────
class LotBody:
    """Duck-typed stand-in for schemas.LotCreate."""
    def __init__(self, **kw):
        self.category = kw.get("category", "LEATHER")
        self.subtype = kw.get("subtype")
        self.article = kw.get("article", "GOAT SUEDE")
        self.colour = kw.get("colour", "PINE GREEN")
        self.supplier_id = kw.get("supplier_id")
        self.attributes = kw.get("attributes", {"thickness": "0.8-1.0", "dcm": 500})


async def _make_lot(db, **kw):
    return await MaterialService(db).create_lot(LotBody(**kw))


async def _raw_lot(db, *, article, colour, thickness, on_hand, code):
    """Insert a lot directly — for cases the strict create_lot path would reject
    or where a second same-spec row is needed to prove the picker's behaviour."""
    lot = MaterialLot(category="LEATHER", article=article, colour=colour,
                      thickness=thickness, uom="dcm", on_hand=on_hand,
                      is_active=True)
    db.add(lot)
    await db.flush()
    db.add(BarcodeRegistry(code=code, type=BarcodeType.LEATHER_LOT.value,
                           status=BarcodeStatus.ACTIVE.value,
                           material_lot_id=lot.id, caption=article))
    await db.commit()
    return lot


# ══════════════════════════════════════════════ 1 · OPTION A: unique lots
@pytest.mark.asyncio
async def test_the_same_spec_twice_is_refused_with_a_pointer_to_the_first(db):
    first = await _make_lot(db)
    with pytest.raises(HTTPException) as exc:
        await _make_lot(db)

    assert exc.value.status_code == 409
    detail = str(exc.value.detail)
    # the error has to be ACTIONABLE: it names the lot to top up instead
    assert str(first["lot_id"]) in detail
    assert "receive" in detail.lower()
    assert "GOAT SUEDE" in detail


@pytest.mark.asyncio
async def test_a_different_colour_is_a_different_lot(db):
    a = await _make_lot(db, colour="PINE GREEN")
    b = await _make_lot(db, colour="FOREST")
    assert a["lot_id"] != b["lot_id"]


@pytest.mark.asyncio
async def test_a_different_thickness_is_a_different_lot(db):
    a = await _make_lot(db, attributes={"thickness": "0.8-1.0", "dcm": 100})
    b = await _make_lot(db, attributes={"thickness": "1.2-1.4", "dcm": 100})
    assert a["lot_id"] != b["lot_id"]


@pytest.mark.asyncio
async def test_a_retired_lot_does_not_block_recreating_that_spec(db):
    first = await _make_lot(db)
    lot = await MaterialService(db).repo.get_lot(first["lot_id"])
    lot.is_active = False
    await db.commit()

    again = await _make_lot(db)          # must not 409
    assert again["lot_id"] != first["lot_id"]


# ══════════════════════════════════════════════════════ 2 · THE PICKER
@pytest.mark.asyncio
async def test_the_picker_returns_the_lot_id_and_its_barcode(db):
    created = await _make_lot(db)
    res = await MaterialService(db).list_lots(category="LEATHER")

    assert res["count"] == 1
    row = res["lots"][0]
    assert row["lot_id"] == created["lot_id"]
    assert row["barcode"] == created["lot_barcode"]
    assert row["available"] == 500.0
    assert row["uom"] == "dcm"


@pytest.mark.asyncio
async def test_filtering_narrows_to_one_row(db):
    await _make_lot(db, colour="PINE GREEN")
    await _make_lot(db, colour="FOREST")
    await _make_lot(db, colour="D.BROWN")
    svc = MaterialService(db)

    assert (await svc.list_lots(category="LEATHER"))["count"] == 3
    narrowed = await svc.list_lots(category="LEATHER", article="GOAT SUEDE",
                                   colour="FOREST", thickness="0.8-1.0")
    assert narrowed["count"] == 1
    assert narrowed["lots"][0]["colour"] == "FOREST"


@pytest.mark.asyncio
async def test_options_drive_the_cascading_dropdowns(db):
    await _make_lot(db, colour="PINE GREEN")
    await _make_lot(db, colour="FOREST")
    await _make_lot(db, article="COW CALF", colour="WHISKY",
                    attributes={"thickness": "1.2-1.4", "dcm": 90})

    opts = (await MaterialService(db).list_lots(category="LEATHER"))["options"]
    assert opts["article"] == ["COW CALF", "GOAT SUEDE"]
    assert opts["colour"] == ["FOREST", "PINE GREEN", "WHISKY"]
    assert opts["thickness"] == ["0.8-1.0", "1.2-1.4"]


@pytest.mark.asyncio
async def test_required_flags_which_lots_can_cover_the_cut(db):
    await _make_lot(db, colour="PINE GREEN",
                    attributes={"thickness": "0.8-1.0", "dcm": 500})
    await _make_lot(db, colour="FOREST",
                    attributes={"thickness": "0.8-1.0", "dcm": 40})

    res = await MaterialService(db).list_lots(category="LEATHER", required=100)
    by_colour = {r["colour"]: r for r in res["lots"]}
    assert by_colour["PINE GREEN"]["covers_required"] is True
    assert by_colour["FOREST"]["covers_required"] is False
    assert res["required"] == 100


@pytest.mark.asyncio
async def test_covers_required_is_null_when_not_asked(db):
    await _make_lot(db)
    res = await MaterialService(db).list_lots(category="LEATHER")
    assert res["lots"][0]["covers_required"] is None


@pytest.mark.asyncio
async def test_an_exhausted_lot_is_listed_not_hidden(db):
    """A manager searching for a lot they know exists must FIND it, with
    available: 0 explaining itself — not get an empty screen."""
    await _raw_lot(db, article="GOAT SUEDE", colour="SPENT", thickness="0.8-1.0",
                   on_hand=0, code="LOT-SPENT")
    res = await MaterialService(db).list_lots(category="LEATHER", colour="SPENT")
    assert res["count"] == 1
    assert res["lots"][0]["available"] == 0.0


# ═════════════════════════════════════════ 3 · DERIVED LAST-USED AUTO-FILL
@pytest.mark.asyncio
async def test_no_suggestion_before_anything_has_been_cut(db, order_tree):
    await _make_lot(db)
    res = await MaterialService(db).list_lots(
        category="LEATHER", sku_id=order_tree["sku"].id)
    assert res["suggested_lot_id"] is None
    assert all(r["last_used_for_sku"] is False for r in res["lots"])


@pytest.mark.asyncio
async def test_the_lot_last_cut_for_this_sku_is_suggested(db, order_tree,
                                                          operations, cutter):
    """The auto-fill is DERIVED from production_event — no stored preference.

    The two cuts are on DIFFERENT work_dates on purpose. `created_at` alone
    cannot separate them: it defaults to now(), which is transaction-start time
    on Postgres and whole seconds on SQLite, so two events written in the same
    test tie. work_date is the business fact the ordering leads on.
    """
    old = await _make_lot(db, colour="FOREST")
    new = await _make_lot(db, colour="PINE GREEN")
    sku = order_tree["sku"]
    today = datetime.date.today()

    for lot_id, day in ((old["lot_id"], today - datetime.timedelta(days=3)),
                        (new["lot_id"], today)):        # most recent day wins
        db.add(ProductionEvent(
            sku_id=sku.id, operation_id=operations["LEATHER_CUTTING"].id,
            employee_id=cutter[0].id, work_date=day, qty=1,
            entered_by="test", leather_lot_id=lot_id))
        await db.commit()

    res = await MaterialService(db).list_lots(category="LEATHER", sku_id=sku.id)
    assert res["suggested_lot_id"] == new["lot_id"]
    flagged = [r for r in res["lots"] if r["last_used_for_sku"]]
    assert [r["lot_id"] for r in flagged] == [new["lot_id"]]
    # and it is sorted to the top so it is the first row the manager sees
    assert res["lots"][0]["lot_id"] == new["lot_id"]


@pytest.mark.asyncio
async def test_another_skus_history_is_not_borrowed(db, order_tree, operations,
                                                    cutter):
    """The key is the SKU — which carries COLOUR. A lot cut for one colourway
    must not be suggested for another."""
    from app.modules.clients.models import SKU
    lot = await _make_lot(db)
    other = SKU(style_id=order_tree["style"].id, color_code="WHIS",
                color_name="WHISKY", size="M", qty_ordered=2,
                code="JP-CLERMONT-WHIS-M")
    db.add(other)
    await db.commit()

    db.add(ProductionEvent(
        sku_id=order_tree["sku"].id, operation_id=operations["LEATHER_CUTTING"].id,
        employee_id=cutter[0].id, work_date=datetime.date.today(), qty=1,
        entered_by="test", leather_lot_id=lot["lot_id"]))
    await db.commit()

    res = await MaterialService(db).list_lots(category="LEATHER", sku_id=other.id)
    assert res["suggested_lot_id"] is None


@pytest.mark.asyncio
async def test_lining_suggestions_read_the_lining_column(db, order_tree,
                                                         operations, cutter):
    lining = await _make_lot(db, category="LINING", subtype="PLAIN_LINING",
                             article="TAFFTA", colour="BLACK",
                             attributes={"thickness": "NA", "mtrs": 300})
    db.add(ProductionEvent(
        sku_id=order_tree["sku"].id, operation_id=operations["LINING_CUTTING"].id,
        employee_id=cutter[0].id, work_date=datetime.date.today(), qty=1,
        entered_by="test", lining_lot_id=lining["lot_id"]))
    await db.commit()

    res = await MaterialService(db).list_lots(
        category="LINING", sku_id=order_tree["sku"].id)
    assert res["suggested_lot_id"] == lining["lot_id"]


# ══════════════════════════════════════════════════ 4 · STOCK VALIDATION
@pytest.mark.asyncio
async def test_cutting_within_stock_raises_no_warning(db):
    created = await _make_lot(db, attributes={"thickness": "0.8-1.0", "dcm": 500})
    svc = MaterialService(db)
    await svc.decrement_for_cut_nocommit(created["lot_id"], 100)
    assert svc.last_decrement_warning is None


@pytest.mark.asyncio
async def test_cutting_beyond_stock_warns_but_still_records(db):
    created = await _make_lot(db, attributes={"thickness": "0.8-1.0", "dcm": 50})
    svc = MaterialService(db)
    available_after = await svc.decrement_for_cut_nocommit(created["lot_id"], 120)
    await db.commit()

    w = svc.last_decrement_warning
    assert w is not None, "a short cut must not pass silently"
    assert w["requested"] == 120.0
    assert w["available_before"] == 50.0
    assert w["short_by"] == 70.0
    assert w["on_hand_after"] == -70.0
    assert "short by" in w["note"].lower()
    # the cut WAS applied — the stock move is real, not rolled back
    assert available_after == -70.0
    lot = await svc.repo.get_lot(created["lot_id"])
    assert float(lot.on_hand) == -70.0


@pytest.mark.asyncio
async def test_the_log_response_carries_the_shortfall(db, order_tree, pieces,
                                                      operations, cutter,
                                                      cutting_mgr):
    """End-to-end: the warning has to reach the floor, not just the service."""
    created = await _make_lot(db, attributes={"thickness": "0.8-1.0", "dcm": 10})
    piece, _ = pieces[0]

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=datetime.date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=created["lot_id"], consumption_qty=25)

    assert res["count_logged"] == 1                  # the cut is recorded
    assert res["consumption_recorded"]["stock_short"] is True
    assert res["stock_warning"] is not None
    assert res["stock_warning"]["short_by"] == 15.0


@pytest.mark.asyncio
async def test_a_healthy_cut_reports_no_stock_warning(db, order_tree, pieces,
                                                      operations, cutter,
                                                      cutting_mgr):
    created = await _make_lot(db, attributes={"thickness": "0.8-1.0", "dcm": 500})
    piece, _ = pieces[0]

    res = await ProductionService(db).log_batch(
        user=cutting_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=datetime.date.today(), screen=ScreenContext.LEATHER_CUT,
        leather_lot_id=created["lot_id"], consumption_qty=12.5)

    assert res["stock_warning"] is None
    assert res["consumption_recorded"]["stock_short"] is False
    assert res["consumption_recorded"]["available_after"] == 487.5
