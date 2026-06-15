# CLAUDE.md — Kairox Leather Intelligence Platform

> **Audience:** Claude Code (and any AI coding assistant) + new engineers.
> **Purpose:** Single source of truth for *how this codebase is shaped, why, and what NOT to break.*
> **Pair with:** `backend/README.md` (run instructions), `backend/FRONTEND_HANDOFF.md` (contract for FE), `Leather_Factory_Workflow.docx` (domain spec / RAG source).

---

## 1. What this is

Real-time production tracking + piece-rate / monthly / daily-wage payroll + AI assistant for a make-to-order **leather garment factory**. Replaces a paper-and-WhatsApp workflow.

End users: **Direct Manager (superuser)**, Cutting Manager, Stitching Manager, Supervisor (daily-wage proxy), shop-floor Employees, external Clients (read-only on their own orders), Viewers (HR/accounts).

Single shipped repo:

```
leather_factory_backend_with_attendance/
├── backend/        FastAPI modular monolith (the source of truth)
└── frontend/       Next.js + JavaScript (consumes /api/v1, mocks via openapi.json)
```

---

## 2. Tech stack — exact pins matter

**Backend** (FastAPI modular monolith, microservice-ready):
- Python 3.12, FastAPI 0.115.0, Uvicorn
- SQLAlchemy 2.0 async + **asyncpg** (live API)
- SQLAlchemy sync + **psycopg2-binary** (Alembic + `scripts/seed.py` only)
- aiosqlite for tests (in-memory)
- Alembic 1.13.2, Pydantic 2.9.2, pydantic-settings
- **Auth:** self-issued **HS256 JWT** via `python-jose` + `passlib[bcrypt]` (bcrypt pinned at 4.0.1 — passlib 1.7.4 breaks on bcrypt ≥4.1; do not bump without testing)
- openpyxl 3.1.5 for Excel ingestion
- pytest 8.3.3 + pytest-asyncio 0.24.0 + httpx 0.27.2

**AI / Intelligence** (verified 2026 set, see `requirements.txt`):
- LangGraph ≥1.2, LangChain ≥1.2, LangChain-core ≥1.4, LangSmith ≥0.8
- langchain-huggingface + sentence-transformers (embeddings)
- langchain-community + faiss-cpu (RAG)
- python-docx (reads the workflow doc)
- Chat model: pluggable via `CHAT_MODEL` env. Default = deterministic router (no LLM). Recommended local = `ollama:qwen2.5:3b-instruct`.

**Frontend:** Next.js + JavaScript. Auth = OAuth2 password flow (form-encoded), Bearer token on every call.

**Database:** PostgreSQL 16 in Docker. SQLite (in-memory) used only for tests. **We migrated OFF Supabase** — we mint our own JWTs now. Do not reintroduce Supabase.

---

## 3. Architecture — the rules that keep this maintainable

### 3.1 Modular monolith, microservice-ready

One deployable app, internally split into self-contained domain modules. Each module follows:

```
modules/<domain>/
├── models.py       SQLAlchemy ORM tables
├── repository.py   Async data access (only place that talks to the DB for this module)
├── service.py      Business rules, RBAC, cross-module orchestration
├── router.py       FastAPI endpoints (HTTP shell, no business logic here)
├── schemas.py      Pydantic API contracts
└── __init__.py     Module docstring
```

### 3.2 The ONE non-negotiable rule

**Cross-module access goes through `service.py` — NEVER another module's `repository`, `models`, or `router`.**

Enforced in CI by **import-linter** (`.importlinter`). To later split a module into its own service: lift the folder, replace the in-process service call with an HTTP call. *Nothing else changes.* Do not bypass this for "just one quick call."

### 3.3 Async-first

The live API is fully async (asyncpg + SQLAlchemy async). The **only** sync contexts are:
1. Alembic migrations
2. `scripts/seed.py`
3. The Excel importer + load_to_db (run via `starlette.concurrency.run_in_threadpool` from async routes — do not rewrite as async without measuring)

Both engines live in `app/core/database.py`. Don't create engines anywhere else.

### 3.4 Layering inside a module

`router → service → repository → models`. Enforced by import-linter. Router never imports a repository. Service never imports another module's repository.

### 3.5 What lives in `app/core/` (and what doesn't)

```
app/core/
├── config.py     pydantic-settings; reads .env, exposes `settings` singleton
├── database.py   async_engine + AsyncSessionLocal + get_db(); sync engine + SessionLocal
├── enums.py      UserRole, WageType, RunStatus, ShipMode  (str-Enums)
├── models.py     UUIDMixin, TimestampMixin, GUID (portable UUID for Postgres + SQLite)
└── security.py   JWT mint/verify, bcrypt, get_current_user, require_roles, rate limiter
```

`app/core/` **never** imports from `app/modules/`. Acyclic dependency graph.

---

## 4. Module map — what each domain owns

| Module          | Owns                                                                                                                          | Public service interface (what others may call)                          |
| --------------- | ----------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------- |
| **users**       | The ONE login table (`app_user`), self-issued JWT, RBAC. Provisions client + employee logins.                                  | `authenticate`, `create_user`, `create_client_user`, `change_password`    |
| **clients**     | `Client → PurchaseOrder → Style → SKU` hierarchy. A CLIENT-role user scoped to their own data.                                 | `get_sku`, `get_skus_for_style`, `get_style`, `list_clients`              |
| **employees**   | Shop-floor workers. `wage_type` is a property of the PERSON, set explicitly.                                                   | `get`, `monthly_employees`, `create`                                      |
| **production**  | Operations, `operation_access` (role→op config), `production_event` (the central event stream). Enforces RBAC + attendance-today gate. | `log_event`, `piece_counts`, `style_progress`, `list_operations`          |
| **attendance**  | `ShiftConfig` singleton (HR-editable), `AttendanceLog` (one row per employee per day). Haversine geofence (100m default). Three flows: self, supervisor proxy, daily-worker onboard. | `is_present_today`, `days_present`, `today_roster`, `history`             |
| **wages**       | Effective-dated `Rate` table, **FROZEN** `WageRun`/`WageLine` snapshots. Three pay forks: piece_rate, monthly, daily_wage.       | `compute_run`, `get_run`, `set_rate`                                      |
| **analytics**   | Read-only cross-module aggregates: dashboard, stage-spread bottleneck alerts, sea-freight risk predictor.                       | `factory_overview`, `stage_spread_alerts`, `freight_risk`                 |
| **imports**     | Idempotent Excel ingestion (preview + commit). Handles messy real workbooks.                                                    | `preview_workbook`, `commit_workbook`                                     |
| **intelligence**| LangGraph ReAct agent + deterministic fallback + RAG over workflow doc. Math lives in tools, never in the LLM.                  | `IntelligenceService.ask(question, use_llm)`                              |
| **procurement** | RESERVED — no models yet. Boundary established for Stage 4 of workflow.                                                         | —                                                                          |

---

## 5. Data model in one paragraph

`Client → PurchaseOrder → Style → SKU(style + colour + size)`. Production is one event stream: each `production_event` row = one employee did N pieces of one operation on one SKU on one day. Weekly cards, wages, dashboards, freight alerts — all **DERIVED** from that stream. **Pieces are NOT assumed to conserve across stages** (rework/recuts happen; Carnaby: Cutting 152, Pasting 155 is normal). The spread is *surfaced as a metric* in analytics, **never rejected** at write time.

`AttendanceLog` is one row per `(employee_id, work_date)` — UniqueConstraint enforces this at the DB level, not in code.

---

## 6. Auth model

- **One central `app_user` table** logs in everyone (managers, employees, supervisors, clients, viewers).
- **Login id = phone number.** OAuth2 password flow at `POST /api/v1/auth/login` (the `username` field carries phone).
- **Self-issued HS256 JWT** signed with `settings.secret_key`. Token payload: `{sub, role, name, exp}`. No network call to verify.
- Seeded password = phone number (bcrypt-hashed), `must_change_password=True`. **Phone-as-password is v1 mocked-data convenience only — rotate before real production use.**
- `require_roles(*allowed)` dependency restricts endpoints. **`DIRECT_MANAGER` always bypasses** role gates (superuser).
- Rate limit: 5 login attempts / 10 min per phone. In-process counter — **move to Redis if you scale to >1 API replica.**

### Roles (`UserRole` enum)
`DIRECT_MANAGER`, `CUTTING_MANAGER`, `STITCHING_MANAGER`, `SUPERVISOR`, `EMPLOYEE`, `CLIENT`, `VIEWER`.

Adding a role = add a line to `app/core/enums.py`. The rest of the app picks it up.

---

## 7. Critical conventions — DO and DO NOT

### DO

- **Idempotent on re-run.** The seed script and the importer both replace-on-key. Any new ingestion must be safe to re-run.
- **Server-side timestamps for attendance** (`datetime.now(timezone.utc)`). Client-side clocks are not trusted (spec).
- **Compute flags at write time** (is_late, is_short, is_overtime) — dashboards must never recompute on read.
- **Use `GUID` from `app/core/models.py`** for UUID columns. Storing native UUID on Postgres, CHAR(32) on SQLite — keeps tests portable.
- **Use `selectinload`** for eager-loading nested trees (e.g. PO → Style → SKU) — lazy loads are unsafe under async.
- **Validate against the source.** The importer checks computed totals against the workbook's own printed totals (GRAND TOTAL, QTY row). Preserve that pattern.
- **Verify, don't stub.** Run the code, see the output. If it's not executed, it's not done.

### DO NOT

- ❌ **Do not reject piece-count differences across operations.** Pieces don't conserve; the spread is a metric, not an error.
- ❌ **Do not recompute a CLOSED wage run.** `WageLine` rows are a frozen snapshot. Editing an old production event must never silently rewrite past payroll.
- ❌ **Do not infer `wage_type` from designation.** TAILOR and CUTTER appear in both monthly and piece-rate blocks in the source files. It's a property of the PERSON, set explicitly.
- ❌ **Do not bypass `service.py` from `router.py`.** The router is a thin HTTP shell. No business logic, no direct repository calls.
- ❌ **Do not import `os.environ` in modules.** Read `settings` from `app/core/config.py`.
- ❌ **Do not create new engines.** Use `get_db()` (async) or `SessionLocal` (sync only for scripts).
- ❌ **Do not let the LLM do arithmetic.** The intelligence module's tools compute exact numbers; the model only routes and phrases. This is what keeps it cheap AND accurate.
- ❌ **Do not put migrations in `if settings.debug: create_all()`.** Production schema is owned by Alembic. The `create_all` path is dev convenience only.
- ❌ **Do not reintroduce Supabase.** We mint our own tokens now.

---

## 8. Project layout

```
backend/
├── app/
│   ├── core/                  config, db, enums, models, security
│   ├── modules/
│   │   ├── users/             central login, JWT auth, RBAC
│   │   ├── clients/           Client → PO → Style → SKU
│   │   ├── employees/         shop-floor workers
│   │   ├── production/        operations + production_event log
│   │   ├── attendance/        geofence + check-in/out + proxy + daily workers
│   │   ├── wages/             rates + frozen wage runs
│   │   ├── analytics/         read-only dashboards + alerts
│   │   ├── imports/           Excel preview/commit (sync, run in threadpool)
│   │   ├── intelligence/      LangGraph agent + RAG + forecast tools
│   │   └── procurement/       RESERVED (no models yet)
│   └── main.py                FastAPI entrypoint, lifespan, CORS, router wiring
├── alembic/                   migrations
├── scripts/
│   ├── seed.py                idempotent seed from real spreadsheets
│   ├── smoke_test.py          end-to-end against in-memory SQLite (no DB needed)
│   └── export_openapi.py      writes openapi.json for FE mocks
├── tests/                     pytest-asyncio, conftest provides async db fixture
├── requirements.txt
├── Dockerfile
├── docker-compose.yml         postgres:16 + api (runs alembic + seed + uvicorn)
├── alembic.ini
├── .importlinter              CI guard: layering + cross-module rules
├── .env.example
└── README.md
```

Source data files live at `/mnt/project/`:
- `employees_detail.xlsx` (24 monthly + 22 piece-rate workers)
- `GARMENT_ORDERPRODUCTION_DETAILS.xlsx` (6 clients: KJ, GGZ, NIPAL, RICANO, JP, NIPAL-NEW)
- `johnpeter.xlsx` (a 7th flat-format client)
- `Leather_Factory_Workflow.docx` (the domain spec — also the RAG corpus)

---

## 9. How to run

### Docker (recommended)
```bash
cd backend
docker compose up --build
# API:        http://localhost:8000
# Swagger UI: http://localhost:8000/docs
# Login:      9000000001 / 9000000001  (seeded direct manager)
```
The api container runs migrations, seeds from the real spreadsheets, and starts uvicorn.

### Local (no Docker)
```bash
pip install -r requirements.txt
cp .env.example .env                # point DATABASE_URL at your Postgres
alembic upgrade head                # (or rely on DEBUG auto-create for dev)
python -m scripts.seed              # loads employees, 7 clients, rates, logins
uvicorn app.main:app --reload
```

### Tests
```bash
pytest -q                           # async tests, in-memory SQLite via conftest
python -m scripts.smoke_test        # end-to-end ASGI check (no Postgres needed)
```

### Frontend mocks
```bash
python -m scripts.export_openapi    # -> openapi.json
npx @stoplight/prism-cli mock openapi.json
```

### Turning on a real chat model (optional)
```bash
export CHAT_MODEL=ollama:qwen2.5:3b-instruct      # local, free
# or: anthropic:claude-3-5-haiku-latest / openai:gpt-4o-mini
export LANGCHAIN_TRACING_V2=true                  # optional LangSmith tracing
```
Then `POST /api/v1/chat` with `{"question": "...", "use_llm": true}`. No model configured → deterministic router (still exact).

---

## 10. API surface (every route under `/api/v1`)

| Prefix         | Endpoints                                                                                                  |
| -------------- | ---------------------------------------------------------------------------------------------------------- |
| `/auth`        | `POST /login`, `GET /me`, `POST /change-password`                                                          |
| `/users`       | `GET /`, `POST /`, `POST /clients` (direct manager creates client login)                                   |
| `/clients`     | `GET /`, `POST /`, `GET /{id}/orders`                                                                      |
| `/employees`   | `GET /`, `POST /`                                                                                          |
| `/production`  | `GET /operations`, `POST /events`, `GET /events`, `GET /styles/{id}/progress`                              |
| `/attendance`  | `POST /check-in`, `POST /check-out`, `POST /proxy/check-in`, `POST /proxy/check-out`, `POST /daily-workers`, `GET /me`, `GET /today`, `GET /config`, `PATCH /config` |
| `/wages`       | rates CRUD, `POST /runs` (compute + freeze), `GET /runs/{id}`                                              |
| `/analytics`   | `GET /overview`, `GET /alerts/stage-spread`, `GET /alerts/freight-risk?today=`                             |
| `/imports`     | `POST /preview` (dry-run), `POST /commit` (idempotent write) — both direct-manager only                    |
| `/chat`        | `POST /` (full JSON answer), `POST /stream` (SSE for typing UI)                                            |

`GET /health` and `GET /` exposed at root for liveness checks.

---

## 11. The intelligence module — design rules

- **Math in tools, not in the model.** Each tool pulls real rows from the DB, runs deterministic forecast math, returns a structured result. The LLM only chooses the tool and phrases the result. Keeps answers exact AND cheap.
- **Two backends, same response shape `{answer, tool, data}`:**
  - `DeterministicRouter` — keyword/entity routing. Default. No model. Zero cost. Already correct for the killer questions.
  - `LangGraph ReAct agent` (`langgraph_agent.py`) — real model bound to the same tools via `@tool`. Activated when `CHAT_MODEL` is set. Falls back to deterministic on any error — never returns a 500.
- **RAG only for free-text** (the workflow `.docx`). Numeric questions never go through RAG. See `rag.py` — chunk → embed → FAISS → top-k → post-filter on similarity threshold.
- **Tools currently bound:** `schedule_status`, `bottleneck`, `plan_production`, `factory_overview`, `search_workflow_docs`.
- **Models catalogue:** `models_catalog.py` documents embedding picks (default `all-MiniLM-L6-v2`, multilingual option `bge-m3`) and chat picks. **Note:** `MiniMaxAI/MiniMax-M2` is a chat LLM, NOT an embedding model — don't wire it as embeddings.

---

## 12. Testing approach

- **Async fixtures** in `tests/conftest.py` — in-memory SQLite via `StaticPool`, schema created fresh per test.
- **Real-file integration tests** for the importer (skip if `/mnt/project/GARMENT_ORDERPRODUCTION_DETAILS.xlsx` is missing) — verify known totals: GGZ=146, NIPAL=259, RICANO=150 pieces ordered.
- **Determinism check:** parsing the same workbook three times must produce identical signatures.
- **Smoke test** (`scripts/smoke_test.py`) — full ASGI loop against in-memory SQLite, exercises auth + RBAC + analytics. Run after any change.
- **No mocks of the SQLAlchemy layer.** Tests run real queries against SQLite.

---

## 13. Outstanding work / known improvements

These are tracked. If you touch nearby code, fix them in the same PR:

1. **Unique DB constraint on `Client.name`** — currently relies on get-or-create logic; concurrent imports could race.
2. **Dead `if False` branch** in `parse_production.py` — remove.
3. **`ProductionEvent` cleanup on re-import** — replace-mode for production events appears incomplete; verify before next prod import.
4. **File-size guard** in the imports upload handler — currently unbounded.
5. **N+1 delete loops** in the importer — replace with bulk deletes.
6. **Fuzzy worker-tag detection** in `parse_production.py` — tighten the regex; some weekly-period rows are misclassified as worker rows.
7. **Style-matching logic** — currently fragile string matching; replace with explicit parsed fields.
8. **Imports HTTP layer bypasses `service.py`** — router calls `build_preview` + `load_preview` directly. Route through `imports.service` for consistency with every other module.
9. **Login rate limiter is in-process** — move to Redis when scaling beyond 1 replica.

---

## 14. Workflow expectations when contributing

- **Show working code, not stubs.** "Done" means it was executed and the output verified. Untested code is not done.
- **Multi-level explanations on request.** When asked to teach, give beginner / intermediate / advanced views simultaneously — that's the explicit ask.
- **"Continue" means resume current task**, not start something new.
- **When a decision is yours to make** (architectural choice, naming, default value) and the user is silent → make the call, document it inline, move on. Do not block.
- **Push back on incompleteness.** If a previous step looks stubbed, over-cautious, or deferred, call it out explicitly rather than papering over it.
- **Format preference:** dense prose, abbreviated where unambiguous, run-on sentences fine. Skip bullet bloat for casual answers.
- **Documentation deliverables.** Technical manuals + CEO-facing overviews + developer-flow docs are produced as Word documents via the `docx` skill when requested.

---

## 15. Environment variables (`.env`)

```
ENVIRONMENT=local                # local | staging | production
DEBUG=true                       # auto-create tables on startup (dev only)

DATABASE_URL=postgresql+psycopg2://factory:factory@localhost:5432/factory
ASYNC_DATABASE_URL=              # blank = auto-derive (psycopg2 → asyncpg)

SECRET_KEY=change-me-to-a-long-random-string
ALGORITHM=HS256
ACCESS_TOKEN_EXPIRE_MINUTES=1440

LOGIN_MAX_ATTEMPTS=5
LOGIN_WINDOW_SECONDS=600

SEA_CUTOFF_WARNING_DAYS=7

CHAT_MODEL=                      # blank = deterministic; or ollama:qwen2.5:3b-instruct
LANGCHAIN_TRACING_V2=            # optional LangSmith
LANGCHAIN_API_KEY=
```

---

## 16. Quick reference — first 5 minutes in this codebase

1. `app/main.py` — see how every module's router gets wired under `/api/v1`.
2. `app/core/enums.py` — the role + wage-type vocabulary the whole app shares.
3. `app/modules/production/service.py` — the cleanest example of the layering + the attendance-gate pattern.
4. `app/modules/wages/service.py` — see how a frozen run is computed across three pay forks.
5. `app/modules/intelligence/langgraph_agent.py` — see how tools are bound and how the deterministic fallback keeps the chat endpoint from ever 500-ing.

If something is unclear: the docstring at the top of each file is the design rationale. Read that before changing the code.