"""
================================================================================
modules/intelligence/tools.py — Agent TOOLS over factory data + forecast engine
================================================================================

PURPOSE
    The chatbot is a TOOL-CALLING AGENT, not a RAG-over-numbers system. Each
    function here is a "tool": it pulls real rows from the DB, runs the
    deterministic forecast engine, and returns a STRUCTURED result. The LLM's
    only jobs are (a) choose which tool + arguments from the user's question and
    (b) phrase the structured result in English. The MATH is never left to the
    LLM — that's what makes answers exact and cheap.

WHY THIS SHAPE (LangChain / LangGraph drop-in)
    Each tool has: a name, a one-line description (what the LLM reads to route),
    and a typed signature. That is exactly LangChain's @tool contract. To go from
    the deterministic router (in agent.py) to a real LangGraph agent you wrap
    these same functions with @tool and bind them to your model — no rewrite.

DATA NOTE — production rate
    "produced" and "recent_daily_rate" come from ProductionEvent rows at the
    FINAL stage (FF). Where a style has no rate history yet we fall back to a
    configurable assumed capacity so the engine still answers ("what rate do you
    NEED") rather than refusing.
================================================================================
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.clients.models import SKU, Client, PurchaseOrder, Style
from app.modules.intelligence.forecast import (
    StyleInput, schedule_status, detect_bottleneck, plan_styles,
)
from app.modules.production.models import Operation, ProductionEvent

# Reasonable defaults where the factory hasn't configured a value yet.
DEFAULT_MAX_DAILY = 20
RATE_LOOKBACK_DAYS = 14
STAGE_ORDER = ["CUTTING", "FUSING", "PASTING", "SHELL", "LA", "LS", "FF"]


async def _final_stage_id(db: AsyncSession) -> int | None:
    return await db.scalar(select(Operation.id).where(Operation.code == "FF"))


async def _style_by_name(db: AsyncSession, name: str) -> Style | None:
    # Case-insensitive contains match so "carnaby" finds "CARNABY".
    res = await db.execute(
        select(Style).where(func.lower(Style.name).like(f"%{name.lower()}%"))
    )
    return res.scalars().first()


async def _produced_and_rate(db: AsyncSession, style_id, ff_id) -> tuple[int, float]:
    """Pieces through final stage, and recent pieces/day for this style."""
    produced = await db.scalar(
        select(func.coalesce(func.sum(ProductionEvent.qty), 0))
        .select_from(ProductionEvent)
        .join(SKU, SKU.id == ProductionEvent.sku_id)
        .where(SKU.style_id == style_id, ProductionEvent.operation_id == ff_id)
    ) or 0

    since = date.today() - timedelta(days=RATE_LOOKBACK_DAYS)
    recent = await db.scalar(
        select(func.coalesce(func.sum(ProductionEvent.qty), 0))
        .select_from(ProductionEvent)
        .join(SKU, SKU.id == ProductionEvent.sku_id)
        .where(SKU.style_id == style_id, ProductionEvent.operation_id == ff_id,
               ProductionEvent.work_date >= since)
    ) or 0
    # average over working days in the window (~6/7 of the days)
    working = max(int(RATE_LOOKBACK_DAYS * 6 / 7), 1)
    return int(produced), round(recent / working, 2)


async def _style_ordered(db: AsyncSession, style_id) -> int:
    return await db.scalar(
        select(func.coalesce(func.sum(SKU.qty_ordered), 0)).where(SKU.style_id == style_id)
    ) or 0


async def _deadline_for_style(db: AsyncSession, style_id) -> date | None:
    return await db.scalar(
        select(PurchaseOrder.delivery_deadline)
        .join(Style, Style.purchase_order_id == PurchaseOrder.id)
        .where(Style.id == style_id)
    )


# ══════════════════════════════════════════════════════════════════════════
# TOOL: schedule status for one style
# ══════════════════════════════════════════════════════════════════════════
async def tool_schedule_status(db: AsyncSession, style_name: str,
                               max_daily: int | None = None) -> dict:
    """Is a given style on schedule? Returns verdict, required rate, projected
    finish, and advice. Use for: 'is X on schedule', 'will X be late', 'what
    daily rate do we need for X'."""
    style = await _style_by_name(db, style_name)
    if not style:
        return {"ok": False, "error": f"No style matching '{style_name}'."}
    ff = await _final_stage_id(db)
    ordered = await _style_ordered(db, style.id)
    produced, rate = await _produced_and_rate(db, style.id, ff)
    deadline = await _deadline_for_style(db, style.id) or (date.today() + timedelta(days=30))

    result = schedule_status(StyleInput(
        style=style.name, ordered=ordered, produced=produced, deadline=deadline,
        max_daily_capacity=max_daily or DEFAULT_MAX_DAILY, recent_daily_rate=rate,
    ))
    out = asdict(result)
    out["ok"] = True
    out["projected_finish"] = result.projected_finish.isoformat() if result.projected_finish else None
    out["deadline"] = deadline.isoformat()
    return out


# ══════════════════════════════════════════════════════════════════════════
# TOOL: bottleneck detection across stages
# ══════════════════════════════════════════════════════════════════════════
async def tool_bottleneck(db: AsyncSession) -> dict:
    """Which department/stage is slowing production? Compares recent throughput
    per stage. Use for: 'where is the bottleneck', 'which dept is slow',
    'why are we behind'."""
    since = date.today() - timedelta(days=RATE_LOOKBACK_DAYS)
    rows = (await db.execute(
        select(Operation.code, func.coalesce(func.sum(ProductionEvent.qty), 0))
        .join(Operation, Operation.id == ProductionEvent.operation_id)
        .where(ProductionEvent.work_date >= since)
        .group_by(Operation.code)
    )).all()
    working = max(int(RATE_LOOKBACK_DAYS * 6 / 7), 1)
    rates = {code: float(total) / working for code, total in rows}
    if not rates:
        return {"ok": True, "results": [], "note": "No recent production logged."}
    results = [asdict(b) for b in detect_bottleneck(rates, STAGE_ORDER)]
    critical = [r for r in results if r["severity"] in ("critical", "watch")]
    return {"ok": True, "results": results, "flagged": critical}


# ══════════════════════════════════════════════════════════════════════════
# TOOL: multi-style production plan (with optional blocked styles)
# ══════════════════════════════════════════════════════════════════════════
async def tool_plan(db: AsyncSession, shared_capacity: int | None = None,
                    blocked_styles: list[str] | None = None) -> dict:
    """Build a daily-target plan across ALL active styles, earliest deadline
    first. Defers 'blocked' styles (e.g. leather not arrived) and reallocates
    capacity. Use for: 'plan production', 're-plan, X leather is late',
    'set daily targets', 'what should we make first'."""
    ff = await _final_stage_id(db)
    styles = (await db.execute(select(Style))).scalars().all()
    inputs: list[StyleInput] = []
    blocked_set = set()
    blocked_lower = {b.lower() for b in (blocked_styles or [])}

    for st in styles:
        ordered = await _style_ordered(db, st.id)
        if ordered == 0:
            continue
        produced, rate = await _produced_and_rate(db, st.id, ff)
        deadline = await _deadline_for_style(db, st.id) or (date.today() + timedelta(days=30))
        if any(b in st.name.lower() for b in blocked_lower):
            blocked_set.add(st.name)
        inputs.append(StyleInput(
            style=st.name, ordered=ordered, produced=produced, deadline=deadline,
            max_daily_capacity=DEFAULT_MAX_DAILY, recent_daily_rate=rate,
        ))

    plan = plan_styles(inputs, shared_capacity=shared_capacity, blocked=blocked_set)
    lines = []
    for p in plan:
        d = asdict(p)
        d["start"] = p.start.isoformat() if p.start != date.max else None
        d["finish"] = p.finish.isoformat() if p.finish != date.max else None
        d["deadline"] = p.deadline.isoformat() if p.deadline != date.max else None
        lines.append(d)
    return {"ok": True, "plan": lines, "blocked": list(blocked_set)}


# ══════════════════════════════════════════════════════════════════════════
# TOOL: factory overview snapshot
# ══════════════════════════════════════════════════════════════════════════
async def tool_overview(db: AsyncSession) -> dict:
    """High-level factory numbers: clients, styles, total ordered vs produced.
    Use for: 'how is the factory doing', 'give me an overview', 'status'."""
    clients = await db.scalar(select(func.count(Client.id))) or 0
    styles = await db.scalar(select(func.count(Style.id))) or 0
    ordered = await db.scalar(select(func.coalesce(func.sum(SKU.qty_ordered), 0))) or 0
    ff = await _final_stage_id(db)
    produced = await db.scalar(
        select(func.coalesce(func.sum(ProductionEvent.qty), 0))
        .where(ProductionEvent.operation_id == ff)
    ) or 0
    pct = round(produced / ordered * 100, 1) if ordered else 0.0
    return {"ok": True, "clients": int(clients), "styles": int(styles),
            "total_ordered": int(ordered), "total_finished": int(produced),
            "pct_complete": pct}
