"""
================================================================================
scripts/seed_employees.py — Seed the real payroll: people + cards + staff logins
================================================================================
RUN:  python -m scripts.seed_employees        (from the backend root)

WHAT THIS LOADS
  Every person from the 20-07-2026 payroll sheet becomes an `employee` row AND
  gets an EMPLOYEE BARCODE (EMP-000001, ...). The card is not optional: since
  workers hold no login, the barcode is the ONLY way a person is identified on
  the floor. No card -> nothing can scan them -> `is_present_today()` is false
  -> production can't be logged against them. A roster without cards is a
  roster you cannot use.

  STAFF (managers / HR / security / MD / merchandiser) get the same employee row
  and the same card, PLUS a linked `app_user` login (User.employee_id -> the
  employee). They are on the payroll like everyone else, so their salary lives
  on employee.monthly_salary — app_user has no salary column and does not need
  one.

  WORKERS get the employee row and the card, and NOTHING in app_user. Shop-floor
  workers are not given system access (see UserRole.login_roles()); SECURITY /
  HR / MD / DM scan them in and out.

IDEMPOTENT — RE-RUN IT FREELY
  Employees are matched case-insensitively on name (matching the DB's unique
  index on lower(name)), logins on phone, and the card is a TOP-UP: an employee
  who already exists but has no active barcode gets one. So a second run creates
  nothing, and a run after a partial failure repairs it.

PASSWORDS
  Default to the person's phone (digits only), bcrypt-hashed, with
  must_change_password=True — the convention used by scripts/seed.py.

WHY ASYNC (scripts/seed.py is sync)
  BarcodeService and UserService are async-only, and this script reuses them
  rather than re-implementing code minting and password/duplicate handling.

PREREQUISITE
  alembic upgrade head — the 'security' and 'merchandiser' values must exist on
  the native `user_role` PG type (20260804_role_values). Without it, those
  logins fail on Postgres.
================================================================================
"""
import asyncio
import csv
import logging
import re
from pathlib import Path

from sqlalchemy import func, select

from app.core.database import AsyncSessionLocal
from app.core.enums import Designation, UserRole, WageType
from app.modules.barcode.repository import BarcodeRepository
from app.modules.barcode.service import BarcodeService
from app.modules.employees.models import Employee
from app.modules.users.models import User
from app.modules.users.schemas import UserCreate
from app.modules.users.service import UserService

# EVERY model module must be imported before the first ORM operation, not just
# the two tables this script writes (CLAUDE.md §11). SQLAlchemy resolves
# relationships by CLASS NAME at configure_mappers() time, which fires on the
# first query — so a half-registered registry fails with
# "expression 'Submission' failed to locate a name", and it fails on the
# EMPLOYEE insert, which never mentions Submission. Same block as
# scripts/seed.py:70-81; keep them in step.
from app.core import models as _core_models            # noqa: F401
from app.modules.clients import models as _clients     # noqa: F401
from app.modules.production import models as _prod     # noqa: F401
from app.modules.wages import models as _wages         # noqa: F401
from app.modules.attendance import models as _att      # noqa: F401
from app.modules.barcode import models as _barcode     # noqa: F401
from app.modules.procurement import models as _proc    # noqa: F401
from app.modules.bom import models as _bom             # noqa: F401
from app.modules.inventory import models as _inv       # noqa: F401
from app.modules.supplier_po import models as _spo     # noqa: F401

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed_employees")

CARDS_CSV = Path("data/employee_cards.csv")


def digits(phone) -> str | None:
    """'+91 98408-89486' -> '9840889486'. Phones are the login username."""
    if not phone:
        return None
    d = re.sub(r"\D", "", str(phone))
    return d or None


# ══════════════════════════════════════════════════════════════════════════
# STAFF — employee row + card + LINKED LOGIN.
# Add a new login by adding one dict here; nothing else changes.
# ══════════════════════════════════════════════════════════════════════════
STAFF = [
    dict(name="PUNDARI PRASAD BARAL", designation="SECURITY",          salary=23100, phone="7302905187", email=None,                     role=UserRole.SECURITY),
    dict(name="Abirami Arivazhagan",  designation="HR",                salary=25000, phone="8072753495", email="admin@ptexports.com",    role=UserRole.HR),
    dict(name="JAYASRI",              designation="HR",                salary=16000, phone="7603956073", email=None,                     role=UserRole.HR),
    dict(name="MOHAMMED TANZEEL",     designation="MERCHANDISER",      salary=32500, phone="9941346771", email="tanzeel@ptexports.com",  role=UserRole.MERCHANDISER),
    dict(name="Zahoor Ahmed C",       designation="MANAGER",           salary=69190, phone="9840889486", email="garments@ptexports.com", role=UserRole.DIRECT_MANAGER),
    dict(name="K Niyamathullah",      designation="STITCHING_MANAGER", salary=35750, phone="9629542708", email=None,                     role=UserRole.STITCHING_MANAGER),
    dict(name="Apsar Khan J",         designation="STITCHING_MANAGER", salary=40000, phone="9843430881", email=None,                     role=UserRole.STITCHING_MANAGER),
    dict(name="Ragavan L",            designation="CUTTING_MANAGER",   salary=47465, phone="8825935258", email=None,                     role=UserRole.CUTTING_MANAGER),
    # NOTE: no salary on record for the two MDs. They are stored MONTHLY with a
    # NULL salary, which the wage run reports as `monthly_salary_missing`
    # (wages/service.py:479) rather than paying a 0.00 payslip. Correct, but it
    # will flag on EVERY run until a figure is filled in.
]


# ══════════════════════════════════════════════════════════════════════════
# WORKERS — employee row + card, NO login.
# (name, designation, wage_type, monthly_salary)
#
# Designations run through Designation.normalise and use the catalogued
# vocabulary that drives the production skill gate. An uncatalogued title
# (ACCOUNTANT, CHEMICAL_TECHNICIAN) is kept as-is and fails OPEN on the gate.
# ══════════════════════════════════════════════════════════════════════════
WORKERS = [
    # ---- MONTHLY ----
    ("A Raheena",            "TAILOR",              WageType.MONTHLY, 14375),
    ("GOMATHI",              "TAILOR",              WageType.MONTHLY, 12500),
    ("H S VIJAYA",           "TRIMMER",             WageType.MONTHLY, 10238),
    ("Varalakshmi",          "TRIMMER",             WageType.MONTHLY, 9713),
    ("Kotikala Yogendramma", "TRIMMER",             WageType.MONTHLY, 9713),
    ("FAIZAN",               "HELPER",              WageType.MONTHLY, 15100),
    ("MD Afzal",             "CUTTER",              WageType.MONTHLY, 22550),
    ("Nasuruddin.C",         "LINING_CUTTER",       WageType.MONTHLY, 25322),
    ("AHSHAN ALI ANSARI",    "CHEMICAL_TECHNICIAN", WageType.MONTHLY, 34000),
    # These two also appear in the draft's commented-out login block. They are
    # seeded as WORKERS ONLY (no login). Move them to STAFF with a role if they
    # need to log in.
    ("YESUPATHAM.J",         "QC_INSPECTOR",        WageType.MONTHLY, 35000),
    ("Amjad Khan",           "ACCOUNTANT",          WageType.MONTHLY, 15000),
    # ---- PIECE_RATE ----
    ("MAHARANI",             "FUSER",               WageType.PIECE_RATE, None),
    ("AMUTHA",               "PASTER",              WageType.PIECE_RATE, None),
    ("DIVYA.S",              "PASTER",              WageType.PIECE_RATE, None),
    ("BOOPATHI",             "TAILOR",              WageType.PIECE_RATE, None),
    ("PRAKASH",              "TAILOR",              WageType.PIECE_RATE, None),
    ("GANESH",               "TAILOR",              WageType.PIECE_RATE, None),
    ("VADIVELU",             "TAILOR",              WageType.PIECE_RATE, None),
    ("SUNDARAM.D",           "TAILOR",              WageType.PIECE_RATE, None),
    ("MD ALI",               "TAILOR",              WageType.PIECE_RATE, None),
    ("BALA",                 "TAILOR",              WageType.PIECE_RATE, None),
    ("RAHIM.I",              "TAILOR",              WageType.PIECE_RATE, None),
    ("MD RAKIM",             "TAILOR",              WageType.PIECE_RATE, None),
    ("NAIYER ALAM",          "TAILOR",              WageType.PIECE_RATE, None),
    ("MD ISHTIYAQUE",        "TAILOR",              WageType.PIECE_RATE, None),
    ("JAKIR HUSSAIN",        "TAILOR",              WageType.PIECE_RATE, None),
    ("JAMEELUDDIN",          "TAILOR",              WageType.PIECE_RATE, None),
    ("MUJAHID",              "TAILOR",              WageType.PIECE_RATE, None),
    ("Sarware Alam Ansari",  "TAILOR",              WageType.PIECE_RATE, None),
    ("Asagar Ali",           "TAILOR",              WageType.PIECE_RATE, None),
    ("PRABHAKAR.V",          "CUTTER",              WageType.PIECE_RATE, None),
    ("ASMATH",               "CUTTER",              WageType.PIECE_RATE, None),
    ("MASJID",               "CUTTER",              WageType.PIECE_RATE, None),
    ("NASREEN",              "CUTTER",              WageType.PIECE_RATE, None),
]


async def _ensure_person(db, *, name, designation, wage_type, salary,
                         phone=None, email=None, role=None) -> dict:
    """Employee + card (+ login when `role` is set), all idempotent.

    NOTE — why not EmployeeService.create(): its EmployeeCreate schema refuses
    DIRECT_MANAGER / MANAGING_DIRECTOR (an API grant-authority rule, and three
    of the staff hold those roles), and its _unique_name would store a second
    run as 'IN-CHAL <name>' instead of skipping. A seed with direct DB access is
    legitimately outside those two API-surface rules. Everything else — code
    minting, password hashing, duplicate checks — is reused, not re-implemented.
    """
    out = {"name": name, "emp_new": False, "card_new": False, "login_new": False}

    # ── 1. employee row (matched the way the unique index matches) ───────────
    emp = await db.scalar(
        select(Employee).where(func.lower(Employee.name) == name.strip().lower())
    )
    if emp is None:
        emp = Employee(
            name=name.strip(),
            designation=Designation.normalise(designation),
            wage_type=wage_type,
            monthly_salary=(float(salary) if salary is not None else None),
            phone=digits(phone),
            email=email,
            is_active=True,
        )
        db.add(emp)
        await db.flush()
        out["emp_new"] = True

    # ── 2. card TOP-UP — also repairs an employee an earlier run left cardless ─
    card = await BarcodeRepository(db).get_for_employee(emp.id, active_only=True)
    if card is None:
        code = await BarcodeService(db).issue_employee_barcode_nocommit(emp.id, emp.name)
        out["card_new"] = True
    else:
        code = card.code

    # ── 3. login — STAFF only ───────────────────────────────────────────────
    ph = digits(phone)
    if role is not None and ph:
        existing = await db.scalar(select(User).where(User.phone == ph))
        if existing is None:
            await UserService(db).provision_user(
                UserCreate(name=emp.name, phone=ph, email=email, role=role,
                        password=ph, employee_id=emp.id),
                must_change_password=True,
            )
            out["login_new"] = True
        elif existing.employee_id is None:
            # login predates the employee row — link them rather than duplicate
            existing.employee_id = emp.id
    elif role is not None:
        log.warning("  ! %s has role %s but no phone — login SKIPPED",
                    name, role.value)

    await db.commit()
    out.update(designation=emp.designation, wage_type=emp.wage_type.value,
               barcode=code, role=(role.value if role else ""))
    return out


async def seed() -> None:
    rows: list[dict] = []
    emp_new = card_new = login_new = failed = 0

    async with AsyncSessionLocal() as db:
        log.info("── STAFF (employee + card + login) ──")
        for s in STAFF:
            try:
                r = await _ensure_person(db, wage_type=WageType.MONTHLY, **s)
            except Exception as exc:                    # noqa: BLE001 — seed resilience
                await db.rollback()
                failed += 1
                log.error("  x %-24s FAILED: %s", s["name"], exc)
                continue
            rows.append(r)
            emp_new += r["emp_new"]; card_new += r["card_new"]; login_new += r["login_new"]
            log.info("  %s %-24s %-18s %-10s %s",
                     "+" if r["emp_new"] else "=", r["name"], r["designation"],
                     r["barcode"], r["role"])

        log.info("── WORKERS (employee + card, no login) ──")
        for name, desig, wage, salary in WORKERS:
            try:
                r = await _ensure_person(db, name=name, designation=desig,
                                         wage_type=wage, salary=salary)
            except Exception as exc:                    # noqa: BLE001
                await db.rollback()
                failed += 1
                log.error("  x %-24s FAILED: %s", name, exc)
                continue
            rows.append(r)
            emp_new += r["emp_new"]; card_new += r["card_new"]
            log.info("  %s %-24s %-18s %-10s %s",
                     "+" if r["emp_new"] else "=", r["name"], r["designation"],
                     r["barcode"], r["wage_type"])

    # ── printable card list ─────────────────────────────────────────────────
    CARDS_CSV.parent.mkdir(parents=True, exist_ok=True)
    with CARDS_CSV.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["name", "designation", "wage_type", "barcode", "role"])
        for r in rows:
            w.writerow([r["name"], r["designation"], r["wage_type"],
                        r["barcode"], r["role"]])

    log.info("")
    log.info("── DONE ── %d people processed, %d failed", len(rows), failed)
    log.info("   employees : %d new / %d already present", emp_new, len(rows) - emp_new)
    log.info("   cards     : %d minted / %d already held one", card_new, len(rows) - card_new)
    log.info("   logins    : %d created (staff only)", login_new)
    log.info("   cards CSV : %s  (print Code128 from the `barcode` column)", CARDS_CSV)
    log.info("   Default password = phone (digits). must_change_password=True.")
    log.info("")
    log.info("   REMINDER: the two MDs have no salary on record — every wage run "
             "will flag them as monthly_salary_missing until one is set.")
    log.info("   REMINDER: no route grants MERCHANDISER access yet — Tanzeel can "
             "log in, but every role gate will 403 until that is decided.")


if __name__ == "__main__":
    asyncio.run(seed())
