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
| Cutting Manager | `cutting_manager` | Logs leather cutting **only**; creates material lots |
| **Lining Manager** | `lining_manager` | **NEW in Phase 1** — logs the lining-cut path |
| Stitching Manager | `stitching_manager` | Logs **every post-cut floor stage**: fusing, pasting, line-stitching, shell-stitching, final finish |
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
- Clicks CHECK → system shows **arrived / used / balance** (see below), plus
  `reserved` and `available = balance − reserved`.
- DM enters the required quantity; if short, clicks ORDER → system **suggests a supplier** from the article.
- Supplier order status: **ORDERED → ARRIVED**.

### Receiving / approval
- DM records **APPROVED** and **REJECTED** quantities separately.
- Rejected qty is **logged** (supplier quality history).
- Approved qty is **added to stock**; the requirement is **RESERVED** so it can't be spent elsewhere.

### A DELIVERY IS ENTERED IN TWO SITTINGS
The van turns up and whoever signs for it has ten seconds. So:

- `POST /materials/arrivals` — **article, colour, total qty**, and an optional
  **sheet count**. That is allowed to be all of it: no thickness, no QC split.
  It mints or tops up the lot, **prints the lot barcode**, and puts the quantity
  into stock **provisionally** so the floor can cut from it straight away.
- `GET /materials/arrivals` — the come-back-to-it queue, oldest first. This is
  what makes the split safe: an unfinished arrival nobody can find is provisional
  stock quietly becoming permanent stock that was never checked.
- `POST /materials/arrivals/{receipt_id}/complete` — the approved/rejected split,
  the thickness, and every hide's own dcm.

`material_receipt.status` is `PENDING | COMPLETED`. **The completion moves stock
by (approved − declared), never by assignment** — the floor may have cut some of
the delivery in between, and assigning would silently undo real production.

Every stock read carries `pending_arrivals` / `pending_arrival_qty`, so a figure
can say which part of itself is provisional.

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

### The three numbers, and they reconcile
Every material read (`/materials/stock`, `/materials/lots`, `/materials/lots/{id}`,
receive, arrive) returns the same block:

| Field | Meaning |
|---|---|
| `arrived` | everything that ever came in = `balance + used` |
| `used` | cut into garments or issued as a kit (`material_lot.used`) |
| `balance` | what is on the shelf now (`material_lot.on_hand`) |
| `reserved` | committed to a requirement, **not** yet spent |
| `available` | `balance − reserved` — what may still be promised |
| `on_hand` | the legacy key; **means `balance`** |

`arrived − used == balance`, always. It did not before: `on_hand` meant *arrived*
on `/materials/stock` and *balance* on a lot's own page, `reserved` silently
included everything already consumed, and **`used` was declared on the lot schema
and never set by the read, so every lot reported 0 however much had been cut**.

**The same three, in HIDES** (leather only): `sheets_arrived`, `sheets_used`
(CONSUMED), `sheets_balance` (IN_STOCK + RETURNED — the shelf), plus
`sheets_allocated` (out on a row, not yet cut) and `sheets_scrapped`, each with
its dcm. A cutter is handed skins; "how many are on the shelf" is not answerable
from a sum of decimetres.

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
| `LEATHER_SHEET` | a hide measured at receiving | No — one label per skin |
| `EMPLOYEE` | employee created | **Yes** — reissue / deactivate |

`DRAWER` is **retired**. Nothing mints one and nothing resolves one; existing
`DRW-` registry rows are kept for audit and degrade to "known code, no payload".

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

**CONFIRMED BY HAMTHAN, 2026-09-20 — this is the factory flow. Do not ask again.**

```
LEATHER_CUTTING → FUSING → PASTING ─┐
                                     ├─► STORE (merge) ─► LINE_STITCHING
LINING_CUTTING ──────────────────────┘                   → SHELL_STITCHING
                                                          → FINAL_FINISH
                                                          → FINAL_INSPECTION  (quality check)
                                                          → PACKAGE_EXPORT
```

- The two cut paths run **in parallel**: leather goes on through fusing and
  pasting; lining goes straight to the store once it is cut.
- **STORE is the merge point** for leather, lining and accessories. It is a
  state on the PIECE (`piece.store_state`), not a place — see below.
- Nothing reaches LINE_STITCHING until the store has merged and released it.
- FINAL_INSPECTION is the quality check. PACKAGE_EXPORT is the last stage.

This is exactly what `ProductionStage.predecessor()` already encodes: both cut
entries and LINE_STITCHING return `None` (the merge gate governs the third), and
the rest form the chain above.

**PHYSICAL DRAWERS ARE NOT TRACKED** (Hamthan, 2026-09-20; see
`docs/KAIROX_PRODUCTION-EVENT_SYSTEM_GUIDE.md` §14). A drawer or bucket is where
leather, lining and accessories are physically merged, but the *drawer itself* is
not a tracked entity: there is no drawer scan, no drawer pool and no drawer
allocation. The store is seven states on the garment — `store_state`,
`leather_in`, `lining_in`, `accessories_in` — and the scan is **employee, then
piece**, two scans not three. `app/modules/drawers/` is retired: unrouted,
commented out of `main.py`, and kept only because its tables hold historical
rows for audit.

### The four gates (cheapest / most-likely-to-fail first)
1. **ROLE** — may this manager's role log this stage? → **403 for the whole request** if not.
2. **SKILL** — is this employee's designation an ORDINARY one for this stage?
   → **per-piece warning, never a block** (`production/service.py` GATE 2 logs the
   piece either way — there is deliberately no `continue`). It is an audit signal,
   not permission.
   **THE FLOOR IS CROSS-TRAINED** (Hamthan, 2026-09-20): a CUTTER also works
   FUSING and the LINING cut; a TAILOR also pastes. `STAGE_DESIGNATIONS` in
   `core/enums_barcode.py` reflects that. Widen it only when the floor genuinely
   cross-trains a role — never to silence a warning.
3. **SEQUENCE** — has the piece completed the previous chain stage? → **per-piece** (`sequence_blocked`).
4. **MERGE (completeness)** — for LINE_STITCHING only: is the piece **SENDED**
   (leather + lining both stored and released)? → **per-piece** (`merge_blocked`).
   Read off `piece.store_state`; there is no drawer to consult.
   `accessories_in` is part of completeness and is a **roll-up over every
   declared accessory line**, each issued by its own packet scan — see §9a.

Gate 1 is whole-request because the role is wrong for the whole batch. Gates 2–4 are per-piece so
**one bad piece never loses the good ones a manager scanned with it.** MD/DM bypass the role gate.

### The cutting grid's hide entry — the dcm IS the lookup
`POST /cutting/rows/{id}/sheets` with a bare `dcm` **finds** the hide of that
row's article + colour which measures it, and allocates it. Exact match first,
then the nearest within `dcm_tolerance` (2%, floor 1 dcm) with the difference
reported. `matched_sheet` on the response says which code it resolved to.

- no leather of that article/colour in stock → **422 saying exactly that**
- a lot but no free hide → **422**
- nothing within tolerance → **422 naming the nearest hides that ARE on the shelf**
- `GET /cutting/rows/{id}/sheet-options?dcm=` ranks the shelf by distance

It used to **create** a sheet, so the commonest entry on the screen silently added
stock nobody had received. Creating one is now `create_if_missing=true`.

### Consumption
The **two cut stages only** capture material consumption per piece and **decrement stock once per
batch**, in the same transaction as the events. The lot link lives on the **event** (the act of
cutting), never on the piece.

---

## 9. The store & the merge gate — THERE IS NO DRAWER

`app/modules/drawers/` is **deleted**, along with the pool, the drawer barcode,
the drawer scan and every drawer route. The store is a **state on the garment**:
`piece.store_state`, plus `leather_in` / `lining_in` / `accessories_in`.

```
WAITING → HOLDING_LEATHER / HOLDING_LINING → HOLDING_BOTH
        → RECEIVED (complete) → SENDED (released) → (piece ships) → WAITING
```

**Why the drawer went.** There were 200 physical drawers. A style releases 100+
garments, so the pool ran dry partway down the list and the remainder were minted
onto a "waiting for a drawer" list — which the merge gate then refused to
line-stitch, because a piece with no drawer could not be proven complete. A DM had
to re-allocate boxes by hand, which was involved enough that it did not happen.
A state has no capacity, so nothing can run out.

- **Store-scan is TWO scans for a cut part:** the worker, then the garment
  (`POST /store/scan`). There is no drawer to scan and no "wrong drawer" 409 —
  that rejection policed an assignment the system invented at upload.
  **An accessory takes a third scan — the packet's own `LOT-ACC-…` label — and
  that one is not a box the system invented, it is the physical thing in the
  operator's hand. See §9a.**
- **Completeness, not sequence:** a lined jacket needs leather **and** lining
  before it's complete; a leather-only piece (`needs_lining=False`) is complete on
  leather alone. A style with an accessory spec also needs its kit.
- **RECEIVED** requires completeness (it auto-fires); **SENDED** requires
  completeness. Line-stitching is blocked until SENDED. PACKAGE_EXPORT takes the
  garment **out of the store** (`release_nocommit`) — there is no pool to return
  to, because a garment ships once.

**The tables stay.** `drawer` / `piece.drawer_id` / `barcode_registry.drawer_id`
are retained, unwritten and unread, so historical rows remain auditable and
Alembic autogenerate does not try to DROP them (§11). `barcode.models` still maps
them; nothing else imports them.

---

## 9a. Accessories — ONE PACKET PER SCAN, and the wrong size never goes in

**The mistake this whole section exists to stop:** an M-size button in an L-size
jacket. It is invisible on the factory floor, it is found by the client in Dubai,
and it is paid for in return freight plus a remade garment.

### Where accessories live
An accessory is a `MaterialLot` — `category=ACCESSORY`, `subtype` = an
**accessory-kind code** — keyed on `(article, colour, thickness, size, subtype)`
with a DB unique index, and each lot mints **one `LOT-ACC-000001` barcode**. So the
"L-size black horn button" packet already is its own lot with its own label,
separate from the M one. There is deliberately **no per-button barcode**: one
button out of 5,000 is any other button (`enums_barcode.py` on `ACCESSORY_LOT`).

### The accessory catalogue — kinds are DATA, and it fills itself in
`MaterialSubtype` offered only BUTTON / ZIP / THREAD / OTHER, so eyelets, lace pins
and rib knit trim all fell to `OTHER` — whose spec requires `description` + `count`
and whose filters are `article, colour` **with no size at all**. Rib knit trim, an
accessory whose size genuinely varies per SKU, had no size field to vary.

`accessory_type` (`barcode/models.py`) makes the kinds data: `code` (≤20 chars, the
width of `subtype` on four tables — one a ledger), `label`, `qty_field`/`qty_uom`
(**the measurement** — there is no separate column, the quantity field *is* it),
`requires`, `filters`, and `size_varies_by_sku`.

**IT POPULATES ITSELF FROM INTAKE.** A kind becomes known because a packet of it
*arrived* — `MaterialService.arrive` and `.create_lot` register an unrecognised
`subtype` instead of rejecting it, and return it as `accessory_type_registered` so
a kind that appears by accident is visible. `GET/PATCH /materials/accessory-types`
refines one. There is no "add a kind" form: the moment anybody knows about a new
accessory is the moment one turns up at the gate.

**`resolve_spec` STAYS PURE.** `materials/accessory_catalog.AccessoryCatalog`
**overlays** the DB rows onto `MATERIAL_SPEC` and falls back to it, so a deployment
with no catalogue rows behaves exactly as before — which is also the state the test
harness runs in, since the seed lives in the migration.

**`size_varies_by_sku` is the 85–90% rule as data** (`true` for ZIP and
RIB_KNIT_TRIM): it decides whether the recipe form fans one line across every SKU
or asks per SKU, and how `copy_from` matches. Neither hardcodes which accessories
are special.

### AN ACCESSORY LINE MUST NAME ITS SKU
**A `SKU` is unique on `(style_id, color_code, size)` — colour *and* size — so a
line that names one has said everything about which garments it is for.** A
style-wide accessory line is a 422.

That single rule deletes a whole apparatus that existed only to police style-wide
lines: `garment_size` on accessories, the size-coverage gate, the size-ambiguity
gate, the "is this number a garment size" guess (which confined a 60cm zip to 4XL),
and a PATCH that silently un-scoped a line.

| Column | Means | Matched against |
|---|---|---|
| `size` | the **material's** size — a 60cm zip, an 18L button | `MaterialLot.size` (finds the lot) |
| `garment_size` | **LEATHER/LINING ONLY** — which garments a style-wide line is for | `SKU.size` |

`garment_size` sent on an accessory line is **refused, not ignored** — a caller who
sends it believes it is doing something, and its answer could only contradict the
SKU's. It remains explicit-and-never-inferred for leather and lining, which can
still be style-wide; `_reads_as_garment_size` is only that 422's trigger.

**The fan-out keeps entry cheap.** `POST .../material-spec/lines` takes
`apply_to: "ALL_SKUS"`, `sku_ids: [...]`, or `per_sku: [{sku_id, size?,
qty_per_piece?}]` — the last for the accessories whose size follows the garment.
Exactly one scope per request; more is a 422. It is **idempotent and partially so**
(`created` vs `already_present`), and it lives on `add_line` rather than the PUT
because `_assert_editable` leaves accessories correctable after release while
`replace_spec` freezes the whole grid — and the wrong button is always found after
release. **The stored shape is per-SKU either way: the convenience is in the
request, never in the data.**

### The release gate checks SKU COVERAGE
One pure check in `core/kit_rules.py`, folded into `release_blockers` at all four
call sites: **`accessory_sku_gaps`** — for every article the style declares on *any*
SKU, every **ordered** SKU must have a line for it. A zip on the NAVY colourways and
not the PINE ones blocks release and names the missing ones.

It replaced `accessory_size_gaps` and `accessory_size_ambiguities`, both of which
only made sense for style-wide lines. `qty_ordered > 0` scopes it, so a
zero-quantity importer row cannot raise a false blocker — and a gate that fires on
the normal case is a gate people learn to ignore.

`kit_rules.size_matches` survives as the single size rule for leather/lining.

### `requirement` AGGREGATES accessories
A purchase order is raised off `GET .../material-spec/requirement`, and an accessory
is SKU-scoped — so one button on a NAVY+PINE order in S/M/L/XL is **eight rows of
4**. Accessory lines are grouped by their **material identity**
(`subtype, article, colour, size`) into an `ACCESSORY_GROUP` row carrying the summed
`total_required`, with the per-SKU rows under `per_sku`.

- **BLACK and TAN buttons are two groups**, not one: different materials, different
  lots, and a combined figure could not be ordered against.
- **`short_by` is computed on the group**, because stock is not reserved per SKU —
  one lot serves every colourway, so comparing each SKU's share against the whole
  lot would report them all covered while the total was short.
- A group whose SKUs take **different** per-piece quantities reports
  `qty_per_piece: null` rather than one of them.

Leather and lining keep the style-wide arithmetic (an override's SKU subtracted from
the style line).

### `copy_from` fans accessories out
Copying maps SKUs on `(color_code, size)` and skips what does not match — and
`include_sku_overrides` defaults to **False**. With accessories SKU-scoped the
default copy would therefore have carried **zero** accessory lines, gutting the
feature. So an accessory is copied as a recipe for an **article** and fanned onto
*this* style's SKUs: matching by size where `size_varies_by_sku`, onto all SKUs
where it does not. Leather/lining copying is unchanged.

### The store scan: `POST /store/scan` with `lot_barcode`
```
scan the WORKER  →  scan the GARMENT  →  scan the PACKET (LOT-ACC-…)
```
Each scan issues **exactly the one recipe line that packet matches**. Scanning a
packet *is* the accessory scan, so `part` may be omitted; a `part` naming anything
else is a 422.

**`part: "ACCESSORY"` with no packet is a 422 — the blanket kit scan is GONE.** It
read the recipe and decremented every accessory line from one tap, so no physical
packet was ever part of the exchange and the wrong size was undetectable *by
construction*. `lines[]`/`KitLineRequest` went with it; the optional `qty` on the
scan covers a short issue (2 of 4 buttons).

The response's `kit` block is the **whole checklist**, not just the packet scanned
— the operator who has just done the zip must be told the buttons are still owed
while they are still at the terminal. `accessories_in` turns true only when every
declared line is issued.

`match_packet` matches in **two tiers**, and the split is what makes a wrong size a
wrong size rather than an unknown packet: tier 1 ignores size (subtype, article,
colour, thickness) so the M packet still finds the style's BUTTON lines; tier 2
then asks the two size questions separately. Its four other verdicts:

| Verdict | Status | What it means / the fix |
|---|---|---|
| `NO_KIT` | 409 | the style declares no accessories, or all its lines are for another colourway/size (`_no_kit_reason` says which) |
| `NOT_IN_RECIPE` | 422 | wrong packet entirely → check it, or `POST /materials/issues` off-spec |
| `NO_LINE_FOR_SIZE` | 409 | the **recipe** has no line for this garment's size → a DM fixes the spec, because every garment of that size is in the same state. This is the release-gate gap showing up on the floor |
| `AMBIGUOUS` | 409 | the packet matches several lines → combine or scope them |

### The wrong size is REFUSED and waits for a DM/MD
**This is the one place in the app where a mid-scan problem is not recorded with a
warning.** The skill gate and the stock shortfall both log the work and flag it,
because the work physically happened and losing the record is worse. Here the wrong
size *is* the failure, so there is nothing worth preserving:

- the scan **409s**, nothing is decremented, no ledger row, `accessories_in` untouched
- a `KitSubstitutionRequest` row is **committed before the raise** — the scan must
  fail, but the ask has to survive the failed request or the operator is told to
  wait for an approval that exists nowhere. The id is in the
  `X-Kit-Substitution-Request` header
- **idempotent on `(piece, spec_line, lot)`** — the protocol is "scan, get refused,
  wait, scan again", so a re-scan finds the same row instead of queueing another
- `GET /store/substitutions` is the DM's queue, oldest first, like the material
  arrivals queue. An ask nobody can find is a garment parked for a reason nobody
  remembers
- `POST /store/substitutions/{id}/approve|reject` is **DM/MD only** — the person
  holding the wrong packet must not be the person who authorises it
- **APPROVING IS PERMISSION, NOT THE ISSUE.** The operator re-scans, and *that*
  writes the movement against the worker's own card. An approval that spent stock
  itself would record a manager as having issued a packet they never touched
- `PENDING → APPROVED → CONSUMED` (terminal: one approval, one garment).
  `REJECTED` is re-approvable — "no" Tuesday and "yes" Wednesday is a real sequence

### A named lot is now CHECKED (`lot_fits_line`)
A caller-supplied `material_lot_id` used to be fetched and decremented with **no
comparison of any kind** — not article, colour, size, nor even `is_active` — so the
M button lot could be spent against an L garment's line and the ledger recorded it
as correct. Category and subtype are **never** waived (a substitution is "this size
will do", never "a zip instead of a button"); article, colour and size are waived
only for a **DM-approved** substitution.

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
- **A run explains its own total.** `diagnostics` on every compute/recompute
  accounts for every piece the window contained — `pieces_paid`,
  `pieces_skipped_wrong_wage_type` (named), `pieces_unrated`,
  `pieces_priced_from_a_backdated_rate` — and `notes` turns whichever bucket is
  non-zero into a sentence. The three causes of a £0 payroll (empty window, wrong
  wage type, rate sheet dated after the work) used to be indistinguishable.
- **A rate entered after the work still prices it.** `effective_rate` falls back
  to the EARLIEST rate on file when nothing is effective on the work date, and
  reports that it did. The rate form defaults `effective_from` to today, so a
  fortnight priced afterwards otherwise came out at zero against a full sheet.
  The fallback is only consulted when the strict query finds nothing.

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

> **Day-to-day workflow, squashing the chain, and every autogenerate trap:
> `docs/ALEMBIC_GUIDE.md`.** The rules below are the repo-specific ones.

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
- **THE CHAIN WAS SQUASHED on 2026-09-28** into one baseline,
  `20260928_1013_initial_migration.py` (revision `1d4a32009101`,
  `down_revision = None`). A squashed baseline is autogenerated from the models, so
  it carries the **schema and nothing else** — every data migration the old chain
  performed is absent by construction. Check for one before you squash again.
- `20260929_accessory_sku_scope` creates `accessory_type` (the accessory kinds, as
  data — see §9a), seeds the 7 starting kinds, and **deactivates any accessory
  recipe line with `sku_id IS NULL`**, because an accessory names its SKU now and
  such a row can never be satisfied. `garment_size` is NOT dropped — leather and
  lining still use it; it is simply never written for an accessory, the same "keep
  the column, stop writing it" pattern the retired drawer tables follow.
- `20260928_garment_size_backfill` is that check having been done once: it **clears
  the inferred `garment_size`** on accessory recipe lines whose `garment_size`
  equals a purely numeric material size — those were the guess, not a human, and
  each one silently removed the accessory from every other size (§9a). It was
  carried over by hand when the squash dropped it. The table it used to ship
  alongside, `kit_substitution_request`, is in the baseline.
  The data fix is a module-level function `clear_inferred_garment_sizes(bind)`
  covered by `tests/integration/test_garment_size_backfill.py` rather than inline in
  `upgrade()`, because it cannot be exercised by running the chain: since the squash
  the DDL applies on SQLite, but an alembic-built SQLite database rejects every
  INSERT — the baseline puts `server_default=sa.text('now()')` on 138 timestamp
  columns and SQLite has no `now()`. Inline, it would have run for the first time on
  production.
- **A test must never name a migration file.** Both tests that did
  (`test_garment_size_backfill`, `test_fk_delete_rules`) died on a
  `FileNotFoundError` the moment the chain was squashed. Find a root revision by its
  `down_revision = None`, and prefer asserting against the schema over a hand-kept
  list inside a migration — the list `20260818_fk_setnull_all.py` carried went with
  the file.
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
| 2 · Integration | `tests/integration/` | two-door log, 4 gates, consumption + single decrement, merge gate, barcode lifecycle, strict materials, pre-mint, **accessory packet scan + wrong-size approval** (`test_accessory_packet_scan.py`), **SKU-scoped accessories + the fan-out** (`test_size_matched_accessories.py`), **the accessory catalogue** (`test_accessory_catalogue.py`), **the garment_size backfill** (`test_garment_size_backfill.py`) | SQLite |
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