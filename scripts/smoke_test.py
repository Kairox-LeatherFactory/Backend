"""
================================================================================
scripts/smoke_test.py — End-to-end async smoke test (no real DB needed)
================================================================================

PURPOSE
    A fast, self-contained sanity check of the whole stack against an in-memory
    SQLite database: it creates the schema, seeds one direct manager, then drives
    the live ASGI app to prove auth, RBAC, and the core read endpoints work.

    This is the script to run after any change to confirm nothing is broken,
    without needing Postgres up.

RUN
    python -m scripts.smoke_test
================================================================================
"""
import asyncio
import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "smoke-test")

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.core.database as dbmod
from app.core.database import Base
from app.core.enums import UserRole


async def main():
    # Import the app FIRST (this binds its routers), then rebind the DB engine
    # to a shared in-memory SQLite so both the seed and the requests see it.
    from app.main import app

    engine = create_async_engine("sqlite+aiosqlite://",
                                 connect_args={"check_same_thread": False},
                                 poolclass=StaticPool)
    dbmod.async_engine = engine
    dbmod.AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Seed one direct manager via the service.
    from app.modules.users.service import UserService
    from app.modules.users import schemas
    async with dbmod.AsyncSessionLocal() as db:
        await UserService(db).create_user(schemas.UserCreate(
            name="Smoke Manager", phone="9000000001", role=UserRole.DIRECT_MANAGER))

    checks = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health"); checks.append(("health", r.status_code == 200))

        r = await c.post("/api/v1/auth/login",
                         data={"username": "9000000001", "password": "9000000001"})
        checks.append(("login", r.status_code == 200))
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = await c.get("/api/v1/auth/me", headers=h)
        checks.append(("me", r.status_code == 200))

        r = await c.get("/api/v1/employees")
        checks.append(("unauth blocked", r.status_code == 401))

        r = await c.get("/api/v1/analytics/overview", headers=h)
        checks.append(("analytics", r.status_code == 200))

    await engine.dispose()

    print("─" * 40)
    ok = True
    for name, passed in checks:
        print(f"  {'✅' if passed else '❌'}  {name}")
        ok = ok and passed
    print("─" * 40)
    print("ALL SMOKE CHECKS PASSED" if ok else "SMOKE TEST FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
