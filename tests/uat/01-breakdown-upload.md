# UAT 01 — Breakdown upload mints the whole order

**Role:** Direct Manager · **Prerequisites:** pre-flight complete, one client exists
**Business rule:** every garment has a barcode and a drawer *before* it is ever cut.

This is the pre-mint inversion. Pieces used to appear at cutting; they now appear at
upload. If this scenario fails, nothing downstream can be scanned.

---

## Steps

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 1 | Log in as the DM | Token issued; `GET /auth/me` shows `direct_manager` | ☐ |
| 2 | `POST /imports/preview` with the client's order-sheet workbook (.xlsx) | 200, a preview listing every style, colour, size and quantity | ☐ |
| 3 | Read the preview against the paper order sheet | Every style and colour on paper appears; totals match | ☐ |
| 4 | Confirm the order number is not already in use | The preview names the order; a duplicate is refused later | ☐ |
| 5 | `POST /imports/commit` with the same workbook and the order number | 200; a summary reporting pieces and drawers created | ☐ |
| 6 | Count the pieces created | Exactly `sum(qty_ordered)` across every SKU — not one more, not one fewer | ☐ |
| 7 | Pick any SKU and list its pieces | Sequence numbers run `1..N` with no gaps and no repeats | ☐ |
| 8 | Read a piece code | Format is `{SKU-CODE}-{seq}`, zero-padded to 3 (e.g. `…-PINE-M-007`) | ☐ |
| 9 | `GET /barcode/resolve?code=<a piece code>` | 200, `type: PIECE`, and the payload names the right style/colour/size | ☐ |
| 10 | Check that piece's drawer | It has one, and the drawer is in state `MERGED` | ☐ |
| 11 | `GET /barcode/resolve?code=<that drawer's code>` | 200, `type: DRAWER` | ☐ |
| 12 | Check `needs_lining` on a lined style and an unlined one | True for the lined style, False for leather-only | ☐ |
| 13 | Print a sheet of piece barcodes via `POST /barcode/print` | A payload with one entry per piece, captions readable | ☐ |
| 14 | Physically scan one printed label with the floor scanner | Resolves to the same piece as step 9 | ☐ |

## Idempotency

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 15 | Commit the **same** workbook again, same order number | Top-up behaviour: no duplicate pieces | ☐ |
| 16 | Count pieces again | Unchanged from step 6 | ☐ |

> ⚠️ **Known blocker.** Step 15 currently returns **500**, not a clean result.
> `premint.py:36-41` assumes pieces cascade when a SKU is deleted; they do not, so
> the replace path hits a foreign-key error. Since pieces now exist from upload time,
> **every** re-import of a committed order hits this. Record it and continue.
> (`docs/audit/pass-01-business-logic.md`)

## Negative cases

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 17 | Upload a `.pdf` renamed to `.xlsx` | 400 — the container check catches it, not just the extension | ☐ |
| 18 | Upload a file larger than the configured limit (default 25 MB) | 413, and no partial order is created | ☐ |
| 19 | Commit with an order number that already exists | Refused; the existing order is untouched | ☐ |
| 20 | Try `POST /imports/commit` as HR or a Cutting Manager | 403 — upload is DM-only | ☐ |
| 21 | Upload a workbook with a quantity of 0 for one SKU | That SKU produces no pieces; the rest of the order still imports | ☐ |

## What "pass" means

The order exists, the piece count equals the ordered quantity exactly, every piece
resolves by barcode, every piece has a drawer, and a non-DM cannot do any of it.

**Two pieces sharing a code, or a piece with no drawer, is a hard fail** — the rest
of the system keys off both.
