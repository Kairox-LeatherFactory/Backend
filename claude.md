# CLAUDE.md — KairoX ERP (Phase 1)

> **Scope of this file.** Everything the system does **up to and including production tracking**:
> materials → barcode → breakdown upload → production stages → attendance → wages → analytics,
> plus `main.py`, Alembic migrations, and the test suite.
>
> **Out of scope here** (Phase 2, the Aug-20 deadline): `bom`, `procurement`, `inventory`,
> `supplier_po`. Those are the auto-generation pipeline and are documented separately.
> See the **"Material vs Inventory"** section for why the Phase-1 `material` module is
> *deliberately separate* from the Phase-2 `inventory` module — they are not duplicates.

---

## 1. What KairoX is

A production-management ERP for a **leather garment factory**. It manages the order-to-production
lifecycle and gives **per-piece traceability**: every individual garment carries a barcode from
the moment the breakdown sheet is uploaded, and every action on it (cutting, stitching, storage,
inspection, export) is scanned and logged against that barcode.

**The real-world business flow the app models:**

1. Client sends an **order sheet + spec sheet**.
2. The **Direct Manager (designer)** creates a **breakdown sheet** from those inputs.
3. **BOM costing** is finalised by the **MD** (by hand, in Phase 1).
4. BOM goes to the client; production starts **only after client approval**.
5. Production runs against the **breakdown sheet**, not the raw spec.

**Phase 1 (this document) = the system of record + tracking layer.** Humans still drive BOM and
costing. The app tracks materials, mints per-piece barcodes at breakdown upload, and records every
production event, attendance, and wage.

---

## 2. Tech stack

- **Backend:** FastAPI (async), Python, SQLAlchemy (async `asyncpg`; sync `psycopg2` for Alembic), Pydantic v2
- **DB:** PostgreSQL (Supabase). `app_user.role` is a **native PG enum** (`user_role`) — see §11.
- **Migrations:** Alembic (deterministic constraint naming)
- **Auth:** self-issued JWT; roles enforced per-route
- **Frontend:** Next.js / Expo (2 devs) — coordinate API contracts
- **Tests:** pytest + pytest-asyncio over in-memory async SQLite (`aiosqlite`); httpx for HTTP layer

---

## 3. Roles (`UserRole`) and who does what

| Role | Value | Phase-1 responsibility |
|---|---|---|
| Managing Director | `managing_director` | Superuser; finalises costing; bypasses stage gates |
| Direct Manager | `direct_manager` | Designer; uploads breakdown; drawer RECEIVED/SENDED; bypasses stage gates |
| Cutting Manager | `cutting_manager` | Logs leather cutting + fusing; creates material lots |
| **Lining Manager** | `lining_manager` | **NEW in Phase 1** — logs the lining-cut path |
| Stitching Manager | `stitching_manager` | Logs pasting, line-stitching, shell-stitching |
| Supervisor | `supervisor` | Reads the floor roster (no attendance writes, no user creation) |
| HR | `hr` | Employees, wages visibility, designation backfill, attendance operator |
| **Security** | `security` | **Gate operator** — scans employee cards in and out |
| Client | `client` | (read-only order views — not core to Phase 1 floor) |
| Viewer | `viewer` | Read-only |
| ~~Employee~~ | ~~`employee`~~ | **LEGACY — never minted. Workers get no login.** |

**Rule: SHOP-FLOOR WORKERS ARE NOT GIVEN SYSTEM ACCESS.** A worker has no login and no
`app_user` row, so creating one needs no phone, email or password — just a name, a designation
and a wage type. They are identified on the floor by their **employee barcode**, and their
attendance is entered *for* them by an **operator: SECURITY / HR / MD / DM**
(`core.enums.ATTENDANCE_OPERATOR_ROLES`). `UserRole.login_roles()` is the authority on who may
hold a login; `UserService._reject_non_login_role` is the single choke point that enforces it,
so neither the users API nor the employee-create path can put a worker in `app_user`.

The `employee` value stays in the enum only because `app_user.role` is a **native PG enum**
(values cannot be dropped) and pre-change rows may still carry it. The `block_employees`
dependency now guards those legacy tokens only.

---

## 4. The data hierarchy

```
Client
 └── ClientOrder            (order_number)
      └── Style             (name, article — e.g. "CLERMONT")
           └── SKU          (colour + size + qty_ordered; code = order·style·colour·size)
                └── Piece   (ONE physical garment; seq 1..N within the SKU)
```

- **`Piece` is the tracked unit.** Its `code` (STYLE-COLOUR-SIZE-seq) **is** the parent barcode.
- A piece also carries `needs_lining` (from the breakdown) and `drawer_id` (its assigned drawer).
- Pieces are minted at **breakdown upload**, not at cutting (see §6).

---

## 5. Stage 0 — Materials & inventory (Phase 1, human-driven)

Three categories, all with the same flow: **Leather**, **Lining**, **Accessories** (buttons, zips,
thread, other). This is the `material` module — **not** the Phase-2 `inventory` module (§12).

### Stock check → order
- DM picks a category, filters by the fields that apply to it (see table below).
- Clicks CHECK → system shows **on-hand / reserved / available** (`available = on-hand − reserved`).
- DM enters the required quantity; if short, clicks ORDER → system **suggests a supplier** from the article.
- Supplier order status: **ORDERED → ARRIVED**.

### Receiving / approval
- DM records **APPROVED** and **REJECTED** quantities separately.
- Rejected qty is **logged** (supplier quality history).
- Approved qty is **added to stock**; the requirement is **RESERVED** so it can't be spent elsewhere.

### Adding new material — STRICT per-category fields
Every lot must carry exactly its category's fields, else the API rejects it (422). Creating a lot
**mints a child barcode** — that is how material formally enters inventory.

| Category / subtype | Required fields | Quantity field (uom) | Filters |
|---|---|---|---|
| Leather | thickness, dcm | dcm (dcm) | article, colour, thickness |
| Lining / plain | thickness, mtrs | mtrs (mtrs) | article, colour, thickness |
| Lining / ribs | kg | kg (kg) | article, colour |
| Lining / knit | pcs | pcs (pcs) | article, colour |
| Accessory / button | size, count | count (pcs) | article, colour, size |
| Accessory / zip | size, count | count (pcs) | article, colour, size |
| Accessory / thread | thickness, mtrs | mtrs (mtrs) | article, colour, thickness |
| Accessory / other | description, count | count (pcs) | article, colour |

`article` + `colour` are required for every material. `GET /materials/spec?category=&subtype=`
returns this list at runtime so the frontend renders the right form + filter boxes.

**Three stored numbers per material:** `on_hand` (real column), `reserved` (a reservation ledger,
never mutates on_hand), `available` (derived — never stored, so the two can't drift).

---

## 6. The barcode system (the spine of Phase 1)

### One registry, one front door
`BarcodeRegistry` is the single table every scan resolves through. Each printed code — a piece, a
material lot, a drawer, an employee card — has exactly one row, carrying its `type`, `status`, and a
nullable FK to the domain row it names.

**`resolve(code)`** is one indexed lookup:
- unknown code → **404**
- retired employee card → **410 Gone** (distinct from "never existed")
- otherwise → the code's `type` + a live payload for that type

### Barcode types
| Type | Minted when | Editable? |
|---|---|---|
| `PIECE` | breakdown upload | No — permanent garment identity |
| `LEATHER_LOT` / `LINING_LOT` / `ACCESSORY_LOT` | material lot created | No |
| `DRAWER` | breakdown upload (one per piece) | No — static code, recycling state |
| `EMPLOYEE` | employee created | **Yes** — reissue / deactivate |

### Employee barcode lifecycle (history is sacred)
- **Reissue** (lost/damaged card): retire the old code, mint a new one. History untouched.
- **Deactivate** (worker leaves): flip the registry `status` to RETIRED → resolve returns 410.
  **The employee row, all production events, and all wage lines stay intact.** You delete the
  scannable code, never the person or their record.

---

## 7. The pre-mint inversion (breakdown upload)

**Before:** pieces were created at cutting.
**Now:** pieces are minted at **breakdown upload** — a garment has a barcode identity and a drawer
*before* it is ever cut.

At upload, for every ordered unit of every SKU, the importer (`imports/premint.py`, runs **sync**
inside the importer's transaction) creates: the **Piece**, its **parent barcode**, a **Drawer**, and
the **drawer barcode** — atomically. It sets `needs_lining` from the breakdown and merges the piece
to its drawer. It is **idempotent** (re-running tops up, never duplicates).

---

## 8. Production — the two-door log & four gates

### One endpoint, two doors, NO stage buttons
`POST /production/log` is the whole floor's logging surface. The caller sends an **actor** (employee,
by barcode or id) and **targets** (pieces, by barcode or sku+seqs). The caller **never sends a stage**:

- a **cut screen** (LEATHER_CUT / LINING_CUT) fixes the cut stage
- otherwise the stage is **inferred** from each piece's own history (the next stage on the chain)

Barcode door and manual door POST the same shape; the router resolves barcodes → ids, then the
service sees ids only.

### The pipeline
```
LEATHER_CUTTING ┐
                ├─(parallel cut paths, per piece)
LINING_CUTTING  ┘
   → FUSING → PASTING → [MERGE GATE] → LINE_STITCHING → SHELL_STITCHING
   → FINAL_FINISH → FINAL_INSPECTION → PACKAGE_EXPORT
```

### The four gates (cheapest / most-likely-to-fail first)
1. **ROLE** — may this manager's role log this stage? → **403 for the whole request** if not.
2. **SKILL** — may this employee's designation work this stage? → **per-piece warning** (partial accept).
3. **SEQUENCE** — has the piece completed the previous chain stage? → **per-piece** (`sequence_blocked`).
4. **MERGE (completeness)** — for LINE_STITCHING only: is the piece's drawer **SENDED**
   (leather + lining both stored and DM-released)? → **per-piece** (`merge_blocked`).

Gate 1 is whole-request because the role is wrong for the whole batch. Gates 2–4 are per-piece so
**one bad piece never loses the good ones a manager scanned with it.** MD/DM bypass the role gate.

### Consumption
The **two cut stages only** capture material consumption per piece and **decrement stock once per
batch**, in the same transaction as the events. The lot link lives on the **event** (the act of
cutting), never on the piece.

---

## 9. Drawers & the merge gate

A drawer is a physical storage slot. It has a **static code** but a **recycling state**:
```
WAITING → MERGED (at upload) → HOLDING_LEATHER → HOLDING_BOTH
        → RECEIVED (DM) → SENDED (DM) → (piece ships) → WAITING
```
- **Store-scan:** scan the **drawer first**, then the piece. The merge map is the authority — a piece
  scanned into the wrong drawer is a **409**.
- **Completeness, not sequence:** a lined jacket needs leather **and** lining before it's complete;
  a leather-only piece (`needs_lining=False`) is complete on leather alone.
- **RECEIVED** requires completeness; **SENDED** requires RECEIVED. Line-stitching is blocked until
  SENDED. When PACKAGE_EXPORT logs, the drawer **recycles** back to WAITING.

---

## 10. Attendance, Employees, Users, Wages, Analytics

### Attendance
- **Every attendance write comes from an operator login — SECURITY / HR / MD / DM.** Nobody else
  can punch, because nobody else on the floor has a login. Both doors share one dependency
  (`require_operator`) so the barcode door and the manual door can never drift apart.
- **Barcode door (primary):** `POST /attendance/scan-check-in` — the operator resolves the worker's
  card and calls the existing `_open_or_reject` / `_close` primitives, so it behaves identically to
  the manual flow (same geofence, same late/short flags, same idempotent re-tap).
- **Manual door (fallback):** `POST /attendance/proxy/check-in|check-out` when a card fails or is
  forgotten. Any wage type.
- `/attendance/check-in|check-out` is now an operator recording **their own** arrival/departure.
- `work_date` uniqueness is enforced at the DB level; check-in is idempotent (re-tap = no-op).
- Production logging requires the employee to be **present today**.

### Employees
- **Designations are always UPPERCASE** (`Designation.normalise`) — a controlled vocabulary that
  drives the skill gate. Unknown designations fail-open (HR backfills).
- **Names are unique**; a collision is prefixed `IN-CHAL`.
- On create: **every** new employee gets an **employee barcode** (issued in the same transaction;
  the code is returned so the card can be printed). **No login is minted for a worker** — wage_type
  is a payroll fact and does not imply system access (a MONTHLY worker used to be auto-given an
  `employee` login; that was removed).
- A login *is* minted alongside the employee row when the caller passes an explicit **staff** role
  (`schemas._EMPLOYEE_LOGIN_ROLES`: HR, SUPERVISOR, CUTTING/LINING/STITCHING_MANAGER, SECURITY) —
  that path does need phone + password. DM/MD are created via user-creation, not here.

### Users
- Self-issued JWT. `provision_user` creates the login for monthly employees.
- `role` is a **native PG enum** — see §11 for the migration implication.

### Wages
- **One line per employee per run.** PIECE_RATE and MONTHLY are **mutually exclusive** — never both.
- Rates are **date-effective**: a mid-period rate change prices each day at the rate effective that day.
- Wage runs support **recomputation with a full audit trail**.
- A **closed run is a frozen snapshot** — never recomputed.
- Wages visible only to **HR / DM / MD**.

### Analytics (read-only — never writes, owns no tables)
- Factory overview, order/style explorer, stage spread, freight-risk alerts.
- **Piece life story** (barcode feature): every stage a piece passed — who, when, rework flag,
  leather consumption at cutting, current stage + what it's waiting on.
- **Consumption vs stock**: leather consumed per style, summed from cut events.
- Analytics is a **live** view; payroll of record stays the frozen wage run — they legitimately
  differ mid-period.

---

## 11. main.py (app assembly)

- Modular **monolith**: one deployable app, many internal modules; cross-module calls go through
  services (so a module can later be lifted out).
- **All model modules must be imported** so `Base.metadata` sees every table (a missed import makes
  Alembic autogenerate try to DROP the table — the schema-drift trap). `barcode.models` holds the
  barcode + material + drawer + supplier tables.
- **Router locking:** manager-only routers are wrapped with `block_employees`; **auth, users,
  attendance and `/barcode/resolve` stay open** at the router level — the attendance write routes
  do their own operator gate (`require_operator`), and resolve must stay reachable from the
  attendance screen. `block_employees` now shuts out legacy `employee` tokens only.
- **One `lifespan`** (deps check + optional sweepers + dev `create_all`). Do not define two.
- Register routers under `/api/v1`.

**Known traps that were fixed (don't reintroduce):** importing `api` from `sqlalchemy.event` (wrong
object — use `app.include_router`); defining `lifespan` twice (the second silently overrides);
importing `block_employees` before it exists in `users/deps.py`.

---

## 12. Material vs Inventory — do NOT merge them

**These are two different systems for two different phases. Keep them separate.**

| | `material` (Phase 1, this doc) | `inventory` (Phase 2, Aug-20) |
|---|---|---|
| Driven by | a human typing a lot in | a **BOM** + a spreadsheet upload |
| Keyed to | category / subtype / article | a **`bom_id`** |
| Has | lot barcodes, 3 floor categories, per-type fields, on-hand/reserved/available | `MaterialAlias`, `UomConversion`, BOM-keyed checks & reservations |
| Core ops | `create_lot`, `stock`, `receive`, supplier orders | `run_inventory_check(bom_id)`, `release_reservations(bom_id)`, alias matching |

**Why not reuse `inventory`?** Its every operation is keyed to a `bom_id`, it ingests spreadsheets,
and it carries alias/UOM machinery for matching messy BOM text — none of which the human floor flow
has or wants. In Phase 1 **there is no BOM** (humans do costing by hand), so there is nothing to key
`inventory` to.

**Why not overwrite `inventory` with `material`?** The BOM module (Aug-20) *depends on* the inventory
service — `run_inventory_check`, `release_reservations`, the alias matcher. Replacing it breaks the
BOM pipeline before it ships.

**The future bridge (Phase 2, when BOM is proven):** the plan is to *connect* them, not replace one.
The BOM's `inventory_check` will read stock; the physical stock it reads against **is** the material
lots. The manual "manager checks stock and places order" step gets replaced by the BOM system doing
it automatically — but the storage layer (material lots, on-hand/reserved/available) stays. Keeping
them separate now is exactly what makes that connection a bridge instead of a rewrite.

---

## 13. Alembic migrations

- **`app_user.role` is a native PG enum** (`Enum(UserRole, name="user_role")`). Adding
  `LINING_MANAGER` to the Python enum is **not enough** — Postgres needs the label added to the DB
  type:
  ```python
  if op.get_bind().dialect.name == "postgresql":
      op.execute("ALTER TYPE user_role ADD VALUE IF NOT EXISTS 'LINING_MANAGER'")
  ```
  (Guarded so it's a no-op on SQLite, where there is no native enum.)
- **ADD THE MEMBER *NAME*, UPPERCASE — NOT THE VALUE.** `Enum(UserRole, ...)` persists the enum
  **member name**, because this project never passes `values_callable`. For
  `SECURITY = "security"` the driver sends `'SECURITY'`. Adding the lowercase `'security'` creates a
  label the ORM will never emit, and the role stays unusable — the INSERT still dies with
  `invalid input value for enum user_role: "SECURITY"` even though `'security'` is on the type.
  This bit `lining_manager`, `security` and `merchandiser`; `20260810_role_case` repairs all three.
  Write the name exactly as it appears left of the `=` in `core/enums.py`.
  The schema's other native enums (`attendance_source`, `wage_type`, `run_status`) are all
  uppercase names — match them.
- The barcode migration adds: `barcode_registry`, `drawer`, `material_lot`,
  `material_reservation`, `material_receipt`, `supplier`, `supplier_order`, plus the 5 columns on
  `piece` / `production_event`.
- Set the migration's `down_revision` to your current head (`alembic heads`) before running.
- **Portability watch (future Oracle):** ids come from the app (`uuid4`), not `gen_random_uuid()`;
  no `JSONB` operators or `ON CONFLICT` in the feature — keep it that way.

---

## 14. Tests (five layers, money-paths-first)

Priority: **money paths > data-integrity paths > read paths.**

| Layer | Location | What it proves | Runs on |
|---|---|---|---|
| 1 · Unit | `tests/unit/` | pure gate/stage/designation predicates | no DB (ran: 49/49 pass) |
| 2 · Integration | `tests/integration/` | two-door log, 4 gates, consumption + single decrement, merge gate, barcode lifecycle, strict materials, pre-mint | SQLite |
| 3 · Functional | `tests/functional/` | one garment cut→export + drawer recycle | SQLite |
| 4 · System/E2E | `tests/system/` | through the FastAPI routers (status codes, role guards) | httpx |
| 5 · UAT | `tests/uat/` | 7 business scenarios (upload→pieces, cut→stock, skill block, no-skip, merge gate, leaver history-safe, shortfall→receive) | SQLite |

```bash
pip install pytest pytest-asyncio aiosqlite httpx
pytest tests/ -v                    # all layers
python verify/run_logic_checks.py   # pure logic, no deps -> PASSED 49 FAILED 0
```

- **SQLite proves logic; add a Postgres CI job** to prove the migration + native-enum path.
- `GUID()` degrades to CHAR(32) and `JSON` off Postgres — the feature uses nothing Postgres-only, so
  SQLite is a faithful stand-in for the tests.

---

## 15. Working principles (how to build in this repo)

- **Repository = all DB access. Service = business logic. Router = HTTP only.** Commits belong in the
  service (batch the whole scan into one transaction), never scattered.
- **Approval gates are hard, audited state transitions** (drawer RECEIVED/SENDED, costing/client
  approval), never soft booleans — write an `audit_log` row.
- **The breakdown sheet is the production source of truth** — production events reference the
  breakdown, not the raw spec.
- **Lazy imports** keep the module graph acyclic: `production.service → materials.service` and
  `→ drawers.service`, and `drawers.service → production.models`, are imported **inside the method**
  that uses them. Don't hoist them.
- **Designations UPPERCASE, names unique, wages one-line-per-employee-per-run, closed runs frozen** —
  these are invariants, not preferences.
- **Phase-1 manual paths must converge with Phase-2 auto-generation on ONE contract**, not two — build
  the seam now (e.g. breakdown ingestion is shaped so an auto-generated breakdown flows through the
  same validation/storage/approval surface).