# 1. What this service is

This is the **cutting grid** — the screen that replaced the cutting manager's Excel sheet.

Before it, every garment's real cutting record lived in a spreadsheet: which hides went to which cutter, what each one measured, the skin he handed back, the extra one he asked for. The system saw none of that — only a single total dcm, typed once at the end. So it could not answer the question the factory actually asks: **how much leather did this jacket take?**

The grid is that spreadsheet, with three things a spreadsheet cannot do:

1. **The hides are real stock rows.** Claiming one makes it unavailable to everybody else.
2. **The total is derived** from the hides on the row, so it cannot disagree with its parts.
3. **Approval is an audited transition**, so the number that reaches the ledger carries a name and a time.

| Part | File |
|---|---|
| HTTP routes | `app/modules/cutting/router.py` |
| Business rules | `app/modules/cutting/service.py` |
| Table | `cutting_row` (in `app/modules/cutting/models.py`) |
| The hides | `material_sheet` (materials module) |
| Estimating sizes | `app/core/leather_norms.py` |

**Who:** the **Cutting Manager** owns this screen; DM and MD are on it because they are on everything. The **Lining Manager is deliberately absent** — lining is cut by the metre and has no hides to track.

---

# 2. One row is ONE garment

That is the sentence to hold on to. A row is not a style, not a batch and not a day's work. It is one jacket, with its own hides, its own cutter and its own total.

```
   style CLERMONT · colour DARK BROWN · 40 garments
   ->  40 rows
       row 1  piece …-001  hides: LS-41-03 (44), LS-41-07 (41), …  cutter: Ramesh
       row 2  piece …-002  hides: LS-41-11 (43), …                 cutter: Salim
       ...
```

---

# 3. The flow, in the order the floor does it

```
 1. GET  /cutting/grid?style_id=&colour=          open the sheet
 2. POST /cutting/rows/generate                   one DRAFT row per un-cut garment,
                                                  with hides already allocated
 3. the hides go to the cutter
    POST   /cutting/rows/{id}/sheets              he needed one more
    DELETE /cutting/rows/{id}/sheets/{sheet_id}   he handed one back
    PATCH  /cutting/rows/{id}/sheets/{sheet_id}   a measurement was wrong
 4. POST /cutting/rows/assign                     fill the cutter column
    or name the cutter on the approve call
 5. POST /cutting/rows/{id}/approve               FREEZE — audited
 6. the cutter's scan logs the cut                (Production service)
```

## Step 1 — the grid

`GET /cutting/grid` returns the rows for one style and colour, plus what the header needs: every colour of that style, how many pieces are still un-cut, and **`present_cutters`**.

> `present_cutters` is who is actually checked in today. The grid must not offer a name that cannot legally be logged, because production **refuses to log an absent worker** — a grid that let one be assigned would collect the work and then fail at the scan, after the leather was already handed out.

## Step 2 — generate

`POST /cutting/rows/generate` creates one DRAFT row per un-cut garment and allocates roughly ten hides to each, sized to the garment.

- **Safe to press twice.** Rows are only created for pieces that have none.
- `material_lot_id` is optional. Omitted, the service picks the only leather lot matching the style's article and colour — and **409s when several match** rather than guessing which hides to spend.
- `allocate: false` generates rows with no hides, for a manager who would rather scan them himself.
- `limit` caps how many rows are made in one go.

### The allocation is an estimate, on purpose

Nobody supplies a per-piece dcm for most styles, so the allocator aims at the norms in `core/leather_norms.py` and **expects to be corrected**. Getting it roughly right turns ten manual picks into one correction; refusing to guess turns the screen back into the Excel.

Each row says what it aimed at and whose number that was:

| `target_source` | Meaning |
|---|---|
| `style_spec` | a measurement somebody signed off in the style's material recipe |
| `size_baseline` | this system's own estimate from the size |

Show that difference. It tells the manager whether to expect to correct the row.

### `cutter_employee_id` is NOT accepted on generate

Sending it is a **422** that names the two routes to use instead.

Generate mints one row per garment — routinely 40+. A cutter on that body would be the cutter on **every** row of the style, which says one person cut all forty jackets. That is not cosmetic: **`cutter_employee_id` is what the piece-rate wage line is paid from**, so one id there pays one worker for everybody's work.

The field is still declared so an old client gets that clear 422 instead of having its cutter silently dropped — a dropped cutter is a whole style approved with nobody on it.

## Step 3 — hides come and go

### Adding one: type the measurement

`POST /cutting/rows/{row_id}/sheets` with **`dcm`** is the normal path, and it is a **lookup, not a create**. The cutter reads the number written on the skin, types it, and the system resolves it to the hide of this row's article and colour that measures that. No code to find, no list to pick from.

How the match works:

1. **Exact** match first.
2. Otherwise the **nearest** hide within a tolerance: **2 % of the typed value, never tighter than 1 dcm**. A hand measurement copied by hand means 223.5 gets typed for a 223 or a 224; demanding exactness pushes the operator straight back to picking from a list.
3. Outside that band the call is **refused with 422** and **names the hides that are on the shelf** (up to 8).

The response carries **`matched_sheet`** — which skin actually left the shelf. The operator typed a number and never chose a code, so the reply has to say what it picked.

You can also send `sheet_code` (scan it) or `sheet_id` (pick it).

> `create_if_missing: true` mints a **new** hide at that measurement when the shelf has nothing matching. It **adds a stock row**, so it must be asked for deliberately. This used to be what a typed measurement did by default, which put stock in that nobody had received.

`GET /cutting/rows/{row_id}/sheet-options` is the lookup behind the dcm box: what is on the shelf for this row, optionally ranked by distance from a measurement the operator is about to type.

### Removing one

`DELETE /cutting/rows/{row_id}/sheets/{sheet_id}` — he handed it back. It returns to the shelf, available to the next row.

### Correcting one

`PATCH /cutting/rows/{row_id}/sheets/{sheet_id}` with a new `dcm`. **The correction follows the skin, not the row** — it is that hide's measurement everywhere.

## Step 4 — the cutter column

`POST /cutting/rows/assign` is the grid's Save button: **one entry per row**, so different garments of the same style go to different people.

It is **partial accept**: a frozen or missing row comes back in `rejected` and never loses the rows that did assign. Read `assigned` and `rejected`, not the status code.

Forty separate PATCHes would be forty chances to lose half of them — that is why the bulk form exists.

## Step 5 — approve (the freeze)

`POST /cutting/rows/{row_id}/approve`

From here, these hides and this total are what the production log will spend and what this garment's leather cost is computed from.

| Check | Result |
|---|---|
| row already APPROVED | **no-op**, 200, `"Row was already approved."` — the manager could not tell whether the first tap landed, and punishing them for checking is how people learn to avoid the button |
| row is not DRAFT | **409** |
| row has no hides | **409** — "there is nothing to approve. Add the hides the cutter was given first." |
| row has no cutter | **409** — name them on this call or PATCH the row first |

**Name the cutter here.** `cutter_employee_id` on the approve body re-assigns the row before it freezes, so the common path is one call per garment: approve with the person who cut it.

### What approval does

- `total_dcm` = the sum of the row's hides, computed by the server.
- `status` → `APPROVED`, with `approved_by` and `approved_at`.
- every hide on the row → `ISSUED`.
- an `audit_log` row naming the cutter, the hide codes, the total and the target.

> **Stock does not move at approval.** The hides go `ISSUED` — out of the shelf's reach — but `lot.on_hand` is untouched, because consumption still happens at the **cutting log**. That keeps **one decrement point** for leather, and leaves every consumption test, shortfall warning and analytics read true.

## Un-freezing

`POST /cutting/rows/{row_id}/reopen?reason=…` puts an approved row back to DRAFT. It is recorded, because it un-signs a signature.

---

# 4. Row states

| State | Meaning |
|---|---|
| `DRAFT` | the manager is still editing. Hides may be added or removed freely; nothing is promised to anyone. |
| `APPROVED` | the cutter confirmed the hides and the manager signed off. These numbers are what the production log will spend. |
| `LOGGED` | the `LEATHER_CUTTING` event exists; the hides are consumed. |
| `CANCELLED` | abandoned; its hides went back to stock. |

> Approval is a **state with a timestamp, an actor and an audit row**, not a boolean `is_approved`. It moves stock and decides what a garment cost — the same rule the breakdown release and the store release follow.

---

# 5. How this connects to production

When the cutter's scan reaches `POST /production/log`, the logger looks for this garment's **approved** cutting row. If it finds one, it does not re-ask for article, colour, lot and dcm — a human already entered and approved them.

```
   approved cutting row ──► production log reads it
                            ──► LEATHER_CUTTING event
                            ──► leather decremented ONCE
                            ──► row marked LOGGED, hides CONSUMED
```

So the grid is the **data-entry** half and the scan is the **confirmation** half. Neither duplicates the other.

---

# 6. Warnings

Rows and the grid carry `warnings[]` — short, **non-blocking** notes: short of stock, over or under the target, no cutter set yet. They never refuse anything. Render them beside the row; they are what tells a manager where to look before pressing approve.

---

# 7. Notes for backend developers

- **Allocation is a state on the hide, not a list on the row.** `material_sheet.cutting_row_id` points from the hide to the row, and the hide's status moves to `ALLOCATED`. That makes "one hide on two garments" **unrepresentable** rather than merely forbidden — a list column could hold the same hide twice and send two cutters for one skin.
- **`total_dcm` is derived**, never typed. Do not add a write path for it.
- **`actor_user_id` on the audit row is a LOGIN id**; the worker is `row.cutter_employee_id` and is a different person in a different table. Do not mix them.
- **Lazy imports** into `materials.service` keep the module graph acyclic. Do not hoist them to the top of the file.
- The tolerance constants (`_DCM_TOLERANCE_FRACTION`, `_DCM_TOLERANCE_FLOOR`, `_NEAR_MISS_LIMIT`) are the tuning knobs for the typed-measurement lookup.
