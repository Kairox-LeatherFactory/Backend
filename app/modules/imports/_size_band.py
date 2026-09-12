"""
================================================================================
imports/_size_band.py — find the size columns by ARITHMETIC, not by a word list
================================================================================
THE PROBLEM WITH A SIZE DICTIONARY
    The old detector asked "is this header one of XS/S/M/L/XL/... or a number?".
    That works until a brand ships sizes the list has never seen — Italian 38-62,
    French 36-52, a "48/M" combined label, a Japanese LL — and then the column is
    silently dropped and the order is short. Worse, it also says YES to any stray
    integer header (a year, an item code), inventing a size out of a decoy.

    A maintained list is a promise to keep editing code every time the factory
    wins a client in a new country. This module removes the promise.

THE SHAPE OF EVERY BREAKDOWN SHEET EVER SENT HERE
    [ descriptor text ] [ SIZE BAND: one column per size ] [ tail: total/price/... ]

    and the sheet PRINTS ITS OWN ANSWER: somewhere to the right there is a total,
    and that total is the sum of the size band. So the band is not recognised —
    it is SOLVED FOR:

        find a column T and a contiguous run of quantity columns [i..j], j < T,
        such that for EVERY data row:  sum(row[i..j]) == row[T]

    A run that satisfies that across a whole sheet is the size band, whatever the
    columns are called. A decoy integer column cannot join it (it would break the
    sum) and cannot be mistaken for it (it does not sum to anything).

WHY THE RUN MAY STOP SHORT OF T
    On the John Peter sheets the columns run
        ... | 38 | 40 | ... | 62 | Prezzo (€) | Totale Capi
    so the column immediately left of the total is the PRICE. A rule that
    required the run to end at T-1 would fail on every sheet that quotes a price
    before its total — which is most of them. So every contiguous run left of T
    is tried, not just the adjacent one.

CONFIDENCE, AND WHY A MISMATCH IS NEVER SILENT
    HIGH    a printed total exists and every row reconciles against it.
    MEDIUM  no total column anywhere — the band is the longest contiguous run of
            quantity columns outside the tail, and a human is told to eyeball it.
    A sheet that HAS a total which does NOT reconcile is never quietly accepted;
    it degrades to MEDIUM and reports the rows that disagree.
================================================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.modules.imports.excel_reader import clean_str, to_date, to_int, to_money

# ── THE ONLY ALIAS LIST IN THIS FILE, and it is not about sizes ──────────────
# These name the TAIL columns — the ones to the right of the band that must never
# be counted as sizes even when they hold numbers. Unlike a size vocabulary this
# list is bounded (a sheet has one price column and one delivery column) and
# adding a word is a one-line edit, not an ongoing obligation.
PRICE_HEADERS = {
    "PRICE", "PRICE (€)", "PRICE(€)", "UNIT PRICE", "UNIT PRICE (€)",
    "PREZZO", "PREZZO (€)", "PREZZO(€)", "PRIX", "RATE", "PRECIO",
}
DELIVERY_HEADERS = {
    "DELIVERY", "DELIVERY DATE", "DELIVERY DEADLINE", "CONSEGNA",
    "SHIP DATE", "SHIPMENT DATE", "DATA CONSEGNA",
}


def _norm(text: str | None) -> str:
    return (clean_str(text) or "").upper()


def is_price_header(text: str | None) -> bool:
    return _norm(text) in PRICE_HEADERS


def is_delivery_header(text: str | None) -> bool:
    return _norm(text) in DELIVERY_HEADERS


def _looks_like_a_size_label(text: str | None) -> bool:
    """Could this header be a size? A SHAPE test, never a vocabulary.

    This does NOT ask "is it one of XS/S/M/L/…" — that is the maintained list the
    whole module exists to avoid, and it is what silently dropped Italian 38-62.
    It asks only whether the label has the shape a size label can have:

      · short. Every size any client here has ever printed fits in 8 characters:
        S, XXXL, 38, 62, XXL/54, S/7.
      · if it is a number, a SMALL one. Garment sizes run to about 70; they are
        never 576000, and they never carry a decimal point.

    That second clause is what keeps a COSTING sheet out. `SAMPLE COSTING SHEET
    FOR SIR.xlsx` has a run of columns headed 576000 / 0.05 / 28800 that
    reconciles across 33 rows — arithmetically a perfect band, and on the numbers
    alone indistinguishable from a breakdown. It is only implausible once you
    look at what the columns are CALLED.

    A new country using 72, or 4XL, or 48/M, passes untouched.
    """
    t = (clean_str(text) or "").strip()
    if not t or len(t) > 8:
        return False
    compact = t.replace(" ", "")
    if "." in compact or "," in compact:
        return False                      # 0.05 is a rate, not a size
    if compact.isdigit():
        return int(compact) < 1000        # 38 yes, 576000 no
    return True


@dataclass
class SizeBand:
    """Where the sizes are on one block of one sheet, and how sure we are."""
    size_cols: dict = field(default_factory=dict)   # {col_index: verbatim label}
    total_col: int | None = None
    price_col: int | None = None
    delivery_col: int | None = None
    confidence: str = "NONE"                        # HIGH | MEDIUM | NONE
    reconciled_rows: int = 0
    mismatched_rows: list = field(default_factory=list)   # [(row, printed, summed)]
    # Things the human should see in the preview: a tail column whose header and
    # whose cells disagree, or a tail printed in an unusual order.
    warnings: list = field(default_factory=list)

    @property
    def first_col(self) -> int | None:
        return min(self.size_cols) if self.size_cols else None


def _qty(ws, r: int, c: int):
    """The cell as a quantity, or None. Blank and non-integer both read None."""
    return to_int(ws.cell(r, c).value)

def _is_quantity_column(ws, profile_rows: list, c: int) -> bool:
    """True when this column holds quantities rather than words.

    `profile_rows` is the SAMPLE the column is judged over, not the rows that
    will be parsed — see the note where it is built in detect_size_band.

    A size column may be entirely EMPTY — a sheet prints size 62 and orders none
    of it, and dropping the column would lose a label the client uses. So an
    empty column qualifies.

    TOLERANT OF TRAILER TEXT, and it has to be. The first version demanded that
    EVERY populated cell be numeric, which read beautifully and was wrong: real
    sheets print a "GRAND TOTAL" caption in the middle of the sheet, and on the
    GGZ order that caption sits in the size-50 column. One stray word ten rows
    below the data disqualified a genuine size column, split the contiguous run
    in half, and silently reported 65 pieces for a 146-piece order.

    So the test is a MAJORITY, not a purity: a quantity column is one whose
    numeric cells outnumber its text cells. The arithmetic downstream is what
    actually proves the band, and it cannot be fooled by tolerating a caption —
    a column of words never sums to anything.
    """
    numeric = text = 0
    for r in profile_rows:
        raw = ws.cell(r, c).value
        if raw is None or (isinstance(raw, str) and raw.strip() == ""):
            continue
        if to_int(raw) is None:
            text += 1
        else:
            numeric += 1
    if numeric == 0 and text == 0:
        return True                      # printed but unordered — still a size
    return numeric > text


def _row_sum(ws, r: int, cols: range) -> int:
    return sum((_qty(ws, r, c) or 0) for c in cols)


def _classify_tail(ws, profile_rows, labels, band_last, total_col, ncols):
    """Name the TAIL columns by SHAPE, with the header name as corroboration.

    THE TAIL IS STRUCTURAL: it is every labelled column right of the band, minus
    the total. That part needs no vocabulary at all. What the columns in it MEAN
    is then decided by what their cells actually are:

        cells that parse as dates   -> DELIVERY
        cells that parse as money   -> PRICE

    WHY NOT PURE POSITION. 95% of sheets print TOTAL | PRICE | DELIVERY, and the
    KairoX template does. But John Peter prints

        ... | 60 | 62 | Prezzo (€) | Totale Capi

    with the PRICE to the LEFT of the total, and that is a real client, not a
    malformed sheet. So position is the EXPECTATION (deviating from it raises a
    warning) and never the test.

    WHY NOT PURE NAME. PRICE_HEADERS/DELIVERY_HEADERS cannot know every word a
    client will use — 'Precio Unitario', 'Costo', 'Valor'. Shape reads those for
    free. The name is kept as a tiebreaker for a column whose cells are ambiguous,
    and as a cross-check: when the header says one thing and the cells say
    another, the cells win and the human is told.

    Returns (price_col, delivery_col, warnings).
    """
    warnings: list = []
    tail = [c for c in range(band_last + 1, ncols + 1)
            if c != total_col and labels.get(c)]

    price_col = delivery_col = None
    for c in tail:
        dates = money = 0
        for r in profile_rows:
            raw = ws.cell(r, c).value
            if raw is None or (isinstance(raw, str) and not raw.strip()):
                continue
            if to_date(raw) is not None:
                dates += 1
            elif to_money(raw)[0] is not None:
                money += 1

        if dates > money:
            shape = "DELIVERY"
        elif money:
            shape = "PRICE"
        else:
            shape = None                 # empty or all text — says nothing

        named = ("PRICE" if is_price_header(labels.get(c))
                 else "DELIVERY" if is_delivery_header(labels.get(c)) else None)

        if named and shape and named != shape:
            warnings.append(
                f"Column {c} is headed {labels[c]!r}, which reads as {named}, but "
                f"its cells look like {shape}. Reading it as {shape} — check the "
                f"sheet if that is wrong.")

        verdict = shape or named         # cells first, header as the tiebreaker
        if verdict == "PRICE" and price_col is None:
            price_col = c
        elif verdict == "DELIVERY" and delivery_col is None:
            delivery_col = c

    # THE 95% LAYOUT IS AN EXPECTATION, NOT A RULE. Everything still parses; the
    # human is simply told the sheet is printed the unusual way round.
    if total_col is not None and price_col is not None and price_col < total_col:
        warnings.append(
            f"PRICE is in column {price_col}, to the LEFT of TOTAL (column "
            f"{total_col}). It was read correctly; the usual layout is TOTAL, "
            f"then PRICE, then DELIVERY.")
    if (price_col is not None and delivery_col is not None
            and delivery_col < price_col):
        warnings.append(
            f"DELIVERY (column {delivery_col}) is printed before PRICE (column "
            f"{price_col}). Both were read correctly; the usual order is PRICE "
            f"then DELIVERY.")
    return price_col, delivery_col, warnings


def detect_size_band(ws, header_row: int, data_rows: list) -> SizeBand:
    """Solve for the size band on one block. See the module docstring.

    Returns an empty band (confidence NONE) when the block has no numeric shape
    at all, which is how a caller tells "this is not a breakdown block" from
    "this is a breakdown block I could not reconcile".
    """
    ncols = ws.max_column

    # WHICH ROWS DESCRIBE THE LAYOUT. A breakdown row carries several numbers —
    # a spread of sizes and usually a total. A FOOTER carries one: a bare
    # subtotal, or a "GRAND TOTAL" caption beside a single figure.
    #
    # Profiling the columns over footers as well as data is what broke the GGZ
    # order: its GRAND TOTAL caption sits in the size-50 column, so that column
    # read as text, the contiguous run split in half, and a 146-piece order
    # reported 65. Footers are excluded from the PROFILE only — every row is
    # still parsed normally afterwards, so nothing is dropped from the import.
    def _numeric_count(r):
        return sum(1 for c in range(1, ncols + 1) if _qty(ws, r, c) is not None)

    # WHY >= 2 AND NOT >= 1 — the name is the trap, so read this before widening it.
    # `profile_rows` is NOT a selection of rows to parse; every row is parsed
    # later regardless. It is the SAMPLE the COLUMNS are profiled over, handed to
    # _is_quantity_column below. Widening it to >= 1 admits footer rows, and a
    # footer caption sitting inside the band disqualifies a real size column:
    #
    #   GGZ-GARMENT ORDER
    #     2  S.NO|STYLE|COLOUR|44|46|48|50|52|54|TOTAL
    #     3     1|BOMBER A|MORO| 2|23|40|43|28|10|  146    <- the only data row
    #    11      |        |    |  |  |  |GRAND TOTAL| 146  <- caption in col 7 ("50")
    #
    #   >= 2 -> profile [3]      -> col 7: numeric=1 text=0 -> a quantity column
    #   >= 1 -> profile [3,5,11] -> col 7: numeric=1 text=1 -> DROPPED
    #
    # Dropping col 7 splits the run 4-10 into [4,5,6] and [8,9,10], so the band
    # becomes 44|46|48 and a 146-piece order reports 65. The same mechanism costs
    # NIPAL 259->206 and RICANO 150->109.
    profile_rows = [r for r in data_rows if _numeric_count(r) >= 2]
    if not profile_rows:
        # A one-size-one-row sheet has a single number per row and nothing to
        # spare. Fall back to any populated row rather than refusing to look.
        # This fires ONLY when the strict filter found nothing at all, which is
        # why relaxing the strict filter is not the same as having this fallback.
        profile_rows = [r for r in data_rows if _numeric_count(r) >= 1]
    if not profile_rows:
        return SizeBand()

    labels = {c: clean_str(ws.cell(header_row, c).value) for c in range(1, ncols + 1)}

    # A PROVISIONAL name-based exclusion, so a column that openly calls itself
    # PRICE or DELIVERY cannot be drafted into the band. This is not the answer —
    # the tail is named properly by _classify_tail once the band is known, which
    # is the only point at which "the tail" even exists. Chicken and egg: the
    # band defines the tail, so the tail cannot define the band.
    named_price = next((c for c in range(1, ncols + 1)
                        if is_price_header(labels.get(c))), None)
    named_delivery = next((c for c in range(1, ncols + 1)
                           if is_delivery_header(labels.get(c))), None)

    # A column may hold sizes only if it is labelled, holds whole numbers, and is
    # not one of the tail columns we can name outright.
    eligible = {
        c for c in range(1, ncols + 1)
        if labels.get(c) and c not in (named_price, named_delivery)
        and _looks_like_a_size_label(labels.get(c))
        and _is_quantity_column(ws, profile_rows, c)
    }

    best = None
    # Try every column that could be the printed total, rightmost first: on a real
    # sheet the total sits at the end of the band, and starting from the right
    # finds it before any accidental left-hand match.
    for total_col in sorted(
            (c for c in range(2, ncols + 1)
             if c not in (named_price, named_delivery) and labels.get(c)
             and any(_qty(ws, r, c) is not None for r in profile_rows)),
            reverse=True):
        if not any(_qty(ws, r, total_col) is not None for r in profile_rows):
            continue
        for start in range(1, total_col):
            if start not in eligible:
                continue
            for end in range(total_col - 1, start - 1, -1):
                run = range(start, end + 1)
                if any(c not in eligible for c in run):
                    continue
                if total_col in run:
                    continue
                # ONLY ROWS THAT ACTUALLY CARRY QUANTITIES ARE DATA ROWS.
                # A sheet's subtotal and GRAND TOTAL rows print a number in the
                # total column and nothing in the band — demanding that those
                # reconcile too means no total column can ever be confirmed, and
                # the whole sheet silently degrades to a guessed band. They are
                # not counter-examples, they are footers.
                checkable = [r for r in profile_rows
                             if any(_qty(ws, r, c) is not None for c in run)]
                if not checkable:
                    continue
                matched = [r for r in checkable
                           if _row_sum(ws, r, run) == _qty(ws, r, total_col)]
                if len(matched) != len(checkable):
                    continue
                # ZERO PROVES NOTHING. A run of all-zero columns reconciles
                # trivially against an all-zero total — 0+0+0 == 0 on every row —
                # and that is HIGH confidence for a band carrying no order at all.
                # `NIPAL-NEW PRODUCTION` is exactly this shape: its stage columns
                # are zero for seven rows, so a DATA row's literal values were
                # read as column labels and a weekly progress sheet came back
                # classified ORDER, parsing to 0 pieces with no warning at all.
                if not any(_row_sum(ws, r, run) > 0 for r in matched):
                    continue
                # Every row reconciles. Prefer the widest such run: a narrower
                # one only wins by dropping columns that happened to be empty,
                # and those are real sizes the client printed.
                score = (len(matched), end - start + 1)
                if best is None or score > best[0]:
                    best = (score, start, end, total_col, len(matched))
                break
        if best is not None:
            break

    if best is not None:
        _score, start, end, total_col, matched = best
        # The band is proven, so the tail now EXISTS and can be named by shape.
        price_col, delivery_col, tail_warnings = _classify_tail(
            ws, profile_rows, labels, end, total_col, ncols)
        return SizeBand(
            size_cols={c: _norm(labels[c]) for c in range(start, end + 1)},
            total_col=total_col, price_col=price_col, delivery_col=delivery_col,
            confidence="HIGH", reconciled_rows=matched, warnings=tail_warnings)

    return _fallback_band(ws, profile_rows, labels, eligible, ncols)


def _fallback_band(ws, profile_rows, labels, eligible, ncols) -> SizeBand:
    """No column reconciles. Take the widest labelled quantity run — at MEDIUM.

    TWO DIFFERENT SHEETS LAND HERE and they must not be reported the same way:

      · one prints NO total at all, so there was never anything to reconcile
        against. The band is a reasonable guess and a human should confirm it.
      · one DOES print a total that does not add up. That is a torn or edited
        sheet, and it is the case the arithmetic exists to catch — so the rows
        that disagree are named, never swallowed.

    Either way the verdict is MEDIUM, never HIGH: the caller surfaces it in the
    preview so nobody commits a band the machine could not prove.
    """
    runs, current = [], []
    for c in range(1, ncols + 1):
        if c in eligible:
            current.append(c)
        else:
            if current:
                runs.append(current)
            current = []
    if current:
        runs.append(current)
    if not runs:
        # No band at all, so there is no tail to speak of either.
        return SizeBand()

    run = max(runs, key=len)

    # THE TOTAL MAY BE THE LAST COLUMN OF THE RUN, and it is a quantity column
    # like any other, so the widest-run rule absorbs it. On the HIGH path that
    # cannot happen (a run containing its own total never reconciles), but here
    # nothing has reconciled yet — so the rightmost column is tested as a total
    # explicitly. Some rows matching and some not is the torn-sheet case, and it
    # is precisely what must be reported rather than folded into the band as an
    # extra "size" that double-counts every row.
    split_total = None
    if len(run) >= 2:
        body, last = run[:-1], run[-1]
        printed = [(r, _qty(ws, r, last)) for r in profile_rows]
        printed = [(r, v) for r, v in printed if v is not None]
        matches = [(r, v) for r, v in printed
                   if _row_sum(ws, r, range(min(body), max(body) + 1)) == v]
        if printed and matches:
            split_total = last
            run = body

    price_col, delivery_col, tail_warnings = _classify_tail(
        ws, profile_rows, labels, max(run), split_total, ncols)

    band = SizeBand(
        size_cols={c: _norm(labels[c]) for c in run},
        total_col=split_total,
        price_col=price_col, delivery_col=delivery_col, confidence="MEDIUM",
        warnings=tail_warnings)

    if split_total is not None:
        printed = [(r, _qty(ws, r, split_total)) for r in profile_rows]
        printed = [(r, v) for r, v in printed if v is not None]
        band.mismatched_rows = [
            (r, v, _row_sum(ws, r, range(min(run), max(run) + 1)))
            for r, v in printed
            if _row_sum(ws, r, range(min(run), max(run) + 1)) != v][:10]
        band.reconciled_rows = len(printed) - len(band.mismatched_rows)
        return band

    # If the sheet DID print something total-shaped, say exactly where it stopped
    # adding up. "row 14: sheet says 30, sizes sum to 27" is actionable; "medium
    # confidence" on its own is not.
    tail = [c for c in range(max(run) + 1, ncols + 1)
            if c not in (price_col, delivery_col) and labels.get(c)]
    for total_col in tail:
        printed = [(r, _qty(ws, r, total_col)) for r in profile_rows]
        printed = [(r, v) for r, v in printed if v is not None]
        if not printed:
            continue
        mismatches = [(r, v, _row_sum(ws, r, range(min(run), max(run) + 1)))
                      for r, v in printed]
        mismatches = [m for m in mismatches if m[1] != m[2]]
        if mismatches and len(mismatches) < len(printed):
            # Some rows reconcile and some do not — that is a damaged sheet, not
            # a different layout, and it is worth naming.
            band.total_col = total_col
            band.mismatched_rows = mismatches[:10]
            band.reconciled_rows = len(printed) - len(mismatches)
        break
    return band


def find_header_row(ws, max_scan: int = 30, min_width: int = 2) -> tuple:
    """(header_row, SizeBand) for the FIRST breakdown block, solved arithmetically.

    THE HEADER IS FOUND BY WHAT FOLLOWS IT, not by what it says. The old detector
    looked for the literal words S.NO / STYLE / DATE in the first columns, so a
    sheet headed `Modello | Materiale | Colore | 38 | 40 | …` — an ordinary
    Italian order sheet — was never classified at all. Here every candidate row is
    tried as a header and the winner is the one whose rows BELOW reconcile: a
    header row is simply the row above a band that adds up.

    Rows are tried top-down and the first HIGH-confidence hit wins, so a title
    row ("MAIN SS27") is skipped naturally — nothing under it reconciles as a
    band starting at that row, because the row beneath it is the real header and
    holds text, not quantities.
    """
    # WHY A MINIMUM WIDTH. A COSTING sheet has the same arithmetic shape as an
    # order sheet — a column of figures reconciling against a printed total — so
    # "it adds up" alone is not enough to call something a breakdown. What makes
    # a breakdown a breakdown is that it SPREADS one style across several size
    # columns. A single reconciling column is a quantity, or a per-pair amount,
    # and treating one as a size band is how `SAMPLE COSTING SHEET FOR SIR.xlsx`
    # came back as a 16,649,667-piece order.
    #
    # The cost of this rule is a genuine one-size order sheet whose header uses
    # no recognisable words; that sheet still imports through the word-based
    # header path, and by shape alone it is honestly indistinguishable from a
    # costing line.
    best_medium = None
    limit = min(ws.max_row, max_scan)
    for r in range(1, limit + 1):
        data_rows = list(range(r + 1, ws.max_row + 1))
        if not data_rows:
            break
        labelled = sum(1 for c in range(1, ws.max_column + 1)
                       if clean_str(ws.cell(r, c).value))
        # A header names at least a few columns. This stays LOW on purpose: the
        # real order sheets here head 10-21 columns, so raising the floor to 7 or
        # 10 never fires on real data — it only rejects narrow sheets and test
        # fixtures. What actually keeps a non-order sheet out is band QUALITY
        # (min_width below, plus the positive-quantity rule in detect_size_band),
        # which is a statement about the data rather than about how wide it is.
        if labelled < 3:
            continue
        band = detect_size_band(ws, r, data_rows)
        if len(band.size_cols) < min_width:
            continue
        if band.confidence == "HIGH":
            return r, band
        if band.confidence == "MEDIUM" and best_medium is None:
            best_medium = (r, band)
    if best_medium:
        return best_medium
    return None, SizeBand()