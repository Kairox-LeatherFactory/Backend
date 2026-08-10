"""
scripts/gen_drawer_barcodes.py — EMERGENCY: create the 200 drawer barcodes NOW.

Creates 200 permanent, barcoded, WAITING drawers (DRW-0001 … DRW-0200), each
with one active DRAWER-type row in barcode_registry. Idempotent: run it twice and
the second run does nothing. Never deletes anything.

RUN (from the repo root, same venv you run Alembic in):

    python -m scripts.gen_drawer_barcodes

or:

    python scripts/gen_drawer_barcodes.py

It uses settings.database_url (the sync psycopg2 URL Alembic already uses), so if
`alembic upgrade head` works, this works.
"""
from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.modules.imports.premint import bootstrap_drawer_pool

# EVERY model module must be imported before the first ORM operation, not just
# the two tables this script writes (CLAUDE.md §11). SQLAlchemy resolves
# relationships by CLASS NAME at configure_mappers() time, which fires on the
# first query — so a half-registered registry fails with
# "expression 'Submission' failed to locate a name", and it fails on the DRAWER
# count, which never mentions Submission. Same block as
# scripts/seed_employees.py:62-78; keep them in step.
from app.core import models as _core_models            # noqa: F401
from app.modules.clients import models as _clients     # noqa: F401
from app.modules.employees import models as _emp       # noqa: F401
from app.modules.users import models as _users         # noqa: F401
from app.modules.production import models as _prod     # noqa: F401
from app.modules.wages import models as _wages         # noqa: F401
from app.modules.attendance import models as _att      # noqa: F401
from app.modules.barcode import models as _barcode     # noqa: F401
from app.modules.procurement import models as _proc    # noqa: F401
from app.modules.bom import models as _bom             # noqa: F401
from app.modules.inventory import models as _inv       # noqa: F401
from app.modules.supplier_po import models as _spo     # noqa: F401

POOL_SIZE = 200


def main() -> int:
    engine = create_engine(settings.database_url, future=True)
    try:
        with Session(engine) as db:
            stats = bootstrap_drawer_pool(db, size=POOL_SIZE)
            db.commit()
    finally:
        engine.dispose()
    print(
        f"[OK] drawer pool ready — minted {stats['drawers_bootstrapped']} new "
        f"drawer(s); pool size = {stats['pool_size']}. "
        f"Codes DRW-0001..DRW-{POOL_SIZE:04d} are active in barcode_registry."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())