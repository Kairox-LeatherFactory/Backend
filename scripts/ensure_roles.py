"""
================================================================================
scripts/ensure_roles.py - audit & repair the LOGIN accounts the workflows need
================================================================================

ADDED 2026-09-17 (Hamthan).

WHY THIS EXISTS, AND WHY IT IS NOT scripts/seed.py
    Two BOM bugs reported on 2026-09-17 turned out not to be bugs in the BOM code
    at all:

      "md did nt have a login to see a bom"
      "cm doesnt have a access for see a bom, backend response is hr role is
       not permitted"

    A `select role, count(*) from app_user group by role` on the configured
    database returns:

        DIRECT_MANAGER 1 , CUTTING_MANAGER 1 , STITCHING_MANAGER 2 , HR 2
        LINING_MANAGER 1 , SECURITY 1 , MERCHANDISER 1

    There is NO managing_director row. Every /boms/{id}/approve|reject|export
    call is MD-gated, so with no MD account those endpoints are unreachable by
    anyone - exactly the symptom. And the "cutting manager" token being used was
    in fact one of the two HR logins, which is why the 403 said 'hr'.

    scripts/seed.py cannot fix this. It is a DEMO seeder: running it against
    this database would insert its whole fixture set (operations, clients,
    styles, employees, rates, 9000000xxx staff) alongside the real records. And
    its _make_user() is idempotent-on-phone in a way that returns an existing
    row untouched, so it can never correct a role either.

    This script does one narrow thing: make sure a usable login exists for each
    role that GATES A ROUTE, and change nothing else.

    WIDENED 2026-09-28. It audited three roles (MD / DM / CUTTING_MANAGER)
    because the reported bugs were BOM ones, but the defect it repairs is not a
    BOM property — it is "a role gates routes, no active login holds it, so
    those routes are unreachable and the 403 names the wrong role". Seven more
    roles gate 331 routes between them, SECURITY and LINING_MANAGER among them.
    See the comment on REQUIRED_ROLES for the counts and for the two roles
    (CLIENT, MERCHANDISER) deliberately left out because they gate nothing.

USAGE
    python scripts/ensure_roles.py                      # audit only, writes nothing
    python scripts/ensure_roles.py --apply              # create the MISSING accounts
    python scripts/ensure_roles.py --promote 9840889486 --to managing_director

    --promote is the option to prefer in anything but a dev box: it hands the MD
    role to a REAL person who already has a login, instead of minting a shared
    account whose password equals its username. `--apply` exists for dev/QA,
    where a throwaway MD login is the point; it prints the credentials it
    created and sets must_change_password so the account nags on first use.

SAFETY
    - Never deletes, never deactivates, never touches a password of an existing
      user, and never changes an existing user's role unless you name that user
      with --promote.
    - --apply only INSERTS accounts for roles that have no active user at all.
    - Without --apply or --promote it is strictly read-only.
================================================================================
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.orm import Session                               # noqa: E402

from scripts import _dbguard
from app.core.database import SessionLocal                       # noqa: E402
from app.core.enums import UserRole                              # noqa: E402
from app.core.security import get_password_hash                  # noqa: E402
from app.modules.users.models import User                        # noqa: E402

# User's mapper can't configure until every model that participates in a
# relationship string ('Submission', 'Client', ...) has been imported - same
# import block scripts/seed.py carries, and for the same reason.
from app.core import models as _core_models                      # noqa: E402,F401
from app.modules.employees.models import Employee                # noqa: E402,F401
from app.modules.clients.models import Client                    # noqa: E402,F401
from app.modules.production.models import Operation              # noqa: E402,F401
from app.modules.wages.models import Rate                        # noqa: E402,F401
from app.modules.attendance.models import AttendanceLog          # noqa: E402,F401
from app.modules.procurement import models as _procurement       # noqa: E402,F401
from app.modules.bom import models as _bom                       # noqa: E402,F401
from app.modules.inventory import models as _inv                 # noqa: E402,F401
from app.modules.supplier_po import models as _spo               # noqa: E402,F401


# ─────────────────────────────────────────────────────────────────────────────
# EVERY ROLE THAT GATES A ROUTE, not just the BOM three.
#
# This list was MD / DM / CUTTING_MANAGER, because the two bugs that prompted the
# script were BOM ones. That scope was wrong the moment it was written: the
# failure being repaired here is "a role gates routes and no active login holds
# it, so those routes are unreachable by anyone and the 403 names whichever role
# the tester actually had". That is not a BOM property. Counting the role gates
# off the live dependency graph:
#
#     DIRECT_MANAGER 188 · MANAGING_DIRECTOR 186 · HR 96 · CUTTING_MANAGER 83
#     LINING_MANAGER 65 · STITCHING_MANAGER 60 · STORE_MANAGER 47
#     SUPERVISOR 43 · SECURITY 20 · VIEWER 9
#
# so seven roles gating 331 routes between them were absent from the audit —
# including SECURITY, which is the ONLY role that can work the attendance gate,
# and LINING_MANAGER, which is the only one that can log the lining cut path.
# Both are Phase-1 additions (CLAUDE.md §3) and both were among the roles the
# native-enum case bug made unusable (§13), so they are exactly the ones worth
# checking on a real database.
#
# CLIENT and MERCHANDISER are deliberately ABSENT: they gate nothing. Phase 1
# gives a buyer nothing to do inside the app and `POST /users/clients` was
# removed, so minting a CLIENT login would create an account with no reachable
# endpoint.
#
# THE PHONE PER ROLE MATCHES scripts/seed.py::seed_users EXACTLY. That is the
# point of restating them rather than picking fresh numbers: on a database that
# seed.py has already filled, `--apply` must find the role held and report OK,
# not mint a second login for it under a different number. Phone IS the username
# (UserRepository.get_by_username queries User.phone); password defaults to the
# same string, as everywhere else in this codebase's seeding.
# ─────────────────────────────────────────────────────────────────────────────
REQUIRED_ROLES: list[tuple[UserRole, str, str]] = [
    # role,                          phone/username, display name
    (UserRole.MANAGING_DIRECTOR,     "9000000000",   "Managing Director"),
    (UserRole.DIRECT_MANAGER,        "9000000001",   "Direct Manager"),
    (UserRole.CUTTING_MANAGER,       "9000000002",   "Cutting Manager"),
    (UserRole.STITCHING_MANAGER,     "9000000003",   "Stitching Manager"),
    (UserRole.VIEWER,                "9000000004",   "Office Viewer"),
    (UserRole.HR,                    "9000000005",   "HR / Accounts"),
    (UserRole.LINING_MANAGER,        "9000000006",   "Lining Manager"),
    (UserRole.SECURITY,              "9000000007",   "Security Gate"),
    (UserRole.SUPERVISOR,            "9000000008",   "Floor Supervisor"),
    # Username, not a phone — the client asked for this account by name, and
    # seed.py creates it the same way. See its comment there.
    #
    # NOTE the password differs from seed.py's on purpose. seed.py sets the
    # specified `STORE` with must_change_password=False, because that is the
    # credential the client asked for in a DEMO database. Here the password is
    # the username and must_change_password is True, like every other row in
    # this list: this script runs against REAL databases, and seeding a weak,
    # well-known, never-expiring credential onto a factory network is not a
    # repair. `--promote` an existing person's login if you would rather not
    # mint a shared account at all.
    (UserRole.STORE_MANAGER,         "STOREMANAGER", "Store Manager"),
]


def audit(db: Session) -> dict[UserRole, list[User]]:
    """Active logins per required role."""
    found: dict[UserRole, list[User]] = {}
    for role, _phone, _name in REQUIRED_ROLES:
        rows = list(db.scalars(
            select(User).where(User.role == role, User.is_active.is_(True))
            .order_by(User.phone)
        ))
        found[role] = rows
    return found


def report(found: dict[UserRole, list[User]]) -> list[UserRole]:
    missing: list[UserRole] = []
    print("-" * 72)
    print("LOGIN AUDIT - every role that gates a route")
    print("-" * 72)
    for role, _phone, _name in REQUIRED_ROLES:
        rows = found[role]
        if rows:
            who = ", ".join(f"{u.phone} ({u.name})" for u in rows)
            print(f"  OK       {role.value:<18} {len(rows)} active - {who}")
        else:
            missing.append(role)
            print(f"  MISSING  {role.value:<18} no active login")
    print("-" * 72)
    if missing:
        print("What is unreachable by anyone while a role is missing:")
        for role in missing:
            for line in _BLOCKED.get(role, ("(no note recorded)",)):
                print(f"  {role.value:<18} {line}")
        if {UserRole.MANAGING_DIRECTOR, UserRole.DIRECT_MANAGER} & set(missing):
            print()
            print("  NOTE: MD and DM bypass the role gate (users/deps.py::")
            print("        SUPERUSER_ROLES), so while one of them exists most")
            print("        manager screens are still reachable — by the wrong")
            print("        person. A green screen under a DM token proves the")
            print("        bypass works, never that the role gate does.")
        print("-" * 72)
    return missing


# What a missing role actually costs, in the factory's terms. A bare "MISSING
# security" does not tell the person running this that nobody can check a worker
# in; naming the consequence is the whole value of the audit.
_BLOCKED: dict[UserRole, tuple[str, ...]] = {
    UserRole.MANAGING_DIRECTOR: (
        "POST /procurement/boms/{id}/approve|reject|export (MD-gated)",
        "POST /store/substitutions/{id}/approve|reject — the wrong-size queue",
    ),
    UserRole.DIRECT_MANAGER: (
        "breakdown upload + RELEASE, the cutting-row approval,",
        "POST /store/substitutions/{id}/approve|reject, the material spec",
    ),
    UserRole.CUTTING_MANAGER: (
        "the leather cut log and the cutting grid's hide allocation",
    ),
    UserRole.LINING_MANAGER: (
        "the LINING cut path — nothing else can log LINING_CUTTING,",
        "so no lined garment can ever reach store completeness",
    ),
    UserRole.STITCHING_MANAGER: (
        "every post-cut floor stage: fusing, pasting, line- and",
        "shell-stitching, final finish",
    ),
    UserRole.STORE_MANAGER: (
        "POST /store/scan and /store/send — the merge itself, so no",
        "garment can be released into line-stitching",
    ),
    UserRole.HR: (
        "employees, the wage runs and the designation backfill",
    ),
    UserRole.SECURITY: (
        "the attendance gate. SECURITY / HR / MD / DM are the only",
        "operators (core.enums.ATTENDANCE_OPERATOR_ROLES), and with no",
        "attendance there is no production logging — the floor is closed",
    ),
    UserRole.SUPERVISOR: (
        "the floor roster reads (no attendance writes by design)",
    ),
    UserRole.VIEWER: (
        "the read-only office screens",
    ),
}


def create_missing(db: Session, missing: list[UserRole]) -> int:
    made = 0
    for role, phone, name in REQUIRED_ROLES:
        if role not in missing:
            continue
        clash = db.scalar(select(User).where(User.phone == phone))
        if clash is not None:
            print(f"  SKIP     {role.value}: username {phone} is already taken by "
                  f"{clash.name} ({clash.role.value}). Use --promote to change a "
                  f"role deliberately.")
            continue
        db.add(User(
            name=name, phone=phone, email=None, role=role,
            password_hash=get_password_hash(phone),
            is_active=True, must_change_password=True,
        ))
        made += 1
        print(f"  CREATED  {role.value:<18} username={phone}  password={phone}")
    if made:
        db.commit()
    return made


def promote(db: Session, phone: str, to_role: UserRole) -> int:
    user = db.scalar(select(User).where(User.phone == phone))
    if user is None:
        print(f"  ERROR    no user with username {phone}")
        return 1
    if user.role == to_role:
        print(f"  NO-OP    {phone} ({user.name}) is already {to_role.value}")
        return 0
    was = user.role.value
    user.role = to_role
    db.commit()
    print(f"  PROMOTED {phone} ({user.name}): {was} -> {to_role.value}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="create a fallback login for each MISSING role")
    ap.add_argument("--promote", metavar="USERNAME",
                    help="give an existing login a different role")
    ap.add_argument("--to", metavar="ROLE",
                    help="the role for --promote, e.g. managing_director")
    _dbguard.add_argument(ap)
    args = ap.parse_args()

    # Writes rows, so it must be sure WHICH database it is writing to.
    # ASYNC_DATABASE_URL overrides DATABASE_URL outright
    # (config.effective_async_url), so the two can silently name
    # different databases — that is how 42 people reached live Supabase
    # on 2026-09-28. See scripts/_dbguard.py.
    _dbguard.assert_one_database("ensure_roles.py",
                                 allow_split=args.allow_split_db)

    db = SessionLocal()
    try:
        missing = report(audit(db))

        if args.promote:
            if not args.to:
                print("--promote requires --to <role>")
                return 2
            try:
                to_role = UserRole(args.to)
            except ValueError:
                print(f"unknown role '{args.to}'. Valid: "
                      f"{', '.join(r.value for r in UserRole.login_roles())}")
                return 2
            return promote(db, args.promote, to_role)

        if not missing:
            print("Nothing to do - every required role has an active login.")
            return 0

        if not args.apply:
            print("Read-only run. Re-run with --apply to create the missing accounts,")
            print("or --promote <username> --to <role> to use a real person's login.")
            return 0

        print("Creating missing accounts:")
        made = create_missing(db, missing)
        print("-" * 72)
        print(f"{made} account(s) created. They must change password on first login.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
