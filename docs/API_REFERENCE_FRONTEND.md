# KairoX ERP — API Reference for Frontend

**Phase 1 · 12 modules · 58 endpoints · generated against the live OpenAPI schema**

This is the document to build the frontend from. Every endpoint the Phase-1 UI needs
is here with its exact request shape, exact response shape, who may call it, what can
go wrong, and what the screen should do with the result.

It is also the backend developer's cross-check: if the code and this document
disagree, one of them is a bug.

| | |
|---|---|
| **Base URL** | `{HOST}/api/v1` |
| **Auth** | `Authorization: Bearer <access_token>` on everything except login and `/health` |
| **Content type** | `application/json`, except the two import endpoints (`multipart/form-data`) |
| **Interactive** | `GET /docs` (Swagger) · `GET /redoc` |
| **Companion** | `docs/PRODUCTION_FLOW.md` — why any of this exists |

Notation: `*` = required · `?` = nullable/optional · `uuid` = UUID string ·
`date` = `YYYY-MM-DD` · `datetime` = ISO-8601.

---

## ⚠️ Read this before you start building

**Six endpoints are currently broken or behave unexpectedly.** They are documented
below with their intended contract so you can build the screens, but **do not treat a
failure from these as your bug**. Full detail in `AUDIT_SUMMARY.md`.

| Endpoint | What happens today | Build the screen? |
|---|---|---|
| `POST /attendance/scan-check-in` | **500 on every call** — the route reads a `direction` field the schema no longer declares | Yes — contract below is correct |
| `GET /production/skus` | **500** — router/service signature mismatch | Yes |
| `GET /production/skus/{sku_id}/pieces` | **500** — same cause | Yes |
| `GET /production/styles/{style_id}/progress` | **500** — same cause | Yes |
| `POST /wages/runs` | Fails for any run containing piece-rate work | Yes |
| `POST /wages/runs/{run_id}/recompute` | **Always 409.** The `confirm_closed` override is not wired to any route | Show the 409 message; there is no override to build |
| `POST /imports/commit` (re-run) | **500** on a second commit of the same order | Yes — but do not offer "re-upload" as a happy path yet |

---

## Conventions

### Standard errors

Every endpoint can return these. Handle them centrally, not per-call.

| Code | Meaning | What the UI should do |
|---|---|---|
| **401** | Missing, expired or invalid token | Redirect to login |
| **403** | Authenticated, but this role may not do this | Hide the control; if it was shown, show the message |
| **404** | Not found — **or** a tenancy miss for a `client` token | "Not found" |
| **409** | Conflict — a state rule was violated | Show the message verbatim; it explains the rule |
| **410** | Gone — a retired barcode | "This card was replaced/retired" |
| **413** | Upload too large | Show the size cap |
| **422** | Validation failed | Field-level errors |
| **500** | Server error | Show the `request_id` from the body — support needs it |

**409 and 422 messages are written for humans.** Render them directly rather than
substituting your own — they name the exact rule, quantity or drawer involved.

### Roles

`managing_director` · `direct_manager` · `cutting_manager` · `lining_manager` ·
`stitching_manager` · `supervisor` · `hr` · `employee` · `client` · `viewer`

Two rules that affect every screen:

- **MD and DM bypass every role gate.** A permission test done as the MD proves nothing.
- **`employee` tokens are blocked from every manager router.** An employee can reach
  only: login, their own attendance, and `/barcode/resolve`.

### Tenancy

A `client` token is automatically scoped to its own `client_id`. Requesting another
buyer's order returns **404, not 403** — deliberately, so the response does not
confirm the resource exists.

---

# 1 · Authentication — `users`

## `POST /auth/login`

**Auth:** none · **Rate-limited** per phone number

```json
{ "username": "9000000001", "password": "secret" }
```

> **`username` is the phone number.** Not an email. Label the field "Phone".

**200**
```json
{ "access_token": "eyJhbGciOi…", "token_type": "bearer",
  "role": "direct_manager", "name": "Priya",
  "user_id": "8f14e45f-…", "must_change_password": false }
```

**Frontend:** the response carries `role` and `name`, so route to the correct home
screen immediately — no follow-up `/auth/me` needed. Store the token; send it on
everything after this.

`must_change_password` is currently always `false` (known defect) — do not depend on
it to force a password rotation.

**Errors:** `401` bad credentials · `429`-ish behaviour after repeated failures.

---

## `GET /auth/me`

**Auth:** any logged-in user

**200**
```json
{ "id": "uuid", "name": "Priya", "phone": "9000000001", "email": "p@f.com",
  "role": "direct_manager", "is_active": true,
  "employee_id": "uuid | null", "client_id": "uuid | null" }
```

**Frontend:** call on app boot to re-hydrate a stored session.
`employee_id` tells you whether this login can check itself in — if it is `null`, hide
the attendance screen. `client_id` marks a tenant-scoped user.

---

## `POST /auth/change-password`

**Auth:** any logged-in user · **204 No Content** on success

```json
{ "current_password": "old", "new_password": "new" }
```

**Errors:** `400` current password wrong.

> Note: changing a password does **not** invalidate existing tokens.

---

## `GET /users`

**Auth:** HR · DM · MD

**Query:** `active_only` `bool` (default `true`)

**200** — array of the `GET /auth/me` shape.

---

## `POST /users`

**Auth:** HR · DM · MD · **201**

```json
{ "name*": "string", "phone*": "string", "email?": "string",
  "role": "employee", "password*": "string", "employee_id?": "uuid" }
```

**200/201** — the created user.

**Frontend:** `role` is restricted by *your* role — HR cannot mint an MD. A refused
grant returns **403**; render the message. Pass `employee_id` to bind the login to an
existing floor worker.

> `password` is currently **required** despite the API description suggesting it
> defaults to the phone number. Always send it.

---

## `POST /users/clients`

**Auth:** DM · MD · **201** — creates a client-portal login bound to a `client_id`.

---

# 2 · Clients, orders, styles — `clients`

## `GET /clients`

**Auth:** any manager role (not `employee`)

**200**
```json
[ { "id": "uuid", "name": "John Peter", "country": "Italy",
    "code": "JP", "currency": "EUR", "default_size_system": "EU" } ]
```

> **No pagination.** Returns every client. Fine at current scale; paginate client-side.

---

## `POST /clients`

**Auth:** DIRECT_MANAGER · **201**

```json
{ "name*": "John Peter", "country?": "Italy", "order_number*": "JP-2026-014" }
```

**201**
```json
{ "id": "uuid", "name": "John Peter", "country": "Italy", "code": "JP",
  "currency": null, "default_size_system": null,
  "order_number": "JP-2026-014", "order_id": "uuid" }
```

**Frontend:** this creates the client **and their first order** in one call — the form
must collect an order number. The returned `order_id` is what you feed to the import
screen next.

**Errors:** `409` if `order_number` is already used **by any client** — order numbers
are globally unique, not per-buyer.

---

## `GET /clients/{client_id}/orders`

**Auth:** any manager role

**200**
```json
[ { "id": "uuid", "order_number": "JP-2026-014",
    "order_date": "2026-05-01", "delivery_deadline": "2026-08-15",
    "sea_cutoff_date": "2026-07-15", "ship_mode": "sea",
    "currency": "EUR", "agent": null, "line": null,
    "styles": [ { "id": "uuid", "name": "CLERMONT", "article": "CL1",
                  "gender": null, "label": null, "thickness": null,
                  "skus": [ … ] } ] } ]
```

**Frontend:** returns the **full nested tree** — order → styles → SKUs — eagerly, with
no depth or page limit. On a large order this is a heavy payload; fetch once and cache.

---

## `POST /clients/{client_id}/orders`

**Auth:** DIRECT_MANAGER · **201**

```json
{ "order_number*": "JP-2026-015", "order_date?": "2026-05-01",
  "delivery_deadline?": "2026-08-15", "sea_cutoff_date?": "2026-07-15",
  "ship_mode": "sea", "currency?": "EUR", "agent?": null, "line?": null }
```

`sea_cutoff_date` drives the freight-risk alert — collect it.

---

## `GET /clients/styles`

**Auth:** any manager role · **Query:** `order_number?` · `client_id?`

**200**
```json
[ { "style_id": "uuid", "style_name": "CLERMONT", "article": "CL1",
    "order_number": "JP-2026-014", "sku_count": 2, "qty_ordered": 50 } ]
```

**Frontend:** the style picker. Note it **silently omits styles with no `code`** — if a
style is missing here, that is why.

---

# 3 · Employees — `employees`

## `GET /employees`

**Auth:** any manager role. **CLIENT and VIEWER get 403.**

**Query:** `active_only` `bool` (default `true`)

**200 — HR / DM / MD**
```json
[ { "id": "uuid", "name": "RAMESH", "designation": "CUTTER",
    "wage_type": "piece_rate", "is_active": true,
    "phone": "91…", "email": null, "monthly_salary": 30000.0 } ]
```

**200 — every other role: identical, but `monthly_salary` is absent.**

> **Build the table off the keys you receive, not a fixed column list.** Rendering a
> "Salary" column unconditionally produces an empty column for supervisors.

---

## `POST /employees`

**Auth:** HR · DM · MD · **201**

```json
{ "name*": "RAMESH", "designation?": "CUTTER",
  "wage_type": "piece_rate", "monthly_salary?": null,
  "phone?": "91…", "email?": null, "password?": null }
```

**201**
```json
{ "id": "uuid", "name": "RAMESH", "designation": "CUTTER",
  "wage_type": "piece_rate", "is_active": true,
  "phone": "91…", "email": null,
  "user_created": false, "login_phone": null,
  "employee_barcode": "EMP-000042" }
```

**Three behaviours the form must account for:**

1. **`employee_barcode` comes back in this response and nowhere else.** Print the card
   immediately — show a print/download action on the success state.
2. **`wage_type: "monthly"` auto-provisions a login.** `user_created` becomes `true`
   and `login_phone` is set, so `phone` and `password` become required for that path.
   Make the form react to the wage-type toggle.
3. **Designations are UPPERCASE** and drive the skill gate. Offer a dropdown, not a
   free-text box: `CUTTER`, `LINING_CUTTER`, `FUSER`, `PASTER`, `TAILOR`,
   `LINE_TAILOR`, `SHELL_TAILOR`, `FINISHER`, `INSPECTOR`, `PACKER`, plus
   `HELPER`/`SUPERVISOR`/`OTHER` which may work any stage.

Duplicate names are accepted and disambiguated with an `IN-CHAL` prefix.

---

# 4 · Attendance — `attendance`

The gate in front of production. **A worker with no attendance row today cannot have
production logged against them.**

## `POST /attendance/check-in`

**Auth:** any logged-in user with an `employee_id` · **201**

```json
{ "lat*": 12.9716, "lon*": 77.5946 }
```

**201**
```json
{ "id": "uuid", "employee_id": "uuid", "work_date": "2026-05-12",
  "check_in_at": "2026-05-12T09:03:00+05:30", "check_out_at": null,
  "source": "self", "is_late": false, "is_short": false,
  "is_overtime": false, "distance_m": 7.4 }
```

**Frontend:**
- **Only lat/lon are sent.** Identity comes from the token and the timestamp is set
  server-side — never send an employee id or a time.
- Request GPS permission before showing the button, and surface a clear "getting your
  location" state. `lat`/`lon` are **mandatory**; there is no way to check in without
  them on this route.
- **Re-tapping is a safe no-op** — the same row comes back with the original
  `check_in_at`. You may let users press it twice without warning them.

**Errors:**
- **403** outside the geofence — *the message names the required radius, show it.*
- **400** this login is not linked to an employee record.

---

## `POST /attendance/check-out`

Same body and response as check-in. **400/404** if there is no open check-in today.

---

## `POST /attendance/scan-check-in`

**Auth:** any logged-in user · ⚠️ **Currently 500s on every call**

```json
{ "employee_barcode*": "EMP-000042",
  "lat?": 12.9716, "lon?": 77.5946,
  "proxy": false, "reason?": "indoor, no GPS fix" }
```

**200**
```json
{ "employee_id": "uuid", "employee_name": "RAMESH",
  "work_date": "2026-05-12", "check_in_at": "…", "check_out_at": null,
  "is_late": false, "present_today": true, "location_unverified": false }
```

**Frontend:** the gate-terminal door. `lat`/`lon` are optional here — but if you omit
them you **must** send a non-empty `reason`, or you get 422. When GPS is missing the
response sets `location_unverified: true`; **badge that row in the UI** so a supervisor
can see it was not position-checked.

**Errors:** `422` no GPS and no reason · `422` a proxy scan without GPS ·
`403` an employee scanning someone else's card · `404`/`410` unknown or retired card.

---

## `POST /attendance/proxy/check-in`

**Auth:** SUPERVISOR · DM · HR · MD · **201**

```json
{ "employee_ids*": ["uuid", "uuid"], "lat*": 12.9716, "lon*": 77.5946 }
```

**201** — array of attendance rows, `source: "proxy"`.

**Frontend:** for daily-wage workers who have no login. The GPS sent is the
**supervisor's** device position, checked once for the whole batch.

**Errors:** **400** if any id is not a `piece_rate` worker — the message names them.
Monthly staff have logins and must check themselves in. **403** outside the fence.

---

## `POST /attendance/proxy/check-out`

Same shape, **200**. No wage-type restriction is applied on this route.

---

## `POST /attendance/daily-workers`

**Auth:** DM · HR · MD · **201** — onboard a daily-wage worker inline.

```json
{ "name*": "SURESH", "phone*": "91…", "designation*": "CUTTER", "daily_rate?": 500 }
```

---

## `GET /attendance/me` · `GET /attendance/me/status`

**Auth:** any logged-in user.

`/me` — **Query** `start?` `end?` → array of attendance rows.

`/me/status` — **200**
```json
{ "server_now": "2026-05-12T14:22:00+05:30", "timezone": "Asia/Kolkata",
  "shift_length_hours": 8.0, "checked_in": true, "checked_out": false,
  "check_in_at": "2026-05-12T09:03:00+05:30",
  "shift_end_at": "2026-05-12T17:03:00+05:30", "remaining_seconds": 9660 }
```

**Frontend:** everything for the worker's home screen. Drive a countdown from
`remaining_seconds` and **anchor it to `server_now`, not the device clock** — that is
why `server_now` is returned.

---

## `GET /attendance/history`

**Auth:** any logged-in user · **Query:** `start*` `end*` `employee_id?`

An `employee` token is forced to its own id regardless of what it sends. HR, DM, MD,
supervisors and floor managers may pass any `employee_id`.

---

## `GET /attendance/today`

**Auth:** SUPERVISOR · HR · CUTTING/STITCHING/LINING managers → today's floor roster.

---

## `GET /attendance/config`

**Auth:** any logged-in user — **the response shape depends on your role.**

**Privileged** (DM, MD, HR, SUPERVISOR, floor managers):
```json
{ "shift_start": "09:00", "shift_length_hours": 8.0, "late_grace_minutes": 15,
  "timezone": "Asia/Kolkata", "factory_lat": 12.9716, "factory_lon": 77.5946,
  "radius_m": 100 }
```

**Everyone else:** the same **without** `factory_lat`, `factory_lon`, `radius_m` — so a
client or worker login cannot read the coordinates needed to forge an in-fence check-in.

**Frontend:** never assume the fence fields are present.

---

## `PATCH /attendance/config`

**Auth:** DM · HR · MD — all fields optional; send only what changes.

```json
{ "shift_start?": "09:00", "shift_length_hours?": 8.0, "late_grace_minutes?": 15,
  "timezone?": "Asia/Kolkata", "factory_lat?": 12.9716,
  "factory_lon?": 77.5946, "radius_m?": 100 }
```

> ⚠️ **This is the most important setup screen in the app.** `factory_lat` and
> `factory_lon` default to `0.0`, which puts the geofence in the Atlantic Ocean. Until
> a DM sets real coordinates, **every check-in fails and the whole factory is blocked
> from logging production.** Put this in the first-run wizard, and consider showing a
> banner while the coordinates are still `0,0`.

---

# 5 · Barcode — `barcode`

## `GET /barcode/resolve`

**Auth:** any logged-in user — **including `employee`.** This is the one manager-ish
route deliberately left open, so a worker can scan their own card.

**Query:** `code*` `string`

**200**
```json
{ "code": "JP-2026-014-CLERMONT-PINE-M-007", "type": "PIECE", "active": true,
  "caption": "CLERMONT · PINE GREEN · M",
  "piece":    { … }, "employee": null, "drawer": null, "lot": null }
```

`type` ∈ `PIECE` · `EMPLOYEE` · `DRAWER` · `LEATHER_LOT` · `LINING_LOT` · `ACCESSORY_LOT`.
**Exactly one** of the four payload objects is populated; the rest are `null`.

**Frontend:** this is the universal scan handler. Switch on `type` and route to the
right screen. Codes are normalised, so case and surrounding whitespace do not matter.

**Errors:**
- **404** — code never existed → "Not one of our barcodes"
- **410** — retired → "This card was replaced" — **a different message from 404**, and
  users notice the difference

---

## `POST /barcode/print`

**Auth:** manager roles · **200**

```json
{ "codes?": ["…"], "sku_id?": "uuid", "order_id?": "uuid" }
```

Send **one** of the three.

**200**
```json
{ "labels": [ { "code": "JP-…-M-007", "symbology": "CODE128",
                "caption": "CLERMONT · PINE GREEN · M", "known": true } ] }
```

**Frontend:** render to a label sheet. `symbology` tells your barcode library which
encoder to use. `known: false` means the registry has no row for that code — mark it
visually rather than printing a label nobody can scan.

---

## `PATCH /employees/{employee_id}/barcode`

**Auth:** DM · MD · HR · **200**

```json
{ "action*": "reissue" }        // "reissue" | "deactivate"
```

**200**
```json
{ "employee_id": "uuid", "employee_barcode": "EMP-000103",
  "active": true, "history_preserved": true }
```

**Frontend:** two very different actions behind one endpoint — give them **separate
buttons with separate confirmations**.

- **Reissue** — card lost or damaged. Old code starts returning 410; a new code is
  minted. Show the new code and offer to print.
- **Deactivate** — worker has left. The card stops scanning. `history_preserved` is
  `true`: their production events and wage lines are untouched. **Say so in the
  confirmation dialog** — this is the fear users have.

---

# 6 · Imports (breakdown upload) — `imports`

`multipart/form-data`, not JSON.

## `POST /imports/preview`

**Auth:** DIRECT_MANAGER

**Form fields:** `order_number*` `string` · `file*` `.xlsx`/`.xlsm`

**200** — a parse summary: styles, colours, sizes and quantities found. **Writes
nothing.**

**Frontend:** always preview before commit. Render the summary as a confirmation table
next to the totals so the DM can check it against the paper sheet.

**Errors:** `400` not an `.xlsx`/`.xlsm`, or not a real workbook (a renamed PDF is
caught) · `413` over the size cap (default 25 MB) · `404` unknown order number.

---

## `POST /imports/commit`

**Auth:** DIRECT_MANAGER · same form fields

**200** — a summary including the counts of pieces and drawers created.

**This is the single biggest write in the system.** For a 50-piece order it creates 50
pieces, 50 drawers and 100 barcodes in one transaction.

**Frontend:**
- Show a **blocking progress state**. This is slow and must not be double-submitted.
- On success, take the user straight to the **print-labels** screen — 50 physical tags
  are now needed before anything can be cut.
- The file must be uploaded **again**; preview and commit are independent requests.

> ⚠️ **Committing the same order twice currently returns 500.** Do not build a
> "re-upload corrected sheet" flow against this yet.

---

# 7 · Materials — `materials`

## `GET /materials/spec`

**Auth:** stock readers (DM, MD, HR, cutting/stitching managers)

**Query:** `category*` (`LEATHER`|`LINING`|`ACCESSORY`) · `subtype?`

**200**
```json
{ "category": "LEATHER", "subtype": null,
  "filters": ["article", "colour", "thickness"],
  "required_to_add": ["dcm", "thickness"],
  "quantity_field": "dcm", "uom": "dcm" }
```

**Frontend: call this first and build the form from it.** Do not hardcode material
fields — the required set differs per category *and* subtype, and the backend rejects
a mismatch with 422.

- `filters` → which search boxes to render on the stock screen
- `required_to_add` → which inputs the Add-Lot form must show and require
- `quantity_field` + `uom` → the quantity input's name and unit label

---

## `GET /materials/stock`

**Auth:** stock readers

**Query:** `category?` `subtype?` `article?` `colour?` `thickness?` `size?` `required?`

**200**
```json
{ "category": "LEATHER", "subtype": null, "article": "SUEDE-A32",
  "colour": "PINE GREEN", "thickness": "1.2mm", "size": null,
  "uom": "dcm", "on_hand": 12000.0, "reserved": 800.0, "available": 11200.0,
  "lot_count": 3,
  "required": 15000.0, "short_by": 3800.0,
  "suggested_supplier": { "id": "uuid", "name": "Chennai Leathers" } }
```

**Frontend:** the three numbers mean different things and should be labelled
distinctly — **On hand** (physically here), **Reserved** (committed elsewhere),
**Available** (`on_hand − reserved`, what you can actually spend).

Pass `required` to run a shortfall check. When `short_by > 0`, show the deficit and
surface `suggested_supplier` as a one-click "Order from…" action. When `required` is
omitted, `short_by` and `suggested_supplier` are `null`.

---

## `POST /materials/lots`

**Auth:** DM · MD · CUTTING_MANAGER · **201**

```json
{ "category*": "LEATHER", "subtype?": null, "article*": "SUEDE-A32",
  "colour?": "PINE GREEN",
  "attributes": { "thickness": "1.2mm", "dcm": 12000 },
  "supplier_id?": "uuid" }
```

**201**
```json
{ "lot_id": "uuid", "lot_barcode": "LOT-LEA-000001",
  "category": "LEATHER", "subtype": null, "article": "SUEDE-A32",
  "colour": "PINE GREEN", "on_hand": 12000.0, "reserved": 0.0,
  "available": 12000.0, "uom": "dcm" }
```

**Frontend:** the category-specific fields go **inside `attributes`**, keyed exactly as
`required_to_add` listed them. `lot_barcode` comes back only here — offer to print it.

> ⚠️ **LINING_MANAGER cannot currently create lots** (403), even though the role must
> supply a lining lot when logging lining cuts. Have a DM create lining lots for now.

**Errors:** **422** naming the exact missing fields · **422** quantity ≤ 0.

---

## `POST /materials/receive`

**Auth:** DM · MD · **200**

```json
{ "lot_id*": "uuid", "supplier_order_id?": "uuid",
  "approved_qty*": 5000, "rejected_qty": 200,
  "reserve_for_required?": 3000 }
```

**200**
```json
{ "lot_id": "uuid", "on_hand": 17000.0, "reserved": 3000.0,
  "available": 14000.0, "rejected_logged": 200.0,
  "supplier_order_status": "ARRIVED" }
```

**Frontend:** approved and rejected are **captured separately** — the form needs both
boxes. Only `approved_qty` is added to stock; `rejected_qty` is logged against the
supplier's quality history. `reserve_for_required` ring-fences the quantity for a job
so it cannot be spent elsewhere.

---

## `POST /suppliers/orders` · `PATCH /suppliers/orders/{order_id}`

**Auth:** DM · MD

**Create — 201**
```json
{ "category*": "LEATHER", "subtype?": null, "article*": "SUEDE-A32",
  "colour?": "PINE GREEN", "qty*": 5000, "supplier_id?": "uuid" }
```
→ `{ "order_id": "uuid", "status": "ORDERED", "article": "…", "qty": 5000,
     "uom": "dcm", "supplier": { … } }`

**Update — 200** — `{ "status*": "ARRIVED" }` → `{ "order_id", "status", "arrived_at" }`

Lifecycle: **ORDERED → ARRIVED → (receive)**. Marking arrived is idempotent.

---

# 8 · Production — `production`

**The most important module for the floor UI.**

## `POST /production/log`

**Auth:** any logged-in user — **the role gate is per-stage, inside the service**

**This one endpoint replaces every per-stage endpoint.** The caller **never sends a
stage**.

```json
{ "screen_context": "LEATHER_CUT",
  "actor":   { "employee_barcode": "EMP-000042" },
  "targets": { "piece_barcodes": ["JP-…-M-001", "JP-…-M-002"] },
  "work_date*": "2026-05-12",
  "consumption": { "leather_lot_id": "uuid", "dcm": 80 } }
```

**`screen_context`** — `LEATHER_CUT` · `LINING_CUT` · `PIPELINE`

- The two cut screens **fix** the stage and require `consumption`.
- `PIPELINE` **infers** each piece's stage from its own history — a mixed tray each
  advances to whatever it needed next.

**`actor`** — send `employee_barcode` **or** `employee_id`.
**`targets`** — send `piece_barcodes` **or** `sku_id` + `piece_seqs`.

The barcode door and the manual door post the **same shape**; build one component.

**201**
```json
{ "stage": "LEATHER_CUTTING",
  "count_logged": 9,
  "logged":           ["JP-…-M-001", "…-002", … ],
  "rework":           ["JP-…-M-010"],
  "not_found":        [],
  "sequence_blocked": [],
  "skill_blocked":    ["JP-…-M-007: PADMA is a PASTER and may not work LEATHER_CUTTING. Allowed: CUTTER."],
  "merge_blocked":    [],
  "screen_role_warning": null,
  "consumption_recorded": { "lot_id": "uuid", "qty_total": 720.0, "pieces_consuming": 9 } }
```

### The response is the whole UX — render all six lists

A partial success is the **normal** case, not an error.

| Field | Meaning | Show as |
|---|---|---|
| `logged` | Accepted | ✅ green |
| `rework` | Accepted, already had this stage | ℹ️ blue — **consumed no new material** |
| `not_found` | Unknown code, or nothing left to do | ⚠️ grey |
| `sequence_blocked` | Previous stage not done | ❌ red, with the message |
| `skill_blocked` | Worker's designation is wrong for this stage | ❌ red, with the message |
| `merge_blocked` | Drawer not `SENDED` | ❌ red, with the message |
| `screen_role_warning` | Wrong screen for your role — **a warning, not a block** | ⚠️ toast |

> **Do not show only `count_logged`.** "9 of 10 logged" with no reason is the single
> worst thing this screen can do. Every blocked entry is a pre-formatted sentence
> naming the piece and the rule — render them as a list.

**Consumption units:** `dcm` is **per piece** and is multiplied by the number of
first-time cuts. Label the input "dcm **per piece**".

**Errors:**
- **403** — your role may not log this stage → **the entire batch is refused.** Nothing
  is written. This is the only whole-request failure.
- **400** — the worker is not checked in today → deep-link to attendance.
- **422** — a cut mixed with non-cut stages in one batch.
- **422** — cutting without `consumption` or without a lot.

---

## `GET /production/operations`

**Auth:** floor readers · **200** — the operation vocabulary: `id`, `code`, `label`,
`sequence`. Use `code` to map to stage names; use `id` where an endpoint wants an
`operation_id`.

---

## `GET /production/events`

**Auth:** floor readers · **Query:** `sku_id?` `employee_id?` `start?` `end?`

**200** — raw production events.

> ⚠️ This endpoint declares no response schema, so its exact shape is not guaranteed
> by the contract. Treat the fields defensively.

---

## `GET /production/skus` ⚠️ · `GET /production/skus/{sku_id}/pieces` ⚠️ · `GET /production/styles/{style_id}/progress` ⚠️

**All three currently return 500** (router/service signature mismatch). Intended
contracts:

**`/skus`** — Query `order_id?` `style_id?` → SKU options for a picker.

**`/skus/{sku_id}/pieces`** — Query `operation_id?` →
```json
{ "operation_code": "FUSING", "total": 30, "done": 12, "pending": 18,
  "blocked": 2,
  "pieces": [ { "code": "JP-…-M-001", "seq": 1, "done": true, "eligible": true } ] }
```
The manual-door piece picker. `eligible: false` means a gate would refuse it — grey it
out **before** the manager scans.

**`/styles/{style_id}/progress`** —
```json
{ "LEATHER_CUTTING": 50, "FUSING": 38, "PASTING": 20, "LINE_STITCHING": 12 }
```
A stage → count map. Renders directly as a funnel bar.

## `POST /production/cutting` · `POST /production/scan`

**410 Gone.** Retired pre-barcode endpoints. Use `POST /production/log`.

---

# 9 · Drawers — `drawers`

## `POST /drawers/store-scan`

**Auth:** store-scan roles · **200**

```json
{ "drawer_barcode?": "DRW-0007", "drawer_id?": null,
  "piece_barcode?": "JP-…-M-007", "piece_id?": null,
  "part*": "LEATHER" }
```

`part` ∈ `LEATHER` · `LINING`

**200**
```json
{ "drawer_code": "DRW-0007", "piece_code": "JP-…-M-007",
  "state": "holding_leather", "needs_lining": true,
  "awaiting": ["LINING"], "ready_for_received": false }
```

**Frontend: scan the drawer FIRST, then the piece.** Build the two-step scanner in
that order — it matches how people work at the rack.

Drive the UI from the response: `awaiting` lists what is still missing, and
`ready_for_received` tells you whether to enable the DM's RECEIVED action.

**Errors:** **409** — that piece does not belong in that drawer. **Show this loudly.**
The merge map is the authority, and this is what prevents a jacket being assembled
from two different garments' parts. **409** also for scanning into a drawer already
`RECEIVED` or `SENDED`.

---

## `POST /drawers/{drawer_id}/receive`

**Auth:** DM · MD · **200**

```json
{ "transition*": "RECEIVED" }      // "RECEIVED" | "SENDED"
```

**200** — `{ "drawer_code": "DRW-0007", "piece_code": "JP-…-M-007", "state": "sended" }`

**Frontend:** a **two-step** release, in order.

1. **RECEIVED** — requires completeness. Refused **409** if parts are missing.
2. **SENDED** — requires RECEIVED. Refused **409** otherwise.

Only after `SENDED` can the piece be line-stitched. Backward transitions are **409**.

**States:** `waiting` · `merged` · `holding_leather` · `holding_lining` ·
`holding_both` · `received` · `sended`. The drawer **code never changes**; the state
recycles to `waiting` after the piece is exported.

---

# 10 · Wages — `wages`

**Visible to HR, DM and MD only.** Every other role gets 403.

## `GET /wages/styles`

**Auth:** DM · MD · HR · **Query:** `order_number?` `client_id?`

**200**
```json
[ { "style_code": "CLERMONT", "style_name": "CLERMONT", "article": "CL1",
    "order_number": "JP-2026-014", "sku_count": 2, "qty_ordered": 50,
    "rated_operations": 5, "total_operations": 9, "fully_rated": false } ]
```

**Frontend:** `fully_rated: false` means work will be logged with **no rate** and will
not be paid. Badge these styles — this is the most useful warning on the rates screen.

---

## `GET /wages/rate-sheet`

**Auth:** DM · MD · HR · **200**

```json
{ "style_code": "CLERMONT", "style_name": "CLERMONT",
  "order_number": "JP-2026-014", "on": "2026-05-12",
  "operations": [ … ], "missing_rate_count": 4 }
```

`on` is the as-of date — rates are date-effective, so the sheet is always "as of" a day.
Surface `missing_rate_count` prominently.

---

## `GET /wages/rate-history`

**Auth:** DM · MD · HR — every rate ever set for a style × operation with effective
dates. Render as a timeline; a new rate **adds** a row, it does not overwrite.

---

## `POST /wages/rates` · `POST /wages/rates/bulk`

**Auth:** DIRECT_MANAGER only (HR gets 403)

```json
{ "style_code*": "CLERMONT", "operation_code*": "LEATHER_CUTTING",
  "rate*": 12.5, "effective_from*": "2026-05-01" }
```

`bulk` takes an array of the same.

**Frontend:** `effective_from` is **required** and is the whole point — a rate change
does not reprice work already done. Default the picker to today and explain that in
helper text.

---

## `GET /wages/runs`

**Auth:** DM · MD · HR

**200** — array of:
```json
{ "id": "uuid", "period_start": "2026-05-01", "period_end": "2026-05-14",
  "status": "closed", "total_amount": 184320.50, "total_pieces": 1204,
  "employee_count": 23, "unrated_operations": [], "gap_days": 0,
  "recomputed": false, "recompute_count": 0 }
```

`gap_days` is the number of uncovered days since the last run — **show it**, it means
somebody's work is not in any payroll period.

---

## `POST /wages/runs`

**Auth:** DIRECT_MANAGER · **201**

```json
{ "period_start*": "2026-05-01", "period_end*": "2026-05-14" }
```

**201** — the run summary plus `lines`.

**Computing a run CLOSES it immediately.** There is no draft state.

**Frontend:** this is the most consequential button in the app.
- Require an explicit confirmation naming the window.
- Show a blocking progress state; never allow a double-submit.
- After success, the run is **frozen** — surface that plainly.
- Render `unrated_operations` and `excluded_untyped_employees` as warnings: that is
  work that was **not paid**.

**Errors:**
- **422** end before start · **422** a window ending in the future
- **409** the window overlaps another run — including one that died half-finished. The
  message names the clashing run. **Do not offer a force option; there isn't one.**

> ⚠️ Currently fails for any run containing piece-rate work, and can 500 *after*
> closing if an active monthly employee has a blank salary. Validate salaries first.

---

## `GET /wages/runs/{run_id}`

**Auth:** DM · MD · HR · **200**

```json
{ "id": "uuid", "period_start": "…", "period_end": "…", "status": "closed",
  "total_amount": 184320.50, "total_pieces": 1204, "employee_count": 23,
  "unrated_operations": [], "gap_days": 0,
  "recomputed": false, "recompute_count": 0,
  "last_recomputed_at": null, "last_recomputed_by": null,
  "lines": [
    { "id": "uuid", "employee_id": "uuid", "employee_name": "RAMESH",
      "designation": "CUTTER", "wage_type": "piece_rate",
      "pieces": 320, "amount": 4000.0, "rate": 12.5,
      "style_codes": ["CLERMONT"],
      "breakdown": [ { "style_code": "CLERMONT", "style_name": "CLERMONT",
                       "operation_code": "LEATHER_CUTTING",
                       "operation_label": "Leather Cutting",
                       "pieces": 320, "rate": 12.5, "amount": 4000.0 } ] },
    { "employee_name": "SALARIED", "wage_type": "monthly",
      "pieces": 0, "amount": 13548.39, "rate": null,
      "style_codes": [], "breakdown": [] }
  ] }
```

**Frontend: this is the payslip screen.**

- **One line per employee, always.** Never two.
- `wage_type` drives the row layout — piece-rate rows show pieces × rate with a
  `breakdown` you can expand per style × operation; monthly rows show a prorated
  amount with `rate: null` and an empty breakdown. **Do not render a "rate" column for
  monthly workers.**
- `rate` on a piece-rate line is the **blended** average when the rate changed
  mid-period; the true per-day rates are in `breakdown`.

> Note: `employee_id` is returned as a UUID here; compare as strings defensively.

---

## `POST /wages/runs/{run_id}/recompute`

**Auth:** DM · MD · ⚠️ **Currently returns 409 unconditionally.**

Every persisted run is CLOSED, and the `confirm_closed` override the error message
mentions is not exposed on any route. **There is no in-app way to correct a payroll
error today** — close the run and issue an adjustment run. Show the 409 message; do
not build an override toggle.

---

# 11 · Analytics — `analytics`

**Read-only. Never writes.** A `client` token is auto-scoped and gets **404** for
another buyer's data.

## `GET /analytics/overview`

**Auth:** MD · DM · HR · SUPERVISOR · floor managers

```json
{ "clients": 4, "styles": 11, "total_pieces_ordered": 2450,
  "total_operations_logged": 18327 }
```

The dashboard's four headline tiles.

---

## `GET /analytics/pieces/{piece_code}/story` ⭐

**The barcode feature.** Scan a piece, get its entire life.

```json
{ "piece_code": "JP-2026-014-CLERMONT-PINE-M-007",
  "style_name": "CLERMONT", "colour": "PINE GREEN", "size": "M",
  "order_number": "JP-2026-014", "client": "John Peter",
  "current_stage": "SHELL_STITCHING",
  "drawer_code": "DRW-0007", "drawer_state": "sended",
  "awaiting": "on the stitching line",
  "stages": [
    { "stage": "LEATHER_CUTTING", "stage_label": "Leather Cutting",
      "employee_name": "RAMESH", "work_date": "2026-05-12",
      "logged_at": "2026-05-12T10:14:22Z", "entered_by": "CutMgr",
      "is_rework": false, "leather_consumption_dcm": 80.0 }
  ] }
```

**Frontend:** render `stages` as a vertical timeline. Flag `is_rework: true` entries
distinctly — the same stage appearing twice is meaningful, not a duplicate. `awaiting`
is a ready-to-display human sentence; do not compose your own.

---

## The rest

| Endpoint | Query | Returns |
|---|---|---|
| `GET /analytics/explorer` | `include_pieces` (default **true**) | Full client → order → style → piece tree |
| `GET /analytics/orders/{order_id}/tree` | — | One order drilled down |
| `GET /analytics/styles/{style_id}/detail` | — | One style's stage progress |
| `GET /analytics/pieces/detail` | `piece_code?` `sku_code?` `seq?` | One piece by code or by SKU+seq |
| `GET /analytics/consumption` | `order_id?` `style_id?` | Leather consumed per style |
| `GET /analytics/alerts/stage-spread` | — | Bottlenecks: `{style, stage, cut, reached_stage, gap, severity}` |
| `GET /analytics/alerts/freight-risk` | `today?` | Orders at risk of missing the sea cut-off |
| `GET /analytics/employee-rates` | `start*` `end*` `employee_id?` `style_code?` | Per-worker output and earnings |

**Auth:** `/overview` and `/employee-rates` are role-gated (the latter to DM/MD/HR
because it exposes pay). The other seven currently have **no role gate** — any
non-employee token reaches them. Gate them in the UI by role even though the API does
not yet.

**Performance:** `/explorer` with `include_pieces=true` (the default) materialises
every piece of every order, and the two alert endpoints scan all styles/orders. **Send
`include_pieces=false`** unless you genuinely need pieces, and do not poll the alerts
on a short interval.

---

# 12 · Health — `core`

| Endpoint | Auth | Returns |
|---|---|---|
| `GET /health` | none | **200** always. Liveness only — checks nothing. |
| `GET /ready` | none | **200** if the DB responds, **503** if not. |

Use `/ready` for a real "is the backend up" indicator; `/health` will say yes even
when the database is down.

---

# Appendix A — Screen → endpoint map

| Screen | Endpoints |
|---|---|
| Login | `POST /auth/login` |
| App boot | `GET /auth/me`, `GET /ready` |
| **Worker home** | `GET /attendance/me/status`, `POST /attendance/check-in`, `POST /attendance/check-out` |
| **Gate terminal** | `GET /barcode/resolve`, `POST /attendance/scan-check-in` ⚠️ |
| Supervisor roster | `GET /attendance/today`, `POST /attendance/proxy/check-in`, `POST /attendance/proxy/check-out` |
| Clients list | `GET /clients` |
| New client + order | `POST /clients`, `POST /clients/{id}/orders` |
| Order detail | `GET /clients/{id}/orders`, `GET /analytics/orders/{id}/tree` |
| **Breakdown upload** | `POST /imports/preview` → `POST /imports/commit` → `POST /barcode/print` |
| Employee roster | `GET /employees`, `POST /employees` |
| Employee card | `PATCH /employees/{id}/barcode` |
| Stock | `GET /materials/spec`, `GET /materials/stock` |
| Add lot | `GET /materials/spec` → `POST /materials/lots` |
| Receiving | `POST /suppliers/orders`, `PATCH /suppliers/orders/{id}`, `POST /materials/receive` |
| **Cut screen** | `GET /barcode/resolve` ×N → `POST /production/log` |
| **Pipeline screen** | `GET /barcode/resolve` ×N → `POST /production/log` |
| Manual picker | `GET /production/skus` ⚠️, `GET /production/skus/{id}/pieces` ⚠️ |
| **Store-scan** | `POST /drawers/store-scan` |
| DM drawer release | `POST /drawers/{id}/receive` |
| Rates | `GET /wages/styles`, `GET /wages/rate-sheet`, `GET /wages/rate-history`, `POST /wages/rates` |
| **Payroll** | `GET /wages/runs`, `POST /wages/runs`, `GET /wages/runs/{id}` |
| Dashboard | `GET /analytics/overview`, `/alerts/stage-spread`, `/alerts/freight-risk` |
| **Piece lookup** | `GET /barcode/resolve` → `GET /analytics/pieces/{code}/story` |

---

# Appendix B — Enums

**`role`** `managing_director` `direct_manager` `cutting_manager` `lining_manager`
`stitching_manager` `supervisor` `hr` `employee` `client` `viewer`

**`wage_type`** `piece_rate` `monthly`

**`status`** (wage run) `open` `closed`

**`source`** (attendance) `self` `proxy`

**`screen_context`** `LEATHER_CUT` `LINING_CUT` `PIPELINE`

**`part`** (store-scan) `LEATHER` `LINING`

**`transition`** (drawer) `RECEIVED` `SENDED`

**`action`** (barcode) `reissue` `deactivate`

**barcode `type`** `PIECE` `EMPLOYEE` `DRAWER` `LEATHER_LOT` `LINING_LOT` `ACCESSORY_LOT`

**drawer `state`** `waiting` `merged` `holding_leather` `holding_lining` `holding_both`
`received` `sended`

**stage** `LEATHER_CUTTING` `LINING_CUTTING` `FUSING` `PASTING` `LINE_STITCHING`
`SHELL_STITCHING` `FINAL_FINISH` `FINAL_INSPECTION` `PACKAGE_EXPORT`

**designation** `CUTTER` `LINING_CUTTER` `FUSER` `PASTER` `TAILOR` `LINE_TAILOR`
`SHELL_TAILOR` `FINISHER` `INSPECTOR` `PACKER` · any-stage: `HELPER` `SUPERVISOR` `OTHER`

**material category** `LEATHER` `LINING` `ACCESSORY` ·
subtypes: `PLAIN` `RIBS` `KNIT` (lining) · `BUTTON` `ZIP` `THREAD` `OTHER` (accessory)

---

# Appendix C — Role × endpoint matrix

`✅` allowed · `—` 403 · MD and DM bypass everything.

| Endpoint group | MD | DM | Cut | Lin | Stitch | Sup | HR | Emp | Client | View |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|:-:|
| `/auth/*` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `/barcode/resolve` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `/attendance/check-in`, `/me*` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| `/attendance/proxy/*` | ✅ | ✅ | — | — | — | ✅ | ✅ | — | — | — |
| `/attendance/today` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| `/attendance/config` PATCH | ✅ | ✅ | — | — | — | — | ✅ | — | — | — |
| `/users` | ✅ | ✅ | — | — | — | — | ✅ | — | — | — |
| `/clients` GET | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ | ✅ |
| `/clients` POST | ✅ | ✅ | — | — | — | — | — | — | — | — |
| `/employees` GET | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| `/employees` POST | ✅ | ✅ | — | — | — | — | ✅ | — | — | — |
| `/employees/{id}/barcode` | ✅ | ✅ | — | — | — | — | ✅ | — | — | — |
| `/imports/*` | ✅ | ✅ | — | — | — | — | — | — | — | — |
| `/materials/stock`, `/spec` | ✅ | ✅ | ✅ | — | ✅ | — | ✅ | — | — | — |
| `/materials/lots` POST | ✅ | ✅ | ✅ | — | — | — | — | — | — | — |
| `/materials/receive`, `/suppliers/*` | ✅ | ✅ | — | — | — | — | — | — | — | — |
| `/production/log` | ✅ | ✅ | per stage | per stage | per stage | — | — | — | — | — |
| `/production/operations`, `/events` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — | — |
| `/drawers/*` | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — | — | — |
| `/wages/*` GET | ✅ | ✅ | — | — | — | — | ✅ | — | — | — |
| `/wages/rates`, `/runs` POST | ✅ | ✅ | — | — | — | — | — | — | — | — |
| `/analytics/overview` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — |
| `/analytics/employee-rates` | ✅ | ✅ | — | — | — | — | ✅ | — | — | — |
| other `/analytics/*` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ scoped | ✅ |

**Stage ownership for `/production/log`:**

| Stage | Who may log it |
|---|---|
| `LEATHER_CUTTING`, `FUSING` | CUTTING_MANAGER |
| `LINING_CUTTING` | LINING_MANAGER |
| `PASTING`, `LINE_STITCHING`, `SHELL_STITCHING` | STITCHING_MANAGER |
| `FINAL_FINISH`, `FINAL_INSPECTION`, `PACKAGE_EXPORT` | **MD / DM only** |

---

*Generated against the live OpenAPI schema. When in doubt, `GET /docs` is the
authority — and if it disagrees with this document, tell the backend team.*
