"""
================================================================================
modules/intelligence/service.py — Chatbot service (orchestrates the agent)
================================================================================
Thin async wrapper: hands the question to the agent, returns the structured
answer. Kept separate from the router so the agent can also be called by a
scheduled job (e.g. nightly "production report") without going through HTTP.
================================================================================
"""
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.modules.intelligence.agent import get_agent


class IntelligenceService:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def ask(self, question: str, use_llm: bool = False) -> dict:
        if use_llm:
            # Real LangGraph ReAct agent. Uses a chat model if CHAT_MODEL is
            # configured (e.g. 'ollama:qwen2.5:3b-instruct'); otherwise the agent
            # runs with a fake model and we fall back to the deterministic router
            # so the response is still useful with zero setup.
            from app.modules.intelligence.langgraph_agent import run_langgraph
            spec = getattr(settings, "chat_model", None)
            if spec:
                return await run_langgraph(self.db, question, model_spec=spec)
            # No model configured -> deterministic router gives exact answers now.
        agent = get_agent(use_llm=False)
        return await agent.answer(self.db, question)
