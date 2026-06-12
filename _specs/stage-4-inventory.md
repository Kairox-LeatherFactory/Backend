# Stage 4 — Inventory Check Spec: BOM-vs-Stock Comparison & Shortfall Resolution

**Status:** Draft for MD / stakeholder review
**Scope:** The Stage-4 *inventory check* only — loading the inventory master into
`inventory_item`, matching every stockable BOM line to stock, computing
required / on-hand / balance / status per line, the reservation (allocation)
policy, the UI report shape, the per-approval performance approach, and the
matching/unit edge cases. It produces the per-BOM stock-status report and the
shortfall lines that **Stage 5** turns into supplier POs. **No code, no
migrations** are part of this document.
**Builds on:** `_specs/stage-0-foundation.md` (schema §1d, ingestion §2, roles §5),
`_specs/stage-2-bom-generation.md` (the `bom`/`bom_item` tree + `bulk_qty` math),
and `_specs/stage-3-approval.md` (the approve/lock edge that fires this check).
**Source of truth for the process:** `data/BOM_Procurement_Workflow_fixed.pdf` /
`data/_wf.txt`, Stage 4 ("Inventory Check"). **Master data:** `data/INVENTORY (1).xlsx`.

> **Stack facts (re-confirmed against the code, not stage prose).** The Stage-4
> tables already exist in `app/modules/procurement/models.py`: `InventoryItem`
> (`description`, `normalized_key`, `uom`, `qty_on_hand` Numeric(12,3), `rate`,
> `category`, `color`, `article_ref`, `is_active`), `InventoryCheck` (`bom_id`,
> `status`, `run_at`, `run_by`) and `InventoryCheckLine` (`inventory_check_id`,
> `bom_item_id`, `inventory_item_id?`, `required_qty`, `on_hand_qty`,
> `shortfall_qty`, `status`). The enums `InventoryCheckStatus` (`running`/`complete`)
> and `InventoryLineStatus` (`sufficient`/`partial`/`out_of_stock`) are defined in
> `enums.py`. `BomItem` already carries `bulk_qty` (= `order_qty × qty_per_garment`,
> computed by `costing.recompute_bom`), `uom`, `material_color`, and `category`
> (`BomItemCategory`). **What does NOT yet exist:** any inventory *importer*; an
> `InventoryReservation` table (§3 recommends adding one); a `uom_conversion`
> reference; the `inventory_check` service / repository / router / presenter; and any
> seed for `inventory_item`. The real master `data/INVENTORY (1).xlsx` is the sheet
> **"Extracted Data"**, 1,525 rows × 4 cols `DESCRIPTION | NOM | PCS | RATE` (NOM = the
> UOM, PCS = on-hand qty, frequently blank), with duplicate descriptions, section/
> noise rows (`FACTORY NETWORK-APR-2025…`, `MARCH MONTH-2025 USAGE`, `I-CATEGORY
> MEMBERSHIP FEE`, `EXPORT-FREIGHT CHARGES`), and UOMs `KGS / ROLL / DCM / NOS / MTRS
> / REEM / PCS / CARTONS`.

---

## 0. Context & why this stage exists

Stage 3 hands off an **approved + locked** `bom` whose numbers are frozen
(`status ∈ {approved, locked}`, `locked_at` stamped, edits return `409 bom_locked`).
Stage 4 is the **procurement reality check**: for every material/consumable line in
that BOM it asks *"do we already have this in stock, and how much must we still
buy?"* It writes one `inventory_check` per approved BOM with a
`SUFFICIENT / PARTIAL / OUT_OF_STOCK` line per stockable `bom_item` and an exact
`shortfall_qty`. Those shortfall lines are the **input to Stage 5** (the
`po_item.bom_item_id` / `po_item.inventory_item_id` links already model that handoff).

Stage 4 owns: the inventory-master ingestion, the BOM→stock matching, the per-line
required/on-hand/balance/status math, the allocation (reservation) policy, and the
report the UI renders. It does **not** generate the supplier PO (Stage 5), send any
supplier email/escalation (Stage 5), or alter the BOM (the BOM is locked).

### The hard problems (why this is not a trivial subtraction)

1. **The master is dirty and the names don't match the BOM.** The BOM line is
   `SHEEP GLASS` / `BLACK`; the inventory row is `SHEEP NAPPA BLACK & PACKING CHARGES`.
   Matching is the crux (§5), and it must be deterministic-first per the platform's
   standing "no LLM unless required" rule (stage-0 §4).
2. **The same physical article appears many times** in the sheet (duplicate
   `SHEEP NAPPA BLACK & PACKING CHARGES` rows at qty 410 / 446 / 673…). On-hand for one
   article is a **sum across lots**, not a single cell (§6, edge case 3).
3. **Units don't line up.** BOM leather DCM is `dm²`; the sheet calls it `DCM`; threads
   are `NOS`, fabric `MTRS`, packaging `KGS`. A unit mismatch must never silently
   produce a false "sufficient" (§6, edge case 2).
4. **Two approved BOMs can want the same stock.** If the check only *computes*, both
   read full stock and both look satisfied, and Stage 5 under-orders. The allocation
   policy (§3) is what keeps the second BOM honest.

### Decisions (confirmed with the product owner)

1. **Ingestion = a recurring, idempotent normalizing importer** (preview + commit,
   mirroring `app/modules/imports`), not a one-shot paste. The spreadsheet is the
   **authoritative physical-stock snapshot**; re-running it re-syncs counts. Conflict
   resolution and the "don't clobber live commitments" rule are in §2.
2. **The check both computes *and* reserves**, via a **separate soft-allocation
   ledger** (`inventory_reservation`), **never** by mutating `qty_on_hand`. `available
   = qty_on_hand − Σ active reservations`. This is the only way concurrent approvals
   don't double-spend the same stock (§3). Releasing a reservation is a lifecycle event,
   not a stock edit.
3. **Matching = deterministic `normalized_key` exact match first**, an optional
   flagged fuzzy fallback for the ambiguous minority, and **never a silent guess** — an
   unmatched line is `OUT_OF_STOCK` with an explicit `unmatched` flag, not a fabricated
   match (§5).
4. **The check runs in SQL** (set-based join + grouped aggregate + `CASE`), inside the
   approval transaction, with `SELECT … FOR UPDATE` on the matched stock rows so the
   reservation write is race-safe (§7). In-memory is rejected with reasoning.
5. **Units are resolved through a small `uom_conversion` reference** (config-seeded,
   like the Stage-1/2 YAML registries); an unconvertible mismatch is a **flagged
   `needs_review`**, computed conservatively, never a false pass (§6).

---

## 1. Position in the workflow (non-goals)

```
 [Stage 3]                        [Stage 4 — THIS SPEC]                      [Stage 5]
 ┌──────────────┐   approve/lock  ┌──────────────────────────────────────┐  ┌─────────────┐
 │ MD approves  │ ───────────────►│ inventory_check:                      │  │ supplier PO │
 │ + locks BOM  │                 │  match bom_item → inventory_item      │─►│ from each   │
 └──────────────┘                 │  required / on_hand / shortfall / stat│  │ shortfall   │
                                   │  reserve available stock per BOM      │  │ line        │
                                   └──────────────────────────────────────┘  └─────────────┘
```

**In scope:** inventory ingestion + normalization, BOM→stock matching, the per-line
required/on-hand/balance/status math, the reservation ledger + its lifecycle, the
report shape, the per-approval performance path, and the matching/unit edge cases.

**Out of scope (Stage 5+):** generating `purchase_order`/`po_item`, supplier
resolution, the email/auto-call escalation, goods-receipt posting (incrementing stock
when a PO is fulfilled — Stage 5/6), and the material-ready MD alert (Stage 6). Stage 4
*forwards* shortfalls; it does not act on them.

---

## 2. Loading `INVENTORY (1).xlsx` — recurring idempotent sync, not one-shot

### 2a. Decision: recurring sync (one-shot is a degenerate first run)

Stage-0 §2 classified the inventory master as **(a) one-shot import + (d) pg_dump
snapshot** because the live check *mutates* it. Stage 4 **refines that**: because the
real warehouse count drifts (consumption, receipts, stocktakes), the factory will
re-export this sheet periodically, so the importer must be **re-runnable** — the
first run is just the empty-table case of the recurring path. It mirrors the existing
`imports` module's proven two-step, idempotent, replace-on-key design (preview →
commit; `app/modules/imports/README.md`).

| Method & path (under `/api/v1/procurement`) | Purpose | Auth |
|---|---|---|
| `POST /inventory/preview` | Upload the `.xlsx`, return the **normalized, deduped** parse + warnings (rows dropped, units coerced, duplicates merged) — **no writes**. | DM / MD |
| `POST /inventory/commit` | Re-parse + validate + **upsert** into `inventory_item` (the sync). | DM / MD |
| `GET /inventory/items` | Paged/searchable stock list (the master view + the FE picker). | DM / MD / VIEWER (read) |

The sync work (openpyxl) is blocking → runs in `run_in_threadpool` (house rule
CLAUDE.md §3.3), exactly as the imports handler and the Stage-1 sniff path do.

### 2b. Normalization (what the importer must clean)

The raw sheet is `DESCRIPTION | NOM | PCS | RATE`. The importer:

1. **Drops non-stock rows.** Section banners and ledger noise (`FACTORY NETWORK…`,
   `MARCH MONTH-2025 USAGE`, `I-CATEGORY MEMBERSHIP FEE`, `EXPORT-FREIGHT CHARGES`,
   any row with no UOM *and* no qty *and* no rate) are excluded — recorded in the
   preview's `dropped` list, never silently swallowed.
2. **Coerces types.** `PCS → qty_on_hand` Numeric(12,3) (blank ⇒ 0), `RATE → rate`
   Numeric(12,2), `NOM → uom` (uppercased, mapped to the canonical UOM set).
3. **Computes `normalized_key`** from `DESCRIPTION` (the matching key, §5): uppercase,
   strip, collapse internal whitespace/punctuation, drop boilerplate suffixes
   (`& PACKING CHARGES`, trailing parentheticals), and split a trailing colour token
   into `color`. Stored alongside the raw `description` (kept verbatim for audit).
4. **Dedups within the sheet by `normalized_key`** (the 410/446/673 lot rows):
   `qty_on_hand = Σ`, `rate = ` the qty-weighted average (or last non-null when qtys are
   blank), `uom = ` the modal UOM (a per-key UOM disagreement is a **preview warning**,
   not a silent merge). This collapses the multi-lot problem at ingest time so the
   check sees **one row per article** (§6 edge case 3 trade-off discussed there).

### 2c. Conflict resolution on re-sync (the crux of "recurring")

The live `inventory_item` table may already hold reservations (§3) and may have been
edited since the last import. The upsert is keyed on **`normalized_key`** and resolves
field-by-field:

| Field | On re-sync | Why |
|---|---|---|
| `qty_on_hand` | **Sheet wins** (snapshot replace). | The spreadsheet is the authoritative *physical* count at export time. Reservations are a **separate** ledger (§3) and are *not* stored in `qty_on_hand`, so overwriting it never destroys a commitment — `available` is always recomputed as `qty_on_hand − Σ active reservations`. This is the single design choice that makes a blind snapshot replace safe. |
| `rate`, `uom`, `category`, `color`, `article_ref` | Sheet wins if present, else keep existing. | Metadata refresh; never blank out a known value with a sheet gap. |
| rows **present** in DB, **absent** from the sheet | **Soft-deactivate** (`is_active = False`), do **not** hard-delete. | A dropped line may still be referenced by an open `inventory_check_line` / reservation; deletion would orphan FKs. Reactivates automatically if it reappears. |
| rows **new** in the sheet | insert (`is_active = True`). | New articles onboard with no code change. |

Idempotent + deterministic (the house invariant): committing the same sheet twice
yields identical rows. The preview is the dry-run safety net the product owner gets
before any live count is touched.

> **pg_dump (stage-0 §2 (d)) still applies** for staging/test fixtures: snapshot the
> cleaned table so CI restores a known 1,525-row master without re-running the parse.

---

## 3. Reservation semantics — compute *and* reserve, via a soft ledger

### 3a. The decision and why

| Option | Behaviour | Verdict |
|---|---|---|
| **Compute only** (stateless) | Each check reads full `qty_on_hand`; nothing is held. | ❌ Two approved BOMs both see the same 600 dm² and both report "sufficient"; Stage 5 then orders too little. No protection against double-spend. |
| **Reserve by decrementing `qty_on_hand`** | Approval subtracts the allocated qty from stock. | ❌ Conflates *physical stock* with *commitments*; breaks the §2c "sheet wins on re-sync" rule (a re-import would wipe the decrement); makes "what do we physically have" unanswerable. |
| **Reserve via a separate ledger** ✅ | `qty_on_hand` stays physical truth; an `inventory_reservation` row holds each BOM's claim; `available = qty_on_hand − Σ active reservations`. | ✅ Concurrent approvals don't double-spend; re-sync is safe; reservations are releasable; the physical count and the committed count are both always queryable. |

**Recommendation: reserve via a separate ledger.** On a Stage-4 run, for each matched
line the check reserves `min(required, available)` against the BOM and writes the
shortfall (`required − reserved`) to the `inventory_check_line`. The reservation makes
the *next* approval that wants the same article see a smaller `available`.

### 3b. New table — `inventory_reservation` (patch to stage-0 §1d)

Same conventions as `models.py` (`UUIDMixin` + `TimestampMixin`, portable `GUID`,
VARCHAR-of-`.value` enums, FKs by table-name string). Registered in `alembic/env.py`
per stage-0 §3; additive, reversible migration.

| Field | Notes |
|---|---|
| `id`, timestamps | |
| `inventory_item_id` FK `inventory_item` | the stock the claim is against |
| `bom_id` FK `bom` | who holds it (the approved BOM) |
| `inventory_check_line_id?` FK `inventory_check_line` | provenance of the claim |
| `qty` Numeric(12,3) | the held amount (≤ available at claim time) |
| `status` String(20) — new enum `ReservationStatus` `ACTIVE / RELEASED / CONSUMED` | `active` counts against `available`; `released`/`consumed` do not |
| `released_at?` / `released_reason?` | audit of why it was freed |
| **index** `(inventory_item_id, status)` | the `Σ active` aggregate reads this |

`available(item) = qty_on_hand − Σ(reservation.qty WHERE status='active')`. New enum
`ReservationStatus` in `procurement/enums.py` (str-enum, VARCHAR — module convention).

### 3c. Reservation lifecycle (when stock is freed)

- **Claim:** on the Stage-4 run for an approved BOM → `active` rows, written in the
  same transaction as the check, under `FOR UPDATE` on the matched stock (§7) so two
  approvals can't both grab the last units.
- **Release → `released`** when the BOM's commitment ends: a BOM/order **cancellation**,
  or a `rejected → reopen` cycle (stage-3 §1b) that supersedes the locked numbers, or a
  manual procurement release. A released reservation immediately raises `available` for
  everyone else. Idempotent: releasing an already-released row is a no-op.
- **Consume → `consumed`** when the material is physically issued to production
  (Stage 6 territory) — terminal, retained for audit. Modeled here so the enum is
  complete; the *trigger* is out of Stage-4 scope.
- **Re-running the check** for the same BOM (e.g. after a re-sync) **releases the prior
  run's reservations first, then re-claims** against fresh `available`, so a BOM never
  holds two stacked allocations. The check is therefore idempotent per BOM.

> Because an approved BOM is locked (stage-3), its *requirement* can't drift under the
> reservation; only stock and competing BOMs move. That keeps the ledger simple.

---

## 4. Per-line comparison — required / on-hand / balance / status

### 4a. Which BOM lines are checked

Only **stockable** categories. `MANUFACTURING` (cutting/stitching) and `FOB_CHARGE` are
services/charges with no inventory and are **excluded** (they carry a `bulk_qty` but
nothing to source). Everything physical is included: `MAIN_MATERIAL`, `SUB_MATERIAL`,
`LINING`, `INTERLINING`, `THREAD`, `ACCESSORY`, `PACKAGING` (the sheet really does carry
plastic sheets, butter paper, thread cones — packaging is stock).

### 4b. The math (per included `bom_item`)

```
required   = bom_item.bulk_qty                      # = order_qty × qty_per_garment (costing.py)
on_hand    = Σ qty_on_hand  over inventory rows matched to this line   (§5)
available  = on_hand − Σ active reservations(other BOMs)               (§3)
reserved   = min(required, available)               # this BOM's new claim
shortfall  = required − reserved                    # what Stage 5 must buy  (≥ 0)

status = SUFFICIENT     if available ≥ required           (shortfall 0)
       | PARTIAL        if 0 < available < required        (shortfall > 0)
       | OUT_OF_STOCK   if available ≤ 0 OR no match        (shortfall = required)
```

`required_qty`, `on_hand_qty`, `shortfall_qty`, `status`, and `inventory_item_id`
(the matched article, null when unmatched) are written to `inventory_check_line`;
`reserved` is captured on the `inventory_reservation` row. `on_hand_qty` is recorded as
the **physical** on-hand (pre-reservation) for transparency; `status`/`shortfall` use
`available`. All quantities are server-computed at write time (house rule — never
recomputed on read), with `Decimal` quantization matching `costing.py` (`0.001` for qty).

---

## 5. Matching `bom_item` → `inventory_item` (deterministic-first)

The whole stage hinges on resolving `SHEEP GLASS / BLACK` (BOM) to
`SHEEP NAPPA BLACK & PACKING CHARGES` (sheet). Mirrors the stage-0 §4 / Stage-1 policy:
**no LLM unless genuinely required**, and **never a silent guess**.

```
 bom_item ─► (1) normalized_key exact match (+ colour) ── hit ─► matched set (lots aggregated)
                │ miss
                ▼
            (2) deterministic alias / synonym table  ─────────── hit ─► matched (method=alias)
                │ miss
                ▼
            (3) OPTIONAL flagged fuzzy/embedding candidate ────► NOT auto-applied:
                │                                                surfaced as a suggestion
                ▼
            (4) no confident match ─────────────────────────────► UNMATCHED:
                                                                   status=OUT_OF_STOCK, flag=unmatched
```

- **(1) Exact normalized key.** Both sides are normalized identically (§2b): the
  `bom_item.name` (+ `material_color`) is run through the same normalizer at check time
  and joined to `inventory_item.normalized_key`. This is an indexed equality join (§7).
- **(2) Alias table** (`material_alias`, config-seeded YAML like the Stage-1/2
  registries): explicit `bom_term → inventory_key` rows the factory curates
  (`SHEEP GLASS → SHEEP NAPPA`). Onboarding a synonym is a row, not a code change.
- **(3) Fuzzy/embedding fallback is *advisory only*.** A token-similarity or the local
  HF embedding (`intelligence/models_catalog.py`; no chat-LLM cost) may propose a
  candidate, but it is **never auto-bound** — it is returned in the report as a
  `suggestion` the procurement user confirms (confirming writes an alias row, which
  *promotes* the pair to the free path (1) next time — the same "promote-by-config"
  loop Stage 1/2 use).
- **(4) Genuinely unmatched** → `OUT_OF_STOCK`, `inventory_item_id = null`, the full
  `required` becomes `shortfall`, and a `unmatched` flag distinguishes "new article we've
  never stocked" from "known article at zero" (both go to Stage 5, but the human reads
  them differently).

A matched **set** (multiple lots, §6 edge 3) is collapsed at ingest (§2b) to one row, so
the live join is one-to-one; the rare runtime duplicate is still summed defensively.

---

## 6. Edge cases

1. **Item in the BOM but not in `inventory_item`.** Handled by §5 path (4): no match →
   `inventory_item_id = null`, `on_hand = 0`, `status = OUT_OF_STOCK`,
   `shortfall = required`, `flag = unmatched`. It is forwarded to Stage 5 as a full buy.
   It is **not** an error and never blocks the check — a brand-new material is expected.
2. **Unit mismatch.** The matched stock UOM ≠ the BOM line UOM. Resolution order:
   (a) **identity** — `dm²` ≡ `DCM` (the sheet's decimetre unit), `pc`/`unit` ≡ `NOS`/`PCS`;
   (b) a **`uom_conversion`** reference row (config-seeded: factor + from/to, e.g.
   `ROLL → MTRS`) converts on-hand into the BOM unit; (c) **no known conversion** →
   the line is **not** silently compared. It is written with `status = OUT_OF_STOCK`
   (conservative — never a false "sufficient") plus a `uom_mismatch` flag and both UOMs in
   the report, so a human reconciles rather than the system inventing a number. "No LLM,
   no silent guess" applies to units as much as to names.
3. **Partial stock across multiple lots.** The sheet's duplicate
   `SHEEP NAPPA BLACK …` rows (410 / 446 / 673) are the same article in different lots.
   **Resolved at ingest (§2b):** dedup aggregates them into one `inventory_item` with
   `qty_on_hand = Σ` and a qty-weighted `rate`, so `on_hand` is already the consolidated
   total at check time and the single `inventory_check_line.inventory_item_id` FK is
   well-defined. *Trade-off (documented):* consolidation loses per-lot rate granularity;
   if lot-level costing is later required, keep lots as separate rows and have the check
   `GROUP BY normalized_key` summing `qty_on_hand` (the SQL in §7 already groups, so the
   switch is a config flag, not a redesign) and point the line FK at the
   primary lot (largest qty / lowest rate) while recording the lot breakdown in the
   report's `lots[]`.
4. **(also covered)** Blank `qty_on_hand` (the many empty `PCS` cells) ⇒ coerced to 0 at
   ingest ⇒ behaves as `OUT_OF_STOCK` for that article unless another lot carries qty.
5. **(also covered)** Excluded categories (`manufacturing`, `fob_charge`) produce **no**
   check line — they appear in the report only as an informational, non-sourced section.

---

## 7. Performance — SQL set-based, not in-memory

**This runs on every approval.** A single BOM is ~10–30 stockable lines; the inventory
master is ~1,525 rows today and **grows**. Two implementations were considered.

| Dimension | In-memory (load inventory → match in Python) | **SQL set-based** ✅ |
|---|---|---|
| Data pulled per approval | the **whole** `inventory_item` table (1,525+ rows) into the app, every time | only the matched rows + the result set |
| Matching | Python dict on `normalized_key` | indexed equality join on `normalized_key` |
| Lot aggregation | manual grouping in Python | `GROUP BY normalized_key, SUM(qty_on_hand)` |
| Reservation consistency | a read-then-write race: two approvals both read full stock before either writes | `SELECT … FOR UPDATE` on the matched rows **inside the approval txn** serializes competing claims |
| Round trips | N (or one big fetch + per-line work) | one statement set, one transaction |
| Determinism / testability | fine | fine; pure SQL, runs on SQLite tests too (no window funcs needed) |

**Recommendation: SQL.** The work is naturally relational — join `bom_item` to a
grouped `inventory_item` aggregate, left-join active reservations, compute
`required/on_hand/available/shortfall/status` with `CASE`, and `INSERT … SELECT` the
`inventory_check_line` rows. Crucially, the reservation correctness in §3 **requires** the
matched stock rows to be locked (`FOR UPDATE`) for the duration of the claim — that is a
DB-level guarantee in-memory cannot give without an external lock. **Window functions are
not needed** (a `GROUP BY` aggregate + `CASE` suffices); keeping it to portable SQL
preserves the SQLite test path. The set is small *per BOM*, but pushing the
match+aggregate+lock to Postgres (where `normalized_key` is indexed) is O(lines) index
probes rather than hauling the growing master into Python on every approval — and it is
the only race-safe option.

- **Trigger & transaction.** The check is fired by the Stage-3 approve edge (or a manual
  `POST /boms/{id}/inventory-check` re-run). It runs in **one transaction**: release the
  BOM's prior reservations (§3c) → lock matched stock `FOR UPDATE` → write
  `inventory_check` (`running`) + lines + `active` reservations → mark `complete` → write
  an `INVENTORY_CHECK_RUN` `audit_log` row. The advisory fuzzy step (§5.3) and unit
  conversion (§6.2) are the only parts done in Python, on the small unmatched/mismatch
  residue, not on the bulk.

---

## 8. Output for the UI — the response shape

Pure-serialization presenter (a `inventory_presenters.py`, mirroring `presenters.py`):
ORM rows → the exact JSON. Two shapes — the **per-check** result (returned by the run +
`GET /inventory-checks/{id}`) and the **grouped dashboard** (the procurement overview,
grouped by client / order / style with status badges, per the prompt).

### 8a. Per-check result

```jsonc
{
  "inventory_check_id": "…",
  "bom_id": "…",
  "status": "complete",
  "run_at": "2026-06-12T09:30:00Z",
  "summary": {                          // the BOM-level badge = worst line status
    "badge": "partial",                 // sufficient | partial | out_of_stock
    "lines_total": 9, "sufficient": 5, "partial": 2, "out_of_stock": 2,
    "flags": { "unmatched": 1, "uom_mismatch": 1 },
    "shortfall_value": 18420.00,        // Σ shortfall_qty × inventory rate (INR), informational
    "currency": "INR"
  },
  "lines": [
    {
      "bom_item_id": "…", "category": "main_material",
      "name": "SHEEP GLASS", "material_color": "BLACK",
      "required_qty": 2070.0, "uom": "dm²",
      "matched": { "inventory_item_id": "…", "description": "SHEEP NAPPA BLACK & PACKING CHARGES",
                   "method": "alias", "uom": "DCM" },
      "on_hand_qty": 1529.0, "reserved_other": 0.0, "available_qty": 1529.0,
      "reserved_for_this_bom": 1529.0,
      "shortfall_qty": 541.0,
      "status": "partial",
      "flags": [],
      "lots": [ {"inventory_item_id":"…","qty_on_hand":1529.0,"rate":6.15} ]   // §6.3 when lot-level
    },
    {
      "bom_item_id": "…", "category": "accessory", "name": "YKK N°5 ZIPPER", "material_color": null,
      "required_qty": 120.0, "uom": "pc",
      "matched": null, "on_hand_qty": 0.0, "available_qty": 0.0,
      "reserved_for_this_bom": 0.0, "shortfall_qty": 120.0,
      "status": "out_of_stock", "flags": ["unmatched"]
    }
  ],
  "excluded": [ {"bom_item_id":"…","name":"CUTTING & STITCHING","category":"manufacturing"} ]
}
```

### 8b. Grouped dashboard (`GET /inventory-checks?…`)

```jsonc
{
  "clients": [
    { "client_id":"…", "client_name":"Beau Geste / CRIMIE",
      "orders": [
        { "client_order_id":"…", "order_number":"1579",
          "styles": [
            { "style_id":"…", "style_name":"CRI 02F5 PL02",
              "bom_id":"…", "inventory_check_id":"…",
              "badge":"partial",                       // status badge the FE renders
              "shortfall_lines": 4, "checked_at":"2026-06-12T09:30:00Z" }
          ] }
      ] }
  ],
  "totals": { "boms_checked": 12, "fully_sufficient": 5, "with_shortfall": 7 }
}
```

`badge` per BOM = the worst line status (`out_of_stock` > `partial` > `sufficient`); the
FE colours it (green / amber / red) and the `flags` drive a small warning glyph for
`unmatched` / `uom_mismatch` lines. The grouping reuses identity from `clients.service`
(a permitted service→service call; procurement never touches the clients repository,
CLAUDE.md §3.2), keyed off `bom.client_order_id` / `bom.style_id`.

### 8c. Endpoints (new, under `/api/v1/procurement`)

| Method & path | Purpose | Auth |
|---|---|---|
| `POST /boms/{bom_id}/inventory-check` | Run (or re-run) the check for an approved BOM. | MD / DM (system on approve) |
| `GET /inventory-checks/{id}` | The per-check result (§8a). | DM / MD / VIEWER |
| `GET /inventory-checks?client_id=&order_id=` | The grouped dashboard (§8b). | DM / MD / VIEWER |
| `GET /boms/{bom_id}/inventory-check` | Latest check for a BOM. | DM / MD / VIEWER |

The check is also **auto-fired** by the Stage-3 approve transition (a service→service
call from `bom_service.approve_bom`), so a freshly approved BOM lands on the dashboard
with its badge without a manual step; the explicit endpoint covers re-runs after a
re-sync.

---

## 9. Acceptance criteria

A reviewer must be able to confirm each, using the real `data/INVENTORY (1).xlsx` + a
generated BOM:

1. **Importer normalizes + dedups.** `POST /inventory/preview` on the real sheet drops
   the banner/ledger rows (`MARCH MONTH-2025 USAGE`, `MEMBERSHIP FEE`, …), coerces blank
   `PCS` to 0, and **merges** the duplicate `SHEEP NAPPA BLACK & PACKING CHARGES` lots
   into one row with summed `qty_on_hand`; `commit` writes it; a second `commit` of the
   same file yields identical rows (idempotent).
2. **Re-sync conflict rule.** After a check has reserved stock, re-committing the sheet
   overwrites `qty_on_hand` but `available` still reflects the active reservation (the
   reservation is a separate ledger, not in `qty_on_hand`); a row absent from the new
   sheet is `is_active=False`, not deleted.
3. **Per-line math reproduces the spec.** For a line with `required` (=`bulk_qty`) 2070
   and `available` 1529 → `status=partial`, `shortfall=541`; `available ≥ required` →
   `sufficient`, `shortfall 0`; `available ≤ 0` or no match → `out_of_stock`,
   `shortfall=required`.
4. **Reservation prevents double-spend.** Two approved BOMs each needing 1000 dm² of an
   article with 1500 on hand: the first reserves 1000 (sufficient), the second sees
   `available=500` → `partial`, `shortfall=500`. Cancelling/ reopening the first releases
   its reservation and the second's available returns to 1500 on re-run.
5. **Matching is deterministic-first, never silent.** `SHEEP GLASS` matches
   `SHEEP NAPPA…` via the alias row (`method: alias`); an article with no key/alias hit is
   `out_of_stock` + `unmatched` (not a fabricated match); a fuzzy candidate appears only
   as a non-applied `suggestion`.
6. **Edge cases.** Unmatched BOM item → full shortfall, `unmatched` flag; a UOM that
   can't be converted → `out_of_stock` + `uom_mismatch` (never a false `sufficient`);
   multi-lot stock is summed into one `on_hand`.
7. **Runs in SQL, race-safe.** The check executes as a set-based statement inside the
   approval transaction with `FOR UPDATE` on matched stock; no full-table load into Python;
   it runs on the SQLite test DB (no window functions).
8. **Report shape.** `GET /inventory-checks/{id}` returns the §8a per-line shape with
   `required/on_hand/available/shortfall/status/flags`; the dashboard groups by
   client→order→style with a per-BOM `badge`; excluded `manufacturing`/`fob_charge` lines
   produce no check line.
9. **Hands off to Stage 5.** Each `partial`/`out_of_stock` line is retrievable as a
   shortfall with its `bom_item_id` (+ `inventory_item_id` when matched) — the exact input
   `po_item` links to. Stage 4 itself creates **no** `purchase_order`.

---

## 10. Schema deltas for Stage 4 (patch to stage-0 §1d)

Stage-0 already defined `inventory_item`, `inventory_check`, `inventory_check_line`.
Stage 4 adds (additive, reversible migrations, registered in `alembic/env.py`):

- **`inventory_reservation`** table (§3b) + **`ReservationStatus`** enum
  (`active`/`released`/`consumed`).
- **`material_alias`** table (§5.2): `bom_term` (normalized), `inventory_key`,
  `is_active`; unique `(bom_term)`; seeded idempotently from
  `config/material_aliases.yaml`.
- **`uom_conversion`** reference (§6.2): `from_uom`, `to_uom`, `factor` Numeric;
  seeded from `config/uom_conversions.yaml` (identity rows `dm²↔DCM` etc. included).
- **`inventory_check_line`** gains a `flags` JSONB (`unmatched` / `uom_mismatch` /
  `suggestion`) and a `matched_method` String(20) (`key`/`alias`/`manual`) — both
  nullable; everything else the line needs already exists.
- **No native-enum `ALTER TYPE`** — all of these are VARCHAR str-enums (module
  convention, stage-0 §3). New audit action: `INVENTORY_CHECK_RUN` (string, no schema
  change to `audit_log`).

---

## 11. Review checklist (definition of done — spec only, nothing to run)

- [ ] Ingestion picks **recurring idempotent sync** (preview+commit, mirroring
      `imports`), with normalization (drop noise, coerce, `normalized_key`, dedup lots)
      and an explicit re-sync **conflict-resolution** table (sheet wins on `qty_on_hand`;
      soft-deactivate absent rows) justified by the separate-ledger reservation choice.
- [ ] Reservation semantics are decided (**reserve via a separate ledger, not by
      mutating `qty_on_hand`**), with the `inventory_reservation` table, the
      `available = on_hand − Σ active` rule, and the release/consume lifecycle
      (cancellation, reopen, re-run) spelled out — and the "compute-only" and
      "decrement stock" alternatives rejected with reasons.
- [ ] The per-line math (`required = bulk_qty`, `on_hand`, `available`, `reserved`,
      `shortfall`, `status` SUFFICIENT/PARTIAL/OUT_OF_STOCK) is exact and maps onto the
      existing `inventory_check_line` columns; excluded categories named.
- [ ] Matching is **deterministic `normalized_key` → alias → flagged-fuzzy → unmatched**,
      never a silent guess; the promote-by-config loop is consistent with Stage 1/2.
- [ ] Edge cases (BOM item absent from inventory; unit mismatch; multi-lot partial; blank
      qty) each have a defined, conservative outcome.
- [ ] Performance picks **SQL set-based + `FOR UPDATE` inside the approval transaction**
      over in-memory, with reasoning (race-safety, growing master, no window funcs,
      SQLite-portable).
- [ ] The UI output shape is specified for both the per-check result and the grouped
      (client→order→style, status badge + flags) dashboard, plus the endpoints; identity
      grouping respects the cross-module service rule.
- [ ] The Stage-5 handoff (shortfall lines keyed by `bom_item_id`/`inventory_item_id`) is
      explicit, and Stage 4 creates no PO.
- [ ] Schema deltas listed as additive, reversible, VARCHAR-enum migrations registered in
      `alembic/env.py`; no native `ALTER TYPE`.
- [ ] No code, migrations, or model edits were produced — spec only.
- [ ] Reviewed / signed off by MD / system stakeholder.
```
