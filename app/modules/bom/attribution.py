"""
================================================================================
modules/bom/attribution.py — DXF fabric-label → material ATTRIBUTION (Stage 2)
================================================================================

WHY THIS EXISTS (the axis fabric_roles.py doesn't cover)
--------------------------------------------------------
`fabric_roles.resolve_fabric_role` answers "is this label leather, and what role"
ONLY for labels already in the DB lexicon. It cannot generalise to a new client's
vocabulary without a human adding a row. This module fills that gap WITHOUT a
per-country dictionary, and — critically — it never BLOCKS BOM generation:

  1. spec present  -> match the DXF label against THIS order's declared materials
                      (a bounded 2-5 candidate set). is_leather comes FROM the
                      matched spec material, not from guessing the label's language.
                      Confidence: ATTR_SPEC_MATCH (0.85).
  2. spec absent   -> classify the label from its own semantics + file context via
                      the LLM (reuse Groq). Weakest path, so lower confidence.
                      Confidence: ATTR_AI_ESTIMATE (0.65).
  3. either way    -> the line is generated NOW with attribution_status=ESTIMATED.
                      A cutting manager OR direct manager confirms later; on confirm
                      the mapping is written to the DB lexicon (status=confirmed) and
                      ONLY that line is regenerated. The next order with that label
                      skips this module entirely (deterministic lexicon hit).

TWO CONFIDENCE AXES — DO NOT CONFLATE (see BomItem):
  attribution_* : did we map the label to the right material/role/is_leather?
                  Confirmed by a MANAGER reviewing the match. (this module)
  dcm_*         : even with correct attribution, is the yield factor right?
                  Confirmed by a real CUTTING event. (confirm_cutting, unchanged)

PROCUREMENT GUARD (sharpening 3): an unconfirmed AI leather estimate informs the
PROJECTED purchase view but never a FIRM PO — see procurement_gate().

PURE + INJECTABLE: the embedder and the LLM are passed in (Protocols below) so this
module has no hard ML dependency and is unit-testable offline. A deterministic
lexical fallback is used when no embedder is supplied.
================================================================================
"""
from __future__ import annotations

import difflib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Callable, Protocol, Sequence


# ── confidence constants (per product decision) ─────────────────────────────
ATTR_SPEC_MATCH = Decimal("0.85")     # label matched to a declared spec material
ATTR_AI_ESTIMATE = Decimal("0.65")    # no spec: LLM classified from label semantics
ATTR_CONFIRMED = Decimal("1.00")      # a human confirmed it

# Band thresholds — DB-overridable defaults (see config note in the writeup).
# Both AI paths (0.65 and 0.85) follow the SAME confirm flow; the band only gates
# what may feed the FIRM vs PROJECTED procurement view (sharpening 3).
PROJECTED_MIN = Decimal("0.65")       # >= this AND estimated -> projected only
FIRM_REQUIRES_CONFIRMED = True        # firm PO quantity needs status == confirmed


class AttributionStatus(str, Enum):
    ESTIMATED = "estimated"           # AI-derived (spec-match or no-spec), awaiting confirm
    NEEDS_REVIEW = "needs_review"     # reserved: flagged for extra scrutiny
    CONFIRMED = "confirmed"           # a cutting/direct manager confirmed the match


class AttributionSource(str, Enum):
    LEXICON = "lexicon"               # deterministic DB row (confirmed) — no AI involved
    SPEC_MATCH = "spec_match"         # matched to a declared spec material (0.85)
    AI_ESTIMATE = "ai_estimate"       # no-spec LLM classification (0.65)
    CONFIRMED = "confirmed"           # promoted to a lexicon row by a human


class FabricRoleStatus(str, Enum):
    SUGGESTED = "suggested"           # AI proposal, not yet human-confirmed
    CONFIRMED = "confirmed"           # human-confirmed (or seeded) — deterministic


# ── shapes ──────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SpecMaterial:
    """One material the spec DECLARES (already typed by extraction). The bounded
    candidate set the DXF label is matched against.

    `name` is the CANONICAL material name the BOM line carries (e.g. "Goat"), while
    `aliases` holds the NATIVE CAD terms the DXF label is actually written in
    (e.g. "VELLUTO", "別布"). Matching scores against name + aliases; the resolved
    category/is_leather/role always come from THIS material, never from the label."""
    name: str                         # e.g. "Goat" — the BOM line name
    category: str                     # BomItemCategory value
    is_leather: bool
    role: str = ""                    # "main" | "sub_material" | ... (optional hint)
    aliases: tuple[str, ...] = ()     # native CAD labels this material is cut under


@dataclass(frozen=True)
class Attribution:
    """The resolved mapping for ONE DXF fabric label, plus its provenance."""
    label: str
    role: str
    category: str
    is_leather: bool
    confidence: Decimal
    source: AttributionSource
    status: AttributionStatus
    matched_material: str | None = None    # which spec material it matched (if any)


# ── injectable dependencies (no hard ML import) ─────────────────────────────
class Embedder(Protocol):
    def __call__(self, texts: Sequence[str]) -> list[list[float]]: ...


class LlmJson(Protocol):
    """Return a JSON object for a classification prompt. Reuse the Groq text rung."""
    def __call__(self, prompt: str) -> dict: ...


# ── (1) spec-anchored match — bounded, safe, no per-country dictionary ───────
def _norm(s: str) -> str:
    return re.sub(r"[\s_\-/()]+", " ", (s or "").strip().casefold()).strip()


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return (dot / (na * nb)) if na and nb else 0.0


def _lexical_sim(a: str, b: str) -> float:
    """Deterministic fallback when no embedder is supplied: token-jaccard blended
    with a character-ratio, so short cross-lingual tokens still get a signal."""
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    jac = len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0
    ratio = difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()
    return max(jac, ratio)


def match_to_spec(label: str, spec_materials: Sequence[SpecMaterial], *,
                  embed: Embedder | None = None,
                  accept_at: float = 0.55) -> Attribution | None:
    """Match a DXF label to ONE declared spec material (bounded candidate set).
    is_leather/category come FROM the matched material — never guessed from the
    label's language. Returns None when nothing clears `accept_at` (caller then
    falls to the no-spec path or leaves it provisional)."""
    if not spec_materials:
        return None
    cands = list(spec_materials)
    # Each candidate is matched on its canonical name AND its native aliases; the
    # candidate's score is the best (max) over all of its surface forms.
    targets = [[m.name, *m.aliases] for m in cands]
    if embed is not None:
        flat = [label]
        spans: list[tuple[int, int]] = []
        for ts in targets:
            start = len(flat)
            flat.extend(ts)
            spans.append((start, len(flat)))
        vecs = embed(flat)
        qv = vecs[0]
        scores = [max(_cosine(qv, vecs[i]) for i in range(s, e)) for (s, e) in spans]
    else:
        scores = [max(_lexical_sim(label, t) for t in ts) for ts in targets]
    best_i = max(range(len(cands)), key=lambda i: scores[i])
    if scores[best_i] < accept_at:
        return None
    m = cands[best_i]
    role = m.role or ("main" if m.category == "main_material"
                      else "sub_material" if m.category == "sub_material"
                      else m.category)
    return Attribution(
        label=label, role=role, category=m.category, is_leather=m.is_leather,
        confidence=ATTR_SPEC_MATCH, source=AttributionSource.SPEC_MATCH,
        status=AttributionStatus.ESTIMATED, matched_material=m.name,
    )


# ── (2) no-spec path — LLM classifies from the label + file context ──────────
_CLASSIFY_PROMPT = """You map a CAD fabric label from a leather-garment cutting file to a BOM role.
Return ONLY a JSON object, no prose, no markdown:
{{"role": "main|sub_material|lining|pocketing|interlining|elastic",
  "category": "main_material|sub_material|lining|interlining|accessory",
  "is_leather": true|false}}

label: {label}
garment_type: {garment}
other labels in the same file: {others}
Rules: is_leather is true ONLY for a leather shell/panel material. Lining, nylon,
textile, pocketing and elastic are is_leather=false. If unsure, prefer is_leather=false."""


def classify_no_spec(label: str, *, garment: str, other_labels: Sequence[str],
                     llm_json: LlmJson) -> Attribution:
    """No spec to anchor to -> classify from the label's own semantics + context.
    Always returns an Attribution at ATTR_AI_ESTIMATE (0.65); on any parse failure
    it degrades to a non-leather lining line (the safe direction)."""
    prompt = _CLASSIFY_PROMPT.format(
        label=label, garment=garment or "unknown",
        others=", ".join(sorted({x for x in other_labels if x and x != label})) or "none")
    try:
        obj = llm_json(prompt) or {}
        role = str(obj.get("role") or "lining")
        category = str(obj.get("category") or "lining")
        is_leather = bool(obj.get("is_leather"))
    except Exception:
        role, category, is_leather = "lining", "lining", False   # honest degrade
    return Attribution(
        label=label, role=role, category=category, is_leather=is_leather,
        confidence=ATTR_AI_ESTIMATE, source=AttributionSource.AI_ESTIMATE,
        status=AttributionStatus.ESTIMATED, matched_material=None,
    )


# ── orchestrator: lexicon -> spec-match -> no-spec (never blocks) ────────────
def resolve_attribution(label: str, *, lexicon_hit, spec_materials, garment,
                        other_labels, embed=None, llm_json=None) -> Attribution:
    """The full ladder for ONE label. `lexicon_hit` is a fabric_roles.FabricRole or
    None (the deterministic DB result). This function NEVER raises and NEVER blocks
    — the worst case is a low-confidence estimate the manager confirms later."""
    if lexicon_hit is not None:                                  # deterministic, done
        return Attribution(
            label=label, role=lexicon_hit.role, category=lexicon_hit.category,
            is_leather=lexicon_hit.is_leather, confidence=ATTR_CONFIRMED,
            source=AttributionSource.LEXICON, status=AttributionStatus.CONFIRMED)
    hit = match_to_spec(label, spec_materials or [], embed=embed)
    if hit is not None:
        return hit
    if llm_json is not None:
        return classify_no_spec(label, garment=garment,
                                other_labels=other_labels or [], llm_json=llm_json)
    # no spec AND no llm wired -> leave it explicitly provisional, non-leather.
    return Attribution(label=label, role="", category="lining", is_leather=False,
                       confidence=Decimal("0"), source=AttributionSource.AI_ESTIMATE,
                       status=AttributionStatus.ESTIMATED)


# ── procurement guard (sharpening 3): firm vs projected ─────────────────────
def procurement_gate(*, attribution_status: str | None,
                     attribution_confidence: Decimal | None) -> str:
    """Which purchase bucket a material line may enter.
      'firm'        -> confirmed; may cut a real PO.
      'projected'   -> estimated but confident enough to inform planning only.
      'excluded'    -> too weak / provisional; no purchase quantity derived yet.
    An unconfirmed AI leather estimate can only ever be 'projected'."""
    status = attribution_status or AttributionStatus.ESTIMATED.value
    conf = attribution_confidence if attribution_confidence is not None else Decimal("0")
    if status == AttributionStatus.CONFIRMED.value:
        return "firm"
    if FIRM_REQUIRES_CONFIRMED and conf >= PROJECTED_MIN:
        return "projected"
    return "excluded"