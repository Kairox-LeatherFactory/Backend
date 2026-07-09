"""fabric_roles.py â€” map a DXF FABRIC string to a BOM role + category + leather flag.
[Stage 2 in ARCHITECTURE_AND_DATAFLOW.md â€” attribute_fabrics: which fabrics are leather.]

WHY A LEXICON, NOT A SPEC-STRING MATCH (the open decision, resolved)
--------------------------------------------------------------------
A DXF piece carries a FABRIC label in the CAD operator's language (this CreaCompo
file: è¡¨ç”Ÿåœ° / åˆ¥å¸ƒ / ã‚¹ãƒ¬ãƒ¼ã‚­ / å¹³ã‚´ãƒ ). The spec sheet carries the *trade* names in
romanised English ("Sheep Glass", "Goat Suede"). Those two vocabularies do NOT
string-match across languages, and fuzzy cross-lingual matching is exactly the
"silent wrong data" failure extraction.py was built to avoid. The CAD terms,
however, are STABLE factory conventions that recur on every CreaCompo export.

So the split is:
  * fabric string -> ROLE/CATEGORY/leather?   â† this lexicon (stable CAD vocab)
  * role          -> SPECIES (sheep/goat/...)  â† from the spec materials at generate
                                                 time (main material's species, etc.)

This keeps the geometry attribution (which the DXF owns) separate from the
material identity (which the spec owns), and never guesses a leather species from
a Japanese cloth label.

WHAT IS / ISN'T LEATHER (the DCM correction)
--------------------------------------------
DCM is leather consumed PER LEATHER MATERIAL. Pocketing (ã‚¹ãƒ¬ãƒ¼ã‚­) and elastic
(å¹³ã‚´ãƒ ) are NOT leather â€” summing their net area into the leather DCM is what made
the old "all-pieces" reconciliation land on a bogus 1.73 yield. Only is_leather
roles feed a leather DCM line; the rest become lining / accessory lines (or are
dropped from leather consumption entirely).

Unknown fabric strings fall through to UNKNOWN_ROLE (role=None) so the caller can
surface them for a human to add one lexicon row â€” same posture as pom_dictionary.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FabricRole:
    role: str            # "main" | "sub_material" | "lining" | "pocketing" | "elastic" | "interlining"
    category: str        # BomItemCategory value the line should carry
    is_leather: bool     # True -> feeds a leather DCM line; False -> lining/accessory


# BomItemCategory values (kept as plain strings so this module has no app import).
_MAIN = "main_material"
_SUB = "sub_material"
_LINING = "lining"
_INTERLINING = "interlining"
_ACCESSORY = "accessory"

UNKNOWN_ROLE = FabricRole(role="", category=_LINING, is_leather=False)

# CreaCompo / JP CAD fabric vocabulary. Keys are matched case-folded by substring,
# so è¡¨ç”Ÿåœ° matches "è¡¨ç”Ÿåœ°(æœ¬ä½“)" too. Romanised aliases included for other CAD systems.
# This is the ONE table a non-technical operator extends (mirrors pom_dictionary).
DEFAULT_FABRIC_LEXICON: dict[str, FabricRole] = {
    # leather-bearing roles
    "表生地": FabricRole("main", _MAIN, True),           # omote-kiji: main shell
    "表地":   FabricRole("main", _MAIN, True),
    "main":   FabricRole("main", _MAIN, True),
    "shell":  FabricRole("main", _MAIN, True),
    "別布":   FabricRole("sub_material", _SUB, True),     # beppu: contrast
    "別生地": FabricRole("sub_material", _SUB, True),
    "contrast": FabricRole("sub_material", _SUB, True),
    # non-leather roles
    "裏地":   FabricRole("lining", _LINING, False),       # uraji: lining
    "裏生地": FabricRole("lining", _LINING, False),
    "lining": FabricRole("lining", _LINING, False),
    "スレーキ": FabricRole("pocketing", _LINING, False),   # pocketing textile
    "ポケット布": FabricRole("pocketing", _LINING, False),
    "pocket": FabricRole("pocketing", _LINING, False),
    "芯地":   FabricRole("interlining", _INTERLINING, False),
    "interlining": FabricRole("interlining", _INTERLINING, False),
    "平ゴム": FabricRole("elastic", _ACCESSORY, False),    # flat elastic
    "ゴム":   FabricRole("elastic", _ACCESSORY, False),
    "elastic": FabricRole("elastic", _ACCESSORY, False),
}


def resolve_fabric_role(fabric: str, lexicon: dict[str, FabricRole] | None = None) -> FabricRole | None:
    """Map a raw DXF FABRIC string to a FabricRole. Returns None when unknown (the
    caller surfaces it for a human to add a lexicon row). Match is case-folded
    substring so labels with suffixes/parentheses still resolve."""
    lex = lexicon or DEFAULT_FABRIC_LEXICON
    f = (fabric or "").strip()
    if not f:
        return None
    # exact first (cheap + unambiguous), then substring containment
    if f in lex:
        return lex[f]
    fold = f.casefold()
    for key, role in lex.items():
        k = key.casefold()
        if k in fold or fold in k:
            return role
    return None


def attribute_fabrics(parsed_fabrics: list[str],
                      lexicon: dict[str, FabricRole] | None = None) -> tuple[dict[str, FabricRole], list[str]]:
    """Resolve every distinct fabric string in a ParsedPattern.

    Returns ({fabric_string: FabricRole}, [unknown_fabric_strings]). Unknown
    strings are reported so the generate response can flag "add a fabric_role row"
    rather than silently dropping leather. Persisted as PatternExtraction.fabric_roles.
    """
    mapping: dict[str, FabricRole] = {}
    unknown: list[str] = []
    for fab in parsed_fabrics:
        role = resolve_fabric_role(fab, lexicon)
        if role is None:
            unknown.append(fab)
        else:
            mapping[fab] = role
    return mapping, unknown