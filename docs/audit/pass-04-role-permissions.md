# Pass 4 — Role-permission audit

Every endpoint in the 11 scoped routers, with its actual guard. 54 endpoints.

**Two facts that qualify every row below:**
1. `require_roles` unconditionally admits `MANAGING_DIRECTOR` and `DIRECT_MANAGER`
   (`users/deps.py:69,96-98`) — every gate in the table is advisory for those two (prior F54).
2. `block_employees` (`users/deps.py:84-91`) rejects **only** `UserRole.EMPLOYEE`. It is a
   deny-list, so `CLIENT` and `VIEWER` pass into every "locked" router (prior F55).

Router lock state, from `main.py:237-259`: **open** — `auth`, `users`, `attendance`, `barcode`.
**locked** — `clients`, `employees`, `production`, `wages`, `analytics`, `imports`, `materials`,
`suppliers`, `drawers`.

---

## Matrix

| Module | Method | Path | Guard | Verdict |
|---|---|---|---|---|
| users | POST | `/auth/login` | none (rate-limited, `security.py:126`) | correct |
| users | GET | `/auth/me` | `get_current_user` | correct |
| users | POST | `/auth/change-password` | `get_current_user` | correct |
| users | GET | `/users` | DM, HR, MD | correct |
| users | POST | `/users` | DM, HR, MD + `_GRANTABLE` (`service.py:104-111`) | **`employee_id` unvalidated — pass-03** |
| users | POST | `/users/clients` | DM, MD | `client_id` unvalidated (prior F-, low impact) |
| clients | GET | `/clients` | `get_current_user` + inline | **no `require_roles`** |
| clients | POST | `/clients` | DIRECT_MANAGER | correct |
| clients | GET | `/clients/{id}/orders` | `get_current_user` + inline | **verify tenancy inline** |
| clients | POST | `/clients/{id}/orders` | DIRECT_MANAGER | correct |
| clients | GET | `/clients/styles` | `get_current_user` | **no `require_roles`** |
| employees | GET | `/employees` | `get_current_user` + inline deny CLIENT/VIEWER (`:32-35`) + pay split (`:39-40`) | correct (F37 closed) |
| employees | POST | `/employees` | DM, HR, MD | correct |
| attendance | POST | `/scan-check-in` | `get_current_user` + inline | **BOLA — pass-03 blocker** |
| attendance | POST | `/check-in` | `get_current_user` | correct (identity from token) |
| attendance | POST | `/check-out` | `get_current_user` | correct |
| attendance | POST | `/proxy/check-in` | SUPERVISOR, DM, HR, MD | correct (F50 closed) |
| attendance | POST | `/proxy/check-out` | SUPERVISOR, DM, HR, MD | **no per-target checks — pass-03** |
| attendance | POST | `/daily-workers` | DM, HR, MD | correct |
| attendance | GET | `/me`, `/me/status` | `get_current_user` | correct |
| attendance | GET | `/config` | `get_current_user` + shape split (`:143-150`) | **factory GPS readable by all (F44)** |
| attendance | PATCH | `/config` | DM, HR, MD | correct |
| attendance | GET | `/history` | `get_current_user` + inline (`:172-184`) | 7 roles may pass any `employee_id` |
| attendance | GET | `/today` | SUPERVISOR, HR, CUTTING/STITCHING/LINING mgr | correct (F35/F49 closed) |
| production | GET | `/operations` | `_FLOOR_READERS` | correct |
| production | GET | `/skus`, `/styles/{id}/progress`, `/skus/{id}/pieces` | `client_scope` only | **no role gate; and all three 500 — pass-01** |
| production | GET | `/events` | `_FLOOR_READERS` | correct |
| production | POST | `/log` | `get_current_user` only | **by design** — role gate is per-stage in-service (`service.py:107-127`) |
| production | POST | `/cutting`, `/scan` | — | 410 stubs, still registered (prior F95) |
| drawers | POST | `/store-scan` | `require_roles(...)` | correct |
| drawers | POST | `/{id}/receive` | `require_roles(...)` | correct |
| barcode | GET | `/resolve` | `get_current_user` | correct — intentionally open (`:36`) |
| barcode | POST | `/print` | `require_roles(...)` | correct |
| barcode | PATCH | `/employees/{id}/barcode` | DM, MD, HR | correct |
| materials | POST | `/lots` | `_LOT_WRITERS` (DM, MD, CUTTING) | **LINING_MANAGER cannot create lining lots** |
| materials | GET | `/spec`, `/stock` | `_STOCK_READERS` | correct |
| materials | POST | `/receive`, suppliers ×3 | `_DM` | correct |
| analytics | GET | `/overview` | 7 roles | correct |
| analytics | GET | `/employee-rates` | DM, MD, HR | correct |
| analytics | GET | 7 others | `client_scope` / `get_current_user` only | **no role gate — pass-03** |
| imports | POST | `/preview`, `/commit` | DIRECT_MANAGER | correct |
| wages | GET | `/styles`, `/rate-sheet`, `/rate-history` | `_RATE_READERS` (DM, MD, HR) | correct |
| wages | POST | `/rates`, `/rates/bulk` | DIRECT_MANAGER | correct |
| wages | GET | `/runs`, `/runs/{id}` | `_PAYROLL_READERS` | correct |
| wages | POST | `/runs` | DIRECT_MANAGER | correct |
| wages | POST | `/runs/{id}/recompute` | DM, MD | correct guard, dead route (pass-02) |

**Path shadowing:** none found. `GET /wages/runs` precedes `/runs/{run_id}`;
`/attendance/me` and `/me/status` are both static. The one cross-router hazard is
`employees_router` (`main.py:250`) and `barcode_emp_router` (`main.py:256`) both owning
`/employees…` six lines apart — no collision today, worth a regression test.

---

## [SEV: MED] [materials] [app/modules/materials/router.py:30-31]

**Issue:** `LINING_MANAGER` cannot create the lining lots the role exists to consume.

**Why it's wrong:** `_LOT_WRITERS = require_roles(DIRECT_MANAGER, MANAGING_DIRECTOR, CUTTING_MANAGER)`.
Prior F52 was recorded as fixed because the role list was widened — but it was widened to
`CUTTING_MANAGER`, not `LINING_MANAGER`. The lining manager logs `LINING_CUTTING`
(`enums_barcode.py:164-174`) and must supply a `lining_lot_id` at that stage
(`production/service.py:279-290`), yet cannot create one.

**Correct behavior:** `LINING_MANAGER` may create `LINING` lots.

**Fix sketch:** add `UserRole.LINING_MANAGER` to `_LOT_WRITERS` and assert category `== LINING`
for that role inside `create_lot`.

**Primary:** as above. **Risk:** a lining manager could create leather lots without the category
assertion — include it. **Fallback (Aug 2):** DM creates all lots; document it as an operating
constraint. Zero code, real friction on the floor.

---

## [SEV: MED] [clients] [app/modules/clients/router.py:26,56,85]

**Issue:** Three client endpoints have no `require_roles`, relying on inline checks.

**Why it's wrong:** `GET /clients`, `GET /clients/{id}/orders` and `GET /clients/styles` declare
only `get_current_user`. `clients_router` is `_LOCKED`, which excludes `EMPLOYEE` and nothing
else — so `VIEWER` and every manager role reach the full client list. Inline checks inside the
handlers are the only control, and they are not visible in the OpenAPI contract.

**Correct behavior:** declare the role set as a dependency so it is enforced and documented in
one place.

**Fix sketch:** define `_CLIENT_READERS = require_roles(...)` and apply, keeping the tenancy
predicate for CLIENT tokens.

**Primary:** as above. **Risk:** low. **Fallback:** D1 — the inline checks do work today; this is
consistency and contract-visibility debt.

---

## [SEV: MED] [core] [app/modules/users/deps.py:69,84-91]

**Issue:** Two structural weaknesses make the whole matrix softer than it reads (prior F54, F55).

**Why it's wrong:** `SUPERUSER_ROLES` bypasses every `require_roles` gate — reasonable for MD,
questionable for DM, who is a working designer with an operational day job. And `block_employees`
is a **deny-list**: any role added to `UserRole` in future is admitted to every locked router by
default. Both were reported; both stand.

**Fix sketch:** invert `block_employees` to an allow-list of roles that may reach manager
routers; make the DM bypass opt-in per route rather than global.

**Primary:** allow-list. **Risk:** touches every router — not a 2-day change. **Fallback (Aug 2):**
add a unit test asserting the exact set of roles admitted by `block_employees`, so a new role
cannot be added silently. Cheap, and it converts a latent hole into a failing test.
