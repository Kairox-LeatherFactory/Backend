"""
INTEGRATION · stage-wise reject & rework, and who is answerable for the piece.

THE REPORTED GAP. "A piece completed in Fusing was rejected during Pasting, but
there is currently no proper way to send it back to Fusing for rework."

There was not. The sequence gate treats a completed stage as complete for good,
re-logging it lands in the `rework` bucket and writes nothing, and the only route
back was an edit in the database.

THE TWO RULES THE FACTORY CHOSE, and the reason each test exists:

  · a garment somebody has called defective STOPS MOVING until the DM decides.
    Otherwise the defect travels down the line and more work is spent on a piece
    that is going back anyway.
  · a redo INVALIDATES the stages done on top of it. Redoing FUSING means the
    PASTING done on that badly-fused panel must be done again — a re-walk, not a
    jump back to where the piece had reached.

AND THE ONE THAT MATTERS MOST TO THEM: if a stage was not done properly and the
garment is damaged because of it, that worker is answerable for that piece. That
is a COLUMN, not a sentence, so it can be counted.
"""
import datetime

import pytest

from app.core.enums import ProductionStage, ScreenContext
from app.modules.production.inspection import InspectionService
from app.modules.production.models import ProductionEvent
from app.modules.production.service import ProductionService

pytestmark = pytest.mark.asyncio
TODAY = datetime.date.today()


async def _log(db, operations, piece, employee_id, code):
    db.add(ProductionEvent(
        sku_id=piece.sku_id, operation_id=operations[code].id,
        employee_id=employee_id, work_date=TODAY, qty=1,
        entered_by="test", piece_id=piece.id))
    await db.commit()


async def _walk_to_line_stitching(db, operations, piece, emp_id):
    """A garment that has reached LINE_STITCHING — the state the bug describes."""
    for code in ("LEATHER_CUTTING", "FUSING", "PASTING", "LINE_STITCHING"):
        await _log(db, operations, piece, emp_id, code)
    piece.store_state = "sended"
    piece.leather_in = True
    piece.lining_in = True
    piece.needs_lining = False
    await db.commit()


def _piece(pieces, i=0):
    return pieces[i]


# ═════════════════════════════════════════════════════════ raising
async def test_a_bare_rejection_is_a_reject_that_gets_fixed_where_it_stands(
        db, operations, pieces, cutter):
    """THE SMALLEST USEFUL CALL: the piece and the stage the defect was seen at.

    `verdict` used to be required and `action` used to be required on a reject,
    so every real call carried two fields with one sensible value each — this
    endpoint is only ever opened because somebody found a defect. Both now
    default: REJECT, and FIX (repair it where it stands, nothing moves).

    A REDO is still explicit, because it sends the garment backwards — see
    test_a_redo_must_name_where_it_goes_back_to.
    """
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    out = await InspectionService(db).raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING",
        reason="looks wrong")
    assert out["verdict"] == "REJECT"
    assert out["action"] == "FIX"
    assert out["return_to_stage"] is None
    # Still the DM's call: a FIX is PENDING until somebody signs it off.
    assert out["status"] == "PENDING"


async def test_a_workmanship_defect_must_name_who_is_answerable(
        db, operations, pieces, cutter):
    """THE FACTORY'S OWN REQUIREMENT, enforced.

    A workmanship defect with nobody named is the state this feature exists to
    end — it is exactly what a free-text reason box produced.
    """
    from fastapi import HTTPException
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    with pytest.raises(HTTPException) as exc:
        await InspectionService(db).raise_inspection(
            piece_id=piece.id, found_at_stage="LINE_STITCHING",
            verdict="REJECT", action="REDO", return_to_stage="FUSING",
            defect_type="WORKMANSHIP")
    assert exc.value.status_code == 422
    assert "answerable" in str(exc.value.detail)


async def test_a_bad_hide_blames_nobody(db, operations, pieces, cutter):
    """PRODUCT_DAMAGE must not name a person.

    Attributing a flawed hide to the cutter who happened to use it puts a defect
    against somebody who did nothing wrong — and teaches the floor to stop
    reporting damage at all.
    """
    from fastapi import HTTPException
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    with pytest.raises(HTTPException) as exc:
        await InspectionService(db).raise_inspection(
            piece_id=piece.id, found_at_stage="LINE_STITCHING",
            verdict="REJECT", action="REDO", return_to_stage="FUSING",
            defect_type="PRODUCT_DAMAGE",
            responsible_employee_id=cutter[0].id)
    assert exc.value.status_code == 422
    assert "no employee is answerable" in str(exc.value.detail)


async def test_a_garment_cannot_be_sent_back_to_a_stage_it_never_reached(
        db, operations, pieces, cutter):
    """Otherwise the reject becomes a door through the chain.

    'Returning' a piece to LINE_STITCHING it has never had would let the log
    write that stage as a rework and skip everything before it — through the one
    mechanism built to bypass the sequence gate.
    """
    from fastapi import HTTPException
    piece = _piece(pieces)
    await _log(db, operations, piece, cutter[0].id, "LEATHER_CUTTING")
    with pytest.raises(HTTPException) as exc:
        await InspectionService(db).raise_inspection(
            piece_id=piece.id, found_at_stage="LEATHER_CUTTING",
            verdict="REJECT", action="REDO", return_to_stage="PASTING")
    assert exc.value.status_code == 409
    assert "never completed" in str(exc.value.detail)


async def test_a_pass_needs_nobody_to_approve_it(db, operations, pieces, cutter):
    """Making somebody sign off a good garment means nobody records passes."""
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    out = await InspectionService(db).raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="PASS")
    assert out["status"] == "RESOLVED"
    assert out["resolved_at"] is not None


# ═════════════════════════════════════════════ the gate while it is pending
async def test_a_rejected_garment_stops_moving_until_the_dm_decides(
        db, operations, pieces, cutter, stitching_mgr):
    """Otherwise the defect walks on and more work is spent on a piece that is
    going back anyway — and by the time the DM approves, the garment is three
    stages past where it was rejected."""
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    await InspectionService(db).raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="WORKMANSHIP",
        responsible_employee_id=cutter[0].id, responsible_stage="FUSING")

    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert res["logged"] == []
    assert res["rejected_blocked"] == [piece.code]
    reason = next(b["reason"] for b in res["blocked"] if b["gate"] == "rejected")
    assert "waiting for the DM" in reason


async def test_one_rejected_garment_does_not_lose_the_tray(
        db, operations, pieces, cutter, stitching_mgr):
    """Per-piece, like every gate after the role one."""
    bad, good = _piece(pieces, 0), _piece(pieces, 1)
    for p in (bad, good):
        await _walk_to_line_stitching(db, operations, p, cutter[0].id)
    await InspectionService(db).raise_inspection(
        piece_id=bad.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="PRODUCT_DAMAGE")

    res = await ProductionService(db).log_batch(
        user=stitching_mgr, employee_id=cutter[0].id,
        piece_ids=[bad.id, good.id], work_date=TODAY,
        screen=ScreenContext.PIPELINE)
    assert res["rejected_blocked"] == [bad.code]
    assert good.code in res["logged"]


# ═══════════════════════════════════════════════════════ the re-walk
async def test_an_approved_redo_makes_the_garment_re_walk_what_it_invalidated(
        db, operations, pieces, cutter, stitching_mgr, dm):
    """THE CORE OF THE FEATURE.

    Redoing FUSING means the PASTING done on that badly-fused panel is no longer
    true, so the garment must be pasted again and line-stitched again, in order.
    Without this the sequence gate reads "furthest completed plus one" and jumps
    the piece straight back to where it was, with the bad pasting still counted
    as done and the defect sewn in.
    """
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    svc = InspectionService(db)
    row = await svc.raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="WORKMANSHIP",
        responsible_employee_id=cutter[0].id, responsible_stage="FUSING")
    await svc.decide(row["inspection_id"], approve=True, actor_user_id=None)

    walked = []
    for _ in range(12):
        out = await ProductionService(db).log_batch(
            user=dm, employee_id=cutter[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.PIPELINE)
        if not out["logged"]:
            break
        walked.append(out["stage"])

    assert walked[:3] == ["FUSING", "PASTING", "LINE_STITCHING"], (
        f"the redo must re-walk what it invalidated, got {walked}")
    # AND THEN CARRIES ON NORMALLY. A stage the piece had never reached was not
    # invalidated by the redo, so it must not be logged twice on the way out.
    assert walked[3:] == ["SHELL_STITCHING", "FINAL_FINISH",
                          "FINAL_INSPECTION", "PACKAGE_EXPORT"]


async def test_the_rework_cost_is_separable_from_the_original(
        db, operations, pieces, cutter, dm):
    """"This order cost X, of which Y was rework" needs both numbers.

    The second cut of a re-made panel is real leather spent, but it is not what
    the garment was supposed to cost — averaging the two hides how much the floor
    is losing to defects.
    """
    from sqlalchemy import func, select
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    svc = InspectionService(db)
    row = await svc.raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="WORKMANSHIP",
        responsible_employee_id=cutter[0].id, responsible_stage="FUSING")
    await svc.decide(row["inspection_id"], approve=True, actor_user_id=None)
    for _ in range(3):
        await ProductionService(db).log_batch(
            user=dm, employee_id=cutter[0].id, piece_ids=[piece.id],
            work_date=TODAY, screen=ScreenContext.PIPELINE)

    rework = await db.scalar(
        select(func.count()).select_from(ProductionEvent)
        .where(ProductionEvent.piece_id == piece.id,
               ProductionEvent.is_rework.is_(True)))
    assert rework == 3, "FUSING, PASTING and LINE_STITCHING were all done twice"


async def test_an_approved_redo_is_consent_for_one_relog_not_forever(
        db, operations, pieces, cutter, dm):
    """Leaving the permission open would let the same stage be redone endlessly,
    and every one of those would count as rework."""
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    svc = InspectionService(db)
    row = await svc.raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="PRODUCT_DAMAGE")
    await svc.decide(row["inspection_id"], approve=True, actor_user_id=None)

    first = await ProductionService(db).log_batch(
        user=dm, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    second = await ProductionService(db).log_batch(
        user=dm, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert first["stage"] == "FUSING"
    assert second["stage"] == "PASTING", "the permission was spent on the redo"


async def test_a_declined_rejection_leaves_the_garment_where_it_was(
        db, operations, pieces, cutter, dm, stitching_mgr):
    """The DM can disagree, and then the piece carries on as if nothing happened."""
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    svc = InspectionService(db)
    row = await svc.raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="PRODUCT_DAMAGE")
    await svc.decide(row["inspection_id"], approve=False, actor_user_id=None,
                     note="acceptable, ship it")

    out = await ProductionService(db).log_batch(
        user=dm, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert out["stage"] == "SHELL_STITCHING", "it carries on, not back"
    assert out["rejected_blocked"] == []


async def test_a_fix_does_not_move_the_garment(db, operations, pieces, cutter, dm):
    """FIX is a repair in place: no earlier stage is re-opened, so approving one
    settles it outright rather than leaving a redo permission behind."""
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    svc = InspectionService(db)
    row = await svc.raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="FIX", defect_type="WORKMANSHIP",
        responsible_employee_id=cutter[0].id, responsible_stage="LINE_STITCHING")
    done = await svc.decide(row["inspection_id"], approve=True, actor_user_id=None)
    assert done["status"] == "APPROVED"
    assert done["resolved_at"] is not None, "a FIX needs no re-log to close it"

    out = await ProductionService(db).log_batch(
        user=dm, employee_id=cutter[0].id, piece_ids=[piece.id],
        work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert out["stage"] == "SHELL_STITCHING", "the piece never went back"


# ═══════════════════════════════════════════════════════ accountability
async def test_defects_can_be_counted_against_the_worker_answerable(
        db, operations, pieces, cutter, paster):
    """THE POINT OF THE COLUMN. A reason box cannot be counted across a month,
    produced in a wage conversation, or told apart from a bad hide."""
    svc = InspectionService(db)
    for i, (emp, stage) in enumerate([(cutter[0], "FUSING"),
                                      (cutter[0], "FUSING"),
                                      (paster[0], "PASTING")]):
        piece = _piece(pieces, i)
        await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
        await svc.raise_inspection(
            piece_id=piece.id, found_at_stage="LINE_STITCHING",
            verdict="REJECT", action="REDO", return_to_stage=stage,
            defect_type="WORKMANSHIP", responsible_employee_id=emp.id,
            responsible_stage=stage)

    report = {(r["employee"], r["stage"]): r["rejections"]
              for r in await svc.responsibility_report()}
    assert report[(cutter[0].name, "FUSING")] == 2
    assert report[(paster[0].name, "PASTING")] == 1


async def test_product_damage_never_reaches_the_responsibility_report(
        db, operations, pieces, cutter):
    """A supplier's bad hide must not land on a person's record."""
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    svc = InspectionService(db)
    await svc.raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="PRODUCT_DAMAGE",
        reason="flaw in the hide")
    assert await svc.responsibility_report() == []


async def test_one_garment_cannot_carry_two_open_rejections(
        db, operations, pieces, cutter):
    """Two open rejections cannot both be acted on, and the second would hide
    the first from the DM's queue."""
    from fastapi import HTTPException
    piece = _piece(pieces)
    await _walk_to_line_stitching(db, operations, piece, cutter[0].id)
    svc = InspectionService(db)
    await svc.raise_inspection(
        piece_id=piece.id, found_at_stage="LINE_STITCHING", verdict="REJECT",
        action="REDO", return_to_stage="FUSING", defect_type="PRODUCT_DAMAGE")
    with pytest.raises(HTTPException) as exc:
        await svc.raise_inspection(
            piece_id=piece.id, found_at_stage="LINE_STITCHING",
            verdict="REJECT", action="FIX", defect_type="PRODUCT_DAMAGE")
    assert exc.value.status_code == 409
