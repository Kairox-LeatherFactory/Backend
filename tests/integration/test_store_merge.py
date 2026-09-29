"""
INTEGRATION · the store, now that the drawer is gone.

WHAT THIS FILE IS. The drawer suite asserted a set of rules that were never
really about drawers — completeness, auto-receive, the store-entry gate, the
lining verdict, the merge gate that opens line-stitching. Those rules survive the
refactor unchanged; only where they are WRITTEN moved, from a numbered box onto
the garment. This file carries them forward so removing the drawer costs no
coverage.

THE ONE RULE THAT DID NOT SURVIVE, and should not have:

    "a piece scanned into the wrong drawer is a 409"

There is no wrong drawer any more. That rejection policed an assignment the
system invented at upload, and enforcing it is what made the DM hand-reallocate
200 boxes across a 100-piece style. Its absence is the feature.

THE ONE THAT MATTERS MOST is test_line_stitching_is_still_shut_without_a_send.
`_merge_ok` is the ONLY gate on LINE_STITCHING — `_sequence_ok` returns True
early for it, because the two cut paths run in parallel and there is no single
predecessor — so if that test ever goes green for the wrong reason, a garment can
be line-stitched having never been cut.
"""
import datetime

import pytest

from app.core.enums import ProductionStage, ScreenContext, StorePart, StoreState
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService
from app.modules.store.service import StoreService

pytestmark = pytest.mark.asyncio


async def _log(db, operations, piece, employee_id, code):
    """Raw event rows: these tests exercise STORE behaviour, so routing them
    through the production gates would make a store test fail for a skill or
    attendance reason that has nothing to do with what it asserts."""
    db.add(ProductionEvent(
        sku_id=piece.sku_id, operation_id=operations[code].id,
        employee_id=employee_id, work_date=datetime.date.today(), qty=1,
        entered_by="test", piece_id=piece.id))
    await db.commit()


async def _pasted(db, operations, piece, cutter_id, paster_id):
    """Walk the leather side to its hand-off. Leather may enter the store only
    after PASTING — see core/store_display.STORE_ENTRY_STAGE."""
    await _log(db, operations, piece, cutter_id, "LEATHER_CUTTING")
    await _log(db, operations, piece, cutter_id, "FUSING")
    await _log(db, operations, piece, paster_id, "PASTING")


# ══════════════════════════════════════════════════════════ the two scans
async def test_leather_then_lining_completes_the_garment(
        db, operations, pieces, cutter, lining_cutter, paster):
    """The happy path, and the flow is now TWO scans: the worker and the garment."""
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)

    r1 = await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    assert r1["part_stored"] == StorePart.LEATHER.value
    assert r1["part_inferred"] is True, "the operator is not asked which side"
    assert r1["store_state"] == StoreState.HOLDING_LEATHER.value
    assert r1["complete"] is False

    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    r2 = await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)
    assert r2["part_stored"] == StorePart.LINING.value
    assert r2["holding"] == "HOLDING BOTH"
    assert r2["complete"] is True
    # Auto-receive: two booleans set by two physical scans are not a guess.
    assert r2["store_state"] == StoreState.RECEIVED.value


async def test_lining_first_then_leather_reaches_the_same_place(
        db, operations, pieces, cutter, lining_cutter, paster):
    """Order of arrival is not a rule. The lining is often cut first."""
    piece = pieces[0]
    svc = StoreService(db)
    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    r1 = await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)
    assert r1["store_state"] == StoreState.HOLDING_LINING.value

    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    r2 = await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    assert r2["holding"] == "HOLDING BOTH"
    assert r2["complete"] is True


async def test_a_part_cannot_enter_the_store_before_its_side_is_finished(
        db, operations, pieces, cutter, paster):
    """The WRITE gate, not just the read overlay.

    Leather could once be scanned in straight off the breakdown upload, before it
    was cut. The store believed it, the merge gate opened on it, and the garment
    reached LINE_STITCHING having never been pasted.
    """
    from fastapi import HTTPException
    piece = pieces[1]
    await _log(db, operations, piece, cutter[0].id, "LEATHER_CUTTING")
    with pytest.raises(HTTPException) as exc:
        await StoreService(db).store_scan(
            piece_id=piece.id, employee_id=cutter[0].id,
            part=StorePart.LEATHER.value)
    assert exc.value.status_code == 409
    assert "PASTING" in str(exc.value.detail)


async def test_scanning_the_same_part_twice_is_refused_not_doubled(
        db, operations, pieces, cutter, lining_cutter, paster):
    """Both sides already in — there is nothing left to put anywhere."""
    from fastapi import HTTPException
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)
    with pytest.raises(HTTPException) as exc:
        await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    assert exc.value.status_code == 409


async def test_a_leather_only_garment_is_complete_on_leather_alone(
        db, operations, pieces, cutter, paster):
    """A jacket that takes no lining must not wait for one forever."""
    piece = pieces[2]
    piece.needs_lining = False
    await db.commit()
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    r = await StoreService(db).store_scan(piece_id=piece.id,
                                          employee_id=paster[0].id)
    assert r["needs_lining"] is False
    assert r["complete"] is True
    # But it does NOT auto-receive: that asks for both parts literally present,
    # because the stored flag was wrong for 925 of 1,425 pieces in one live order.
    assert r["store_state"] == StoreState.HOLDING_LEATHER.value


# ══════════════════════════════════════════════════════════ the release
async def test_send_releases_only_the_complete_ones(
        db, operations, pieces, cutter, lining_cutter, paster, dm):
    """One incomplete garment must never lose the complete ones beside it."""
    ready = pieces[0]
    unready = pieces[1]
    svc = StoreService(db)
    await _pasted(db, operations, ready, cutter[0].id, paster[0].id)
    await _log(db, operations, ready, lining_cutter[0].id, "LINING_CUTTING")
    await svc.store_scan(piece_id=ready.id, employee_id=paster[0].id)
    await svc.store_scan(piece_id=ready.id, employee_id=lining_cutter[0].id)

    out = await svc.send(piece_ids=[ready.id, unready.id], actor_user_id=dm.id)
    assert out["sent"] == [ready.code]
    assert len(out["not_ready"]) == 1
    assert out["not_ready"][0]["piece"] == unready.code
    assert out["not_ready"][0]["missing"] == "leather"


async def test_sending_twice_is_a_no_op(db, operations, pieces, cutter,
                                        lining_cutter, paster, dm):
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)
    await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)
    again = await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert again["sent"] == [piece.code]
    assert not again["not_ready"]


async def test_a_sent_garment_accepts_nothing_more(
        db, operations, pieces, cutter, lining_cutter, paster, dm):
    from fastapi import HTTPException
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)
    await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)
    with pytest.raises(HTTPException) as exc:
        await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    assert exc.value.status_code == 409


# ══════════════════════════════════════════════════════════ THE MERGE GATE
async def test_line_stitching_is_still_shut_without_a_send(
        db, operations, pieces, cutter, paster, tailor, stitching_mgr):
    """THE LOAD-BEARING TEST OF THE WHOLE REFACTOR.

    `_merge_ok` is the ONLY gate on LINE_STITCHING: `_sequence_ok` returns True
    early because LINE_STITCHING has no single predecessor by design. If this
    goes green for the wrong reason, a garment can be line-stitched having never
    been cut, and nothing else in the system would notice.
    """
    piece = pieces[0]
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=tailor[0].id, piece_ids=[piece.id],
        work_date=datetime.date.today(), screen=ScreenContext.PIPELINE)
    assert res["count_logged"] == 0
    assert res["merge_blocked"] == [piece.code]


async def test_line_stitching_opens_once_the_garment_is_sent(
        db, operations, pieces, cutter, lining_cutter, paster, tailor,
        stitching_mgr, dm):
    """The other half: the gate must actually OPEN, or nothing ships."""
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)
    await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)

    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=tailor[0].id, piece_ids=[piece.id],
        work_date=datetime.date.today(), screen=ScreenContext.PIPELINE)
    assert res["count_logged"] == 1
    assert not res["merge_blocked"]


async def test_a_complete_but_unsent_garment_is_told_to_send(
        db, operations, pieces, cutter, lining_cutter, paster, tailor,
        stitching_mgr):
    """Completeness and release are different failures with different fixes.

    Collapsing them into one message sends the floor looking for a missing part
    that is already there.
    """
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)

    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=tailor[0].id, piece_ids=[piece.id],
        work_date=datetime.date.today(), screen=ScreenContext.PIPELINE)
    assert res["merge_blocked"] == [piece.code]
    reason = next(b["reason"] for b in res["blocked"] if b["gate"] == "merge")
    assert "not been sent" in reason
    assert "awaiting" not in reason, "it is not missing a part; it is unsent"


async def test_there_is_no_such_thing_as_the_wrong_drawer(
        db, operations, pieces, cutter, paster):
    """The rejection that made the DM re-allocate 200 boxes by hand is gone.

    Any garment can be scanned into the store from anywhere, because the store is
    a state the garment is in and not a place with a fixed number of slots. This
    test exists so that its absence is a decision on the record rather than an
    oversight somebody re-adds later.
    """
    first = pieces[0]
    second = pieces[1]
    svc = StoreService(db)
    for p in (first, second):
        await _pasted(db, operations, p, cutter[0].id, paster[0].id)
        r = await svc.store_scan(piece_id=p.id, employee_id=paster[0].id)
        assert r["store_state"] == StoreState.HOLDING_LEATHER.value


# ══════════════════════════════════════════════════════════ the lookup
async def test_anyone_on_the_floor_can_find_where_a_garment_is(
        db, operations, pieces, cutter, paster):
    """Bug #15. The DM assigns somebody to place garments who has no DM login.

    Under the drawer routes they could not look a barcode up at all, so they had
    to find the DM. Reading where a garment is tells nobody anything they should
    not know.
    """
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)

    row = await svc.piece_row(piece)
    assert row["piece_code"] == piece.code
    assert row["holding"] == "HOLDING LEATHER"
    assert row["store_state"] == StoreState.HOLDING_LEATHER.value

    listing = await svc.list_pieces()
    assert piece.code in [p["piece_code"] for p in listing["pieces"]]


async def test_package_export_takes_the_garment_out_of_the_store(
        db, operations, pieces, cutter, paster):
    """A drawer recycled because the BOX was reused. A garment ships once."""
    piece = pieces[0]
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    assert piece.leather_in is True

    await svc.release_nocommit(piece.id)
    await db.commit()
    assert piece.store_state == StoreState.WAITING.value
    assert piece.leather_in is False


# ══════════════════════════════════════════════════ the lining verdict, ported
# These carry forward test_lining_gate_regression.py, which protected a real
# production incident: `piece.needs_lining` is written ONCE at upload and never
# recomputed, and on the live database two orders holding the SAME 17 styles came
# out 0-flagged and 925-flagged. The completeness gate read that frozen flag as
# gospel, so a KNIT jacket whose flag said False was "complete" on its leather
# alone — it received, it sent, the merge gate opened, and the garment ran to
# PACKAGE_EXPORT having never had a lining cut.
#
# The rule that replaced it: the stored flag is EVIDENCE, not the verdict, and
# every source can only ever ADD a lining requirement. Moving the store from the
# drawer onto the piece must not weaken that, which is what these assert.

async def _knit_piece(db, pieces, name="SHINOBI KNIT"):
    from app.modules.clients.models import SKU, Style
    piece = pieces[0]
    sku = await db.get(SKU, piece.sku_id)
    style = await db.get(Style, sku.style_id)
    style.name = name
    piece.needs_lining = False            # the stale flag, exactly as it was
    await db.commit()
    return piece


async def test_a_knit_style_needs_a_lining_despite_a_false_flag(
        db, operations, pieces, cutter, paster, dm):
    """The incident, pinned. A stale False must not make a lined jacket complete."""
    piece = await _knit_piece(db, pieces)
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    r = await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    assert r["needs_lining"] is True, "the style NAME says it takes a lining"
    assert r["complete"] is False
    assert "KNIT" in (r["lining_reason"] or "").upper()

    out = await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert out["sent"] == []
    assert out["not_ready"][0]["missing"] == "lining"


async def test_scanning_the_lining_in_unblocks_the_knit_style(
        db, operations, pieces, cutter, lining_cutter, paster, dm):
    """The other half — the requirement must be satisfiable, or nothing ships."""
    piece = await _knit_piece(db, pieces)
    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    r = await svc.store_scan(piece_id=piece.id, employee_id=lining_cutter[0].id)
    assert r["complete"] is True
    out = await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert out["sent"] == [piece.code]


async def test_a_genuinely_leather_only_garment_still_sends_on_leather_alone(
        db, operations, pieces, cutter, paster, dm):
    """The rule may only ADD requirements — it must not invent one.

    A plainly-named style with no lining colour, no lining cut and a False flag is
    leather-only, and it has to stay sendable or the fix would stall the floor.
    """
    from app.modules.clients.models import SKU, Style
    piece = pieces[3]
    sku = await db.get(SKU, piece.sku_id)
    style = await db.get(Style, sku.style_id)
    style.name = "CLERMONT"
    style.article = "GOAT SUEDE"
    piece.needs_lining = False
    await db.commit()

    svc = StoreService(db)
    await _pasted(db, operations, piece, cutter[0].id, paster[0].id)
    r = await svc.store_scan(piece_id=piece.id, employee_id=paster[0].id)
    assert r["needs_lining"] is False
    assert r["complete"] is True
    out = await svc.send(piece_ids=[piece.id], actor_user_id=dm.id)
    assert out["sent"] == [piece.code]


async def test_a_lining_cut_outranks_a_declared_no(
        db, operations, pieces, cutter, lining_cutter, paster):
    """Somebody physically cut a lining. The paperwork does not get to disagree."""
    from app.modules.clients.models import SKU, Style
    piece = pieces[4]
    sku = await db.get(SKU, piece.sku_id)
    style = await db.get(Style, sku.style_id)
    style.name = "CLERMONT"
    style.needs_lining = False            # the DM's explicit "no"
    piece.needs_lining = False
    await db.commit()

    await _log(db, operations, piece, lining_cutter[0].id, "LINING_CUTTING")
    needs, reason = await StoreService(db).needs_lining(piece)
    assert needs is True, "a logged lining cut settles the question outright"


# ══════════════════════════════════ the accessory answer is a LIST, not a flag
#
# `piece.accessories_in` is a ROLL-UP: true only when every accessory line the
# style declares has been issued in full. It is the right thing to gate a SEND
# on and the wrong thing to show an operator, because it cannot say whether the
# zip is missing or the buttons are. These pin the per-line read the store
# screens use to verify a kit without issuing anything.
async def test_the_store_lookup_names_every_accessory_line(
        db, operations, pieces, cutter, paster):
    """Four buttons and one zip come back as two lines, each with what is owed."""
    from app.modules.barcode.models import MaterialLot, StyleMaterialSpec
    from app.modules.clients.models import SKU
    piece = pieces[0]
    sku = await db.get(SKU, piece.sku_id)
    for kw in (dict(subtype="BUTTON", article="BTN-4H", size="18L",
                    qty=4, on_hand=1000),
               dict(subtype="ZIP", article="ZIP-YKK", size="60cm",
                    qty=1, on_hand=500)):
        db.add(MaterialLot(category="ACCESSORY", subtype=kw["subtype"],
                           article=kw["article"], colour="BLACK",
                           size=kw["size"], uom="pcs", on_hand=kw["on_hand"],
                           is_active=True))
        db.add(StyleMaterialSpec(
            style_id=sku.style_id, category="ACCESSORY", subtype=kw["subtype"],
            article=kw["article"], colour="BLACK", size=kw["size"],
            qty_per_piece=kw["qty"], uom="pcs"))
    await db.commit()

    svc = StoreService(db)
    detail = await svc.piece_detail(piece)

    assert detail["kit_required"] is True
    assert detail["accessories_in"] is False
    # THE POINT: two named lines, not one boolean.
    assert {a["article"] for a in detail["accessories"]} == {"BTN-4H", "ZIP-YKK"}
    assert {a["outstanding"] for a in detail["accessories"]} == {4.0, 1.0}
    assert "ACCESSORIES" in detail["awaiting"]
    assert detail["kit_status"] == "PENDING"


async def test_a_style_with_no_accessories_hides_the_checklist(
        db, operations, pieces, cutter, paster):
    """NOT_REQUIRED is not 'nothing issued'.

    Every style released before the material spec declares no accessories, and
    rendering them an empty panel they can never satisfy is how the store learns
    to ignore the column.
    """
    piece = pieces[0]
    detail = await StoreService(db).piece_detail(piece)
    assert detail["kit_required"] is False
    assert detail["kit_status"] == "NOT_REQUIRED"
    assert detail["accessories"] == []
    assert "ACCESSORIES" not in detail["awaiting"]


async def test_a_no_accessory_style_is_not_permanently_owed_a_kit(
        db, operations, pieces, cutter, paster):
    """`kit.complete` means NOTHING IS OWED, and that is true of a style with
    no accessory lines.

    It used to require kit_required, so every garment released before the
    material spec came back `complete: false` on a kit it could never be given,
    and the store screen carried an outstanding chip forever. The write path has
    always read kit_rules.kit_satisfied; this is the read path saying the same.
    """
    from app.modules.materials.style_spec_service import StyleSpecService
    piece = pieces[0]
    view = await StyleSpecService(db).kit_view(piece.id)
    assert view["status"] == "NOT_REQUIRED"
    assert view["complete"] is True
