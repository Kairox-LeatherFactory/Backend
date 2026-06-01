"""
================================================================================
modules/intelligence/agent.py — The chatbot agent (LLM-pluggable, runs today)
================================================================================

PURPOSE
    Turns a natural-language question into: (1) a tool choice + arguments,
    (2) a structured tool result (exact math), (3) an English answer. The agent
    is deliberately model-agnostic so you can run it for FREE right now and swap
    in a real LLM / LangGraph later by replacing ONE class.

TWO BACKENDS
    DeterministicRouter (DEFAULT, no API key, fully tested):
        Keyword + entity routing. Picks the right tool, fills arguments from the
        question (style name, blocked style, capacity), runs it, and renders a
        clear answer from the structured result. This alone already answers your
        example questions correctly.

    LLMRouter (STUB, where you plug your model):
        Same interface. Inside, you bind the tools (tools.py) to your model with
        function-calling and let it choose. Recommended path:
          - LangGraph ReAct agent with these tools bound
          - a SMALL model is enough (routing + phrasing, not reasoning about
            numbers) — e.g. an instruct model via your provider, or a local
            quantized model (llama.cpp / Ollama) to avoid per-call cost
          - LangSmith for tracing by setting LANGCHAIN_TRACING_V2=true
        The math still comes from the tools, so the model never computes — it
        only routes and phrases. That keeps it cheap AND accurate.

WHY NOT RAG HERE
    These questions are arithmetic over structured rows; the tools answer them
    exactly. RAG belongs ONLY to free-text sources (your workflow .docx) — see
    the `note` in answer() for where a doc-RAG tool would slot in as one more
    tool, not as the core mechanism.
================================================================================
"""
from __future__ import annotations

import re
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.intelligence import tools


class LLMBackend(Protocol):
    async def answer(self, db: AsyncSession, question: str) -> dict: ...


# ──────────────────────────────────────────────────────────────────────────
# Deterministic router (default; no model, no API cost)
# ──────────────────────────────────────────────────────────────────────────
class DeterministicRouter:
    """Keyword/entity routing over the tools. Good enough to ship; gives exact
    answers because all numbers come from the forecast tools."""

    async def _known_style_names(self, db: AsyncSession) -> list[str]:
        from sqlalchemy import select
        from app.modules.clients.models import Style
        rows = (await db.execute(select(Style.name))).scalars().all()
        return sorted({r for r in rows}, key=len, reverse=True)

    def _find_style(self, q: str, names: list[str]) -> str | None:
        ql = q.lower()
        for n in names:                       # longest first => best match
            if n.lower() in ql:
                return n
        return None

    def _find_capacity(self, q: str) -> int | None:
        m = re.search(r"(\d+)\s*(?:/\s*day|per day|a day|jackets? a day)", q.lower())
        return int(m.group(1)) if m else None

    async def answer(self, db: AsyncSession, question: str) -> dict:
        q = question.lower().strip()
        names = await self._known_style_names(db)
        style = self._find_style(question, names)
        cap = self._find_capacity(question)

        # ── intent routing ──────────────────────────────────────────────
        # bottleneck / slow department
        if any(w in q for w in ["bottleneck", "slow", "slowing", "behind which",
                                "which dept", "which department", "lagging"]):
            res = await tools.tool_bottleneck(db)
            return self._render_bottleneck(res)

        # planning / re-plan / targets
        if any(w in q for w in ["plan", "target", "schedule all", "re-plan",
                                "replan", "recalibrate", "what should we make",
                                "start first", "leather"]):
            blocked = []
            # "X leather is late / hasn't arrived" => block X
            if any(w in q for w in ["late", "not arrived", "hasn't arrived",
                                    "hasnt arrived", "delayed", "missing"]) and style:
                blocked = [style]
            res = await tools.tool_plan(db, shared_capacity=cap, blocked_styles=blocked)
            return self._render_plan(res, blocked)

        # overview / how is the factory
        if any(w in q for w in ["overview", "how is the factory", "factory doing",
                                "status of the factory", "summary", "snapshot"]):
            res = await tools.tool_overview(db)
            return self._render_overview(res)

        # schedule status (default if a style is named or schedule words appear)
        if style or any(w in q for w in ["on schedule", "on track", "late",
                                         "delay", "deadline", "finish in time",
                                         "rate", "behind"]):
            if not style:
                return {"answer": "Which style do you mean? Try e.g. "
                        f"'is {names[0] if names else 'CARNABY'} on schedule?'",
                        "tool": None, "data": {"styles": names[:8]}}
            res = await tools.tool_schedule_status(db, style, max_daily=cap)
            return self._render_schedule(res)

        # fallback
        return {
            "answer": ("I can answer questions about production schedules, "
                       "bottlenecks, and planning. Try: 'Is CARNABY on schedule?', "
                       "'Where is the bottleneck?', or 'Plan production, RICANO "
                       "leather is late.'"),
            "tool": None, "data": {"styles": names[:8]},
        }

    # ── renderers: structured result -> plain English ────────────────────
    def _render_schedule(self, r: dict) -> dict:
        if not r.get("ok"):
            return {"answer": r.get("error", "Style not found."), "tool": "schedule_status", "data": r}
        return {"answer": f"{r['headline']} {r['advice']}", "tool": "schedule_status", "data": r}

    def _render_bottleneck(self, r: dict) -> dict:
        flagged = r.get("flagged", [])
        if not flagged:
            return {"answer": "No bottleneck right now — every stage is keeping pace with the one before it.",
                    "tool": "bottleneck", "data": r}
        worst = sorted(flagged, key=lambda x: 0 if x["severity"] == "critical" else 1)[0]
        return {"answer": worst["note"], "tool": "bottleneck", "data": r}

    def _render_plan(self, r: dict, blocked: list[str]) -> dict:
        lines = r.get("plan", [])
        active = [l for l in lines if l["status"] != "blocked"]
        infeasible = [l for l in active if l["status"] == "infeasible"]
        # sort by deadline, show the most urgent handful
        dated = [l for l in active if l.get("deadline")]
        dated.sort(key=lambda l: l["deadline"])
        top = dated[:5]

        parts = []
        if top:
            parts.append("Next up (earliest deadlines): " +
                         "; ".join(f"{l['style']} → {l['daily_target']}/day by {l['deadline']}"
                                   for l in top))
        if infeasible:
            names = ", ".join(l["style"] for l in infeasible[:6])
            more = f" (+{len(infeasible)-6} more)" if len(infeasible) > 6 else ""
            parts.append(f"At risk — cannot hit deadline at current capacity: {names}{more}")
        if blocked:
            parts.append(f"Deferred (material late): {', '.join(blocked)} — capacity reallocated to the rest")
        parts.append(f"{len(active)} active styles planned, earliest-deadline-first.")
        return {"answer": " | ".join(parts), "tool": "plan", "data": r}

    def _render_overview(self, r: dict) -> dict:
        return {"answer": (f"{r['clients']} clients, {r['styles']} styles. "
                           f"{r['total_finished']} of {r['total_ordered']} pieces finished "
                           f"({r['pct_complete']}%)."),
                "tool": "overview", "data": r}


# ──────────────────────────────────────────────────────────────────────────
# LLM router (STUB — drop your model / LangGraph here)
# ──────────────────────────────────────────────────────────────────────────
class LLMRouter:
    """Real LangGraph ReAct backend. Delegates to langgraph_agent.run_langgraph,
    which binds the tools to a chat model (set via settings.chat_model, e.g.
    'ollama:qwen2.5:3b-instruct'). If no model is configured it falls back to
    the deterministic router so answers are still exact with zero setup."""
    def __init__(self):
        self._fallback = DeterministicRouter()

    async def answer(self, db: AsyncSession, question: str) -> dict:
        from app.core.config import settings
        spec = getattr(settings, "chat_model", None)
        if not spec:
            return await self._fallback.answer(db, question)
        try:
            from app.modules.intelligence.langgraph_agent import run_langgraph
            return await run_langgraph(db, question, model_spec=spec)
        except Exception:
            # Any model/runtime issue -> exact deterministic answer, never a 500.
            return await self._fallback.answer(db, question)


def get_agent(use_llm: bool = False) -> LLMBackend:
    return LLMRouter() if use_llm else DeterministicRouter()
