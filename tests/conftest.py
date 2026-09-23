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
    StoreState,
    BarcodeStatus, BarcodeType, ProductionStage, UserRole, WageType,
)
from app.modules.barcode.models import BarcodeRegistry, MaterialLot, MaterialSupplier
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
    style = Style(client_order_id=order.id, name="CLERMONT", article="CL1", production_status="RELEASED")
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


# ── put a piece in the state the STORE actually accepts ──────────────────────
# The store is the merge point of the two cut paths, and StoreService now
# enforces that at the WRITE (core/store_display.STORE_ENTRY_STAGE):
#
#     leather may enter a drawer only after PASTING
#     lining  may enter a drawer only after LINING_CUTTING
#
# The `pieces` fixture deliberately hands back "the state after breakdown
# upload, before any cutting", so every test that scans into a drawer has to
# walk the piece up to its hand-off first. This does that with raw event rows
# rather than through ProductionService, because these tests are exercising
# DRAWER behaviour — routing them through the production gates would make a
# drawer test fail for a skill/role/attendance reason that has nothing to do
# with what it is asserting.
async def _log_stage(db, operations, piece, employee_id, code):
    from app.modules.production.models import ProductionEvent
    db.add(ProductionEvent(
        sku_id=piece.sku_id, operation_id=operations[code].id,
        employee_id=employee_id, work_date=date.today(), qty=1,
        piece_id=piece.id, entered_by="test"))


async def _ready_for_store(db, operations, piece, employee_id, *,
                           leather=True, lining=True):
    """Log the cut-side stages that let `piece` be scanned into the store.

    leather=True  → LEATHER_CUTTING, FUSING, PASTING (the whole leather side, so
                    the piece's history is realistic and not just the one stage
                    the gate happens to read)
    lining=True   → LINING_CUTTING
    """
    if leather:
        for code in ("LEATHER_CUTTING", "FUSING", "PASTING"):
            await _log_stage(db, operations, piece, employee_id, code)
    if lining:
        await _log_stage(db, operations, piece, employee_id, "LINING_CUTTING")
    await db.commit()


@pytest_asyncio.fixture
async def ready_for_store(db, operations, cutter):
    """`await ready_for_store(piece)` → that piece may now be stored.

    Pass leather=False / lining=False to leave one side unfinished, which is how
    a test asserts the store-entry gate actually fires.
    """
    async def _f(piece, *, leather=True, lining=True):
        await _ready_for_store(db, operations, piece, cutter[0].id,
                               leather=leather, lining=lining)
    return _f


@pytest_asyncio.fixture
async def in_store(db, operations, cutter):
    """`await in_store(piece)` → both parts scanned in, garment complete.

    The store replaced the drawer, so the prerequisite for a merge-gate test is
    no longer "put it in a box" but "scan its parts in". This runs the real
    StoreService so the test exercises the same path the floor does.
    """
    from app.modules.store.service import StoreService

    async def _f(piece, *, leather=True, lining=True):
        svc = StoreService(db)
        await _ready_for_store(db, operations, piece, cutter[0].id,
                               leather=leather, lining=lining)
        from app.core.enums import StorePart
        if leather:
            await svc.store_scan(piece_id=piece.id, employee_id=cutter[0].id,
                                 part=StorePart.LEATHER.value)
        if lining:
            await svc.store_scan(piece_id=piece.id, employee_id=cutter[0].id,
                                 part=StorePart.LINING.value)
        await db.refresh(piece)
        return piece
    return _f


@pytest_asyncio.fixture
async def sent_from_store(db, in_store, dm):
    """`await sent_from_store(piece)` → complete AND released, so the merge gate
    is open. Completeness and release are different things; this does both."""
    from app.modules.store.service import StoreService

    async def _f(piece):
        await in_store(piece)
        await StoreService(db).send(piece_ids=[piece.id], actor_user_id=dm.id)
        await db.refresh(piece)
        return piece
    return _f


@pytest_asyncio.fixture
async def cut_pieces(db, operations, cutter, pieces):
    """`pieces`, but every one of them already through BOTH cut paths.

    The drawer/store tests are asserting what a drawer does with parts that
    arrive; they are not asserting how a piece gets to the store. This is that
    prerequisite as a fixture, so those tests read `cut_pieces` and stay about
    the store. Tests that are specifically probing the store-entry gate should take
    `pieces` + `ready_for_store` instead and control each side themselves.
    """
    for piece in pieces:
        await _ready_for_store(db, operations, piece, cutter[0].id)
    return pieces


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
    """5 pieces of the SKU, needs_lining=True — the state after breakdown upload.

    A LIST OF PIECES, not a list of (piece, drawer) tuples. The fixture used to
    mint a vestigial drawer per piece purely to keep that tuple shape alive for
    the call sites that unpacked it; the drawer module is gone, so the scaffolding
    went with it. A freshly minted piece is WAITING — not in the store yet —
    which is exactly what premint writes.
    """
    sku = order_tree["sku"]
    out = []
    for seq in range(1, 6):
        p = Piece(code=f"JP-CLERMONT-PINE-M-{seq:03d}", seq=seq, sku_id=sku.id,
                  current_operation_id=None)
        if hasattr(p, "needs_lining"):
            p.needs_lining = True
        if hasattr(p, "store_state"):
            p.store_state = StoreState.WAITING.value
        db.add(p)
        await db.flush()
        db.add(BarcodeRegistry(code=p.code, type=BarcodeType.PIECE.value,
                               status=BarcodeStatus.ACTIVE.value, piece_id=p.id,
                               caption=p.code))
        out.append(p)
    await db.commit()
    for p in out:
        await db.refresh(p)
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


# ==============================================================================
# SYSTEM-LAYER FIXTURES (router → service → repo → DB over HTTP)
# ------------------------------------------------------------------------------
# ADDED because they were genuinely missing from the shared harness: the only
# HTTP client in the suite was defined PRIVATELY inside
# tests/system/test_role_guards.py, so a second system-layer file had no way to
# speak HTTP without copying it. Copying an auth-override fixture is how two
# files end up disagreeing about what "authenticated" means, which is exactly
# the class of bug the system layer exists to catch. Promoted verbatim in
# behaviour so test_role_guards.py's own copy stays valid.
# ==============================================================================

class FakeAuthUser:
    """Duck-typed stand-in for the User ORM row `get_current_user` returns.

    Deliberately NOT the `FakeUser` above: that one models the ACTOR passed to a
    service (role + employee_id), while the deps layer also reads `is_active`
    and `phone`. Kept separate so a service-level test cannot accidentally
    depend on HTTP-only attributes.
    """
    def __init__(self, role, employee_id=None, client_id=None):
        self.id = uuid.uuid4()
        self.role = role
        self.name = role.value
        self.phone = "9000000000"
        self.employee_id = employee_id
        self.client_id = client_id
        self.is_active = True


@pytest_asyncio.fixture
async def api_client(db):
    """An httpx client bound to the real FastAPI app and the test session.

    `get_db` is overridden so the app writes to the same in-memory SQLite the
    fixtures seeded. Auth is NOT overridden here — a test must opt in via
    `as_role`, so an endpoint's behaviour for an ANONYMOUS caller stays testable.
    """
    from httpx import ASGITransport, AsyncClient
    from app.core.database import get_db
    from app.main import app

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def as_role():
    """`as_role(UserRole.HR)` installs that role as the authenticated caller.

    Overriding `get_current_user` rather than minting real JWTs keeps these tests
    about AUTHORIZATION instead of about signing — the token path has its own
    coverage in the users module.
    """
    from app.core.database import get_db          # noqa: F401  (kept symmetrical)
    from app.main import app
    from app.modules.users.deps import get_current_user

    def _install(role, **kw):
        user = FakeAuthUser(role, **kw)
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    return _install


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
    style = Style(client_order_id=order.id, name="SEEDSTYLE", article="SS1", production_status="RELEASED")
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

# ══════════════════════════════════════════════════════════════════════════════
# REAL LOGINS AND REAL TOKENS
# ══════════════════════════════════════════════════════════════════════════════
# Most tests here override `get_current_user` (see `as_role`) because they are
# about AUTHORIZATION, not about signing. A handful are not: they drive
# /attendance/proxy/* and /employees over HTTP with a real `Authorization: Bearer`
# header, because the thing under test IS the operator gate as the router sees it.
#
# Those tests referenced `client`, `security_user`, `hr_token`, `md_token`,
# `dm_token`, `employee_token`, `security_token`, `emp_id`, `other_emp_id`,
# `monthly_emp` and `monthly_card` — none of which existed. Every one of them
# errored at COLLECTION, so ten tests covering attendance integrity and employee
# RBAC silently never ran. They are the tests that should have caught the payroll
# and proxy-attendance holes. Fixtures below; do not let this rot again.
# ──────────────────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client(db):
    """Alias of `api_client`.

    Kept as its own name because the HTTP tests in tests/system/ each define a
    local `client`, and a local fixture shadows this one — so adding it here is
    additive and changes nothing that already worked.
    """
    from httpx import ASGITransport, AsyncClient
    from app.core.database import get_db
    from app.main import app

    async def _db():
        yield db

    app.dependency_overrides[get_db] = _db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()


async def _make_login(db, role, name, phone, employee_id=None):
    """A real app_user row — `get_current_user` does a live lookup and requires
    is_active, so a signed token alone is not enough."""
    from app.core.security import get_password_hash
    from app.modules.users.models import User

    user = User(name=name, phone=phone, email=f"{phone}@test.local",
                password_hash=get_password_hash("test-password-123"),
                role=role, is_active=True, must_change_password=False,
                employee_id=employee_id)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


def _token_for(user):
    from app.core.security import create_access_token
    return create_access_token(user_id=user.id, role=user.role, name=user.name)


async def _make_worker(db, name, wage_type, designation="CUTTER"):
    from app.core.enums import WageType
    from app.modules.employees.models import Employee

    emp = Employee(name=name, designation=designation,
                   wage_type=wage_type, monthly_salary=0, is_active=True)
    db.add(emp)
    await db.commit()
    await db.refresh(emp)
    return emp


# ── operator logins (the four roles that may record attendance) ──────────────
@pytest_asyncio.fixture
async def security_user(db):
    return await _make_login(db, UserRole.SECURITY, "GATE ONE", "9700000001")


@pytest_asyncio.fixture
async def hr_token(db):
    return _token_for(await _make_login(db, UserRole.HR, "HR ONE", "9700000002"))


@pytest_asyncio.fixture
async def md_token(db):
    return _token_for(
        await _make_login(db, UserRole.MANAGING_DIRECTOR, "MD ONE", "9700000003"))


@pytest_asyncio.fixture
async def dm_token(db):
    return _token_for(
        await _make_login(db, UserRole.DIRECT_MANAGER, "DM ONE", "9700000004"))


@pytest_asyncio.fixture
async def security_token(security_user):
    return _token_for(security_user)


@pytest_asyncio.fixture
async def cutting_mgr_token(db):
    return _token_for(
        await _make_login(db, UserRole.CUTTING_MANAGER, "CUT ONE", "9700000005"))


@pytest_asyncio.fixture
async def employee_token(db):
    """A LEGACY `employee` login.

    Workers get no login (CLAUDE.md s3), so this role is never minted any more —
    but `app_user.role` is a native PG enum and pre-change rows may still carry
    it, which is exactly what `block_employees` still guards. This fixture exists
    to prove that guard, so it deliberately mints the role nothing else mints.
    """
    return _token_for(
        await _make_login(db, UserRole.EMPLOYEE, "LEGACY ONE", "9700000006"))


# ── workers being recorded (no logins — they are scanned, not users) ─────────
@pytest_asyncio.fixture
async def monthly_emp(db):
    from app.core.enums import WageType
    return await _make_worker(db, "MONTHLY WORKER", WageType.MONTHLY, "TAILOR")


@pytest_asyncio.fixture
async def monthly_card(db, monthly_emp):
    """The monthly worker's employee barcode — how the gate identifies them."""
    from app.core.enums import BarcodeType
    from app.modules.barcode.models import BarcodeRegistry

    card = BarcodeRegistry(code=f"EMP-{str(monthly_emp.id)[:8].upper()}",
                           type=BarcodeType.EMPLOYEE, status="ACTIVE",
                           employee_id=monthly_emp.id)
    db.add(card)
    await db.commit()
    await db.refresh(card)
    return card


@pytest_asyncio.fixture
async def emp_id(db):
    from app.core.enums import WageType
    return (await _make_worker(db, "PIECE WORKER A", WageType.PIECE_RATE)).id


@pytest_asyncio.fixture
async def other_emp_id(db):
    from app.core.enums import WageType
    return (await _make_worker(db, "PIECE WORKER B", WageType.PIECE_RATE)).id
