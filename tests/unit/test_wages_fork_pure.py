"""
UNIT · the PIECE_RATE / MONTHLY fork, as a pure predicate. No DB.

The single most expensive bug this module can have is paying one person twice:
once for the pieces they cut and again as salary. `_as_wage_type`
(wages/service.py:65-81) is the only thing standing between the two branches —
both branches skip on an `is not` identity test against a WageType member, so a
value that coerces to neither member falls through BOTH and the worker is paid
nothing, while a value that wrongly coerces to a member pays them from the wrong
scheme.

`test_wage_math.py` already owns proration arithmetic. This file owns the FORK.
"""
import pytest

from app.core.enums import WageType
from app.modules.wages.service import _MONTHLY_SALARY_MISSING, _as_wage_type


# ══════════════════════════════════════════════════ happy path: real members
@pytest.mark.money
@pytest.mark.parametrize("member", list(WageType))
def test_a_real_member_passes_through_unchanged(member):
    assert _as_wage_type(member) is member


# ═════════════════════════════════════════════ common variation: ORM strings
@pytest.mark.money
@pytest.mark.parametrize("raw,expected", [
    ("monthly", WageType.MONTHLY),
    ("piece_rate", WageType.PIECE_RATE),
    ("MONTHLY", WageType.MONTHLY),
    ("Piece_Rate", WageType.PIECE_RATE),
])
def test_a_raw_string_from_the_orm_still_reaches_the_right_branch(raw, expected):
    """The docstring at service.py:66-73 says raw strings have leaked through
    before because employees compares wage_type with .lower(). An `is` check
    against a string fails silently and would send a monthly worker into the
    piece loop — so the coercion is load-bearing, not defensive dressing."""
    assert _as_wage_type(raw) is expected


# ══════════════════════════════════════════════════════ the exclusivity proof
@pytest.mark.money
@pytest.mark.parametrize("value", [
    WageType.MONTHLY, WageType.PIECE_RATE, "monthly", "piece_rate",
])
def test_no_value_can_satisfy_both_branches(value):
    """The invariant stated in CLAUDE.md §10: 'PIECE_RATE and MONTHLY are
    mutually exclusive — never both'. Both branches test the SAME coerced value
    with `is`, so satisfying both is impossible by construction. This pins that
    the coercion is a single function and not two divergent ones."""
    coerced = _as_wage_type(value)
    in_piece = coerced is WageType.PIECE_RATE
    in_monthly = coerced is WageType.MONTHLY
    assert not (in_piece and in_monthly)
    assert in_piece or in_monthly


# ═════════════════════════════════════════════════════ error / unknown input
@pytest.mark.money
@pytest.mark.parametrize("junk", [
    "daily", "", "  ", "hourly", "PIECE RATE", "piece-rate", 0, 1, [], {},
])
def test_an_unrecognised_wage_type_coerces_to_none_not_a_guess(junk):
    """A guess here is a mispayment. None is correct: it fails both branch
    tests, the worker is excluded, and `_populate_run` reports them under
    `excluded_untyped_employees` (service.py:417-419, :518-522) rather than
    swallowing them."""
    assert _as_wage_type(junk) is None


@pytest.mark.money
def test_none_stays_none():
    assert _as_wage_type(None) is None


# ══════════════════════════════════════ the monthly-salary-missing sentinel
@pytest.mark.money
def test_the_salary_missing_sentinel_cannot_collide_with_a_style_id():
    """`unrated` is keyed by (style_id, op_id) tuples AND by
    (_MONTHLY_SALARY_MISSING, employee_id) tuples (service.py:480). `_name_unrated`
    splits them on `k[0] != _MONTHLY_SALARY_MISSING` (service.py:547). A UUID can
    never equal this string, so the two key spaces cannot collide — which is what
    stops a missing salary from being resolved as a style code and 404ing."""
    import uuid
    assert isinstance(_MONTHLY_SALARY_MISSING, str)
    assert _MONTHLY_SALARY_MISSING != str(uuid.uuid4())
    assert not _MONTHLY_SALARY_MISSING.replace("_", "").isdigit()
