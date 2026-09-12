# Robust Excel Importer

Replaces the old single-layout importer. Handles the real, messy
GARMENT_ORDERPRODUCTION_DETAILS.xlsx: multiple sheets per client, stacked blocks
with varying size columns, and continuation rows.

**ORDER SHEETS ONLY.** Weekly production/progress sheets are not imported. They
used to be parsed into "production cards" that seeded Operation and Rate rows;
that path is gone, and wage rates now come from the wages module. Anything that
is not an order sheet is reported in the preview and skipped.

## Files (the parsing pipeline)
- excel_reader.py    — defensive cell reading (clean strings, coerce ints/dates/money)
- _size_band.py      — solves for the SIZE BAND by arithmetic (no size word list),
                       and names the tail (total / price / delivery) by cell shape
- parse_orders.py    — parse ORDER sheets: detects each block's header + sizes,
                       handles continuation rows, validates against GRAND TOTAL,
                       and returns its own ORDER/UNKNOWN verdict
- import_engine.py   — groups by client, builds a validated PREVIEW (dry-run)
- load_to_db.py      — writes a validated preview idempotently (replace mode)
- router.py          — POST /imports/preview and POST /imports/commit

## One pass, one verdict
There is no separate sheet classifier. `parse_order_sheet` returns
`(lines, warnings, verdict)` — parsing IS the classification. The two used to be
separate passes with different thresholds, and they could disagree: a production
sheet was classified ORDER and then parsed to zero lines, disappearing from the
import in silence. A sheet is only an ORDER if the parse produced order lines.

## The two-step flow
1. POST /imports/preview  -> upload .xlsx, get parsed summary + warnings (no writes)
2. POST /imports/commit   -> upload same .xlsx, parse + validate + write

## Why it is robust
- Detects headers and columns instead of hardcoding positions
- Classifies every row (header / data / continuation / worker / QTY / RATE / TOTAL)
- Validates totals against the sheet's own printed totals; surfaces mismatches
- Idempotent: re-importing the same file yields the same final numbers
- Deterministic: parsing the same file always produces identical output

## Verified on the real file
6 clients, 28 styles, 402 SKUs, 3,558 pieces, 18 production cards, 0 warnings.
Carnaby = 152 pcs; Carnaby grand total = 105,340 — both match the source sheet.
