"""
================================================================================
modules/wages/proration.py — Monthly salary allocation across a run window
================================================================================
Pure functions, no I/O, no DB. Unit-testable against a calendar and nothing else.

WHY THIS EXISTS
    The manager types period_start and period_end by hand, so a run window will
    routinely land off calendar-month boundaries (24 Apr .. 12 May). A monthly
    employee cannot be paid a full salary for that window, and cannot be paid
    nothing either. Allocation is per calendar month:

        month_share = salary * (days_of_that_month_inside_window / days_in_month)

    A full calendar month yields exactly `salary`. February and August both pay a
    whole month despite 28 vs 31 days — that is what "monthly salary" means.

THE PROPERTY THAT MATTERS
    Consecutive non-overlapping windows that tile a month sum to exactly one
    salary. 1-15 Apr + 16-30 Apr == salary, not 1.03 * salary. Rounding happens
    once at the end of each call, so the sum can drift by at most a paisa or two
    across many splits — acceptable, and far better than the alternative of
    dividing by a fixed 30 which overpays every February.
================================================================================
"""
from calendar import monthrange
from datetime import date, timedelta


def days_in_month(year: int, month: int) -> int:
    """Calendar length of a month. 2024-02 -> 29."""
    return monthrange(year, month)[1]


def month_spans(start: date, end: date) -> list[tuple[date, date, int]]:
    """Split an INCLUSIVE window into (span_start, span_end, days_in_that_month).

    24 Apr .. 2 May  ->  [(2026-04-24, 2026-04-30, 30), (2026-05-01, 2026-05-02, 31)]

    Returns [] when the window is inverted. Exposed separately so a payslip can
    show the manager the month-by-month breakdown behind a prorated figure.
    """
    if end < start:
        return []
    spans: list[tuple[date, date, int]] = []
    cursor = start.replace(day=1)
    while cursor <= end:
        dim = days_in_month(cursor.year, cursor.month)
        month_end = cursor.replace(day=dim)
        lo, hi = max(cursor, start), min(month_end, end)
        if lo <= hi:
            spans.append((lo, hi, dim))
        cursor = month_end + timedelta(days=1)
    return spans


def prorate_monthly(salary: float, start: date, end: date) -> float:
    """Salary owed for the INCLUSIVE window [start, end].

    A window covering exactly one calendar month returns `salary` unchanged.
    Returns 0.0 for a falsy salary or an inverted window.
    """
    if not salary or end < start:
        return 0.0
    total = 0.0
    for lo, hi, dim in month_spans(start, end):
        total += float(salary) * (((hi - lo).days + 1) / dim)
    return round(total, 2)
