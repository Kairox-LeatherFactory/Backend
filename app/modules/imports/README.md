# Robust Excel Importer

Replaces the old single-layout importer. Handles the real, messy
GARMENT_ORDERPRODUCTION_DETAILS.xlsx: multiple sheets per client, two sheet
types (order + production), stacked blocks with varying size columns,
continuation rows, worker-tag rows, and stacked production cards.

## Files (the parsing pipeline)
- excel_reader.py    — defensive cell reading (clean strings, coerce ints/dates)
- parse_orders.py    — parse ORDER sheets: detects each block's header + sizes,
                       handles continuation rows, validates against GRAND TOTAL
- parse_production.py— parse PRODUCTION sheets: finds each card's header (col A
                       or B), reads week + worker rows, QTY/RATE/TOTAL, validates
                       row sums against the printed QTY row
- import_engine.py   — classifies sheets, groups by client, builds a validated
                       PREVIEW (dry-run, no DB writes)
- load_to_db.py      — writes a validated preview idempotently (replace mode)
- router.py          — POST /imports/preview and POST /imports/commit

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
