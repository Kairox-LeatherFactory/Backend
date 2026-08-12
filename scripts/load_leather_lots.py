"""
================================================================================
scripts/load_leather_lots.py — one-shot idempotent loader for clean_leather_lots
================================================================================
Loads clean_leather_lots_finalZZ.csv into material_supplier + material_lot.

WHY SYNC, NOT ASYNC:
    Mirrors premint.py / the existing seed discipline. Seed scripts use the sync
    engine (settings.database_url, psycopg2) — SessionLocal — not the async API
    path. No event loop, no asyncpg, runs standalone.

WHY TWO PHASES:
    material_lot.supplier_id is an FK → material_supplier.id. Suppliers MUST exist
    before lots or every lot lands with a null supplier link (the documented
    materials failure mode). Phase 1 upserts suppliers, Phase 2 inserts lots and
    resolves supplier_id from the in-memory name→id map.

IDEMPOTENCY:
    material_lot has no natural unique key in the model, so this script defines a
    load key: (category, subtype, article, colour, supplier_id, uom). Check-then-
    insert on that key. Re-running does NOT duplicate. material_supplier is keyed
    on name. Safe to run any number of times.

CSV-vs-MODEL FIXES APPLIED (do not remove — the raw CSV is dirty):
    • category  "ACCESSORIES" → "ACCESSORY"   (enum is singular; else filters miss)
    • subtype   free-text supplier strings ("THREADS (S.N. & M.V.P.N.K)") →
                MaterialSubtype enum (THREAD/ZIP/RIBS/PLAIN_LINING). Raw strings
                are 26 chars and would overflow subtype String(20) on Postgres.
    • uom       DERIVED via uom_for(category, subtype) — never trust the CSV uom
                column ("CONES" is not a known uom; casing is wrong). Keeps stock
                arithmetic consistent with the rest of materials.
    • price cols  "Unit Price"/"Value (PCS x Price)" are row-shifted junk in the
                source and are IGNORED. Real price/pcs come from attributes JSON.
    • blank rows (no category / no uom) are skipped.

RUN:
    python -m scripts.load_leather_lots            # commit
    python -m scripts.load_leather_lots --dry-run  # validate + report, no write
    python -m scripts.load_leather_lots --csv /path/to/file.csv
================================================================================
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings          # noqa: F401  (kept for parity/URL logs)
from app.core.database import SessionLocal
from app.core.enums import (BarcodeStatus, BarcodeType, MaterialCategory,
                            MaterialSubtype, uom_for)
from app.modules.barcode.models import (BarcodeRegistry, MaterialLot,
                                        MaterialSupplier)
# Imported, NOT re-declared: these two tables are the vocabulary the API's
# create_lot uses to build a lot barcode. A copy here would drift the moment
# someone adds a subtype, and the floor would print labels in a different
# format from the ones the app mints. Single source of truth.
from app.modules.materials.service import _CAPTION_FIELDS, _LOT_BARCODE_TYPE

# ── REGISTER EVERY MODEL MODULE (CLAUDE.md §11) ─────────────────────────────
# This block is NOT decorative and must not be trimmed to "just what we query".
#
# core.models.Document declares its relationship by NAME — relationship(
# "Submission", ...) — and SQLAlchemy resolves that string LAZILY, at first
# mapper configuration, which is triggered by the first ORM query. Importing
# only barcode.models registers Document but NOT procurement.Submission, so the
# very first select() in upsert_suppliers died with:
#
#     InvalidRequestError: When initializing mapper Mapper[Document(document)],
#     expression 'Submission' failed to locate a name ('Submission').
#
# main.py imports all of these at startup, so the API never sees this; a
# standalone script bypasses main.py and must do it itself. Mapper configuration
# cascades over the WHOLE registry, so importing only procurement would just
# move the failure to the next cross-module name reference — import them all.
from app.modules.users import models as _users              # noqa: F401,E402
from app.modules.employees import models as _employees      # noqa: F401,E402
from app.modules.clients import models as _clients          # noqa: F401,E402
from app.modules.production import models as _production    # noqa: F401,E402
from app.modules.wages import models as _wages              # noqa: F401,E402
from app.modules.attendance import models as _attendance    # noqa: F401,E402
from app.core import models as _core_models                 # noqa: F401,E402
from app.modules.procurement import models as _procurement  # noqa: F401,E402
from app.modules.bom import models as _bom                  # noqa: F401,E402
from app.modules.inventory import models as _inventory      # noqa: F401,E402
from app.modules.supplier_po import models as _supplier_po  # noqa: F401,E402

DEFAULT_CSV = r"data\clean_leather_lots.csv"

# ── CSV → enum remaps ───────────────────────────────────────────────────────
CATEGORY_REMAP = {
    "LEATHER": MaterialCategory.LEATHER.value,
    "LINING": MaterialCategory.LINING.value,
    "ACCESSORIES": MaterialCategory.ACCESSORY.value,   # <- singular in the enum
    "ACCESSORY": MaterialCategory.ACCESSORY.value,
}

# The CSV crams a supplier name into `subtype`. Map the recognisable token to the
# real MaterialSubtype enum. Anything unmatched under LINING/ACCESSORY falls back
# below; LEATHER carries no subtype (None).
SUBTYPE_TOKEN_MAP = {
    "THREAD": MaterialSubtype.THREAD.value,
    "ZIP": MaterialSubtype.ZIP.value,
    "RIBS": MaterialSubtype.RIBS.value,
    "KNIT": MaterialSubtype.KNIT.value,
    "TAFFTA": MaterialSubtype.PLAIN_LINING.value,   # taffeta = plain lining
    "COTTON": MaterialSubtype.PLAIN_LINING.value,
    "BUTTON": MaterialSubtype.BUTTON.value,
}


def resolve_subtype(category: str, raw_subtype: str) -> str | None:
    """LEATHER → None. LINING/ACCESSORY → enum value matched by token, else a
    sane category default (PLAIN_LINING / OTHER)."""
    if category == MaterialCategory.LEATHER.value:
        return None
    s = (raw_subtype or "").upper()
    for token, value in SUBTYPE_TOKEN_MAP.items():
        if token in s:
            return value
    if category == MaterialCategory.LINING.value:
        return MaterialSubtype.PLAIN_LINING.value
    return MaterialSubtype.OTHER.value


def is_header_row(r: dict) -> bool:
    """The source workbook restates its header mid-sheet, so a row arrives with
    the literal column names as data: article='ARTICLE', colour='COLOUR',
    supplier='SUPLIER' (sic), blank qty. It is a non-blank LEATHER row and
    passes every other validity check.

    BOTH phases must agree on this. Phase 1 scans the raw rows for supplier
    names and ran BEFORE phase 2's filtering, so the header's 'SUPLIER' was
    being created as a real material_supplier — a junk entry in every supplier
    dropdown, pointed at by no lot. Match on article+colour, not the typo.
    """
    article = (r.get("article") or "").strip().upper()
    colour = (r.get("colour") or "").strip().upper()
    return article == "ARTICLE" and colour == "COLOUR"


def clean(v: str | None) -> str | None:
    """'' → None, strip whitespace. Prevents empty-string rows defeating the
    nullable columns and the idempotency key."""
    if v is None:
        return None
    v = v.strip()
    return v or None


def to_decimal(v: str | None) -> Decimal:
    v = clean(v)
    if v is None:
        return Decimal("0")
    try:
        return Decimal(v.replace(",", ""))
    except (InvalidOperation, ValueError):
        return Decimal("0")


def parse_attributes(raw: str | None, supplier_name: str | None) -> dict:
    """CSV `attributes` is a JSON string ({'pcs','buyer_ref','unit_price'}). Keep
    it, and stamp the source supplier name for traceability."""
    attrs: dict = {}
    raw = clean(raw)
    if raw:
        try:
            attrs = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            attrs = {"raw": raw}
    if supplier_name:
        attrs.setdefault("source_supplier_name", supplier_name)
    return attrs


# ── phase 1: suppliers ──────────────────────────────────────────────────────
def upsert_suppliers(session, rows: list[dict], dry: bool) -> dict[str, object]:
    """Distinct non-blank supplier_name → material_supplier row. Returns
    {name: supplier_obj_or_id}. Idempotent on name."""
    names = {
        clean(r.get("supplier_name"))
        for r in rows
        if clean(r.get("supplier_name")) and not is_header_row(r)
    }
    name_to_supplier: dict[str, object] = {}
    for name in sorted(n for n in names if n):
        existing = session.execute(
            select(MaterialSupplier).where(MaterialSupplier.name == name)
        ).scalar_one_or_none()
        if existing:
            name_to_supplier[name] = existing
            continue
        supplier = MaterialSupplier(name=name, articles=None, is_active=True)
        if not dry:
            session.add(supplier)
            session.flush()          # get PK for the lot FK, no commit yet
        name_to_supplier[name] = supplier
        print(f"  + supplier: {name}")
    return name_to_supplier


# ── phase 2: lots ───────────────────────────────────────────────────────────
SPEC_KEY = ("category", "subtype", "article", "colour", "thickness", "size")


def spec_key(category, subtype, article, colour, thickness, size) -> tuple:
    """THE identity of a material — and it MUST equal the DB constraint.

    The DB enforces `uq_material_lot_spec` on exactly
    (category, subtype, article, colour, thickness, size).

    The first version of this key was (category, subtype, article, colour,
    supplier_id, uom) and that was wrong in BOTH directions:

      • it INCLUDED supplier_id — but a lot is WHAT the material is, not who
        sold it (materials/service.py:123 "ONE LOT PER MATERIAL SPEC"). The same
        GOAT SUEDE / PINE GREEN from two tanneries is ONE lot, so keying on
        supplier let a second row through and the DB rejected it.
      • it OMITTED thickness and size — two genuinely different materials
        (0.6mm vs 0.9mm) would have been wrongly judged the same.

    Keep this function and the migration's constraint in lockstep.
    """
    return (category, subtype, article, colour, thickness, size)


def _merge_source(lot: MaterialLot, attributes: dict, supplier_name: str | None) -> None:
    """Fold a duplicate CSV row's provenance into the lot it tops up.

    on_hand is summed by the caller; this keeps the AUDIT TRAIL. The first row
    wins for supplier_id (a lot has one FK but a material can come from several
    tanneries), so without this the other deliveries would vanish silently and
    nobody could reconcile the lot's quantity against the stock sheet.
    `merged_sources` records each folded-in delivery's supplier and pcs.

    Reassigns `attributes` rather than mutating in place: the column is plain
    JSON, not MutableDict, so an in-place edit is not seen as dirty and would
    never be written.
    """
    merged = list((lot.attributes or {}).get("merged_sources", []))
    merged.append({k: v for k, v in
                   {"supplier": supplier_name,
                    "pcs": attributes.get("pcs"),
                    "unit_price": attributes.get("unit_price")}.items()
                   if v is not None})
    lot.attributes = {**(lot.attributes or {}), "merged_sources": merged}


def find_existing_lot(session, key: tuple) -> MaterialLot | None:
    """The lot already committed for this spec, or None. NULL-safe: `== None`
    does not match NULL in SQL, so each nullable column needs is_()."""
    stmt = select(MaterialLot)
    for col_name, value in zip(SPEC_KEY, key):
        col = getattr(MaterialLot, col_name)
        stmt = stmt.where(col == value) if value is not None else stmt.where(col.is_(None))
    return session.execute(stmt.limit(1)).scalar_one_or_none()


# ── phase 3: lot barcodes ───────────────────────────────────────────────────
# WHY THIS PHASE EXISTS
#     A lot with no barcode is HALF-CREATED. CLAUDE.md §5: "Creating a lot mints
#     a child barcode — that is how material formally enters inventory", and §6
#     makes BarcodeRegistry the single front door every scan resolves through.
#     The API's create_lot mints one (materials/service.py:158); this loader
#     inserts MaterialLot rows directly and so bypassed it. Without this phase
#     the 117 lots would be filterable and cuttable but NOT scannable, and
#     `barcodes_by_lot` would return null for every row in the cut screen's lot
#     picker — a lot the floor cannot scan is not in inventory.
#
# WHY THE COUNTER IS CACHED IN MEMORY
#     BarcodeRepository._next_code re-reads the max code per mint. That is right
#     for one interactive create; here it would be one SELECT per lot, and — far
#     worse — every read inside this single uncommitted transaction would return
#     the SAME max, so all 117 lots would collide on one code. Seed each prefix's
#     counter ONCE from the DB, then increment in memory. Same code FORMAT as
#     _next_code (prefix + 6-digit zero-padded tail), so the two interleave.
_LOT_PREFIX = {"LEATHER_LOT": "LOT-LEA", "LINING_LOT": "LOT-LIN",
               "ACCESSORY_LOT": "LOT-ACC"}


def _seed_counter(session, prefix: str) -> int:
    """Highest existing numeric tail for this prefix, or 0. Mirrors
    BarcodeRepository._next_code: the tail is fixed-width zero-padded, so
    lexicographic DESC == numeric DESC and one row is enough."""
    top = session.execute(
        select(BarcodeRegistry.code)
        .where(BarcodeRegistry.code.like(f"{prefix}-%"))
        .order_by(BarcodeRegistry.code.desc())
        .limit(1)
    ).scalar_one_or_none()
    if top:
        tail = top[len(prefix) + 1:]
        if tail.isdigit():
            return int(tail)
    return 0


def lot_caption(cat, subtype, article, colour, thickness, size, description,
                qty, uom) -> str:
    """Byte-identical to MaterialService._caption, driven by the SAME imported
    _CAPTION_FIELDS table — only the input shape differs (raw values here, a
    pydantic body there)."""
    key = (cat, None) if cat == MaterialCategory.LEATHER.value else \
        (cat, subtype or MaterialSubtype.PLAIN_LINING.value)
    fields = _CAPTION_FIELDS.get(key, ["article", "colour", "qty"])
    values = {
        "article": article, "colour": colour, "thickness": thickness,
        "size": size, "description": description, "qty": f"{qty} {uom}",
    }
    return " · ".join(str(values[f]) for f in fields if values.get(f))


def mint_lot_barcodes(session, pending: list[tuple], dry: bool) -> int:
    """One ACTIVE BarcodeRegistry row per newly inserted lot. `pending` is
    [(lot, description)] built in phase 2. Caller commits."""
    counters: dict[str, int] = {}
    minted = 0
    for lot, description in pending:
        btype = _LOT_BARCODE_TYPE[lot.category]
        prefix = _LOT_PREFIX[btype.value]
        if prefix not in counters:
            counters[prefix] = _seed_counter(session, prefix)
        counters[prefix] += 1
        code = f"{prefix}-{counters[prefix]:06d}"

        caption = lot_caption(lot.category, lot.subtype, lot.article, lot.colour,
                              lot.thickness, lot.size, description,
                              lot.on_hand, lot.uom)
        if not dry:
            session.add(BarcodeRegistry(
                code=code, type=btype.value,
                status=BarcodeStatus.ACTIVE.value,
                material_lot_id=lot.id, caption=caption))
        minted += 1
    return minted


def load(csv_path: Path, dry: bool) -> None:
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    inserted = skipped_junk = skipped_dupe = minted = merged = 0
    pending_barcodes: list[tuple] = []      # [(lot, description)] for phase 3
    batch_lots: dict[tuple, MaterialLot] = {}   # spec_key → lot built this run
    session = SessionLocal()
    try:
        print("Phase 1 — suppliers")
        suppliers = upsert_suppliers(session, rows, dry)

        print("Phase 2 — lots")
        for i, r in enumerate(rows, start=1):
            raw_cat = clean(r.get("category"))
            if not raw_cat:
                skipped_junk += 1
                continue
            category = CATEGORY_REMAP.get(raw_cat.upper())
            if category is None:
                print(f"  ! row {i}: unknown category {raw_cat!r} — skipped")
                skipped_junk += 1
                continue

            subtype = resolve_subtype(category, r.get("subtype", ""))
            article = clean(r.get("article"))
            if not article:
                print(f"  ! row {i}: no article — skipped")
                skipped_junk += 1
                continue

            if is_header_row(r):     # see is_header_row — shared with phase 1
                print(f"  ! row {i}: repeated header row — skipped")
                skipped_junk += 1
                continue

            colour = clean(r.get("colour"))
            thickness = clean(r.get("thickness"))
            size = clean(r.get("size"))
            uom = uom_for(category, subtype)          # DERIVED, not from CSV
            on_hand = to_decimal(r.get("on_hand"))

            supplier_name = clean(r.get("supplier_name"))
            supplier_obj = suppliers.get(supplier_name) if supplier_name else None
            supplier_id = getattr(supplier_obj, "id", None) if supplier_obj else None

            attributes = parse_attributes(r.get("attributes"), supplier_name)

            key = spec_key(category, subtype, article, colour, thickness, size)

            # ── SAME SPEC SEEN EARLIER IN THIS CSV → TOP UP, DON'T INSERT ─────
            # 21 of the 117 rows are repeat purchases of a material already
            # listed (GOAT SUEDE / D.BLUE appears 4 times — four deliveries).
            # Option A says a lot is WHAT the material is, so a repeat is a
            # top-up of on_hand, exactly what POST /materials/receive does.
            #
            # This check MUST be in memory. The rows added earlier in this loop
            # are still unflushed, so a DB query cannot see them — which is
            # precisely how the first run got two GOAT SUEDE / PINE GREEN rows
            # past the check and died on uq_material_lot_spec at flush.
            if (seen := batch_lots.get(key)) is not None:
                seen.on_hand += on_hand
                _merge_source(seen, attributes, supplier_name)
                merged += 1
                continue

            # ── ALREADY COMMITTED FROM A PREVIOUS RUN → SKIP (idempotency) ────
            # Deliberately a skip, NOT a top-up: re-running the same CSV must not
            # keep inflating stock. Re-import is a no-op; a genuine new delivery
            # goes through POST /materials/receive.
            if find_existing_lot(session, key) is not None:
                skipped_dupe += 1
                continue

            lot = MaterialLot(
                category=category, subtype=subtype, article=article,
                colour=colour, thickness=thickness, size=size, uom=uom,
                on_hand=on_hand, supplier_id=supplier_id,
                attributes=attributes, is_active=True,
            )
            if not dry:
                session.add(lot)
            batch_lots[key] = lot
            # `description` is an ACCESSORY/OTHER caption field and lives only in
            # the attributes JSON, so it is carried alongside rather than re-read.
            pending_barcodes.append((lot, attributes.get("description")))
            inserted += 1

        print("Phase 3 — lot barcodes")
        if not dry:
            # Lots must hit the DB before their barcodes: barcode_registry
            # .material_lot_id → material_lot.id is a non-deferrable FK and
            # UUIDMixin.id is a PYTHON-side default only filled at flush, so
            # lot.id is None until this runs. Same ordering rule as premint.py.
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
          f"merged_into_existing={merged}  skipped_dupe={skipped_dupe}  "
          f"skipped_junk={skipped_junk}  total_rows={len(rows)}")
    accounted = inserted + merged + skipped_dupe + skipped_junk
    if accounted != len(rows):
        print(f"  ! {len(rows) - accounted} row(s) unaccounted for — investigate.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Load leather lots CSV (idempotent).")
    ap.add_argument("--csv", default=DEFAULT_CSV, help="path to the CSV file")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate and report; write nothing")
    args = ap.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    print(f"Loading {csv_path}  (dry_run={args.dry_run})")
    load(csv_path, dry=args.dry_run)


if __name__ == "__main__":
    main()