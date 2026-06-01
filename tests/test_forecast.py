"""Deterministic forecast engine — hand-checkable numbers, no DB, no model."""
from datetime import date

from app.modules.intelligence.forecast import (
    StyleInput, schedule_status, detect_bottleneck, plan_styles)


def test_behind_schedule_recommends_higher_rate():
    # 152 left, ~2 weeks, doing 5/day -> behind, must raise rate
    r = schedule_status(StyleInput(
        style="CARNABY", ordered=152, produced=0, deadline=date(2026, 6, 8),
        max_daily_capacity=20, recent_daily_rate=5, today=date(2026, 5, 25)))
    assert r.on_schedule is False
    assert r.required_rate > r.current_rate
    assert "Raise daily rate" in r.advice


def test_infeasible_when_capacity_too_low():
    # Need far more than capacity -> at risk, not just "raise rate"
    r = schedule_status(StyleInput(
        style="X", ordered=100, produced=0, deadline=date(2026, 5, 29),
        max_daily_capacity=20, recent_daily_rate=15, today=date(2026, 5, 25)))
    assert r.feasible is False
    assert "not achievable" in r.advice.lower()


def test_on_schedule_when_pace_is_enough():
    r = schedule_status(StyleInput(
        style="Y", ordered=100, produced=80, deadline=date(2026, 6, 30),
        max_daily_capacity=25, recent_daily_rate=20, today=date(2026, 5, 25)))
    assert r.on_schedule is True


def test_bottleneck_identifies_slow_stage():
    rates = {"CUTTING": 40, "PASTING": 8, "FF": 30}
    order = ["CUTTING", "FUSING", "PASTING", "SHELL", "LA", "LS", "FF"]
    res = detect_bottleneck(rates, order)
    crit = [b for b in res if b.severity == "critical"]
    assert any(b.stage == "PASTING" for b in crit)


def test_plan_defers_blocked_style():
    styles = [
        StyleInput("A", 100, 0, date(2026, 6, 10), 20, 10, date(2026, 5, 25)),
        StyleInput("B", 100, 0, date(2026, 6, 15), 20, 0, date(2026, 5, 25)),
    ]
    plan = plan_styles(styles, blocked={"B"})
    blocked_line = [p for p in plan if p.style == "B"][0]
    assert blocked_line.status == "blocked"
    assert blocked_line.daily_target == 0
