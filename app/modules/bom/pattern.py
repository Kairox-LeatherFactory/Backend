"""pattern.py — the DXF source (Source 1c) for the DcmResolver.
[Stages 4/5/7 in ARCHITECTURE_AND_DATAFLOW.md — dcm_for_category, graded_dxf, learn_yield.]

DXF and history COMPOSE: the DXF gives the precise part (net pattern area per size per
fabric, measured geometry); confirmed orders give the empirical part (the per-species
yield — the waste multiplier). DCM(size) = net_qty_sf(size, leather-fabrics-of-category)
× yield(species). Per order 1579: sheep 13.77 sf × ~2.5 = 34.4 (truth 34.5); goat 1.22
× ~2.1 = 2.56 (truth 2.6). PER MATERIAL — the old all-pieces 1.73 conflated leather +
pocketing + elastic, which is wrong; pocketing/elastic are is_leather:false and excluded.

Pure functions; they read a PatternData (or any object exposing .fabric_roles /
.fabric_matrix / .master_size — the persisted PatternExtraction row is compatible).
"""
from __future__ import annotations
from decimal import Decimal

# role (from the lexicon) -> the BomItemCategory-style key the resolver resolves on
_ROLE_TO_CATEGORY = {"main": "main_material", "sub_material": "sub_material",
                     "lining": "lining", "interlining": "interlining"}

SF_TO_DM2 = Decimal("9.290304")



def role_to_category(role: str) -> str:
    return _ROLE_TO_CATEGORY.get(role, role)


def _net_qty_for_category(pattern, category: str, size) -> tuple[str | None, float]:
    """Σ net_qty_sf of the LEATHER fabrics whose resolved role maps to `category`, at
    `size` (falling back to master_size). Returns (resolved_size_key, net_sf).

    (a) default: if NO fabric resolves to a leather role AND the pattern has ≤1 distinct
    fabric label (unlabelled/single-fabric export — e.g. Orfatti), the whole net area at
    the size is attributed to `category`. With multiple labelled-but-unmapped fabrics we
    refuse (return None) so the operator maps them (override b) rather than mis-attributing
    lining/textile area to leather."""
    fabric_roles = pattern.fabric_roles or {}
    leather = {f for f, m in fabric_roles.items()
               if m.get("is_leather") and m.get("category") == category}
    fm = pattern.fabric_matrix or {}
    key = str(size)
    if key not in fm:
        key = pattern.master_size if pattern.master_size in fm else None
    if key is None:
        return None, 0.0
    at = fm.get(key, {})
    if leather:
        net = sum(float(v) for f, v in at.items() if f in leather)
        return key, round(net, 2)
    distinct = {f for f in at if f}                       # blank/"" counts as "unlabelled"
    if len(distinct) <= 1:                                # single-fabric / unlabelled export
        net = sum(float(v) for v in at.values())
        return key, round(net, 2)
    return None, 0.0                                      # labelled but unmapped → refuse



def dcm_for_category(pattern, *, category, size, species="_default", yields=None):
    """DXF-driven DCM in dm² (CANONICAL): net cut area (sf, from geometry)
    × per-species yield (dimensionless) × SF_TO_DM2. Returns Decimal dm² or
    None when the pattern can't attribute this category (no leather fabrics
    mapped and multiple labelled fabrics — see _net_qty_for_category)."""
    key, net_sf = _net_qty_for_category(pattern, category, size)
    if key is None or not net_sf:
        return None
    y = (yields or {}).get(species) or (yields or {}).get("_default")
    if not y:
        return None
    return (Decimal(str(net_sf)) * Decimal(str(y)) * SF_TO_DM2).quantize(Decimal("0.01"))


def graded_dxf(pattern, *, category: str, sizes, species: str, yields: dict) -> dict:
    """Per-size DCM straight from the DXF (each size has its own measured net area), so a
    DXF line is graded by exact geometry rather than a POM-ratio proxy. {size: dcm_sf}."""
    out: dict = {}
    for s in sizes:
        d = dcm_for_category(pattern, category=category, size=s, species=species, yields=yields)
        if d is None:                       # size missing AND no master fallback
            d = dcm_for_category(pattern, category=category, size=pattern.master_size,
                                 species=species, yields=yields)
        out[s] = d
    return out


def learn_yield(pattern, *, category, size, species, confirmed_dcm_sf):
    """Yield observation from a cutting-manager confirm. DESPITE the legacy
    parameter name, the value arriving from the BOM is dm² (the item's
    qty_per_garment) — convert to sf so the yield is dimensionless against the
    sf net area:  yield = gross_sf / net_sf  ==  gross_dm² / net_dm².
    Returns the observation dict or None when the pattern has no net area."""
    key, net_sf = _net_qty_for_category(pattern, category, size)
    if key is None or not net_sf:
        return None
    gross_sf = Decimal(str(confirmed_dcm_sf)) / SF_TO_DM2
    implied = (gross_sf / Decimal(str(net_sf))).quantize(Decimal("0.0001"))
    return {"species": species, "category": category, "size": key,
            "net_sf": float(net_sf), "implied_yield": float(implied)}


def effective_dxf_yields(seed_yields: dict,
                         observations_by_species: dict[str, list[float]] | None = None) -> dict:
    """Merge learned per-species mean implied-yield over the seeds bootstrap. A species
    with observations uses their mean; otherwise the seed. Keeps `_default`."""
    out = dict(seed_yields or {})
    for sp, ys in (observations_by_species or {}).items():
        vals = [float(y) for y in ys if y]
        if vals:
            out[sp] = round(sum(vals) / len(vals), 3)
    return out
