"""
INTEGRATION · The delivery entered in TWO SITTINGS, and the stock figures it
leaves behind.

WHAT THIS IS FOR, in the floor's own words. The van turns up and whoever signs
for it has ten seconds: article, colour, total dcm. Splitting that into approved
and rejected, counting the bundles and measuring every hide is twenty minutes of
quiet work that happens later — the same afternoon, or the next day.

`create_lot` and `receive` both demanded the whole story at once. A form like
that has exactly two outcomes on a real floor, and both are worse than nothing:
the delivery goes unrecorded until somebody has twenty minutes, or numbers get
invented to get past the required fields. So a delivery ARRIVES now and is
FINISHED later, and the stock it brought is usable in between.

THE MONEY PATH THIS PROTECTS is the correction at the end. The arrival put the
declared total into stock and the floor may have cut some of it since; the
completion therefore moves on_hand by (approved − declared) and NEVER assigns it,
because assigning would silently undo real production. That is the one piece of
arithmetic here that could lose data, and it has its own test.
"""
import uuid
from decimal import Decimal

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.enums import IntakeStatus, SheetStatus
from app.modules.barcode.models import BarcodeRegistry, MaterialLot, MaterialReceipt
from app.modules.materials import schemas
from app.modules.materials.service import MaterialService

pytestmark = pytest.mark.integrity

ACTOR = uuid.uuid4()


def arrival(**kw):
    """What the person at the gate actually knows, and nothing more."""
    base = dict(article="GOAT SUEDE", colour="FOREST", total_qty=1240)
    base.update(kw)
    return schemas.ArrivalCreate(**base)


# ══════════════════════════════════════════════════ 1 · the gate entry
async def test_three_fields_are_enough_to_book_a_delivery_in(db):
    """Article, colour, total. NOT thickness — that is on the packing note.

    `create_lot` refuses a leather lot with no thickness (422, by design: it is
    the considered path and the field set is strict there). The gate is not the
    considered path, and holding a physical delivery out of the system because
    nobody has read a packing note yet is how a floor learns to work around an
    ERP.
    """
    out = await MaterialService(db).arrive(arrival(), actor_id=ACTOR)
    assert out["status"] == IntakeStatus.PENDING.value
    assert out["lot_created"] is True
    assert out["lot_barcode"], "the label must print at the gate, not at QC"
    assert out["thickness"] is None
    # Named, so the completion form knows what it is still owed.
    assert "approved_qty" in out["outstanding"]
    assert "sheets" in out["outstanding"]
    assert "thickness" in out["outstanding"]


async def test_the_material_is_cuttable_the_moment_it_is_booked_in(db):
    """PROVISIONAL, not fictional. The leather is physically in the building.

    Holding it out of stock until somebody has done QC would mean the cutting
    floor cannot see material it can see with its own eyes, which ends with the
    hides being cut anyway and the ledger finding out afterwards.
    """
    await MaterialService(db).arrive(arrival(), actor_id=ACTOR)
    stock = await MaterialService(db).stock(category="LEATHER")
    assert stock["arrived"] == 1240.0
    assert stock["balance"] == 1240.0
    assert stock["available"] == 1240.0
    # …and it says which part of itself is provisional. A stock figure that
    # cannot is one people stop trusting.
    assert stock["pending_arrivals"] == 1
    assert stock["pending_arrival_qty"] == 1240.0


async def test_a_second_delivery_of_the_same_material_tops_the_lot_up(db):
    """ONE LOT PER SPEC still holds. A lot says WHAT the material is.

    Two rows for one article/colour would split its stock so neither showed the
    true figure, and the cut screen's picker would offer two identical rows with
    no way to tell them apart.
    """
    svc = MaterialService(db)
    first = await svc.arrive(arrival(), actor_id=ACTOR)
    second = await svc.arrive(arrival(total_qty=600), actor_id=ACTOR)
    assert second["lot_id"] == first["lot_id"]
    assert second["lot_created"] is False
    assert second["arrived"] == 1840.0
    assert second["receipt_id"] != first["receipt_id"], \
        "two deliveries are two receipts — that is the purchase history"


# ══════════════════════════════════════════════════ 2 · the worklist
async def test_the_queue_is_what_makes_a_half_entered_delivery_safe(db):
    """An unfinished arrival nobody can find is provisional stock quietly
    becoming permanent stock that was never checked."""
    svc = MaterialService(db)
    await svc.arrive(arrival(), actor_id=ACTOR)
    await svc.arrive(arrival(colour="BLACK", total_qty=900), actor_id=ACTOR)

    queue = await svc.list_arrivals()
    assert queue["count"] == 2
    assert {r["colour"] for r in queue["arrivals"]} == {"FOREST", "BLACK"}
    assert all(r["outstanding"] for r in queue["arrivals"])
    # Oldest first: the one nobody has come back to for three days is the one
    # that matters, and newest-first buries it.
    assert queue["arrivals"][0]["colour"] == "FOREST"


async def test_a_completed_arrival_leaves_the_queue(db):
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(), actor_id=ACTOR)
    await svc.complete_arrival(
        booked["receipt_id"],
        schemas.ArrivalComplete(approved_qty=1240, rejected_qty=0),
        actor_id=ACTOR)
    assert (await svc.list_arrivals())["count"] == 0
    assert (await svc.list_arrivals(status_filter="COMPLETED"))["count"] == 1


# ══════════════════════════════════════════════════ 3 · the second sitting
async def test_completing_enters_the_split_the_hides_and_the_thickness(db):
    """The twenty-minute job, all in one call."""
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(sheet_count=4), actor_id=ACTOR)
    out = await svc.complete_arrival(
        booked["receipt_id"],
        schemas.ArrivalComplete(
            approved_qty=1240, rejected_qty=90, thickness="1.2mm",
            sheets=[schemas.SheetIn(dcm=d) for d in (280, 310, 320, 330)]),
        actor_id=ACTOR)

    assert out["status"] == IntakeStatus.COMPLETED.value
    assert out["rejected_logged"] == 90.0
    assert len(out["sheets"]) == 4
    assert out["sheets_arrived"] == 4
    assert out["sheets_balance"] == 4, "nothing has claimed a hide yet"
    assert all(s["code"].startswith("LS-") for s in out["sheets"]), \
        "every hide gets its own printable label"

    lot = await svc.get_lot(booked["lot_id"])
    assert lot["thickness"] == "1.2mm", \
        "the one identity field a gate entry cannot supply is filled in here"


async def test_the_rejected_quantity_never_entered_stock(db):
    """It went back on the van. It is the supplier's quality history, nothing else."""
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(total_qty=1240), actor_id=ACTOR)
    out = await svc.complete_arrival(
        booked["receipt_id"],
        schemas.ArrivalComplete(approved_qty=1240, rejected_qty=90),
        actor_id=ACTOR)
    assert out["balance"] == 1240.0, "the rejected 90 was never in the building"
    lot = await svc.get_lot(booked["lot_id"])
    assert lot["rejected"] == 90.0


async def test_the_correction_is_a_delta_so_it_cannot_undo_a_cut(db):
    """THE ONE PIECE OF ARITHMETIC HERE THAT COULD LOSE PRODUCTION DATA.

    1240 was booked in at the gate. The floor cut 400 of it that afternoon. Only
    then does somebody enter the QC split, and it turns out 1200 was approved.

    on_hand must move by (1200 − 1240) = −40, landing on 800. Assigning
    `on_hand = approved` would land on 1200 and silently put back 400 dcm of
    leather that is already in garments.
    """
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(total_qty=1240), actor_id=ACTOR)
    await MaterialService(db).decrement_for_cut_nocommit(booked["lot_id"], 400)
    await db.commit()

    out = await MaterialService(db).complete_arrival(
        booked["receipt_id"],
        schemas.ArrivalComplete(approved_qty=1200, rejected_qty=40),
        actor_id=ACTOR)

    assert out["on_hand_delta"] == -40.0
    assert out["balance"] == 800.0, "the 400 that was cut stays cut"
    assert out["used"] == 400.0
    assert out["arrived"] == 1200.0
    assert out["arrived"] - out["used"] == out["balance"]
    assert any(w["kind"] == "arrival_corrected" for w in out["warnings"])


async def test_a_gate_bundle_count_that_disagrees_is_reported_not_refused(db):
    """A count taken at the gate is a glance.

    Refusing the measurements over it is how a floor learns to stop counting at
    the gate at all — so the mismatch is surfaced where a human can see it, and
    the hides that were actually measured are the ones that exist.
    """
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(sheet_count=6), actor_id=ACTOR)
    out = await svc.complete_arrival(
        booked["receipt_id"],
        schemas.ArrivalComplete(
            approved_qty=1240,
            sheets=[schemas.SheetIn(dcm=d) for d in (280, 310, 320, 330)]),
        actor_id=ACTOR)
    assert out["sheets_arrived"] == 4
    mismatch = next(w for w in out["warnings"]
                    if w["kind"] == "sheet_count_mismatch")
    assert mismatch["declared_sheet_count"] == 6
    assert mismatch["sheets_entered"] == 4


async def test_an_arrival_cannot_be_completed_twice(db):
    """A second completion would re-apply the delta and move stock again.

    The escape hatch is named in the message: a correction to a finished delivery
    is an ADJUSTMENT, which carries a reason, so the movement keeps a name
    against it.
    """
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(), actor_id=ACTOR)
    body = schemas.ArrivalComplete(approved_qty=1240)
    await svc.complete_arrival(booked["receipt_id"], body, actor_id=ACTOR)
    with pytest.raises(HTTPException) as exc:
        await MaterialService(db).complete_arrival(
            booked["receipt_id"], body, actor_id=ACTOR)
    assert exc.value.status_code == 409
    assert "adjust" in str(exc.value.detail)


async def test_an_arrival_of_nothing_is_refused(db):
    with pytest.raises(Exception):
        await MaterialService(db).arrive(arrival(total_qty=0), actor_id=ACTOR)


# ══════════════════════════════════════════════════ 4 · the figures it leaves
async def test_used_is_a_real_number_on_a_lot_page(db):
    """IT USED TO BE A LIE, and it was the only figure on the page nobody could
    act on.

    `used` was declared on the response schema with a default of 0.0 and the read
    never set it, so every lot in the building reported nothing used however much
    of it had been cut.
    """
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(total_qty=1240), actor_id=ACTOR)
    await MaterialService(db).decrement_for_cut_nocommit(booked["lot_id"], 430)
    await db.commit()

    lot = await MaterialService(db).get_lot(booked["lot_id"])
    assert lot["used"] == 430.0
    assert lot["arrived"] == 1240.0
    assert lot["balance"] == 810.0
    assert lot["arrived"] - lot["used"] == lot["balance"]
    assert lot["on_hand"] == lot["balance"]


async def test_the_hide_count_moves_with_the_hides(db):
    """The same three numbers, in skins: arrived, used, and what is on the shelf."""
    svc = MaterialService(db)
    booked = await svc.arrive(arrival(), actor_id=ACTOR)
    await svc.complete_arrival(
        booked["receipt_id"],
        schemas.ArrivalComplete(
            approved_qty=1240,
            sheets=[schemas.SheetIn(dcm=d) for d in (280, 310, 320, 330)]),
        actor_id=ACTOR)

    lot = await MaterialService(db).get_lot(booked["lot_id"])
    assert (lot["sheets_arrived"], lot["sheets_balance"]) == (4, 0 + 4)
    assert lot["sheets_used"] == 0
    assert lot["sheets_arrived_dcm"] == 1240.0

    # One goes out to a cutter and is cut.
    sheets = await MaterialService(db).repo.sheets_for_lot(booked["lot_id"])
    sheets[0].status = SheetStatus.CONSUMED.value
    await db.commit()

    lot = await MaterialService(db).get_lot(booked["lot_id"])
    assert lot["sheets_used"] == 1
    assert lot["sheets_balance"] == 3
    assert lot["sheets_arrived"] == 4, "arrived never falls — it is a total"
