"""
UNIT · BOM costing math + the native-term/style-name matching helpers. No database,
no event loop — these are pure functions/predicates lifted straight out of
app/modules/bom/costing.py and app/modules/bom/service.py.

THE CLAIM UNDER TEST is that the money math (costing.recompute_bom) and the two
"guess it from a string" helpers (PomDict/suggest_pom_code term resolution,
BomService._style_codes/_suggest_by_name filename matching) are correct in
isolation, before a DB round-trip or an HTTP request ever touches them. The
worked reference for costing is BMO-1 (CRIMIE / CRI 02F5 PL02, FOB US$107.25),
taken directly from costing.py's own docstring.
"""
from decimal import Decimal

import pytest

from app.modules.bom.costing import compute_line, recompute_bom
from app.modules.bom.service import BomService, PomDict, suggest_pom_code


# ══════════════════════════════════════════════════ costing.compute_line
def test_compute_line_matches_the_bmo1_reference():
    """34.5 dm² of leather @ $1.80, 60 garments — the worked example in the module
    docstring. Any drift here is a drift in every BOM's FOB price."""
    d = compute_line(qty_per_garment=Decimal("34.5"), unit_price=Decimal("1.80"), order_qty=60)
    assert d["total_cost"] == Decimal("62.10")
    assert d["bulk_qty"] == Decimal("2070.000")
    assert d["line_bulk"] == Decimal("3726.00")


def test_compute_line_defaults_qty_to_1_for_non_material_lines():
    """Manufacturing/packaging/FOB-charge lines carry their cost in unit_price with
    qty_per_garment left unset — compute_line must default that to 1, not 0."""
    d = compute_line(qty_per_garment=None, unit_price=Decimal("30.00"), order_qty=60)
    assert d["total_cost"] == Decimal("30.00")
    assert d["bulk_qty"] == Decimal("60.000")


def test_compute_line_treats_missing_price_as_zero():
    """A buyer/client-supplied line (unit_price never set) must cost 0, not error."""
    d = compute_line(qty_per_garment=Decimal("2"), unit_price=None, order_qty=10)
    assert d["total_cost"] == Decimal("0.00")


def test_compute_line_rounds_half_up_to_the_cent():
    d = compute_line(qty_per_garment=Decimal("1"), unit_price=Decimal("0.125"), order_qty=1)
    assert d["total_cost"] == Decimal("0.13")


def test_compute_line_accepts_float_and_string_input():
    """The API layer hands recompute_bom Decimals coerced from float PATCH values —
    compute_line must coerce just as safely when called with a bare float/str."""
    d = compute_line(qty_per_garment=34.5, unit_price="1.80", order_qty=60)
    assert d["total_cost"] == Decimal("62.10")


# ══════════════════════════════════════════════════ costing.recompute_bom
def test_recompute_bom_rolls_up_to_the_bmo1_header_total():
    """order_qty=60, one 34.5 dm² leather line @1.80 (=62.10/garment) plus a flat
    45.15 of non-material charges -> FOB 107.25, bulk 6,435.00 (60 x 107.25)."""
    lines = [
        {"qty_per_garment": Decimal("34.5"), "unit_price": Decimal("1.80")},
        {"qty_per_garment": None, "unit_price": Decimal("45.15")},  # flat per-garment charge
    ]
    result = recompute_bom(lines, order_qty=60)
    assert result["garment_fob_price"] == Decimal("107.25")
    assert result["bulk_total"] == Decimal("6435.00")
    assert len(result["lines"]) == 2


def test_recompute_bom_with_no_lines_is_zero_not_an_error():
    """A BOM whose spec extraction failed before any line seeded (GB‑B1 in the
    manual test plan) must recompute to zero totals, never raise."""
    result = recompute_bom([], order_qty=500)
    assert result["garment_fob_price"] == Decimal("0.00")
    assert result["bulk_total"] == Decimal("0.00")
    assert result["lines"] == []


def test_recompute_bom_accepts_objects_not_just_dicts():
    """BomService._recompute passes plain dicts today, but recompute_bom is
    documented to also accept attribute-bearing objects — lock that contract."""
    class Line:
        qty_per_garment = Decimal("2")
        unit_price = Decimal("5")

    result = recompute_bom([Line()], order_qty=10)
    assert result["garment_fob_price"] == Decimal("10.00")


# ══════════════════════════════════════════════════ PomDict (native term -> pom_code)
def test_pomdict_resolves_an_exact_seeded_term():
    d = PomDict([("en", "Chest Width", "CHEST"), ("ja", "胸囲", "CHEST")])
    assert d.resolve("Chest Width") == "CHEST"


def test_pomdict_is_whitespace_and_case_insensitive():
    """_norm_term strips ALL whitespace and lowercases — 'Chest  Width' and
    'chestwidth' must resolve to the same dictionary key."""
    d = PomDict([("en", "Chest Width", "CHEST")])
    assert d.resolve("  chestwidth ") == "CHEST"


def test_pomdict_prefers_the_detected_language_then_falls_back():
    """Two dictionaries disagree on a term across languages; passing the term's own
    detected language must pick that language's mapping, not just the first-seen one."""
    d = PomDict([("en", "waist", "WAIST_EN"), ("ja", "waist", "WAIST_JA")])
    assert d.resolve("waist", language="ja") == "WAIST_JA"
    assert d.resolve("waist", language="fr") == "WAIST_EN"   # no fr row -> any-language fallback


def test_pomdict_miss_returns_none():
    d = PomDict([("en", "chest", "CHEST")])
    assert d.resolve("inseam") is None


# ══════════════════════════════════════════════════ suggest_pom_code (alias/fuzzy/LLM ladder)
def test_suggest_pom_code_alias_substring_hit():
    """'torace' is a seeded CHEST alias (Italian) — an exact alias substring match
    must win at the highest non-LLM confidence tier (0.85) without ever calling the LLM."""
    code, conf = suggest_pom_code("torace superiore", llm_json_call=None)
    assert code == "CHEST"
    assert conf == 0.85


def test_suggest_pom_code_fuzzy_hit_above_threshold():
    """A near-miss spelling of a seeded alias should still resolve via fuzzy match,
    at a lower (0.75x ratio) confidence than an exact alias hit."""
    code, conf = suggest_pom_code("shulder", llm_json_call=None)   # 1-letter typo of "shoulder"
    assert code == "SHOULDER"
    assert 0 < conf < 0.85


def test_suggest_pom_code_no_match_and_no_llm_returns_none():
    code, conf = suggest_pom_code("xyzzyx unmatched term", llm_json_call=None)
    assert code is None
    assert conf == 0.0


def test_suggest_pom_code_blank_term_short_circuits_without_calling_the_llm():
    calls = []
    code, conf = suggest_pom_code("   ", llm_json_call=lambda p: calls.append(p))
    assert (code, conf) == (None, 0.0)
    assert calls == []          # the LLM must never be invoked for an empty term


def test_suggest_pom_code_falls_back_to_the_llm_only_when_alias_and_fuzzy_both_miss():
    """A native term with no romanized alias (e.g. Japanese armhole '袖ぐり' minus its
    seeded alias) should reach the LLM rung, and a valid constrained JSON answer wins."""
    def fake_llm(prompt):
        assert "SHOULDER" in prompt or "CHEST" in prompt   # the allowed-codes list is passed through
        return {"pom_code": "shoulder"}                    # lower-case on purpose: must be upper()'d

    code, conf = suggest_pom_code("totally unrecognised measurement label", llm_json_call=fake_llm)
    assert code == "SHOULDER"
    assert conf == 0.6


def test_suggest_pom_code_ignores_an_llm_code_outside_the_allowed_set():
    """A hallucinated code that isn't in the allowed vocabulary must be discarded,
    not passed through to the caller as if it were resolved."""
    code, conf = suggest_pom_code("mystery label", llm_json_call=lambda p: {"pom_code": "NOT_A_REAL_CODE"})
    assert (code, conf) == (None, 0.0)


def test_suggest_pom_code_llm_exception_degrades_to_none():
    """The LLM ladder must never let a downstream failure bubble up and break BOM
    generation — extraction.py's whole design is 'never raise, degrade'."""
    def boom(prompt):
        raise RuntimeError("LLM backend unreachable")

    code, conf = suggest_pom_code("mystery label", llm_json_call=boom)
    assert (code, conf) == (None, 0.0)


# ══════════════════════════════════════════════════ BomService._style_codes / _suggest_by_name
@pytest.fixture
def svc():
    """A DB-free BomService — the matching helpers under test touch neither
    self.db nor self.repo, so a session-less instance is enough."""
    return BomService(db=None)


def test_style_codes_extracts_normalized_codes_from_a_messy_filename(svc):
    codes = svc._style_codes("SP-64806 (SP74006)")
    assert codes == {"SP64806", "SP74006"}


def test_style_codes_requires_hyphen_or_space_not_underscore_as_separator(svc):
    """_CODE_RE's separator class is `[- ]` (hyphen/space only) and requires
    3-6 digits right after the letters. The method's own docstring claims
    'CLEREMONT_15-06-26-P53' -> {'P53'}, but that string has neither: '_'
    isn't in the separator class, and 'P53' has only 2 digits (the regex
    needs >= 3). Actual behaviour today is an EMPTY set for that input — this
    pins the real behaviour so a docstring/regex fix is a deliberate, visible
    change here, not a silent one.
    """
    assert svc._style_codes("CLEREMONT_15-06-26-P53") == set()
    # the hyphen-separated form the docstring's OTHER example uses does work:
    assert svc._style_codes("CLEREMONT-P530") == {"P530"}


def test_style_codes_of_empty_text_is_empty_set(svc):
    assert svc._style_codes("") == set()
    assert svc._style_codes(None) == set()


class _Candidate:
    def __init__(self, name):
        self.name = name


def test_suggest_by_name_single_code_match_wins(svc):
    candidates = [_Candidate("SP74006_pattern.dxf"), _Candidate("SP99999_pattern.dxf")]
    hit = svc._suggest_by_name("Order SP-74006", candidates, key=lambda c: c.name)
    assert hit is candidates[0]


def test_suggest_by_name_ambiguous_code_match_returns_none(svc):
    """Two candidates share the same embedded style code — the safer outcome is no
    pre-selection at all (force a manual click) rather than a coin-flip guess."""
    candidates = [_Candidate("SP74006_v1.dxf"), _Candidate("SP74006_v2.dxf")]
    hit = svc._suggest_by_name("Order SP-74006", candidates, key=lambda c: c.name)
    assert hit is None


def test_suggest_by_name_falls_back_to_word_overlap_when_no_code_present(svc):
    candidates = [_Candidate("Clermont Jacket Spec"), _Candidate("Tower Coat Spec")]
    hit = svc._suggest_by_name("CLERMONT", candidates, key=lambda c: c.name)
    assert hit is candidates[0]


def test_suggest_by_name_no_candidates_returns_none(svc):
    assert svc._suggest_by_name("CLERMONT", [], key=lambda c: c.name) is None
