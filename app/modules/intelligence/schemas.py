"""API contract for the chatbot (intelligence) module."""
from pydantic import BaseModel


class ChatRequest(BaseModel):
    question: str
    use_llm: bool = False        # flip to True once an LLM backend is wired


class ChatResponse(BaseModel):
    answer: str
    tool: str | None = None
    data: dict | None = None
