"""
================================================================================
modules/clients/utlis.py — the deterministic code makers
================================================================================
THE ONE PLACE A STYLE / SKU CODE IS BUILT. `clients/service.py` re-exports these
rather than keeping its own copies: it used to define a second `_slug` and
`make_sku_code`, and `imports/load_to_db.py` imported THAT one while
`clients/repository.py` imported this one. Two identical functions in two modules
is a format change applied to half the system — which is exactly the shape of bug
this file now prevents.

DETERMINISTIC => IDEMPOTENT. The same order/style/colour/size always produces the
same code, so a re-import matches what is already there instead of duplicating
it. (The importer additionally matches Style on (order, name) and SKU on
(style, colour, size), and only sets `code` on CREATION — so changing the format
here never rewrites an existing row's code, and never breaks a printed label.)
================================================================================
"""
import re


def _slug(s: str | None) -> str:
    """UPPER, runs of non-alnum -> single '_', trimmed. '' -> 'NA'."""
    out = re.sub(r"[^A-Za-z0-9]+", "_", (s or "").strip()).strip("_").upper()
    return out or "NA"


def make_style_code(order_number: str | None, style_name: str | None,
                    article: str | None = None) -> str:
    """Deterministic, readable, one-per-style code.

        make_style_code('JP', 'CLERMONT + VEST')              -> 'JP-CLERMONT_VEST'
        make_style_code('JP', 'CLERMONT + VEST', 'GOAT SUEDE')
                                          -> 'JP-CLERMONT_VEST-GOAT_SUEDE'

    THE ARTICLE SEGMENT IS OMITTED WHEN THERE IS NO ARTICLE — it is not rendered
    as 'NA'. Two reasons, and both matter:

      • BACKWARD COMPATIBILITY. A style with no article produces the exact code
        it produced before this parameter existed, so adding the article changes
        the format only for rows that actually have one. Nothing already in the
        database or on a printed traveler shifts underneath.
      • A PLACEHOLDER IS NOISE. 'JP-CLERMONT_VEST-NA-DARK_BROWN-46' makes every
        code longer and tells the manager reading it nothing. The article is on
        the sticker precisely because it is information; a segment that is
        permanently 'NA' is the opposite.

    Codes are therefore variable-length by segment count. Nothing parses them
    positionally — they are matched whole or by prefix — so that is safe; do not
    introduce `code.split('-')[2]` anywhere.
    """
    parts = [_slug(order_number), _slug(style_name)]
    if (article or "").strip():
        parts.append(_slug(article))
    return "-".join(parts)


def make_sku_code(order_number: str | None, style_name: str | None,
                  colour: str | None, size: str | None,
                  article: str | None = None) -> str:
    """Deterministic, globally-unique, readable SKU code.

        make_sku_code('JP','CLERMONT + VEST','DARK BROWN','46','GOAT SUEDE')
            -> 'JP-CLERMONT_VEST-GOAT_SUEDE-DARK_BROWN-46'

    BUILT ON make_style_code, NOT ALONGSIDE IT. The style code must be the exact
    prefix of every SKU code beneath it — that is how a manager holding traveler
    JP-CLERMONT_VEST-GOAT_SUEDE-DARK_BROWN-46 reads the style code off the front,
    and how the article lands in the same position in both. It used to be a
    copy-pasted pair of segments; composing the two functions makes the prefix
    property true by construction instead of by agreement.

    The piece code is in turn `{sku_code}-{seq:03d}` (imports/premint.py), so the
    article reaches the individual garment for free.
    """
    return "-".join((
        make_style_code(order_number, style_name, article),
        _slug(colour), _slug(size),
    ))


def sku_label(style_name, color_name, color_code, size) -> str:
    """Display name (not a code) — human punctuation, no slugging."""
    colour = color_name or color_code or "NA"
    return " · ".join(p for p in (style_name or "NA", colour, size or "NA"))
