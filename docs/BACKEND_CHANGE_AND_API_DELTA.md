# KairoX ERP — Backend Change Set & API Delta

**Generated:** 17 August 2026  ·  **Base URL:** `/api/v1`  ·  **Auth:** Bearer JWT
**Supersedes:** the endpoint tables in *KairoX ERP · Frontend API Reference* (13 Aug 2026, 66 endpoints)
**Source:** the annotated change list `NEED_TO_RESOLVE-v1`

---

## 0 · How to read this

The 13-Aug reference documented **66 endpoints across 7 modules**. The running app
serves **121 Phase-1 endpoints across 14 modules**, and this change set adds 18
more. So the PDF was not merely out of date — three whole modules (**Wages**,
**Materials**, **Imports**) and most of **Analytics** and **Clients** were never
in it at all. §4 is the complete inventory, marked so the regenerated PDF can be
checked against it.

Sections:

| § | Contents |
|---|---|
| 1 | What was implemented in this change set, per change-list item |
| 2 | The four CTO decisions — how each was resolved, and what still needs your sign-off |
| 3 | **API delta** — every NEW / CHANGED / REMOVED endpoint, with request and response |
| 4 | Full endpoint inventory (121 + 18), flagged against the old PDF |
| 5 | Migration and deploy runbook |
| 6 | Frontend action list, per person |
| 7 | Still pending in the backend |

Status chips used below:
**`DONE`** shipped in this change set · **`PENDING`** not started · **`FRONTEND`** no backend work needed

---

## 1 · What changed in the backend

### 1.1 `DONE` · Item 10 — the lining bypass (CRITICAL bug, money/integrity path)

**The bug.** A piece with no lining stage reached STORE and ran all the way to
PACKAGE_EXPORT.

**Root cause — not what the change list guessed.** It was not the SEQUENCE gate
treating lining as optional. It was `piece.needs_lining`: a flag written **once**
at breakdown upload and never recomputed. The completeness gate read it as gospel:

```python
complete = leather_in and (lining_in or not piece.needs_lining)
```

On the live database that flag is wrong for most of an order — two orders holding
the *same 17 styles* came out 0-flagged and 925-flagged
(`scripts/backfill_needs_lining.py`). So a KNIT jacket flagged `False` was
"complete" on its leather alone: it received, it sent, `_merge_ok` saw
`DrawerState.SENDED` and opened, and the garment walked the whole chain.

**The fix.** A new single authority, `app/core/lining_rules.py`. The stored flag
is now **evidence, not the verdict**. A garment needs a lining if **any** of:

1. `piece.needs_lining` is `True` — the stored flag
2. the SKU carries a lining colour (`knit_color` / `nylon_color`)
3. the style **name or article** carries a lining marker
   (`KNIT, WOOL, FUR, LINING, NYLON, QUILT, DETACH, VEST, MIX`)
4. a `LINING_CUTTING` event already exists on the piece — somebody physically cut
   a lining, which settles it outright

The OR is **one-directional by design**: a signal can only ever *add* a lining
requirement, never remove one. A stale `False` can no longer let a lined garment
through, and a genuinely leather-only garment still sends on its leather alone.

**Every completeness decision now routes through it** — `store_scan`,
`transition(RECEIVED)`, `send_batch`, `drawer_detail`, `list_labels` (including
the `sendable=` SQL filter) and `piece_state`. The importer imports the same
marker list rather than keeping its own copy, so the side that *writes* the flag
and the side that *gates* on it cannot drift.

**New response fields:** `needs_lining` (effective) and `lining_reason` on
drawer rows, drawer detail, store-scan and piece-state. `lining_reason` is
written for the operator — *"its style name contains 'KNIT'"* — because
"awaiting lining" on a garment the sheet says needs none reads as a system fault
rather than an instruction.

**Regression tests:** `tests/integration/test_lining_gate_regression.py` (7) and
`tests/unit/test_lining_rules.py` (27). Includes the **false-positive guard** —
a fix that simply required lining everywhere would satisfy the bug tests and
strand the whole factory, so that case is pinned too.

> ⚠️ **Operational consequence, expect calls on day one.** Drawers currently
> sitting `HOLDING_LEATHER` on a wrongly-flagged KNIT style will stop being
> sendable. That is the fix working. Each rejection names the evidence. Running
> `python -m scripts.backfill_needs_lining --only-false` will realign the stored
> flags with the same rule, which is cosmetic now but makes the dashboards agree.

---

### 1.2 `DONE` · Item 9 — breakdown upload → DM release → mint

**Before.** `POST /imports/commit` parsed the sheet **and** minted a per-piece
barcode + a drawer for every ordered unit, in one irreversible step. A piece
barcode is a permanent garment identity and the drawer pool is finite, so a sheet
uploaded *for review* consumed both, for styles nobody had agreed to cut.

**Now — a two-phase commit.**

```
POST /imports/commit                       →  styles + SKUs written. Nothing minted.
                                              Every style lands production_status = DRAFT.
GET  /imports/breakdown/{order_number}     →  the CRUD-able table
PATCH/DELETE /imports/breakdown/skus/{id}  →  DM corrects it (DRAFT only)
POST /imports/breakdown/{order}/release    →  DM names the styles → NOW pieces,
                                              barcodes and drawer merges are created
```

**Release is a hard, audited state transition** — `Style.production_status`
(`DRAFT | RELEASED | CANCELLED`) + `released_at` + `released_by` + an `audit_log`
row. Never a boolean `is_released`.

**Edits are DRAFT-only, and that is the point.** Once a style is RELEASED its
pieces carry printed, scanned barcodes; rewriting the breakdown behind a garment
already on the floor destroys traceability. The API returns **409** and tells you
the alternative: raise the quantity and release again — *release tops up, it never
rewrites* (premint is idempotent).

**Phase-2 seam.** `BreakdownService.release_styles` is the **one** contract that
turns a breakdown into production. When Phase 2 auto-generates a breakdown from a
BOM it writes the same DRAFT rows and calls the same method. If a second
piece-minting path ever appears, that is the divergence this seam exists to
prevent.

**The drawer pool is now genuinely finite.**

- `premint_order(..., allow_pool_growth=False)` is the default. Pieces beyond the
  free drawers are **minted anyway** (they keep their barcodes and identity) with
  `drawer_id = NULL`, and reported as `pieces_waiting_for_drawer`.
- A waiting piece is not broken and not lost. It cannot be *stored*, so it cannot
  pass the merge gate — which is correct, because there is physically nowhere to
  put its parts. A visible stall beats a pool that silently grows to 4,000
  drawers nobody built shelves for.
- `GET /drawers/pool` reports `pool_size / free_drawers / pieces_waiting_for_drawer /
  shortfall`. `POST /drawers/pool {add: N}` (DM/MD only, one-way) adds permanent
  barcoded drawers **and drains the waiting list into them**.
  `POST /drawers/allocate-waiting` drains without growing, for when a garment
  ships and recycles its drawer.

**Backfill note:** the migration stamps every **existing** style `RELEASED`, not
`DRAFT` — those styles already have minted pieces on the floor, and showing them
as DRAFT would offer the DM a "release" button over live garments.

---

### 1.3 `DONE` · Item 11 — store access

- `STORE_MANAGER` already existed with its enum migration (`20260813_bugfix_v1`),
  so **no new enum label is needed** and the non-transactional `ALTER TYPE` trap
  does not apply to this change set. Nothing in this release touches a native
  Postgres enum.
- **`STITCHING_MANAGER` added to `_SENDERS`** — it may now `POST /drawers/send`.
  Sending is what releases a batch into LINE_STITCHING; the person waiting on that
  batch is the stitching manager, and on the floor they already walk to the store
  and take the drawers.
- **It is a role grant on the store surface, not a production permission.**
  `STAGE_ROLE_ACCESS` is untouched, and `STORE_MANAGER` stays absent from it, so
  store access and stage access remain two separate questions.
- CUTTING/LINING managers are still **excluded** from sending. They put parts in;
  they do not decide what leaves.
- DM/MD already passed via `SUPERUSER_ROLES`.

---

### 1.4 `DONE` · Item 3 — payroll

**The guardrail conflict, resolved.** The change list asks to "re-compute the
frozen payroll"; the standing rule says a CLOSED run is a frozen snapshot never
recomputed. Both are right — they were about **different documents**:

| Status | Meaning | Recompute |
|---|---|---|
| `OPEN` | a **DRAFT**. Nothing paid against it. | free, no ceremony |
| `CLOSED` | **FROZEN** — the document the cash was counted against. | **409.** Reopen first. |

- `POST /wages/runs` now takes **`freeze`** (default `true`, so every existing
  caller is unchanged). Send `false` for a draft.
- `POST /wages/runs/{id}/close` freezes a draft. Idempotent; refuses an empty run.
- `POST /wages/runs/{id}/reopen` is the **only** door out of CLOSED. DM/MD, and it
  **requires a reason** (≥5 chars) that is stored and belongs on every reissued
  payslip. Counted separately from recompute: *"recomputed twice"* and *"unfrozen
  twice after payment"* are different facts, and only the second needs explaining
  to an auditor.
- A reopened run **stays a draft until closed** — three deliberate steps. Auto-
  refreezing after recompute would hand back a new frozen document nobody had
  looked at, which is how a corrected run gets paid twice-wrong instead of
  once-wrong. It is not silent: the run reads OPEN with `reopen_count: 1` and the
  ledger shows both.
- `confirm_closed: true` on recompute remains as a one-call escape hatch. It
  works, it stamps, and it records **no reason** — which is exactly why the reopen
  door is the intended route.

> 🐛 **Bug found and fixed while doing this:** `POST /runs/{id}/recompute` never
> passed `confirm_closed` from the request, and `compute_run` always closed the
> run immediately. Every run was therefore CLOSED from birth and the recompute
> endpoint **could never succeed on any run** — it 409'd unconditionally. It has
> presumably never worked in production.

**Scoped runs.** `POST /wages/runs` accepts `order_number` **or** `style_code`.
The scope is **stored on the run** so a recompute reproduces it — a recompute that
silently widened a one-style run into a whole-factory one would pay everyone twice.

> ⚠️ **A scoped run pays PIECE-RATE work only** (`piece_rate_only: true`). A
> monthly salary is a fact about a *person*, not a style; emitting it on a
> one-style run would pay it again on the next style's run in the same window.
> The overlap guard is now scope-aware: two runs over the same fortnight for
> **different** styles are allowed (disjoint pieces); an **unscoped** run still
> blocks everything in its window.

**New reporting surface** (all read the FROZEN rows, nothing re-derived):

- `GET /wages/orders` — order cards for the landing screen (order → style → rate).
- `GET /wages/runs/{id}/breakdown` — one run folded **three ways**: per style
  (with its stages and workers nested), per stage factory-wide, per employee.
  All three are folds of the **same** frozen row set, computed server-side, so the
  three tabs cannot disagree with each other or with the run total.
- `GET /wages/runs/{id}/pieces` — **per-piece** detail: garment, stage, worker,
  **employee barcode**, and what that one piece paid. Pieces come from
  `production_event` (immutable history); the money comes from the frozen
  `(employee, style, operation)` cell. A rate corrected after close does not change
  what this says the run paid. Unpriced rows return `amount: null` **with a
  `note`**, never `0` — a zero reads as work worth nothing.
- `GET /wages/ledger` — every run, **latest computed first**, searchable by order /
  style / window / status, each row carrying `recompute_count` and `reopen_count`.

---

### 1.5 `DONE` · Item 1 — remove Overview + Risk Alerts, surface alerts on the dashboard

`GET /dashboard/alerts` — **readable by every manager role**, not the DM alone.
Three blocks, most-actionable first: `bottleneck` (the **deepest queue**, not the
first unfinished stage), `stage_spread`, `freight_risk`, plus `alert_count` for a
badge.

The three removed routes answer **410 Gone**, not 404, and the body names the
replacement — same courtesy `/production/cutting` and `/production/scan` were
given. A 404 on a screen that worked yesterday reads as "the server is broken".
**Delete the stubs one release after the frontend stops calling them.**

---

### 1.6 `DONE` · Item 6 — store drawer search

`GET /drawers?code=` — case-insensitive **contains**, so `42` and `drw-004` both
work. `GET /drawers/by-code/{code}` returns the full drawer detail body, so a
searched row opens exactly like a clicked one (which is what puts the Send button
*inside* the drawer as well as outside it). Use `limit=10` for the "10 latest
drawers" production view.

---

### 1.7 `DONE` · Item 7 — material CRUD + material barcodes

- `GET /materials/lots/{id}` · `PATCH /materials/lots/{id}` ·
  `PATCH /materials/lots/{id}/adjust` · `DELETE /materials/lots/{id}`
- `GET /barcode/materials` — the material-barcode print screen.

**Deliberately NOT patchable:**
- `category` / `subtype` / `uom` — they decide the lot's unit and its whole
  required-field set. `LEATHER → LINING` would leave a lot measured in dcm
  claiming to be metres, and every cut event pointing at it would silently
  re-denominate. Retire and recreate.
- `on_hand` — it is a **ledger**, not a field. It moves by receiving and by
  cutting. Use `/adjust`, which records a **movement + a reason** so the ledger
  still adds up and someone can ask a year later why 40 dcm disappeared.

`DELETE` is a **retire**, never a hard delete: the lot row and every cut event
survive (that is the consumption history the costing screens are built on), and
the barcode goes RETIRED so an old label scans **410 Gone** — *"this lot was
retired"* — rather than 404 *"invalid barcode"*. Refuses while stock is reserved.
`MATERIAL_SPEC` validation on create is untouched; `available` stays derived.

> **Naming:** the backend has only ever called it `DRAWER` — there is no `bucket`
> anywhere in the API. The *"bucket barcode" → "drawer barcode"* rename is
> **frontend-only**.

---

### 1.8 `FRONTEND` · Item 5 — factory lat/long

**No backend change needed.** Every endpoint already accepts `lat`/`lon`
(`/attendance/check-in`, `/check-out`, `/proxy/*` require them; `/scan-check-in`
takes them optionally). `GET /attendance/config` already returns
`factory_lat` / `factory_lon` / `radius_m` — **but only to HR/DM/MD/managers**;
other roles get shift times without the fence, deliberately, so a client login
cannot read the coordinates needed to forge an in-fence check-in.

**Frontend contract:** capture via browser geolocation or a map pin. The user must
never type raw coordinates into two decimal boxes.

### 1.9 `FRONTEND` · Item 8 — remove the Direct-Manager Flow section

Nothing in the backend serves a route by that name. Confirm no frontend route
references it and delete.

### 1.10 Item 4 — copy of the app

Staging / duplicate-DB work. **Hamthan owns**; out of scope here.

---

## 2 · The four CTO decisions

| # | Decision | Resolved as | Needs your sign-off? |
|---|---|---|---|
| 1 | Drawer pool size & growth | **200 is the pool.** It only grows when DM/MD calls `POST /drawers/pool`, and it never shrinks. New drawers are **permanent and barcoded**, and recycle like the rest. Overflow is a waiting list, not temporary drawers. | ✅ Implemented — **confirm 200 is still the number** |
| 2 | STORE access = role or permission | **Role grant on the store surface.** `STAGE_ROLE_ACCESS` untouched, so store access and stage access stay separate questions. | ✅ Implemented — **confirm stitching manager may SEND** |
| 3 | Payroll draft vs reopen | **Both.** `freeze=false` gives drafts; `reopen` (with a mandatory reason) is the audited door out of CLOSED. Ledger always reads the frozen rows. | ✅ Implemented |
| 4 | Waiting-list overflow state | **`piece.drawer_id IS NULL`** — no new table, no new state machine. A waiting piece has a barcode and an identity, just no drawer, so it cannot be stored and cannot pass the merge gate. | ✅ Implemented |

**Two open questions for you:**

1. **Is the initial pool still 200?** `INITIAL_DRAWER_POOL = 200` in
   `app/modules/imports/premint.py`. The live database may already hold more from
   the old unbounded behaviour — check `GET /drawers/pool` after deploy.
2. **Should `POST /drawers/pool` cap how many drawers one call may add?** It is
   capped at 1000 as a typo guard (`add: 20000` would mint 20,000 permanent
   barcoded drawers, and there is no un-mint). Say if that is too low.

---

## 3 · API delta

### 3.1 NEW — Imports / Breakdown (5)

#### `GET /api/v1/imports/breakdown/{order_number}`
**Purpose.** The uploaded breakdown as an editable table. The screen between
upload and production.
**Who.** DM, MD.

| Field | Type | Req | Notes |
|---|---|---|---|
| order_number | string | yes | (path) |

**Response 200**

| Field | Type | Notes |
|---|---|---|
| order_id | uuid | |
| order_number | string | |
| styles | object[] | one per style — expanded below |
| totals | object | `{styles, styles_draft, styles_released, qty_ordered, qty_draft, minted_pieces}` |

*styles[]*

| Field | Type | Notes |
|---|---|---|
| style_id / style_code / style_name / article | uuid / string? | |
| production_status | string | `DRAFT` \| `RELEASED` \| `CANCELLED` |
| released_at / released_by | datetime? / string? | |
| editable | boolean | `false` once RELEASED — render read-only |
| needs_lining | boolean | the same verdict the store gate will apply — visible **before** release |
| sku_count / qty_ordered / minted_pieces | int | `minted_pieces > 0` means real barcoded garments exist |
| skus | object[] | `{sku_id, sku_code, colour, color_code, size, qty_ordered, knit_color, nylon_color}` |

---

#### `PATCH /api/v1/imports/breakdown/skus/{sku_id}`
**Purpose.** Correct one DRAFT line. **409 once the style is RELEASED.**
**Who.** DM, MD.

**Request (body — send only what changes):** `qty_ordered` int≥0? · `color_name` str? ·
`color_code` str? · `size` str? · `knit_color` str? · `nylon_color` str?

**Response 200:** `{sku_id, sku_code, qty_ordered, colour, size, changed{}}`

---

#### `DELETE /api/v1/imports/breakdown/skus/{sku_id}`
**Purpose.** Drop a line the sheet should not have had. DRAFT + **zero minted
pieces** only — the 409 is what keeps a hard delete safe here.
**Who.** DM, MD. **Response 200:** `{deleted, sku_id, sku_code}`

---

#### `POST /api/v1/imports/breakdown/{order_number}/cancel`
**Purpose.** Withdraw DRAFT styles from the release screen.
**Who.** DM, MD. **Request:** `{style_ids: uuid[]}` · **Response 200:** `{cancelled[], rejected[]}`

---

#### `POST /api/v1/imports/breakdown/{order_number}/release`  ← **THE MINT**
**Purpose.** Release named styles into production. Creates, atomically and only
now: the `Piece` rows, their per-piece barcodes (compact `PC-XXXXXX` + the long
alias) and the drawer merge for each. Stamps each style RELEASED with actor and
time, and writes an audit row.
**Who.** DM, MD. **201.**

| Field | Type | Req | Notes |
|---|---|---|---|
| style_ids | uuid[] | yes | (body) min 1 |
| grow_drawer_pool | boolean | — | (body) default `false`. `true` mints the drawer shortfall in the same call. Opt-in so the pool never grows by accident. |

**Response 201**

| Field | Type | Notes |
|---|---|---|
| order_number | string | |
| released | object[] | `{style_id, style_code, style_name, production_status}` |
| rejected | object[] | **PARTIAL ACCEPT** — `{style_id, style_code, reason}`. One already-released style must not lose the four ticked with it. |
| minted | object | `{pieces_minted, drawers_reused, drawers_minted, pieces_waiting_for_drawer, pieces_needing_lining, sample_barcodes[]}` |
| message | string | one human sentence — safe to display verbatim |

> **Read `minted.pieces_waiting_for_drawer`, not the HTTP status.** Non-zero means
> those pieces have barcodes but nowhere to be stored, so they cannot pass the
> merge gate until a drawer frees up or the pool is grown.

---

### 3.2 NEW — Drawers / Pool (4)

#### `GET /api/v1/drawers/pool`
**Who.** MD, DM, HR, Supervisor, Cutting/Lining/Stitching Manager, Store Manager.
**Response 200:** `{pool_size, initial_pool_size, free_drawers, occupied_drawers,
pieces_waiting_for_drawer, shortfall}`. `shortfall` is what a DM must add right now
to clear the list.

#### `POST /api/v1/drawers/pool`  — **DM/MD only, one-way**
**Request:** `{add: int 1..1000}` · **201:** `{added, pool_size, allocated,
still_waiting, status{}, by}`. Adds permanent barcoded drawers **and drains the
waiting list into them** — growing the pool while pieces still wait would leave
the DM staring at empty drawers beside a waiting list. **Print the new codes:** a
drawer with no printed label cannot be scanned, so it does not exist to the floor.

#### `POST /api/v1/drawers/allocate-waiting`
**Who.** MD, DM, Store Manager, Stitching Manager. Merges drawer-less pieces into
whatever is currently free. Safe to call repeatedly; a no-op when nothing waits.
**200:** `{allocated, still_waiting, status{}}`

#### `GET /api/v1/drawers/by-code/{code}`
Same body as `GET /drawers/{drawer_id}`. **404** if no such code.

---

### 3.3 NEW — Wages (6)

#### `GET /api/v1/wages/orders`
**Purpose.** Order cards for the payroll landing screen. The drill is
**order → style → rate sheet**; it used to open straight onto hundreds of style
cards with nothing saying which order they belonged to.
**Who.** DM, MD, HR. **Query:** `on` date? · `unpriced_only` bool
**200:** `[{order_number, styles, styles_priced, fully_priced, sku_count, qty_ordered, style_codes[]}]`

#### `POST /api/v1/wages/runs/{run_id}/close`
Freeze a draft. **Idempotent** (a double-click changed nothing, so it is not an
error). **409** on a run with no lines. **Who.** DM, MD.
**Response:** `WageRunDetail`

#### `POST /api/v1/wages/runs/{run_id}/reopen`
Unfreeze a CLOSED run. **Who.** DM, MD.
**Request:** `{reason: string, 5..500}` — not optional, not decorative.
**200:** `{id, status, period_start, period_end, reopen_count, last_reopened_by, last_reopen_reason, message}`

#### `GET /api/v1/wages/runs/{run_id}/breakdown`
**The Run Engine result screen.** **Who.** DM, MD, HR.

| Field | Type | Notes |
|---|---|---|
| run_id / period_start / period_end / status | | |
| scope_order_number / scope_style_code | string? | null = whole factory |
| computed_at / recompute_count / reopen_count | | |
| total_amount / total_pieces | number / int | |
| by_style | object[] | `{style_code, style_name, pieces, amount, stages[], employees[]}` — **nested**, because that is the question the screen asks |
| by_stage | object[] | `{operation_code, operation_label, sequence, pieces, amount, rate}` factory-wide |
| by_employee | object[] | `{employee_id, employee_name, designation, pieces, amount, styles[]}` |

> **Do not sum these client-side.** All three are folds of the same frozen row
> set; re-deriving one in the frontend is how the three tabs start disagreeing.

#### `GET /api/v1/wages/runs/{run_id}/pieces`
**Per-piece payroll detail.** **Who.** DM, MD, HR.
**Query:** `style_code` str? · `limit` 1..2000 (500) · `offset`

**200:** `{run_id, status, total, count, items[]}`

*items[]*: `piece_code, serial, colour, size, style_code, style_name,
operation_code, operation_label, employee_id, employee_name, designation,`
**`employee_barcode`**`, work_date, qty, rate, amount, note`

> `rate`/`amount` are **null (never 0)** when the cell was not priced in this run
> — a monthly worker, or a stage unrated at run time — and `note` says which.

#### `GET /api/v1/wages/ledger`
**Who.** DM, MD, HR.
**Query:** `order_number` · `style_code` · `date_from` · `date_to` · `status`
(`open|closed`) · `limit` 1..200 (50) · `offset`
**200:** `{count, items[]}` where each item is
`{run_id, period_start, period_end, status, scope_order_number, scope_style_code,
computed_at, recompute_count, last_recomputed_at, reopen_count, total_amount,
total_pieces, employee_count}`

Ordered by **when it was computed**, not by period — the question the ledger
answers is *"what did we just run"*.

---

### 3.4 NEW — Materials (4) & Barcode (1)

| Endpoint | Who | Notes |
|---|---|---|
| `GET /materials/lots/{lot_id}` | stock readers | + `editable_fields`, `required_attributes` so the edit form needs no second call |
| `PATCH /materials/lots/{lot_id}` | DM, MD, Cutting, Lining | body: `article? colour? thickness? size? supplier_id?`. **409** if the new spec collides with another lot |
| `PATCH /materials/lots/{lot_id}/adjust` | DM, MD | body: `{delta: number (non-zero), reason: string 3..300}`. Records a **movement**, audited. **409** below reserved, **422** below zero |
| `DELETE /materials/lots/{lot_id}` | DM, MD | **retire**, not delete. **409** while reserved. Returns `{lot_id, is_active, barcode_retired, on_hand, history_preserved, message}` |
| `GET /barcode/materials` | MD, DM, HR, Supervisor, Cutting/Lining/Stitching | query `category?`, `active_only` (true). Rows: `{code, type, status, caption, lot_id, category, subtype, article, colour, thickness, size, uom, on_hand, label_line}` |

Encode `code` as **Code128**; typeset `label_line` under it as text. A row with
`status: retired` belongs to a retired lot — grey it, do not print it.

---

### 3.5 NEW — Dashboard (1)

#### `GET /api/v1/dashboard/alerts`
**Who.** MD, DM, HR, Supervisor, Cutting/Lining/Stitching Manager, Store Manager.
**Query:** `today` date? (override for freight risk)
**200:** `{bottleneck{stage,label,pending,queue}, stage_spread[], freight_risk[], alert_count}`

---

### 3.6 CHANGED

| Endpoint | Change |
|---|---|
| `POST /imports/commit` | **Mints nothing now.** Response gains `release_required: true` and `pieces_minted: 0`. Next call is `GET /imports/breakdown/{order_number}`. |
| `POST /drawers/send` | **STITCHING_MANAGER may now call it.** `not_ready[]` entries gain `needs_lining`, and the `reason` names *why* a lining is expected when the stored flag disagrees. |
| `GET /drawers` | New `code=` search param. `needs_lining` on every row is now the **effective** requirement; `lining_reason` added; `can_send` applies the same rule `send` enforces. |
| `GET /drawers/{id}` | `needs_lining` is now effective; `lining_reason` added. |
| `POST /drawers/store-scan` | `lining_reason` added beside `needs_lining`/`awaiting`. |
| `POST /drawers/{id}/receive` (deprecated) | Sender set widened to match `/send`; the 409 now names why a lining is expected. |
| `GET /production/piece-state` | Top-level `needs_lining` + `lining_reason` added. The `LINING_CUTTING` stage card's `not_applicable` verdict now uses the effective rule, so the screen and the store gate agree. |
| `POST /wages/runs` | New body fields `freeze` (default `true`), `order_number?`, `style_code?` (mutually exclusive → 422). Response gains `scope_order_number`, `scope_style_code`, `piece_rate_only`. |
| `POST /wages/runs/{id}/recompute` | Now accepts an optional body `{confirm_closed: bool}` — **it was declared but never wired, so this endpoint could not succeed on any run**. On a CLOSED run the 409 now names the reopen route. |
| `GET /wages/runs/{id}` | Gains `scope_*`, `piece_rate_only`, `reopen_count`, `last_reopened_at/by`, `last_reopen_reason`. |

### 3.7 REMOVED → **410 Gone**

| Endpoint | Replacement |
|---|---|
| `GET /analytics/overview` | `GET /dashboard/direct-manager`, or the role dashboards |
| `GET /analytics/alerts/stage-spread` | `GET /dashboard/alerts` |
| `GET /analytics/alerts/freight-risk` | `GET /dashboard/alerts` |

Already 410 from earlier releases: `POST /production/cutting`, `POST /production/scan`.

---

## 4 · Full endpoint inventory

**121 existing + 18 new = 139 Phase-1 endpoints** (Phase-2 `procurement` adds 62
more, out of scope). The 13-Aug PDF covered 66. `NEW` = this change set;
**`MISSING`** = present in the app but absent from the old PDF, i.e. must appear
in the regenerated one.

| Module | Count | Status |
|---|---|---|
| Auth & Users | 6 | in PDF |
| Employees | 5 | in PDF |
| Attendance | 12 | in PDF |
| Barcode | 9 | 8 in PDF + 1 NEW (`/barcode/materials`) |
| Production | 9 | in PDF |
| Drawers & Store | 9 | 5 in PDF + 4 NEW |
| Dashboards | 22 | 21 in PDF + 1 NEW (`/dashboard/alerts`) |
| **Wages** | **15** | **MISSING entirely** — 9 existing + 6 NEW |
| **Materials & Suppliers** | **12** | **MISSING entirely** — 8 existing + 4 NEW |
| **Imports** | **7** | **MISSING entirely** — 2 existing + 5 NEW |
| **Clients** | **5** | **MISSING entirely** |
| **Analytics** | **10** | **MISSING entirely** (3 now 410) |
| Chat (intelligence) | 2 | MISSING |

<details><summary><b>Wages — 15</b></summary>

`GET /wages/orders` NEW · `GET /wages/styles` · `GET /wages/rate-sheet` ·
`GET /wages/rate-history` · `POST /wages/rates` · `POST /wages/rates/bulk` ·
`GET /wages/runs` · `POST /wages/runs` · `GET /wages/runs/{run_id}` ·
`POST /wages/runs/{run_id}/recompute` · `POST /wages/runs/{run_id}/reopen` NEW ·
`POST /wages/runs/{run_id}/close` NEW · `GET /wages/runs/{run_id}/breakdown` NEW ·
`GET /wages/runs/{run_id}/pieces` NEW · `GET /wages/ledger` NEW
</details>

<details><summary><b>Materials & Suppliers — 12</b></summary>

`GET /materials/spec` · `GET /materials/stock` · `GET /materials/lots` ·
`POST /materials/lots` · `GET /materials/lots/{id}` NEW ·
`PATCH /materials/lots/{id}` NEW · `PATCH /materials/lots/{id}/adjust` NEW ·
`DELETE /materials/lots/{id}` NEW · `POST /materials/receive` ·
`POST /suppliers/orders` · `PATCH /suppliers/orders/{id}` ·
`PATCH /suppliers/orders/{id}/spec`
</details>

<details><summary><b>Imports — 7</b></summary>

`POST /imports/preview` · `POST /imports/commit` ·
`GET /imports/breakdown/{order_number}` NEW ·
`PATCH /imports/breakdown/skus/{sku_id}` NEW ·
`DELETE /imports/breakdown/skus/{sku_id}` NEW ·
`POST /imports/breakdown/{order}/cancel` NEW ·
`POST /imports/breakdown/{order}/release` NEW
</details>

<details><summary><b>Clients — 5 · Analytics — 10 · Chat — 2</b></summary>

`GET /clients` · `POST /clients` · `GET /clients/styles` ·
`GET /clients/{id}/orders` · `POST /clients/{id}/orders`

`GET /analytics/overview` **410** · `GET /analytics/alerts/stage-spread` **410** ·
`GET /analytics/alerts/freight-risk` **410** · `GET /analytics/explorer` ·
`GET /analytics/consumption` · `GET /analytics/employee-rates` ·
`GET /analytics/orders/{id}/tree` · `GET /analytics/styles/{id}/detail` ·
`GET /analytics/pieces/detail` · `GET /analytics/pieces/{code}/story`

`POST /chat` · `POST /chat/stream`
</details>

<details><summary><b>Drawers & Store — 9</b></summary>

`GET /drawers` (+`code=`) · `GET /drawers/pool` NEW · `POST /drawers/pool` NEW ·
`POST /drawers/allocate-waiting` NEW · `GET /drawers/by-code/{code}` NEW ·
`POST /drawers/send` · `POST /drawers/store-scan` · `GET /drawers/{id}` ·
`POST /drawers/{id}/receive` *(deprecated)*
</details>

> **Also correct in the regenerated PDF:** `DrawerLabel.sent_to` no longer exists
> (dropped by migration `20260813_drop_drawer_sent_to`), and `POST /drawers/send`
> takes **no `destination`** — the store sits at one point in the pipeline, so a
> merged drawer has exactly one way forward.

---

## 5 · Migration & deploy runbook

**One migration:** `20260817_release_payroll` (down_revision `20260813_drop_drawer_sent_to`).

```
style.production_status  VARCHAR(20) NOT NULL DEFAULT 'DRAFT'  + index
style.released_at        TIMESTAMPTZ NULL
style.released_by        VARCHAR(120) NULL
wage_run.scope_order_number  VARCHAR(50)  NULL + index
wage_run.scope_style_code    VARCHAR(200) NULL + index
wage_run.reopen_count        INTEGER NOT NULL DEFAULT 0
wage_run.last_reopened_at    TIMESTAMPTZ NULL
wage_run.last_reopened_by    VARCHAR(120) NULL
wage_run.last_reopen_reason  VARCHAR(500) NULL
```

**No native PG enum is touched**, so the migration is **fully transactional** — no
`autocommit_block`, nothing to un-wedge if it fails halfway. That property is worth
keeping: check it before adding anything to this file.

`production_status` is VARCHAR, not a native enum, deliberately. `app_user.role`
is a native enum and adding a label to it has broken a deploy **three times**
(`20260804`, `20260810_role_case`, the `STORE_MANAGER` add in `20260813`) — the
trap being that `Enum(PyEnum, name=…)` persists the **member NAME**, so a
hand-written migration adding the lowercase **value** creates a label the ORM will
never emit. Adding a fourth release state must never need an `ALTER TYPE`.

**Backfill inside the migration:** every style that already has minted pieces is
stamped `RELEASED`. Styles with no pieces stay `DRAFT` and can be released
properly.

**Deploy order**
1. `alembic upgrade head`
2. Deploy the backend.
3. `GET /drawers/pool` — record `pool_size` and `pieces_waiting_for_drawer`.
4. *(optional, cosmetic)* `python -m scripts.backfill_needs_lining --dry-run`, then
   `--only-false` to realign stored flags with the live rule. The gate no longer
   depends on it; this only makes the dashboards agree.
5. Deploy the frontend.
6. One release later: delete the three analytics 410 stubs and
   `POST /drawers/{id}/receive`.

**Test status:** 788 passed. The 9 failures + 10 errors are **pre-existing and
identical to the pre-change baseline** (employee-login invariants, attendance
proxy, `test_gates_pure` designation cases, and the lining-manager lot-writer
audit finding). 49 tests added.

---

## 6 · Frontend action list

**Riswan**
- Delete the Analytics Overview and Risk-Alert pages. Render `GET /dashboard/alerts`
  on every manager dashboard — `alert_count` as the badge, `bottleneck` as the
  headline.
- New material-barcode screen from `GET /barcode/materials`.

**Nishath**
- **Stage spreadsheet** — three searchable inputs (order / style / piece).
  Order → `GET /dashboard/direct-manager/orders/{id}`; style →
  `GET /dashboard/direct-manager/styles/{id}`; piece →
  `GET /dashboard/pieces/{piece_code}` (full stage history incl. STORE overlay).
  Existing endpoints — no backend work.
- **Payroll** — order cards (`GET /wages/orders`) → style cards
  (`GET /wages/styles?order_number=`) → rate sheet. Run Engine posts
  `POST /wages/runs` with `freeze:false`, renders
  `GET /wages/runs/{id}/breakdown` (three tabs, do **not** re-sum), then
  `POST /runs/{id}/close`. Ledger = `GET /wages/ledger`. Per-piece +
  employee barcode = `GET /wages/runs/{id}/pieces`.
- **Geolocation** — capture lat/lon from the browser/map; never two decimal boxes.
- **Store** — `GET /drawers?limit=10` for the production view, `?code=` for search,
  `GET /drawers/by-code/{code}` for the row, Send inside **and** outside the drawer.
- **Material stock section** — full CRUD against `/materials/lots*`. Note `on_hand`
  is edited via `/adjust` (delta + reason), not by typing a new total.
- **Breakdown screen (new)** — `GET /imports/breakdown/{order_number}` table,
  inline edit on DRAFT rows, then multi-select → `POST .../release`. Surface
  `minted.pieces_waiting_for_drawer` prominently.
- Rename **"bucket barcode" → "drawer barcode"** (label only).
- Remove the Direct-Manager Flow section.

**Both**
- Handle **410** as *"this screen was removed"*, distinct from 404.
- Render `lining_reason` wherever `needs_lining` is shown — that string is what
  tells an operator *why* a garment the sheet says needs no lining is waiting for
  a lining.

---

## 7 · Still pending in the backend

| Item | Why it is not done | Owner |
|---|---|---|
| Duplicate app / staging stack | Infra, not code. develop/main split, two Docker stacks, Caddy. | Hamthan |
| `needs_lining` data backfill on the live DB | Deliberately a script, not a migration — it changes production data on a heuristic that may change again, so it should be a decision with a dry run first. The gate no longer depends on it. | Hamthan |
| Three known schema gaps: **damage tracking**, **BOM baseline consumption**, **employee daily target / photo** | No table backs them. Dashboards return `null` with `meta.unsupported` rather than fabricating a column. Needs a product decision before modelling. | CTO |
| Analytics 410 stubs + `POST /drawers/{id}/receive` | Kept one release for a frontend mid-deploy. Delete after the frontend ships. | Backend |
| Postgres CI job | The suite runs on SQLite, which proves the logic but not the migration or the native-enum path. Every enum incident so far was invisible to SQLite. | Backend |
| Pre-existing test failures (9 + 10 errors) | Untouched by this change set — they predate it and each is a separate finding (employee-login invariants, attendance proxy, `test_gates_pure` designation table, lining-manager lot-writer). | Backend |
| `MERCHANDISER` role | Can log in; every `require_roles` gate 403s it. No route grants it anything. | CTO — decide its access |

---

*KairoX ERP · Phase 1 · backend change set of 17 August 2026. Endpoint lists were
enumerated from the running FastAPI app, not from the previous document.*
