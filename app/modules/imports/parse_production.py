"""
parse_production.py — Parse PRODUCTION sheets into structured cards.

A production sheet contains one or more "cards" (one per style/colourway).
Each card has:
  - a title line (e.g. "CLINTON-TOBAC-100 PCS")
  - a header row: WEEK PERIOD | CUTTING | FUSING | ... (operations)
  - week rows (date ranges) AND worker-tag rows (e.g. "SARWARE-(14/01..)"),
    both carrying piece counts per operation
  - a QTY row, a RATE row, a TOTAL row
  - an "Overall Total" recap block we ignore (it duplicates QTY)

The header can start in column A (RICANO/NIPAL) or column B (KJ/GGZ). We detect
which by finding the cell that literally says "WEEK PERIOD".
"""
from __future__ import annotations
from dataclasses import dataclass, field
from app.modules.imports.excel_reader import clean_str, to_int, looks_like_week_period, is_summary_label


@dataclass
class ProductionRow:
    label: str                       # the week period text or worker tag
    is_worker_tag: bool              # True if it's a named worker, not a date range
    counts: dict[str, int]           # {"CUTTING": 81, "FUSING": 81, ...}
    source_row: int


@dataclass
class ProductionCard:
    title: str
    operations: list[str]            # ordered op codes from the header
    rows: list[ProductionRow] = field(default_factory=list)
    qty: dict[str, int] = field(default_factory=dict)      # printed QTY row
    rate: dict[str, int] = field(default_factory=dict)     # printed RATE row
    total: dict[str, int] = field(default_factory=dict)    # printed TOTAL row
    grand_total: int | None = None
    warnings: list[str] = field(default_factory=list)


def _find_header(ws, start_row):
    """From start_row downward, find the 'WEEK PERIOD' header.

    Returns (header_row, label_col, op_cols) or (None, None, None).
    op_cols maps column index -> operation code.
    """
    for r in range(start_row, ws.max_row + 1):
        for lc in (1, 2):                          # label may be in col A or B
            cell = clean_str(ws.cell(r, lc).value)
            if cell and cell.upper() == "WEEK PERIOD":
                op_cols = {}
                for c in range(lc + 1, ws.max_column + 1):
                    h = clean_str(ws.cell(r, c).value)
                    if h:
                        op_cols[c] = h.upper()
                return r, lc, op_cols
    return None, None, None


def _is_worker_tag(label: str) -> bool:
    """A worker tag starts with a name (letters) then often '-(date range)'.

    'SARWARE-(14/01/2026 TO 30/01/2026)'  -> worker
    '14/01/2026 TO 22/01/2026'            -> week (starts with a digit)
    'LADIES GROUP TAILOR'                 -> worker (letters, no date range)
    """
    import re
    s = label.strip()
    if re.match(r"^\d", s):                # starts with a digit -> it's a date range
        return False
    # starts with a letter -> a name (with or without a trailing date range)
    return bool(re.match(r"^[A-Za-z]", s))


def parse_production_sheet(ws) -> tuple[list[ProductionCard], list[str]]:
    """Parse a production sheet that may contain several stacked cards."""
    cards: list[ProductionCard] = []
    warnings: list[str] = []
    r = 1
    n = ws.max_row

    while r <= n:
        # A card begins with a non-empty title cell in column A that is not a header.
        title = clean_str(ws.cell(r, 1).value)
        header_row, label_col, op_cols = _find_header(ws, r)
        if header_row is None:
            break                                  # no more cards

        # The title is the col-A text on the row just above the header (or earlier).
        card_title = None
        for tr in range(header_row - 1, r - 1, -1):
            t = clean_str(ws.cell(tr, 1).value)
            if t and "WEEK PERIOD" not in t.upper():
                card_title = t
                break
        if not card_title:
            card_title = clean_str(ws.cell(r, 1).value) or f"Card@row{header_row}"

        card = ProductionCard(title=card_title, operations=list(op_cols.values()))

        # Read rows below the header until we hit the next card's title/header or end.
        rr = header_row + 1
        while rr <= n:
            label = clean_str(ws.cell(rr, label_col).value)
            # stop if a new header appears
            _, _, peek_ops = _find_header(ws, rr) if False else (None, None, None)
            # detect a new card: a title line followed by a WEEK PERIOD header soon after
            nxt_hdr, _, _ = _find_header(ws, rr)
            if nxt_hdr is not None and nxt_hdr == rr:
                break

            counts = {}
            for c, op in op_cols.items():
                v = to_int(ws.cell(rr, c).value)
                if v is not None:
                    counts[op] = v

            tag = is_summary_label(label)
            if tag == "QTY":
                card.qty = {op: counts.get(op, 0) for op in op_cols.values()}
            elif tag == "RATE":
                card.rate = {op: counts.get(op, 0) for op in op_cols.values()}
            elif tag == "TOTAL":
                card.total = {op: counts.get(op, 0) for op in op_cols.values()}
                # grand total is often the lone number after the op columns
                for c in range(max(op_cols) + 1, ws.max_column + 1):
                    gv = to_int(ws.cell(rr, c).value)
                    if gv:
                        card.grand_total = gv
            elif tag == "OVERALL":
                break                              # recap block; card is done
            elif label:
                # a real data row: week range OR worker tag.
                # A worker tag has an alphabetic NAME, optionally followed by a
                # date range in parentheses, e.g. "SARWARE-(14/01.. TO ..)".
                # A pure week row is just a date range with no leading name.
                if any(v for v in counts.values()):
                    is_worker = _is_worker_tag(label)
                    card.rows.append(ProductionRow(
                        label=label,
                        is_worker_tag=is_worker,
                        counts={k: v for k, v in counts.items() if v},
                        source_row=rr))
            rr += 1

        # Validate: do the week+worker rows sum to the printed QTY per operation?
        summed = {}
        for row in card.rows:
            for op, v in row.counts.items():
                summed[op] = summed.get(op, 0) + v
        for op, printed in card.qty.items():
            got = summed.get(op, 0)
            if printed and got != printed:
                card.warnings.append(
                    f"{card.title}: {op} rows sum to {got} but QTY row says {printed}")

        cards.append(card)
        r = rr                                     # continue after this card

    return cards, warnings
