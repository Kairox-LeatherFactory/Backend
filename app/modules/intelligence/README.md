# Intelligence Module — AI Chatbot (LangGraph) + Forecasting Brain

## Two backends, one response shape
- **DeterministicRouter** (default, no model, no cost) — keyword/entity routing
  over the forecast tools. Exact answers right now.
- **LangGraph ReAct agent** (real, verified) — `langgraph_agent.py`. Binds the
  tools to a chat model and reasons about multi-step questions. Enabled by
  setting `CHAT_MODEL` (else it falls back to deterministic, never errors).

Both return `{answer, tool, data}`, so the frontend never changes.

## Why math is in tools, not the LLM
The killer questions are arithmetic over SQL (ordered/produced/deadline/rate).
The tools compute them EXACTLY; the model only routes + phrases. That's cheap
(use a small/local model) AND accurate. RAG is used for ONE thing only — the
free-text workflow doc (`rag.py`, `search_workflow_docs` tool).

## Verified version set (installs & runs; tested 2026)
    langgraph>=1.2  langchain>=1.2  langchain-core>=1.4  langsmith>=0.8
    langchain-huggingface>=1.2  sentence-transformers>=5.2  faiss-cpu  python-docx

## Turn on a real model (one env var)
    CHAT_MODEL=ollama:qwen2.5:3b-instruct     # local, free (recommended)
    CHAT_MODEL=anthropic:claude-3-5-haiku-latest
    CHAT_MODEL=openai:gpt-4o-mini
Then POST /api/v1/chat with {"question": "...", "use_llm": true}.
LangSmith tracing: set LANGCHAIN_TRACING_V2=true and LANGCHAIN_API_KEY.

## Models you can swap & test yourself  (models_catalog.py)
Embeddings: all-MiniLM-L6-v2 (default, fast), all-mpnet-base-v2, bge-m3
(multilingual, 2026 OSS standard), e5-large-v2, nomic-embed-v1.5.
Chat: qwen2.5-3b / llama3.2-3b (local), claude-haiku, gpt-4o-mini, minimax-m2.

NOTE on "MiniMaxAI/MiniMax-M2": it is a CHAT/reasoning LLM (use as CHAT_MODEL),
NOT a sentence-similarity model — for embeddings use one of the EMBEDDING_MODELS.

## RAG design (rag.py): vector search + post-filter
Workflow doc -> chunk -> embed (HF model) -> FAISS ANN index -> top-k cosine ->
post-filter on a score threshold (drops weak matches). In-process FAISS is right
at this scale; move to a managed vector DB when the corpus grows.

## Files
forecast.py (pure math) · tools.py (DB+math tools) · agent.py (routers) ·
langgraph_agent.py (real ReAct agent) · models_catalog.py (model picks) ·
rag.py (doc RAG) · service.py · router.py (/chat, /chat/stream).
