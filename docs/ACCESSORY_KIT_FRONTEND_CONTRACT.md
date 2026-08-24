# KairoX ERP — Accessory Consumption & the Per-Piece Material Spec

**Generated:** 21 August 2026 · **Base URL:** `/api/v1` · **Auth:** Bearer JWT
**Extends:** *Backend Change Set & API Delta* (17 Aug 2026)
**Audience:** the two frontend developers. No backend action required from you.

---

## 0 · The problem this solves, in one paragraph

Until now the only material the factory could spend automatically was **leather**,
and even its quantity was typed by the cutting manager on every single scan.
Accessories — buttons, zips, thread, "other" — could be stocked, barcoded and
received, but **nothing ever decremented them**. Two consequences on the floor:
accessory stock was fiction (`on_hand` only ever went up), and nobody at the
drawer knew what a garment actually needed, so every kit was assembled from
memory.

The fix has three parts:

1. **A style declares its recipe before it is released** — dcm per piece, lining
   per piece, and every accessory with its article and count.
2. **The store spends that recipe** when it kits the accessories into the drawer.
3. **Every scan shows the checklist** — scanning a piece barcode or a drawer
   barcode now returns what the garment needs and what it has already been given.

---

## 1 · What you must change (the short list)

There is exactly **one required change**:

> **The release screen must confirm the material spec before it releases.**
> `POST /styles/{id}/material-spec/confirm` → then `POST /imports/breakdown/{order}/release`.
> Without it, newly uploaded styles land in the release response's existing
> `rejected[]` array with a `blockers[]` list explaining why.

Everything else is **new screens** and **new keys on responses you already read**.
Nothing you read today changed name, type or meaning. There is a system test that
machine-checks that promise (`tests/system/test_style_spec_endpoints.py`).

---

## 2 · The product flow, screen by screen

```
UPLOAD ─▶ SPEC ─▶ CHECK STOCK ─▶ CONFIRM ─▶ RELEASE ─▶ CUT ─▶ STORE ─▶ KIT ─▶ SEND ─▶ …
          │        │              │                     │              │
          │        │              │                     │              └─ spends accessory stock
          │        │              │                     └─ dcm is now PREFILLED from the recipe
          │        │              └─ unlocks release; audited
          │        └─ qty_ordered x per-piece vs stock → raise supplier orders here
          └─ NEW SCREEN: the recipe grid
```

### 2.1 · Screen — the recipe grid (NEW)

Reached from a DRAFT style on the breakdown screen. One row per material.

```
STYLE: CLERMONT                                    status: DRAFT · editable
────────────────────────────────────────────────────────────────────────────
 SCOPE   CATEGORY   SUBTYPE  ARTICLE     COLOUR  SIZE   PER PIECE   STOCK
 STYLE   LEATHER    —        SUEDE-A32   PINE    —      12.5 dcm    4 200 ✓
 STYLE   LINING     PLAIN    PL-22       BLACK   —       1.2 mtrs     380 ✓
 STYLE   ACCESSORY  BUTTON   BTN-4H      BLACK   18L        4 pcs   4 000 ✓
 STYLE   ACCESSORY  ZIP      ZIP-YKK     BLACK   60cm       1 pcs     900 ✓
 SKU     ACCESSORY  BUTTON   BTN-4H      TAN     18L        4 pcs      30 ⚠
         └─ override: CLERMONT / TAN / M
────────────────────────────────────────────────────────────────────────────
 [+ Add line]  [Copy from another style]  [Check stock]  [Confirm spec]
```

**Three rules the grid has to express.**

- **Scope.** A row with no `sku_id` is the **style-wide default**. A row with one
  is an **override for that colour+size**.
- **An override replaces only its own article.** Keyed on
  `(category, subtype, article)` — the real case is "the TAN colourway takes TAN
  buttons of the same article". Everything else in the recipe still applies.
- **`qty_per_piece: 0` on an override removes** that material for that colourway.
  It is rejected on a style-wide row (a recipe entry that consumes nothing is a
  typo with a row in it).

**Do not send `uom`.** It is derived from `(category, subtype)` server-side and a
sent value is discarded — buttons are `pcs` and thread is `mtrs` whatever the
client posts. Honouring a caller's unit would let one style measure thread in
yards and another in metres while both told the ledger "mtrs".

**Pin the lot when you can.** If the user picked a specific lot in the article
box, send its `material_lot_id`. It makes every downstream read one indexed
lookup instead of a six-column search, and it keeps resolving after a lot is
renamed underneath the recipe.

### 2.2 · Screen — requirement & shortfall (NEW)

`GET /styles/{id}/material-spec/requirement`. **This is the screen that should
stop an order**, and the moment to read it is *before* release: afterwards the
garments exist and a shortfall is a stoppage instead of a purchase order.

```
CLERMONT · 1 425 pieces ordered
──────────────────────────────────────────────────────────────────────────
 MATERIAL              NEEDED     AVAILABLE   SHORT BY   SUPPLIER
 SUEDE-A32 PINE       17 812 dcm    4 200      13 612    ACME LEATHER  [Order]
 BTN-4H BLACK 18L      5 700 pcs    4 000       1 700    ACME TRIMS    [Order]
 ZIP-YKK BLACK 60cm    1 425 pcs      900         525    ACME TRIMS    [Order]
──────────────────────────────────────────────────────────────────────────
 3 of 5 material lines are short. Raise a supplier order before releasing.
```

`[Order]` posts to the **existing** `POST /suppliers/orders` — no new endpoint.

Each line carries `reserved` **separately** from `available`. Show it. Nothing in
the system can currently release a `MaterialReservation`, so a stuck reservation
would otherwise present as a phantom shortfall with no visible cause.

### 2.3 · Screen — release (CHANGED)

Call `confirm` first, then `release`. The `confirm` response already returns
`release_blockers[]`, so the screen can render them **before** the release button
rather than explaining a rejection afterwards.

`no_accessories: true` is the DM stating *this garment takes none*. It is the
only thing that lets a style with an empty accessory list through the gate — an
empty list on its own is ambiguous (nobody entered them yet?), and releasing that
silently is how a whole order reaches the store with no kit. Sending `true` while
accessory lines exist is a **422**, not a precedence rule.

### 2.4 · Screen — cutting (CHANGED, optional)

`GET /production/piece-state` now returns `suggested_dcm_per_piece`. **Prefill
the consumption field with it.** The operator still confirms or overrides the
number that reaches the ledger — the recipe is a suggestion here, never a
substitution.

There is also an opt-in server-side fallback (`consumption.use_style_spec: true`
on `POST /production/log`), but you should not need it: prefilling is better,
because the number the operator saw is the number that gets recorded.

### 2.5 · Screen — the store (CHANGED, the main new interaction)

The scan order is unchanged: **employee → drawer → piece**. What is new is a
third part and a checklist that rides *every* scan.

```
DRAWER DRW-0042 · JP-CLERMONT-PINE-M-007
────────────────────────────────────────────────────
 ✓ LEATHER   stored
 ✓ LINING    stored
 ▢ ACCESSORY KIT                       [Issue kit]
     4 × BTN-4H BLACK 18L    outstanding 4
     1 × ZIP-YKK BLACK 60cm  outstanding 1
────────────────────────────────────────────────────
 Still awaiting ACCESSORIES before it can be received.
```

- **`part` is never inferred as `ACCESSORY`.** The server only ever guesses
  between the two cut parts. A mis-inferred kit would *spend stock nobody asked
  to spend*, so the kit is always an explicit button press.
- **The scan is idempotent.** A second tap issues nothing and returns the same
  `200` with `issued_now: []`. Do not disable the button defensively; a
  double-tap is safe by construction.
- **`unresolved[]` outranks everything else on the screen.** It means the recipe
  asks for stock that does not exist under that article — no amount of scanning
  fixes it, and `next_action` says so. Show that sentence, not "awaiting
  accessories".

### 2.6 · Anywhere a barcode is scanned (CHANGED)

`GET /barcode/resolve` returns `material_requirement` on **both** the piece
payload and the drawer payload. Render the panel from `kit_status`:

| `kit_status` | What it means | What to render |
|---|---|---|
| `NOT_REQUIRED` | this style declares no accessories | **hide the panel entirely** |
| `PENDING` | declared, nothing issued yet | the checklist, all outstanding |
| `PARTIAL` | some issued, or a line will not resolve | the checklist + the warning |
| `ISSUED` | every line issued in full | the checklist, ticked |

`NOT_REQUIRED` is the state of **every style released before this feature**.
Collapsing it into `PENDING` would show a thousand live garments an empty
accessory panel they can never satisfy.

---

## 3 · API delta

### 3.1 · NEW · the recipe — `/styles/{style_id}/material-spec`

Reads: DM, MD, HR, CUTTING/LINING/STITCHING manager, SECURITY, STORE_MANAGER.
Writes: **DM / MD only**. All writes `409` once the style is RELEASED.

| Method | Path | Purpose |
|---|---|---|
| GET | `/styles/{id}/material-spec` | the grid + `release_blockers` |
| PUT | `/styles/{id}/material-spec` | save the whole grid (idempotent) |
| POST | `/styles/{id}/material-spec/lines` | add one line |
| PATCH | `/styles/{id}/material-spec/lines/{line_id}` | edit one line |
| DELETE | `/styles/{id}/material-spec/lines/{line_id}` | soft-remove one line |
| POST | `/styles/{id}/material-spec/confirm` | sign off — **unlocks release** |
| POST | `/styles/{id}/material-spec/copy-from` | seed from another style |
| GET | `/styles/{id}/material-spec/requirement` | qty x per-piece vs stock |

**`PUT` request**

```jsonc
{ "lines": [
  { "sku_id": null, "category": "LEATHER", "subtype": null,
    "article": "SUEDE-A32", "colour": "PINE GREEN", "thickness": "1.2mm",
    "size": null, "qty_per_piece": 12.5, "material_lot_id": "…", "note": null },
  { "sku_id": null, "category": "ACCESSORY", "subtype": "BUTTON",
    "article": "BTN-4H", "colour": "BLACK", "size": "18L", "qty_per_piece": 4 },
  { "sku_id": "…", "category": "ACCESSORY", "subtype": "BUTTON",
    "article": "BTN-4H", "colour": "TAN", "size": "18L", "qty_per_piece": 4 }
] }
```

**`GET` / `PUT` response**

```jsonc
{ "style_id": "…", "style_code": "JP-CLERMONT", "style_name": "CLERMONT",
  "production_status": "DRAFT", "editable": true,
  "confirmed": false, "confirmed_at": null, "confirmed_by": null,
  "no_accessories_declared": null,
  "release_blockers": ["CLERMONT's material spec has not been confirmed. …"],
  "sku_overrides_count": 1,
  "lines": [
    { "line_id": "…", "scope": "STYLE", "sku_id": null,
      "category": "ACCESSORY", "subtype": "BUTTON", "article": "BTN-4H",
      "colour": "BLACK", "thickness": null, "size": "18L",
      "qty_per_piece": 4.0, "uom": "pcs", "material_lot_id": "…",
      "note": null, "is_active": true,
      "resolution": "PINNED",            // PINNED | MATCHED | NONE | AMBIGUOUS
      "lot": { "lot_id": "…", "article": "BTN-4H", "colour": "BLACK",
               "uom": "pcs", "on_hand": 4000.0, "reserved": 0.0,
               "available": 4000.0 },
      "candidate_lot_ids": [] } ] }
```

**`POST .../confirm`** — `{"no_accessories": false}` →

```jsonc
{ "style_id": "…", "confirmed": true, "confirmed_at": "…", "confirmed_by": "DM",
  "no_accessories_declared": false,
  "line_count": 5, "accessory_line_count": 2,
  "release_blockers": [],
  "warnings": ["No LINING line — the lining cut screen will not prefill …"],
  "message": "CLERMONT's material spec is confirmed. It may now be released." }
```

**`GET .../requirement`** — adds to each line: `pieces`, `total_required`,
`short_by`, `suggested_supplier`; and at the top `qty_ordered`, `short_lines`,
`confirmed`, `release_blockers`, `message`.

**Errors**: `422` unknown `(category, subtype)` · `422` missing `article` ·
`422` `sku_id` from another style · `422` `qty_per_piece: 0` on a style-wide line
· `409` duplicate material on one style · `409` the style is RELEASED.

### 3.2 · NEW · the off-spec correction — `POST /materials/issues`

Roles: DM, MD, **STORE_MANAGER**.

```jsonc
// request
{ "piece_barcode": "PC-004112", "lot_barcode": "LOT-ACC-000112",
  "employee_barcode": "EMP-0042", "qty": 4,
  "note": "spec said BLACK, gave TAN" }
// 201
{ "piece_code": "…", "article": "BTN-4H", "colour": "TAN", "qty": 4.0,
  "uom": "pcs", "source": "MANUAL", "available_after": 396.0,
  "stock_warning": null, "message": "Recorded 4 pcs of BTN-4H issued to …" }
```

**Why this exists.** A RELEASED style's recipe is frozen, so a typo'd article
would otherwise be uncorrectable for a whole order and the floor would issue
stock the system never saw. This records what was *actually* handed over without
rewriting the recipe under live garments. It is deliberately **repeatable**, and
it never makes the kit checklist think a spec line was satisfied.

### 3.3 · CHANGED · `POST /drawers/store-scan`

**Request** — `part` widens to `^(LEATHER|LINING|ACCESSORY)$`, plus one optional
field used only on an accessory scan:

```jsonc
{ "employee_barcode": "EMP-0042", "drawer_barcode": "DRW-0042",
  "piece_barcode": "PC-004112", "part": "ACCESSORY",
  // OPTIONAL. Omit to issue the whole kit as the recipe says. Send it to issue
  // part of the kit (short of one article) or to substitute a lot for one line.
  "lines": [ { "spec_id": "…", "qty": 4, "material_lot_id": "…" } ] }
```

**Response** — everything you read today is unchanged; two keys are added:

```jsonc
{ "…all existing fields…",
  "awaiting": ["ACCESSORIES"],       // this array may now contain ACCESSORIES
  "late_kit": false,                 // NEW: kit issued into an already-RECEIVED drawer
  "kit": {                           // NEW: returned on EVERY scan, not just ACCESSORY
    "status": "ISSUED",              // NOT_REQUIRED | PENDING | PARTIAL | ISSUED
    "summary_line": "4 pcs BTN-4H BLACK 18L · 1 pcs ZIP-YKK BLACK 60cm",
    "issued_now":     [ { "spec_id": "…", "article": "BTN-4H", "colour": "BLACK",
                          "size": "18L", "qty": 4.0, "uom": "pcs",
                          "lot_id": "…", "available_after": 3996.0 } ],
    "already_issued": [],            // populated on a repeat scan
    "outstanding":    [],
    "unresolved":     [],            // { article, reason, candidate_lot_ids, note }
    "stock_warnings": [],            // one per line that went short — see below
    "complete": true } }
```

On a **LEATHER/LINING** scan the `kit` block is **read-only** — nothing is
issued. That is deliberate: the person holding the leather is the person who also
has to find the buttons, so the checklist is answered on the scan they were
already doing.

**Shortfall warns, never blocks.** If a lot goes short the issue is still
recorded, `on_hand` may go negative, and `stock_warnings[]` explains it. Same
rule the cut path has always had: the buttons are physically in the operator's
hand, and refusing to record them to protect a number loses the record.

### 3.4 · CHANGED · `GET /barcode/resolve`

**No schema change** — `piece` and `drawer` were already free-form objects. Both
gain one key; the drawer also gains `accessories_in`.

```jsonc
"material_requirement": {
  "kit_status": "PENDING", "kit_required": true, "spec_confirmed": true,
  "summary_line": "4 pcs BTN-4H BLACK 18L · 1 pcs ZIP-YKK BLACK 60cm",
  "leather": { "article": "SUEDE-A32", "colour": "PINE GREEN",
               "thickness": "1.2mm", "qty_per_piece": 12.5, "uom": "dcm",
               "lot_id": "…", "available": 4200.0, "resolution": "MATCHED",
               "consumed": 14.0 },        // what the CUT actually recorded
  "lining": { "…same shape…" },
  "accessories": [
    { "spec_id": "…", "scope": "STYLE", "subtype": "BUTTON",
      "article": "BTN-4H", "colour": "BLACK", "size": "18L",
      "qty_per_piece": 4.0, "uom": "pcs",
      "issued_qty": 0.0, "outstanding": 4.0,
      "lot_id": "…", "available": 4000.0,
      "resolution": "MATCHED", "short": false } ] }
```

`leather.qty_per_piece` is what the recipe says; `leather.consumed` is what the
cut actually recorded. **The two differing is normal** and is the first thing a
costing question asks about — show both.

### 3.5 · CHANGED · `POST /production/log`

Request gains one optional field inside `consumption`:
`use_style_spec: false` (see §2.4 — prefer prefilling instead).

Response gains two fields:

```jsonc
"consumption_source": "typed",     // "typed" | "style_spec" | null
"kit_by_piece": {                  // deliberately lean — a 40-piece batch
  "PC-004112": { "kit_required": true,
                 "kit_status": "PENDING",
                 "outstanding": 5.0 } }
```

`GET /production/piece-state` gains `suggested_dcm_per_piece` and the full
`material_requirement` block.

### 3.6 · CHANGED · `POST /imports/breakdown/{order}/release`

**Request is unchanged.** Rejected entries keep their existing shape and gain a
`blockers[]` array:

```jsonc
{ "style_id": "…", "style_code": "JP-CLERMONT",
  "reason": "CLERMONT cannot be released yet. CLERMONT's material spec has not been confirmed. …",
  "blockers": [ "CLERMONT's material spec has not been confirmed. …",
                "CLERMONT has no LEATHER line — …" ] }
```

Render `blockers[]` as a list against the row; `reason` is the same content
joined, for callers that only read one string.

---

## 4 · Nothing you have today breaks

| Surface | Verdict |
|---|---|
| `GET /barcode/resolve` | **No schema change.** Both payloads gained one key; none removed. Machine-checked in `tests/system/test_style_spec_endpoints.py`. |
| `POST /drawers/store-scan` | +2 optional response fields. `part` pattern only *widens* — a client sending LEATHER/LINING is unaffected. |
| `GET /drawers` · `GET /drawers/{id}` | +`accessories_in`, +`kit_required`, both booleans defaulting to false. |
| `POST /production/log` | +2 optional response fields, +1 optional request field. |
| `POST /imports/breakdown/{order}/release` | Request untouched. `rejected[]` keeps its shape. |
| `/materials/*` | Unchanged. |
| Styles already in production | Unchanged in every respect — see below. |

**Why styles already on the floor are unaffected, by construction.** They have no
recipe lines, so `kit_required` is false, so the completeness rule
(`leather AND (lining OR no-lining) AND (kit OR no-kit-required)`) collapses to
exactly the two clauses it had before. Every drawer, every RECEIVED transition
and every send behaves identically. That is asserted over the whole truth table
in `tests/unit/test_kit_rules_pure.py`, not merely intended.

---

## 5 · Open items (backend, not yours)

- **Reservations are reported, never created.** `requirement` shows `reserved`
  beside `available`, but this feature does not reserve stock — nothing in the
  system can currently *release* a `MaterialReservation`, and creating one would
  permanently wedge `adjust_lot` and `retire_lot`. Separate ticket.
- **Line-stitching is not gated on the kit**, deliberately. Accessories are an
  input to finishing, not to stitching; blocking the line for a button it does
  not yet need would stall production. The kit gates the **send** — the garment
  physically leaving the store — which is the moment that matters.
