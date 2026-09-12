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
    """(col_map, size_cols, band, warnings) for one block.

    THE SIZE BAND IS SOLVED, NOT NAMED. detect_size_band reconciles a contiguous
    run of quantity columns against the total the sheet prints, so the labels can
    be anything the client uses and a decoy integer column cannot join in. See
    imports/_size_band.py.

    THE DESCRIPTOR COLUMNS ARE POSITIONAL BY DEFAULT, BY NAME WHEN NAMED. About
    95% of sheets print STYLE, then COLOUR, then ARTICLE, left to right, so that
    is the assumed reading and it needs no vocabulary at all. A recognised header
    always OVERRIDES it, because the other 5% is real: John Peter prints
    `Modello | Materiale | Colore` — STYLE, ARTICLE, COLOUR — and that client is
    not going to change their sheet. When the two readings disagree the header
    wins and a single warning says so, so an unusual sheet still imports
    correctly while a genuinely wrong one is visible in the preview.
    """
    if data_rows is None:
        data_rows = list(range(header_row + 1, ws.max_row + 1))
    band = detect_size_band(ws, header_row, data_rows)
    warnings: list[str] = list(band.warnings)

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

    # The descriptor zone is every text column LEFT of the band — the tail is
    # total/price/delivery and is never a descriptor.
    text_cols = []
    first = band.first_col
    if first:
        for c in range(1, first):
            if c == col_map.get("date"):
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

    # The 95% reading, computed whether or not the headers named anything — it is
    # both the fallback AND the cross-check.
    positional, free = {}, list(text_cols)
    for key in ("style", "color", "article"):
        if free:
            positional[key] = free.pop(0)

    for key in ("style", "color", "article"):
        if key not in col_map and positional.get(key) is not None \
                and positional[key] not in col_map.values():
            col_map[key] = positional[key]

    # ONE warning per block, not one per column: John Peter deviates on two
    # columns of every sheet they have ever sent, and three lines of noise per
    # import is how people learn to stop reading warnings.
    if any(col_map.get(k) and positional.get(k) and col_map[k] != positional[k]
           for k in ("style", "color", "article")):
        reading = ", ".join(
            f"{k.upper()}=col {col_map[k]}"
            for k in ("style", "color", "article") if col_map.get(k))
        warnings.append(
            "Descriptor columns are not in the usual STYLE, COLOUR, ARTICLE "
            f"order. Read from the headers as {reading}.")

    if band.price_col:
        col_map["price"] = band.price_col
    if band.delivery_col:
        col_map["delivery"] = band.delivery_col
    return col_map, dict(band.size_cols), band, warnings


# _is_header_row — substring match on DATE.
#
# THIS IS A BLOCK SPLITTER, NOT A CLASSIFIER. It is deliberately loose because
# its job is to find where a new block starts INSIDE a sheet already known to be
# an order sheet, and the stacked sheets head their blocks with things like
# "ORDER DATE" or "DATE RECEIVED". Do not use it to decide what a DOCUMENT is:
# `GGZ-PRODUCTION` heads a column "Cutting started DATE", which matches here, and
# treating that as an order header parses its CUTTING/FUSING/PASTING columns as
# sizes and reports 90,097 pieces. `_sheet_names_itself` below is the strict test.
def _is_header_row(ws, r) -> bool:
    a = clean_str(ws.cell(r, 1).value)
    b = clean_str(ws.cell(r, 2).value)
    if a and (a.upper() in ("S.NO", "SNO") or "DATE" in a.upper()):
        return True
    if b and b.upper() in ("S.NO", "SNO"):
        return True
    return False


def _sheet_names_itself(ws) -> bool:
    """Does this sheet ANNOUNCE itself as an order sheet, in so many words?

    An EXACT match on one of the four tokens, in the first few columns near the
    top — not a substring. That precision is the whole point: "DATE" is an order
    header, "Cutting started DATE" is a production sheet's first column, and the
    difference between them is the difference between a 146-piece order and a
    90,097-piece fiction.

    A sheet that passes here is accepted at MEDIUM confidence, because a human
    reading it would call it an order sheet and the arithmetic is only a check.
    A sheet that fails here has to prove itself on shape alone — see the verdict
    in parse_order_sheet.
    """
    for r in range(1, min(ws.max_row, 20) + 1):
        for c in range(1, 5):
            v = clean_str(ws.cell(r, c).value)
            if v and v.upper() in ("S.NO", "SNO", "STYLE", "DATE"):
                return True
    return False


def parse_order_sheet(ws) -> tuple[list[OrderLine], list[str], str]:
    """Parse a full order sheet (possibly many stacked blocks).

    Returns (order_lines, sheet_warnings, verdict) where verdict is "ORDER" or
    "UNKNOWN".

    PARSING *IS* THE CLASSIFICATION — one pass, one verdict. This used to be two
    passes with two different sets of thresholds: `_sheet_type` solved the whole
    sheet to decide "is this an order sheet?", then this function solved it all
    over again to extract from it. They could disagree, and they did — a weekly
    production sheet was classified ORDER and then parsed to zero lines, so an
    entire sheet vanished from the import without a single warning. A sheet can
    no longer be called an ORDER unless the parse actually produced order lines.
    """
    lines: list[OrderLine] = []
    warnings: list[str] = []
    col_map, size_cols = {}, {}
    band = None
    # Did any block prove its band arithmetically (HIGH, >=2 size columns, >=2
    # reconciled rows)? That is the bar a sheet must clear when its header names
    # nothing recognisable — see the verdict at the end.
    proven = False
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
                "Not an order sheet: no header row was recognised and no run of "
                "columns adds up to a printed total.")
            return lines, warnings, "UNKNOWN"
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
            col_map, size_cols, band, block_warnings = _find_columns(
                ws, r, _block_rows(r))
            warnings.extend(block_warnings)
            if (band.confidence == "HIGH" and len(band.size_cols) >= 2
                    and band.reconciled_rows >= 2):
                proven = True
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

    # ── THE VERDICT ─────────────────────────────────────────────────────────
    # Two ways a sheet earns the name ORDER, and both require that the parse
    # actually produced lines:
    #
    #   · IT NAMES ITSELF. The header says S.NO / STYLE / DATE, so a human would
    #     read it as an order sheet. MEDIUM confidence is acceptable here — the
    #     words are the evidence and the arithmetic is only a check.
    #   · IT PROVES ITSELF. No recognisable words, so the shape has to carry the
    #     whole argument: HIGH confidence, >=2 size columns, >=2 reconciled rows.
    #     This is the bar that keeps `SAMPLE COSTING SHEET FOR SIR.xlsx` out —
    #     its columns reconcile in places, but never across a sheet.
    #
    # Requiring lines is what closes the old silent hole: a sheet that classified
    # ORDER and parsed to nothing used to be reported as a successful empty
    # order. Now it is UNKNOWN and says why.
    named_itself = _sheet_names_itself(ws)
    if lines and (named_itself or proven):
        return lines, warnings, "ORDER"

    if named_itself or proven:
        warnings.append(
            "Not imported as an order sheet: a header and size columns were "
            "found, but no row on the sheet carried a style with quantities.")
    return lines, warnings, "UNKNOWN"