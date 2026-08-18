"""
================================================================================
scripts/backfill_needs_lining.py — recompute a frozen flag against current rules
================================================================================

WHY THIS EXISTS
    `piece.needs_lining` is written ONCE, at breakdown upload, by
    imports/premint.py::_sku_needs_lining, and never recomputed. That is fine
    until the detection rules change — and they did. An order minted before the
    style-name heuristic landed carries the answer that was current then,
    permanently.

    On the live database that produced two orders holding the SAME 17 styles:

        UM-1     1,425 pieces,   0 flagged needs_lining
        han123   1,425 pieces, 925 flagged needs_lining

    which is why the lining dashboard showed a lining-required count far below
    the ordered total and looked broken. It was not a reporting bug; it was a
    stale flag on half the pieces.

WHAT IT DOES
    Recomputes `needs_lining` for existing pieces using the CURRENT
    `_sku_needs_lining`, which it IMPORTS rather than reimplements — one
    definition, so this script cannot drift from what the importer does to the
    next order.

WHY IT IS A SCRIPT AND NOT A MIGRATION
    It changes production data based on a heuristic that may change again. That
    is a decision someone should take deliberately, with a dry run first — not
    something that happens silently during a deploy.

WHY IT IS NOT DONE AT READ TIME
    The merge gate reads the stored flag to decide whether a drawer is complete.
    If the dashboard quietly substituted a derived answer, the screen and the gate
    would disagree about the same piece. The flag is the contract; this fixes the
    flag.

SAFETY
    Idempotent, batched, and it never flips a flag it was not asked to. `--dry-run`
    reports the per-style breakdown and writes nothing.

USAGE
    python -m scripts.backfill_needs_lining --dry-run       # look first
    python -m scripts.backfill_needs_lining                 # apply
    python -m scripts.backfill_needs_lining --order UM-1    # one order
    python -m scripts.backfill_needs_lining --only-false    # never un-flag a piece
================================================================================
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import SessionLocal

# EVERY model module, so the mapper can configure. Not optional and not tidy-able
# away: core.Document declares relationships to `Submission` and `Supplier`, which
# live in procurement / supplier_po, and SQLAlchemy resolves those by NAME at
# first use. Import only the tables this script reads and the very first query
# dies with "expression 'Submission' failed to locate a name". Same list as
# tests/conftest.py, for the same reason.
import app.core.models                      # noqa: F401
import app.modules.clients.models           # noqa: F401
import app.modules.employees.models         # noqa: F401
import app.modules.users.models             # noqa: F401
import app.modules.production.models        # noqa: F401
import app.modules.barcode.models           # noqa: F401
import app.modules.attendance.models        # noqa: F401
import app.modules.wages.models             # noqa: F401
import app.modules.procurement.models       # noqa: F401
import app.modules.supplier_po.models       # noqa: F401
import app.modules.bom.models               # noqa: F401
import app.modules.inventory.models         # noqa: F401

from app.modules.clients.models import SKU, ClientOrder, Style
from app.modules.imports.premint import _sku_needs_lining
from app.modules.production.models import Piece


def backfill(db: Session, *, order_number: str | None = None,
             only_false: bool = False, batch: int = 1000,
             dry_run: bool = False) -> dict:
    """Recompute needs_lining per SKU and apply it to that SKU's pieces.

    Computed ONCE PER SKU, not per piece: `_sku_needs_lining` is a function of the
    SKU and its style, so evaluating it per piece would re-read the same style row
    thousands of times for the same answer — the exact shape premint was optimised
    to remove.
    """
    stats = {"skus": 0, "pieces_seen": 0, "flipped_true": 0, "flipped_false": 0,
            "unchanged": 0}
    per_style: dict[str, dict[str, int]] = defaultdict(
        lambda: {"->True": 0, "->False": 0, "same": 0})

    sku_q = select(SKU).join(Style, Style.id == SKU.style_id)
    if order_number:
        sku_q = sku_q.join(ClientOrder, ClientOrder.id == Style.client_order_id) \
                    .where(ClientOrder.order_number == order_number)

    pending = 0
    for sku in db.scalars(sku_q).all():
        stats["skus"] += 1
        should = _sku_needs_lining(sku, db)
        style = db.get(Style, sku.style_id)
        style_name = style.name if style else "?"

        pieces = db.scalars(
            select(Piece).where(Piece.sku_id == sku.id,
                                Piece.is_active.is_(True))).all()
        for piece in pieces:
            stats["pieces_seen"] += 1
            current = bool(getattr(piece, "needs_lining", False))
            if current == should:
                stats["unchanged"] += 1
                per_style[style_name]["same"] += 1
                continue
            # --only-false: turn flags ON but never OFF. The safe direction when
            # someone has hand-corrected data you do not want to overwrite.
            if only_false and current is True:
                stats["unchanged"] += 1
                per_style[style_name]["same"] += 1
                continue

            if should:
                stats["flipped_true"] += 1
                per_style[style_name]["->True"] += 1
            else:
                stats["flipped_false"] += 1
                per_style[style_name]["->False"] += 1

            if not dry_run:
                piece.needs_lining = should
                pending += 1
                if pending >= batch:
                    db.commit()
                    pending = 0

    if not dry_run:
        db.commit()
    stats["_per_style"] = dict(per_style)
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--order", default=None,
                    help="limit to one order_number (e.g. UM-1)")
    ap.add_argument("--only-false", action="store_true",
                    help="only turn flags ON; never clear one that is already set")
    ap.add_argument("--batch", type=int, default=1000, help="rows per commit")
    args = ap.parse_args()

    with SessionLocal() as db:
        stats = backfill(db, order_number=args.order, only_false=args.only_false,
                         batch=args.batch, dry_run=args.dry_run)

    per_style = stats.pop("_per_style", {})
    mode = "DRY RUN — nothing written" if args.dry_run else "committed"
    print(f"needs_lining backfill ({mode})"
          + (f" · order={args.order}" if args.order else ""))
    for k, v in stats.items():
        print(f"  {k:<14} {v}")

    changed = {s: c for s, c in per_style.items()
               if c["->True"] or c["->False"]}
    if changed:
        print("\n  per style (only styles that change):")
        for style, c in sorted(changed.items(),
                               key=lambda kv: -(kv[1]["->True"] + kv[1]["->False"])):
            print(f"    {style[:38]:38} ->True {c['->True']:5}  "
                  f"->False {c['->False']:5}  same {c['same']:5}")
    else:
        print("\n  every piece already agrees with the current rules.")


if __name__ == "__main__":
    main()
