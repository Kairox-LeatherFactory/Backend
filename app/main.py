"""
================================================================================
app/main.py — FastAPI application entry point
================================================================================

RESPONSIBILITIES
  - Create the FastAPI app instance.
  - Manage startup/shutdown lifecycle (async): in DEBUG, auto-create tables so a
    fresh dev machine works without running Alembic first; in production, do
    nothing destructive (migrations own the schema).
  - Register every module's router under a versioned /api/v1 prefix.
  - Configure CORS for the frontend dev servers.
  - Expose /health and / for liveness checks.

ARCHITECTURE
  This is a modular MONOLITH: one deployable app, many internal modules
  (users, clients, employees, production, wages, analytics, imports). Cross-
  module calls go through services, so a module can later be lifted into its own
  service by swapping in-process calls for HTTP — nothing else changes.

  All persistence is ASYNC (asyncpg + SQLAlchemy async). The two sync contexts
  (Alembic migrations and scripts/seed.py) use the separate sync engine in
  core/database.py.

ROUTE MAP (every router lives under /api/v1)
  /api/v1/auth        login, me, change-password         (self-issued JWT)
  /api/v1/users       create/list logins                 (direct manager)
  /api/v1/clients     clients & their orders
  /api/v1/employees   shop-floor employees
  /api/v1/production  operations & production events
  /api/v1/wages       rates & payroll runs
  /api/v1/analytics   dashboard + freight/bottleneck alerts
  /api/v1/imports     Excel preview/commit
================================================================================
"""
import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import Base, async_engine


def _configure_logging() -> None:
    """Configure the root logger ONCE so every module's `logging.getLogger(__name__)`
    surfaces to stdout (captured by Docker / journald). Level is driven by
    settings.log_level — set LOG_LEVEL=DEBUG in .env to trace each pipeline step.
    Runs at import time so even startup logs are formatted consistently."""
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        force=True,   # override uvicorn's default handler so our format wins
    )
    # asyncpg/sqlalchemy chatter stays at WARNING unless we explicitly want it.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("app.main")

# Import every module's models so SQLAlchemy's metadata knows all tables.
from app.modules.users import models as _users          # noqa: F401
from app.modules.employees import models as _employees  # noqa: F401
from app.modules.clients import models as _clients      # noqa: F401
from app.modules.production import models as _production  # noqa: F401
from app.modules.wages import models as _wages           # noqa: F401
from app.modules.attendance import models as _attendance           # noqa: F401
# Cross-cutting tables (document/notification/audit_log) live in core after the
# procurement monolith was split into procurement (Stage 1) / bom (Stage 2-3) /
# inventory (Stage 4) / supplier_po (Stage 5). Every module's models MUST be imported
# so create_all/Alembic register all tables (a missed import makes autogenerate try to
# DROP the table — the schema-drift trap).
from app.core import models as _core_models                         # noqa: F401
from app.modules.procurement import models as _procurement          # noqa: F401  Stage 1
from app.modules.bom import models as _bom                          # noqa: F401  Stage 2/3
from app.modules.inventory import models as _inventory              # noqa: F401  Stage 4
from app.modules.supplier_po import models as _supplier_po          # noqa: F401  Stage 5

# Routers
from app.modules.users.router import auth_router, users_router
from app.modules.clients.router import router as clients_router
from app.modules.employees.router import router as employees_router
from app.modules.production.router import router as production_router
from app.modules.wages.router import router as wages_router
from app.modules.analytics.router import router as analytics_router
from app.modules.imports.router import router as imports_router
from app.modules.intelligence.router import router as chat_router
from app.modules.attendance.router import router as attendance_router
from app.modules.procurement.router import router as procurement_router
from app.modules.bom.router import router as bom_router
from app.modules.inventory.router import router as inventory_router
from app.modules.supplier_po.router import router as supplier_po_router

API_PREFIX = "/api/v1"


# ──────────────────────────────────────────────────────────
# Lifecycle Management (async)
# ──────────────────────────────────────────────────────────
async def _notification_sweeper():
    """Stage-3 escalation loop (stage-3 spec §2c). Every `notification_sweep_seconds`
    it sends the auto-email for any in-app BOM-review notice that went unseen past its
    2-hour deadline. DB-driven + idempotent (repo.due_escalations' NOT-EXISTS guard),
    so it survives restarts and never double-emails.

    SINGLE-REPLICA ONLY (documented in config): one sweeper per process. At >1 API
    replica, move to SELECT ... FOR UPDATE SKIP LOCKED or an external worker — the same
    caveat as the in-process login rate-limiter (CLAUDE.md §6/§13.9)."""
    from app.core.database import AsyncSessionLocal
    from app.modules.bom.notification_service import NotificationService

    while True:
        try:
            await asyncio.sleep(settings.notification_sweep_seconds)
            async with AsyncSessionLocal() as db:
                sent = await NotificationService(db).run_escalations()
                if sent:
                    print(f"📧 escalated {sent} unseen BOM-review notification(s) to email")
        except asyncio.CancelledError:
            raise
        except Exception as exc:                      # one bad sweep must not kill the loop
            print(f"⚠️  notification sweeper error: {exc}")


async def _po_escalation_sweeper():
    """Stage-5 supplier-chase ladder (stage-5 spec §7c). Every `notification_sweep_seconds`
    it advances one rung (email → WhatsApp → auto-call → exhausted) for any sent PO whose
    `next_escalation_at` is due and that the supplier has not acknowledged. DB-driven +
    idempotent (the `acknowledged_at IS NULL` + `current_rung < 3` guards make a second
    pass a no-op), so it survives restarts and never double-fires.

    SINGLE-REPLICA ONLY (documented in config): one sweeper per process. At >1 API replica,
    move to SELECT ... FOR UPDATE SKIP LOCKED or an external worker — the same caveat as the
    BOM-review sweeper above (stage-5 §7c)."""
    from app.core.database import AsyncSessionLocal
    from app.modules.supplier_po.po_service import PoService

    while True:
        try:
            await asyncio.sleep(settings.notification_sweep_seconds)
            async with AsyncSessionLocal() as db:
                advanced = await PoService(db).sweep_escalations()
                if advanced:
                    print(f"📞 advanced {advanced} supplier-PO escalation rung(s)")
        except asyncio.CancelledError:
            raise
        except Exception as exc:                      # one bad sweep must not kill the loop
            print(f"⚠️  PO escalation sweeper error: {exc}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ──
    print(f"🚀 Starting {settings.app_name}")
    print(f"📦 Environment: {'DEBUG' if settings.debug else 'PRODUCTION'}")
    if settings.debug:
        # Dev convenience only — production schema is owned by Alembic.
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        print("✅ Database tables verified")

    sweeper = None
    if settings.notification_sweeper_enabled:
        sweeper = asyncio.create_task(_notification_sweeper())
        print(f"⏰ Notification escalation sweeper started "
              f"(every {settings.notification_sweep_seconds}s, "
              f"{settings.bom_review_escalation_hours}h deadline)")

    po_sweeper = None
    if settings.po_escalation_sweeper_enabled:
        po_sweeper = asyncio.create_task(_po_escalation_sweeper())
        print(f"📦 Supplier-PO escalation sweeper started "
              f"(every {settings.notification_sweep_seconds}s, "
              f"{settings.po_escalation_hours}h ladder window)")

    yield

    # ── SHUTDOWN ──
    for task in (sweeper, po_sweeper):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await async_engine.dispose()
    print("🛑 Database connections closed")


# ──────────────────────────────────────────────────────────
# Create FastAPI App
# ──────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    description="Real-time leather manufacturing intelligence & traceability backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ──────────────────────────────────────────────────────────
# Middleware (CORS)
# ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",     # Expo web
        "http://localhost:19006",    # Expo web alt
        "http://localhost:3000",     # React dev
        "*",                         # tighten to your frontend origin in production
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────
# Router Registration (all under /api/v1)
# ──────────────────────────────────────────────────────────
app.include_router(auth_router, prefix=f"{API_PREFIX}/auth")
app.include_router(users_router, prefix=f"{API_PREFIX}/users")
app.include_router(clients_router, prefix=API_PREFIX)
app.include_router(employees_router, prefix=API_PREFIX)
app.include_router(production_router, prefix=API_PREFIX)
app.include_router(wages_router, prefix=API_PREFIX)
app.include_router(analytics_router, prefix=API_PREFIX)
app.include_router(imports_router, prefix=API_PREFIX)
app.include_router(chat_router, prefix=API_PREFIX)
app.include_router(attendance_router, prefix=API_PREFIX)
app.include_router(procurement_router, prefix=API_PREFIX)   # Stage 1 intake
app.include_router(bom_router, prefix=API_PREFIX)           # Stage 2/3 BOM + notifications
app.include_router(inventory_router, prefix=API_PREFIX)     # Stage 4 inventory
app.include_router(supplier_po_router, prefix=API_PREFIX)   # Stage 5 supplier PO


# ──────────────────────────────────────────────────────────
# Health Check
# ──────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy", "app": settings.app_name, "version": "1.0.0"}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/", tags=["Root"])
async def root():
    return {"message": f"Welcome to {settings.app_name}", "docs": "/docs", "health": "/health"}

try:
    from app.core.database import SessionLocal      # your SYNC sessionmaker
    from app.modules.bom import config_store
    with SessionLocal() as s:
        config_store.refresh_from_session(s)
except Exception:
    logger.exception("config_store warm-up failed; using built-in defaults")