"""
INTEGRATION · the store scan's audit actor — with FOREIGN KEYS ACTUALLY ENFORCED.

THE BUG THIS FILE EXISTS FOR
    `store_scan` took one `actor_id` and the router passed the SCANNED WORKER
    into it. That id reached `_audit`, which writes `AuditLog.actor_user_id` — a
    foreign key to `app_user.id`. An employee is not a user, so Postgres rejected
    the row:

        insert or update on table "audit_log" violates foreign key constraint
        ... Key (actor_user_id)=(...) is not present in table "app_user"

    reported from the floor as "the employee barcode ID is not found in the
    app_user table". It only fired on the scan that COMPLETED a drawer, because
    the auto-RECEIVED branch is the only path in store_scan that audits — so the
    first scan worked and the second one 500'd, which is what made it look
    intermittent.

WHY THE WHOLE SUITE MISSED IT
    SQLite does not enforce foreign keys unless `PRAGMA foreign_keys=ON`, and the
    shared harness does not set it. Every existing store-scan test therefore wrote
    a dangling actor id and passed.

    So this module builds its OWN engine with the pragma on. That is the same
    technique tests/integration/test_premint_insert_order.py:43 uses for the
    insert-order bug, and for the same reason: a foreign-key bug is invisible on
    the default harness, so the guard has to bring its own database.

THE RULE BEING PINNED
    actor_id    = the LOGIN that performed the action  → app_user.id
    employee_id = the WORKER whose card was scanned    → employee.id, recorded as
                  DATA on the audit row, never as its actor.
"""
import uuid
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.database import Base
from app.core.enums import (
    BarcodeStatus, BarcodeType, DrawerPart, DrawerState, UserRole, WageType,
)
from app.core.models import AuditLog
from app.modules.barcode.models import BarcodeRegistry, Drawer
from app.modules.clients.models import SKU, Client, ClientOrder, Style
from app.modules.drawers.service import DrawerService
from app.modules.employees.models import Employee
from app.modules.production.models import Piece
from app.modules.users.models import User

pytestmark = pytest.mark.integrity


@pytest_asyncio.fixture
async def fk_db():
    """An async session on SQLite with foreign keys ENFORCED.

    Deliberately not the shared `db` fixture: the whole point is the constraint
    the shared harness leaves switched off.
    """
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _rec):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    Session = async_sessionmaker(bind=engine, class_=AsyncSession,
                                 expire_on_commit=False)
    async with Session() as s:
        yield s
    await engine.dispose()


@pytest_asyncio.fixture
async def scene(fk_db):
    """A login, a worker with a card, and one lined piece merged to a drawer."""
    user = User(name="Store Mgr", phone="STOREMANAGER-T", role=UserRole.STORE_MANAGER,
                password_hash="x", is_active=True)
    emp = Employee(name="LINA", designation="LINING_CUTTER",
                   wage_type=WageType.PIECE_RATE, monthly_salary=0, is_active=True)
    client = Client(name="JP", country="IT")
    fk_db.add_all([user, emp, client])
    await fk_db.flush()

    order = ClientOrder(client_id=client.id, order_number="FK-AUDIT-1")
    fk_db.add(order); await fk_db.flush()
    style = Style(client_order_id=order.id, name="CLERMONT", article="CL1",
                  code="FKA-CL")
    fk_db.add(style); await fk_db.flush()
    sku = SKU(style_id=style.id, color_code="P", color_name="PINE", size="M",
              qty_ordered=1, code="FKA-CL-P-M")
    fk_db.add(sku); await fk_db.flush()

    drawer = Drawer(code="DRW-0001", seq=1, state=DrawerState.MERGED.value)
    fk_db.add(drawer); await fk_db.flush()
    piece = Piece(code="FKA-CL-P-M-001", seq=1, sku_id=sku.id,
                  current_operation_id=None)
    piece.needs_lining = True
    piece.drawer_id = drawer.id
    fk_db.add(piece); await fk_db.flush()
    drawer.current_piece_id = piece.id
    fk_db.add(BarcodeRegistry(code="EMP-FKA001", type=BarcodeType.EMPLOYEE.value,
                              status=BarcodeStatus.ACTIVE.value,
                              employee_id=emp.id, caption="LINA"))
    await fk_db.commit()
    return {"user": user, "employee": emp, "piece": piece, "drawer": drawer}


# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.asyncio
async def test_completing_a_drawer_writes_an_audit_row_that_does_not_violate_the_fk(
    fk_db, scene
):
    """THE REGRESSION. The second scan completes the drawer and audits; with a
    worker id as the actor this raised IntegrityError against app_user."""
    svc = DrawerService(fk_db)
    piece, drawer = scene["piece"], scene["drawer"]
    user, emp = scene["user"], scene["employee"]

    first = await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                 part=DrawerPart.LEATHER,
                                 actor_id=user.id, employee_id=emp.id)
    assert first["auto_received"] is False        # no audit written yet

    # This one completes the drawer → auto-RECEIVED → audit row.
    second = await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id,
                                  part=DrawerPart.LINING,
                                  actor_id=user.id, employee_id=emp.id)
    assert second["auto_received"] is True
    assert second["state"] == DrawerState.RECEIVED.value

    row = (await fk_db.execute(
        select(AuditLog).where(AuditLog.entity_id == drawer.id))).scalar_one()
    # THE ACTOR IS THE LOGIN...
    assert row.actor_user_id == user.id
    assert row.actor_user_id != emp.id
    # ...and the worker is preserved as DATA, not thrown away.
    assert row.after["employee_id"] == str(emp.id)
    assert row.after["part"] == "LINING"
    assert row.after["piece"] == piece.code


@pytest.mark.asyncio
async def test_an_employee_id_as_actor_would_be_rejected_by_the_database(
    fk_db, scene
):
    """Proves the harness really enforces the constraint — otherwise the test
    above would pass for the wrong reason, exactly as the old suite did."""
    from sqlalchemy.exc import IntegrityError

    fk_db.add(AuditLog(
        actor_user_id=scene["employee"].id,          # an employee, not a user
        action="DRAWER_RECEIVED", entity_type="drawer",
        entity_id=scene["drawer"].id, after={}, at=datetime.now(timezone.utc)))
    with pytest.raises(IntegrityError):
        await fk_db.flush()
    await fk_db.rollback()


@pytest.mark.asyncio
async def test_the_scan_credits_the_worker_in_its_response(fk_db, scene):
    svc = DrawerService(fk_db)
    out = await svc.store_scan(
        drawer_id=scene["drawer"].id, piece_id=scene["piece"].id,
        part=DrawerPart.LEATHER,
        actor_id=scene["user"].id, employee_id=scene["employee"].id)
    assert out["employee_id"] == str(scene["employee"].id)


@pytest.mark.asyncio
async def test_a_batch_send_also_audits_against_the_login(fk_db, scene):
    """send_batch already took the login; this pins it so the two paths cannot
    drift apart again."""
    svc = DrawerService(fk_db)
    piece, drawer, user = scene["piece"], scene["drawer"], scene["user"]
    for part in (DrawerPart.LEATHER, DrawerPart.LINING):
        await svc.store_scan(drawer_id=drawer.id, piece_id=piece.id, part=part,
                             actor_id=user.id, employee_id=scene["employee"].id)

    out = await svc.send_batch(drawer_ids=[drawer.id], actor_id=user.id)
    assert out["count_sent"] == 1

    actors = set((await fk_db.execute(
        select(AuditLog.actor_user_id).where(AuditLog.entity_id == drawer.id)
    )).scalars())
    assert actors == {user.id}, "every drawer audit row must name the login"
