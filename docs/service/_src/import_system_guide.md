# 1. What this service is

This service turns the **breakdown sheet** — an Excel file the Direct Manager builds from the client's order sheet and spec sheet — into rows in the database, and then into **barcoded garments**.

It is the single most important service to understand, because **this is where a garment is born**. Before release, a style is a spreadsheet row you can freely correct. After release, it is physical garments with printed barcodes, and almost nothing can be changed.

| Part | File |
|---|---|
| HTTP routes | `app/modules/imports/router.py` |
| Excel parsing | `app/modules/imports/parse_orders.py`, `import_engine.py` |
| Writing to the database | `app/modules/imports/load_to_db.py` |
| Draft table, edit, release | `app/modules/imports/breakdown.py` |
| Minting pieces + barcodes | `app/modules/imports/premint.py` |

Everything here is **Direct Manager** work. The Managing Director passes too. Nobody else can call any of these endpoints.

---

# 2. The whole flow on one page

```
   1. PREVIEW        POST /imports/preview        nothing is written
         |           read the warnings
         v
   2. COMMIT         POST /imports/commit         styles + SKUs written as DRAFT
         |                                        NO barcodes yet
         v
   3. REVIEW         GET  /imports/breakdown/{order_number}
         |           PATCH /imports/breakdown/skus/{sku_id}      fix a line
         |           PATCH /imports/breakdown/styles/{style_id}  fix a style
         |           DELETE /imports/breakdown/skus/{sku_id}     drop a line
         |           POST  /imports/breakdown/{n}/cancel         drop a style
         v
   4. RELEASE        POST /imports/breakdown/{order_number}/release
                     >>> THE MINT <<<
                     creates Piece rows + piece barcodes
                     stamps needs_lining on every piece
                     CANNOT BE UNDONE
```

Five words to remember: **preview, commit, review, release, mint.**

---

# 3. Step 1 — Preview (safe)

`POST /api/v1/imports/preview` — form upload with two fields: `order_number` and `file`.

- The **order must already exist**. This endpoint never creates one. An unknown number gives **404** — *"Order number not found. Please verify with the client record."* Create the order first with `POST /clients` or `POST /clients/{id}/orders`.
- Nothing at all is written to the database.
- The answer is the parsed workbook: per client, per style, the size split, the price, the delivery date, and every warning.

## What the file must be

| Check | Failure |
|---|---|
| File name ends with `.xlsx` or `.xlsm` | **400** "Please upload an .xlsx file" |
| Size is within the server limit (`max_upload_mb`) | **413** "File exceeds the N MB limit." |
| The file really is a workbook (an `.xlsx` is a zip inside) | **400** "File is not a valid .xlsx workbook." |

The third check matters: a `.pdf` renamed to `.xlsx` is rejected **before** the parser opens it.

## How sheets are read

- Each worksheet is parsed **once**, and that one parse also decides what the sheet is. There is no separate "what type is this sheet" pass — two passes could disagree, and once did: a whole sheet vanished from an import with no warning.
- The sheet name's prefix is the **client key**: `KJ GARMENT ORDER` and `KJ PRODUCTION` both belong to client `KJ`.
- Only **order sheets** are imported. A spec sheet, a costing sheet or a weekly production sheet is reported in `sheets[]` as `UNKNOWN` and skipped, with a warning naming it.

## `size_confidence` — read this before showing numbers

For each style the preview returns `size_confidence`:

- **`HIGH`** — the per-size split was proved against a printed total on the sheet.
- **`MEDIUM`** — it could not be proved. The numbers may still be right, but nobody checked them. The reason is in `warnings`.

Show `MEDIUM` styles in a different colour. A wrong size split becomes a wrong number of garments, and after release it becomes wrong barcodes.

---

# 4. Step 2 — Commit (writes, but mints nothing)

`POST /api/v1/imports/commit` — same two fields, same file.

It parses again and **writes the styles and SKUs into that order**. What it does **not** do:

- It does **not** create the order (404 if missing).
- It does **not** create pieces or barcodes. `pieces_minted` is always `0` and `release_required` is always `true`.
- It does **not** create wage rates or operations any more. Those keys are still in the response (always `0`) so old callers do not break; rates are entered in the Wages module.

## It is idempotent — re-uploading is safe

Codes are deterministic and rows are matched on identity, so uploading the same workbook twice does **not** duplicate anything:

- a Style is matched on (order, name)
- a SKU is matched on (style, colour, size)
- a `code` is only written when a row is **created**, never rewritten

That is why `written.skus_updated` exists. A second upload with corrected quantities updates in place.

> **You do not need to save the response.** The order is a permanent row. `GET /imports/orders` lists every order with its breakdown status and a ready-made `breakdown_url`. Nothing is lost by closing the tab.

---

# 5. Step 3 — Review and correct the DRAFT

`GET /api/v1/imports/breakdown/{order_number}` is the screen between upload and production. Every style carries `production_status`:

| Status | Meaning | Editable? |
|---|---|---|
| `DRAFT` | uploaded, nothing minted | **Yes** |
| `RELEASED` | pieces exist with printed barcodes | **No** — 409 on any edit |
| `CANCELLED` | withdrawn, off the release screen | No |

Use the `editable` flag on each style row rather than comparing strings yourself.

## The four correction doors

| Call | Use it for |
|---|---|
| `PATCH /imports/breakdown/skus/{sku_id}` | one line: quantity, colour name/code, size, knit/nylon colour — **and optionally the parent style in the same call** |
| `PATCH /imports/breakdown/styles/{style_id}` | the style header on its own: name, article, code, gender, label, thickness, season, refs, unit price, currency, lining answer |
| `DELETE /imports/breakdown/skus/{sku_id}` | a line the sheet should not have had |
| `POST /imports/breakdown/{order_number}/cancel` | withdraw whole DRAFT styles so they leave the release screen |

Two things worth knowing:

- **The SKU patch can carry a `style` block.** The table shows a style and its SKUs as one editable row group, so both are written in **one transaction**. Either both save or neither does — the screen can never end up with the colour saved and the article not.
- **Anything structural is a re-upload.** Moving a SKU to a different style is not a correction; upload a corrected workbook instead (it is idempotent).
- A style `code` collision is **409**, not 500. The code is what a barcode caption and a rate card resolve through, so it is unique.
- An empty patch is **422** "Nothing to update."

---

# 6. Step 4 — Release: the mint

`POST /api/v1/imports/breakdown/{order_number}/release`

This is the moment a spreadsheet row becomes garments. In one transaction it:

1. creates one **Piece** row for every ordered unit of every SKU of the released style,
2. creates the **piece barcode** for each one (a short `PC-XXXXXX` code plus the long readable alias),
3. stamps `needs_lining` on the style and copies it onto every piece,
4. marks the style `RELEASED` with who did it and when, and writes an `audit_log` row.

**It cannot be undone.** A piece barcode is a permanent garment identity.

## Every style must answer the lining question

Send one entry per style:

```json
{
  "styles": [
    {"style_id": "9c2f...", "needs_lining": true},
    {"style_id": "9c2f...", "needs_lining": false}
  ]
}
```

When one answer covers the whole batch, send it once at the top:

```json
{ "style_ids": ["9c2f...", "9c2f..."], "needs_lining": true }
```

A per-style answer **beats** the top-level one, so you can broadcast the common case and name the exceptions.

### The three states of `needs_lining`

| Value | Meaning |
|---|---|
| `true` | the garment has a lining. It gets a LINING_CUTTING stage, and the store must hold **both** leather and lining before line-stitching. |
| `false` | leather only. No lining cut; the store releases it on leather alone. |
| `null` | **nobody has been asked.** This is not "no". Release refuses it. |

The DM's answer **outranks the system's guess in both directions** — including declaring a style whose name contains "KNIT" as leather-only. That is the whole reason the question is asked: a name cannot know that one particular wool shell has no lining, and the person holding the spec sheet can. The answer is audited with the actor and the time, so switching a lining requirement **off** is a traceable decision.

### The deprecated shape

`style_ids` **without** a top-level `needs_lining` still works, but it releases on a **guess** from the style name. Every style released that way comes back with `lining_declared: false`, is listed in `styles_released_without_lining_answer`, and is named in the warning inside `message`. Move the release screen to `styles` and this goes away.

> The request body uses `extra="forbid"`. A misspelt or misplaced field is a **422 that names it**, instead of being silently dropped — which is exactly what used to happen to a top-level `needs_lining`.

## Release is partial-accept

One bad style never loses the good ones. Read `released` and `rejected`, **not** the HTTP status. A style is rejected when:

| Reason | Fix |
|---|---|
| already `RELEASED` | nothing to do — releasing again mints nothing new |
| not on this order | wrong order number or wrong id |
| `qty_ordered` is 0 | correct the quantity first |
| the material spec is not ready | see below |

### The material-spec gate

A style cannot be released until its **per-piece material recipe** is confirmed. The blockers come back as whole sentences in `rejected[].blockers`, ready to display:

- *"…'s material spec has not been confirmed. Enter the per-piece consumption (PUT /styles/{id}/material-spec) and confirm it."*
- *"… has no LEATHER line — the dcm consumed per piece is what the material ledger and the costing are built on. Add it before releasing."*
- *"…'s spec names no accessories and nobody has declared that it needs none. Add the accessory lines, or confirm the spec with `no_accessories: true`."*

Release is the **last moment** anyone can be asked these questions, which is why they are asked here.

## Releasing more of a style later

Release **tops up**. To make more of a style that is already released, raise the quantity in a new upload (commit is idempotent and updates the SKU) and release again. The existing pieces are untouched; only the new ones are minted.

> There is no capacity limit. Releases used to be capped by a pool of 200 physical drawers, so a style of 100+ garments ran the pool dry partway down. The store is now a **state on the garment**, and a state cannot run out.

---

# 7. Status roll-up on the order index

`GET /imports/orders` gives every order a `breakdown_status` rolled up from its styles, so the list says what still needs doing without opening anything:

| Status | Meaning |
|---|---|
| `NOT_UPLOADED` | the order exists, no breakdown sheet committed yet |
| `DRAFT` | sheet uploaded, nothing released, nothing minted |
| `PARTIALLY_RELEASED` | some styles in production, some still editable |
| `RELEASED` | every live style released |
| `CANCELLED` | every style on the order was cancelled |

Rows are sorted **most-recently-worked-on first** (the newer of the order's creation and its last style write), not by the client's `order_date` — a sheet that landed this morning outranks an order raised months ago and never touched.

`pieces_minted` tells a `RELEASED` row that actually produced something apart from one that produced nothing.

---

# 8. Notes for backend developers

- **Parsing and the bulk load are synchronous** (openpyxl + the sync SQLAlchemy session). They run in a worker thread via `run_in_threadpool`, so the event loop is never blocked and the proven idempotent loader stays as it is.
- **No temp file.** Starlette already spooled the upload into a seekable file, and openpyxl reads a file-like object happily. The three guards (extension, size, zip container) run against that stream.
- **An order-level lock** is taken before the load (`_acquire_order_import_lock`), so two people committing the same order at the same time cannot interleave.
- **`premint.py` runs sync inside the release transaction.** It creates the Piece, its barcode, and sets `needs_lining` — atomically. It is idempotent: re-running tops up and never duplicates.
- **Lining declarations are stamped on the style before premint reads them.** That is why the declaration travels with the mint into the same session — splitting them would mint pieces against the previous answer.
