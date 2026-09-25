"""
================================================================================
scripts/seed.py — Load the MOCK factory dataset into the database
================================================================================

PURPOSE
    One runnable script that fills an empty (or half-filled) database with a
    complete, self-consistent Phase-1 dataset, derived from the real factory
    spreadsheets in data/:
      - GARMENT_ORDERPRODUCTION_DETAILS.xlsx  (6 clients: KJ, GGZ, NIPAL,
                                               RICANO, JP, NIPAL-NEW)
      - johnpeter.xlsx                        (a 7th, flat-format client)
    plus the hard-coded payroll roster in scripts/seed_employees.py.

    What it writes, in order:
      1. Operations (the ProductionStage vocabulary) + role->operation access.
      2. Clients, ClientOrders, Styles, SKUs — through the real import engine.
      3. Suppliers + material lots for all three floor categories, each with its
         child barcode (delegates to scripts/seed_materials.py — the single
         stock record).
      4. A MOCK material spec per style: the leather dcm/piece PLUS four trim
         lines that resolve to the lots step 3 just created, because the release
         gate refuses an unspecced style and a no-accessory declaration would
         make the whole kit subsystem unreachable.
      5. RELEASE — mints the Piece rows and their parent barcodes. This is the
         step that makes the floor scannable.
      6. Employees + employee barcodes + staff logins (delegates to
         scripts/seed_employees.py — the single roster of record).
      7. Mock piece rates, one card per style.
      8. Management / client logins.
      9. Stage-1..4 reference registries (templates, garment types, POM, aliases).

    THERE IS NO DRAWER STEP, and that is the change, not an omission. The store
    is a state on the GARMENT (`piece.store_state`), so a drawer pool would seed
    nothing the floor reads and the old "N pieces waiting for a drawer" warning
    was telling operators about a constraint that no longer exists. See
    `release_orders` for the details and for what happened to allow_pool_growth.

WHAT CHANGED (and why this script needed updating at all)
    The importer moved on and this script did not, so it died on its first call:

      · load_preview_into_order() no longer creates clients. It writes INTO a
        ClientOrder the caller names: (db, preview, order_number=..., replace=).
        The old `country_map=` argument is gone. The seed now does what the API
        does — create the client + order first, then commit the sheet into it.
      · build_preview() keys its result by SHEET NAME ("KJ GARMENT ORDER"),
        not by client, so the seed folds a client's sheets back together before
        loading them into that client's one order.
      · parse_order_sheet() returns THREE values now (lines, warnings, verdict).
      · ClientPreview no longer carries `production_cards` — the importer stopped
        parsing the weekly production sheets, so it no longer yields wage rates.
        Rates are the wages module's record; the seed writes a mock card instead.
      · Uploading no longer mints pieces. Minting happens at RELEASE
        (BreakdownService.release_styles), so the seed releases explicitly.

WHY SYNC
    Bulk seeding is a one-shot batch job. It uses the SYNC engine/session from
    core/database.py (the same one Alembic uses) — simpler and perfectly fine off
    the request path. Step 6 is the one exception: the barcode and user services
    are async-only, so that step runs its own event loop.

USAGE
    python -m scripts.seed                      # everything
    python -m scripts.seed --no-release         # orders only, mint nothing
    python -m scripts.seed --skip-people        # leave the roster alone
    python -m scripts.seed --skip-stock         # leave suppliers + lots alone
    python -m scripts.seed --no-accessories     # leather-only recipe (old shape)

    Idempotent: re-running reuses existing rows and tops up what is missing. It
    never deletes anything outside the orders it owns.

MOCK CREDENTIALS
    Management logins are 9000000000..9000000007, password == the number, all
    flagged must_change_password. Client portal logins are 92000000NN. These are
    DEMO CREDENTIALS — they are predictable by design and must not survive
    contact with a real factory network.
================================================================================
"""
import argparse
import asyncio
import os
import re
import sys
from datetime import date, datetime, timezone
from decimal import Decimal

# Make `import app...` work whether run as a module or a file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, engine
from app.core.enums import (
    MaterialCategory, ProductionReleaseStatus, ProductionStage,
    STAGE_ROLE_ACCESS, UserRole,
)
from app.core.enums_barcode import uom_for
from app.core.security import get_password_hash

# Import models (registers tables on Base.metadata). EVERY model module, not
# just the ones this script writes: SQLAlchemy resolves relationships by CLASS
# NAME at configure_mappers() time, which fires on the first query, so a
# half-registered registry fails on an unrelated insert (CLAUDE.md §11).
from app.modules.users.models import User
from app.modules.employees.models import Employee
from app.modules.clients.models import Client, Style, SKU
from app.modules.production.models import Operation, OperationAccess, Piece
from app.modules.wages.models import Rate
from app.modules.attendance.models import AttendanceLog, ShiftConfig  # noqa: F401
from app.modules.barcode.models import MaterialLot, StyleMaterialSpec
from app.core import models as _core_models  # noqa: F401  (cross-cutting tables)
from app.modules.procurement import models as _procurement  # noqa: F401
from app.modules.bom import models as _bom  # noqa: F401
from app.modules.inventory import models as _inv  # noqa: F401
from app.modules.supplier_po import models as _spo  # noqa: F401
from app.modules.procurement.seed_templates import seed_client_templates
from app.modules.bom.seed_stage2 import seed_stage2
from app.modules.inventory.seed_inventory import seed_inventory

# Import engine (sync) for the spreadsheets.
from app.modules.imports.import_engine import ClientPreview, ImportPreview, build_preview
from app.modules.imports.parse_orders import parse_order_sheet
from app.modules.imports.load_to_db import (
    load_preview_into_order, _get_or_create_client, _get_or_create_order,
)
# premint HAS NO DRAWER HALF ANY MORE. bootstrap_drawer_pool /
# allocate_waiting_pieces / grow_drawer_pool / INITIAL_DRAWER_POOL are deleted
# along with the drawers module — see the note above `release_orders`.

# ── Reference data ────────────────────────────────────────────────────────
GARMENT_FILE = "data/GARMENT_ORDERPRODUCTION_DETAILS.xlsx"
JOHNPETER_FILE = "data/johnpeter.xlsx"

# ── Operations & access, DERIVED FROM THE ENUM (F02/F58) ────────────────────
# The seed operation codes MUST equal the ProductionStage vocabulary the runtime
# uses, or every /production/log scan 500s (F02). We therefore GENERATE OPS from
# the enum rather than hand-listing codes that drift. The two parallel cut
# entries (LEATHER_CUTTING, LINING_CUTTING) plus the linear leather chain give
# the full operation set; LINING_CUTTING is inserted right after LEATHER_CUTTING.
def _seed_operations_spec() -> list[tuple[str, str, int]]:
    """[(code, label, sequence)] for every ProductionStage, enum-derived."""
    chain = ProductionStage.leather_chain()          # includes LEATHER_CUTTING first
    # Insert LINING_CUTTING right after LEATHER_CUTTING (parallel cut entry).
    ordered: list[ProductionStage] = []
    for st in chain:
        ordered.append(st)
        if st is ProductionStage.LEATHER_CUTTING:
            ordered.append(ProductionStage.LINING_CUTTING)
    spec = []
    for i, st in enumerate(ordered, start=1):
        label = st.value.replace("_", " ").title()
        spec.append((st.value, label, i))
    return spec


OPS = _seed_operations_spec()

# Which manager role may log which operation — DERIVED from STAGE_ROLE_ACCESS so
# a role that owns a stage (incl. LINING_MANAGER, F58) always gets seeded access
# and can never silently be omitted. DM/MD bypass in the service, so they are not
# listed here by design.
def _seed_access_spec() -> dict[str, list[str]]:
    access: dict[str, list[str]] = {}
    for stage, roles in STAGE_ROLE_ACCESS.items():
        for role in roles:
            rv = getattr(role, "value", role)
            access.setdefault(rv, []).append(stage.value)
    return access


ACCESS = _seed_access_spec()

# Each spreadsheet client key -> (display name, country, order number).
# The ORDER NUMBER is the seed's own invention: an order is created by the
# client-onboarding flow in real life, and the importer now requires one to
# exist before a sheet can be committed into it.
CLIENT_META = {
    "KJ":        ("Khawaja (KJ)", "Pakistan", "KJ-SS26-001"),
    "GGZ":       ("GGZ SRL",      "Italy",    "GGZ-SS26-001"),
    "NIPAL":     ("Nipal",        "Italy",    "NIPAL-SS26-001"),
    "RICANO":    ("Ricano",       "Spain",    "RICANO-SS26-001"),
    "JP":        ("JP Garments",  "India",    "JP-SS26-001"),
    "NIPAL-NEW": ("Nipal (New)",  "Italy",    "NIPAL-NEW-SS26-001"),
}
JOHNPETER_META = ("John Peter", "Italy", "JOHNPETER-SS26-001")

DEFAULT_RATE_DATE = date(2026, 1, 1)

# MOCK piece rates, rupees per piece, one card applied to every style. The
# importer used to read these off the weekly production sheets; it no longer
# parses those sheets at all, and rates are the wages module's system of record.
# A flat card is honest about being mock data and still gives a wage run
# something to price PIECE_RATE work with.
MOCK_RATES = {
    "LEATHER_CUTTING":  18.00,
    "LINING_CUTTING":    9.00,
    "FUSING":            5.00,
    "PASTING":           6.00,
    "LINE_STITCHING":   22.00,
    "SHELL_STITCHING":  35.00,
    "FINAL_FINISH":     12.00,
    "FINAL_INSPECTION":  6.00,
    "PACKAGE_EXPORT":    4.00,
}

# MOCK per-piece leather consumption (dcm). One number for every style: the real
# figure comes off the DM's material-spec screen per garment, and inventing a
# per-style number here would look like data rather than a placeholder.
MOCK_LEATHER_DCM = Decimal("12.500")

# ── The mock ACCESSORY recipe ────────────────────────────────────────────────
# WHY THE SEED DECLARES ACCESSORIES AT ALL. It used to stamp every style
# `material_spec_no_accessories = True`, which is the one answer that makes the
# entire kit subsystem unreachable: no accessory line means kit_required is
# False for every garment, so the store scan has nothing to issue,
# `piece_material_issue` stays empty, and POST /materials/issues, the kit
# checklist and every KitStatus other than NOT_REQUIRED can never be seen on a
# seeded database. A seed that hides half the feature is not a seed of it.
#
# EACH LINE RESOLVES TO EXACTLY ONE SEEDED LOT, which is the whole point —
# `_resolve_lot` matches on (category, subtype, article, colour, thickness,
# size), so these six values are copied from scripts/seed_materials.py's
# catalogue and must stay in step with it. A line that matched nothing would
# seed a recipe that resolves NONE and issues nothing, which is exactly the
# broken state this is here to avoid.
#
# `garment_size` is deliberately NULL on all four: these are the trims every
# jacket takes whatever its size, and a sized line would only reach the SKUs of
# that size (see StyleSpecService.applies_to_size).
MOCK_ACCESSORIES = [
    dict(subtype="THREAD", article="POLY CORE THREAD", colour="BLACK",
         thickness="TEX 40", size=None, qty_per_piece=Decimal("120.000")),
    dict(subtype="OTHER", article="MAIN LABEL", colour="BLACK",
         thickness=None, size=None, qty_per_piece=Decimal("1.000")),
    dict(subtype="OTHER", article="HANG TAG", colour="PRINTED",
         thickness=None, size=None, qty_per_piece=Decimal("1.000")),
    dict(subtype="OTHER", article="POLY BAG", colour="CLEAR",
         thickness=None, size=None, qty_per_piece=Decimal("1.000")),
]

SEED_ACTOR = "seed script"


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", name.strip().lower()).strip(".")


# ── Sheet name -> client key ────────────────────────────────────────────────
# import_engine._client_key keys a preview by the FULL sheet name now, so
# "KJ GARMENT ORDER" and "KJ PRODUCTION" are two separate keys for one client.
# The seed folds them back onto the client they belong to.
_SHEET_MARKERS = (" GARMENT ORDER", "-GARMENT ORDER", " PRODUCTION", "-PRODUCTION")


def _client_key_from_sheet(sheet_key: str) -> str:
    name = sheet_key.upper().strip()
    for marker in _SHEET_MARKERS:
        if marker in name:
            return name.split(marker)[0].strip(" -")
    return name


# ── Step 1: operations + access ─────────────────────────────────────────────
def seed_operations(db: Session) -> dict[str, Operation]:
    ops: dict[str, Operation] = {}
    for code, label, seq in OPS:
        o = db.scalar(select(Operation).where(Operation.code == code))
        if not o:
            o = Operation(code=code, label=label, sequence=seq)
            db.add(o); db.flush()
        elif (o.label, o.sequence) != (label, seq):
            # REPAIR, not just get-or-create. Rows seeded by an older spec kept
            # their old sequence — LEATHER_CUTTING and LINING_CUTTING both sat at
            # 1 — which makes any sequence-ordered read of the chain ambiguous.
            o.label, o.sequence = label, seq
            db.flush()
        ops[code] = o
    for role, codes in ACCESS.items():
        for code in codes:
            exists = db.scalar(select(OperationAccess).where(
                OperationAccess.role == role,
                OperationAccess.operation_id == ops[code].id))
            if not exists:
                db.add(OperationAccess(role=role, operation_id=ops[code].id))
    db.commit()
    return ops


# ── Step 2: clients, orders, styles, SKUs ───────────────────────────────────
def _load_into_order(db: Session, key: str, cp: ClientPreview,
                     name: str, country: str, order_number: str) -> dict:
    """Create the client + its order, then commit one client's sheets into it.

    This mirrors POST /imports/commit exactly: the order number is entered
    first and the loader writes into it, with `replace=True` so a re-run
    replaces that order's styles rather than adding to them.

    AN ALREADY-RELEASED ORDER IS SKIPPED, and that check belongs here rather
    than in the loader. `replace=True` DELETEs the order's SKUs first, and the
    loader's own guard only looks for ProductionEvent rows — but release mints
    Piece rows long before anyone logs a stage against them, and `piece.sku_id`
    is a plain FK. So a second seed run over a released order died on

        ForeignKeyViolation: update or delete on table "sku" violates
        "fk_piece_sku_id_sku" ... still referenced from table "piece"

    halfway through the batch. Loading additively instead is not an option:
    _upsert_sku SUMS into an existing SKU, so replace=False would silently
    double every ordered quantity on each run. Once an order has barcoded
    garments it is live data, not seed data — leave it exactly as it is.
    """
    from app.modules.clients.models import ClientOrder

    client = _get_or_create_client(db, name, country)
    if not client.code:
        client.code = key
    order = _get_or_create_order(db, client, order_number)
    if not order.currency:
        order.currency = client.currency
    db.flush()

    minted = db.scalar(
        select(func.count(Piece.id))
        .join(SKU, Piece.sku_id == SKU.id)
        .join(Style, SKU.style_id == Style.id)
        .where(Style.client_order_id == order.id)) or 0
    if minted:
        db.commit()
        return {"styles": 0, "skus_created": 0, "skus_updated": 0,
                "skipped_pieces": int(minted)}

    one = ImportPreview(clients={key: cp})
    stats = load_preview_into_order(db, one, order_number=order_number,
                                    replace=True)
    return stats


def seed_orders(db: Session) -> dict[str, str]:
    """Load every order sheet. Returns {display name: order_number}."""
    loaded: dict[str, str] = {}

    preview = build_preview(GARMENT_FILE)

    # Fold the sheet-keyed previews back onto one preview per factory client.
    # A client with two order sheets (JP-GARMENT ORDER-1 / -2) is ONE order.
    by_client: dict[str, ClientPreview] = {}
    for sheet_key, cp in preview.clients.items():
        if not cp.order_lines:
            continue                    # production / unknown sheets: nothing to load
        key = _client_key_from_sheet(sheet_key)
        target = by_client.setdefault(key, ClientPreview(key=key))
        target.order_lines.extend(cp.order_lines)
        target.warnings.extend(cp.warnings)

    for key, cp in by_client.items():
        name, country, order_number = CLIENT_META.get(
            key, (key.title(), None, f"{key}-SS26-001"))
        stats = _load_into_order(db, key, cp, name, country, order_number)
        loaded[name] = order_number
        print(f"   - {name:16} {order_number:22} " + (
            f"ALREADY RELEASED ({stats['skipped_pieces']} pieces) - left as is"
            if stats.get("skipped_pieces") else
            f"styles={stats['styles']:<3} "
            f"skus={stats['skus_created'] + stats['skus_updated']}"))

    # John Peter — a flat single-sheet order (Date|Style|Suede Colour|Article|sizes).
    # Routed through the SAME loader rather than hand-rolled inserts: the old
    # hand-rolled path called _upsert_sku, which SUMS quantities, so every re-run
    # silently doubled the order.
    wb = openpyxl.load_workbook(JOHNPETER_FILE, data_only=True)
    lines, warns, verdict = parse_order_sheet(wb.active)       # 3-tuple now
    if verdict == "ORDER" and lines:
        name, country, order_number = JOHNPETER_META
        cp = ClientPreview(key="JOHN PETER", order_lines=lines, warnings=warns)
        stats = _load_into_order(db, "JOHN PETER", cp, name, country, order_number)
        loaded[name] = order_number
        print(f"   - {name:16} {order_number:22} " + (
            f"ALREADY RELEASED ({stats['skipped_pieces']} pieces) - left as is"
            if stats.get("skipped_pieces") else
            f"styles={stats['styles']:<3} "
            f"skus={stats['skus_created'] + stats['skus_updated']}"))
    else:
        print(f"   ! {JOHNPETER_FILE} parsed as {verdict} - skipped")

    return loaded


# ── Step 3: a mock material spec, so the release gate can pass ──────────────
def seed_material_specs(db: Session, order_numbers: list[str], *,
                        with_accessories: bool = True) -> dict:
    """Give every seeded style a confirmed recipe: a LEATHER line + the trims.

    kit_rules.release_blockers refuses to release a style whose material spec is
    unconfirmed or has no leather line, because release freezes the recipe the
    ledger then spends. Real data comes off the DM's material-spec screen; the
    seed writes the minimum truthful placeholder so the rest of the pipeline is
    reachable, and leaves `note` saying so.

    THE ACCESSORY HALF IS WHAT MAKES THE KIT REACHABLE (see MOCK_ACCESSORIES).
    With it, a seeded garment reports kit_required=True / kit_status=PENDING and
    the store scan has something to issue; without it every garment is
    NOT_REQUIRED and the ledger stays empty forever. Pass
    `with_accessories=False` (`--no-accessories`) for the old leather-only
    behaviour.

    THE THREE-STATE `no_accessories` FLAG IS SET TO MATCH WHAT WAS WRITTEN, not
    hard-coded: declaring "this style takes none" while accessory lines exist is
    the exact contradiction StyleSpecService.confirm refuses with a 422, and a
    seed must not write a row the API would have rejected.
    """
    from app.modules.clients.models import ClientOrder

    now = datetime.now(timezone.utc)
    uom = uom_for(MaterialCategory.LEATHER.value, None)
    made = {"leather": 0, "accessory": 0, "styles": 0}

    # ── WHICH LEATHER, and why this is not `style.article` ───────────────────
    # It used to write `article=style.article, thickness=style.thickness`, and
    # that line resolved AMBIGUOUS for every seeded style — often against all
    # seven leather lots at once. Two reasons, both worth stating:
    #   · `style.article` is the GARMENT's article code ("CL1"), not a leather
    #     article ("GOAT SUEDE"). They are different vocabularies that happen to
    #     share a column name, and _resolve_lot matches the material one.
    #   · it is NULL on most of the seeded styles anyway, and a line with a null
    #     article/colour/thickness constrains nothing, so find_lots returns the
    #     whole shelf.
    # So the seed picks a REAL lot per style, round-robin across whatever leather
    # the catalogue holds, and copies that lot's own six columns onto the line.
    # material_lot_id is pinned as well: that is what the column is for, and it
    # makes the line resolve PINNED rather than depending on the key match
    # staying unique as more stock arrives.
    leather_lots = list(db.scalars(
        select(MaterialLot)
        .where(MaterialLot.category == MaterialCategory.LEATHER.value,
               MaterialLot.is_active.is_(True))
        .order_by(MaterialLot.created_at, MaterialLot.id)))
    if not leather_lots:
        print("   ! no LEATHER lots in stock — the leather spec line will fall "
              "back to the style's own article and resolve to no lot. Run "
              "without --skip-stock, or seed materials first.")
    # Scoped to the orders THIS run loaded. A style that arrived some other way
    # belongs to whoever entered it; the seed does not confirm recipes it did
    # not write.
    styles = db.scalars(
        select(Style).join(ClientOrder, Style.client_order_id == ClientOrder.id)
        .where(ClientOrder.order_number.in_(order_numbers))
    ).all()

    for index, style in enumerate(styles):
        made["styles"] += 1
        line = db.scalar(select(StyleMaterialSpec).where(
            StyleMaterialSpec.style_id == style.id,
            StyleMaterialSpec.sku_id.is_(None),
            StyleMaterialSpec.category == MaterialCategory.LEATHER.value,
            StyleMaterialSpec.is_active.is_(True)))
        if line is None:
            lot = leather_lots[index % len(leather_lots)] if leather_lots else None
            db.add(StyleMaterialSpec(
                style_id=style.id, sku_id=None,
                category=MaterialCategory.LEATHER.value, subtype=None,
                article=(lot.article if lot else style.article),
                colour=(lot.colour if lot else None),
                thickness=(lot.thickness if lot else style.thickness),
                size=None, garment_size=None,
                material_lot_id=(lot.id if lot else None),
                qty_per_piece=MOCK_LEATHER_DCM, uom=uom,
                note="MOCK consumption seeded by scripts/seed.py — replace with "
                     "the real per-piece figure before costing anything.",
                is_active=True))
            made["leather"] += 1

        if with_accessories:
            for trim in MOCK_ACCESSORIES:
                # Matched on the same identity the duplicate rule uses, so a
                # re-run adds nothing — the seed is run repeatedly against a
                # half-filled database and must not grow the recipe each time.
                existing = db.scalar(select(StyleMaterialSpec).where(
                    StyleMaterialSpec.style_id == style.id,
                    StyleMaterialSpec.sku_id.is_(None),
                    StyleMaterialSpec.category == MaterialCategory.ACCESSORY.value,
                    StyleMaterialSpec.subtype == trim["subtype"],
                    StyleMaterialSpec.article == trim["article"],
                    StyleMaterialSpec.is_active.is_(True)))
                if existing is not None:
                    continue
                db.add(StyleMaterialSpec(
                    style_id=style.id, sku_id=None,
                    category=MaterialCategory.ACCESSORY.value,
                    subtype=trim["subtype"], article=trim["article"],
                    colour=trim["colour"], thickness=trim["thickness"],
                    size=trim["size"], garment_size=None,
                    qty_per_piece=trim["qty_per_piece"],
                    # DERIVED, never typed — buttons are pcs and thread is
                    # mtrs whatever anyone writes here (StyleSpecService
                    # discards a sent uom for exactly this reason).
                    uom=uom_for(MaterialCategory.ACCESSORY.value,
                                trim["subtype"]),
                    note="MOCK trim seeded by scripts/seed.py — resolves to the "
                         "lot scripts/seed_materials.py creates.",
                    is_active=True))
                made["accessory"] += 1

        # The three-state accessory answer: NULL ("nobody asked") does not pass
        # the gate, so the seed answers it explicitly — and answers it TRUTHFULLY
        # against what it just wrote.
        style.material_spec_no_accessories = not with_accessories
        if style.material_spec_confirmed_at is None:
            style.material_spec_confirmed_at = now
            style.material_spec_confirmed_by = SEED_ACTOR
    db.commit()
    return made


# ── Step 5: release — this is what mints pieces and barcodes ────────────────
def release_orders(db: Session, order_numbers: list[str]) -> dict:
    """Release every DRAFT style, minting pieces + their parent barcodes.

    Uploading a breakdown no longer mints anything — the DM releases the styles
    that are actually going to the floor, and that transition is what mints. The
    seed reuses `breakdown._release_sync`, the exact sync half of
    BreakdownService.release_styles, so the seeded rows are identical to the ones
    the real DM transition produces (style stamped RELEASED, pieces minted, all
    in one transaction per order).

    IT SKIPS THE API's PER-STYLE SPEC GATE, which step 4 has already satisfied,
    and it writes no audit_log row — a seed has no DM to name.

    NO DRAWER POOL, AND NOTHING IS "WAITING" FOR ONE.
        This step used to bootstrap 200 drawers, then report thousands of pieces
        as `pieces_waiting_for_drawer` and warn that they "cannot pass the merge
        gate until one frees up". That warning is now FALSE, and it was the most
        misleading line the seed printed: the store moved onto the garment
        (`piece.store_state`, 20260902_store_piece), and
        ProductionService._merge_ok says so in as many words — "a piece with no
        drawer is no longer a piece that cannot be line-stitched: there are no
        drawers to run out of". StoreService issues kits with `drawer=None`.

        Every one of the 4,983 seeded garments is immediately storable, sendable
        and line-stitchable. `allow_pool_growth` is gone from the signature; a
        caller that still passes it is swallowed by `**_legacy` rather than
        dying on a TypeError mid-release.
    """
    from app.modules.clients.models import ClientOrder
    from app.modules.imports.breakdown import _release_sync

    total = {"styles_released": 0, "pieces_minted": 0}
    for number in order_numbers:
        order = db.scalar(select(ClientOrder).where(
            ClientOrder.order_number == number))
        if order is None:
            continue
        style_ids = db.scalars(select(Style.id).where(
            Style.client_order_id == order.id,
            Style.production_status == ProductionReleaseStatus.DRAFT.value,
        )).all()
        if not style_ids:
            continue
        db.commit()          # _release_sync opens its own session; don't hold locks
        # No lining declarations are supplied by the seed; release falls back
        # to the normal style inference for needs_lining.
        stats = _release_sync(order.id, list(style_ids), SEED_ACTOR)
        total["styles_released"] += len(style_ids)
        total["pieces_minted"] += int(stats.get("pieces_minted", 0))
        print(f"   - {number:22} styles={len(style_ids):<3} "
              f"pieces={stats.get('pieces_minted', 0)}")
    return total


# ── Step 6: employees, cards and staff logins ───────────────────────────────
def seed_people(db: Session) -> int:
    """Delegate to scripts/seed_employees.py — the single roster of record.

    This step used to read data/employees_detail.xlsx and invent a phone and an
    email per person, which produced a SECOND, conflicting roster: the same
    people under different designations and — the part that actually breaks the
    floor — with NO employee barcode. A worker with no card cannot be scanned in,
    so `is_present_today()` is false and nothing can be logged against them.
    seed_employees.py mints the card in the same transaction as the person.
    """
    from scripts.seed_employees import seed as seed_roster

    db.commit()          # the async seeder opens its own connection; release ours
    asyncio.run(seed_roster())
    return int(db.scalar(select(func.count(Employee.id))) or 0)


# ── Step 3: suppliers + material lots ───────────────────────────────────────
def seed_stock(db: Session) -> int:
    """Delegate to scripts/seed_materials.py — the single stock record.

    WHY THE FULL SEED NEEDED THIS AT ALL. It released 4,983 garments into a
    factory holding ZERO material: the cut screen's lot picker
    (GET /materials/lots) came back empty, so no frontend could supply the
    `leather_lot_id` POST /production/log wants; `/materials/stock` read zero for
    every category; and no accessory line could resolve to anything. Orders and
    people were seeded, and the entire Stage-0 half of Phase 1 was not.

    IT RUNS BEFORE THE MATERIAL SPECS ON PURPOSE (step 4). A spec line resolves
    to a lot by matching six columns; if the lots do not exist yet, every line
    resolves NONE and the seeded recipe is a recipe for nothing.

    Same delegation shape as seed_people: one script owns the data, this one
    calls it, and the two cannot drift into two catalogues. `only=set()` means
    all three categories; `dry=False` writes.
    """
    from scripts.seed_materials import seed as seed_catalogue

    db.commit()          # the async seeder opens its own connection; release ours
    rc = asyncio.run(seed_catalogue(set(), dry=False))
    if rc:
        print("   ! seed_materials reported failures — see the log above. The "
              "seed continues; accessory spec lines for missing lots will "
              "resolve NONE until the stock is there.")
    return int(db.scalar(select(func.count(MaterialLot.id))) or 0)


# ── Step 7: mock piece rates ────────────────────────────────────────────────
def seed_rates(db: Session, ops: dict[str, Operation],
               order_numbers: list[str]) -> int:
    """One flat rate card per seeded style, effective from DEFAULT_RATE_DATE."""
    from app.modules.clients.models import ClientOrder

    count = 0
    style_ids = db.scalars(
        select(Style.id).join(ClientOrder, Style.client_order_id == ClientOrder.id)
        .where(ClientOrder.order_number.in_(order_numbers))
    ).all()
    for style_id in style_ids:
        for code, amount in MOCK_RATES.items():
            op = ops.get(code)
            if not op:
                continue
            exists = db.scalar(select(Rate.id).where(
                Rate.style_id == style_id, Rate.operation_id == op.id,
                Rate.effective_from == DEFAULT_RATE_DATE))
            if exists:
                continue
            db.add(Rate(style_id=style_id, operation_id=op.id,
                        rate=amount, effective_from=DEFAULT_RATE_DATE))
            count += 1
        db.flush()
    db.commit()
    return count


# ── Step 8: management and client logins ────────────────────────────────────
def _make_user(db: Session, *, name: str, phone: str, role: UserRole,
               email: str | None = None, employee_id=None, client_id=None,
               password: str | None = None,
               must_change_password: bool = True) -> User:
    # Idempotent on phone OR email. If the user already exists, refresh the
    # foreign-key links (client/employee IDs change when orders are re-imported
    # with replace=True) instead of inserting a duplicate.
    existing = db.scalar(select(User).where(User.phone == phone))
    if not existing and email:
        existing = db.scalar(select(User).where(User.email == email))
    if existing:
        if employee_id is not None:
            existing.employee_id = employee_id
        if client_id is not None:
            existing.client_id = client_id
        db.flush()
        return existing
    u = User(
        name=name, phone=phone, email=email, role=role,
        # Default stays "password == phone"; `password` overrides it for the one
        # account the client specified by name (see the store manager below).
        password_hash=get_password_hash(password or phone),
        is_active=True, must_change_password=must_change_password,
        employee_id=employee_id, client_id=client_id,
    )
    db.add(u); db.flush()
    return u


def seed_users(db: Session) -> dict[str, int]:
    """Seed the LOGIN accounts — management, store, and one per client portal."""
    stats = {"staff": 0, "clients": 0}

    # Management / viewer accounts. MANAGING_DIRECTOR is the superuser / BOM
    # approver; DIRECT_MANAGER stays as operational lead (still bypasses role
    # gates during the MD transition — see users/deps.py::SUPERUSER_ROLES).
    staff = [
        ("Managing Director", "9000000000", UserRole.MANAGING_DIRECTOR),
        ("Direct Manager", "9000000001", UserRole.DIRECT_MANAGER),
        ("Cutting Manager", "9000000002", UserRole.CUTTING_MANAGER),
        ("Stitching Manager", "9000000003", UserRole.STITCHING_MANAGER),
        ("Office Viewer", "9000000004", UserRole.VIEWER),
        ("HR / Accounts", "9000000005", UserRole.HR),
        ("Lining Manager", "9000000006", UserRole.LINING_MANAGER),
        ("Security Gate", "9000000007", UserRole.SECURITY),
    ]
    for nm, ph, role in staff:
        _make_user(db, name=nm, phone=ph, role=role,
                   email=f"{_slug(nm)}@factory.local")
        stats["staff"] += 1

    # ── The Store Management login (bug #16) ─────────────────────────────────
    # The client specified this account by name: username STOREMANAGER, password
    # STORE. `User.phone` IS the login username (UserRepository.get_by_username
    # queries User.phone), so the username goes in that column — it is a login
    # identifier here, not a phone number.
    #
    # THIS IS A WEAK, SHARED, WELL-KNOWN CREDENTIAL and it is seeded because it
    # was asked for explicitly. must_change_password is False so it keeps working
    # as specified rather than forcing a reset on first use — which also means
    # nothing will ever prompt anyone to change it. Rotate it before this reaches
    # a real factory network; the role itself is correctly scoped (store hub
    # only), so the exposure is the store, not the whole ERP.
    _make_user(db, name="Store Manager", phone="STOREMANAGER",
               role=UserRole.STORE_MANAGER, email="storemanager@factory.local",
               password="STORE", must_change_password=False)
    stats["staff"] += 1

    # NO logins for shop-floor employees. Workers are not given system access:
    # they hold an employee record + a scannable card, and SECURITY / HR / MD /
    # DM check them in and out. Seeding an EMPLOYEE login here would recreate
    # exactly the app_user rows the app no longer mints (UserRole.login_roles()).

    # One CLIENT login per client (manager-provisioned in real life; pre-seeded here).
    cidx = 0
    for client in db.scalars(select(Client).order_by(Client.name)):
        cidx += 1
        _make_user(db, name=f"{client.name} (portal)", phone=f"920000{cidx:04d}",
                   role=UserRole.CLIENT, email=f"{_slug(client.name)}@client.local",
                   client_id=client.id)
        stats["clients"] += 1

    db.commit()
    return stats


def _count(db: Session, model) -> int:
    return int(db.scalar(select(func.count(model.id))) or 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("PURPOSE")[0].strip())
    ap.add_argument("--no-release", dest="release", action="store_false",
                    help="load the orders but mint no pieces or barcodes")
    ap.add_argument("--skip-people", action="store_true",
                    help="leave the employee roster and staff logins untouched")
    ap.add_argument("--skip-stock", action="store_true",
                    help="leave suppliers and material lots alone (the accessory "
                         "spec lines will then resolve to no lot)")
    ap.add_argument("--no-accessories", dest="accessories",
                    action="store_false",
                    help="seed a LEATHER-only recipe, as this script used to. "
                         "Every garment then reports kit_required=false and the "
                         "kit/issue surface cannot be exercised.")
    ap.add_argument("--create-all", action="store_true",
                    help="dev only: CREATE TABLE anything missing (prod uses Alembic)")
    args = ap.parse_args()

    if args.create_all:
        Base.metadata.create_all(engine)

    db = SessionLocal()
    try:
        print("1/9  operations + role access")
        ops = seed_operations(db)

        print("2/9  clients, orders, styles, SKUs")
        loaded = seed_orders(db)

        if args.skip_stock:
            print("3/9  suppliers + material lots SKIPPED (--skip-stock)")
            n_lots = _count(db, MaterialLot)
        else:
            print("3/9  suppliers + material lots (Stage-0 stock)")
            n_lots = seed_stock(db)

        print("4/9  material specs — leather%s (the release gate)"
              % (" + trims" if args.accessories else " only"))
        spec_stats = seed_material_specs(db, list(loaded.values()),
                                         with_accessories=args.accessories)

        if args.release:
            print("5/9  release: minting pieces + their barcodes")
            release_stats = release_orders(db, list(loaded.values()))
        else:
            print("5/9  release SKIPPED (--no-release)")
            release_stats = {}

        if args.skip_people:
            print("6/9  people SKIPPED (--skip-people)")
            n_people = _count(db, Employee)
        else:
            print("6/9  employees, cards, staff logins")
            n_people = seed_people(db)

        print("7/9  mock piece rates")
        n_rates = seed_rates(db, ops, list(loaded.values()))

        print("8/9  management + client logins")
        user_stats = seed_users(db)

        print("9/9  reference registries (stage 1/2/4)")
        n_templates = seed_client_templates(db)   # Stage-1 validation registry (§4)
        stage2_stats = seed_stage2(db)            # Stage-2 garment types + POM dict (§3)
        stage4_stats = seed_inventory(db)         # Stage-4 aliases + uom + inventory master

        print("-" * 62)
        print("SEED COMPLETE")
        print(f"   operations : {len(ops)}")
        print(f"   clients    : {_count(db, Client)}")
        print(f"   styles     : {_count(db, Style)}")
        print(f"   skus       : {_count(db, SKU)}")
        print(f"   pieces     : {_count(db, Piece)}")
        print(f"   lots       : {n_lots}  (suppliers + leather/lining/accessory)")
        print(f"   specs      : {spec_stats['styles']} styles · "
              f"{spec_stats['leather']} new leather line(s) · "
              f"{spec_stats['accessory']} new trim line(s)")
        print(f"   employees  : {n_people}")
        print(f"   rates      : {n_rates} new")
        print(f"   templates  : {n_templates}")
        print(f"   stage2     : {stage2_stats}")
        print(f"   stage4     : {stage4_stats}")
        print(f"   users      : {user_stats}")
        if release_stats:
            print(f"   release    : {release_stats}")
            print("   Every released garment is immediately storable and "
                  "line-stitchable: the store is a state on the PIECE, so "
                  "nothing is queued waiting for space.")
        print("-" * 62)
        print("   Login (Swagger Authorize / POST /api/v1/auth/login):")
        print("     username=9000000000  password=9000000000  (managing director)")
        print("     username=9000000001  password=9000000001  (direct manager)")
        print("   Every seeded login must change its password on first use.")
        print("   Printable labels: data/employee_cards.csv (worker cards) and "
              "data/material_lot_labels.csv (lot + hide labels).")
    finally:
        db.close()


if __name__ == "__main__":
    main()
