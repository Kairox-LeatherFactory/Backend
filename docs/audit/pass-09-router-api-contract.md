# Pass 9 — Router / API contract

---

## [SEV: BLOCKER] [attendance] [app/modules/attendance/router.py:64]

**Issue:** The router reads a request field the schema does not declare.

**Why it's wrong:** `router.py:64` passes `direction=body.direction`. `ScanCheckIn`
(`attendance/schemas.py:14-22`) declares `employee_barcode`, `lat`, `lon`, `proxy`, `reason` —
there is no `direction`. The stale dump at `attendance/attendance_dump.txt:433` still carries
`direction: str = Field(pattern="^(in|out)$")`, so the field was removed from the live schema and
the router was not updated. Every call to `POST /attendance/scan-check-in` raises `AttributeError`
→ 500 before reaching the service.

This is the **barcode attendance door** — the primary way workers punch in on the floor. It is
100% broken, and it masks the BOLA in `pass-03`.

**Correct behavior:** the contract declares every field the handler reads.

**Fix sketch:** restore `direction: Literal["in", "out"] = "in"` on `ScanCheckIn`, or drop the
argument and let `barcode_scan` infer direction from open/closed state as `_open_or_reject` /
`_close` already can.

**Primary:** restore the field — it is explicit and matches the dump's original design.
**Risk:** the frontend may not be sending it; default it to `"in"` so existing callers work.
**Fallback:** infer direction server-side; fewer moving parts, but a double-tap then closes a
shift the worker meant to open.

---

## [SEV: BLOCKER] [production] [app/modules/production/router.py:71,98,110]

Three GETs pass a kwarg the service does not accept — full finding in `pass-01`. It is a contract
defect as much as a logic one: the router and service disagree about the signature, and nothing
in the type system caught it because the service methods use bare `def ... -> dict` with untyped
`**`-free signatures.

---

## [SEV: MED] [production] [app/modules/production/router.py:74-88]

**Issue:** `GET /production/events` declares no `response_model` and returns ORM rows.

**Why it's wrong:** The handler returns bare `ProductionEvent` objects from
`repository.py:167-181`. With no `response_model`, FastAPI serialises whatever attributes it can
reach — and `ProductionEvent.operation` / `.piece` are lazy relationships
(`production/models.py:118-119`) on an async session. Any serialisation that touches them raises
`MissingGreenlet`; any that does not silently omits them. The endpoint's shape is undefined and
undocumented.

**Fix sketch:** define `EventRead` in `production/schemas.py` and declare it.

**Primary:** add the response model. **Risk:** pins a shape the frontend may already depend on —
check before narrowing. **Fallback:** D1.

---

## [SEV: MED] [production] [app/modules/production/schemas.py:38-141]

**Issue:** Over 100 lines of pre-barcode schemas remain alongside the live ones.

**Why it's wrong:** `CuttingCreate`, `ScanBatchCreate`, `ScanBatchResult`, `ScanResult` describe
the retired one-endpoint-per-stage design. The live contracts (`Actor`, `Targets`, `Consumption`,
`LogRequest`, `LogResult`) are at `:143-190`. Both sets are exported, both appear plausible, and
the retired endpoints they belong to are still registered as 410 stubs (`router.py:171-176`,
prior F95) so they still appear in the OpenAPI document.

**Fix sketch:** delete the dead schemas and unregister the 410 stubs once the frontend has
migrated.

**Primary:** delete. **Risk:** the 410s are a deliberate migration aid — keep those, delete only
the unreferenced schemas. **Fallback:** D2.

---

## [SEV: MED] [analytics] [app/modules/analytics/]

**Issue:** The module has no `schemas.py`, so no analytics response is validated or documented (prior F106).

**Why it's wrong:** All ten endpoints return bare `dict`/`list[dict]` built in the service. The
OpenAPI document describes them as untyped objects, so the frontend has no contract, and a service
change that renames a key breaks callers with no failing test and no schema diff.

**Fix sketch:** add response models for at least `/overview`, `/pieces/{code}/story` and
`/consumption` — the three the dashboard depends on.

**Primary:** add the three. **Risk:** none. **Fallback:** D1/D2 — real debt, but no runtime
defect, and the 2-day window has blockers in it.

---

## [SEV: MED] [all] [pagination]

**Issue:** Only one list endpoint in the API is paginated (prior F93).

**Why it's wrong:** No `limit`/`offset`/`page` parameter appears in `analytics/router.py`,
`clients/router.py`, `materials/router.py` or `imports/router.py`. Unbounded:
`GET /clients` (`repository.py:26`), `GET /clients/{id}/orders` (full eager-loaded tree,
`repository.py:40-45`), `GET /clients/styles` (`repository.py:243-260`),
`GET /analytics/explorer` (every client → order → style → **piece**, `service.py:68-122`, with
`include_pieces` defaulting to `True` at `router.py:50`), `GET /materials/stock`
(`repository.py:43-61`, no LIMIT).

**Fix sketch:** a shared `Page` dependency (`limit: int = Query(50, le=200)`, `offset: int = 0`)
applied to the list routes.

**Primary:** shared dependency. **Risk:** the frontend must handle paged responses — coordinate.
**Fallback (Aug 2):** cap `include_pieces` on `/explorer` to `False` by default and add a hard
`LIMIT 500` inside the three worst queries. Invisible to callers, removes the outage risk. See
`pass-10`.

---

## Status codes and error messages

Correct: `201` on creates (`wages/router.py:139`, `employees/router.py:43`,
`production/router.py:113`, `clients/router.py:40`), `204` on change-password
(`users/router.py:51`), `410` on retired endpoints, `409` on drawer conflicts
(`drawers/service.py:74-78,136-146`), `413` on oversized upload (`imports/router.py:53-68`),
`503` on `/ready` (`main.py:286-295`).

One live issue carried forward: **authorization failures echo the caller's role back**
(prior F122, `users/deps.py:100-103`) — a 403 body naming the caller's own role is minor
information disclosure and, more practically, tells an attacker exactly which role to target.

**REST naming** is consistent apart from `POST /attendance/daily-workers` (`router.py:99`), which
creates employees from an attendance router — a resource that belongs under `/employees`.
D2; renaming it now would break the frontend inside the window.
