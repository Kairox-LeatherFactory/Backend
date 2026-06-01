"""
================================================================================
core/database.py — SQLAlchemy engine + session management (SYNC and ASYNC)
================================================================================

PURPOSE
    The single source of every database connection in the app. Modules NEVER
    create their own engine; they depend on get_db() (async) or, for scripts,
    use SessionLocal (sync). Provides BOTH engines from one place:

      ASYNC  (the live API)         async_engine  + AsyncSessionLocal + get_db()
      SYNC   (Alembic + seed script) engine       + SessionLocal      + get_sync_db()

WHY TWO ENGINES
    The web app is fully async for throughput: a request awaiting the database
    no longer blocks the event loop, so one worker serves many concurrent
    requests efficiently. But two contexts are inherently synchronous and far
    simpler kept that way:
      - Alembic migrations (run at deploy time, single-threaded)
      - the seed script (a one-shot bulk load)
    Forcing those into async buys nothing and complicates them. So we expose a
    sync engine for them and the async engine for everything that serves traffic.

DRIVERS
    async : postgresql+asyncpg   (fastest async Postgres driver)
    sync  : postgresql+psycopg2  (mature, what Alembic expects)
    Tests may use sqlite+aiosqlite / sqlite — handled transparently.

DEPENDENCY USAGE (in routers)
    async def my_route(db: AsyncSession = Depends(get_db)):
        result = await db.execute(select(Model))
        ...
    The session is opened per-request and guaranteed to close in the finally
    block even if the handler raises.
================================================================================
"""
from collections.abc import AsyncGenerator, Generator

from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import settings


# ──────────────────────────────────────────────────────────────────────────
# Declarative base — every module's models inherit from this.
# ──────────────────────────────────────────────────────────────────────────
class Base(DeclarativeBase):
    """Shared declarative base. One metadata object => Alembic sees every table."""
    pass


# ──────────────────────────────────────────────────────────────────────────
# Engine construction helpers
# ──────────────────────────────────────────────────────────────────────────
def _engine_kwargs(url: str) -> dict:
    """Pooling args only make sense for Postgres; SQLite rejects them."""
    if url.startswith("postgresql"):
        return dict(pool_pre_ping=True, pool_size=5, max_overflow=10)
    # SQLite (tests/local): a single shared connection, no pool sizing.
    return dict()


# ──────────────────────────────────────────────────────────────────────────
# ASYNC engine + session  (the live API runs on these)
# ──────────────────────────────────────────────────────────────────────────
async_engine = create_async_engine(
    settings.effective_async_url,
    echo=False,
    future=True,
    **_engine_kwargs(settings.effective_async_url),
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    autoflush=False,
    expire_on_commit=False,   # objects stay usable after commit (no lazy reload)
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields an async session, always closes it."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


# ──────────────────────────────────────────────────────────────────────────
# SYNC engine + session  (Alembic migrations + scripts/seed.py)
# ──────────────────────────────────────────────────────────────────────────
engine = create_engine(
    settings.database_url,
    echo=False,
    future=True,
    **_engine_kwargs(settings.database_url),
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)


def get_sync_db() -> Generator[Session, None, None]:
    """Sync dependency/generator for scripts and any rare sync context."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
