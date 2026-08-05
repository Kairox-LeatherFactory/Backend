"""
UNIT · money math. No DB, no I/O, no fixtures.

Highest-priority layer under deadline pressure: these are the functions that
decide what a person is paid. Every one is pure, so a failure here is a real
arithmetic defect and never a harness problem.

Covers `app/modules/wages/proration.py` in full.
"""
from datetime import date

import pytest

from app.modules.wages.proration import days_in_month, month_spans, prorate_monthly

SALARY = 30000.0


# ── days_in_month ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("year,month,expected", [
    (2026, 1, 31), (2026, 2, 28), (2026, 4, 30), (2026, 12, 31),
    (2024, 2, 29),          # leap
    (2000, 2, 29),          # divisible by 400 -> leap
    (1900, 2, 28),          # divisible by 100, not 400 -> not leap
])
def test_days_in_month(year, month, expected):
    assert days_in_month(year, month) == expected


# ── month_spans ──────────────────────────────────────────────────────────────
def test_month_spans_splits_across_the_boundary():
    """The docstring's own worked example, pinned."""
    assert month_spans(date(2026, 4, 24), date(2026, 5, 2)) == [
        (date(2026, 4, 24), date(2026, 4, 30), 30),
        (date(2026, 5, 1), date(2026, 5, 2), 31),
    ]


def test_month_spans_single_full_month_is_one_span():
    assert month_spans(date(2026, 4, 1), date(2026, 4, 30)) == [
        (date(2026, 4, 1), date(2026, 4, 30), 30)]


def test_month_spans_single_day():
    assert month_spans(date(2026, 4, 15), date(2026, 4, 15)) == [
        (date(2026, 4, 15), date(2026, 4, 15), 30)]


def test_month_spans_inverted_window_is_empty():
    assert month_spans(date(2026, 5, 2), date(2026, 4, 24)) == []


def test_month_spans_covers_every_day_exactly_once():
    """No gaps, no overlaps — the property proration depends on."""
    start, end = date(2026, 1, 20), date(2026, 4, 9)
    spans = month_spans(start, end)
    days = [lo.toordinal() + i
            for lo, hi, _ in spans
            for i in range((hi - lo).days + 1)]
    assert days == sorted(days)
    assert len(days) == len(set(days))
    assert days[0] == start.toordinal()
    assert days[-1] == end.toordinal()
    assert len(days) == (end - start).days + 1


# ── prorate_monthly ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("month,last", [(2, 28), (4, 30), (8, 31)])
def test_full_calendar_month_pays_exactly_salary(month, last):
    """February and August both pay a whole month. This is the rule that a naive
    `salary / 30 * days` implementation gets wrong, and it is why the module
    exists — see proration.py:14-18."""
    assert prorate_monthly(SALARY, date(2026, month, 1),
                           date(2026, month, last)) == SALARY


def test_half_month_pays_half():
    # April: 15 of 30 days
    assert prorate_monthly(SALARY, date(2026, 4, 1), date(2026, 4, 15)) == 15000.0


def test_split_windows_tiling_a_month_sum_to_one_salary():
    """THE property named in the module docstring (proration.py:20-26):
    1-15 Apr + 16-30 Apr == salary, not 1.03 x salary."""
    first = prorate_monthly(SALARY, date(2026, 4, 1), date(2026, 4, 15))
    second = prorate_monthly(SALARY, date(2026, 4, 16), date(2026, 4, 30))
    assert first + second == pytest.approx(SALARY, abs=0.02)


def test_window_across_two_months_is_the_sum_of_its_parts():
    whole = prorate_monthly(SALARY, date(2026, 4, 24), date(2026, 5, 2))
    april = SALARY * (7 / 30)    # 24..30 inclusive
    may = SALARY * (2 / 31)      # 1..2 inclusive
    assert whole == pytest.approx(round(april + may, 2), abs=0.01)


def test_leap_february_pays_a_whole_month():
    assert prorate_monthly(SALARY, date(2024, 2, 1), date(2024, 2, 29)) == SALARY


def test_one_day_of_february_pays_more_than_one_day_of_january():
    """A shorter month means each day is worth proportionally more — the direct
    consequence of dividing by the real month length."""
    jan = prorate_monthly(SALARY, date(2026, 1, 5), date(2026, 1, 5))
    feb = prorate_monthly(SALARY, date(2026, 2, 5), date(2026, 2, 5))
    assert feb > jan


@pytest.mark.parametrize("salary", [0, 0.0, None])
def test_falsy_salary_pays_nothing(salary):
    assert prorate_monthly(salary, date(2026, 4, 1), date(2026, 4, 30)) == 0.0


def test_inverted_window_pays_nothing():
    assert prorate_monthly(SALARY, date(2026, 4, 30), date(2026, 4, 1)) == 0.0


def test_result_is_rounded_to_paise():
    got = prorate_monthly(33333.33, date(2026, 4, 7), date(2026, 5, 19))
    assert got == round(got, 2)


def test_a_full_year_pays_twelve_salaries():
    got = prorate_monthly(SALARY, date(2026, 1, 1), date(2026, 12, 31))
    assert got == pytest.approx(SALARY * 12, abs=0.01)


# ── boundary values called out in the audit (pass-12) ────────────────────────
def test_absurdly_long_window_is_not_rejected_here():
    """AUDIT (pass-12-boundary-values.md): `_validate_window` bounds ordering and
    overlap but NOT span, so a 400-day 'fortnight' prices without complaint. This
    test pins the CURRENT behaviour so the intended bound is a visible change,
    not a silent one — the guard belongs in the service, not in this pure helper.
    """
    got = prorate_monthly(SALARY, date(2026, 1, 1), date(2027, 2, 5))
    assert got > SALARY * 13
