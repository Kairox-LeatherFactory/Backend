"""
================================================================================
scripts/backfill_short_codes.py — give every already-minted piece a compact code
================================================================================

WHY THIS EXISTS (bug #19)
    From this build on, breakdown upload mints TWO registry rows per piece: the
    compact `PC-…` code that is printed and scanned (the PRIMARY), and the long
    `KJ2451-CLERMONT-57-M-005` code kept alive as an ALIAS so labels already on
    the factory floor keep resolving.

    Pieces minted BEFORE the change have only the long row, and that row is the
    one carrying order_id / sku_id / style_id. This script brings them to the new
    shape:

        1. mint a `PC-…` PRIMARY row for the piece, MOVING the order/sku/style
           FKs onto it, and
        2. demote the existing long row to an alias (is_alias=True, FKs cleared).

    Step 2 is what keeps the counts honest. Leaving the order_id on both rows
    would make `GET /barcode/orders/{id}/analytics` report twice the pieces it
    should, drive `balance` negative, and light up the duplicates!=0 integrity
    warning — see BarcodeRegistry.is_alias.

NOTHING IS DELETED AND NOTHING STOPS SCANNING.
    The long code keeps status=ACTIVE. Every label already printed still resolves
    to the same piece; `resolve()` simply reports `is_alias: true` so the UI can
    suggest a reprint.

IDEMPOTENT
    A piece that already has a non-alias `PC-…` row is skipped. Safe to re-run,
    and safe to run while the app is up (it commits in batches).

WHY SYNC
    Same reason as scripts/seed.py and imports/premint.py: this is a one-shot
    batch job on the sync engine, off the request path.

USAGE
    python -m scripts.backfill_short_codes            # backfill everything
    python -m scripts.backfill_short_codes --dry-run  # report, write nothing
    python -m scripts.backfill_short_codes --batch 2000
================================================================================
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal
from app.core.enums import BarcodeStatus, BarcodeType
from app.modules.barcode.models import BarcodeRegistry
from app.modules.barcode.repository import (
    SHORT_CODE_PREFIX, decode_short, encode_short,
)

# EVERY model module must be imported before the first ORM operation, not just
# the one table this script writes (CLAUDE.md §11). SQLAlchemy resolves
# relationships by CLASS NAME at configure_mappers() time, which fires on the
# first query — so a half-registered registry fails with
#   InvalidRequestError: When initializing mapper Mapper[Document(document)],
#   expression 'Submission' failed to locate a name ('Submission')
# on the very first SELECT, which mentions neither Document nor Submission and
# reads like a broken model rather than a missing import. Same block as
# scripts/bootstrap_drawers.py and scripts/seed_employees.py; keep them in step.
from app.core import models as _core_models            # noqa: F401,E402
from app.modules.clients import models as _clients     # noqa: F401,E402
from app.modules.employees import models as _emp       # noqa: F401,E402
from app.modules.users import models as _users         # noqa: F401,E402
from app.modules.production import models as _prod     # noqa: F401,E402
from app.modules.wages import models as _wages         # noqa: F401,E402
from app.modules.attendance import models as _att      # noqa: F401,E402
from app.modules.procurement import models as _proc    # noqa: F401,E402
from app.modules.bom import models as _bom             # noqa: F401,E402
from app.modules.inventory import models as _inv       # noqa: F401,E402
from app.modules.supplier_po import models as _spo     # noqa: F401,E402


def _max_short_counter(db: Session) -> int:
    """Highest short-code counter already issued. One row transferred — the
    encoding is fixed-width over an ascending alphabet, so lexicographic max is
    numeric max (the same trick BarcodeRepository._next_code uses)."""
    top = db.scalar(
        select(BarcodeRegistry.code)
        .where(BarcodeRegistry.code.like(f"{SHORT_CODE_PREFIX}-%"))
        .order_by(BarcodeRegistry.code.desc())
        .limit(1)
    )
    return decode_short(top)


def backfill(db: Session, *, batch: int = 1000, dry_run: bool = False) -> dict:
    stats = {"pieces_seen": 0, "already_had": 0, "minted": 0, "demoted": 0}

    # Every piece that ALREADY has a primary short code — skipped below. Read as
    # a set of piece_ids in one query rather than an EXISTS per row: this runs
    # against ~1,400 pieces per order and the whole point is to stay linear.
    have_short = set(db.scalars(
        select(BarcodeRegistry.piece_id)
        .where(BarcodeRegistry.type == BarcodeType.PIECE.value,
               BarcodeRegistry.is_alias.is_(False),
               BarcodeRegistry.code.like(f"{SHORT_CODE_PREFIX}-%"),
               BarcodeRegistry.piece_id.isnot(None))
    ).all())

    legacy_rows = db.scalars(
        select(BarcodeRegistry)
        .where(BarcodeRegistry.type == BarcodeType.PIECE.value,
               BarcodeRegistry.piece_id.isnot(None),
               BarcodeRegistry.is_alias.is_(False))
        .order_by(BarcodeRegistry.created_at.asc())
    ).all()

    counter = _max_short_counter(db)
    pending = 0

    for row in legacy_rows:
        if row.code.startswith(f"{SHORT_CODE_PREFIX}-"):
            continue                      # already a compact primary
        stats["pieces_seen"] += 1
        if row.piece_id in have_short:
            stats["already_had"] += 1
            continue

        counter += 1
        if dry_run:
            stats["minted"] += 1
            stats["demoted"] += 1
            continue

        # The new PRIMARY inherits the FKs; the old row becomes a pure alias.
        db.add(BarcodeRegistry(
            code=encode_short(counter), type=BarcodeType.PIECE.value,
            status=BarcodeStatus.ACTIVE.value, piece_id=row.piece_id,
            caption=row.caption, order_id=row.order_id, sku_id=row.sku_id,
            style_id=row.style_id, is_alias=False,
        ))
        row.is_alias = True
        row.order_id = None
        row.sku_id = None
        row.style_id = None

        stats["minted"] += 1
        stats["demoted"] += 1
        have_short.add(row.piece_id)

        pending += 1
        if pending >= batch:
            db.commit()
            pending = 0

    if not dry_run:
        db.commit()
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--batch", type=int, default=1000,
                    help="rows per commit (default 1000)")
    args = ap.parse_args()

    with SessionLocal() as db:
        stats = backfill(db, batch=args.batch, dry_run=args.dry_run)

    mode = "DRY RUN — nothing written" if args.dry_run else "committed"
    print(f"short-code backfill ({mode})")
    for k, v in stats.items():
        print(f"  {k:<14} {v}")


if __name__ == "__main__":
    main()
