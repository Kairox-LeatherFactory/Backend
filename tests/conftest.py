"""
================================================================================
tests/conftest.py — shared async test harness
================================================================================
Layer strategy (per the project testing standard):
  • unit         — pure predicates, no DB (tests/unit)
  • integration  — real DB round-trips on SQLite+aiosqlite (tests/integration)
  • functional   — one business capability end-to-end through services
  • system/e2e   — router → service → repo → DB via httpx AsyncClient
  • uat          — scripted business scenarios as executable checklists

Money/data-integrity paths get the deepest coverage: the merge gate, cutting
consumption + stock decrement, skill/sequence gates, and barcode retire-without-
history-loss.

DB: in-memory SQLite via aiosqlite. GUID() degrades to CHAR(32) off Postgres,
JSON_VARIANT to plain JSON — both already handled by core/models, so the schema
creates cleanly. Anything Postgres-only (a real ON CONFLICT, JSONB operators) is
NOT used by the feature, so SQLite is a faithful stand-in for these tests.

RUN:
    pip install pytest pytest-asyncio aiosqlite httpx
    pytest tests/ -v
    pytest tests/unit -v            # just the fast layer
    pytest -k merge_gate -v         # a single concern across layers
"""
import asyncio
import uuid
from datetime import date, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

# Import Base + every model module so metadata is complete before create_all.
from app.core.database import Base
import app.core.models                      # noqa: F401  Document / Notification / AuditLog
import app.modules.clients.models           # noqa: F401
import app.modules.employees.models         # noqa: F401
import app.modules.users.models             # noqa: F401
import app.modules.production.models        # noqa: F401
import app.modules.barcode.models           # noqa: F401  (drawer/material/supplier too)
import app.modules.attendance.models        # noqa: F401
import app.modules.wages.models             # noqa: F401

# ── OUT-OF-SCOPE imports, required only to build the schema ──────────────────
# AUDIT F145 (docs/audit/pass-05-data-integrity.md): app/core/models.py:113-115
# declares document.submission_id -> submission.id, and :141-143 -> supplier.id.
# Those tables live in `procurement` / `supplier_po`, which are OUT of the audit
# scope. Without them Base.metadata.create_all raises:
#     NoReferencedTableError: Foreign key associated with column
#     'document.submission_id' could not find table 'submission'
# So the Phase-1 schema cannot be built alone. These imports are the workaround,
# NOT the fix — the fix is use_alter=True on those two FKs (see the finding).
# Nothing in tests/ exercises these modules; they are metadata only.
import app.modules.procurement.models       # noqa: F401
import app.modules.supplier_po.models       # noqa: F401
import app.modules.bom.models               # noqa: F401
import app.modules.inventory.models         # noqa: F401

from app.core.enums import (
    BarcodeStatus, BarcodeType, DrawerState, ProductionStage, UserRole, WageType,
)
from app.modules.barcode.models import BarcodeRegistry, Drawer, MaterialLot, MaterialSupplier
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.employees.models import Employee
from app.modules.production.models import Operation, OperationAccess, Piece


# ── engine / session ─────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db(engine) -> AsyncSession:
    Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with Session() as session:
        yield session


# ── canonical operations (the pipeline) ──────────────────────────────────────
STAGE_SEQ = {
    "LEATHER_CUTTING": 1, "LINING_CUTTING": 1, "FUSING": 2, "PASTING": 3,
    "LINE_STITCHING": 4, "SHELL_STITCHING": 5, "FINAL_FINISH": 6,
    "FINAL_INSPECTION": 7, "PACKAGE_EXPORT": 8,
}


@pytest_asyncio.fixture
async def operations(db) -> dict[str, Operation]:
    ops = {}
    for code, seq in STAGE_SEQ.items():
        op = Operation(code=code, label=code.title(), sequence=seq, is_active=True)
        db.add(op)
        ops[code] = op
    await db.commit()
    for op in ops.values():
        await db.refresh(op)
    return ops


# ── a client → order → style → sku, ready to mint pieces against ─────────────
@pytest_asyncio.fixture
async def order_tree(db):
    client = Client(name="John Peter", country="IT")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number="JP-PO")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name="CLERMONT", article="CL1")
    db.add(style)
    await db.flush()
    sku = SKU(style_id=style.id, color_code="PINE", color_name="PINE GREEN",
              size="M", qty_ordered=5, code="JP-CLERMONT-PINE-M")
    db.add(sku)
    await db.commit()
    for obj in (client, order, style, sku):
        await db.refresh(obj)
    return {"client": client, "order": order, "style": style, "sku": sku}


# ── employees with designations + barcodes ───────────────────────────────────
async def _mark_present(db, employee_id):
    """Write today's attendance row so the production presence gate passes.

    `ProductionService._assert_present` (app/modules/production/service.py:176-182)
    refuses to log output for an employee with no attendance row *today*. That is
    correct behaviour, so a worker fixture that is about to be used on the floor
    has to be clocked in — otherwise every production test 400s.

    work_date comes from `AttendanceService._local_today()` (service.py:103-108),
    the factory-local day, NOT `date.today()`. Using the service's own helper is
    what keeps this row findable by `is_present_today` (service.py:332-335).
    """
    from datetime import datetime, timezone
    from app.modules.attendance.models import AttendanceLog, AttendanceSource
    from app.modules.attendance.service import AttendanceService

    work_date = await AttendanceService(db)._local_today()
    db.add(AttendanceLog(
        employee_id=employee_id, work_date=work_date,
        check_in_at=datetime.now(timezone.utc), source=AttendanceSource.SELF))


async def _make_employee(db, name, designation, wage_type=WageType.PIECE_RATE,
                         monthly_salary=0, present=True):
    emp = Employee(name=name, designation=designation, wage_type=wage_type,
                   monthly_salary=monthly_salary, is_active=True)
    db.add(emp)
    await db.flush()
    bc = BarcodeRegistry(
        code=f"EMP-{str(emp.id)[:6].upper()}", type=BarcodeType.EMPLOYEE.value,
        status=BarcodeStatus.ACTIVE.value, employee_id=emp.id, caption=name)
    db.add(bc)
    if present:
        await _mark_present(db, emp.id)
    await db.commit()
    await db.refresh(emp)
    await db.refresh(bc)
    return emp, bc


@pytest_asyncio.fixture
async def mark_present(db):
    """Factory for tests that create their own employees: `await mark_present(id)`."""
    async def _f(employee_id):
        await _mark_present(db, employee_id)
        await db.commit()
    return _f


@pytest_asyncio.fixture
async def absent_worker(db):
    """A worker who has NOT clocked in — for asserting the presence gate fires."""
    return await _make_employee(db, "ABSENTEE", "CUTTER", present=False)


@pytest_asyncio.fixture
async def cutter(db):
    return await _make_employee(db, "RAMESH", "CUTTER")


@pytest_asyncio.fixture
async def lining_cutter(db):
    return await _make_employee(db, "LINA", "LINING_CUTTER")


@pytest_asyncio.fixture
async def paster(db):
    return await _make_employee(db, "PADMA", "PASTER")


@pytest_asyncio.fixture
async def tailor(db):
    return await _make_employee(db, "TARA", "TAILOR")


# ── fake users (the actor of a request) ──────────────────────────────────────
class FakeUser:
    def __init__(self, role, name="Mgr", employee_id=None):
        self.id = uuid.uuid4()
        self.role = role
        self.name = name
        self.employee_id = employee_id
        self.client_id = None


@pytest.fixture
def md():
    return FakeUser(UserRole.MANAGING_DIRECTOR, "MD")


@pytest.fixture
def dm():
    return FakeUser(UserRole.DIRECT_MANAGER, "DM")


@pytest.fixture
def cutting_mgr():
    return FakeUser(UserRole.CUTTING_MANAGER, "CutMgr")


@pytest.fixture
def lining_mgr():
    return FakeUser(UserRole.LINING_MANAGER, "LinMgr")


@pytest.fixture
def stitching_mgr():
    return FakeUser(UserRole.STITCHING_MANAGER, "StitchMgr")


# ── helper: mint pieces the way premint does (for tests that need pieces) ────
@pytest_asyncio.fixture
async def pieces(db, order_tree):
    """5 pieces of the SKU, each merged to a drawer, needs_lining=True — the
    state after breakdown upload, before any cutting."""
    sku = order_tree["sku"]
    out = []
    for seq in range(1, 6):
        drawer = Drawer(code=f"DRW-{seq:04d}", seq=seq, state=DrawerState.MERGED.value)
        db.add(drawer)
        await db.flush()
        p = Piece(code=f"JP-CLERMONT-PINE-M-{seq:03d}", seq=seq, sku_id=sku.id,
                  current_operation_id=None)
        if hasattr(p, "needs_lining"):
            p.needs_lining = True
        if hasattr(p, "drawer_id"):
            p.drawer_id = drawer.id
        db.add(p)
        await db.flush()
        drawer.current_piece_id = p.id
        db.add(BarcodeRegistry(code=p.code, type=BarcodeType.PIECE.value,
                               status=BarcodeStatus.ACTIVE.value, piece_id=p.id,
                               caption=p.code))
        db.add(BarcodeRegistry(code=drawer.code, type=BarcodeType.DRAWER.value,
                               status=BarcodeStatus.ACTIVE.value, drawer_id=drawer.id,
                               caption=drawer.code))
        out.append((p, drawer))
    await db.commit()
    for p, d in out:
        await db.refresh(p)
        await db.refresh(d)
    return out


@pytest_asyncio.fixture
async def leather_lot(db):
    lot = MaterialLot(category="LEATHER", article="SUEDE-A32", colour="PINE GREEN",
                      thickness="1.2mm", uom="dcm", on_hand=1000, is_active=True)
    db.add(lot)
    await db.flush()
    db.add(BarcodeRegistry(code="LOT-LEA-000001", type=BarcodeType.LEATHER_LOT.value,
                           status=BarcodeStatus.ACTIVE.value, material_lot_id=lot.id,
                           caption="SUEDE-A32"))
    await db.commit()
    await db.refresh(lot)
    return lot


@pytest.fixture
def today():
    return date.today()


@pytest.fixture
def last_week():
    return date.today() - timedelta(days=7)


# ==============================================================================
# COMPATIBILITY FIXTURES (added for the audit-fix regression suites)
# ------------------------------------------------------------------------------
# The regression test files added during the fix pass (test_wages_money_path.py,
# test_materials_fixes.py, test_production_fixes.py, test_attendance_fixes.py,
# etc.) refer to `db_session` and `seed_min`. Rather than rename every test or
# disturb the fixtures above, these two thin fixtures bridge the naming. They are
# purely additive — no existing fixture or test is affected.
# ==============================================================================

@pytest_asyncio.fixture
async def db_session(db) -> AsyncSession:
    """Alias for `db`. The audit-fix suites were written against `db_session`;
    the original harness names the session `db`. Same object, both names work."""
    return db


@pytest_asyncio.fixture
async def seed_min(db, operations):
    """Minimal seed used by a couple of structural regression tests (F05, F34).

    Guarantees the canonical operation vocabulary exists (via `operations`) plus
    a single client→order→style→sku and one active employee, so a test that needs
    'a database that resembles production' has one. Returns a dict of handles.

    Deliberately small: the tests that request it are structural (they assert a
    fix is present) and only need the fixture to resolve, not a full factory.
    """
    client = Client(name="Seed Client", country="IT")
    db.add(client)
    await db.flush()
    order = ClientOrder(client_id=client.id, order_number="SEED-PO")
    db.add(order)
    await db.flush()
    style = Style(client_order_id=order.id, name="SEEDSTYLE", article="SS1")
    db.add(style)
    await db.flush()
    sku = SKU(style_id=style.id, color_code="BLK", color_name="BLACK",
              size="M", qty_ordered=1, code="SEED-PO-SEEDSTYLE-BLK-M")
    db.add(sku)
    emp = Employee(name="SEEDWORKER", designation="CUTTER",
                   wage_type=WageType.PIECE_RATE, monthly_salary=0, is_active=True)
    db.add(emp)
    await db.commit()
    for obj in (client, order, style, sku, emp):
        await db.refresh(obj)
    return {
        "client": client, "order": order, "style": style, "sku": sku,
        "employee": emp, "operations": operations,
    }