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
import secrets

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
# A REAL RANDOM KEY, not a placeholder. This was "smoke-test" — 10 characters —
# and config.py enforces a 32-character minimum with no environment exempt
# (F32/H5), so the one script whose whole job is "run this after any change to
# confirm nothing is broken" died on import with a RuntimeError about the key.
# setdefault could not save it either: an os.environ value BEATS .env, so
# supplying a good key in .env did not help — the placeholder won. The key is
# ephemeral and this process signs its own tokens with it, which is all it needs.
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))

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
            name="Smoke Manager", phone="9000000001", role=UserRole.DIRECT_MANAGER,
            password="9000000001"))

    checks = []
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/health"); checks.append(("health", r.status_code == 200))

        r = await c.post("/api/v1/auth/login",
                         json={"username": "9000000001", "password": "9000000001"})
        checks.append(("login", r.status_code == 200))
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = await c.get("/api/v1/auth/me", headers=h)
        checks.append(("me", r.status_code == 200))

        r = await c.get("/api/v1/employees")
        checks.append(("unauth blocked", r.status_code == 401))

        # THE FACTORY-WIDE READ MOVED TO THE ROLE DASHBOARDS.
        # This was GET /api/v1/analytics/overview, which is now retired and
        # answers 410 Gone naming its replacements — so the smoke test failed on
        # a route the app deliberately removed, which reads as a regression and
        # is not one. The DM dashboard is the whole-factory view now.
        r = await c.get("/api/v1/dashboard/direct-manager", headers=h)
        checks.append(("dm dashboard", r.status_code == 200))

        # The retirement itself is worth asserting: 410 (not 404) is what tells a
        # frontend holding an old build "this was removed" rather than "you typed
        # it wrong", and the body names where the figures went.
        r = await c.get("/api/v1/analytics/overview", headers=h)
        checks.append(("retired overview -> 410", r.status_code == 410))

        # /barcode/resolve is every scan's front door (CLAUDE.md §6), and an
        # unknown code must be 404 — distinct from 410, which means a retired
        # employee card. If this ever 200s or 500s, every scanner is affected.
        r = await c.get("/api/v1/barcode/resolve",
                        params={"code": "NO-SUCH-CODE-0000"}, headers=h)
        checks.append(("unknown barcode -> 404", r.status_code == 404))

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
