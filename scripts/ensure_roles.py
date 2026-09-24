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
    role the Stage-2/3 workflow gates on, and change nothing else.

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


# The roles the BOM / procurement workflow gates on, and the fallback login to
# create for each when --apply is passed. Phone IS the username
# (UserRepository.get_by_username queries User.phone); password defaults to the
# same string, as everywhere else in this codebase's seeding.
REQUIRED_ROLES: list[tuple[UserRole, str, str]] = [
    # role,                          phone/username, display name
    (UserRole.MANAGING_DIRECTOR,     "9000000000",   "Managing Director"),
    (UserRole.DIRECT_MANAGER,        "9000000001",   "Direct Manager"),
    (UserRole.CUTTING_MANAGER,       "9000000002",   "Cutting Manager"),
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
    print("LOGIN AUDIT - roles the BOM Stage-2/3 endpoints gate on")
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
        print("Endpoints unreachable by anyone while a role is missing:")
        if UserRole.MANAGING_DIRECTOR in missing:
            print("  POST /api/v1/procurement/boms/{id}/approve")
            print("  POST /api/v1/procurement/boms/{id}/reject")
            print("  POST /api/v1/procurement/boms/{id}/export")
        if UserRole.CUTTING_MANAGER in missing:
            print("  (cutting-manager reads/edits still work for DM/MD as superusers)")
        print("-" * 72)
    return missing


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
    args = ap.parse_args()

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
