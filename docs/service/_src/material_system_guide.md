# 1. What this service is

Everything about **physical material**: what arrived, what is on the shelf, what one garment takes, what was spent on it, and who to buy more from.

It has four parts, and they are easier to learn one at a time:

| Part | Answers | Main endpoints |
|---|---|---|
| **A · Lots & stock** | what material exists and how much is left | `/materials/lots`, `/materials/stock` |
| **B · Arrivals & receiving** | a delivery entered in two sittings | `/materials/arrivals*`, `/materials/receive` |
| **C · Suppliers** | who to buy from, and ordering | `/suppliers/orders` |
| **D · The style recipe** | how much ONE garment takes | `/styles/{id}/material-spec*` |

| Code | File |
|---|---|
| Routes (all four parts) | `app/modules/materials/router.py` |
| Lots, stock, arrivals, suppliers | `app/modules/materials/service.py` |
| The style recipe | `app/modules/materials/style_spec_service.py` |
| Tables | `app/modules/barcode/models.py` |
| The per-category field rules | `app/core/enums_barcode.py` (`MATERIAL_SPEC`) |

> **This is NOT the Phase-2 `inventory` module.** That one is driven by a BOM and keyed to a `bom_id`. This one is driven by a human typing in a delivery. They are deliberately separate and must not be merged — see `CLAUDE.md` §12.

---

# 2. Part A — Lots and the three stock numbers

A **lot** is one material specification on the shelf: *GOAT SUEDE · DARK BROWN · 0.8-1.0 mm*. Creating one mints a **child barcode** — that is how material formally enters inventory.

## The rule: one lot per specification

If a new lot would collide with an existing one on the same spec, you get **409**. Two rows a cutter cannot tell apart is worse than no row at all.

## Read these six numbers, and read them exactly

Every material screen returns the same block:

| Field | Meaning |
|---|---|
| `arrived` | everything that ever came in (`balance + used`) |
| `used` | cut or issued out of it |
| `balance` | **what is on the shelf right now** |
| `reserved` | committed to a requirement, not yet spent |
| `available` | `balance − reserved` — what you may promise to something new |
| `on_hand` | the legacy key. It equals `balance`. |

> `on_hand` once meant `arrived` on one endpoint and `balance` on another, so a manager comparing two screens saw numbers that could not both be right. **Read `arrived`, `used` and `balance`** — they say what they are.

`reserved` is **shown, not silently subtracted** from the shelf figure. A reservation stuck open would otherwise look like a phantom shortage with no visible cause.

## Each category needs exactly its own fields

`GET /materials/spec?category=&subtype=` returns, at runtime, which boxes to render and which are required. **Build the Add-Material form from this call** — do not hard-code it.

| Category / subtype | Required to add | Quantity field (unit) | Filter boxes |
|---|---|---|---|
| Leather | thickness, dcm | `dcm` (dcm) | article, colour, thickness |
| Lining / plain | thickness, mtrs | `mtrs` (mtrs) | article, colour, thickness |
| Lining / ribs | kg | `kg` (kg) | article, colour |
| Lining / knit | pcs | `pcs` (pcs) | article, colour |
| Accessory / button | size, count | `count` (pcs) | article, colour, size |
| Accessory / zip | size, count | `count` (pcs) | article, colour, size |
| Accessory / thread | thickness, mtrs | `mtrs` (mtrs) | article, colour, thickness |
| Accessory / other | description, count | `count` (pcs) | article, colour |

`article` and `colour` are required for **every** material. A missing field is a **422**.

Defaults: LEATHER ignores `subtype`. LINING with no subtype means **plain lining**. ACCESSORY **must** have a subtype — there is no generic accessory quantity.

## Hides — why leather is different

A leather lot can be created with `sheets`: one row per **hide**, each with its own `dcm` and its own barcode.

A lot code names a *spec*, and what is behind it is interchangeable — one metre of lining is any other metre. **A hide is not interchangeable**: it is individually measured, individually expensive, and issued to one named cutter for one garment. "How much leather did this jacket take?" is the sum of the specific hides that went into it.

Hides have their own life:

```
IN_STOCK  ->  ALLOCATED  ->  ISSUED  ->  CONSUMED
(on the      (a draft       (in the     (the cut
 shelf)       cutting row    cutter's    was logged)
              claims it)     hands)
```

**The state is the permission.** A hide still `IN_STOCK` on no cutting row has not been acted on, so editing it is data entry. From `ALLOCATED` onwards every write answers **409** — its dcm is part of what a garment was cut from.

| Call | Allowed while |
|---|---|
| `PATCH /materials/sheets/{id}` (fix a measurement) | `IN_STOCK`, not on a cutting row |
| `DELETE /materials/sheets/{id}` (typed by mistake) | same |
| `POST /materials/lots/{id}/sheets` (the one that got missed) | any time — **stock is not touched** |

`GET /materials/sheets/{id}` returns `editable`, computed from the same rule the writes enforce, so an Edit button and the endpoint behind it cannot disagree.

Hides are listed **smallest first**. That is not cosmetic: the allocator spends offcuts before it breaks into a big skin, so that is the order a cutter is offered them in.

## Correcting a lot

| You want to… | Use | Why |
|---|---|---|
| fix article / colour / thickness / size / supplier | `PATCH /materials/lots/{id}` | identity only |
| change the quantity | `PATCH /materials/lots/{id}/adjust` | a **delta with a reason**, audited |
| stop using a lot | `DELETE /materials/lots/{id}` | **retires** it — never a hard delete |

**`category`, `subtype`, `uom` and `on_hand` are not patchable, on purpose.**

- Moving LEATHER → LINING would leave a lot measured in dcm claiming to be metres, and every cut event pointing at it would silently change unit.
- `on_hand` is a **ledger**. It moves by receiving and by cutting. Setting it directly makes the stock and the movement history disagree with no record of who changed it.

`adjust` refuses to take stock below what is already reserved, and refuses to go negative. `retire` refuses (**409**) while any stock is still reserved. A retired lot's barcode answers **410 Gone** — "this lot was retired" — never 404.

## The lot picker

`GET /materials/lots` is what a cut screen uses to obtain a `leather_lot_id`. The other two reads cannot give you one: `/materials/spec` returns the **form definition**, `/materials/stock` returns **totals with the lot ids summed away**.

```
GET /materials/lots?category=LEATHER&sku_id=<sku>
   -> `options` fills the article / colour / thickness dropdowns
   -> the row with last_used_for_sku: true is pre-selected
...narrow with &article=&colour=&thickness=
   -> one row; take its lot_id
POST /production/log with consumption.leather_lot_id + dcm
```

Pass `required` (dcm per piece × piece count) and every lot reports `covers_required`, so a lot that cannot cover the batch is greyed out **before** the cut instead of warning after it.

Exhausted lots are **returned, not hidden**. A manager searching for a lot they know exists must find it, with `available: 0` explaining itself.

---

# 3. Part B — A delivery, entered in two sittings

This is the flow that matches what really happens at the gate.

```
  SITTING 1 — the van is here, somebody is standing in the yard
  POST /materials/arrivals        article, colour, total — and that may be all
       -> lot minted or topped up
       -> lot barcode printed
       -> quantity IS IN STOCK, so the floor can cut from it now
       -> status PENDING, and `outstanding` names what is still owed

  SITTING 2 — later, with the paperwork and a measuring tape
  POST /materials/arrivals/{receipt_id}/complete
       -> approved / rejected split
       -> every hide's dcm
       -> stock corrected, status COMPLETED
```

## Why this shape

Signing for a van is a floor job. It must be possible for whoever is standing there when it pulls in — a gate form only a DM can fill in is a gate form nobody fills in. So intake is open to cutting managers, lining managers, HR, DM and MD.

## The stock is provisional until completion

Every read says so: `pending_arrivals` and `pending_arrival_qty` ride along on the lot page, the lot directory and the stock check.

`GET /materials/arrivals` is the **come-back-to-it queue**, `PENDING` by default and **oldest first**. That is the point of it: an unfinished arrival nobody can find is provisional stock quietly becoming permanent stock that was never checked. Newest-first would bury the three-day-old one that actually matters.

## Completion corrects by difference, never by assignment

Stock moves by **(approved − declared)**, not to `approved`. The floor may have cut some of this delivery in between, and assigning the number would silently undo that.

The **rejected** quantity is logged against the supplier's quality history. It was never in stock — it went back on the van.

`sheet_count` given at the gate is checked against the hides entered at completion. A mismatch is **reported, never enforced**: a bundle count taken at the gate is a glance.

## Correcting an arrival

| Call | Rule |
|---|---|
| `PATCH /materials/arrivals/{id}` | PENDING only. **`declared_qty` moves stock** — correcting 3400 to 340 takes 3060 back out, applied as a delta so a cut made in between is not undone. 409 once COMPLETED, and 409 if it would drive the lot negative. |
| `DELETE /materials/arrivals/{id}` | Voids a PENDING arrival (the van entered twice). The stock it put on the floor comes back out. 409 once COMPLETED, and 409 if any of it has already been cut. |

> **The lot survives a void**, even when that was its only delivery. Its barcode may be printed and a recipe may already point at it. A lot at zero is an empty shelf, which is a true statement. Retire it separately if it should never have existed.

## `POST /materials/receive` — the other receiving door

This is receiving against a **supplier order**: `approved` adds to stock, `rejected` is logged.

A **PO mismatch** — the supplier sent a different article — is a **409**, unless a **DM or MD** sends `approve_mismatch: true`, which receives it into a new **substitute lot**. That one decision stays DM/MD only even though ordinary receiving is open to HR: it is a costing decision, not a clerical one.

---

# 4. Part C — Suppliers

| Call | What it does |
|---|---|
| `POST /suppliers/orders` | raise a manual order (status `ORDERED`). It validates the requested article against the chosen or suggested supplier's catalogue. |
| `PATCH /suppliers/orders/{id}` | flip `ORDERED` → `ARRIVED`. This is what cues the receiving screen. **Idempotent.** |
| `PATCH /suppliers/orders/{id}/spec` | edit an ORDERED order's article / colour / thickness / dcm / quantity. DM or MD. |

When stock is short, `GET /materials/stock` and the recipe's requirement view both return a **suggested supplier**, derived from the article. That suggestion is what `POST /suppliers/orders` is meant to be called with.

---

# 5. Part D — The style recipe (`/styles/{id}/material-spec`)

**How much of each material does ONE garment take?** This is the per-piece recipe. It is a materials concept that happens to hang off a style, which is why it lives in this module.

## Who can do what

- **Read: the whole floor.** The cutting manager needs the dcm; the store manager needs the accessory list.
- **Write: DM and MD only.**

## The lines

A line names a material by six columns (category, subtype, article, colour, thickness, size) plus `qty_per_piece` and `uom`.

Two `size`-like fields exist and they mean different things — do not mix them up:

| Field | Meaning |
|---|---|
| `size` | **the material's** size — a 60 cm zip |
| `garment_size` | **which jackets** this line is for — the size-L line |

A line has a **scope**:

- `STYLE` — every garment of the style.
- `SKU` — one colourway only. That is how a NAVY jacket gets a navy knit and a PINE GREEN one does not.

## How a line finds real stock — `resolution`

| Value | Meaning | What the screen should do |
|---|---|---|
| `PINNED` | the line names an exact `material_lot_id` | fine |
| `MATCHED` | exactly one lot matches the six columns | fine |
| `NONE` | no lot matches | "receive this material first" |
| `AMBIGUOUS` | several lots match | show `candidate_lot_ids` and make the user pick |

## Confirming the recipe unlocks release

`POST /styles/{id}/material-spec/confirm` is the sign-off. It is **separate from the release call on purpose**: the DM can finish the recipe days earlier, and the release screen can show `release_blockers` **before** the button is pressed instead of explaining a rejection afterwards.

The three blockers:

1. the spec is not confirmed;
2. there is **no LEATHER line** — the dcm per piece is what the ledger and the costing are built on;
3. the spec names **no accessories** and nobody declared that it needs none.

That third one is a **three-state** field. An empty accessory list on its own is ambiguous: it could be a garment that genuinely takes none, or one whose buttons nobody has entered yet. So it needs an explicit `no_accessories: true`; `null` — nobody asked — does not pass.

> **A missing LINING line is a warning, not a blocker.** Lining consumption is optional on the cut path, so requiring it here would contradict the ledger rule downstream.

## The requirement view — the screen that should stop an order

`GET /styles/{id}/material-spec/requirement` multiplies ordered quantity by per-piece consumption and compares it with what is on the shelf, line by line.

**Read it before release.** After release the garments exist, and a shortfall is a stoppage instead of a purchase order. `short_by` and `suggested_supplier` feed straight into `POST /suppliers/orders`.

`pieces` is scoped per line, which is what makes the numbers right:

- a **style-wide** line is needed by every ordered garment, **minus** those SKUs that have their own override — so the same garment is never counted against two lines for the same material;
- a **sized** line reaches only garments of that size. Without this, a style with three sized zip lines ordered three zips per garment instead of one, and the requirement is what a purchase is raised from.

## The recipe freezes at release — and the escape hatch

Once a style is RELEASED, its leather and lining lines cannot be edited: its garments carry printed barcodes and the recipe is already being spent against them. Accessories stay correctable.

To record something that actually went into a released garment, use **`POST /materials/issues`**:

```json
{ "piece_barcode": "PC-23456A", "lot_barcode": "LOT-ACC-000205",
  "qty": 2, "employee_barcode": "EMP-000123", "note": "wrong button swapped" }
```

- It **spends stock** and writes a ledger row.
- It is **repeatable on purpose**: three corrections on one garment are three real events.
- It never makes the kit checklist think a spec line was satisfied.
- Send `piece_barcode` **or** `piece_id`, and `lot_barcode` **or** `material_lot_id` — one of each (**422** otherwise). The worker is optional here, unlike the production and store doors.
- Store Manager can call it, as well as DM and MD: the correction is made **at the store**, and waiting for a DM to record a swapped button is how the correction stops being made at all.

## Copying a recipe

`POST /styles/{id}/material-spec/copy-from` seeds this style's recipe from another. A leather factory repeats styles season after season.

SKU overrides copy **only where the colourways match** on (colour code, size). Two styles rarely share SKU ids, and copying an override onto the wrong colourway would silently issue the wrong colour button — so unmatched overrides are **reported** in `skipped_detail`, never guessed at.

**The copy does not confirm.** Somebody still has to look at the numbers for this style.

---

# 6. Who can call what

| Group | Roles | Can |
|---|---|---|
| Lot writers | DM, MD, Cutting Mgr, Lining Mgr, **HR** | create/edit lots, hides, arrivals |
| Stock readers | + Stitching Mgr, Store Mgr, Security | every read, including the recipe |
| Receivers | DM, MD, HR | `receive`, `adjust`, retire a lot, void an arrival |
| DM only (+MD) | | supplier orders, all recipe **writes** |
| Issuers | DM, MD, **Store Manager** | `POST /materials/issues` |

> **HR is a writer here deliberately.** HR could read stock but not correct it, so a wrong lot had to be fixed in the database by hand — the worst possible way to change a stock figure: no audit row, no reason, no chance for anyone to see it later. Giving HR the same lot rights as the floor managers replaces an untracked database edit with a tracked API call.

---

# 7. Notes for backend developers

- **Route order matters.** `GET /materials/lots/{lot_id}/history` is declared **before** `GET /materials/lots/{lot_id}`; otherwise `"history"` would be parsed as a lot id and 422.
- **`stock_numbers()` is the single shape** for the six figures. Five call sites used to assemble them by hand with three different meanings for `on_hand`.
- **`replace_spec` diffs, it does not delete-then-insert.** A line that has been issued against is referenced by `piece_material_issue` rows; recreating it would give the same recipe entry a new id and orphan every issue pointing at the old one. Matching lines are updated in place, absent ones are **deactivated**.
- **`line_not_applicable_reason` turns a silent filter into a sentence** (`other_sku`, `other_size`, `zeroed`). That is what stops the "the requirement view shows three accessories but the scan says none" confusion — both statements are true, one is style-wide and one is per garment.
- **The lot picker is three queries** regardless of how many lots match. It opens on every scan.
