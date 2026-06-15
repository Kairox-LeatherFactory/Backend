"""
================================================================================
modules/inventory/seed_inventory.py — Seed the Stage-4 reference data (§10)
================================================================================

Idempotently loads the two DB-backed Stage-4 configs (the same replace-on-key
pattern as seed_templates.py / seed_stage2.py):

  - config/material_aliases.yaml  → `material_alias`  (BOM-term → inventory-key §5.2)
  - config/uom_conversions.yaml   → `uom_conversion`  (stock-UOM → BOM-UOM §6.2)

It can also load the real inventory MASTER (`data/INVENTORY (1).xlsx`) into
`inventory_item` for a dev/demo DB — the same normalizing parse the live
`POST /inventory/commit` runs, but through the sync Session (seeding is a one-shot
batch job; the live sync stays on the async importer). Used by scripts/seed.py.

FUNCTION GUIDE  (all SYNC — seeding runs on the sync engine via scripts/seed.py)
  _load(path) -> list[dict]   [private] read a YAML file.
  seed_material_aliases(db, path?) -> int    upsert each alias by bom_term. Returns count.
  seed_uom_conversions(db, path?) -> int     upsert each conversion by (from,to). Returns count.
  seed_inventory_master(db, path?) -> int    load+normalize+dedup the real INVENTORY xlsx into
      inventory_item (same parse as the live importer; sheet-wins; absent rows soft-deactivate).
      No-op if the file is absent (CI). Returns count upserted.
  seed_inventory(db) -> {material_aliases, uom_conversions, inventory_items}
      Run all three. CALLED FROM: scripts/seed.py + tests.
================================================================================
"""
from __future__ import annotations

import glob
import os

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.inventory.inventory_import import parse_inventory
from app.modules.inventory.models import InventoryItem, MaterialAlias, UomConversion

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config",
)
_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "data",
)
_ALIAS_YAML = os.path.join(_CONFIG_DIR, "material_aliases.yaml")
_UOM_YAML = os.path.join(_CONFIG_DIR, "uom_conversions.yaml")


def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def seed_material_aliases(db: Session, path: str | None = None) -> int:
    """Upsert every alias by `bom_term`. Returns the count seeded."""
    rows = _load(path or _ALIAS_YAML)
    count = 0
    for r in rows:
        term = r["bom_term"]
        existing = db.scalar(select(MaterialAlias).where(MaterialAlias.bom_term == term))
        fields = dict(inventory_key=r["inventory_key"], is_active=r.get("is_active", True))
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            db.add(MaterialAlias(bom_term=term, **fields))
        count += 1
    db.commit()
    return count


def seed_uom_conversions(db: Session, path: str | None = None) -> int:
    """Upsert every conversion by (from_uom, to_uom). Returns the count seeded."""
    rows = _load(path or _UOM_YAML)
    count = 0
    for r in rows:
        frm, to = r["from_uom"], r["to_uom"]
        existing = db.scalar(
            select(UomConversion).where(
                UomConversion.from_uom == frm, UomConversion.to_uom == to)
        )
        if existing:
            existing.factor = r.get("factor", 1)
        else:
            db.add(UomConversion(from_uom=frm, to_uom=to, factor=r.get("factor", 1)))
        count += 1
    db.commit()
    return count


def seed_inventory_master(db: Session, path: str | None = None) -> int:
    """Load + normalize + dedup the real INVENTORY workbook into `inventory_item`
    (the same parse the live importer runs). Idempotent: sheet wins on qty_on_hand,
    absent rows soft-deactivate (§2c). Returns the count upserted. No-op if the file
    is absent (CI / a fresh checkout without /data)."""
    if path is None:
        hits = [p for p in glob.glob(os.path.join(_DATA_DIR, "INVENTORY*.xlsx"))
                if "~$" not in p]
        if not hits:
            return 0
        path = hits[0]
    with open(path, "rb") as fh:
        prev = parse_inventory(fh.read())
    keep: set[str] = set()
    for r in prev.rows:
        existing = db.scalar(
            select(InventoryItem).where(InventoryItem.normalized_key == r.normalized_key)
        )
        if existing:
            existing.qty_on_hand = r.qty_on_hand
            existing.description = r.description
            existing.uom = r.uom or existing.uom
            existing.rate = r.rate if r.rate is not None else existing.rate
            existing.color = r.color or existing.color
            existing.is_active = True
        else:
            db.add(InventoryItem(
                description=r.description, normalized_key=r.normalized_key, uom=r.uom,
                qty_on_hand=r.qty_on_hand, rate=r.rate, color=r.color, is_active=True))
        keep.add(r.normalized_key)
    for item in db.scalars(select(InventoryItem).where(InventoryItem.is_active.is_(True))):
        if item.normalized_key not in keep:
            item.is_active = False
    db.commit()
    return len(prev.rows)


def seed_inventory(db: Session) -> dict[str, int]:
    """Seed the Stage-4 reference data (aliases + conversions) and, when present, the
    real inventory master."""
    return {
        "material_aliases": seed_material_aliases(db),
        "uom_conversions": seed_uom_conversions(db),
        "inventory_items": seed_inventory_master(db),
    }
