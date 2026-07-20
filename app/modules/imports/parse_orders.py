"""
parse_orders.py — Parse ORDER sheets into structured rows.

An order sheet contains one or more "blocks". Each block has its own header
row (S.NO | STYLE | COLOUR | ...size columns... | TOTAL) followed by data rows.
Some sheets are "flat": a single header with Date|Style|Colour|Article|sizes.

We DETECT the header wherever it is and read whatever size columns it declares,
rather than hardcoding column positions. This is what makes it survive the fact
that every block can have a different set of sizes.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from app.modules.imports.excel_reader import clean_str, to_int, to_date

# Words that appear in a size-column header. Anything that is one of these,
# or looks like a numeric size, is treated as a size column.
ALPHA_SIZES = {"XS", "S", "M", "L", "XL", "XXL", "XXXL", "2XL", "3XL", "4XL",
              "F", "FREE"}
NON_SIZE_HEADERS = {"S.NO", "SNO", "STYLE", "COLOUR", "COLOR", "SUEDE COLOUR",
                    "SUEDE COLOR", "ARTICLE", "ARTICLES", "TOTAL", "TOTAL QTY",
                    "DATE", "SIZES", "DESCRIPTION"}


def _is_size_header(text: str | None) -> bool:
    if not text:
        return False
    t = text.strip().upper()
    if t in NON_SIZE_HEADERS:
        return False
    base = t.split("/")[0].strip()      # "S/7" -> "S"
    if base in ALPHA_SIZES:
        return True
    return base.replace(".", "").isdigit()


@dataclass
class OrderLine:
    style: str
    color: str | None
    article: str | None
    sizes: dict[str, int]          # {"M": 53, "L": 52, ...}
    total: int
    source_row: int                # for traceability in warnings
    order_date: "date | None" = None
    warnings: list[str] = field(default_factory=list)


def _find_columns(ws, header_row: int):
    col_map = {}
    size_cols = {}
    sub_row = header_row + 1
    for c in range(1, ws.max_column + 1):
        h = clean_str(ws.cell(header_row, c).value)
        sub = clean_str(ws.cell(sub_row, c).value)
        H = h.upper() if h else None
        if H == "STYLE":
            col_map["style"] = c
        elif H in ("COLOUR", "COLOR", "SUEDE COLOUR", "SUEDE COLOR"):
            col_map["color"] = c
        elif H in ("ARTICLE", "ARTICLES"):
            col_map["article"] = c
        elif H and "DATE" in H and "date" not in col_map and "DELIVERY" not in H:
            col_map["date"] = c
        elif _is_size_header(sub):
            size_cols[c] = sub.upper()
        elif _is_size_header(h):
            size_cols[c] = h.upper()
    return col_map, size_cols


# _is_header_row — substring match on DATE
def _is_header_row(ws, r) -> bool:
    a = clean_str(ws.cell(r, 1).value)
    b = clean_str(ws.cell(r, 2).value)
    if a and (a.upper() in ("S.NO", "SNO") or "DATE" in a.upper()):
        return True
    if b and b.upper() in ("S.NO", "SNO"):
        return True
    return False


def parse_order_sheet(ws) -> tuple[list[OrderLine], list[str]]:
    """Parse a full order sheet (possibly many stacked blocks).

    Returns (order_lines, sheet_warnings).
    """
    lines: list[OrderLine] = []
    warnings: list[str] = []
    col_map, size_cols = {}, {}
    last_style = None
    last_article = None
    declared_block_total = None        # the subtotal printed under a block
    running_block_total = 0
    last_date = None

    def close_block():
        nonlocal running_block_total, declared_block_total
        running_block_total = 0
        declared_block_total = None

    for r in range(1, ws.max_row + 1):
        # New header? Re-detect columns for this block.
        if _is_header_row(ws, r):
            close_block()
            col_map, size_cols = _find_columns(ws, r)
            if not size_cols:
                warnings.append(f"Header at row {r} had no recognizable size columns")
            continue

        if not size_cols:
            continue                    # haven't hit a header yet; skip preamble

        # Read the candidate data row.
        style = clean_str(ws.cell(r, col_map.get("style", 2)).value)
        color = clean_str(ws.cell(r, col_map.get("color", 3)).value)
        article = clean_str(ws.cell(r, col_map.get("article", 0) or 999).value) \
            if col_map.get("article") else None    
        row_date = (
            to_date(ws.cell(r, col_map["date"]).value)
            if col_map.get("date")
            else None
        )
        sizes = {}
        for c, label in size_cols.items():
            q = to_int(ws.cell(r, c).value)
            if q and q > 0:
                sizes[label] = q
        row_total = sum(sizes.values())

        # A "continuation" row: no style but has a colour + quantities -> inherit style.
        if not style and color and row_total > 0:
            style = last_style
            article = article or last_article
            row_date = row_date or last_date 

        if style and row_total > 0:
            last_style = style
            if article:
                last_article = article
            eff_date = row_date or last_date
            if eff_date:
               last_date = eff_date 
            running_block_total += row_total
            lines.append(OrderLine(
                style=style, color=color, article=article,
                sizes=sizes, total=row_total, source_row=r, order_date=eff_date))
        else:
            # Not a data row. It may be a SUBTOTAL row: a lone number sitting in
            # the TOTAL column (or any column) with no style and no size spread.
            # We capture the largest lone number as the block's declared subtotal,
            # so close_block() can cross-check it against what the rows summed to.
            lone_vals = []
            for c in range(1, ws.max_column + 1):
                if c in size_cols:
                    continue
                v = to_int(ws.cell(r, c).value)
                if v and v > 0:
                    lone_vals.append(v)
            if lone_vals and not style:
                declared_block_total = max(lone_vals)

    close_block()

    # Reliable validation: find the GRAND TOTAL the sheet prints (a cell whose
    # neighbour text says 'GRAND TOTAL', or the single largest lone number on the
    # sheet) and compare it to what our parsed rows actually sum to.
    parsed_total = sum(l.total for l in lines)
    grand = None
    for r in range(1, ws.max_row + 1):
        for c in range(1, ws.max_column + 1):
            txt = clean_str(ws.cell(r, c).value)
            if txt and "GRAND TOTAL" in txt.upper():
                # the grand total number is usually just to the right or below
                for cc in range(c, ws.max_column + 1):
                    v = to_int(ws.cell(r, cc).value)
                    if v:
                        grand = v
                        break
    if grand is not None and grand != parsed_total:
        warnings.append(
            f"GRAND TOTAL check: sheet says {grand}, parsed rows sum to {parsed_total}")
    elif grand is not None:
        warnings.append(f"OK: grand total {grand} matches parsed rows")

    return lines, warnings
