"""
================================================================================
scripts/load_trims_inventory.py — idempotent loader for the four trim workbooks
================================================================================
Loads LININGS.xlsx, RIBS.xlsx, THREADS.xlsx and ZIPPERS CURRENT INVENTORY.xlsx
into material_lot + barcode_registry.

WHY THIS IS A SIBLING OF load_leather_lots.py, NOT A COPY:
    Everything downstream of "a row became a lot" is IDENTICAL for leather and
    for trims — the spec key, the null-safe duplicate lookup, the caption format
    and the barcode-code format. Those are imported from load_leather_lots, not
    restated, for the reason that file already gives: a second copy drifts the
    moment someone adds a subtype, and the floor would print labels in a format
    the app does not mint. This module owns ONLY what is genuinely different:
    four dirty spreadsheet shapes -> one common row.

    Importing that module also runs its "register every model module" block,
    which is what stops SQLAlchemy's lazy mapper resolution from dying on the
    first query. Do not replace the import with a narrower one.

IDEMPOTENCY (the point of the script):
    The load key is the DB constraint, `uq_material_lot_spec` on
        (category, subtype, article, colour, thickness, size)
    reached through the SHARED `spec_key()`. Two levels of defence, both needed:
      * in-memory (`batch_lots`) — rows repeated INSIDE one run are topped up,
        because rows added earlier in the loop are unflushed and a DB query
        cannot see them.
      * in-DB (`find_existing_lot`) — a spec committed by a PREVIOUS run is
        SKIPPED, not topped up. Re-running must never inflate stock. A genuine
        new delivery goes through POST /materials/receive.
    Barcodes are minted only for lots this run actually inserted, so a re-run
    mints nothing either. Safe to run any number of times.

THE THREE DECISIONS THIS LOADER MAKES (each one is load-bearing — read before
editing, and see the run summary which reports every row they touched):

 1. UOM IS DERIVED, NEVER TRUSTED FROM THE SHEET.
        `uom_for(category, subtype)` is the authority, exactly as in the leather
        loader. The sheets disagree with it in two places and that disagreement
        is REAL, not a casing problem:
            RIBS    600 STRIPS  -> derived kg
            THREADS PCS/CONE/BOX -> derived mtrs
        A strip is not a kilo and a cone is not a metre, and no sheet supplies
        the conversion factor. The row is still loaded (a trim missing from
        inventory is worse than one with a soft unit), but `attributes` carries
        `source_unit`, `source_qty` and `uom_mismatch: true`, and every such row
        is listed in the summary. Fixing them later is a data update, not a
        migration. MTS->mtrs and NOS->pcs are renames, not conversions, so they
        are not flagged.

 2. THE PACK FORM GOES IN `size` FOR THREADS — IT IS NOT COSMETIC.
        'ART NO : 5009 - TKT : 30' in 'NAVY - 974' appears TWICE in THREADS.xlsx:
        30 PCS and 10 CONE. Same article, same colour, same ticket => the SAME
        spec key, so without a discriminator the second row silently tops up the
        first and 30 spools + 10 cones becomes "40". Three such collisions exist
        in that one file. `size` is unused by the THREAD spec (its filters are
        article/colour/thickness), so the pack form lives there: it keeps the
        key honest and stays out of the DM's filter boxes.

 3. FOR ZIPPERS THE DISCRIMINATOR IS TAPE SHADE, NOT PULLER.
        Puller is 'BRIDGE OPEN SLIDER' on every metal row; the column that
        actually varies is Tape Shade (YKK-169 vs YKK009), and it varies across
        the same size. So `size` is composed as '63 · YKK-169'. Puller is
        recorded in `attributes`. Get this backwards and six of the twelve metal
        zipper rows collapse into the other six.

WHAT IS DELIBERATELY LEFT INCOMPLETE:
    LININGS.xlsx has a Width column but no thickness, and MATERIAL_SPEC requires
    `thickness` for LINING/PLAIN_LINING. This loader writes MaterialLot directly
    (as the leather loader does) so the strict 422 in MaterialService.create_lot
    does not fire — the lot loads with thickness NULL, width in `size`, and
    `attributes.spec_incomplete: ["thickness"]` so it is greppable. Backfill it
    when the mill confirms the gauge. Nothing is silently invented.

SUPPLIERS:
    None of the four sheets names a supplier, so every lot lands with
    supplier_id NULL. That is accurate, not a gap — there is no name to upsert.

RUN:
    python -m scripts.load_trims_inventory --dry-run   # validate + report, no write
    python -m scripts.load_trims_inventory             # commit
    python -m scripts.load_trims_inventory --only THREADS.xlsx
    python -m scripts.load_trims_inventory --dir /path/to/workbooks
================================================================================
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from app.core.config import settings          # noqa: F401  (parity with sibling)
from app.core.database import SessionLocal
from app.core.enums import MaterialCategory, MaterialSubtype, uom_for
from app.modules.barcode.models import MaterialLot

# Shared with the leather loader ON PURPOSE — see the module docstring. This
# import also performs that module's model-registration block.
from scripts.load_leather_lots import (       # noqa: E402
    clean,
    find_existing_lot,
    lot_caption,
    mint_lot_barcodes,
    spec_key,
    to_decimal,
)

DEFAULT_DIR = "."

# The four workbooks, each pinned to the (category, subtype) it loads. A file
# not in this table is not loadable — we do not guess a material class from a
# filename.
LININGS = "LININGS.xlsx"
RIBS = "RIBS.xlsx"
THREADS = "THREADS.xlsx"
ZIPPERS = "ZIPPERS CURRENT INVENTORY.xlsx"

# Units that mean the derived unit under another name. A rename is safe; a
# genuine unit difference (STRIPS vs kg) must NOT be listed here — that is the
# uom_mismatch flag's job.
_UNIT_ALIASES = {
    "MTS": "mtrs", "MTRS": "mtrs", "M": "mtrs",
    "PCS": "pcs", "NOS": "pcs", "NO": "pcs", "PC": "pcs",
    "KG": "kg", "KGS": "kg",
}


@dataclass
class LotRow:
    """One spreadsheet line, already shaped like a MaterialLot."""
    category: str
    subtype: str | None
    article: str
    colour: str | None
    thickness: str | None
    size: str | None
    on_hand: Decimal
    attributes: dict = field(default_factory=dict)
    source: str = ""            # 'FILE row N' — for readable warnings


# ── spreadsheet reading ─────────────────────────────────────────────────────
def read_table(path: Path) -> list[dict]:
    """Rows of a one-sheet workbook as dicts keyed by its header labels.

    Finds the header by locating the row containing a 'Description' cell rather
    than assuming A1: ZIPPERS CURRENT INVENTORY.xlsx starts at column N after
    thirteen empty columns, and hard-coding an offset would break the moment
    someone tidies the file. The same reader therefore serves all four shapes.
    """
    try:
        import openpyxl
    except ModuleNotFoundError:
        sys.exit("openpyxl is required to read the workbooks: pip install openpyxl")

    ws = openpyxl.load_workbook(path, data_only=True).worksheets[0]
    grid = [list(r) for r in ws.iter_rows(values_only=True)]

    header_idx = header_cols = None
    for i, row in enumerate(grid):
        for j, cell in enumerate(row):
            if cell is not None and str(cell).strip().lower() == "description":
                header_idx, header_cols = i, j
                break
        if header_idx is not None:
            break
    if header_idx is None:
        sys.exit(f"{path.name}: no 'Description' header cell found.")

    headers = [
        (str(c).strip() if c is not None else "")
        for c in grid[header_idx][header_cols:]
    ]
    rows: list[dict] = []
    for offset, raw in enumerate(grid[header_idx + 1:], start=1):
        cells = list(raw[header_cols:])
        if all(c is None or str(c).strip() == "" for c in cells):
            continue
        record = {
            h: (None if v is None else str(v).strip())
            for h, v in zip(headers, cells) if h
        }
        record["__row__"] = header_idx + 1 + offset      # 1-based sheet row
        rows.append(record)
    return rows


# ── value helpers ───────────────────────────────────────────────────────────
def norm_num(v: str | None) -> str | None:
    """'63.0' -> '63', '21 cm' -> '21 cm', '—' -> None.

    openpyxl hands back floats for numeric cells, so a zipper size arrives as
    '63.0'. Left alone it would go into `size` as '63.0' and read as a typo on a
    printed label.
    """
    v = clean(v)
    if v is None or v == "—":
        return None
    try:
        f = float(v)
    except ValueError:
        return v
    return str(int(f)) if f.is_integer() else str(f)


def parse_money(v: str | None) -> tuple[float | None, str | None]:
    """'₹54.00' -> (54.0, 'INR'); 'USD 1.50' -> (1.5, 'USD'); 'Not provided' -> (None, None).

    Price is a costing/display fact, never stock — it is stamped into attributes
    WITH its currency because these four sheets mix INR and USD, and a bare
    number that lost its symbol would invite exactly the cross-currency sum this
    tuple exists to prevent.
    """
    v = clean(v)
    if not v or v.lower().startswith("not provided") or v == "—":
        return None, None
    currency = "INR" if "₹" in v else ("USD" if "usd" in v.lower() else None)
    m = re.search(r"[\d,]+(?:\.\d+)?", v)
    if not m:
        return None, currency
    try:
        return float(m.group(0).replace(",", "")), currency
    except ValueError:
        return None, currency


def split_colour_code(raw: str | None) -> tuple[str | None, str | None]:
    """'TAUPE – 985' -> ('TAUPE', '985'). Splits on en dash OR hyphen.

    The thread sheet writes the shade name and the mill's colour number in one
    cell with an EN DASH (U+2013), not a hyphen. Splitting on '-' alone leaves
    the code welded to the name and 'TAUPE' never matches 'TAUPE – 985' in a
    filter.
    """
    raw = clean(raw)
    if not raw:
        return None, None
    parts = re.split(r"\s*[–—-]\s*", raw, maxsplit=1)
    if len(parts) == 2 and parts[1]:
        return (clean(parts[0]) or None), (clean(parts[1]) or None)
    return raw, None


def unit_check(source_unit: str | None, derived: str) -> tuple[bool, str | None]:
    """(mismatch?, normalised source unit). See decision 1 in the docstring."""
    u = clean(source_unit)
    if not u or u == "—":
        return False, None
    canon = _UNIT_ALIASES.get(u.upper(), u.upper())
    return (canon.lower() != derived.lower()), u


def _base_attrs(qty_field: str, on_hand: Decimal, source_unit: str | None,
                derived_uom: str, price_cell: str | None, file_name: str) -> dict:
    """The attribute block every trim lot carries.

    `qty_field` is written so a seeded lot is shaped like an API-created one:
    MaterialService.create_lot reads the quantity out of the category's own field
    (dcm/mtrs/kg/count), and a lot missing it would be the odd one out on any
    screen or later validation that looks there.
    """
    mismatch, src_unit = unit_check(source_unit, derived_uom)
    price, currency = parse_money(price_cell)
    attrs: dict = {qty_field: float(on_hand), "source_file": file_name}
    if src_unit:
        attrs["source_unit"] = src_unit
    if mismatch:
        attrs["source_qty"] = float(on_hand)
        attrs["uom_mismatch"] = True
    if price is not None:
        attrs["unit_price"] = price
    if currency:
        attrs["currency"] = currency
    return attrs


# ── adapters: one dirty shape each -> LotRow ────────────────────────────────
def adapt_linings(rows: list[dict], fname: str) -> list[LotRow]:
    """Description | Colour | Width | Qty | Unit | Unit Price | Total Value"""
    out = []
    for r in rows:
        article = clean(r.get("Description"))
        if not article:
            continue
        derived = uom_for(MaterialCategory.LINING.value,
                          MaterialSubtype.PLAIN_LINING.value)
        on_hand = to_decimal(r.get("Qty"))
        attrs = _base_attrs("mtrs", on_hand, r.get("Unit"), derived,
                            r.get("Unit Price"), fname)
        width = norm_num(r.get("Width"))
        if width:
            attrs["width"] = width
        # thickness is genuinely absent from this sheet — recorded, not invented.
        attrs["spec_incomplete"] = ["thickness"]
        out.append(LotRow(
            category=MaterialCategory.LINING.value,
            subtype=MaterialSubtype.PLAIN_LINING.value,
            article=article, colour=clean(r.get("Colour")),
            thickness=None, size=width,
            on_hand=on_hand, attributes=attrs,
            source=f"{fname} row {r['__row__']}",
        ))
    return out


def adapt_ribs(rows: list[dict], fname: str) -> list[LotRow]:
    """Description | Colour | Specification | Qty | Unit | Unit Price | Total Value

    The BLACK row has Qty '—' and Specification 'ONLY FOR QUILTING'. It loads at
    on_hand 0 rather than being skipped: the material exists and the floor can
    scan it, it simply has no stock. Dropping it would make a real rib invisible.
    """
    out = []
    for r in rows:
        article = clean(r.get("Description"))
        if not article:
            continue
        derived = uom_for(MaterialCategory.LINING.value, MaterialSubtype.RIBS.value)
        on_hand = to_decimal(r.get("Qty"))
        attrs = _base_attrs("kg", on_hand, r.get("Unit"), derived,
                            r.get("Unit Price"), fname)
        spec = clean(r.get("Specification"))
        if spec and spec != "—":
            attrs["note"] = spec
        out.append(LotRow(
            category=MaterialCategory.LINING.value,
            subtype=MaterialSubtype.RIBS.value,
            article=article, colour=clean(r.get("Colour")),
            thickness=None, size=None,
            on_hand=on_hand, attributes=attrs,
            source=f"{fname} row {r['__row__']}",
        ))
    return out


_THREAD_DESC = re.compile(
    r"ART\s*NO\s*:?\s*(\d+)\s*[-–—]\s*TKT\s*:?\s*(\d+)\s*(.*)", re.I)


def adapt_threads(rows: list[dict], fname: str) -> list[LotRow]:
    """Description | Colour | Qty | Unit | Unit Price | Total Value

    'ART NO : 5009 - TKT : 30' is two facts in one cell: the article number and
    the ticket, which IS the thread's gauge in the trade — so it maps to
    `thickness`, the field the THREAD spec requires and filters on. An
    unparseable description falls back to the raw string as `article` (nothing
    corrupts; a later backfill can split it) rather than being dropped.
    See decision 2 for why `size` carries the pack form.
    """
    out = []
    for r in rows:
        desc = clean(r.get("Description"))
        if not desc:
            continue
        derived = uom_for(MaterialCategory.ACCESSORY.value,
                          MaterialSubtype.THREAD.value)
        on_hand = to_decimal(r.get("Qty"))
        attrs = _base_attrs("mtrs", on_hand, r.get("Unit"), derived,
                            r.get("Unit Price"), fname)
        attrs["raw_description"] = desc

        m = _THREAD_DESC.match(desc)
        if m:
            art_no, tkt, tail = m.group(1), m.group(2), (m.group(3) or "").strip()
            article, thickness = f"ART {art_no}", f"TKT {tkt}"
            attrs["art_no"] = art_no
            attrs["ticket"] = tkt
            if "lining" in tail.lower():
                attrs["use"] = "LINING"
        else:
            article, thickness = desc, None
            attrs["spec_incomplete"] = ["thickness"]

        colour, colour_code = split_colour_code(r.get("Colour"))
        if colour_code:
            attrs["colour_code"] = colour_code
        pack = clean(r.get("Unit"))
        if pack and pack != "—":
            attrs["pack"] = pack

        out.append(LotRow(
            category=MaterialCategory.ACCESSORY.value,
            subtype=MaterialSubtype.THREAD.value,
            article=article, colour=colour, thickness=thickness,
            size=pack if pack and pack != "—" else None,
            on_hand=on_hand, attributes=attrs,
            source=f"{fname} row {r['__row__']}",
        ))
    return out


def adapt_zippers(rows: list[dict], fname: str) -> list[LotRow]:
    """Description | Colour | Size / Specification | Puller | Tape Shade | Qty |
       Unit | Unit Price | Total Value

    See decision 3: `size` is 'length · tape shade' because tape shade is the
    column that actually varies across otherwise identical rows.
    """
    out = []
    for r in rows:
        article = clean(r.get("Description"))
        if not article:
            continue
        derived = uom_for(MaterialCategory.ACCESSORY.value,
                          MaterialSubtype.ZIP.value)
        on_hand = to_decimal(r.get("Qty"))
        attrs = _base_attrs("count", on_hand, r.get("Unit"), derived,
                            r.get("Unit Price"), fname)

        length = norm_num(r.get("Size / Specification"))
        tape = clean(r.get("Tape Shade"))
        tape = None if tape == "—" else tape
        puller = clean(r.get("Puller"))
        puller = None if puller == "—" else puller
        if length:
            attrs["length"] = length
        if tape:
            attrs["tape_shade"] = tape
        if puller:
            attrs["puller"] = puller

        size = " · ".join(p for p in (length, tape) if p) or None
        if size and len(size) > 40:          # material_lot.size is String(40)
            size = size[:40]
            attrs["size_truncated"] = True

        out.append(LotRow(
            category=MaterialCategory.ACCESSORY.value,
            subtype=MaterialSubtype.ZIP.value,
            article=article, colour=clean(r.get("Colour")),
            thickness=None, size=size,
            on_hand=on_hand, attributes=attrs,
            source=f"{fname} row {r['__row__']}",
        ))
    return out


ADAPTERS = {
    LININGS: adapt_linings,
    RIBS: adapt_ribs,
    THREADS: adapt_threads,
    ZIPPERS: adapt_zippers,
}


# ── load ────────────────────────────────────────────────────────────────────
def collect(base: Path, only: str | None) -> list[LotRow]:
    lots: list[LotRow] = []
    for fname, adapter in ADAPTERS.items():
        if only and only.lower() not in fname.lower():
            continue
        path = base / fname
        if not path.exists():
            print(f"  ! {fname}: not found at {path} — skipped")
            continue
        rows = read_table(path)
        parsed = adapter(rows, fname)
        print(f"  + {fname}: {len(rows)} sheet rows -> {len(parsed)} lot rows")
        lots.extend(parsed)
    return lots


def load(base: Path, dry: bool, only: str | None) -> None:
    print("Phase 1 — read workbooks")
    rows = collect(base, only)
    if not rows:
        sys.exit("No rows parsed — nothing to do.")

    inserted = merged = skipped_dupe = 0
    mismatches: list[str] = []
    incomplete: list[str] = []
    pending_barcodes: list[tuple] = []
    batch_lots: dict[tuple, MaterialLot] = {}

    session = SessionLocal()
    try:
        print("\nPhase 2 — lots")
        for row in rows:
            if row.attributes.get("uom_mismatch"):
                mismatches.append(
                    f"{row.source}: {row.attributes.get('source_qty')} "
                    f"{row.attributes.get('source_unit')} stored as "
                    f"{uom_for(row.category, row.subtype)}")
            if row.attributes.get("spec_incomplete"):
                incomplete.append(
                    f"{row.source}: missing "
                    f"{', '.join(row.attributes['spec_incomplete'])}")

            key = spec_key(row.category, row.subtype, row.article, row.colour,
                           row.thickness, row.size)

            # Same spec twice in this run -> top up the unflushed row in memory.
            if (seen := batch_lots.get(key)) is not None:
                seen.on_hand += row.on_hand
                folded = list((seen.attributes or {}).get("merged_sources", []))
                folded.append({"source": row.source,
                               "qty": float(row.on_hand),
                               "unit": row.attributes.get("source_unit")})
                # Reassigned, not mutated: the column is plain JSON, not
                # MutableDict, so an in-place edit is never seen as dirty.
                seen.attributes = {**(seen.attributes or {}),
                                   "merged_sources": folded}
                merged += 1
                continue

            # Committed by an earlier run -> SKIP. Re-import must not inflate.
            if find_existing_lot(session, key) is not None:
                skipped_dupe += 1
                continue

            lot = MaterialLot(
                category=row.category, subtype=row.subtype, article=row.article,
                colour=row.colour, thickness=row.thickness, size=row.size,
                uom=uom_for(row.category, row.subtype),
                on_hand=row.on_hand, supplier_id=None,
                attributes=row.attributes, is_active=True,
            )
            if not dry:
                session.add(lot)
            batch_lots[key] = lot
            pending_barcodes.append((lot, row.attributes.get("description")))
            inserted += 1

        print("Phase 3 — lot barcodes")
        if not dry:
            # Lots must reach the DB before their barcodes: material_lot_id is a
            # non-deferrable FK and UUIDMixin.id is a Python-side default only
            # filled at flush. Same ordering rule as premint.py.
            session.flush()
        minted = mint_lot_barcodes(session, pending_barcodes, dry)

        if dry:
            session.rollback()
            print("\n[DRY RUN] rolled back — nothing written.")
        else:
            session.commit()
            print("\nCommitted.")
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    print(f"\nSummary: inserted={inserted}  barcodes_minted={minted}  "
          f"merged_in_batch={merged}  skipped_existing={skipped_dupe}  "
          f"total_rows={len(rows)}")
    accounted = inserted + merged + skipped_dupe
    if accounted != len(rows):
        print(f"  ! {len(rows) - accounted} row(s) unaccounted for — investigate.")

    if mismatches:
        print(f"\nUNIT MISMATCHES ({len(mismatches)}) — quantity kept, unit is the "
              f"derived one; no conversion factor exists in the sheet:")
        for m in mismatches:
            print(f"  · {m}")
    if incomplete:
        print(f"\nINCOMPLETE SPEC ({len(incomplete)}) — loaded with the field NULL, "
              f"flagged in attributes.spec_incomplete for backfill:")
        for m in incomplete:
            print(f"  · {m}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Load the four trim workbooks into material_lot (idempotent).")
    ap.add_argument("--dir", default=DEFAULT_DIR,
                    help="directory holding the four .xlsx files")
    ap.add_argument("--only", default=None,
                    help="load just one workbook (substring match, e.g. THREADS)")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report; write nothing")
    args = ap.parse_args()

    base = Path(args.dir)
    if not base.is_dir():
        sys.exit(f"Not a directory: {base}")

    print(f"Loading trims from {base.resolve()}  (dry_run={args.dry_run})")
    load(base, dry=args.dry_run, only=args.only)


if __name__ == "__main__":
    main()
