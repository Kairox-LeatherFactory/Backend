"""
================================================================================
tests/conftest.py — Async test fixtures (in-memory SQLite)
================================================================================
Provides an async DB session and helpers bound to a single shared in-memory
SQLite connection (StaticPool), so every fixture sees the same schema/data
within a test. The app is fully async, so tests are async too.
================================================================================
"""
import os

os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.core.database as dbmod
from app.core.database import Base

# Import every module's models so metadata is complete.
from app.modules.users import models as _u   # noqa
from app.modules.clients import models as _c   # noqa
from app.modules.employees import models as _e  # noqa
from app.modules.production import models as _p  # noqa
from app.modules.wages import models as _w       # noqa


@pytest_asyncio.fixture
async def db():
    """A fresh async session on a shared in-memory SQLite DB per test."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Point the app's session factory at this engine too.
    dbmod.async_engine = engine
    dbmod.AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with dbmod.AsyncSessionLocal() as session:
        yield session

    await engine.dispose()
