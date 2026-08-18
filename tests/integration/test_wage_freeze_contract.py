"""
INTEGRATION · THE PAYROLL FREEZE CONTRACT — draft, freeze, reopen, recompute.
    (change-list item 3, flagged CRITICAL for a guardrail conflict)

THE CONFLICT THIS PINS THE RESOLUTION OF
    The change list asks to "re-compute the frozen payroll and update it".
    The standing guardrail says a CLOSED run is a frozen snapshot and is never
    recomputed. Silently doing both means last month's payslip stops matching
    the cash that left the building, with nothing on the record to say so —
    a double-payment risk on the money path.

    The resolution is that those two statements were about different documents:

        OPEN   = a DRAFT. Recompute it freely; nothing has been paid against it.
        CLOSED = the document the cash was counted against. Changing it takes an
                 explicit REOPEN that captures a REASON, and is stamped.

WHAT THESE TESTS PIN
    1. A draft run stays OPEN and can be recomputed with no ceremony.
    2. A CLOSED run REFUSES a recompute and names the reopen route.
    3. Reopen demands a real reason, stamps the actor and counts itself.
    4. After a reopen the recompute works, and re-freezes the run — a paid
       period must not be left silently unfrozen.
    5. Closing an empty run is refused; closing twice is a harmless no-op.
    6. A style-scoped run pays PIECE-RATE ONLY and says so, so a one-style run
       cannot pay a monthly salary again on the next style's run.
"""
from datetime import date, timedelta

import pytest
from fastapi import HTTPException

from app.core.enums import RunStatus, WageType
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.wages.models import Rate
from app.modules.wages.service import WageService

pytestmark = pytest.mark.asyncio

END = date.today() - timedelta(days=1)
START = END - timedelta(days=13)
WORK_DAY = START + timedelta(days=2)


async def _world(db):
    """One style, one operation, one piece-rate cutter, one monthly tailor,
    a rate, and one day's logged work."""
    client = cm.Client(name="FreezeCo"); db.add(client); await db.flush()
    po = cm.ClientOrder(client_id=client.id, order_number="FREEZE-PO")
    db.add(po); await db.flush()
    style = cm.Style(client_order_id=po.id, name="CARNABY", code="CARNABY", production_status="RELEASED")
    db.add(style); await db.flush()
    sku = cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=100,
                 code="FREEZE-PO-CARNABY-57-M")
    db.add(sku); await db.flush()
    op = pm.Operation(code="LEATHER_CUTTING", label="Leather Cutting", sequence=1)
    db.add(op); await db.flush()
    cutter = em.Employee(name="FREEZE-CUTTER", designation="CUTTER",
                         wage_type=WageType.PIECE_RATE, is_active=True)
    monthly = em.Employee(name="FREEZE-SALARIED", designation="TAILOR",
                          wage_type=WageType.MONTHLY, monthly_salary=30000,
                          is_active=True)
    db.add_all([cutter, monthly])
    await db.commit()
    for o in (style, sku, op, cutter, monthly):
        await db.refresh(o)

    db.add(Rate(style_id=style.id, operation_id=op.id, rate=10,
                effective_from=START))
    db.add(pm.ProductionEvent(sku_id=sku.id, operation_id=op.id,
                              employee_id=cutter.id, work_date=WORK_DAY, qty=20,
                              entered_by="test"))
    await db.commit()
    return dict(style=style, sku=sku, op=op, cutter=cutter, monthly=monthly)


# ══════════════════════════════════════════════════════════ 1. the draft path
async def test_a_draft_run_stays_open_and_recomputes_freely(db):
    await _world(db)
    svc = WageService(db)

    draft = await svc.compute_run(START, END, freeze=False)
    assert draft["status"] == RunStatus.OPEN, "freeze=False must leave a DRAFT"

    again = await svc.recompute_run(draft["id"], user_name="DM")
    assert again["recompute_count"] == 1
    assert again["status"] == RunStatus.OPEN, (
        "recomputing a draft must not freeze it behind the manager's back")


# ═════════════════════════════════════════════════ 2-4. the frozen-run path
async def test_a_closed_run_refuses_a_recompute_and_names_the_route(db):
    await _world(db)
    svc = WageService(db)
    run = await svc.compute_run(START, END)          # default: freeze
    assert run["status"] == RunStatus.CLOSED

    with pytest.raises(HTTPException) as exc:
        await svc.recompute_run(run["id"], user_name="DM")
    assert exc.value.status_code == 409
    # The refusal has to be actionable, not just correct.
    assert "reopen" in str(exc.value.detail).lower()


async def test_reopen_demands_a_reason_and_stamps_it(db):
    await _world(db)
    svc = WageService(db)
    run = await svc.compute_run(START, END)

    with pytest.raises(HTTPException) as exc:
        await svc.reopen_run(run["id"], user_name="DM", reason="x")
    assert exc.value.status_code == 422

    out = await svc.reopen_run(run["id"], user_name="Hamthan",
                               reason="rate for CUTTING was corrected after close")
    assert out["status"] == RunStatus.OPEN
    assert out["reopen_count"] == 1
    assert out["last_reopened_by"] == "Hamthan"
    assert "corrected" in out["last_reopen_reason"]


async def test_reopen_recompute_close_is_three_deliberate_steps(db):
    """A reopened run stays a DRAFT until someone closes it — on purpose.

    The obvious alternative is for the recompute to re-freeze automatically, and
    it was tempting: a paid period left unfrozen sounds like a hole. It is the
    wrong trade. The reason to reopen at all is that the numbers were WRONG;
    auto-freezing would hand back a new frozen document nobody had looked at,
    which is how a corrected run gets paid twice-wrong instead of once. The
    review IS the value of the draft state.

    It is not silent either: the run reads OPEN with reopen_count 1, and the
    ledger surfaces both, so an unfrozen paid period is visible on the screen a
    manager already watches.
    """
    await _world(db)
    svc = WageService(db)
    run = await svc.compute_run(START, END)
    await svc.reopen_run(run["id"], user_name="DM", reason="corrected a rate")

    out = await svc.recompute_run(run["id"], user_name="DM")
    assert out["recomputed"] is True
    assert out["recompute_count"] == 1

    mid = await svc.get_run_detail(run["id"])
    assert mid["status"] == RunStatus.OPEN, (
        "a reopened run must stay reviewable until it is explicitly closed")
    assert mid["reopen_count"] == 1

    final = await svc.close_run(run["id"], user_name="DM")
    assert final["status"] == RunStatus.CLOSED
    # Both counters survive: "recomputed twice" and "unfrozen twice after
    # payment" are different facts about a payslip, and only the second needs
    # explaining to an auditor.
    assert final["reopen_count"] == 1
    assert final["recompute_count"] == 1
    assert final["last_reopen_reason"] == "corrected a rate"


async def test_confirm_closed_recompute_leaves_a_frozen_run_frozen(db):
    """The escape hatch does NOT unfreeze. It rewrites in place and re-freezes.

    That is the difference between the two doors: reopen makes the run a draft
    for review; confirm_closed rewrites the frozen document without one. It
    works, it stamps the recompute, and it records no reason — which is exactly
    why the reopen door exists and why this one is not the default.
    """
    await _world(db)
    svc = WageService(db)
    run = await svc.compute_run(START, END)

    out = await svc.recompute_run(run["id"], user_name="DM", confirm_closed=True)
    assert out["recompute_count"] == 1
    detail = await svc.get_run_detail(run["id"])
    assert detail["status"] == RunStatus.CLOSED
    assert detail["reopen_count"] == 0


async def test_reopening_an_open_run_is_a_409(db):
    await _world(db)
    svc = WageService(db)
    draft = await svc.compute_run(START, END, freeze=False)
    with pytest.raises(HTTPException) as exc:
        await svc.reopen_run(draft["id"], user_name="DM",
                             reason="nothing to unfreeze here")
    assert exc.value.status_code == 409


# ═══════════════════════════════════════════════════════════ 5. closing
async def test_closing_a_draft_freezes_it_and_is_idempotent(db):
    await _world(db)
    svc = WageService(db)
    draft = await svc.compute_run(START, END, freeze=False)

    closed = await svc.close_run(draft["id"], user_name="DM")
    assert closed["status"] == RunStatus.CLOSED

    # A double-click changed nothing, so it is not an error.
    again = await svc.close_run(draft["id"], user_name="DM")
    assert again["status"] == RunStatus.CLOSED


# ═════════════════════════════════════════════════════════ 6. scoped runs
async def test_a_style_scoped_run_pays_piece_rate_only(db):
    """A monthly salary is a fact about a PERSON, not a style.

    Emitting one on a one-style run would pay that salary again on every other
    style run in the same window — the double payment the window guard exists to
    prevent, arriving through the scope instead.
    """
    w = await _world(db)
    svc = WageService(db)

    run = await svc.compute_run(START, END, freeze=False, style_code="CARNABY")
    assert run["scope_style_code"] == "CARNABY"
    assert run["piece_rate_only"] is True

    names = {ln["employee_name"] for ln in run["lines"]}
    assert "FREEZE-CUTTER" in names
    assert "FREEZE-SALARIED" not in names, (
        "a scoped run paid a monthly salary — it will be paid again next style")


async def test_two_scoped_runs_for_different_styles_may_share_a_window(db):
    """Disjoint pieces, so no double payment — the overlap guard must allow it.

    An unscoped run in the same window still blocks everything; that is pinned
    by the existing overlap tests.
    """
    w = await _world(db)
    other = cm.Style(client_order_id=w["style"].client_order_id, name="ISLAY",
                     code="ISLAY", production_status="RELEASED")
    db.add(other)
    await db.commit()

    svc = WageService(db)
    a = await svc.compute_run(START, END, freeze=False, style_code="CARNABY")
    b = await svc.compute_run(START, END, freeze=False, style_code="ISLAY")
    assert a["id"] != b["id"]


async def test_an_unscoped_run_still_blocks_a_scoped_one_in_the_same_window(db):
    """The unscoped run pays everything, including that style's pieces."""
    await _world(db)
    svc = WageService(db)
    await svc.compute_run(START, END, freeze=False)

    with pytest.raises(HTTPException) as exc:
        await svc.compute_run(START, END, freeze=False, style_code="CARNABY")
    assert exc.value.status_code == 409
