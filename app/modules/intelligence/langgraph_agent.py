"""
================================================================================
modules/intelligence/langgraph_agent.py — REAL LangGraph ReAct agent (v1 API)
================================================================================

PURPOSE
    The production agentic brain, built on LangGraph 1.x's `create_react_agent`
    (the stable v1.0 ReAct prebuilt). It binds the factory tools (the SAME
    deterministic math from tools.py, wrapped as LangChain @tool) to a chat
    model, and lets the model REASON about which tool(s) to call and how to
    phrase the result. Multi-step questions ("is Carnaby on track AND where's
    the bottleneck?") are handled by the ReAct loop calling several tools.

    Verified import-compatible set (Feb–May 2026):
        langgraph>=1.2, langgraph-prebuilt>=1.1, langchain-core>=1.4
        langchain-huggingface>=1.2, sentence-transformers>=5.2  (for embeddings)

WHY THE MATH STAYS IN TOOLS
    The model never computes numbers — it only decides WHICH tool and reads back
    the structured result. That is what keeps answers exact AND lets you use a
    small/cheap model. (See agent.py for the deterministic fallback that needs
    no model at all.)

MODEL PLUGGING (one line)
    Pass any LangChain chat model to build_langgraph_agent(model=...):
      - Local & free:   ChatOllama(model="qwen2.5:3b-instruct")   # via Ollama
      - Hosted:         init_chat_model("anthropic:claude-...")   # or openai:...
    If you pass model=None we use a FAKE deterministic chat model so the whole
    graph still runs in tests/CI with no key — proving the wiring end to end.

LANGSMITH TRACING
    Set env LANGCHAIN_TRACING_V2=true and LANGCHAIN_API_KEY=... — no code change;
    create_react_agent emits traces automatically.

ASYNC + DB
    Tools need a DB session. LangChain tool functions are sync-signature, so we
    inject the AsyncSession via a contextvar set per request (set_db_session),
    and the tool bodies run their async DB work on the running loop. This keeps
    the tool schema clean (the model sees only business args, never `db`).
================================================================================
"""
from __future__ import annotations

import contextvars
import json

from langchain_core.tools import tool

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.intelligence import tools as factory_tools

# Per-request DB session, injected so the model never sees `db` as an argument.
_db_ctx: contextvars.ContextVar[AsyncSession] = contextvars.ContextVar("db_session")


def set_db_session(db: AsyncSession) -> None:
    _db_ctx.set(db)


# ── Tools the model can call (native-async LangChain wrappers) ───────────────
# These are `async def`, so LangChain awaits them directly via .ainvoke — no
# event-loop bridging. The model sees only business args (never `db`).
@tool
async def schedule_status(style_name: str, max_daily: int | None = None) -> str:
    """Check if a production STYLE is on schedule and what daily rate is needed.
    Use for questions like 'is CARNABY on schedule', 'will X be late', or
    'what rate do we need for X'. style_name is the garment style."""
    return json.dumps(await factory_tools.tool_schedule_status(_db_ctx.get(), style_name, max_daily))


@tool
async def bottleneck() -> str:
    """Find which production department/stage is the bottleneck (slowest vs the
    stage feeding it). Use for 'where is the bottleneck', 'which dept is slow'."""
    return json.dumps(await factory_tools.tool_bottleneck(_db_ctx.get()))


@tool
async def plan_production(shared_capacity: int | None = None,
                          blocked_styles: list[str] | None = None) -> str:
    """Build a daily-target production plan across all styles, earliest deadline
    first. Pass blocked_styles (e.g. ['RICANO']) when a style's leather is late
    to defer it and reallocate capacity. Use for 'plan production', 're-plan'."""
    return json.dumps(await factory_tools.tool_plan(_db_ctx.get(), shared_capacity, blocked_styles))


@tool
async def factory_overview() -> str:
    """High-level factory snapshot: clients, styles, pieces ordered vs finished.
    Use for 'how is the factory doing', 'give me an overview', 'status'."""
    return json.dumps(await factory_tools.tool_overview(_db_ctx.get()))


@tool
async def search_workflow_docs(question: str) -> str:
    """Answer FREE-TEXT process/policy questions from the factory workflow
    document (e.g. 'what happens during fusing?', 'why does a cutting delay
    cascade?'). Uses RAG (vector search) over the workflow doc. Do NOT use for
    numeric schedule/rate questions — those have dedicated tools."""
    from app.modules.intelligence.rag import answer_from_docs
    return json.dumps(answer_from_docs(question))


FACTORY_TOOLS = [schedule_status, bottleneck, plan_production, factory_overview, search_workflow_docs]

SYSTEM_PROMPT = (
    "You are the Kairox factory operations assistant. Answer questions about "
    "leather-garment production using ONLY the provided tools — never invent "
    "numbers. Call the right tool, then explain its result in one clear, concise "
    "answer for a factory manager. If a style's material is late, use "
    "plan_production with blocked_styles to re-plan."
)


def _make_react_agent(model, tools, prompt):
    """Build a ReAct agent using the current LangChain v1 API, falling back to
    the LangGraph prebuilt on older installs. Both produce an equivalent graph
    (agent -> tools -> agent loop)."""
    try:
        # LangChain v1.0+ canonical location.
        from langchain.agents import create_agent
        return create_agent(model, tools=tools, system_prompt=prompt)
    except Exception:
        from langgraph.prebuilt import create_react_agent
        return create_react_agent(model, tools=tools, prompt=prompt)


def load_chat_model(spec: str | None = None):
    """Resolve a chat model from an env/string spec, or return None to use the
    deterministic fallback. Examples:
        'ollama:qwen2.5:3b-instruct'   -> local, free (needs Ollama running)
        'anthropic:claude-3-5-haiku'   -> hosted (needs ANTHROPIC_API_KEY)
        'openai:gpt-4o-mini'           -> hosted (needs OPENAI_API_KEY)
    Uses langchain.chat_models.init_chat_model so any provider works uniformly."""
    if not spec:
        return None
    try:
        if spec.startswith("ollama:"):
            from langchain_ollama import ChatOllama
            return ChatOllama(model=spec.split("ollama:", 1)[1], temperature=0)
        from langchain.chat_models import init_chat_model
        return init_chat_model(spec, temperature=0)
    except Exception as e:  # missing provider package / key -> caller falls back
        import logging
        logging.getLogger(__name__).warning("Chat model '%s' unavailable: %s", spec, e)
        return None


def build_langgraph_agent(model=None):
    """Create the ReAct agent. `model` is any LangChain chat model; if None, a
    deterministic fake model is used so the graph runs without an API key."""
    if model is None:
        model = _fake_model()
    return _make_react_agent(model, FACTORY_TOOLS, SYSTEM_PROMPT)


def _fake_model():
    """A deterministic stand-in chat model for tests/CI (no API key). It is NOT
    a real LLM — it just lets the graph wiring run. Swap for a real model in
    production via build_langgraph_agent(model=...)."""
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    # Emits a single assistant message (no tool call) — enough to prove the
    # graph executes. Real routing/tooling happens with a real model.
    return GenericFakeChatModel(messages=iter([
        AIMessage(content="(fake model) Wire a real chat model to enable tool routing.")
    ]))


async def run_langgraph(db: AsyncSession, question: str, model_spec: str | None = None) -> dict:
    """Service entry point: set the request DB, build the agent (real model if
    model_spec resolves, else fake), run the ReAct loop, return the answer dict
    in the SAME shape the deterministic router returns."""
    from langchain_core.messages import HumanMessage
    set_db_session(db)
    model = load_chat_model(model_spec)
    used_real = model is not None
    agent = build_langgraph_agent(model=model)
    result = await agent.ainvoke({"messages": [HumanMessage(content=question)]})
    final = result["messages"][-1].content
    tools_used = [m.name for m in result["messages"]
                  if m.__class__.__name__ == "ToolMessage"]
    return {"answer": final, "tool": tools_used[-1] if tools_used else None,
            "data": {"tools_called": tools_used, "model": model_spec if used_real else "fake"}}
