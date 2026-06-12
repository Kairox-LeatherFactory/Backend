"""
================================================================================
modules/procurement/registry.py — Config-driven client template matching (§4)
================================================================================

Validation profiles live in the `client_template` registry (DB rows seeded from
config/client_templates.yaml), NOT in `if client == "beau_geste"` branches.
Onboarding a new client = add one YAML entry + re-seed; deploy nothing.

This module is the PURE, SYNC scoring core (no DB session, no ORM) so the whole
heuristic runs inside run_in_threadpool. The service loads the ORM rows and hands
them in as plain `ProfileView`s.

SCORING (the §3a heuristic, made concrete against the real /data files)
    For a document's extracted text blob, against one profile:
      anchor_ratio  = Σ(weight of anchor groups with ANY term present) / Σ(weights)
      fingerprint_hits = # of the profile's regexes that match the blob
      confidence    = min(1, anchor_ratio + 0.25·min(fingerprint_hits, 2) + grid_bonus)
    The best client profile (most fingerprint hits, then confidence) names the
    client; if none clears a minimal bar we fall back to `_generic` (stricter
    thresholds, no client prior). The matched profile's thresholds drive the §3
    accept/reject/escalate bands.
================================================================================
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.modules.procurement.enums import SpecType
from app.modules.procurement.sniffing import DocFeatures

GENERIC_CODE = "_generic"


@dataclass
class ProfileView:
    """A plain, session-free view of one `client_template` row."""
    client_code: str
    doc_kind: str
    display_name: str | None = None
    expected_layout: str | None = None
    spec_type_hint: str | None = None
    anchors: list = field(default_factory=list)
    fingerprints: list = field(default_factory=list)
    grid_signals: dict = field(default_factory=dict)
    thresholds: dict = field(default_factory=dict)

    @property
    def accept_high(self) -> float:
        return float((self.thresholds or {}).get("accept_high", 0.80))

    @property
    def reject_low(self) -> float:
        return float((self.thresholds or {}).get("reject_low", 0.30))


@dataclass
class ProfileScore:
    profile: ProfileView
    confidence: float
    fingerprint_hits: int
    signals_matched: list[str]
    anchors_total: int
    anchors_hit: int


def _find_term(blob_low: str, term: str, kind: str | None) -> bool:
    """Is `term` present in the (lowercased) blob? size_tokens match as standalone
    tokens so "S" doesn't match inside "DELIVERS"."""
    t = term.lower()
    if kind == "size_tokens":
        return re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", blob_low) is not None
    return t in blob_low


def score_profile(feats: DocFeatures, profile: ProfileView) -> ProfileScore:
    blob = feats.text_blob or ""
    blob_low = blob.lower()
    signals: list[str] = []

    # ── weighted anchors ────────────────────────────────────────────────────
    total_w = hit_w = 0.0
    anchors_total = anchors_hit = 0
    for group in profile.anchors or []:
        terms = group.get("any") or []
        weight = float(group.get("weight", 1))
        kind = group.get("kind")
        total_w += weight
        anchors_total += 1
        matched = [t for t in terms if _find_term(blob_low, t, kind)]
        if matched:
            hit_w += weight
            anchors_hit += 1
            label = "size_tokens" if kind == "size_tokens" else "anchor"
            signals.append(f"{label}:{','.join(matched[:5])}")
    anchor_ratio = (hit_w / total_w) if total_w else 0.0

    # ── fingerprints (strong client identity) ───────────────────────────────
    fp_hits = 0
    for pat in profile.fingerprints or []:
        try:
            m = re.search(pat, blob, re.IGNORECASE)
        except re.error:
            continue
        if m:
            fp_hits += 1
            signals.append(f"fingerprint:{m.group(0).strip()[:40]}")

    # ── spec grid-shape bonus (a by-product, helps separate the two spec shapes)
    grid_bonus = 0.0
    if profile.doc_kind == "spec_sheet" and profile.grid_signals:
        gs = profile.grid_signals
        want = gs.get("spec_type")
        detected = detect_spec_type(feats)
        if want and detected == want:
            grid_bonus = 0.15
            signals.append(f"grid:{detected}")

    confidence = min(1.0, anchor_ratio + 0.25 * min(fp_hits, 2) + grid_bonus)
    return ProfileScore(
        profile=profile, confidence=confidence, fingerprint_hits=fp_hits,
        signals_matched=signals, anchors_total=anchors_total, anchors_hit=anchors_hit,
    )


def best_match(feats: DocFeatures, profiles: list[ProfileView]) -> ProfileScore:
    """Pick the best client profile; fall back to `_generic` if nothing clears a
    minimal bar. Ranking favours fingerprint hits first (a hard client signal),
    then confidence."""
    client_scores = [
        score_profile(feats, p) for p in profiles if p.client_code != GENERIC_CODE
    ]
    generic = next((p for p in profiles if p.client_code == GENERIC_CODE), None)
    generic_score = score_profile(feats, generic) if generic else None

    ranked = sorted(
        client_scores, key=lambda s: (s.fingerprint_hits, s.confidence), reverse=True
    )
    best = ranked[0] if ranked else None
    # A client profile only "wins" if it shows a real signal (a fingerprint or a
    # meaningful anchor ratio); otherwise the stricter generic profile decides.
    if best and (best.fingerprint_hits > 0 or best.confidence >= 0.40):
        return best
    if generic_score is not None:
        return generic_score
    return best or generic_score or ProfileScore(
        profile=ProfileView(client_code=GENERIC_CODE, doc_kind=feats.ext),
        confidence=0.0, fingerprint_hits=0, signals_matched=[],
        anchors_total=0, anchors_hit=0,
    )


def detect_spec_type(feats: DocFeatures) -> str:
    """Cheap MEASUREMENT_GRID vs NARRATIVE_TECHPACK discriminator (Stage 1 classifies
    the shape; Stage 2 reads the numbers). A wide, numeric grid ⇒ measurement grid
    (Beau Geste 67×268); a tall, narrow, low-numeric key→value sheet ⇒ narrative
    tech pack (Jackie 56×3)."""
    if feats.n_cols >= 8 and feats.numeric_ratio >= 0.20:
        return SpecType.MEASUREMENT_GRID.value
    if feats.n_cols and feats.n_cols <= 5:
        return SpecType.NARRATIVE_TECHPACK.value
    # Fallback: numeric density decides.
    return (
        SpecType.MEASUREMENT_GRID.value
        if feats.numeric_ratio >= 0.20
        else SpecType.NARRATIVE_TECHPACK.value
    )
