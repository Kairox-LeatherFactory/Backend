"""
================================================================================
scripts/seed.py — Populate the database from the REAL factory spreadsheets
================================================================================

PURPOSE
    A single runnable script that loads everything needed to demo/operate the
    platform, derived from the actual files in /mnt/project:
      - employees_detail.xlsx               (24 monthly + 22 piece-rate workers)
      - GARMENT_ORDERPRODUCTION_DETAILS.xlsx (6 clients: KJ, GGZ, NIPAL, RICANO,
                                              JP, NIPAL-NEW — orders + production)
      - johnpeter.xlsx                       (a 7th flat-format client: John Peter)

    What it seeds, in order:
      1. Operations (CUTTING..FF) and role->operation access rules.
      2. Client orders + styles + SKUs (via the proven idempotent import engine).
      3. Employees, with MOCKED phone/email (real files have no contact data).
      4. Piece-rate Rates from the production cards.
      5. USER LOGINS for everyone:
           - 1 managing director (superuser), 1 direct manager, 1 cutting manager,
             1 stitching manager, 1 viewer, 1 HR
           - 1 EMPLOYEE login per employee   (linked via employee_id)
           - 1 CLIENT login per client       (linked via client_id)
         Password defaults to the user's phone (bcrypt-hashed) and
         must_change_password=True, so every account is forced to reset on first
         login.

WHY SYNC
    Bulk seeding is a one-shot batch job. It uses the SYNC engine/session from
    core/database.py (the same one Alembic uses) — simpler and perfectly fine
    off the request path. The live API remains fully async.

USAGE
    # from the project root, with .env pointing DATABASE_URL at your DB:
    python -m scripts.seed
    # or:  python scripts/seed.py

    Idempotent enough to re-run in dev: existing rows are reused, not duplicated.

MOCKED CREDENTIALS (v1)
    Phones are generated deterministically so logins are predictable in a demo:
      direct manager   90000000 01
      cutting manager  90000000 02
      stitching mgr    90000000 03
      viewer           90000000 04
      employees        9100000 0NN   (NN = sequential)
      clients          9200000 0NN   (NN = sequential)
    Email = a slugified name @ factory.local. REPLACE with real data before
    production use; phone-as-password is a known weak default.
================================================================================
"""
import os
import re
import sys
from datetime import date

# Make `import app...` work whether run as a module or a file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import openpyxl
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import Base, SessionLocal, engine
from app.core.enums import UserRole, WageType
from app.core.security import get_password_hash

# Import models (registers tables on Base.metadata).
from app.modules.users.models import User
from app.modules.employees.models import Employee
from app.modules.clients.models import Client, Style
from app.modules.production.models import Operation, OperationAccess
from app.modules.wages.models import Rate
from app.modules.attendance.models import AttendanceLog, ShiftConfig  # noqa: F401
from app.modules.procurement import models as _procurement  # noqa: F401  (register tables)
from app.modules.procurement.seed_templates import seed_client_templates
from app.modules.procurement.seed_stage2 import seed_stage2
from app.modules.procurement.seed_inventory import seed_inventory

# Import engine (sync) for the spreadsheets.
from app.modules.imports.import_engine import build_preview
from app.modules.imports.parse_orders import parse_order_sheet
from app.modules.imports.load_to_db import (
    load_preview, _get_or_create_client, _get_or_create_order,
    _get_or_create_style, _upsert_sku,
)

# ── Reference data ────────────────────────────────────────────────────────
GARMENT_FILE = "data/GARMENT_ORDERPRODUCTION_DETAILS.xlsx"
EMPLOYEES_FILE = "data/employees_detail.xlsx"
JOHNPETER_FILE = "data/johnpeter.xlsx"

OPS = [("CUTTING", "Cutting", 1), ("FUSING", "Fusing", 2), ("PASTING", "Pasting", 3),
       ("SHELL", "Shell stitch", 4), ("LA", "Lining attach", 5),
       ("LS", "Lining stitch", 6), ("FF", "Final finish", 7)]

# Which manager role may log which operation (config, not hardcoded in code).
ACCESS = {
    UserRole.CUTTING_MANAGER.value: ["CUTTING"],
    UserRole.STITCHING_MANAGER.value: ["FUSING", "PASTING", "SHELL", "LA", "LS", "FF"],
}

# Map each spreadsheet client key to a friendly name + country (best-effort).
CLIENT_META = {
    "KJ": ("Khawaja (KJ)", "Pakistan"),
    "GGZ": ("GGZ SRL", "Italy"),
    "NIPAL": ("Nipal", "Italy"),
    "RICANO": ("Ricano", "Spain"),
    "JP": ("JP Garments", "India"),
    "NIPAL-NEW": ("Nipal (New)", "Italy"),
}

DEFAULT_RATE_DATE = date(2026, 1, 1)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", ".", name.strip().lower()).strip(".")


# ── Step 1: operations + access ─────────────────────────────────────────────
def seed_operations(db: Session) -> dict[str, Operation]:
    ops: dict[str, Operation] = {}
    for code, label, seq in OPS:
        o = db.scalar(select(Operation).where(Operation.code == code))
        if not o:
            o = Operation(code=code, label=label, sequence=seq)
            db.add(o); db.flush()
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


# ── Step 2: client orders from both workbooks ───────────────────────────────
def seed_orders(db: Session) -> None:
    # Multi-sheet workbook (6 clients) via the auto-detecting import engine.
    # We keep the raw client keys (KJ, GGZ, NIPAL, RICANO, JP, NIPAL-NEW) as the
    # stored client names so the importer's get-or-create-by-name stays idempotent
    # across re-runs. The friendly names live in CLIENT_META for display use.
    preview = build_preview(GARMENT_FILE)
    country_map = {k: meta[1] for k, meta in CLIENT_META.items()}
    load_preview(db, preview, country_map=country_map, replace=True)
    db.commit()

    # John Peter — a flat single-sheet order (Date|Style|Suede Colour|Article|sizes).
    wb = openpyxl.load_workbook(JOHNPETER_FILE, data_only=True)
    lines, _ = parse_order_sheet(wb.active)
    jp = _get_or_create_client(db, "John Peter", "Italy")
    order = _get_or_create_order(db, jp, "JOHNPETER-PO")
    for line in lines:
        style = _get_or_create_style(db, order, line.style, line.article)
        for size, qty in line.sizes.items():
            _upsert_sku(db, style, line.color, size, qty)
    db.commit()


# ── Step 3: employees with mocked contact details ───────────────────────────
def seed_employees(db: Session) -> list[Employee]:
    """The file has two blocks separated by 'per pieces based salary':
    above = MONTHLY staff, below = PIECE_RATE workers."""
    wb = openpyxl.load_workbook(EMPLOYEES_FILE, data_only=True)
    ws = wb.active
    piece_rate_mode = False
    seeded: list[Employee] = []
    idx = 0
    for r in range(1, ws.max_row + 1):
        b = ws.cell(r, 2).value
        if isinstance(b, str) and "per pieces" in b.lower():
            piece_rate_mode = True
            continue
        name = ws.cell(r, 2).value
        desig = ws.cell(r, 3).value
        sno = ws.cell(r, 1).value
        if not name or not isinstance(sno, (int, float)) or not desig:
            continue
        name = str(name).strip()
        existing = db.scalar(select(Employee).where(Employee.name == name))
        if existing:
            seeded.append(existing)
            continue
        idx += 1
        emp = Employee(
            name=name,
            designation=str(desig).strip(),
            wage_type=WageType.PIECE_RATE if piece_rate_mode else WageType.MONTHLY,
            monthly_salary=None if piece_rate_mode else 0,
            phone=f"910000{idx:04d}",                 # MOCKED
            email=f"{_slug(name)}@factory.local",     # MOCKED
        )
        db.add(emp); db.flush()
        seeded.append(emp)
    db.commit()
    return seeded


# ── Step 4: piece-rate rates from production cards ───────────────────────────
def seed_rates(db: Session, ops: dict[str, Operation]) -> int:
    """Read RATE rows from the production cards and attach to matching styles.
    The import engine already parsed these; we re-read for rate attachment."""
    preview = build_preview(GARMENT_FILE)
    # Normalise card op codes (e.g. 'L/A' -> 'LA', 'LINING STICH' -> 'LS').
    code_norm = {"L/A": "LA", "LINING STICH": "LS", "FF-SAMPLE": "FF",
                 "FF-SMS": "FF", "LINING STITCH": "LS"}
    count = 0
    seen: set = set()        # (style_id, op_id) inserted this run
    for key, cp in preview.clients.items():
        for card in cp.production_cards:
            tprefix = card.title.split("-")[0].strip().upper()
            ref_style = None
            for st in db.scalars(select(Style)):
                if st.name.upper() in card.title.upper() or tprefix in st.name.upper():
                    ref_style = st
                    break
            if not ref_style:
                continue
            for raw_code, rate_val in card.rate.items():
                if not rate_val:
                    continue
                code = code_norm.get(raw_code.upper(), raw_code.upper())
                op = ops.get(code)
                if not op:
                    continue
                pair = (ref_style.id, op.id)
                if pair in seen:
                    continue
                exists = db.scalar(select(Rate).where(
                    Rate.style_id == ref_style.id, Rate.operation_id == op.id,
                    Rate.effective_from == DEFAULT_RATE_DATE))
                if not exists:
                    db.add(Rate(style_id=ref_style.id, operation_id=op.id,
                                rate=rate_val, effective_from=DEFAULT_RATE_DATE))
                    db.flush()      # make visible to the next lookup
                    count += 1
                seen.add(pair)
    db.commit()
    return count


# ── Step 5: user logins for staff, employees, and clients ───────────────────
def _make_user(db: Session, *, name: str, phone: str, role: UserRole,
               email: str | None = None, employee_id=None, client_id=None) -> User:
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
        password_hash=get_password_hash(phone),   # password == phone
        is_active=True, must_change_password=True,  # force reset on first login
        employee_id=employee_id, client_id=client_id,
    )
    db.add(u); db.flush()
    return u


def seed_users(db: Session, employees: list[Employee]) -> dict[str, int]:
    stats = {"staff": 0, "employees": 0, "clients": 0}

    # Management / viewer accounts. MANAGING_DIRECTOR is the new superuser / BOM
    # approver; DIRECT_MANAGER stays as operational lead (still bypasses role gates
    # during the MD transition — see users/deps.py::SUPERUSER_ROLES).
    staff = [
        ("Managing Director", "9000000000", UserRole.MANAGING_DIRECTOR),
        ("Direct Manager", "9000000001", UserRole.DIRECT_MANAGER),
        ("Cutting Manager", "9000000002", UserRole.CUTTING_MANAGER),
        ("Stitching Manager", "9000000003", UserRole.STITCHING_MANAGER),
        ("Office Viewer", "9000000004", UserRole.VIEWER),
        ("HR / Accounts", "9000000005", UserRole.HR),
    ]
    for nm, ph, role in staff:
        _make_user(db, name=nm, phone=ph, role=role,
                   email=f"{_slug(nm)}@factory.local")
        stats["staff"] += 1

    # One login per employee.
    for emp in employees:
        if not emp.phone:
            continue
        _make_user(db, name=emp.name, phone=emp.phone, role=UserRole.EMPLOYEE,
                   email=emp.email, employee_id=emp.id)
        stats["employees"] += 1

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


def main():
    # Dev safety: ensure tables exist. Production uses Alembic.
    Base.metadata.create_all(engine)
    db = SessionLocal()
    try:
        ops = seed_operations(db)
        seed_orders(db)
        employees = seed_employees(db)
        n_rates = seed_rates(db, ops)
        user_stats = seed_users(db, employees)
        n_templates = seed_client_templates(db)   # Stage-1 validation registry (§4)
        stage2_stats = seed_stage2(db)             # Stage-2 garment types + POM dict (§3)
        stage4_stats = seed_inventory(db)          # Stage-4 aliases + uom + inventory master

        n_clients = db.scalar(select(__import__("sqlalchemy").func.count(Client.id)))
        n_styles = db.scalar(select(__import__("sqlalchemy").func.count(Style.id)))
        print("─" * 60)
        print("✅ SEED COMPLETE")
        print(f"   operations : {len(ops)}")
        print(f"   clients    : {n_clients}")
        print(f"   styles     : {n_styles}")
        print(f"   employees  : {len(employees)}")
        print(f"   rates      : {n_rates}")
        print(f"   templates  : {n_templates}")
        print(f"   stage2     : {stage2_stats}")
        print(f"   stage4     : {stage4_stats}")
        print(f"   users      : {user_stats}")
        print("─" * 60)
        print("   Login (Swagger Authorize / POST /api/v1/auth/login):")
        print("     username=9000000000  password=9000000000  (managing director / superuser)")
        print("     username=9000000001  password=9000000001  (direct manager)")
        print("   All seeded users must change password on first login.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
