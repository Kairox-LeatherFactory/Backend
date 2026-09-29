# 1. What this service is

Two small things that do not belong to any one module, plus how the whole application is put together.

| Part | Endpoints | Needs a token? |
|---|---|---|
| **Health** | `GET /health`, `GET /ready` | **No** |
| **Root** | `GET /` | **No** |

> Note the paths. `/health`, `/ready` and `/` are **not** under `/api/v1`, and none of them needs a token.

| Part | File |
|---|---|
| App assembly, health, root | `app/main.py` |

> **The chatbot moved out of this document.** `POST /api/v1/chat` and `POST /api/v1/chat/stream` are their own service now — see `INTELLIGENCE_SYSTEM_GUIDE.docx` and `INTELLIGENCE_API_REFERENCE.docx`.

---

# 2. Health and readiness — and why there are two

They answer **different questions**, and that difference is the whole point.

## `GET /health` — liveness

*"Is this process up?"*

It checks **nothing external**, deliberately. It does not touch the database.

```json
{ "status": "healthy", "app": "KairoX", "version": "1.0.0" }
```

> If `/health` touched the database, a transient database blip would make an orchestrator **restart a perfectly healthy process** — turning a five-second glitch into a rolling outage.

## `GET /ready` — readiness

*"Can this replica actually serve traffic?"*

It runs a trivial `SELECT 1`. If the connection pool is exhausted or the database is unreachable, it answers **503**:

```json
{ "status": "not_ready" }
```

A load balancer takes that replica **out of rotation** without killing it. When the database comes back, the replica starts answering 200 again and returns to service by itself.

| | `/health` | `/ready` |
|---|---|---|
| Touches the database | no | yes |
| Failure means | the process is dead → **restart it** | this replica cannot serve → **stop sending it traffic** |
| Used by | the container's liveness probe | the load balancer / readiness probe |

**Wire them to the right probe.** Swapping them is how a database hiccup becomes a restart loop.

---

# 3. `GET /` — the root

```json
{ "message": "Welcome to KairoX", "docs": "/docs", "health": "/health" }
```

A pointer, nothing more. `GET /favicon.ico` answers `204` so browsers stop asking.

---

# 4. The interactive docs

`/docs` (Swagger) and `/redoc` are **on in every environment**, including production.

They used to switch themselves off outside a dev box, which quietly broke the people who need them most: the frontend developers integrating against staging, and anyone verifying a deploy.

> **Hiding the route map is not a security control.** Every route still enforces its token and its role gate, and anyone who can reach the host can enumerate it anyway. The real control is not exposing this API to the open internet.

It is reversible: `DOCS_ENABLED=false` on any single deploy turns them dark again.

---

# 5. How the application is assembled

| Fact | Why it matters |
|---|---|
| **Modular monolith** — one deployable app, many internal modules | cross-module calls go through services, so a module can later be lifted out |
| **Every model module is imported** in `main.py` | a missed import makes Alembic autogenerate try to **DROP** that table — the schema-drift trap |
| **Every router is registered under `/api/v1`** | except health and root |
| **Manager-only routers are wrapped in `block_employees`** | `auth`, `users`, `attendance` and `/barcode/resolve` stay open at the router level, because the attendance screen must stay reachable |
| **There is exactly ONE `lifespan`** | defining a second one silently overrides the first |

## Errors have one shape everywhere

Every failure — 400, 403, 404, 409, 410, 422, 500 — carries a `request_id`:

```json
{ "detail": "…", "request_id": "0f2c7f0e-2b7a-4f1b-9a5a-9f0b3f0c7e11" }
```

**Put the `request_id` in the bug report.** It is what lets the backend find the exact request in the log. A 422 is the exception: its `detail` is a **list**, one entry per bad field, preserved verbatim so a form can mark the right box.

A `403` is logged at WARNING, not ERROR — a 403 is the system working. A burst of them is still how a misconfigured role shows up.

## Middleware, in order

| Middleware | What it does |
|---|---|
| GZip (min 1000 bytes) | dashboards and analytics send large JSON to tablets on factory wifi; gzip typically removes 70–90% |
| TrustedHost | which `Host` headers the app answers to. Set `TRUSTED_HOSTS` in production — an app that answers any Host and reflects it into a link is how cache poisoning happens |
| CORS | origins come from `CORS_ORIGINS`, not from the code, so adding a frontend domain is not a redeploy. Paired with `allow_credentials` and an **explicit** origin list — never `*` |

## Background sweepers

Two run in the API process: BOM notification escalation and supplier-PO escalation.

They are wrapped in a **Redis single-flight lock**, so exactly one process in the whole fleet does the work each cycle. Without it, `workers × replicas` copies of every escalation **email** go out per cycle — at `WEB_CONCURRENCY=4` that is four copies from one box, and 4×N behind a load balancer.

Both can be switched off with their settings flags.

---

# 6. Notes for backend developers

- **Tables are not created automatically.** `create_all` is deliberately commented out. On a new database run `alembic upgrade head` **before** the first app start.
- **`config_store` warms inside `lifespan`, not at import time.** Importing `app.main` must not require a live database — tooling (OpenAPI export, test collection, linters) only imports the module. A warm-up failure is logged and non-fatal.
- **Known traps, already fixed — do not reintroduce:** importing `api` from `sqlalchemy.event` (it is a SQLAlchemy internal, not the app — use `app.include_router`); defining `lifespan` twice; importing `block_employees` before it exists in `users/deps.py`.
- **Ids come from the app (`uuid4`), not from the database.** No `gen_random_uuid()`, no `JSONB` operators, no `ON CONFLICT` — the schema stays portable.
