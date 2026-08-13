"""
================================================================================
app/main.py — FastAPI application entry point  (CORRECTED + barcode wired)
================================================================================
WHAT CHANGED vs your version (read these — they are real fixes):

  1. REMOVED  `from sqlalchemy.event import api`  and every `api.include_router(...)`
     call. `api` there was a SQLAlchemy internal, not your app — those lines did
     nothing and would crash. All registration now goes through `app.include_router`.

  2. MERGED the two duplicate `lifespan` functions into ONE. Your file defined
     `lifespan` twice; the second (deps-check only) silently overrode the first,
     so your notification + PO sweepers never started. Now one lifespan does the
     deps check AND the sweepers AND table-create.

  3. block_employees did NOT exist in users/deps.py (your import would crash).
     It is now defined in users/deps.py (see PASTE_block_employees_into_users_deps.py).

  4. ADDED the barcode feature: model imports + router registration for
     barcode / materials / drawers / attendance-scan.

ROUTER LOCKING (employees may reach ONLY their own attendance):
  Every write router already 403s an employee via its own require_roles, so the
  _LOCKED wrapper is belt-and-braces. resolve + scan-check-in stay OPEN so a
  worker can scan their own card.
================================================================================
"""
import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.database import Base, async_engine


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        force=True,
    )
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger("app.main")

# ──────────────────────────────────────────────────────────
# Model imports — every module's models MUST be imported so Base.metadata knows
# all tables (a missed import makes Alembic autogenerate try to DROP the table).
# ──────────────────────────────────────────────────────────
from app.modules.users import models as _users              # noqa: F401
from app.modules.employees import models as _employees      # noqa: F401
from app.modules.clients import models as _clients          # noqa: F401
from app.modules.production import models as _production     # noqa: F401
from app.modules.wages import models as _wages              # noqa: F401
from app.modules.attendance import models as _attendance    # noqa: F401
from app.modules.barcode import models as _barcode          # noqa: F401  (barcode + materials + drawer + supplier tables all live here)
from app.core import models as _core_models                 # noqa: F401
from app.modules.procurement import models as _procurement  # noqa: F401  Stage 1
from app.modules.bom import models as _bom                  # noqa: F401  Stage 2/3
from app.modules.inventory import models as _inventory      # noqa: F401  Stage 4
from app.modules.supplier_po import models as _supplier_po  # noqa: F401  Stage 5

# ──────────────────────────────────────────────────────────
# Router imports
# ──────────────────────────────────────────────────────────
from app.modules.users.router import auth_router, users_router
from app.modules.clients.router import router as clients_router
from app.modules.employees.router import router as employees_router
from app.modules.production.router import router as production_router
from app.modules.wages.router import router as wages_router
from app.modules.analytics.router import router as analytics_router
from app.modules.imports.router import router as imports_router
from app.modules.intelligence.router import router as chat_router
from app.modules.attendance.router import router as attendance_router
# Manager dashboards (cutting / lining / stitching / store). READ-ONLY, and it
# owns NO tables — every query reads other modules' models — so there is no
# `dashboard.models` to add to the model-import block above. Verified before
# registering: a missed model import is the schema-drift trap in §11.
from app.modules.dashboard.router import router as dashboard_router
from app.modules.procurement.router import router as procurement_router
from app.modules.bom.router import router as bom_router
from app.modules.inventory.router import router as inventory_router
from app.modules.supplier_po.router import router as supplier_po_router

# ── NEW: barcode-feature routers ──────────────────────────────────────────────
from app.modules.barcode.router import router as barcode_router
from app.modules.barcode.router import emp_router as barcode_emp_router
from app.modules.materials.router import router as materials_router
from app.modules.materials.router import sup_router as suppliers_router
from app.modules.drawers.router import router as drawers_router

from app.modules.users.deps import block_employees

API_PREFIX = "/api/v1"
_LOCKED = [Depends(block_employees)]   # employee role blocked; managers pass through


# ──────────────────────────────────────────────────────────
# Lifecycle (ONE lifespan — deps check + sweepers + dev table-create)
# ──────────────────────────────────────────────────────────
async def _notification_sweeper():
    from app.core.database import AsyncSessionLocal
    from app.modules.bom.notification_service import NotificationService
    while True:
        try:
            await asyncio.sleep(settings.notification_sweep_seconds)
            async with AsyncSessionLocal() as db:
                sent = await NotificationService(db).run_escalations()
                if sent:
                    logger.info("escalated %s unseen BOM-review notification(s)", sent)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("notification sweeper error: %s", exc)


async def _po_escalation_sweeper():
    from app.core.database import AsyncSessionLocal
    from app.modules.supplier_po.po_service import PoService
    while True:
        try:
            await asyncio.sleep(settings.notification_sweep_seconds)
            async with AsyncSessionLocal() as db:
                advanced = await PoService(db).sweep_escalations()
                if advanced:
                    logger.info("advanced %s supplier-PO escalation rung(s)", advanced)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("PO escalation sweeper error: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ──
    logger.info("Starting %s (%s)", settings.app_name,
                "DEBUG" if settings.debug else "PRODUCTION")

    # fail loud if extractor deps missing, not at first upload
    from app.core.deps_check import verify_extractor_deps
    verify_extractor_deps(strict=True)

    if settings.debug:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        logger.info("Database tables verified (debug create_all)")

    # F130: config_store warm-up runs HERE (inside lifespan), not at module
    # import time. Importing app.main must not require a live database — tooling
    # (OpenAPI export, test collection, linters) only imports the module. The
    # warm cache is an optimisation, so a failure is logged and non-fatal.
    try:
        from app.core.database import AsyncSessionLocal
        from app.modules.bom import config_store
        async with AsyncSessionLocal() as s:
            await s.run_sync(lambda sync_s: config_store.refresh_from_session(sync_s))
        logger.info("config_store warmed from database")
    except Exception:
        logger.exception("config_store warm-up failed; using built-in defaults")

    sweeper = po_sweeper = None
    if settings.notification_sweeper_enabled:
        sweeper = asyncio.create_task(_notification_sweeper())
    if settings.po_escalation_sweeper_enabled:
        po_sweeper = asyncio.create_task(_po_escalation_sweeper())

    yield

    # ── SHUTDOWN ──
    for task in (sweeper, po_sweeper):
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    await async_engine.dispose()
    logger.info("Database connections closed")


# ──────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────
app = FastAPI(
    title=settings.app_name,
    description="Real-time leather manufacturing intelligence & traceability backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:8081",
        "http://localhost:19006",
        "http://localhost:3000",
        "https://frontend-rust-pi-23.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────
# F120: global exception handler — correlation id + generic body
# ──────────────────────────────────────────────────────────
# Without this, any unhandled exception returns Starlette's default 500 with no
# correlation id and (under DEBUG) a full traceback served to the client. This
# logs every unhandled error with a request id the client can quote to support,
# and returns a generic body. The traceback is included ONLY in debug.
import uuid as _uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    # Let FastAPI/Starlette handle HTTPException (401/403/404/409/422 ...) itself;
    # this catch-all is only for genuinely unexpected 500s.
    if isinstance(exc, StarletteHTTPException):
        raise exc
    request_id = str(_uuid.uuid4())
    logger.exception("unhandled error request_id=%s path=%s method=%s",
                     request_id, request.url.path, request.method)
    body = {"detail": "Internal server error", "request_id": request_id}
    if settings.debug:
        body["error"] = repr(exc)
    return JSONResponse(status_code=500, content=body)

# ──────────────────────────────────────────────────────────
# Router registration (ALL under /api/v1)
# ──────────────────────────────────────────────────────────
# auth + users are NOT locked (login/change-password must be reachable).
app.include_router(auth_router, prefix=f"{API_PREFIX}/auth")
app.include_router(users_router, prefix=f"{API_PREFIX}/users")

# attendance is NOT locked (a worker checks themselves in/out).
app.include_router(attendance_router, prefix=API_PREFIX)   # NEW: barcode check-in

# barcode resolve/print router is NOT wrapped in _LOCKED because /barcode/resolve
# must be reachable by a worker scanning their own card; /barcode/print is guarded
# by require_roles inside the router.
app.include_router(barcode_router, prefix=API_PREFIX)           # NEW: /barcode/resolve + /print + /spec-less

# everything below is manager-only work — locked.
app.include_router(clients_router,     prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(employees_router,   prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(production_router,  prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(wages_router,       prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(analytics_router,   prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(dashboard_router,   prefix=API_PREFIX, dependencies=_LOCKED)  # manager dashboards
app.include_router(imports_router,     prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(chat_router,        prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(barcode_emp_router, prefix=API_PREFIX, dependencies=_LOCKED)  # NEW: /employees/{id}/barcode
app.include_router(materials_router,   prefix=API_PREFIX, dependencies=_LOCKED)  # NEW
app.include_router(suppliers_router,   prefix=API_PREFIX, dependencies=_LOCKED)  # NEW
app.include_router(drawers_router,     prefix=API_PREFIX, dependencies=_LOCKED)  # NEW

# Aug-20 stages (BOM/procurement/inventory/supplier_po) — locked.
app.include_router(procurement_router, prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(bom_router,         prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(inventory_router,   prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(supplier_po_router, prefix=API_PREFIX, dependencies=_LOCKED)


# ──────────────────────────────────────────────────────────
# Health
# ──────────────────────────────────────────────────────────
@app.get("/health", tags=["Health"])
async def health_check():
    """Liveness: the process is up. Deliberately checks nothing external so an
    orchestrator does not restart a healthy process on a transient DB blip."""
    return {"status": "healthy", "app": settings.app_name, "version": "1.0.0"}


@app.get("/ready", tags=["Health"])
async def readiness_check():
    """Readiness (F136): can this replica actually serve traffic? Executes a
    trivial query so a pool-exhausted or DB-unreachable replica reports NOT
    ready and is taken out of rotation, instead of looking healthy from outside."""
    from sqlalchemy import text
    from app.core.database import AsyncSessionLocal
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception as exc:
        logger.warning("readiness check failed: %s", exc)
        return Response(
            content='{"status": "not_ready"}',
            status_code=503,
            media_type="application/json",
        )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(status_code=204)


@app.get("/", tags=["Root"])
async def root():
    return {"message": f"Welcome to {settings.app_name}", "docs": "/docs", "health": "/health"}