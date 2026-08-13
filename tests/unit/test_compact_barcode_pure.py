"""
UNIT · the compact piece code (bug #19) — pure, no database.

WHAT THIS DEFENDS
    The scanned value is now a small id (`PC-23456A`) and the business identity
    moved to the printed text. Three properties make that safe, and all three are
    cheap to break by "tidying" the alphabet:

      1. ROUND TRIP      encode(n) must decode back to n, or the counter drifts
                         and the next mint collides on uq_barcode_code.
      2. LEXICOGRAPHIC == NUMERIC ORDER
                         `max_short_code_counter` finds the highest code with
                         ORDER BY code DESC LIMIT 1 — one row instead of the whole
                         prefix. That is only correct while the fixed-width
                         encoding sorts the same way in text as in numbers, which
                         requires the alphabet to be in ascending ASCII order.
                         Reorder the alphabet and minting silently starts reusing
                         codes.
      3. NON-CODES DECODE TO ZERO
                         A legacy long code, a null, or a `PC-` row with a foreign
                         character must not raise and must not raise the maximum.
                         (This is the class of bug that jams `_next_code` — see
                         test_barcode_lifecycle.py — so the compact codec is built
                         not to have it.)

    Also pinned: the alphabet excludes 0/O and 1/I, the four characters a human
    re-keying a smudged label confuses.
"""
import pytest

from app.modules.barcode.repository import (
    SHORT_CODE_PREFIX, SHORT_CODE_WIDTH, _B30, decode_short, encode_short,
)

pytestmark = pytest.mark.integrity


def test_the_alphabet_has_no_ambiguous_characters():
    for banned in "01OI":
        assert banned not in _B30, f"{banned!r} is confusable on a printed label"
    assert len(_B30) == len(set(_B30)) == 30


def test_the_alphabet_is_ascii_ascending():
    """The whole one-row `ORDER BY code DESC` optimisation rests on this."""
    assert list(_B30) == sorted(_B30)


@pytest.mark.parametrize("n", [0, 1, 2, 29, 30, 31, 899, 900, 12_345, 728_999_999])
def test_encode_decode_round_trips(n):
    code = encode_short(n)
    assert code.startswith(f"{SHORT_CODE_PREFIX}-")
    assert len(code) == len(SHORT_CODE_PREFIX) + 1 + SHORT_CODE_WIDTH
    assert decode_short(code) == n


def test_codes_are_short_enough_to_be_worth_the_change():
    """The point of bug #19: the symbol shrinks. A long code was ~24 chars."""
    assert len(encode_short(500_000)) == 9


def test_lexicographic_order_equals_numeric_order():
    codes = [encode_short(n) for n in range(0, 20_000)]
    assert codes == sorted(codes)
    # and the max of the strings is the encoding of the max of the numbers
    assert max(codes) == encode_short(19_999)


def test_the_encoding_is_injective():
    codes = [encode_short(n) for n in range(0, 20_000)]
    assert len(set(codes)) == len(codes)


@pytest.mark.parametrize("junk", [
    None, "", "   ", "PC-", "NOPE-1",
    "KJ2451-CLERMONT-57-M-005",     # a legacy long piece code
    "EMP-000123", "DRW-0001", "LOT-LEA-000001",
    "PC-ZZZ0ZZ",                    # '0' is deliberately not in the alphabet
    "PC-ZZZIZZ",                    # nor is 'I'
])
def test_anything_that_is_not_a_compact_code_decodes_to_zero(junk):
    """Never raises, never inflates the counter."""
    assert decode_short(junk) == 0


def test_decoding_is_case_and_whitespace_insensitive():
    code = encode_short(4242)
    assert decode_short(f"  {code.lower()}  ") == 4242
