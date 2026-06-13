"""
================================================================================
modules/bom/seed_stage2.py — Seed the Stage-2 reference registries (§3)
================================================================================

Idempotently loads the two DB-backed Stage-2 configs (the same replace-on-key
pattern as seed_templates.py):

  - config/garment_types.yaml   → `garment_type` (required POMs + area heuristic)
  - config/pom_dictionary.yaml  → `pom_dictionary` (native term → standard code)

The other two Stage-2 configs (extraction_adapters.yaml, bom_checks.yaml) are
BEHAVIOUR config read straight off disk at runtime (extraction.py / checks.py),
not DB rows — so they have no seed here. Used by scripts/seed.py and tests.

NOTE: these are SYNC functions (plain Session), because seeding runs via scripts/seed.py
on the sync engine — the only non-async path in the app (CLAUDE.md §3.3).

FUNCTION GUIDE
  _load(path) -> list[dict]   [private] read a YAML file into a list of dicts.
  seed_garment_types(db, path?) -> int
      Upsert each garment_type by `code` (replace-on-key). Returns the count. Run FIRST.
  seed_pom_dictionary(db, path?) -> int
      Upsert each term by (language, source_term, garment_type_id). Resolves an optional
      garment_type code → id. Returns the count.
  seed_stage2(db) -> {garment_types, pom_dictionary}
      Run both in dependency order (types before the dictionary that FKs them).
      CALLED FROM: scripts/seed.py + tests.
================================================================================
"""
from __future__ import annotations

import os

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.bom.models import GarmentType, PomDictionary

_CONFIG_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config",
)
_GARMENT_YAML = os.path.join(_CONFIG_DIR, "garment_types.yaml")
_POM_DICT_YAML = os.path.join(_CONFIG_DIR, "pom_dictionary.yaml")


def _load(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def seed_garment_types(db: Session, path: str | None = None) -> int:
    """Upsert every garment type by `code`. Returns the count seeded."""
    rows = _load(path or _GARMENT_YAML)
    count = 0
    for r in rows:
        code = r["code"]
        existing = db.scalar(select(GarmentType).where(GarmentType.code == code))
        fields = dict(
            label=r.get("label"),
            required_poms=r.get("required_poms") or [],
            area_formula=r.get("area_formula") or {},
            default_wastage_pct=r.get("default_wastage_pct"),
        )
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            db.add(GarmentType(code=code, **fields))
        count += 1
    db.commit()
    return count


def seed_pom_dictionary(db: Session, path: str | None = None) -> int:
    """Upsert every term by (language, source_term, garment_type_id=None). The YAML
    carries no per-type scoping for the real files, so garment_type_id stays null;
    onboarding a per-type override is still one row. Returns the count seeded."""
    rows = _load(path or _POM_DICT_YAML)
    # resolve optional garment_type code → id once
    gt_by_code = {g.code: g.id for g in db.scalars(select(GarmentType))}
    count = 0
    for r in rows:
        lang, term = r["language"], r["source_term"]
        gt_id = gt_by_code.get(r.get("garment_type")) if r.get("garment_type") else None
        existing = db.scalar(
            select(PomDictionary).where(
                PomDictionary.language == lang,
                PomDictionary.source_term == term,
                PomDictionary.garment_type_id == gt_id,
            )
        )
        fields = dict(pom_code=r["pom_code"], weight=r.get("weight", 1))
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            db.add(PomDictionary(language=lang, source_term=term,
                                 garment_type_id=gt_id, **fields))
        count += 1
    db.commit()
    return count


def seed_stage2(db: Session) -> dict[str, int]:
    """Seed both Stage-2 registries (garment types first — the dictionary FKs them)."""
    n_gt = seed_garment_types(db)
    n_pom = seed_pom_dictionary(db)
    return {"garment_types": n_gt, "pom_dictionary": n_pom}
