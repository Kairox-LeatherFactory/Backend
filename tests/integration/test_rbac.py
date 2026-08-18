"""GATE 1 (ROLE): a manager may only log the stages their role owns.

PORTED. This file was written against the pre-barcode API — `ProductionService.cut()`
and `.scan()`, one endpoint per stage. Both were removed when the two-door log
landed; the surviving endpoints are 410 stubs (app/modules/production/router.py:171-176)
and `log_batch` is now the whole logging surface. The assertions below are the
originals; only the call shape changed.

Gate 1 is deliberately WHOLE-REQUEST (service.py:241-243): a role that cannot log
one stage in the batch loses the batch. Gates 2-4 are per-piece. MD and DM bypass
gate 1 entirely (`_STAGE_BYPASS_ROLES`, service.py:59).
"""
import datetime
import uuid

import pytest
from fastapi import HTTPException

from app.core.enums import BarcodeStatus, BarcodeType, UserRole, WageType
from app.core.enums_barcode import ScreenContext
from app.modules.barcode.models import BarcodeRegistry
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.production.service import ProductionService
from app.modules.users.models import User

TODAY = datetime.date.today()


async def _seed_min(db):
    """One SKU, two pieces, a present CUTTER, and the two operations the tests use.

    Stage codes must be the canonical `ProductionStage` values — `log_batch`
    resolves the inferred stage to an Operation by code (service.py:232-239), so
    the old "CUTTING"/"SHELL" labels no longer resolve.
    """
    client = cm.Client(name="C"); db.add(client); await db.flush()
    po = cm.ClientOrder(client_id=client.id, order_number="PO1"); db.add(po); await db.flush()
    style = cm.Style(client_order_id=po.id, name="CARNABY", production_status="RELEASED"); db.add(style); await db.flush()
    sku = cm.SKU(style_id=style.id, color_code="57", size="M", qty_ordered=10,
                 code="PO1-CARNABY-57-M")
    db.add(sku); await db.flush()

    cut = pm.Operation(code="LEATHER_CUTTING", label="Leather Cutting", sequence=1)
    fuse = pm.Operation(code="FUSING", label="Fusing", sequence=2)
    paste = pm.Operation(code="PASTING", label="Pasting", sequence=3)
    db.add_all([cut, fuse, paste]); await db.flush()
    db.add(pm.OperationAccess(role="cutting_manager", operation_id=cut.id))

    # CUTTER also covers FUSING (STAGE_DESIGNATIONS, enums_barcode.py:245), so the
    # skill gate does not interfere with what these tests are asserting.
    emp = em.Employee(name="W", designation="CUTTER", wage_type=WageType.PIECE_RATE,
                      is_active=True)
    db.add(emp); await db.flush()

    # HELPER is in MULTI_STAGE_DESIGNATIONS (enums_barcode.py:255), so gate 2
    # never fires for them. Needed to isolate gate 1: the DM bypass covers the
    # ROLE gate only (service.py:59), NOT the skill gate, so a CUTTER logging
    # PASTING is still skill-blocked no matter who the manager is.
    helper = em.Employee(name="H", designation="HELPER", wage_type=WageType.PIECE_RATE,
                         is_active=True)
    db.add(helper); await db.flush()

    pieces = []
    for seq in (1, 2):
        p = pm.Piece(code=f"PO1-CARNABY-57-M-{seq:03d}", seq=seq, sku_id=sku.id)
        db.add(p); await db.flush()
        db.add(BarcodeRegistry(code=p.code, type=BarcodeType.PIECE.value,
                               status=BarcodeStatus.ACTIVE.value, piece_id=p.id,
                               caption=p.code))
        pieces.append(p)

    # the presence gate (service.py:176-182) runs for a work_date of today
    from app.modules.attendance.models import AttendanceLog, AttendanceSource
    from app.modules.attendance.service import AttendanceService
    wd = await AttendanceService(db)._local_today()
    for e in (emp, helper):
        db.add(AttendanceLog(
            employee_id=e.id, work_date=wd,
            check_in_at=datetime.datetime.now(datetime.timezone.utc),
            source=AttendanceSource.SELF))

    await db.commit()
    return sku, cut, fuse, paste, emp, helper, pieces


def _user(role, name):
    return User(id=uuid.uuid4(), name=name, phone=str(uuid.uuid4())[:8], role=role,
                password_hash="x", is_active=True)


async def _complete(db, piece, op, emp):
    """Log a completed event so the piece's inferred next stage advances."""
    db.add(pm.ProductionEvent(sku_id=piece.sku_id, operation_id=op.id,
                              employee_id=emp.id, work_date=TODAY, qty=1,
                              entered_by="test", piece_id=piece.id))
    await db.commit()


@pytest.mark.asyncio
async def test_cutting_manager_cannot_log_stitching(db, leather_lot):
    sku, cut, fuse, paste, emp, helper, pieces = await _seed_min(db)
    ps = ProductionService(db)
    cutting_mgr = _user(UserRole.CUTTING_MANAGER, "C")

    # ALLOWED: a fresh piece infers LEATHER_CUTTING, which this role owns.
    res = await ps.log_batch(
        user=cutting_mgr, employee_id=emp.id, piece_ids=[pieces[0].id],
        work_date=TODAY, screen=ScreenContext.LEATHER_CUT,
        consumption_qty=10, leather_lot_id=leather_lot.id)
    assert res["count_logged"] == 1

    # FORBIDDEN: advance a piece to PASTING, which belongs to STITCHING_MANAGER
    # (enums_barcode.py:168). The role gate fires before anything is written.
    await _complete(db, pieces[1], cut, emp)
    await _complete(db, pieces[1], fuse, emp)

    with pytest.raises(HTTPException) as exc:
        await ps.log_batch(
            user=cutting_mgr, employee_id=emp.id, piece_ids=[pieces[1].id],
            work_date=TODAY, screen=ScreenContext.PIPELINE)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_direct_manager_bypasses_access(db):
    sku, cut, fuse, paste, emp, helper, pieces = await _seed_min(db)
    ps = ProductionService(db)
    direct = _user(UserRole.DIRECT_MANAGER, "D")

    # PASTING has no OperationAccess row and is not in DIRECT_MANAGER's
    # STAGE_ROLE_ACCESS set — the DM bypass (service.py:59) is the only reason
    # this succeeds, which is exactly what the test is pinning.
    for p in pieces:
        await _complete(db, p, cut, emp)
        await _complete(db, p, fuse, emp)

    res = await ps.log_batch(
        user=direct, employee_id=helper.id, piece_ids=[p.id for p in pieces],
        work_date=TODAY, screen=ScreenContext.PIPELINE)

    assert res["count_logged"] == 2
    assert not res["sequence_blocked"]
