"""
================================================================================
modules/procurement/supplier_match.py — shortfall line → supplier (§1)
================================================================================

PURE matching + ranking (no DB): given a shortfall line's normalized key + colour
+ category bucket and a pool of CANDIDATE `supplier_supply_history` rows (fetched
set-based by the repository — never the whole ledger), resolve the supplier
deterministically-first (§1b), never a silent guess:

    (1) exact historical supply  → method=ledger   (BOM tokens ⊆ a buy's article)
    (2) alias rewrite, re-try (1) → method=alias    (SHEEP GLASS → SHEEP NAPPA)
    (3) category / MODE fallback  → method=category  (ranked lower, a guess)
    (4) advisory fuzzy candidate  → suggestion only, NEVER auto-bound
    (5) nothing confident         → unresolved (caller holds a needs_supplier PO)

Ranking (§1c): recency · frequency · contactability − rate. Ties / category-only
hits / low confidence → `ambiguous=True` (do not auto-select — surface the
shortlist, the buyer picks). Contactability is a ranking TERM, not a hard filter:
a frequent contactless vendor still surfaces (top, even) but the PO is flagged
`no_contact_channel` downstream (§5/§7).
================================================================================
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.modules.procurement.inventory_match import Alias
from app.modules.procurement.inventory_normalize import tokens

# Score weights (§1c). Tunable; recency dominates, contactability is a strong term,
# rate is a tie-break only.
_W_RECENCY = 1.0
_W_FREQUENCY = 0.6
_W_CONTACTABLE = 0.8
_W_RATE = 0.3
# Two top suppliers whose scores fall within this band are "too close to call" → do
# not auto-select (§1c ambiguity fallback).
_TIE_BAND = 0.12
# Advisory fuzzy floor (token Jaccard) — mirrors the Stage-4 inventory matcher.
_SUGGEST_FLOOR = 0.34


@dataclass
class HistoryCandidate:
    """A duck-typed `supplier_supply_history` row + its supplier's contactability —
    the unit the matcher ranks. `has_contact` = the supplier has an email or phone."""
    supplier_id: object
    supplier_name: str
    normalized_description: str
    mode: str | None
    txn_count: int
    last_purchased_at: date | None
    last_rate: Decimal | None
    uom: str | None
    has_contact: bool


@dataclass
class SupplierScore:
    supplier_id: object
    supplier_name: str
    score: float
    txn_count: int
    last_purchased_at: date | None
    last_rate: Decimal | None
    uom: str | None
    has_contact: bool
    mode: str | None
    matched_description: str


@dataclass
class MatchResult:
    method: str | None = None                 # ledger | alias | category | None
    ranked: list[SupplierScore] = field(default_factory=list)
    chosen_supplier_id: object = None         # None when unresolved / ambiguous
    ambiguous: bool = False                   # True → hold needs_supplier, buyer picks
    suggestion: dict | None = None            # advisory fuzzy, never auto-applied


def _color_ok(color: str | None, desc: str) -> bool:
    if not color:
        return True
    return color.upper() in tokens(desc)


def _recency(d: date | None, today: date) -> float:
    """1.0 for a buy today, decaying ~half each year — bounded [0, 1]."""
    if d is None:
        return 0.2
    days = max((today - d).days, 0)
    return 1.0 / (1.0 + days / 365.0)


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _rank(matched: list[HistoryCandidate], today: date) -> list[SupplierScore]:
    """Aggregate matching history rows per supplier (best/most-recent buy wins) and
    score each supplier (§1c). Returns the shortlist, highest score first."""
    best_by_supplier: dict[object, HistoryCandidate] = {}
    txn_by_supplier: dict[object, int] = {}
    for c in matched:
        txn_by_supplier[c.supplier_id] = txn_by_supplier.get(c.supplier_id, 0) + c.txn_count
        cur = best_by_supplier.get(c.supplier_id)
        if cur is None or (c.last_purchased_at or date.min) > (cur.last_purchased_at or date.min):
            best_by_supplier[c.supplier_id] = c
    rates = [float(c.last_rate) for c in best_by_supplier.values() if c.last_rate]
    max_rate = max(rates) if rates else 0.0

    out: list[SupplierScore] = []
    for sid, c in best_by_supplier.items():
        txns = txn_by_supplier[sid]
        rate_term = (float(c.last_rate) / max_rate) if (c.last_rate and max_rate) else 0.0
        score = (_W_RECENCY * _recency(c.last_purchased_at, today)
                 + _W_FREQUENCY * math.log1p(txns)
                 + _W_CONTACTABLE * (1.0 if c.has_contact else 0.0)
                 - _W_RATE * rate_term)
        out.append(SupplierScore(
            supplier_id=sid, supplier_name=c.supplier_name, score=round(score, 4),
            txn_count=txns, last_purchased_at=c.last_purchased_at, last_rate=c.last_rate,
            uom=c.uom, has_contact=c.has_contact, mode=c.mode,
            matched_description=c.normalized_description,
        ))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def _decide(ranked: list[SupplierScore], method: str, *, weak: bool) -> MatchResult:
    """Auto-select the top supplier unless it's a weak (category) hit or a near-tie
    with the runner-up (§1c) — then hold ambiguous for the buyer."""
    if not ranked:
        return MatchResult(method=None)
    ambiguous = weak
    if len(ranked) >= 2 and (ranked[0].score - ranked[1].score) < _TIE_BAND:
        ambiguous = True
    chosen = None if ambiguous else ranked[0].supplier_id
    return MatchResult(method=method, ranked=ranked, chosen_supplier_id=chosen,
                       ambiguous=ambiguous)


def match_shortfall(bom_key: str, color: str | None, category_mode: str | None,
                    candidates: list[HistoryCandidate], aliases: list[Alias],
                    *, today: date) -> MatchResult:
    """Resolve one shortfall line to a supplier (§1b). `category_mode` is the line's
    MODE bucket (LEATHER/MATERIALS/SERVICE) for the §1b(3) fallback."""
    bom_tokens = tokens(bom_key)

    # (1) exact historical supply: every BOM token present in a buy's article (colour-aware)
    if bom_tokens:
        ledger = [c for c in candidates
                  if bom_tokens <= tokens(c.normalized_description)
                  and _color_ok(color, c.normalized_description)]
        if ledger:
            return _decide(_rank(ledger, today), "ledger", weak=False)

    # (2) alias rewrite then re-try (1)
    for al in aliases:
        al_terms = tokens(al.bom_term)
        if al_terms and al_terms <= bom_tokens:
            target = tokens(al.inventory_key)
            hits = [c for c in candidates
                    if target <= tokens(c.normalized_description)
                    and _color_ok(color, c.normalized_description)]
            if hits:
                return _decide(_rank(hits, today), "alias", weak=False)

    # (3) category / MODE fallback — ranked lower, always a guess (ambiguous)
    if category_mode:
        cat = [c for c in candidates if (c.mode or "").upper() == category_mode.upper()]
        if cat:
            return _decide(_rank(cat, today), "category", weak=True)

    # (4) advisory fuzzy candidate — surfaced, never auto-bound
    best, best_score = None, 0.0
    for c in candidates:
        s = _jaccard(bom_tokens, tokens(c.normalized_description))
        if s > best_score:
            best, best_score = c, s
    if best is not None and best_score >= _SUGGEST_FLOOR:
        return MatchResult(method=None, suggestion={
            "supplier_id": str(best.supplier_id), "supplier_name": best.supplier_name,
            "matched_description": best.normalized_description, "score": round(best_score, 2)})

    # (5) genuinely unresolved
    return MatchResult(method=None)
