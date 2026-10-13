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
     barcode / materials / store / attendance-scan..

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
from fastapi.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

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
from app.modules.barcode import models as _barcode          # noqa: F401  (barcode + materials + supplier + style-spec/issue-ledger tables all live here; the retired drawer table too, so autogenerate does not drop it)
from app.modules.jobwork import models as _jobwork            # noqa: F401  (vendor — production_event FKs to it)
from app.modules.cutting import models as _cutting          # noqa: F401  (cutting_row — material_sheet FKs to it, so it must load with the barcode tables)
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
from app.modules.materials.router import spec_router as style_spec_router
# THE DRAWER IS GONE — app/modules/drawers is deleted, replaced by
# app/modules/store. There were 200 physical drawers; a style releases 100+
# garments, so the pool stalled mid-chain and a DM had to re-allocate by hand,
# which in practice did not happen. Every fact a drawer held was a fact about the
# GARMENT and now lives on the piece (store_state / leather_in / lining_in /
# accessories_in), where it has no capacity to run out of.
#
# THE TABLES STAY, unwritten and unread, so the historical movement rows remain
# auditable — `barcode.models` still maps them so Alembic autogenerate does not
# try to DROP them (the schema-drift trap, §11). Nothing imports them.
from app.modules.cutting.router import router as cutting_router
from app.modules.store.router import router as store_router
from app.modules.production.inspection_router import router as inspection_router
from app.modules.jobwork.router import router as jobwork_router

from app.modules.users.deps import block_employees

API_PREFIX = "/api/v1"
_LOCKED = [Depends(block_employees)]   # employee role blocked; managers pass through


# ──────────────────────────────────────────────────────────
# Lifecycle (ONE lifespan — deps check + sweepers + dev table-create)
# ──────────────────────────────────────────────────────────
# THESE SWEEPERS RUN IN THE API PROCESS, SO THEY RUN ONCE PER PROCESS.
#
# That is `gunicorn workers x replicas` times per cycle, and escalation SENDS
# EMAIL — it is not idempotent from the recipient's side. At WEB_CONCURRENCY=4
# one box already sends four copies of every escalation per cycle; behind a load
# balancer with N tasks it is 4N. It was also the main thing stopping the API
# from being scaled horizontally at all.
#
# `single_flight` puts a short Redis lock in front of each cycle so exactly one
# process in the whole fleet does the work and the rest skip. See
# core/single_flight.py for why this is not simply moved to Celery beat yet.
async def _sweep_forever(job_name: str, interval: int, run):
    """Run `run(db)` every `interval` seconds, once across the whole fleet."""
    from app.core.database import AsyncSessionLocal
    from app.core.single_flight import single_flight

    while True:
        try:
            await asyncio.sleep(interval)
            # TTL just under the interval: a process that dies mid-sweep frees
            # the job by the next tick instead of wedging it.
            async with single_flight(job_name,
                                     ttl_seconds=max(5, interval - 5)) as mine:
                if not mine:
                    continue
                async with AsyncSessionLocal() as db:
                    await run(db)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("%s sweeper error: %s", job_name, exc)


async def _notification_sweeper():
    from app.modules.bom.notification_service import NotificationService

    async def _run(db):
        sent = await NotificationService(db).run_escalations()
        if sent:
            logger.info("escalated %s unseen BOM-review notification(s)", sent)

    await _sweep_forever("notification-escalation",
                         settings.notification_sweep_seconds, _run)


async def _po_escalation_sweeper():
    from app.modules.supplier_po.po_service import PoService

    async def _run(db):
        advanced = await PoService(db).sweep_escalations()
        if advanced:
            logger.info("advanced %s supplier-PO escalation rung(s)", advanced)

    await _sweep_forever("po-escalation",
                         settings.notification_sweep_seconds, _run)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── STARTUP ──
    logger.info("Starting %s (%s)", settings.app_name,
                "DEBUG" if settings.debug else "PRODUCTION")

    # fail loud if extractor deps missing, not at first upload
    from app.core.deps_check import verify_extractor_deps
    verify_extractor_deps(strict=True)
    
    #I DON'T WANT TO CREATE TABLES AUTOMATICALLY, ALEMBIC WILL HANDLE IT.......
    # if settings.debug:
    #     # Never create_all on a database Alembic manages. It builds tables from
    #     # TODAY's models ahead of the migrations, which (a) hides revisions that
    #     # were never written, so a fresh `alembic upgrade head` later dies on a
    #     # missing table, and (b) makes the next migration die with
    #     # DuplicateColumn/DuplicateTable. On a new DB: run `alembic upgrade head`
    #     # BEFORE the first app start.
    #     from sqlalchemy import inspect as _inspect
    #     async with async_engine.begin() as conn:
    #         managed = await conn.run_sync(
    #             lambda c: _inspect(c).has_table("alembic_version"))
    #         if managed:
    #             logger.info("Alembic-managed database: skipping debug create_all")
    #         else:
    #             await conn.run_sync(Base.metadata.create_all)
    #             logger.info("Database tables verified (debug create_all)")

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
# THE INTERACTIVE DOCS STAY ON — IN EVERY ENVIRONMENT (Hamthan, 2026-09-23).
#
# They used to switch themselves off outside a dev box (`if is_production`),
# which quietly broke the people who need them most: the two frontend devs
# integrating against staging, and anyone verifying a deploy. Hiding the route
# map is not a security control — every route still enforces its JWT and its
# role gate, and an attacker who can reach the host can enumerate it anyway.
# The real control is not exposing this API to the open internet.
#
# It is a deliberate, reversible trade: DOCS_ENABLED=false on any single deploy
# turns them dark again without touching this file.
_docs_url = "/docs" if settings.docs_enabled else None
_redoc_url = "/redoc" if settings.docs_enabled else None

app = FastAPI(
    title=settings.app_name,
    description="Real-time leather manufacturing intelligence & traceability backend",
    version="1.0.0",
    lifespan=lifespan,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
)

# RESPONSES ARE COMPRESSED. The dashboard and analytics surfaces return large
# JSON aggregates to tablets on factory wifi; gzip typically takes 70-90% off a
# payload of that shape. 1000 bytes is the usual floor — below it the CPU and the
# extra header cost more than the saving.
app.add_middleware(GZipMiddleware, minimum_size=1000)

# WHICH HOST HEADERS THIS APP ANSWERS TO.
#
# Default "*" keeps local dev and the container health check working. In
# production set TRUSTED_HOSTS to the real domains: an app that answers to any
# Host and reflects it into a generated link is how cache poisoning and
# forged password-reset links happen. Added here rather than left to the load
# balancer so the guarantee travels with the app.
app.add_middleware(TrustedHostMiddleware,
                   allowed_hosts=settings.trusted_host_list)

# ORIGINS COME FROM THE ENVIRONMENT (settings.cors_origin_list), not from this
# file. They used to be a hardcoded list here, so adding a frontend domain meant
# a code change and a redeploy of the API. Note this pairs `allow_credentials`
# with an explicit origin list — never with "*", which browsers reject anyway and
# which would make every site on the internet a trusted caller.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
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
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
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


# ONE ERROR SHAPE FOR EVERY FAILURE, NOT JUST THE 500s.
#
# The handler above gives an unhandled error a `request_id` the caller can quote
# to support. Every DELIBERATE failure — the 403 on the payroll gate, the 409 on
# a piece released before its parts merged, the 422 on a material lot missing its
# category's fields — came back as a bare {"detail": ...} with nothing to quote.
# Those are the errors the floor actually hits, and they were the ones support
# could not trace.
#
# The two handlers below keep the status codes and the messages exactly as they
# are and only ADD `request_id`, so nothing that reads `detail` today changes.
@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    request_id = str(_uuid.uuid4())
    # Logged at WARNING, not ERROR: a 403 is the system working. It is still
    # worth a line, because a burst of them is how a misconfigured role shows up.
    logger.warning("http error request_id=%s status=%s path=%s method=%s detail=%s",
                   request_id, exc.status_code, request.url.path, request.method,
                   exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "request_id": request_id},
        # 401 carries WWW-Authenticate; dropping it would break the auth flow.
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request,
                                       exc: RequestValidationError):
    """422s from request validation.

    `errors` is preserved verbatim — it is what tells a form WHICH field is
    wrong, and the import and material screens depend on it.
    """
    request_id = str(_uuid.uuid4())
    logger.warning("validation error request_id=%s path=%s method=%s",
                   request_id, request.url.path, request.method)
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(exc.errors()),
                 "request_id": request_id},
    )

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
# The per-piece material spec. Mounted at /styles (previously unclaimed) and
# locked like every other manager surface; its own routes then split read
# (_STOCK_READERS — the floor needs the accessory list) from write (DM/MD).
app.include_router(style_spec_router, prefix=API_PREFIX, dependencies=_LOCKED)
app.include_router(cutting_router,     prefix=API_PREFIX, dependencies=_LOCKED)  # Cutting V2
app.include_router(store_router,       prefix=API_PREFIX, dependencies=_LOCKED)  # the merge, on the piece
app.include_router(inspection_router,  prefix=API_PREFIX, dependencies=_LOCKED)  # reject & rework
app.include_router(jobwork_router,     prefix=API_PREFIX, dependencies=_LOCKED)  # outsourcing

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
