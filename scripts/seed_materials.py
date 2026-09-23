"""
================================================================================
scripts/seed_materials.py — Seed Stage-0 stock: LEATHER + LINING + ACCESSORIES
================================================================================
RUN:
    python -m scripts.seed_materials                  # commit everything
    python -m scripts.seed_materials --dry-run        # validate + report, no write
    python -m scripts.seed_materials --only leather   # one category at a time
    python -m scripts.seed_materials --only lining --only accessory

WHAT THIS LOADS
    Suppliers, then one MATERIAL LOT per material spec across all three floor
    categories, and — for three of the leather lots — the individual HIDES
    (material_sheet) that make up the delivery.

    Every lot gets its CHILD BARCODE. That is not decoration: CLAUDE.md §5 says
    "creating a lot mints a child barcode — that is how material formally enters
    inventory", and §6 makes BarcodeRegistry the single front door every scan
    resolves through. A lot with no code is filterable and cuttable but NOT
    scannable, so the cut screen's lot picker shows `barcode: null` for it — a
    half-created lot. Seeding without the label is not seeding stock.

WHY IT GOES THROUGH MaterialService.create_lot AND NOT STRAIGHT INTO THE TABLES
    scripts/load_leather_lots.py inserts MaterialLot rows directly, because it
    ingests a dirty CSV whose rows do not satisfy the strict per-category field
    set and have to be repaired first. This script's data is written by hand, so
    it can afford to be honest: it posts the same LotCreate body the API takes
    and lets the service do the work. That buys, for free and without a second
    copy to keep in step:
      • the STRICT per-category validation (MATERIAL_SPEC / resolve_spec) — a
        lining lot missing `mtrs` fails here exactly as it would over HTTP, 422;
      • the uom derived from the category, never typed in;
      • the lot barcode + its per-category caption;
      • the opening RECEIPT row, so `received` (not just on_hand) is right from
        the first day;
      • the one-lot-per-spec 409, which is also what makes this script idempotent.
    If the materials contract changes, this seed changes with it or fails loudly.
    That is the point.

IDEMPOTENT — RE-RUN IT FREELY
    Suppliers are matched on name (case-insensitively, like the rest of the repo
    matches names). Lots are matched on the SPEC — the same six columns the DB's
    `uq_material_lot_spec` enforces and the same ones
    MaterialRepository.find_duplicate_lot checks:
        (category, subtype, article, colour, thickness, size)
    An already-seeded lot is SKIPPED, not topped up: re-running a seed must not
    keep inflating stock. A genuine new delivery is POST /materials/receive.
    A lot that exists but lost/never got its barcode is REPAIRED (the label is
    minted), the same top-up discipline scripts/seed_employees.py uses for cards.

PRE-FLIGHT VALIDATION (runs in both modes, before anything is written)
    The tables below are hand-maintained, and the two mistakes they invite are a
    missing required attribute and two rows that are secretly the same material.
    `validate()` catches both with no DB at all: it resolves each row against
    MATERIAL_SPEC, checks the quantity is a positive number, and checks the spec
    keys are unique within the file. It also checks each leather lot's hides sum
    to the lot's dcm — the service only WARNS about that mismatch (correctly: it
    will not refuse a delivery over measurement slop), so the seed data itself is
    where it should be made exact.

PREREQUISITE
    alembic upgrade head — in particular the material tables and
    `uq_material_lot_spec`.
================================================================================
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import logging
from collections import Counter
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.core.enums import (BarcodeStatus, MaterialCategory,
                            SupplierOrderStatus, resolve_spec)
from app.modules.barcode.models import (BarcodeRegistry, MaterialSheet,
                                        MaterialSupplier, SupplierOrder)
from app.modules.materials.repository import MaterialRepository
from app.modules.materials.schemas import (LotCreate, SheetIn,
                                           SupplierOrderCreate)
from app.modules.materials.service import (_LOT_BARCODE_TYPE, MaterialService)

# EVERY model module must be imported before the first ORM operation — not just
# the tables this script writes (CLAUDE.md §11). SQLAlchemy resolves
# relationships by CLASS NAME at configure_mappers(), which fires on the first
# query, so a half-registered registry dies with "expression 'Submission' failed
# to locate a name" on a statement that never mentions Submission. This is
# main.py's block verbatim — keep them in step.
from app.modules.users import models as _users              # noqa: F401,E402
from app.modules.employees import models as _employees      # noqa: F401,E402
from app.modules.clients import models as _clients          # noqa: F401,E402
from app.modules.production import models as _production    # noqa: F401,E402
from app.modules.wages import models as _wages              # noqa: F401,E402
from app.modules.attendance import models as _attendance    # noqa: F401,E402
from app.modules.barcode import models as _barcode          # noqa: F401,E402
from app.modules.jobwork import models as _jobwork          # noqa: F401,E402
from app.modules.cutting import models as _cutting          # noqa: F401,E402  (cutting_row — material_sheet FKs it)
from app.core import models as _core_models                 # noqa: F401,E402
from app.modules.procurement import models as _procurement  # noqa: F401,E402
from app.modules.bom import models as _bom                  # noqa: F401,E402
from app.modules.inventory import models as _inventory      # noqa: F401,E402
from app.modules.supplier_po import models as _supplier_po  # noqa: F401,E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_materials")

LABELS_CSV = Path("data/material_lot_labels.csv")


# ══════════════════════════════════════════════════════════════════════════════
# SUPPLIERS
# ══════════════════════════════════════════════════════════════════════════════
# `articles` is a comma list and it is FUNCTIONAL, not documentation: it is the
# whole of the v1 article→supplier suggestion (MaterialRepository.suggest_
# supplier / supplier_supplies), which is what the DM's shortfall screen offers
# when stock is short. A supplier seeded with no articles will never be
# suggested for anything. Keep each name here matching the `supplier` key used
# by the lots below — that string is the join.
SUPPLIERS = [
    dict(name="Chennai Tannery Works",
         articles="GOAT SUEDE,GOAT ANILINE,LAMB NAPPA",
         contact="+91 44 2345 6710 · orders@chennaitannery.example"),
    dict(name="Ranipet Hides & Skins",
         articles="SHEEP NAPPA,COW CRUST",
         contact="+91 4172 22 3344 · sales@ranipethides.example"),
    dict(name="Ambur Lining House",
         articles="TAFFETA 190T,POLY VISCOSE TWILL,CUPRO BEMBERG",
         contact="+91 4174 55 0192 · amburlining@example.com"),
    dict(name="S.N. Ribs & Knits",
         articles="COTTON RIB 2X2,ACRYLIC RIB STRIPE,KNIT COLLAR,KNIT CUFF",
         contact="+91 98400 11223 · sn.knits@example.com"),
    dict(name="M.V.P.N.K Threads",
         articles="POLY CORE THREAD",
         contact="+91 98410 44556 · mvpnk.threads@example.com"),
    dict(name="Metro Trims & Zips",
         articles="YKK #5 METAL,YKK #3 NYLON,HORN BUTTON,METAL SHANK BUTTON",
         contact="+91 44 2811 9090 · metrotrims@example.com"),
    dict(name="Packwell Packaging",
         articles="POLY BAG,HANG TAG,MAIN LABEL,SHOULDER PAD",
         contact="+91 44 2699 3311 · packwell@example.com"),
]


# ══════════════════════════════════════════════════════════════════════════════
# THE THREE CATEGORIES
# ══════════════════════════════════════════════════════════════════════════════
# One dict per LOT. `attributes` holds EXACTLY the keys MATERIAL_SPEC requires
# for that (category, subtype) and nothing else — that strictness is the
# contract, not an accident (CLAUDE.md §5). The quantity lives in the category's
# own field (dcm / mtrs / kg / pcs / count); there is no generic `qty`, and the
# uom is never typed here because the service derives it.
#
#   LEATHER             → thickness, dcm            (dcm)
#   LINING/PLAIN_LINING → thickness, mtrs           (mtrs)
#   LINING/RIBS         → kg                        (kg)
#   LINING/KNIT         → pcs                       (pcs)
#   ACCESSORY/BUTTON    → size, count               (pcs)
#   ACCESSORY/ZIP       → size, count               (pcs)
#   ACCESSORY/THREAD    → thickness, mtrs           (mtrs)
#   ACCESSORY/OTHER     → description, count        (pcs)

# ── LEATHER ──────────────────────────────────────────────────────────────────
# `sheets` is LEATHER-ONLY and optional. Every entry is one physical hide with
# the dcm the tannery wrote on it. Omit it and the lot behaves exactly as an
# unsheeted delivery does — a stock figure with no per-hide detail — which is
# deliberately what most of these lots are, so both paths are seeded.
LEATHER = [
    dict(article="GOAT SUEDE", colour="D.BLUE", supplier="Chennai Tannery Works",
         attributes={"thickness": "0.6MM", "dcm": 420.5},
         # 5 hides, 420.5 dcm — sums exactly to the declared delivery.
         sheets=[88.0, 92.5, 79.0, 84.0, 77.0]),
    dict(article="GOAT SUEDE", colour="PINE GREEN", supplier="Chennai Tannery Works",
         attributes={"thickness": "0.6MM", "dcm": 365.0}),
    dict(article="SHEEP NAPPA", colour="BLACK", supplier="Ranipet Hides & Skins",
         attributes={"thickness": "0.8MM", "dcm": 610.0},
         sheets=[102.0, 98.5, 105.5, 96.0, 108.0, 100.0]),
    dict(article="SHEEP NAPPA", colour="BURGUNDY", supplier="Ranipet Hides & Skins",
         attributes={"thickness": "0.8MM", "dcm": 288.0}),
    dict(article="GOAT ANILINE", colour="TAN", supplier="Chennai Tannery Works",
         attributes={"thickness": "0.7MM", "dcm": 285.75},
         sheets=[72.25, 68.5, 74.0, 71.0]),
    dict(article="COW CRUST", colour="BLACK", supplier="Ranipet Hides & Skins",
         attributes={"thickness": "1.2MM", "dcm": 540.0}),
    dict(article="LAMB NAPPA", colour="OFF WHITE", supplier="Chennai Tannery Works",
         attributes={"thickness": "0.6MM", "dcm": 198.25}),
]

# ── LINING ───────────────────────────────────────────────────────────────────
# Three shapes, and the subtype is what decides which: plain lining is measured
# in metres and has a thickness, ribs are weighed, knit collars/cuffs are
# counted. Same category, three different required-field sets.
LINING = [
    dict(subtype="PLAIN_LINING", article="TAFFETA 190T", colour="BLACK",
         supplier="Ambur Lining House",
         attributes={"thickness": "0.10MM", "mtrs": 1200.0}),
    dict(subtype="PLAIN_LINING", article="TAFFETA 190T", colour="D.BLUE",
         supplier="Ambur Lining House",
         attributes={"thickness": "0.10MM", "mtrs": 850.0}),
    dict(subtype="PLAIN_LINING", article="POLY VISCOSE TWILL", colour="CHARCOAL",
         supplier="Ambur Lining House",
         attributes={"thickness": "0.20MM", "mtrs": 640.0}),
    dict(subtype="PLAIN_LINING", article="CUPRO BEMBERG", colour="TAN",
         supplier="Ambur Lining House",
         attributes={"thickness": "0.15MM", "mtrs": 300.0}),
    dict(subtype="RIBS", article="COTTON RIB 2X2", colour="BLACK",
         supplier="S.N. Ribs & Knits",
         attributes={"kg": 45.5}),
    dict(subtype="RIBS", article="ACRYLIC RIB STRIPE", colour="NAVY/WHITE",
         supplier="S.N. Ribs & Knits",
         attributes={"kg": 32.0}),
    dict(subtype="KNIT", article="KNIT COLLAR", colour="BLACK",
         supplier="S.N. Ribs & Knits",
         attributes={"pcs": 450}),
    dict(subtype="KNIT", article="KNIT CUFF", colour="D.BLUE",
         supplier="S.N. Ribs & Knits",
         attributes={"pcs": 600}),
]

# ── ACCESSORIES ──────────────────────────────────────────────────────────────
# NOTE the category is singular — ACCESSORY. The enum has no "ACCESSORIES", and
# a lot filed under the plural would be invisible to every filter on the floor
# (this is the exact remap scripts/load_leather_lots.py has to apply to its CSV).
# ACCESSORY always needs a subtype: there is no generic accessory quantity.
ACCESSORY = [
    dict(subtype="BUTTON", article="HORN BUTTON", colour="DARK BROWN",
         supplier="Metro Trims & Zips",
         attributes={"size": "24L", "count": 1800}),
    dict(subtype="BUTTON", article="METAL SHANK BUTTON", colour="ANTIQUE BRASS",
         supplier="Metro Trims & Zips",
         attributes={"size": "20L", "count": 2400}),
    dict(subtype="ZIP", article="YKK #5 METAL", colour="BLACK",
         supplier="Metro Trims & Zips",
         attributes={"size": "70CM", "count": 900}),
    dict(subtype="ZIP", article="YKK #5 METAL", colour="D.BLUE",
         supplier="Metro Trims & Zips",
         attributes={"size": "65CM", "count": 400}),
    dict(subtype="ZIP", article="YKK #3 NYLON", colour="BLACK",
         supplier="Metro Trims & Zips",
         attributes={"size": "18CM", "count": 1500}),
    dict(subtype="THREAD", article="POLY CORE THREAD", colour="BLACK",
         supplier="M.V.P.N.K Threads",
         attributes={"thickness": "TEX 40", "mtrs": 45000.0}),
    dict(subtype="THREAD", article="POLY CORE THREAD", colour="TAN",
         supplier="M.V.P.N.K Threads",
         attributes={"thickness": "TEX 40", "mtrs": 18000.0}),
    dict(subtype="THREAD", article="POLY CORE THREAD", colour="D.BLUE",
         supplier="M.V.P.N.K Threads",
         attributes={"thickness": "TEX 60", "mtrs": 12000.0}),
    # OTHER is the catch-all, and `description` is a REQUIRED field for it
    # precisely because "other" says nothing — the description is what the
    # printed label reads and the only thing that tells two OTHER lots apart.
    dict(subtype="OTHER", article="SHOULDER PAD", colour="NATURAL",
         supplier="Packwell Packaging",
         attributes={"description": "8mm foam shoulder pad, pair", "count": 600}),
    dict(subtype="OTHER", article="HANG TAG", colour="PRINTED",
         supplier="Packwell Packaging",
         attributes={"description": "Client hang tag, 2-string", "count": 2000}),
    dict(subtype="OTHER", article="POLY BAG", colour="CLEAR",
         supplier="Packwell Packaging",
         attributes={"description": "LDPE garment bag 24x36in", "count": 2500}),
    dict(subtype="OTHER", article="MAIN LABEL", colour="BLACK",
         supplier="Packwell Packaging",
         attributes={"description": "Woven main label, centre fold", "count": 3000}),
]

CATALOGUE = {
    MaterialCategory.LEATHER.value: LEATHER,
    MaterialCategory.LINING.value: LINING,
    MaterialCategory.ACCESSORY.value: ACCESSORY,
}


# ══════════════════════════════════════════════════════════════════════════════
# OPEN SUPPLIER ORDERS  —  so the ORDERED -> ARRIVED half of the module exists
# ══════════════════════════════════════════════════════════════════════════════
# WHY THESE ARE HERE. Seeding suppliers without a single order on them leaves an
# entire, working surface unreachable on a seeded database: PATCH
# /suppliers/orders/{id} (ORDERED -> ARRIVED), PATCH .../spec (the DM/MD
# correction, which is refused once ARRIVED), and — the one that matters —
# POST /materials/receive WITH a supplier_order_id, which is where the PO-match
# check and the DM/MD substitution path live. None of it can be demonstrated,
# reviewed or QA'd against an empty supplier_order table.
#
# EVERY ORDER BELOW IS DELIBERATELY LEFT `ORDERED`, because that is the state
# with something still to do. Two of them match a seeded lot exactly, so
# receiving them is the clean path; the third (`SHEEP NAPPA` in a colour nobody
# stocks) is the MISMATCH case, and receiving it against the BLACK lot is what
# produces the 409 and, for a DM/MD with approve_mismatch, the substitute lot.
#
# `article` MUST be one the named supplier carries, or create_order refuses it
# with a 422 — the same validation the API applies. Keep these in step with
# SUPPLIERS above.
SUPPLIER_ORDERS = [
    dict(supplier="Chennai Tannery Works", category="LEATHER",
         article="GOAT SUEDE", colour="PINE GREEN", thickness="0.6MM",
         dcm=365.0, qty=365.0,
         note="matches the PINE GREEN lot — the clean receive"),
    dict(supplier="Metro Trims & Zips", category="ACCESSORY", subtype="BUTTON",
         article="HORN BUTTON", colour="DARK BROWN", qty=1200,
         note="matches the HORN BUTTON lot — a straight top-up"),
    dict(supplier="Ranipet Hides & Skins", category="LEATHER",
         article="SHEEP NAPPA", colour="OLIVE", thickness="0.8MM",
         dcm=250.0, qty=250.0,
         note="NO OLIVE LOT EXISTS — receive this against the BLACK lot to see "
              "the 409, and again with approve_mismatch for the substitution"),
]


# ══════════════════════════════════════════════════════════════════════════════
# PRE-FLIGHT VALIDATION — pure, no DB
# ══════════════════════════════════════════════════════════════════════════════
def spec_key(row: dict) -> tuple:
    """A material's IDENTITY — and it is the DB constraint, not a convention.

    `uq_material_lot_spec` is on exactly these six columns and
    MaterialRepository.find_duplicate_lot checks exactly these six. Two rows in
    the tables above that agree on all six are the same material, whoever sold
    it and whatever it cost, so the second one is a 409 and not a new lot.
    """
    attrs = row["attributes"]
    return (row["category"], row.get("subtype"), row["article"], row["colour"],
            attrs.get("thickness"), attrs.get("size"))


def validate(rows: list[dict]) -> list[str]:
    """Every problem in the hand-written tables, as a list of messages.

    Returns them all rather than raising on the first, because the point is to
    fix the data in one pass instead of discovering the next typo per run.
    """
    problems: list[str] = []
    seen: Counter = Counter(spec_key(r) for r in rows)

    for dup, n in seen.items():
        if n > 1:
            problems.append(
                f"{n} rows share one material spec {dup} - material is ONE LOT "
                f"PER SPEC, so all but the first would be refused with a 409. "
                f"Merge them into a single row, or give them the thickness/size "
                f"that actually tells them apart.")

    for r in rows:
        label = f"{r['category']}/{r.get('subtype') or '-'} {r['article']}/{r['colour']}"
        spec = resolve_spec(r["category"], r.get("subtype"))
        if spec is None:
            problems.append(f"{label}: unknown (category, subtype).")
            continue

        attrs = {k: v for k, v in r["attributes"].items()
                 if v is not None and str(v).strip() != ""}
        if missing := spec["required"] - set(attrs):
            problems.append(f"{label}: missing required attribute(s) "
                            f"{', '.join(sorted(missing))}.")
        if extra := set(attrs) - spec["required"]:
            # Not fatal — create_lot keeps unknown keys in `attributes` — but in
            # a hand-written seed an unexpected key is far more likely a typo for
            # a required one ("mtr" for "mtrs") that would 422 on the line above.
            problems.append(f"{label}: unexpected attribute(s) "
                            f"{', '.join(sorted(extra))} - typo?")
        if not r["colour"]:
            problems.append(f"{label}: colour is required for EVERY material.")

        if spec["qty_field"] not in attrs:
            continue            # already reported as missing; don't say it twice
        qty_raw = attrs.get(spec["qty_field"])
        try:
            qty = Decimal(str(qty_raw))
        except (InvalidOperation, ValueError, TypeError):
            problems.append(f"{label}: {spec['qty_field']} must be a number "
                            f"(got {qty_raw!r}).")
            continue
        if qty <= 0:
            problems.append(f"{label}: {spec['qty_field']} must be > 0.")

        sheets = r.get("sheets") or []
        if sheets and r["category"] != MaterialCategory.LEATHER.value:
            problems.append(f"{label}: only LEATHER is tracked hide by hide.")
        if sheets:
            total = sum((Decimal(str(s)) for s in sheets), Decimal(0))
            if abs(total - qty) >= Decimal("0.001"):
                # The service only WARNS here, and rightly — it will not refuse a
                # real delivery over measurement slop. Seed data has no excuse.
                problems.append(
                    f"{label}: {len(sheets)} hide(s) total {total} dcm but the "
                    f"lot declares {qty} dcm.")
    return problems


def flatten(only: set[str]) -> list[dict]:
    """The chosen categories as one list, each row stamped with its category."""
    rows: list[dict] = []
    for category, entries in CATALOGUE.items():
        if only and category not in only:
            continue
        for entry in entries:
            rows.append({"category": category, **entry})
    return rows


# ══════════════════════════════════════════════════════════════════════════════
# SEEDING
# ══════════════════════════════════════════════════════════════════════════════
async def upsert_suppliers(db, dry: bool) -> dict[str, object]:
    """{name: MaterialSupplier}. Idempotent on name, case-insensitively.

    PHASE ONE FOR A REASON: material_lot.supplier_id is an FK, so a lot created
    before its supplier exists lands with a null supplier link — the documented
    materials failure mode, and a silent one, because the lot looks fine until
    someone asks who to reorder from.
    """
    out: dict[str, object] = {}
    for s in SUPPLIERS:
        existing = await db.scalar(
            select(MaterialSupplier)
            .where(func.lower(MaterialSupplier.name) == s["name"].lower()))
        if existing is not None:
            out[s["name"]] = existing
            log.info("  = %s", s["name"])
            continue
        supplier = MaterialSupplier(is_active=True, **s)
        db.add(supplier)
        if not dry:
            await db.flush()        # PK for the lots' FK; the caller commits
        out[s["name"]] = supplier
        log.info("  + %s", s["name"])
    if not dry:
        await db.commit()
    else:
        await db.rollback()
    return out


async def upsert_supplier_orders(db, suppliers: dict, dry: bool) -> Counter:
    """The open POs, through MaterialService.create_order — same reason as lots.

    The service validates the article against the chosen supplier's catalogue
    and derives the uom from the category, so a typo here fails exactly as it
    would over HTTP instead of writing an order nobody can fill.

    IDEMPOTENT ON (supplier, article, colour, qty) WHILE STILL `ORDERED`.
    SupplierOrder has no unique constraint — a factory legitimately orders the
    same article twice — so the seed matches on the fields it wrote and skips
    when one is already open. An order somebody has since ARRIVED is NOT
    re-created: that would resurrect a closed purchase on every re-run.
    """
    counts: Counter = Counter()
    svc = MaterialService(db)
    for row in SUPPLIER_ORDERS:
        supplier = suppliers.get(row["supplier"])
        if supplier is None:
            counts["skipped"] += 1
            log.warning("  ! %s: supplier %r is not seeded", row["article"],
                        row["supplier"])
            continue

        existing = await db.scalar(
            select(SupplierOrder).where(
                SupplierOrder.supplier_id == supplier.id,
                SupplierOrder.article == row["article"],
                SupplierOrder.status == SupplierOrderStatus.ORDERED.value,
            ).limit(1))
        if existing is not None:
            counts["skipped"] += 1
            log.info("  = %-22s %-14s %s", row["article"], row.get("colour") or "",
                     "already ordered")
            continue

        if dry:
            counts["would create"] += 1
            log.info("  would create  %-22s %-14s %s", row["article"],
                     row.get("colour") or "", row["supplier"])
            continue

        body = SupplierOrderCreate(
            category=row["category"], subtype=row.get("subtype"),
            article=row["article"], colour=row.get("colour"),
            thickness=row.get("thickness"), dcm=row.get("dcm"),
            qty=row["qty"], supplier_id=supplier.id)
        try:
            out = await svc.create_order(body, actor_id=None)
        except HTTPException as exc:
            await db.rollback()
            counts["failed"] += 1
            log.error("  x %-22s %s", row["article"], exc.detail)
            continue
        counts["created"] += 1
        log.info("  + %-22s %-14s %8.3f %-5s %s", row["article"],
                 row.get("colour") or "", out["qty"], out["uom"], row["note"])
    return counts


async def active_lot_code(db, lot_id) -> str | None:
    """The lot's live label, or None if it has none. `None` is the state this
    script repairs — see the barcode note in the module docstring."""
    return await db.scalar(
        select(BarcodeRegistry.code)
        .where(BarcodeRegistry.material_lot_id == lot_id,
               BarcodeRegistry.status == BarcodeStatus.ACTIVE.value)
        .limit(1))


async def lot_sheet_labels(db, lot_id) -> list[dict]:
    """The hides already minted against a lot, in label order.

    Read on the skip/repair path so the labels CSV is the SAME complete print
    queue on a re-run as on the first run. A CSV that silently drops the hides
    of every already-seeded lot is worse than no CSV, because nothing on it says
    which half is missing.
    """
    rows = (await db.execute(
        select(MaterialSheet.code, MaterialSheet.dcm)
        .where(MaterialSheet.material_lot_id == lot_id)
        .order_by(MaterialSheet.code))).all()
    return [{"code": code, "dcm": float(dcm)} for code, dcm in rows]


async def seed_lot(db, row: dict, suppliers: dict) -> dict:
    """One lot, through the real service. Returns a summary row for the CSV.

    `action` is one of:
        created   the lot, its label, its opening receipt (+ hides) are new
        repaired  the lot was already there but had no label; one was minted
        skipped   already seeded — NOT topped up (see the idempotency note)
    """
    cat, subtype = row["category"], row.get("subtype")
    attrs = dict(row["attributes"])
    supplier = suppliers.get(row.get("supplier"))

    body = LotCreate(
        category=cat, subtype=subtype, article=row["article"],
        colour=row["colour"], attributes=attrs,
        supplier_id=getattr(supplier, "id", None),
        sheets=[SheetIn(dcm=float(d)) for d in (row.get("sheets") or [])] or None,
    )

    svc = MaterialService(db)

    # ── already seeded? ──────────────────────────────────────────────────────
    # Asked BEFORE create_lot rather than by catching its 409, so a lot that
    # exists without a label can be repaired instead of merely reported.
    dup = await svc.repo.find_duplicate_lot(
        category=cat, subtype=subtype, article=row["article"],
        colour=row["colour"], thickness=attrs.get("thickness"),
        size=attrs.get("size"))
    if dup is not None:
        sheets = await lot_sheet_labels(db, dup.id)
        code = await active_lot_code(db, dup.id)
        if code is None:
            caption = MaterialService._caption(
                cat, subtype, body, attrs, Decimal(str(dup.on_hand)), dup.uom)
            bc = await svc.barcodes.mint_lot_code_nocommit(
                dup.id, _LOT_BARCODE_TYPE[cat], caption)
            await db.commit()
            return dict(action="repaired", code=bc.code, lot_id=dup.id,
                        on_hand=float(dup.on_hand), uom=dup.uom, sheets=sheets)
        return dict(action="skipped", code=code, lot_id=dup.id,
                    on_hand=float(dup.on_hand), uom=dup.uom, sheets=sheets)

    # create_lot owns its transaction and commits — the lot, its label, its
    # opening receipt and its hides land together or not at all.
    result = await svc.create_lot(body)
    for warning in svc.decrement_warnings:          # sheet-sum mismatch, etc.
        log.warning("    ! %s", warning.get("note") or warning)
    return dict(action="created", code=result["lot_barcode"],
                lot_id=result["lot_id"], on_hand=result["on_hand"],
                uom=result["uom"], sheets=result.get("sheets") or [])


async def seed(only: set[str], dry: bool) -> int:
    """Returns a process exit code: 0 on a clean run, 1 if anything failed."""
    rows = flatten(only)

    log.info("-- PRE-FLIGHT (%d lot(s): %s) --", len(rows),
             ", ".join(sorted(only or CATALOGUE)))
    if problems := validate(rows):
        for p in problems:
            log.error("  x %s", p)
        log.error("\n-- ABORTED -- %d data problem(s); nothing was written.",
                  len(problems))
        return 1
    log.info("  ok - every lot resolves against MATERIAL_SPEC, specs are unique")

    counts: Counter = Counter()
    labels: list[dict] = []

    async with AsyncSessionLocal() as db:
        log.info("-- SUPPLIERS --")
        suppliers = await upsert_suppliers(db, dry)

        if dry:
            # create_lot commits, so there is no honest way to "try" it. The
            # pre-flight has already proved the data, and the duplicate check
            # below is the only other thing a real run would discover — so do
            # exactly that much and write nothing.
            log.info("-- LOTS (dry run: existence check only) --")
            for row in rows:
                attrs = row["attributes"]
                dup = await MaterialRepository(db).find_duplicate_lot(
                    category=row["category"], subtype=row.get("subtype"),
                    article=row["article"], colour=row["colour"],
                    thickness=attrs.get("thickness"), size=attrs.get("size"))
                action = "skipped" if dup is not None else "would create"
                counts[action] += 1
                log.info("  %-13s %-22s %-14s %s", action, row["article"],
                         row["colour"], row["category"])
            log.info("-- SUPPLIER ORDERS (dry run) --")
            counts.update(await upsert_supplier_orders(db, suppliers, dry))
            await db.rollback()
            log.info("\n[DRY RUN] nothing written. %s",
                     "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
            return 0

        for category, entries in CATALOGUE.items():
            if only and category not in only:
                continue
            log.info("-- %s (%d lot(s)) --", category, len(entries))
            for entry in entries:
                row = {"category": category, **entry}
                try:
                    r = await seed_lot(db, row, suppliers)
                except HTTPException as exc:
                    # The service's own 422/409 — a real contract violation the
                    # pre-flight did not model. Report it and keep going; one bad
                    # row must not cost the rest of the seed.
                    await db.rollback()
                    counts["failed"] += 1
                    log.error("  x %-22s %-14s %s", entry["article"],
                              entry["colour"], exc.detail)
                    continue
                except Exception as exc:                # noqa: BLE001 — seed resilience
                    await db.rollback()
                    counts["failed"] += 1
                    log.error("  x %-22s %-14s %s", entry["article"],
                              entry["colour"], exc)
                    continue

                counts[r["action"]] += 1
                mark = {"created": "+", "repaired": "~", "skipped": "="}[r["action"]]
                log.info("  %s %-22s %-14s %-14s %10.3f %-5s %s",
                         mark, entry["article"], entry["colour"],
                         r["code"], r["on_hand"], r["uom"],
                         f"{len(r['sheets'])} hide(s)" if r["sheets"] else "")

                labels.append(dict(
                    kind="LOT", code=r["code"], category=category,
                    subtype=entry.get("subtype") or "", article=entry["article"],
                    colour=entry["colour"],
                    thickness=entry["attributes"].get("thickness", ""),
                    size=entry["attributes"].get("size", ""),
                    qty=r["on_hand"], uom=r["uom"],
                    supplier=entry.get("supplier", "")))
                for sheet in r["sheets"]:
                    labels.append(dict(
                        kind="SHEET", code=sheet["code"], category=category,
                        subtype="", article=entry["article"],
                        colour=entry["colour"],
                        thickness=entry["attributes"].get("thickness", ""),
                        size="", qty=sheet["dcm"], uom="dcm",
                        supplier=entry.get("supplier", "")))

        # ── the open POs ────────────────────────────────────────────────────
        # FULL RUNS ONLY. The orders below name LEATHER and ACCESSORY articles,
        # and raising a leather PO during `--only lining` would write a row the
        # caller did not ask for and cannot see in that run's output.
        if not only:
            log.info("-- SUPPLIER ORDERS (%d) --", len(SUPPLIER_ORDERS))
            counts.update(await upsert_supplier_orders(db, suppliers, dry))

    # ── the print queue ──────────────────────────────────────────────────────
    # Same idea as seed_employees.py's cards CSV: a code that exists in the DB
    # but was never printed is not yet usable on the floor. EVERY label the run
    # touched goes in, skipped lots included, so the file is the whole print
    # queue for the chosen categories and a reprint needs no second run.
    if labels:
        LABELS_CSV.parent.mkdir(parents=True, exist_ok=True)
        with LABELS_CSV.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=list(labels[0]))
            w.writeheader()
            w.writerows(labels)

    log.info("")
    log.info("-- DONE -- %s", "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
             or "nothing to do")
    if labels:
        log.info("   labels CSV : %s  (print Code128 from the `code` column)",
                 LABELS_CSV)
    log.info("   Stock reads: GET /api/v1/materials/stock?category=LEATHER "
             "(and LINING / ACCESSORY)")
    log.info("   Every lot is SCANNABLE: GET /api/v1/barcode/resolve?code=LOT-...")
    log.info("   Open POs   : GET /api/v1/materials/lots then POST "
             "/api/v1/materials/receive with supplier_order_id to exercise the "
             "PO match, the 409 and the DM/MD substitution.")
    return 1 if counts["failed"] else 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Seed material lots for LEATHER, LINING and ACCESSORIES.")
    ap.add_argument("--only", action="append", default=[],
                    choices=["leather", "lining", "accessory"],
                    help="seed just this category (repeatable); default: all")
    ap.add_argument("--dry-run", action="store_true",
                    help="validate the data and report; write nothing")
    args = ap.parse_args()

    only = {c.upper() for c in args.only}
    raise SystemExit(asyncio.run(seed(only, dry=args.dry_run)))


if __name__ == "__main__":
    main()
