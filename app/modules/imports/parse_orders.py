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
from decimal import Decimal
from app.modules.imports.excel_reader import clean_str, to_int, to_date, to_money
from app.modules.imports._size_band import (
    detect_size_band, find_header_row, is_delivery_header, is_price_header)

# ── SIZES ARE NO LONGER RECOGNISED FROM A WORD LIST ─────────────────────────
# ALPHA_SIZES used to decide what a size was, so a brand shipping Italian 38-62
# or a "48/M" combined label had its columns silently dropped, and any stray
# integer header (a year, an item code) was invented into a size. The band is
# now SOLVED arithmetically — see imports/_size_band.py — which needs no
# vocabulary and gains a new country for free.
#
# NON_SIZE_HEADERS survives only to keep descriptor columns out of the band on
# the legacy two-row-header sheets; it no longer decides what IS a size.
NON_SIZE_HEADERS = {"S.NO", "SNO", "STYLE", "COLOUR", "COLOR", "SUEDE COLOUR",
                    "SUEDE COLOR", "ARTICLE", "ARTICLES", "TOTAL", "TOTAL QTY",
                    "DATE", "SIZES", "DESCRIPTION"}

# Descriptor headers. NOT a size list — these name the text columns LEFT of the
# band, and there is a positional fallback below for sheets that use none of
# them. Bounded and one-line-editable, like the price/delivery aliases.
STYLE_HEADERS = {"STYLE", "MODELLO", "MODEL", "MODELE", "ESTILO"}
COLOUR_HEADERS = {"COLOUR", "COLOR", "SUEDE COLOUR", "SUEDE COLOR", "COLORE",
                  "COULEUR"}
ARTICLE_HEADERS = {"ARTICLE", "ARTICLES", "MATERIALE", "MATERIAL",
                   "DESCRIPTION", "DESCRIZIONE"}


@dataclass
class OrderLine:
    style: str
    color: str | None
    article: str | None
    sizes: dict[str, int]          # {"M": 53, "L": 52, ...} — labels VERBATIM
    total: int
    source_row: int                # for traceability in warnings
    order_date: "date | None" = None
    warnings: list[str] = field(default_factory=list)
    # ── per-STYLE commercial facts, printed once on the style's row ─────────
    # They live on the LINE because that is where the sheet prints them; the
    # loader folds them up onto the Style, which is the level they describe.
    unit_price: "Decimal | None" = None
    currency: str | None = None
    delivery_date: "date | None" = None
    # HIGH when the row's sizes reconciled against a printed total, MEDIUM when
    # the sheet printed no total to check against. Carried per line so the
    # preview can tell the human WHICH rows were merely inferred.
    size_confidence: str = "HIGH"


def _find_columns(ws, header_row: int, data_rows: list | None = None):
    """(col_map, size_cols, band) for one block.

    THE SIZE BAND IS SOLVED, NOT NAMED. detect_size_band reconciles a contiguous
    run of quantity columns against the total the sheet prints, so the labels can
    be anything the client uses and a decoy integer column cannot join in. See
    imports/_size_band.py.

    THE DESCRIPTOR COLUMNS still go by header, with a POSITIONAL FALLBACK: on a
    sheet whose descriptors are in a language nobody catalogued, everything left
    of the band is descriptor text, so the first populated text column is the
    style and the next is the colour. That keeps an unknown sheet importable
    instead of rejecting it for want of a word.
    """
    if data_rows is None:
        data_rows = list(range(header_row + 1, ws.max_row + 1))
    band = detect_size_band(ws, header_row, data_rows)

    col_map = {}
    for c in range(1, ws.max_column + 1):
        h = clean_str(ws.cell(header_row, c).value)
        H = h.upper() if h else None
        if H in STYLE_HEADERS and "style" not in col_map:
            col_map["style"] = c
        elif H in COLOUR_HEADERS and "color" not in col_map:
            col_map["color"] = c
        elif H in ARTICLE_HEADERS and "article" not in col_map:
            col_map["article"] = c
        elif (H and "DATE" in H and "date" not in col_map
                and not is_delivery_header(h)):
            col_map["date"] = c

    # Positional fallback for whatever the headers did not name. Only columns
    # LEFT of the band are considered — the tail is price/total/delivery and
    # never a descriptor.
    first = band.first_col
    if first:
        taken = set(col_map.values())
        text_cols = []
        for c in range(1, first):
            if c in taken:
                continue
            populated = [clean_str(ws.cell(r, c).value) for r in data_rows]
            populated = [v for v in populated if v]
            # Mostly-text and mostly-filled: a serial-number column is numeric,
            # a spacer column is empty, and neither describes the garment.
            if not populated:
                continue
            texty = sum(1 for v in populated if to_int(v) is None)
            if texty >= max(1, len(populated) // 2):
                text_cols.append(c)
        for key in ("style", "color", "article"):
            if key not in col_map and text_cols:
                col_map[key] = text_cols.pop(0)

    if band.price_col:
        col_map["price"] = band.price_col
    if band.delivery_col:
        col_map["delivery"] = band.delivery_col
    return col_map, dict(band.size_cols), band


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
    band = None
    last_style = None
    last_article = None
    declared_block_total = None        # the subtotal printed under a block
    running_block_total = 0
    last_date = None

    def close_block():
        nonlocal running_block_total, declared_block_total
        running_block_total = 0
        declared_block_total = None

    # WHERE DOES THE FIRST HEADER LIVE? The legacy signal is the literal words
    # S.NO / DATE in the first columns, which is how the stacked multi-block
    # sheets announce a new block — that still works and still splits blocks.
    # But a flat sheet headed `Modello | Materiale | Colore | 38 | …` says none
    # of those words, and used to parse to nothing at all. So when the words are
    # absent the header is found ARITHMETICALLY: it is the row above a run of
    # columns that adds up to a printed total (see _size_band.find_header_row).
    # BLOCK BOUNDARIES, COMPUTED UP FRONT. A stacked sheet restates its header
    # for every block, and each block declares a DIFFERENT set of sizes (KJ block
    # one is 46-56, block two is 25-28 plus 46-56). Detection therefore has to
    # look at one block's rows only: handed the whole sheet it sees three
    # incompatible layouts at once, reconciles none of them, and falls back to a
    # guessed band that drops columns — which is how a 347-piece order reported
    # 279. `_block_rows` gives each header exactly its own rows.
    header_rows = [r for r in range(1, ws.max_row + 1) if _is_header_row(ws, r)]

    def _block_rows(header_row: int) -> list:
        later = [h for h in header_rows if h > header_row]
        stop = later[0] if later else ws.max_row + 1
        return list(range(header_row + 1, stop))

    solved_header = None
    if not header_rows:
        solved_header, solved_band = find_header_row(ws)
        if solved_header is None:
            warnings.append(
                "No size columns could be reconciled on this sheet — no header "
                "row was recognised and no run of columns adds up to a printed "
                "total.")
            return lines, warnings
        if solved_band.confidence == "MEDIUM":
            warnings.append(
                f"MEDIUM confidence: this sheet prints no total to check the "
                f"size columns against, so the band "
                f"({', '.join(solved_band.size_cols.values())}) was inferred "
                f"from column shape. Eyeball it before committing.")
        for row, printed, summed in solved_band.mismatched_rows:
            warnings.append(
                f"Row {row}: the sheet prints a total of {printed} but its size "
                f"cells sum to {summed}.")

    for r in range(1, ws.max_row + 1):
        # New header? Re-detect columns for this block.
        if _is_header_row(ws, r) or r == solved_header:
            close_block()
            col_map, size_cols, band = _find_columns(ws, r, _block_rows(r))
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
        # PRICE AND DELIVERY, read where the sheet prints them: once, on the
        # style's own row. Both are per STYLE — not per SKU and not per order —
        # so they ride the line here and the loader folds them onto the Style.
        unit_price, currency = (
            to_money(ws.cell(r, col_map["price"]).value)
            if col_map.get("price") else (None, None))
        delivery_date = (
            to_date(ws.cell(r, col_map["delivery"]).value)
            if col_map.get("delivery") else None)

        sizes = {}
        for c, label in size_cols.items():
            q = to_int(ws.cell(r, c).value)
            if q and q > 0:
                # LABEL VERBATIM. clean_str + upper only, so "XL ", "xl" and "XL"
                # are ONE size instead of forking into three SKUs, three codes
                # and three barcodes — but "XXL/54" stays "XXL/54", because
                # whatever the client wrote is what the floor will read back.
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
                sizes=sizes, total=row_total, source_row=r, order_date=eff_date,
                unit_price=unit_price, currency=currency,
                delivery_date=delivery_date,
                size_confidence=(band.confidence if band else "HIGH")))
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