# Leather Factory Intelligence Platform — Backend

Real-time production tracking + piece-rate wages for make-to-order leather apparel.
Modular monolith (FastAPI + PostgreSQL), microservice-ready.

> **Architecture in this build**
> - **Fully async** persistence: asyncpg + SQLAlchemy 2.0 async on the live API.
>   A separate **sync** engine backs Alembic migrations and `scripts/seed.py`.
>   Both engines live in `app/core/database.py`.
> - **Self-issued JWT auth** (no Supabase): users log in with **phone + password**
>   at `POST /api/v1/auth/login`; we mint and verify our own HS256 tokens.
>   See `app/core/security.py`.
> - **One central user table** (`app_user`) authenticates everyone — managers,
>   employees, clients, viewers — and the `role` column drives authorisation.
>   The direct manager provisions **client logins** at `POST /api/v1/users/clients`.
> - Centralised enums in `app/core/enums.py` (`UserRole`, `WageType`, `RunStatus`,
>   `ShipMode`).
> - Every route is namespaced under **`/api/v1`**.

## Authentication
| Field | Meaning |
|-------|---------|
| login id | the user's **phone number** (the OAuth2 `username` field) |
| password | seeded as the phone number, bcrypt-hashed |
| first login | `must_change_password=True` — clients/staff are forced to reset |

Seeded direct-manager credentials for a demo: `9000000001` / `9000000001`.

## Run locally
```bash
docker compose up --build
# API:        http://localhost:8000
# Swagger UI: http://localhost:8000/docs
```
The container runs migrations, seeds from the real spreadsheets, and starts the API.

```bash
# or, without Docker:
pip install -r requirements.txt
cp .env.example .env            # point DATABASE_URL at your Postgres
alembic upgrade head            # (or rely on DEBUG auto-create)
python -m scripts.seed          # loads employees, 7 clients, rates, logins
uvicorn app.main:app --reload
pytest -q                       # 9 async tests
```

## Architecture
One deployable app, internally split into domain modules:
`users` (central auth) · `clients` (order hierarchy) · `employees` ·
`production` (the event log) · `wages` · `procurement` ·
`analytics` (dashboards/alerts) · `imports`.

Each module is layered: `router -> service -> repository -> models`.

### The one rule that keeps microservices cheap later
Modules call each other ONLY through `service`, never another module's
`repository` or `models`. Enforced in CI by import-linter (`.importlinter`).
To split a module into its own service: lift its folder out, replace the
in-process service call with an HTTP call. Nothing else changes.

## The data model in one paragraph
`Client -> PurchaseOrder -> Style -> SKU(style+colour+size)`. Production is one
event stream: each `production_event` = one employee did N pieces of one
operation on one SKU on one day. Weekly cards, wages, and dashboards are all
DERIVED from that stream. Operations are NOT assumed to conserve pieces across
stages (rework/recuts happen) — the spread is surfaced as a metric, never rejected.

## Wages
- Piece-rate: `qty * rate(style, operation, effective_date)`, summed per employee.
- Monthly: flat `monthly_salary`, independent of production.
- A wage run is FROZEN (`wage_line` snapshot) so closed periods never recompute.

## Frontend handoff
`python -m scripts.export_openapi` writes `openapi.json`.
Frontend mocks the whole API with: `prism mock openapi.json` (or MSW).
Build UI immediately — no waiting on the backend.
