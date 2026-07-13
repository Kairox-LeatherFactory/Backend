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


def _load_map(path: str) -> dict:      # for mapping YAML (seed.yaml, cost_catalog.yaml)
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}

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


def seed_stage2(db) -> dict[str, int]:
    return {
        "garment_types": seed_garment_types(db),
        "pom_dictionary": seed_pom_dictionary(db),
        "dxf_yields": seed_dxf_yields(db),          # slice 1
        "fabric_roles": seed_fabric_roles(db),      # slice 1
        "cost_catalog": seed_cost_catalog(db),      # slice 2
        "bom_checks": seed_bom_checks(db),          # slice 3
    }

_SEED_YAML = os.path.join(_CONFIG_DIR, "seed.yaml")

def seed_dxf_yields(db, path: str | None = None) -> int:
    from app.modules.bom.models import DxfYield
    data = _load_map(path or _SEED_YAML).get("dxf_yield_factor") or {}
    count = 0
    for species, factor in data.items():
        existing = db.scalar(select(DxfYield).where(DxfYield.species == species))
        if existing:
            existing.factor = factor
        else:
            db.add(DxfYield(species=species, factor=factor))
        count += 1
    db.commit()
    return count

def seed_fabric_roles(db, path: str | None = None) -> int:
    from app.modules.bom.models import FabricRoleRow
    rows = _load_map(path or _SEED_YAML).get("fabric_role") or []
    count = 0
    for r in rows:
        label = r["label"]
        existing = db.scalar(select(FabricRoleRow).where(FabricRoleRow.label == label))
        fields = dict(role=r["role"], category=r["category"], is_leather=bool(r["is_leather"]))
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            db.add(FabricRoleRow(label=label, **fields))
        count += 1
    db.commit()
    return count

_COST_CATALOG_YAML = os.path.join(_CONFIG_DIR, "cost_catalog.yaml")

def seed_cost_catalog(db, path: str | None = None) -> int:
    from app.modules.bom.models import CostCatalogLine
    data = _load_map(path or _COST_CATALOG_YAML)
    count = 0
    for code, lines in (data or {}).items():
        for i, ln in enumerate(lines or []):
            cat, name = ln.get("category"), ln.get("name")
            if not cat or not name:
                continue
            existing = db.scalar(select(CostCatalogLine).where(
                CostCatalogLine.garment_code == code,
                CostCatalogLine.category == cat,
                CostCatalogLine.name == name))
            fields = dict(uom=ln.get("uom"), unit_price=ln.get("unit_price"),
                          qty_per_garment=ln.get("qty_per_garment", 1), sort_order=i)
            if existing:
                for k, v in fields.items():
                    setattr(existing, k, v)
            else:
                db.add(CostCatalogLine(garment_code=code, category=cat, name=name, **fields))
            count += 1
    db.commit()
    return count

_BOM_CHECKS_YAML = os.path.join(_CONFIG_DIR, "bom_checks.yaml")
_KNOWN_RULE_KEYS = {"id", "kind", "severity", "field", "range"}

def seed_bom_checks(db, path: str | None = None) -> int:
    from app.modules.bom.models import ClientCheckRule
    entries = _load(path or _BOM_CHECKS_YAML)
    count = 0
    for entry in entries or []:
        code = entry.get("client_code")
        for i, rule in enumerate(entry.get("checks") or []):
            rid = rule.get("id")
            rng = rule.get("range") or [None, None]
            params = {k: v for k, v in rule.items() if k not in _KNOWN_RULE_KEYS} or None
            existing = db.scalar(select(ClientCheckRule).where(
                ClientCheckRule.client_code == code, ClientCheckRule.rule_id == rid))
            fields = dict(kind=rule.get("kind", "range"), severity=rule.get("severity", "warn"),
                          field=rule.get("field"), range_lo=rng[0], range_hi=rng[1],
                          params=params, sort_order=i)
            if existing:
                for k, v in fields.items():
                    setattr(existing, k, v)
            else:
                db.add(ClientCheckRule(client_code=code, rule_id=rid, **fields))
            count += 1
    db.commit()
    return count
