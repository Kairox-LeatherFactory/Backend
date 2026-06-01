"""
================================================================================
modules/intelligence/router.py — Chatbot HTTP API (with streaming)
================================================================================
  POST /api/v1/chat       -> full JSON answer (answer + tool + structured data)
  POST /api/v1/chat/stream -> Server-Sent-Events token stream for a typing UI

STREAMING NOTE
    The deterministic backend computes the whole answer at once, so /stream
    chunks the final text for a live-typing effect. When you wire a real LLM,
    swap the chunker for the model's native token stream — the endpoint shape
    and the frontend stay the same.
================================================================================
"""
import asyncio
import json

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.modules.intelligence.schemas import ChatRequest, ChatResponse
from app.modules.intelligence.service import IntelligenceService
from app.modules.users.deps import get_current_user
from app.modules.users.models import User

router = APIRouter(prefix="/chat", tags=["Chatbot"])


@router.post("", response_model=ChatResponse)
async def chat(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await IntelligenceService(db).ask(body.question, use_llm=body.use_llm)
    return ChatResponse(**result)


@router.post("/stream")
async def chat_stream(
    body: ChatRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    result = await IntelligenceService(db).ask(body.question, use_llm=body.use_llm)
    text = result["answer"]

    async def gen():
        # word-by-word SSE for a live-typing effect
        for word in text.split(" "):
            yield f"data: {json.dumps({'delta': word + ' '})}\n\n"
            await asyncio.sleep(0.02)
        yield f"data: {json.dumps({'done': True, 'tool': result.get('tool'), 'data': result.get('data')})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")
