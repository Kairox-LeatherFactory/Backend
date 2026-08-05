# Pass 3 — Security

Ranked by severity. Prior findings F32–F49 re-verified in `delta-register.md`; the ones that
remain open are re-cited here only where the line numbers moved or the exposure changed.

---

## [SEV: BLOCKER] [attendance] [app/modules/attendance/router.py:52-61]

**Issue:** Any non-EMPLOYEE token — including CLIENT and VIEWER — can check in any employee by barcode.

**Why it's wrong:** The guard is:
```python
if user.role == UserRole.EMPLOYEE:
    if getattr(user, "employee_id", None) != employee_id:  raise 403
    if body.proxy:                                          raise 403
elif body.proxy and user.role not in proxy_roles:           raise 403
```
The `elif` at `:59` fires **only when `body.proxy` is true**. With `proxy=False` (the default,
`schemas.py:21`) any other role falls through with an arbitrary `employee_id`. `attendance_router`
is deliberately not wrapped in `block_employees` (`main.py:241`) so workers can punch in, which
means a CLIENT token reaches this route. The row is written as `AttendanceSource.SELF` with
`recorded_by=actor.id` (`service.py:208-212`), and it flips `is_present_today` — the gate
production logging depends on (`production/service.py:176-182`).

Combined with the GPS-optional path below, **a client login can fabricate the entire factory's
attendance record from off-site.** Attendance feeds monthly wage proration
(`wages/service.py:490-495`) and gates all production logging.

Currently masked by F141 (the route 500s before reaching the service). Fixing F141 without
fixing this makes the hole live.

**Correct behavior:** a non-proxy scan may only ever check in the token's own `employee_id`;
scanning someone else's card is proxy by definition and requires a proxy role.

**Fix sketch:**
```python
if body.proxy or getattr(user, "employee_id", None) != employee_id:
    if user.role not in proxy_roles: raise HTTPException(403, ...)
```

**Primary:** as above — one condition, closes both cases. **Risk:** none; it is what the route's
own docstring describes. **Fallback:** none. This must ship.

---

## [SEV: BLOCKER] [attendance] [app/modules/attendance/service.py:195-207]

**Issue:** The geofence is skipped entirely when GPS is omitted (prior F34, still open).

**Why it's wrong:** `ScanCheckIn.lat/lon` are `float | None` (`schemas.py:19-20`). With both
absent, the `else` branch requires only that `reason` be a non-empty string ≤200 chars
(`schemas.py:22`) — never validated, never reviewed by any code in the module. `distance_m` is
written `NULL` and the response carries `"location_unverified": True` (`service.py:222`), but
nothing acts on that flag: the row still satisfies `is_present_today` (`service.py:332-335`).
The entire geofence control is opt-out by the client, at the client's discretion.

**Correct behavior:** an unverified check-in must not count as present until a supervisor
confirms it.

**Fix sketch:** write the row with a `pending_review` flag and exclude
`distance_m IS NULL AND source = SELF` rows from `is_present_today` until approved.

**Primary:** as above. **Risk:** genuinely GPS-less devices need the supervisor path — that is
the point. **Fallback (2-day cut):** reject GPS-less SELF scans with 422 and require the
supervisor's proxy flow, which already carries coordinates (`service.py:234`). Two lines, no new
state.

---

## [SEV: HIGH] [core] [app/core/config.py:50-51,124,247-273]

**Issue:** An unconfigured deployment boots on the publicly-known JWT secret (prior F32, partially fixed).

**Why it's wrong:** The validator added at `:247-273` correctly rejects the shipped default
(`:124`, `"dev-only-insecure-change-me-in-prod"`) against `_INSECURE_SECRET_PREFIXES` (`:243-244`)
and enforces a 32-char minimum (`:268-273`). But `:256` exempts
`environment == "local" and debug`, and `environment` defaults to `"local"` (`:50`) with
`debug` defaulting to `True` (`:51`). A deploy that sets neither variable boots on the default
key with **no warning**. The same `debug` flag also triggers `Base.metadata.create_all`
(`main.py:144-147`), so the configuration that disables the secret guard also bypasses Alembic.

**Correct behavior:** the escape hatch should require an explicit opt-in, not a default.

**Fix sketch:** flip the defaults — `environment: str = "production"`, `debug: bool = False` —
so local development opts *in* via `.env` and production is safe by default.

**Primary:** flip the two defaults. **Risk:** every developer must add two lines to `.env`; the
repo's `.env` already sets both. **Fallback:** keep the defaults but log a `CRITICAL` line at
startup naming the insecure key — visible, not preventive.

---

## [SEV: HIGH] [attendance] [app/modules/attendance/service.py:255-262]

**Issue:** `proxy_check_out` trusts an arbitrary list of employee ids with no checks at all.

**Why it's wrong:** It loops straight into `_close(employee_id=emp_id)` with no existence check,
no `WageType.PIECE_RATE` restriction, and no scoping to the supervisor's own team — while its
sibling `proxy_mark_present` does check wage type (`:245-247`). Any SUPERVISOR can close out any
employee in the factory, including monthly staff and managers, writing `is_short`/`is_overtime`
onto their record (`:325`). Those flags feed the wage run.

**Correct behavior:** mirror `proxy_mark_present`'s checks, and bound the set to workers the
supervisor is responsible for.

**Fix sketch:** reuse the existence + `PIECE_RATE` validation loop from `:240-247` before
closing.

**Primary:** reuse the sibling's checks (D0 — it is a copy-paste). **Risk:** none. **Fallback:**
team-scoping needs a supervisor↔worker mapping that does not exist yet — defer that half to D2
and ship the wage-type check now.

---

## [SEV: HIGH] [users] [app/modules/users/service.py:102-121]

**Issue:** `body.employee_id` is never validated, so a second login can be minted for any worker.

**Why it's wrong:** `from_user_create` passes `employee_id=body.employee_id` straight through
with no check that the employee exists, is active, or already has a login. HR is permitted to
grant the `EMPLOYEE` role (`service.py:99`), so an HR account can create a second credential bound
to any existing `employee_id`. That login then passes `self_check_in` (`attendance/service.py:157-162`)
and `GET /attendance/me` as that worker — an undetectable identity graft, since both logins are
legitimate rows.

**Correct behavior:** one active login per employee, and the employee must exist.

**Fix sketch:** look up the employee and 409 if `User.employee_id` is already taken; add a
partial unique index on `app_user.employee_id WHERE employee_id IS NOT NULL`.

**Primary:** service check + DB constraint. **Risk:** the constraint needs a migration inside the
window; the service check alone is D0-shippable. **Fallback:** service check for Aug 2,
constraint D1.

---

## [SEV: MED] [users] [app/modules/users/schemas.py:51; service.py:120]

**Issue:** `must_change_password` is always `False` for admin-created logins.

**Why it's wrong:** `password: str` is declared **required** (no default) at `schemas.py:51`,
contradicting the comment on the same line and the module docstring (`service.py:14-18`) which
both say it defaults to the phone number when omitted. Because it can never be `None`,
`must_change_password = body.password is None` (`service.py:120`) is always `False` — so no
admin-provisioned account is ever flagged to rotate its initial password.

**Correct behavior:** either make it optional and default to the phone number as documented, or
set `must_change_password=True` unconditionally for admin-created logins.

**Fix sketch:** `password: str | None = None` and keep the existing derivation.

**Primary:** make it optional as documented. **Risk:** the frontend may already send a password —
harmless, the flag just becomes accurate. **Fallback:** hardcode `must_change_password=True`;
one line, achieves the security goal without a contract change.

---

## [SEV: MED] [analytics] [app/modules/analytics/router.py:49,62,72,81,109,117,127,135]

**Issue:** Seven of ten analytics endpoints carry no role gate (prior F57, still open).

**Why it's wrong:** Only `/overview` (`:38-41`) and `/employee-rates` (`:101-102`) use
`require_roles`. The rest depend on `client_scope` (`:23-32`), which is a **tenancy filter, not
an authorization check** — it returns `None` for every non-CLIENT role, i.e. unrestricted. So a
`LINING_MANAGER` or `SUPERVISOR` token reads the full order explorer, freight-risk alerts and
consumption-vs-stock. `block_employees` (`main.py:253`) only excludes `EMPLOYEE`.

**Correct behavior:** each endpoint declares the roles that need it.

**Fix sketch:** add the `/overview` role list as a module-level `_ANALYTICS_READERS` dependency
and apply it to the seven.

**Primary:** as above — one dependency, seven decorators. **Risk:** a role legitimately using a
dashboard gets a 403; verify the list against the frontend's role→screen map first.
**Fallback:** D1 — the data is internal-only (CLIENT is correctly scoped), so this is an
excessive-access issue, not a leak to outsiders.

---

## [SEV: MED] [core] [app/main.py:194-201]

**Issue:** CORS origins are hardcoded, including three `localhost` dev origins, with credentials allowed (prior F46).

**Why it's wrong:** Not read from `settings`, so a new frontend domain needs a code change and
redeploy. `allow_credentials=True` with `allow_methods=["*"]` and `allow_headers=["*"]` means any
of the four listed origins can drive the API with the user's cookies. `http://localhost:3000` is
trivially claimable on a developer's or a victim's machine.

**Fix sketch:** `allow_origins=settings.cors_origins` (a `list[str]` field), dev origins supplied
via `.env` only.

**Primary:** move to settings. **Risk:** a misconfigured env breaks the frontend — stage it.
**Fallback:** drop the three `localhost` entries from the production list for Aug 2; keep the
hardcoding. One-line diff, removes the exploitable origins.

---

## Rate limiting, in one place

There is **no HTTP rate limiting anywhere in `app/`** — no `slowapi`, no middleware, no per-route
throttle. The only limiter is the in-process login guard at `core/security.py:126-144`, keyed on
the submitted phone number, which `security.py:41` and `config.py:163` both acknowledge does not
survive multiple replicas and does not limit per source IP. `/imports/commit`
(`imports/router.py:119`) accepts unthrottled 25 MB uploads that each spawn a threadpool worker
doing a full workbook parse plus bulk write — the cheapest denial-of-service surface in the app.

**Primary:** `slowapi` with a Redis backend, strict limits on `/auth/login` and `/imports/commit`.
**Risk:** a new infrastructure dependency inside a 2-day window — not realistic.
**Fallback (recommended for Aug 2):** rate-limit at the reverse proxy / load balancer, which is
configuration rather than code, and record the in-app gap in the risk register.

## SQLi

**No finding.** Every query in the scoped modules is built from SQLAlchemy Core/ORM constructs;
a repo-wide grep for `text(` across the scoped modules returns one false positive
(`production/router.py:156`, `ScreenContext(body.screen_context.upper())`). Path traversal is
also correctly handled in `core/storage.py:119-129`.
