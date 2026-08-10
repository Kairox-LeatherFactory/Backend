"""
scripts/bootstrap_drawers.py — Create the initial permanent drawer pool (200).

WHY SYNC
    bootstrap_drawer_pool() speaks the sync Session API (same as premint.py),
    because it shares primitives with the upload-time allocator. This script uses
    a sync engine derived from the app's sync (psycopg2) URL — the same one
    Alembic uses — so it runs against the real Postgres.

USAGE
    python -m scripts.bootstrap_drawers            # pool of 200 (idempotent)
    python -m scripts.bootstrap_drawers --size 200

IDEMPOTENT
    Running it again does nothing once the pool is at/above the target size; if
    someone lowered the count it tops back up. It NEVER deletes drawers.
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.modules.imports.premint import INITIAL_DRAWER_POOL, bootstrap_drawer_pool

# EVERY model module must be imported before the first ORM operation (CLAUDE.md
# §11) — relationships resolve by CLASS NAME at configure_mappers() time, which
# fires on the first query. A half-registered registry fails with
# "expression 'Submission' failed to locate a name" on the DRAWER count.
# Same block as scripts/seed_employees.py:62-78; keep them in step.
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


def _sync_url() -> str:
    # Prefer an explicit sync URL if the config exposes one; else derive from the
    # async URL by swapping the asyncpg driver for psycopg2 (Alembic's driver).
    for attr in ("effective_sync_url", "sync_database_url", "database_url"):
        val = getattr(settings, attr, None)
        if val:
            return str(val).replace("+asyncpg", "+psycopg2")
    async_url = getattr(settings, "effective_async_url", None) or getattr(
        settings, "async_database_url", None)
    if not async_url:
        raise RuntimeError("No database URL found on settings.")
    return str(async_url).replace("+asyncpg", "+psycopg2")


def main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the drawer pool.")
    parser.add_argument("--size", type=int, default=INITIAL_DRAWER_POOL,
                        help=f"Target pool size (default {INITIAL_DRAWER_POOL}).")
    args = parser.parse_args()

    engine = create_engine(_sync_url(), future=True)
    try:
        with Session(engine) as db:
            stats = bootstrap_drawer_pool(db, size=args.size)
            db.commit()
    finally:
        engine.dispose()

    print(f"Drawer pool bootstrap complete: "
          f"minted {stats['drawers_bootstrapped']} new drawer(s); "
          f"pool size = {stats['pool_size']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())