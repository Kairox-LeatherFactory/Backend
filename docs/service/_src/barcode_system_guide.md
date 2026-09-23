# 1. What this service is

The barcode service is **the front door of every scan in the factory**.

There is **one table** — `barcode_registry` — and every printed code in the building has exactly one row in it: a garment, a leather lot, a lining lot, an accessory lot, a single hide, an employee card. One scan endpoint reads that table and answers "what is this, and what is its live state?".

> Why one registry and not one table per thing: the scan gun does not know what it is pointing at. It sends a string. Something has to turn a string into meaning with **one indexed lookup**, and that something must never guess.

| Part | File |
|---|---|
| HTTP routes | `app/modules/barcode/router.py` |
| Resolve / print / lifecycle | `app/modules/barcode/service.py` |
| Code minting + queries | `app/modules/barcode/repository.py` |
| Tables | `app/modules/barcode/models.py` |
| Types and statuses | `app/core/enums_barcode.py` |

---

# 2. The barcode types

| Type | Names | Minted when | Can it be changed? |
|---|---|---|---|
| `PIECE` | one physical garment | the DM **releases** a style | **No.** Permanent garment identity. |
| `LEATHER_LOT` | a leather delivery on the shelf | a leather lot is created | No |
| `LINING_LOT` | a lining lot | a lining lot is created | No |
| `ACCESSORY_LOT` | a packet of buttons / zips / thread | an accessory lot is created | No |
| `LEATHER_SHEET` | **ONE hide** | a leather lot is created with `sheets` | No |
| `EMPLOYEE` | a worker's card | the employee is created | **Yes** — reissue or deactivate |
| `DRAWER` | retired | — | the module is withdrawn; rows kept for audit only |

## Why a hide gets its own code and a button does not

A **lot** code names a *specification* — "SUEDE-A32 · NAVY · 1.2 mm" — and what is behind it is interchangeable. One metre of that lining is any other metre; one button out of a 5,000-button packet is any other button. One code for the packet and a count that goes down says everything true about it.

A **leather sheet is not interchangeable.** It is one hide, individually measured (43, 47, 40 dcm — no two alike), individually expensive, and issued to one named cutter for one garment. "How much leather did this jacket take?" cannot be answered from a lot-level number — it is the sum of the specific hides that went into it. So the hide carries the code.

That is also why there is deliberately **no** accessory-unit code: it would mean printing five thousand labels to learn nothing the packet count already says.

---

# 3. The code formats

| Type | Format | Example |
|---|---|---|
| Piece (scannable) | `PC-` + 6 characters | `PC-23456A` |
| Piece (long identity, on the sticker text) | `STYLE-COLOUR-SIZE-seq` | `KJ2451-CLERMONT-57-M-005` |
| Employee | `EMP-` + 6 digits | `EMP-000123` |
| Leather lot | `LOT-LEA-` + 6 digits | `LOT-LEA-000041` |
| Lining lot | `LOT-LIN-` + 6 digits | `LOT-LIN-000012` |
| Accessory lot | `LOT-ACC-` + 6 digits | `LOT-ACC-000205` |
| Hide (sheet) | the sheet's own code | `LS-000041-03` |

## The small code and the sticker are two different things

A piece's identity used to **be** its printed code — about 24 characters. As a Code128 symbol that is a wide label and a slow, error-prone scan, and the client also wants the **article** on the sticker, which makes it longer still. So the two jobs were separated:

```
   the BARCODE carries a small unique id   ->  PC-23456A
   the STICKER prints the business fact    ->  order · style · article ·
                                               colour · size · serial
```

**Old labels still work.** The long code stays in the registry as an **alias row** pointing at the same piece. When you resolve one, the response carries `is_alias: true` — the scan worked, but the label is old and should be reprinted with the small code.

### Why base 30 and not base 36

The alphabet is `23456789ABCDEFGHJKMNPQRSTVWXYZ`. It leaves out `0`, `O`, `1` and `I` — the four characters a human re-keying a smudged label gets wrong. It is also in ascending ASCII order, so a fixed-width code sorts alphabetically exactly as it sorts numerically.

---

# 4. `GET /barcode/resolve` — the one call every scanner makes

Send the scanned string as `?code=`. Three outcomes, and the difference between the last two matters:

| Result | Meaning | What the screen should do |
|---|---|---|
| **200** | known and active | act on `type` |
| **404** | this code has never existed | "Unknown barcode — check the label" |
| **410 Gone** | it existed and was **retired** | "This card was retired" — a different message from 404 |

A 410 is not an error in the system. It is the correct answer for a card that was reissued, or for a worker who has left. **Their history is untouched** — only the scannable code was retired.

Any logged-in user can call resolve, including the legacy `employee` role. That is deliberate: the gate operator must be able to resolve a worker's card on the attendance screen, and a 403 there would stop the shift.

## What comes back

```json
{
  "code": "PC-23456A",
  "type": "PIECE",
  "active": true,
  "caption": "NB-B-1 · CLERMONT · GOAT SUEDE · DARK BROWN · 46 · 005",
  "is_alias": false,
  "piece": { ...see below... },
  "employee": null, "lot": null, "sheet": null,
  "next_expected_scan": null,
  "next_stage": "FUSING",
  "next_stage_label": "Fusing",
  "next_stage_blocked_reason": null
}
```

**Branch on `type`.** Exactly one of `piece`, `employee`, `lot`, `sheet` is filled in — except for a hide, which returns **both** `sheet` (the skin in your hand) and `lot` (what article and colour it is).

### `next_expected_scan` — which code to present next

| You scanned | It says | Why |
|---|---|---|
| `EMPLOYEE` | `"PIECE"` | the worker is identified; the garment is next |
| any lot | `"PIECE"` | a material was named; scan what it is cut for |
| a hide | `"PIECE"` | same |
| `PIECE` | `null` | there is nothing left to present |

The store scan is **two codes, not three** — worker, then garment. There is no box to find. This field is **guidance for the screen**, not permission: the authority on whether a scan is legal is the store service.

### `next_stage` — where the garment is going

For a piece code only, the response says which production stage it is due at next, and, when that stage cannot be logged yet, **why** (`next_stage_blocked_reason` — for example the garment has not been released from the store). The stage is still reported when blocked: the screen shows where the piece is going **and** what is holding it there.

`next_stage` is `null` once the piece has finished the chain.

## The `piece` block

| Field | Meaning |
|---|---|
| `piece_id` | UUID of the garment |
| `code` | the long human identity |
| `short_code` | the compact `PC-…` the label carries. `null` on a very old piece not yet backfilled — the long code still scans |
| `sku_id`, `sku_code` | its colour/size line |
| `style_id`, `style_name`, `article` | its design |
| `colour`, `size`, `seq`, `serial` | `serial` is `seq` zero-padded — `"005"` |
| `order_id`, `order_number`, `client` | where it came from |
| `current_stage` | the last stage logged |
| `store` | `{state, holding, leather_in, lining_in, accessories_in}` |
| `store_state` | the same word, flat, for older callers |
| `leather_consumption_dcm` | dcm recorded at the cut, or `null` |
| `needs_lining` | declared at release. Decides whether the store must hold both parts |
| `material_requirement` | the kit checklist for this garment |
| `label_line` | the sticker text, pre-joined, so every screen prints it identically |

## The `employee` block

`employee_id`, `name`, `designation`, `wage_type`, `is_active`.

## The `lot` block

`lot_id`, `category`, `subtype`, `article`, `colour`, `thickness`, `size`, `uom`, `on_hand`, `available`.

## The `sheet` block

`sheet_id`, `code`, `dcm`, `status`, `cutting_row_id`.

`status` is the field an operator is usually asking about. The same label reads **IN_STOCK** on the shelf, **ALLOCATED** once a draft cutting row claims it, **ISSUED** in the cutter's hands, and **CONSUMED** after the cut is logged.

---

# 5. Printing labels

`POST /barcode/print` takes **one** of three things and returns one entry per label:

| Send | Meaning |
|---|---|
| `codes: ["PC-23456A", ...]` | print these exact labels |
| `sku_id` | every piece of that colour/size line |
| `order_id` | every piece of that order |

Sending none of them is a **422**.

Each label carries:

- `code` — **encode this** as Code128. It is the compact id.
- `caption` and `label_line` — **typeset this as text** underneath.
- `details` — `{order_number, article, style, colour, size, serial, piece_code}`, or `null` for a label that names no garment (employee, lot, hide).
- `known` — `false` means the code is not in the registry. Print it if you want, but nothing will resolve it.

`GET /barcode/materials` is the **reprint screen for material labels**. Branch on `kind`:

| `kind` | One sticker for… | Carries |
|---|---|---|
| `LOT` | the shelf | article · colour · thickness · total `on_hand` |
| `SHEET` | one skin | article · colour · that hide's `dcm`, plus `sheet_status` and `cutting_row_id` |

Omit `kind` to get both, each lot followed by its own hides. A row whose `status` is `retired` belongs to a retired lot or hide: **grey it out, do not print it**.

---

# 6. The employee card lifecycle

`PATCH /employees/{employee_id}/barcode` with `{"action": "reissue"}` or `{"action": "deactivate"}`. HR, DM or MD only.

| Action | When | What happens |
|---|---|---|
| **reissue** | the card is lost or damaged | the old code is retired, a new one is minted. **History is untouched.** The worker keeps every production event and every wage line. |
| **deactivate** | the worker leaves | the registry row flips to `RETIRED`, so resolve answers **410**. The employee row, all production events and all wage lines **stay exactly as they are**. |

> **You delete the scannable code, never the person or their record.** This is a rule of the system, not a preference. Payroll, traceability and any later dispute all depend on history that cannot be edited by someone leaving.

The response is `{employee_id, employee_barcode, active, history_preserved}`. `history_preserved` is always `true` — it is there so the screen can say so to the person pressing the button.

---

# 7. The order screens

These four reads exist so somebody can answer "did this order's barcodes actually get made?" without opening a garment.

| Endpoint | Use |
|---|---|
| `GET /barcode/orders` | the order picker — which orders have barcodes, how many, first and last mint date. Paged, newest minting first. |
| `GET /barcode/orders/by-number/{order_number}` | turn a human order number into that picker row. **404** if the order has no barcodes yet. |
| `GET /barcode/orders/{order_id}/skus` | SKU + style options for the filter dropdowns |
| `GET /barcode/orders/{order_id}/analytics` | planned vs generated vs balance, for the order and per style |
| `GET /barcode/orders/{order_id}/barcodes` | the history table, filterable by SKU / style / style+size / status / date range |

## Reading the analytics

```json
{
  "order_id": "...",
  "order_total": {"planned": 840, "generated": 840, "balance": 0,
                  "active": 838, "retired": 2, "duplicates": 0,
                  "half_minted": false, "fully_generated": true},
  "by_style": [{"style_id": "...", "style_name": "CLERMONT",
                "style_code": "...", "planned": 152, "minted": 152, "balance": 0}]
}
```

- `duplicates` **must be 0**. It is an integrity proof: two codes for one garment would break traceability. If it is ever non-zero, raise it with the backend team immediately.
- `half_minted` is `true` when some, but not all, of the order's pieces exist — normally a style that was released while others were still draft.

> Note the history table uses the **older page shape** — `{order_id, page, page_size, total, pages, items}` with `page` starting at 1 — not the `{items, total, limit, offset}` envelope. It is an already-published contract that a screen reads today, so it was left alone deliberately.

---

# 8. `GET /barcode/pieces/{code}/materials`

Scan a garment and see **what goes into it, and what does not**. This is the scan-gun door onto the same answer `GET /store/pieces/{code}/materials` gives — the same service call, so the store screen and the gun can never disagree.

| Block | What it holds |
|---|---|
| `applies` | leather, lining and every accessory line that reaches **this colourway at this size**, with `issued_qty` and `outstanding` |
| `not_applicable` | the style's other lines, each saying why it is not this garment's: `other_sku`, `other_size`, or `zeroed` |
| `issued` | the ledger — what physically went in, from which lot, when, through whose card, manual corrections included |
| `consumed` | the dcm actually recorded at the cut (it lives on the production event, not the issue ledger) |

Read `not_applicable` when a kit scan reports nothing to issue while the style's `/material-spec/requirement` shows a full recipe. That view is **style-wide**; a kit is issued **per garment**.

---

# 9. Notes for backend developers

- **`resolve` is one indexed lookup.** Keep it that way. It is on the critical path of every scan in the building.
- **`_payload_for` names every block the response can carry.** `BarcodeResolve` is used as a `response_model`, and FastAPI serialises **through** it — a key the model does not declare is silently dropped. That once cost an operator the entire `sheet` block: the service returned it correctly and the screen showed nothing. Add the field to the schema when you add a block.
- **The hide branch must come before the generic lot branch.** A `LEATHER_SHEET` row carries `material_lot_id` too, so the lot branch would swallow it and answer about the lot instead of the skin in the operator's hand.
- **Retirement applies to every type**, not just employee cards.
- **A sheet's registry code is the sheet's own code**, not a generated serial — the label and the row cannot disagree, and it avoids a known counter bug in the `EMP-` prefix minting.
