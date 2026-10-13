# 1. What this service is

**Phase 2, Stage 4 — the reality check.**

Stage 2 produced a bill of materials: what this garment needs. Stage 4 answers the only question that stands between it and a purchase order:

> *"How much of that do we already have — and what, exactly, is missing?"*

The answer is a **stored document**, not a calculation. It names every line, what it matched to, how it matched, what is on the shelf, what is already promised to someone else, and what must be bought. Stage 5 buys precisely that shortfall.

```
 STAGE 1        STAGE 2 + 3     STAGE 4          STAGE 5
 INTAKE   ──▶   BOM        ──▶  THIS SERVICE ──▶ SUPPLIER PO
                                 match, reserve,
                                 shortfall
```

| Document | Covers |
|---|---|
| `PROCUREMENT_SYSTEM_GUIDE.docx` | Stage 1 (intake) and Stage 5 (purchase orders) |
| `BOM_SYSTEM_GUIDE.docx` | Stages 2 and 3 (generation and approval) |
| **this document** | **Stage 4 — `app/modules/inventory/`** |

---

# 2. ⚠ This is NOT the Phase-1 `material` module

There are two stock systems in KairoX and **they are deliberately not the same one**. Merging them is the single most tempting wrong move in this codebase, so it is the first thing in this guide.

| | `material` (Phase 1) | **`inventory` (this service, Phase 2)** |
|---|---|---|
| Driven by | a human typing a lot in | **a BOM** plus a spreadsheet upload |
| Keyed to | category / subtype / article | **a `bom_id`** |
| Has | lot barcodes, three floor categories, per-type required fields, on-hand / reserved / available | **`MaterialAlias`, UOM conversions, BOM-keyed checks and reservations** |
| Core operations | `create_lot`, `stock`, `receive`, supplier orders | **`run_check(bom_id)`, reservations, alias matching** |
| Documented in | `MATERIAL_SYSTEM_GUIDE.docx` | this document |

**Why not reuse `inventory` for the factory floor?** Every one of its operations is keyed to a `bom_id`, it ingests spreadsheets, and it carries alias and UOM machinery for matching messy BOM text. In Phase 1 **there is no BOM** — humans do costing by hand — so there is nothing to key it to.

**Why not overwrite `inventory` with `material`?** The BOM module depends on this service: `run_check`, the reservation ledger, the alias matcher. Replacing it breaks the pipeline this document describes.

**The future bridge is a connection, not a rewrite.** When Phase 2 is proven, the BOM's inventory check will read stock, and the physical stock it reads against **is** the Phase-1 material lots. Keeping the two separate now is exactly what makes that a bridge instead of a rewrite. (`CLAUDE.md` §12.)

---

# 3. First, load the warehouse

The check runs against a **stock master** — one row per article, built from the warehouse spreadsheet.

```
POST /procurement/inventory/preview   parse and normalise, WRITE NOTHING
POST /procurement/inventory/commit    the same parse, written
GET  /procurement/inventory/items     read the master
```

**Always preview first.** The commit overwrites the master every future check is measured against.

## What the parser actually does to the sheet

The raw file is four columns — `DESCRIPTION | NOM | PCS | RATE` — and roughly 1,500 rows of real warehouse data, which means duplicate lots of the same article, blank quantities, and section banners and ledger markers mixed in with stock (`FACTORY NETWORK…`, `MARCH MONTH-2025 USAGE`, `I-CATEGORY MEMBERSHIP FEE`). The importer:

1. **drops the noise rows** — recorded in `dropped`, never silently swallowed
2. coerces `PCS` → `qty_on_hand`, `RATE` → `rate`, and `NOM` → a **canonical UOM** (`DM²`, `SQDM` and `DM2` all become `DCM`; `KG` becomes `KGS`)
3. computes a **normalized key** and extracts the colour
4. **deduplicates by that key** — the 410 / 446 / 673 lots of one article become one row, with the quantity **summed**, the rate **quantity-weighted**, and the **modal** UOM taken. A per-key UOM clash becomes a warning.

Columns are located by header name, falling back to positions 0–3.

## Reading the preview

| Field | Meaning |
|---|---|
| `raw_count` | rows in the sheet |
| `kept` | rows that became stock |
| `dropped` | rows rejected as noise |
| `warnings[]` | **the rows it could not use cleanly — read these** |
| `rows[]` | the deduped result: `normalized_key`, `description`, `uom`, `qty_on_hand`, `rate`, `color`, `lots` |

`commit` returns that same block plus two more:

| Field | Meaning |
|---|---|
| `committed` | rows upserted |
| `deactivated` | **rows in the master that this sheet did not mention** |

That last one is the important one. The commit is an **idempotent upsert keyed on `normalized_key`** — the sheet wins on quantity — and any key **absent** from the sheet is **soft-deactivated**, not deleted. A partial sheet therefore deactivates everything it left out. Check `deactivated` before you accept a commit.

## The key is computed identically at both ends

`normalize_key()` is run on the inventory description at import **and** on the BOM line at check time. If the two were normalised differently, nothing would ever match. That is why the normalisation lives in one pure module shared by the importer and the matcher, and why nothing else should reimplement it.

---

# 4. Running the check

```
POST /procurement/boms/{bom_id}/inventory-check    run (or re-run) it
GET  /procurement/boms/{bom_id}/inventory-check    the latest one for this BOM
GET  /procurement/inventory-checks/{check_id}      one stored check
GET  /procurement/inventory-checks                 the dashboard
```

**The check also runs by itself.** Approving a BOM (`POST /boms/{id}/approve`) runs it as a side-effect and returns the `inventory_check_id`. That side-effect is best-effort — if it fails the BOM is still approved and the id comes back `null`, and you run the POST yourself.

## The one precondition

**The BOM must be approved.** A BOM that is not `approved`, `locked` or `exported` is a **409 `bom_not_approved`**. A missing BOM is a **404**.

## What a re-run does

A re-run **releases this BOM's prior reservations first**, then re-claims from scratch. It does not stack claims on top of old ones. Each run writes a **new stored check**; the old one stays readable.

## Which lines are considered

Seven of the nine BOM categories are stockable: `main_material`, `sub_material`, `lining`, `interlining`, `thread`, `accessory`, `packaging`.

**`manufacturing` and `fob_charge` are excluded** — they are services and charges, and there is no stock of them. They are not reported as out of stock; they come back in a separate **`excluded[]`** block. Render that block; a line that vanished silently reads as a line that was forgotten.

---

# 5. How a BOM line is matched to stock — deterministic first, and never a silent guess

This is the heart of the service. For one line, in order:

| # | Step | `matched.method` | Applied? |
|---|---|---|---|
| 1 | the **normalized key** matches exactly | `key` | yes |
| 2 | a **curated alias** maps the BOM term to a stock key (`SHEEP GLASS` → `SHEEP NAPPA`) | `alias` | yes |
| 3 | **fuzzy** token overlap above the floor | — | **NO — advisory only** |
| 4 | nothing | `null` | line is `unmatched` |

**Step 3 is never auto-applied.** A fuzzy candidate is surfaced as a `suggestion` object on the line and flagged, and the line is otherwise treated as unmatched. There is **no LLM and no silent guess** here: an unmatched line is reported as out of stock *with diagnostics*, rather than force-bound to a wrong article and quietly costed as covered.

A colour gate runs over the match, so `BLACK` never matches `BROWN`.

**Lots sharing a key are returned together** and their on-hand is **summed** — that is what the importer's dedup is for.

## Units are reconciled, and an unconvertible clash is flagged

Stock is held in the warehouse's unit; a BOM line is priced in its own. Each matched lot's quantity is converted into the BOM line's UOM:

- identical units pass through (`dm²` ≡ `DCM`)
- a seeded conversion factor is applied where one exists
- **no conversion available → the quantity contributes 0 and the line is flagged `uom_mismatch`**

That last rule is deliberately pessimistic. A unit clash that silently read as "sufficient" is a garment that never gets its material.

---

# 6. Reading a check line

| Field | Meaning |
|---|---|
| `bom_item_id`, `category`, `name`, `material_color`, `uom` | which BOM line this is |
| `required_qty` | **what the BOM needs in bulk** — `order_qty × qty_per_garment`, straight from Stage-2 costing |
| `matched` | the stock row it matched, and **how** (`method`). **`null` = unmatched** |
| `on_hand_qty` | on the shelf, summed across lots, converted into this line's UOM |
| `available_qty` | `on_hand` **minus what other BOMs have reserved** (floored at 0) |
| `reserved_for_this_bom` | `min(required, available)` — what this check just claimed |
| `shortfall_qty` | **`required − reserved`. This is what must be bought.** |
| `status` | `sufficient` / `partial` / `out_of_stock` |
| `flags[]` | `unmatched`, `uom_mismatch`, `suggestion` |
| `suggestion` | the advisory fuzzy candidate, when there is one |

## Two flags deserve their own treatment on the screen

- **`unmatched`** — the material could not be found in the master **at all**. It is not "out of stock"; **nobody knows**. Those two states look identical in the `status` column and are completely different problems: one is a purchase, the other is a missing master row or a missing alias.
- **`uom_mismatch`** — it matched, but the units disagree and no conversion exists. The number may be meaningless until somebody looks.

## How `status` is decided

| `status` | Condition |
|---|---|
| `sufficient` | `available ≥ required`, `required > 0`, and not an all-zero UOM mismatch |
| `partial` | some availability, but less than required |
| `out_of_stock` | nothing available — **including every unmatched line** |

---

# 7. The summary and the badge

Every check carries a `summary`:

| Field | Meaning |
|---|---|
| `badge` | **the WORST line in the BOM** — `out_of_stock` beats `partial` beats `sufficient` |
| `lines_total`, `sufficient`, `partial`, `out_of_stock` | the counts |
| `flags` | how many lines carry `unmatched` and `uom_mismatch` |
| `shortfall_value` | `Σ (shortfall × rate)` — **the money the shortfall represents**, rounded to 2dp |
| `currency` | `INR` |

Use the **badge** for the row on a dashboard and the **counts** for the detail. A BOM whose badge is `sufficient` needs no purchase order at all.

## The dashboard

`GET /procurement/inventory-checks` returns the **latest** check per BOM, grouped **client → order → style**, each style carrying its `badge`, `shortfall_lines` and `checked_at`. It takes optional `client_id` and `order_id` filters, and a `totals` block: `boms_checked`, `fully_sufficient`, `with_shortfall`.

---

# 8. The GET does not recompute — and that is the point

`GET /inventory-checks/{id}` and `GET /boms/{id}/inventory-check` **re-render the stored rows**. They do not re-run the match and they do not re-read stock.

> **A purchase decision must be traceable to the numbers it was made on.** If the read recomputed, then opening last week's check today would show today's stock, and the purchase order raised against it would look wrong — or worse, look right for the wrong reason.

One consequence to know: `available_qty` is **not persisted**, because it is a live figure. On a stored read it is rendered as `on_hand_qty`, and `reserved_for_this_bom` is recovered as `required − shortfall`. The shortfall, the status, the match and the flags are all real stored values.

---

# 9. Reservations — why two BOMs cannot be promised the same stock

A check **reserves** what it can: `min(required, available)`, spread across the matched lots until the claim is filled, skipping any lot with no free capacity.

| `status` | Means |
|---|---|
| `active` | **counts against `available`** for every other BOM |
| `released` | freed — a re-run releases this BOM's own prior claims |
| `consumed` | physically issued (terminal) |

Two safeguards make this real rather than decorative:

- **the whole check is one transaction** — the lines, the reservations and the audit row commit together, or none of them do;
- **the candidate stock rows are fetched `FOR UPDATE`**, so two approvals landing at the same instant cannot both see the same free quantity and both claim it.

The candidate pool is fetched **set-based** — the exact keys, plus alias targets and a longest-token probe — and never the whole master. That is what keeps the check fast on a 1,500-row warehouse without loading it into memory.

---

# 10. What the check triggers

On completion it writes an `INVENTORY_CHECK_RUN` audit row and **advances the production board** to `inventory_checked`. That advance is best-effort and never blocks the check.

Stage 5 then reads the **latest check's shortfall lines** — through this service's DTO, never its tables — and turns them into purchase orders.

---

# 11. Who can do what

MD and DM are superusers and pass every gate here.

| Action | Roles |
|---|---|
| `POST /inventory/preview`, `POST /inventory/commit` | **DM, MD** |
| `POST /boms/{id}/inventory-check` (run it) | **DM, MD** |
| `GET /inventory/items` | DM, MD, **Viewer** |
| `GET /inventory-checks`, `GET /inventory-checks/{id}`, `GET /boms/{id}/inventory-check` | DM, MD, **Viewer** |

> **`Viewer` is a real read role in Phase 2**, and almost nowhere else in the app. It exists so an accountant can see committed spend and the stock position without being able to commit any. Nothing in this service admits it to a write.

---

# 12. Errors you will actually see

| Status | When |
|---|---|
| **404** | the BOM does not exist; the check id does not exist; **no check has ever run for this BOM** |
| **409 `bom_not_approved`** | the BOM is still draft, in review, or rejected |
| **413** | the uploaded spreadsheet exceeds `MAX_UPLOAD_MB` (default 25 MB) |
| **403** | a role gate — including a Viewer attempting `commit` or a check run |

A 404 on `GET /boms/{id}/inventory-check` means *"no check yet"*, not *"no such BOM"*. Render it as an empty state with a **Run check** button, not as an error.

---

# 13. Notes for backend developers

- **`presenters.py` is pure serialisation** — rows in, response dict out. No session, no rules, no I/O. The service stays orchestration.
- **`inventory_normalize.py` is shared by the importer and the matcher on purpose.** A key computed one way at ingest must compare equal to a key computed the same way at check time. Do not fork it.
- **`inventory_match.py` is pure and takes a candidate pool**, not a session. The set-based fetch and the `FOR UPDATE` lock live in the repository; the matching decision is testable with no database.
- **Cross-module reads go through the owning service's DTO.** This service reads a BOM through `BomService.get_bom_dto` and client identity through `ClientService` — never the other module's repository or models. Supplier-PO reads this service's shortfall the same way.
- **openpyxl is blocking**, so both `preview` and `commit` run the parse in a threadpool. Keep it there.
