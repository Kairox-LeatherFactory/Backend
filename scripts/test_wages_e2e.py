"""
================================================================================
scripts/test_wages_e2e.py — End-to-end verification harness for the WAGES module
================================================================================
WHAT THIS IS
    A standalone async harness that drives the REAL WageService + WageRepository
    (plus the clients / employees / production services they compose) against a
    fresh in-memory SQLite database. No Postgres, no Docker, no HTTP — it exercises
    the exact code paths the /api/v1/wages routes call, one layer below FastAPI.

WHY IT EXISTS
    tests/test_wages.py is stale: it calls the old set_rate(style_id, op_id, rate,
    date) arity, reads run.lines (compute_run now returns a summary dict), and calls
    ProductionService.log_event(), which is commented out at
    app/modules/production/service.py:216. So the module had no runnable proof it
    works. This is that proof.

WHAT IT DOES NOT TOUCH
    Nothing under app/modules/wages/ is imported for mutation — the service is
    tested as-is. Production rows are inserted directly via the session because the
    old qty-based log_event entry point is gone; a test harness may write the DB
    directly, that is not a layering claim about the app.

RUN
    python -m scripts.test_wages_e2e
    Exit code 0 = every scenario passed; non-zero = at least one failed.

Each scenario maps to a documented "command"/contract in one wages file. The detail
string each returns is the actual number observed, so the report quotes real output.
================================================================================
"""
import asyncio
import os
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import date

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.core.database as dbmod
from app.core.database import Base
from app.core.enums import RunStatus, WageType

# Import every module's models so Base.metadata is complete (client_order FKs to
# document, so the procurement/bom/inventory/supplier_po tables must exist too).
from app.modules.users import models as _u  # noqa: F401
from app.modules.clients import models as cm  # noqa: F401
from app.modules.employees import models as em  # noqa: F401
from app.modules.production import models as pm  # noqa: F401
from app.modules.wages import models as wm  # noqa: F401
from app.modules.attendance import models as _a  # noqa: F401
from app.core import models as _core_models  # noqa: F401
from app.modules.procurement import models as _pr  # noqa: F401
from app.modules.bom import models as _bom  # noqa: F401
from app.modules.inventory import models as _inv  # noqa: F401
from app.modules.supplier_po import models as _spo  # noqa: F401

from app.modules.wages import proration
from app.modules.wages import schemas as ws
from app.modules.wages.service import WageService


# ── infrastructure ────────────────────────────────────────────────────────────
@asynccontextmanager
async def fresh_session():
    """A brand-new in-memory SQLite DB with the full schema, per scenario.

    Fresh per scenario so a CLOSED run in one test can't leak into another's
    overlap check.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod.async_engine = engine
    dbmod.AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session = dbmod.AsyncSessionLocal()
    try:
        yield session
    finally:
        await session.close()
        await engine.dispose()


async def seed(db):
    """Minimal factory: CARNABY style (152 pieces across 2 SKUs), CUTTING+PASTING
    operations, one PIECE_RATE cutter and one MONTHLY tailor (salary 18000)."""
    client = cm.Client(name="Beau Geste")
    db.add(client)
    await db.flush()
    po = cm.ClientOrder(client_id=client.id, order_number="PO-1579")
    db.add(po)
    await db.flush()
    # Style.code MUST be set — every wage code-resolution path filters on it.
    style = cm.Style(client_order_id=po.id, name="CARNABY", code="CARNABY", article="ART-1")
    db.add(style)
    await db.flush()
    skus = [
        cm.SKU(style_id=style.id, color_code="57", size="S", qty_ordered=100),
        cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=52),
    ]
    db.add_all(skus)
    await db.flush()
    ops = {}
    for code, seq in [("CUTTING", 1), ("PASTING", 3)]:
        o = pm.Operation(code=code, label=code, sequence=seq)
        db.add(o)
        await db.flush()
        ops[code] = o
    cutter = em.Employee(name="Cutter1", designation="CUTTER", wage_type=WageType.PIECE_RATE)
    monthly = em.Employee(
        name="Monthly1", designation="TAILOR", wage_type=WageType.MONTHLY, monthly_salary=18000
    )
    db.add_all([cutter, monthly])
    await db.commit()
    return {"style": style, "skus": skus, "ops": ops, "cutter": cutter, "monthly": monthly}


async def add_event(db, sku_id, op_id, emp_id, work_date, qty):
    """Insert a production_event directly (log_event is commented out)."""
    db.add(
        pm.ProductionEvent(
            sku_id=sku_id, operation_id=op_id, employee_id=emp_id,
            work_date=work_date, qty=qty,
        )
    )
    await db.commit()


async def expect_http(coro, code):
    """Assert an awaited call raises HTTPException with the given status code."""
    try:
        await coro
    except HTTPException as e:
        assert e.status_code == code, f"expected {code}, got {e.status_code}"
        return e.status_code
    raise AssertionError(f"expected HTTPException {code}, none raised")


# ── scenarios ─────────────────────────────────────────────────────────────────
async def s1_proration():
    """proration.py — full month == salary; consecutive splits tile to salary;
    Feb pays a full month; inverted window == 0.0."""
    full = proration.prorate_monthly(18000, date(2026, 4, 1), date(2026, 4, 30))
    a = proration.prorate_monthly(18000, date(2026, 4, 1), date(2026, 4, 15))
    b = proration.prorate_monthly(18000, date(2026, 4, 16), date(2026, 4, 30))
    feb = proration.prorate_monthly(18000, date(2026, 2, 1), date(2026, 2, 28))
    inv = proration.prorate_monthly(18000, date(2026, 4, 30), date(2026, 4, 1))
    assert full == 18000, full
    assert round(a + b, 2) == 18000, (a, b)
    assert feb == 18000, feb
    assert inv == 0.0, inv
    return f"full={full}; split {a}+{b}={round(a + b, 2)}; feb={feb}; inverted={inv}"


async def s2_set_rate_and_sheet():
    """service.set_rate (code-in) + repository.upsert_rate + service.rate_sheet.
    Lower-case codes must resolve; PASTING stays unpriced; missing_rate_count==1."""
    async with fresh_session() as db:
        await seed(db)
        svc = WageService(db)
        await svc.set_rate(
            ws.RateSet(style_code="carnaby", operation_code="cutting", rate=80,
                       effective_from=date(2026, 3, 1))
        )
        sheet = await svc.rate_sheet("CARNABY", date(2026, 3, 15))
        by = {r["operation_code"]: r["rate"] for r in sheet["operations"]}
        assert by["CUTTING"] == 80.0, by
        assert by["PASTING"] is None, by
        assert sheet["missing_rate_count"] == 1, sheet["missing_rate_count"]
        return f"CUTTING={by['CUTTING']} read back via code; PASTING unpriced; missing_rate_count={sheet['missing_rate_count']}"


async def s3_bulk_and_duplicate():
    """service.set_rates_bulk saves a whole sheet in one call; the schema validator
    rejects a duplicate operation_code before any write."""
    async with fresh_session() as db:
        await seed(db)
        svc = WageService(db)
        res = await svc.set_rates_bulk(
            ws.RateBulkSet(
                style_code="CARNABY", effective_from=date(2026, 3, 1),
                lines=[ws.RateSetLine(operation_code="CUTTING", rate=80),
                       ws.RateSetLine(operation_code="PASTING", rate=12.5)],
            )
        )
        assert res["saved"] == 2, res
        rejected = False
        try:
            ws.RateBulkSet(
                style_code="CARNABY", effective_from=date(2026, 3, 1),
                lines=[ws.RateSetLine(operation_code="CUTTING", rate=80),
                       ws.RateSetLine(operation_code="cutting", rate=90)],
            )
        except Exception:
            rejected = True
        assert rejected, "duplicate operation_code was not rejected"
        return f"bulk saved={res['saved']}; duplicate-op rejected={rejected}"


async def s4_effective_dating():
    """repository.effective_rate — a mid-period rate change prices each day at the
    rate in force on THAT day (10@80 before, 10@100 after = 1800)."""
    async with fresh_session() as db:
        s = await seed(db)
        svc = WageService(db)
        await svc.set_rate(ws.RateSet(style_code="CARNABY", operation_code="CUTTING",
                                      rate=80, effective_from=date(2026, 3, 1)))
        await svc.set_rate(ws.RateSet(style_code="CARNABY", operation_code="CUTTING",
                                      rate=100, effective_from=date(2026, 3, 20)))
        sku, cut, cutter = s["skus"][0], s["ops"]["CUTTING"], s["cutter"]
        await add_event(db, sku.id, cut.id, cutter.id, date(2026, 3, 10), 10)  # @80
        await add_event(db, sku.id, cut.id, cutter.id, date(2026, 3, 25), 10)  # @100
        run = await svc.compute_run(date(2026, 3, 1), date(2026, 3, 31))
        detail = await svc.get_run_detail(run["id"])
        # Assert the CUTTER's line specifically — the run total also carries the
        # monthly tailor's 18000, which this scenario is not about.
        cutter_amt = next(ln["amount"] for ln in detail["lines"] if ln["employee_name"] == "Cutter1")
        assert cutter_amt == 10 * 80 + 10 * 100, cutter_amt
        return f"Cutter1 10@80 + 10@100 = {cutter_amt} (expected 1800); run_total incl. monthly = {run['total_amount']}"


async def s5_compute_matches_card():
    """service.compute_run — piece-rate Cutter1 = 152 x 80 = 12160; monthly tailor
    paid salary independent of production; summary + frozen detail agree."""
    async with fresh_session() as db:
        s = await seed(db)
        svc = WageService(db)
        await svc.set_rate(ws.RateSet(style_code="CARNABY", operation_code="CUTTING",
                                      rate=80, effective_from=date(2026, 3, 1)))
        cut, cutter = s["ops"]["CUTTING"], s["cutter"]
        for sku in s["skus"]:
            await add_event(db, sku.id, cut.id, cutter.id, date(2026, 3, 23), sku.qty_ordered)
        run = await svc.compute_run(date(2026, 3, 1), date(2026, 3, 31))
        detail = await svc.get_run_detail(run["id"])
        amt = {ln["employee_name"]: ln["amount"] for ln in detail["lines"]}
        pcs = {ln["employee_name"]: ln["pieces"] for ln in detail["lines"]}
        assert amt["Cutter1"] == 152 * 80, amt
        assert pcs["Cutter1"] == 152, pcs
        assert amt["Monthly1"] == 18000, amt
        assert pcs["Monthly1"] == 0, pcs
        assert run["total_amount"] == 152 * 80 + 18000, run["total_amount"]
        return (f"Cutter1={amt['Cutter1']} (152x80), pieces={pcs['Cutter1']}; "
                f"Monthly1={amt['Monthly1']} pieces={pcs['Monthly1']}; run_total={run['total_amount']}")


async def s6_no_double_pay():
    """THE core guard — a MONTHLY tailor who logs 400 pieces gets exactly ONE
    monthly line (amount 18000, pieces 0), never a piece-rate line on top."""
    async with fresh_session() as db:
        s = await seed(db)
        svc = WageService(db)
        await svc.set_rate(ws.RateSet(style_code="CARNABY", operation_code="CUTTING",
                                      rate=80, effective_from=date(2026, 3, 1)))
        sku, cut, monthly = s["skus"][0], s["ops"]["CUTTING"], s["monthly"]
        await add_event(db, sku.id, cut.id, monthly.id, date(2026, 3, 15), 400)
        run = await svc.compute_run(date(2026, 3, 1), date(2026, 3, 31))
        detail = await svc.get_run_detail(run["id"])
        m_lines = [ln for ln in detail["lines"] if ln["employee_name"] == "Monthly1"]
        assert len(m_lines) == 1, m_lines
        assert m_lines[0]["wage_type"] == "monthly", m_lines[0]
        assert m_lines[0]["amount"] == 18000, m_lines[0]
        assert m_lines[0]["pieces"] == 0, m_lines[0]
        return (f"Monthly1 logged 400 pcs -> {len(m_lines)} line, "
                f"wage_type={m_lines[0]['wage_type']}, amount={m_lines[0]['amount']}, pieces={m_lines[0]['pieces']}")


async def s7_unrated_surfaces():
    """service.compute_run — real work on an UNRATED operation is not silently
    zeroed: it rides out in unrated_operations with codes, not UUIDs."""
    async with fresh_session() as db:
        s = await seed(db)
        svc = WageService(db)
        await svc.set_rate(ws.RateSet(style_code="CARNABY", operation_code="CUTTING",
                                      rate=80, effective_from=date(2026, 3, 1)))
        sku, cut, paste, cutter = s["skus"][0], s["ops"]["CUTTING"], s["ops"]["PASTING"], s["cutter"]
        await add_event(db, sku.id, cut.id, cutter.id, date(2026, 3, 10), 50)     # paid
        await add_event(db, sku.id, paste.id, cutter.id, date(2026, 3, 11), 55)   # unrated
        run = await svc.compute_run(date(2026, 3, 1), date(2026, 3, 31))
        detail = await svc.get_run_detail(run["id"])
        # Only the 50 rated CUTTING pieces are paid to Cutter1; the 55 PASTING
        # pieces price to nothing and must appear in unrated_operations instead.
        cutter_amt = next(ln["amount"] for ln in detail["lines"] if ln["employee_name"] == "Cutter1")
        assert cutter_amt == 50 * 80, cutter_amt
        u = run["unrated_operations"]
        assert len(u) == 1, u
        assert u[0]["operation_code"] == "PASTING", u
        assert u[0]["unpaid_pieces"] == 55, u
        assert u[0]["style_code"] == "CARNABY", u
        return (f"Cutter1 paid={cutter_amt} (50x80); unrated {u[0]['style_code']}/"
                f"{u[0]['operation_code']} unpaid_pieces={u[0]['unpaid_pieces']}; "
                f"run_total incl. monthly = {run['total_amount']}")


async def s8_window_guards():
    """service._validate_window — inverted window -> 422, future window -> 422,
    a window overlapping a CLOSED run -> 409 (would pay the same days twice)."""
    async with fresh_session() as db:
        await seed(db)
        svc = WageService(db)
        inv = await expect_http(svc.compute_run(date(2026, 3, 31), date(2026, 3, 1)), 422)
        fut = await expect_http(svc.compute_run(date(2030, 1, 1), date(2030, 1, 31)), 422)
        await svc.compute_run(date(2026, 3, 1), date(2026, 3, 31))  # closes a run
        ov = await expect_http(svc.compute_run(date(2026, 3, 15), date(2026, 4, 15)), 409)
        return f"inverted->{inv}; future->{fut}; overlap-closed->{ov}"


async def s9_freeze_and_stored_type():
    """models/service — a CLOSED run re-reads identical from get_run_detail; the
    wage_type column is a frozen plain string ('piece_rate' / 'monthly')."""
    async with fresh_session() as db:
        s = await seed(db)
        svc = WageService(db)
        await svc.set_rate(ws.RateSet(style_code="CARNABY", operation_code="CUTTING",
                                      rate=80, effective_from=date(2026, 3, 1)))
        cut, cutter = s["ops"]["CUTTING"], s["cutter"]
        for sku in s["skus"]:
            await add_event(db, sku.id, cut.id, cutter.id, date(2026, 3, 23), sku.qty_ordered)
        run = await svc.compute_run(date(2026, 3, 1), date(2026, 3, 31))
        detail = await svc.get_run_detail(run["id"])
        assert detail["status"] == RunStatus.CLOSED, detail["status"]
        assert detail["total_amount"] == run["total_amount"], (detail["total_amount"], run["total_amount"])
        stored = set((await db.execute(select(wm.WageLine.wage_type))).scalars().all())
        assert stored == {"piece_rate", "monthly"}, stored
        return f"status={detail['status'].value}; total re-read={detail['total_amount']}; stored wage_types={sorted(stored)}"


async def s10_list_runs():
    """repository.list_runs — SQL-aggregated summary matches the computed run."""
    async with fresh_session() as db:
        s = await seed(db)
        svc = WageService(db)
        await svc.set_rate(ws.RateSet(style_code="CARNABY", operation_code="CUTTING",
                                      rate=80, effective_from=date(2026, 3, 1)))
        cut, cutter = s["ops"]["CUTTING"], s["cutter"]
        for sku in s["skus"]:
            await add_event(db, sku.id, cut.id, cutter.id, date(2026, 3, 23), sku.qty_ordered)
        run = await svc.compute_run(date(2026, 3, 1), date(2026, 3, 31))
        runs = await svc.list_runs()
        assert len(runs) == 1, len(runs)
        assert runs[0]["total_amount"] == run["total_amount"], (runs[0]["total_amount"], run["total_amount"])
        assert runs[0]["total_pieces"] == run["total_pieces"], (runs[0]["total_pieces"], run["total_pieces"])
        assert runs[0]["status"] == RunStatus.CLOSED, runs[0]["status"]
        return (f"1 run listed; total_amount={runs[0]['total_amount']} matches compute; "
                f"total_pieces={runs[0]['total_pieces']}")


SCENARIOS = [
    ("S1  proration.prorate_monthly", s1_proration),
    ("S2  set_rate + rate_sheet (code-in)", s2_set_rate_and_sheet),
    ("S3  set_rates_bulk + duplicate guard", s3_bulk_and_duplicate),
    ("S4  effective-dated rate (mid-period change)", s4_effective_dating),
    ("S5  compute_run piece-rate + monthly", s5_compute_matches_card),
    ("S6  monthly logger not double-paid (THE guard)", s6_no_double_pay),
    ("S7  unrated operation surfaces", s7_unrated_surfaces),
    ("S8  window guards 422/422/409", s8_window_guards),
    ("S9  freeze + stored wage_type", s9_freeze_and_stored_type),
    ("S10 list_runs aggregation", s10_list_runs),
]


async def main():
    print("=" * 78)
    print("WAGES MODULE — end-to-end harness (in-memory SQLite, real service)")
    print("=" * 78)
    results = []
    for name, fn in SCENARIOS:
        try:
            detail = await fn()
            results.append((name, True, detail or ""))
            print(f"[PASS] {name}\n       {detail}")
        except Exception as e:  # noqa: BLE001 — harness reports, never crashes silently
            results.append((name, False, f"{type(e).__name__}: {e}"))
            print(f"[FAIL] {name}\n       {type(e).__name__}: {e}")
            traceback.print_exc()
    passed = sum(1 for _, ok, _ in results if ok)
    total = len(results)
    print("-" * 78)
    print(f"RESULT: {passed}/{total} scenarios passed")
    print("=" * 78)
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
