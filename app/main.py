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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import Base, async_engine

# Import every module's models so SQLAlchemy's metadata knows all tables.
from app.modules.users import models as _users          # noqa: F401
from app.modules.employees import models as _employees  # noqa: F401
from app.modules.clients import models as _clients      # noqa: F401
from app.modules.production import models as _production  # noqa: F401
from app.modules.wages import models as _wages           # noqa: F401
from app.modules.attendance import models as _attendance           # noqa: F401

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

API_PREFIX = "/api/v1"


# ──────────────────────────────────────────────────────────
# Lifecycle Management (async)
# ──────────────────────────────────────────────────────────
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

    yield

    # ── SHUTDOWN ──
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


# ──────────────────────────────────────────────────────────
# Health Check
# ──────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health_check():
    return {"status": "healthy", "app": settings.app_name, "version": "1.0.0"}


@app.get("/", tags=["Root"])
async def root():
    return {"message": f"Welcome to {settings.app_name}", "docs": "/docs", "health": "/health"}
