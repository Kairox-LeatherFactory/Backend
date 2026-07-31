# Pass 15 — Production readiness (assume 10 factories)

Everything below assumes the target the brief names: **10 factories**, not one. Several items
that are acceptable for a single site become blocking at that scale, and they are marked.

---

## [SEV: HIGH] [core] [app/core/config.py:50-51]

**Issue:** The two most dangerous defaults are the permissive ones.

`environment = "local"` and `debug = True` together produce, in an unconfigured deployment:

| Consequence | Where |
|---|---|
| JWT signed with the shipped default key | `config.py:256` exemption + `:124` |
| Alembic bypassed; schema built by `create_all` | `main.py:144-147` |
| Exception reprs returned to callers | `main.py:229-230` |

One unset environment variable produces all three. Detailed in `pass-03` and `pass-13`;
repeated here because at ten sites the probability that every deployment sets both correctly
approaches zero.

**Primary:** flip both defaults to the safe values. **Risk:** developers add two lines to `.env`
(the repo's `.env` already has them). **Fallback:** a startup assertion that refuses to boot when
`debug and not environment == "local"`. Louder, still defaults-dependent.

---

## [SEV: HIGH] [scale] [pagination and N+1]

At ten factories the unbounded reads in `pass-09` and `pass-10` stop being latency and become
availability:

- `GET /analytics/explorer` materialises every client → order → style → **piece**
  (`service.py:68-122`), with `include_pieces=True` by default (`router.py:50`).
- `stage_spread_alerts` and `freight_risk` are unbounded N+1 loops on dashboard endpoints.
- `POST /production/log` issues 250–300 round trips per 40-piece tray.

**Primary:** the batched queries in `pass-10`. **Risk:** the production-log batching touches the
gates — do it with tests green. **Fallback for Aug 2:** default `include_pieces=False`, hard
`LIMIT` on the three worst queries, and the single-`IN` piece fetch. Removes the outage risk
without touching gate logic.

---

## [SEV: MED] [core] [app/main.py:40-50]

**Issue:** Logging is unstructured and has no request correlation (prior F132).

**Why it's wrong:** `logging.basicConfig` with a plain text format, `force=True`, at import time.
No JSON, no request id in the format string — even though `main.py:225-228` generates one per 500.
A user reporting "request 7f3a… failed" cannot be traced without a full-text log search, and
across ten sites there is no field to filter by factory.

**Fix sketch:** structured JSON logging with a `contextvars` request id injected by middleware,
plus a `site_id` field from config.

**Primary:** as above. **Risk:** log-ingestion config changes. **Fallback (Aug 2):** add
`%(request_id)s` to the format string and set it in the existing handler — partial correlation,
one line.

---

## [SEV: MED] [core] [app/main.py:271-274]

**Issue:** `/health` checks nothing (prior F136).

**Why it's wrong:** It is a pure liveness probe by design, and `/ready` (`:278-295`) does execute
`SELECT 1` and returns 503 correctly. The gap is that nothing checks the **dependencies** —
storage backend reachability, Celery broker, the `bom.config_store` the lifespan loads. An
orchestrator sees a green pod that cannot serve an upload.

**Fix sketch:** extend `/ready` to check the storage backend and broker, with a short timeout.

**Primary:** extend `/ready`, leave `/health` alone. **Risk:** a flaky dependency check causes
pod churn — use a generous timeout and check dependencies as `degraded`, not `down`.
**Fallback:** D2 for one factory; D1 before the second site.

---

## [SEV: MED] [core] [app/core/storage.py:269-275]

**Issue:** An unrecognised `STORAGE_BACKEND` silently falls back to local disk.

**Why it's wrong:** `get_storage()` returns `LocalStorageBackend` for any unknown value. A typo'd
`STORAGE_BACKEND=s3 ` (trailing space) in production writes uploaded breakdown sheets to the
container's ephemeral filesystem, where they vanish on restart — with no error at any point.

**Fix sketch:** raise on an unrecognised value at startup.

**Primary:** fail loud. **Risk:** a bad env var now prevents boot — which is the correct
behaviour. **Fallback:** none; this is a four-line change that prevents silent data loss.

The path-traversal guard in the same file (`:119-129`) is correctly implemented — worth noting
alongside.

---

## [SEV: MED] [core] [app/main.py:188-189]

**Issue:** `/docs` and `/redoc` are exposed unconditionally (prior F137).

**Fix sketch:** `docs_url="/docs" if settings.debug else None`.

**Primary:** gate on environment. **Risk:** none — internal users can read the schema from a
staging deploy. **Fallback:** leave open behind the load balancer's IP allowlist.

---

## [SEV: MED] [core] [app/core/database.py:122-129]

**Issue:** Connection pools are at library defaults, and a sync engine is created at import (prior F130, F138).

**Why it's wrong:** Neither `create_async_engine` nor `create_engine` sets `pool_size`,
`max_overflow`, `pool_pre_ping` or `pool_recycle`. The default pool of 5 + 10 overflow, shared
across the N+1 loops above, is the second constraint the system will hit after the queries
themselves. Without `pool_pre_ping`, a connection dropped by a Supabase idle timeout surfaces as a
random 500. And `engine` / `SessionLocal` are constructed at module import, so every process —
including the API, which uses the async engine — opens a sync pool it never uses.

**Fix sketch:**
```python
create_async_engine(url, pool_size=20, max_overflow=10, pool_pre_ping=True, pool_recycle=1800)
```

**Primary:** set the four parameters; `pool_pre_ping` is the one that matters most against a
managed Postgres. **Risk:** none. **Fallback:** none — this is configuration, and it should ship
for Aug 2.

---

## Backups, DR, migrations

- **No backup or restore procedure exists in the repository** (prior F135) — no documented
  `pg_dump` schedule, no restore runbook, no tested recovery. Supabase provides point-in-time
  recovery on paid tiers; whether it is enabled is unverified from here. **This is the single
  largest unquantified risk in the deployment and it is not a code fix.** Confirm the Supabase
  backup tier before Aug 2 and write the restore steps down.
- **Migrations:** the chain is now single-root, single-head and clean (`pass-05`). The two
  remaining risks are stale `.pyc` for six deleted revisions and the possibility that a staging
  database is stamped at one of them — a one-query check before deploy (`pass-05`).
- **Error recovery:** the four-commit payroll run (`pass-02`) is the one place where a crash
  leaves inconsistent committed state that no process reconciles.
- **HA:** three components assume exactly one replica (prior F131) — the in-process sweepers
  (`main.py:163-166`), the login rate limiter (`security.py:126`) and the barcode/drawer code
  counters (`barcode/repository.py:53-76`, `premint.py:97`). At one factory, run one replica and
  this is contained. **At ten factories none of these are viable** and all three need external
  state (Redis) before the second site goes live.

---

## Aug 2 readiness call

For **one factory, one replica**, with the blockers in passes 1–3 fixed, the deployment is
viable with a named-risk register covering: no rate limiting, no backup runbook, unstructured
logs, default pools, and the single-replica assumptions.

For **ten factories**, the work in this pass is not optional and none of it fits the two-day
window. It should be scheduled as an explicit pre-scale milestone between Aug 2 and Aug 20 —
before the second site, not after.
