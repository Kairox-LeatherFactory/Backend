"""
================================================================================
modules/intelligence/forecast.py — Deterministic production forecasting engine
================================================================================

PURPOSE
    The factory's *brain*: pure arithmetic over production numbers. NO LLM, NO
    model, NO API cost. This is what actually answers the MD's questions:
      - "Is the Carnaby style on schedule?"        -> schedule_status()
      - "What daily rate finishes it on time?"     -> required_rate()
      - "Which department is slowing us down?"      -> bottleneck()
      - "Leather for style X is late — re-plan."    -> replan()

    These are STRUCTURED-DATA problems (counts, dates, rates), so they're solved
    exactly with math — not RAG, not embeddings. A future LLM chatbot calls these
    functions as tools and just phrases the numeric answer in English.

WHY PURE FUNCTIONS
    No DB, no I/O here — just inputs -> dataclass outputs. That makes every rule
    unit-testable with hand-checkable numbers, and lets the same engine run from
    an API route, a scheduled report, or a chatbot tool unchanged.
================================================================================
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, timedelta


@dataclass
class StyleInput:
    """Everything the engine needs to judge one style's schedule."""
    style: str
    ordered: int                 # total pieces ordered (final-stage target)
    produced: int                # pieces through the final stage so far
    deadline: date
    max_daily_capacity: int      # max pieces/day this style can realistically do
    recent_daily_rate: float     # observed avg pieces/day over recent history
    today: date = field(default_factory=date.today)


@dataclass
class ScheduleResult:
    style: str
    on_schedule: bool
    remaining: int
    working_days_left: int
    required_rate: float          # pieces/day needed from now to hit the deadline
    current_rate: float
    projected_finish: date | None
    days_late: int                # 0 if on time; >0 = forecast days past deadline
    feasible: bool                # can it be done at all within capacity?
    headline: str                 # one-line plain-English verdict
    advice: str


def _working_days(start: date, end: date) -> int:
    """Mon–Sat working days between start (exclusive) and end (inclusive).
    Leather factories typically run 6 days; tweak if your week differs."""
    if end <= start:
        return 0
    days = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() != 6:      # 6 = Sunday off
            days += 1
    return days


def schedule_status(s: StyleInput) -> ScheduleResult:
    remaining = max(s.ordered - s.produced, 0)
    wdays = _working_days(s.today, s.deadline)

    # Required rate to finish exactly on the deadline.
    required = (remaining / wdays) if wdays > 0 else float("inf")
    current = max(s.recent_daily_rate, 0.0)

    # Projected finish at the current observed rate.
    if remaining == 0:
        projected, days_late, on_sched = s.today, 0, True
    elif current <= 0:
        projected, days_late, on_sched = None, 9999, False
    else:
        days_needed = math.ceil(remaining / current)
        projected = s.today
        added = 0
        while added < days_needed:
            projected += timedelta(days=1)
            if projected.weekday() != 6:
                added += 1
        days_late = max(_working_days(s.deadline, projected), 0)
        on_sched = projected <= s.deadline

    feasible = required <= s.max_daily_capacity if wdays > 0 else False

    # Build the verdict + advice.
    if remaining == 0:
        headline = f"{s.style}: complete. All {s.ordered} pieces finished."
        advice = "No action needed."
    elif on_sched:
        headline = (f"{s.style}: ON SCHEDULE. {remaining} left, "
                    f"{wdays} working days — current pace {current:.0f}/day is enough.")
        advice = f"Hold pace at {math.ceil(required)}/day or better."
    elif feasible:
        headline = (f"{s.style}: BEHIND. At {current:.0f}/day it finishes "
                    f"~{days_late} working day(s) late.")
        advice = (f"Raise daily rate to {math.ceil(required)}/day "
                  f"(capacity is {s.max_daily_capacity}/day) to hit {s.deadline}.")
    else:
        # Even at full capacity it can't be done in time.
        max_possible = int(s.max_daily_capacity * wdays)
        headline = (f"{s.style}: AT RISK. Needs {math.ceil(required)}/day but "
                    f"capacity is only {s.max_daily_capacity}/day.")
        advice = (f"Deadline not achievable as-is (max {max_possible} pieces by "
                  f"{s.deadline}). Options: extend deadline, add a line/shift, or "
                  f"split shipment.")

    return ScheduleResult(
        style=s.style, on_schedule=on_sched, remaining=remaining,
        working_days_left=wdays, required_rate=round(required, 1),
        current_rate=round(current, 1), projected_finish=projected,
        days_late=days_late, feasible=feasible, headline=headline, advice=advice,
    )


# ══════════════════════════════════════════════════════════════════════════
# Bottleneck detection — which stage is dragging?
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class BottleneckResult:
    stage: str
    throughput: float            # pieces/day at this stage recently
    upstream_stage: str | None
    upstream_throughput: float | None
    severity: str                # "ok" | "watch" | "critical"
    note: str


def detect_bottleneck(stage_rates: dict[str, float], stage_order: list[str]) -> list[BottleneckResult]:
    """Compare each stage's recent throughput to the FASTEST stage upstream of it.
    A stage processing far fewer pieces/day than what's feeding into the line is
    the constraint. Using the max upstream rate (not just the adjacent stage)
    means a gap is still caught when an intermediate stage has no recent data.
    stage_rates: {stage_code: pieces_per_day}; stage_order: pipeline sequence."""
    out: list[BottleneckResult] = []
    for i, stage in enumerate(stage_order):
        if stage not in stage_rates:
            continue
        rate = stage_rates[stage]
        # fastest stage strictly upstream that actually has data
        upstream = {s: stage_rates[s] for s in stage_order[:i] if s in stage_rates}
        up = max(upstream, key=upstream.get) if upstream else None
        up_rate = upstream.get(up) if up else None
        severity, note = "ok", f"{stage} flowing at {rate:.0f}/day."
        if up_rate and up_rate > 0:
            ratio = rate / up_rate
            if ratio < 0.5:
                severity = "critical"
                note = (f"{stage} ({rate:.0f}/day) is processing under half of "
                        f"{up}'s {up_rate:.0f}/day upstream — this is the bottleneck.")
            elif ratio < 0.75:
                severity = "watch"
                note = (f"{stage} ({rate:.0f}/day) trails {up} ({up_rate:.0f}/day) "
                        f"— watch for a backlog forming.")
        out.append(BottleneckResult(stage, round(rate, 1), up,
                    round(up_rate, 1) if up_rate else None, severity, note))
    return out


# ══════════════════════════════════════════════════════════════════════════
# Multi-style planner — N styles, shared capacity, individual deadlines
# ══════════════════════════════════════════════════════════════════════════
@dataclass
class PlanLine:
    style: str
    daily_target: int
    start: date
    finish: date
    deadline: date
    status: str                  # "planned" | "tight" | "infeasible" | "blocked"
    note: str


def plan_styles(styles: list[StyleInput], shared_capacity: int | None = None,
                blocked: set[str] | None = None) -> list[PlanLine]:
    """Earliest-deadline-first plan. If shared_capacity is set, total daily
    targets are kept under it. `blocked` styles (e.g. leather not arrived) are
    deferred and the rest get the freed capacity — the re-calibration case."""
    blocked = blocked or set()
    active = [s for s in styles if s.style not in blocked]
    active.sort(key=lambda s: s.deadline)        # EDF: most urgent first

    plan: list[PlanLine] = []
    cap_left = shared_capacity

    for s in active:
        r = schedule_status(s)
        target = math.ceil(r.required_rate) if r.required_rate != float("inf") else s.max_daily_capacity
        target = min(target, s.max_daily_capacity)
        status, note = "planned", r.advice
        if not r.feasible:
            status, note = "infeasible", r.advice
        elif not r.on_schedule:
            status = "tight"
        if shared_capacity is not None:
            if cap_left is not None and target > cap_left:
                target = max(cap_left, 0)
                status = "tight"
                note = f"Capacity-limited to {target}/day after higher-priority styles."
            if cap_left is not None:
                cap_left -= target
        plan.append(PlanLine(
            style=s.style, daily_target=target, start=s.today,
            finish=r.projected_finish or s.deadline, deadline=s.deadline,
            status=status, note=note,
        ))

    for st in blocked:
        plan.append(PlanLine(style=st, daily_target=0, start=date.max, finish=date.max,
                    deadline=date.max, status="blocked",
                    note="Material not arrived — deferred; capacity reallocated to other styles."))
    return plan
