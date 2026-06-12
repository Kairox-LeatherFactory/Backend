"""
================================================================================
modules/procurement/inventory_match.py — BOM line → stock matching (§5/§6)
================================================================================

PURE matching logic (no DB): given a BOM line's normalized key + colour and a small
pool of CANDIDATE inventory rows (fetched set-based by the repository — never the
whole table), decide the match deterministically-first:

    (1) normalized_key exact  → method=key
    (2) curated alias         → method=alias  (SHEEP GLASS → SHEEP NAPPA …)
    (3) flagged fuzzy         → a `suggestion` only — NEVER auto-applied
    (4) nothing               → unmatched (caller → OUT_OF_STOCK + `unmatched` flag)

"No LLM, no silent guess" (stage-0 §4): an unmatched line is reported as out-of-stock
with diagnostics, not force-bound to a wrong article. The UOM helper reconciles the
matched stock unit with the BOM line unit (§6.2); an unconvertible clash is flagged.
================================================================================
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from app.modules.procurement.enums import MatchMethod
from app.modules.procurement.inventory_normalize import tokens

# A fuzzy suggestion is surfaced only above this token-overlap (Jaccard) floor.
_SUGGEST_FLOOR = 0.34


@dataclass
class Alias:
    bom_term: str       # normalized
    inventory_key: str  # normalized target token(s)


@dataclass
class MatchResult:
    rows: list = field(default_factory=list)        # matched inventory rows (lots)
    method: str | None = None                       # MatchMethod value or None
    suggestion: dict | None = None                  # advisory candidate, NOT applied


def _color_ok(bom_color: str | None, inv_key: str) -> bool:
    """If the BOM line names a colour, the inventory key must contain it (so BLACK
    leather isn't matched to BROWN). No BOM colour → colour is not a constraint."""
    if not bom_color:
        return True
    return bom_color.upper() in tokens(inv_key)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def match_line(bom_key: str, bom_color: str | None, candidates: list,
               aliases: list[Alias]) -> MatchResult:
    """Resolve one BOM line against the candidate pool. `candidates` are inventory rows
    (duck-typed: .normalized_key, .color). Lots sharing a key are returned together so
    the caller sums on-hand across them (§6 edge 3)."""
    bom_tokens = tokens(bom_key)

    # (1) exact normalized-key hit (colour-aware)
    exact = [c for c in candidates
             if c.normalized_key == bom_key and _color_ok(bom_color, c.normalized_key)]
    if exact:
        return MatchResult(rows=exact, method=MatchMethod.KEY.value)

    # (2) curated alias: an alias whose bom_term tokens are all present in the BOM key,
    #     then inventory rows whose key contains the alias target tokens (colour-aware).
    for al in aliases:
        if tokens(al.bom_term) and tokens(al.bom_term) <= bom_tokens:
            target = tokens(al.inventory_key)
            hits = [c for c in candidates
                    if target <= tokens(c.normalized_key)
                    and _color_ok(bom_color, c.normalized_key)]
            if hits:
                return MatchResult(rows=hits, method=MatchMethod.ALIAS.value)

    # (3) flagged fuzzy suggestion — advisory only, never bound
    best, best_score = None, 0.0
    for c in candidates:
        score = _jaccard(bom_tokens, tokens(c.normalized_key))
        if score > best_score:
            best, best_score = c, score
    if best is not None and best_score >= _SUGGEST_FLOOR:
        return MatchResult(rows=[], method=None, suggestion={
            "inventory_item_id": str(best.id),
            "description": best.description, "score": round(best_score, 2)})

    # (4) genuinely unmatched
    return MatchResult(rows=[], method=None, suggestion=None)


def convert_on_hand(on_hand: Decimal, stock_uom: str | None, bom_uom: str | None,
                    conversions: dict[tuple[str, str], Decimal]) -> tuple[Decimal, bool]:
    """Convert a matched on-hand qty from the stock UOM into the BOM line UOM (§6.2).

    Returns (converted_qty, mismatch). Identity when the units already agree (incl.
    dm²≡DCM after canonicalization); a seeded `uom_conversion` factor otherwise; and a
    conservative (qty 0, mismatch=True) when no conversion exists — so an unconvertible
    unit can never produce a false 'sufficient'."""
    s = (stock_uom or "").upper()
    b = (bom_uom or "").upper()
    # The BOM uses dm²/pc; the master uses DCM/NOS|PCS. Canonicalize the BOM side.
    b_canon = {"DM²": "DCM", "DM2": "DCM", "DCM": "DCM", "PC": "PCS",
               "UNIT": "PCS", "NOS": "PCS", "PCS": "PCS"}.get(b, b)
    s_canon = {"NOS": "PCS"}.get(s, s)
    if not s or s_canon == b_canon or s == b:
        return on_hand, False
    factor = conversions.get((s, b_canon)) or conversions.get((s, b))
    if factor is not None:
        return (on_hand * Decimal(str(factor))), False
    return Decimal("0"), True
