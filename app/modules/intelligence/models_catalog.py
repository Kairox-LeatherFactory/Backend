"""
================================================================================
modules/intelligence/models_catalog.py — Open-source model picks + HF wiring
================================================================================

PURPOSE
    A single place to choose and swap open-source models, so you can test each
    one yourself without touching the rest of the code. Two jobs the chatbot
    can use models for:
      (A) EMBEDDINGS / sentence-similarity  — for RAG over the workflow doc and
          semantic intent matching. (Numbers questions DON'T use this — they use
          the deterministic tools.)
      (B) CHAT model — the LangGraph agent's reasoner/phraser (see
          langgraph_agent.load_chat_model).

ABOUT THE MODEL YOU NAMED — "MiniMaxAI/MiniMax-M2"
    That is a large CHAT/reasoning LLM, NOT a sentence-similarity model. You can
    use it as the agent's CHAT model (job B) via load_chat_model, but it is the
    wrong type for embeddings (job A) and far too big to run as a similarity
    encoder. For embeddings, use one of the EMBEDDING_MODELS below.

EMBEDDING PICKS (verified current as of 2026 research; install
    `pip install langchain-huggingface "sentence-transformers>=5.2.0"`):
      - all-MiniLM-L6-v2     : 384-dim, ~80MB. Fastest; great default for small
                               corpora like a single workflow doc. Apache-2.0.
      - all-mpnet-base-v2    : 768-dim. Higher quality, slower. Apache-2.0.
      - BAAI/bge-m3          : multilingual, strong retrieval, 1024-dim. The
                               open-source production standard in 2026. MIT.
      - intfloat/e5-large-v2 : strong English retrieval (use "query:"/"passage:"
                               prefixes). MIT.
      - nomic-ai/nomic-embed-text-v1.5 : good multilingual, long context. Apache.
    Pick small (MiniLM) for speed at your scale; move to bge-m3 if you add lots
    of multilingual documents. Benchmark on YOUR data — generic scores don't
    always transfer.

CHAT-MODEL PICKS (for the agent; small is enough — it only routes + phrases):
      - ollama:qwen2.5:3b-instruct   : local, free, fast. Recommended default.
      - ollama:llama3.2:3b-instruct  : local, free alternative.
      - anthropic:claude-3-5-haiku   : hosted, cheap, very reliable tool-calling.
      - openai:gpt-4o-mini           : hosted, cheap.
    Use a LOCAL quantized model (Ollama) to avoid per-call cost. The math is in
    the tools, so a small model is plenty.

USAGE
    from app.modules.intelligence.models_catalog import get_embeddings
    emb = get_embeddings("all-MiniLM-L6-v2")      # any key from EMBEDDING_MODELS
    vec = emb.embed_query("is cutting on schedule?")
================================================================================
"""
from __future__ import annotations

# name -> (hf_model_id, dimension, note)
EMBEDDING_MODELS: dict[str, tuple[str, int, str]] = {
    "all-MiniLM-L6-v2":  ("sentence-transformers/all-MiniLM-L6-v2", 384,
                          "Fast, tiny, Apache-2.0. Best default for small corpora."),
    "all-mpnet-base-v2": ("sentence-transformers/all-mpnet-base-v2", 768,
                          "Higher quality, slower. Apache-2.0."),
    "bge-m3":            ("BAAI/bge-m3", 1024,
                          "Multilingual, strong retrieval. 2026 OSS standard. MIT."),
    "e5-large-v2":       ("intfloat/e5-large-v2", 1024,
                          "Strong English retrieval; needs query:/passage: prefixes."),
    "nomic-embed-v1.5":  ("nomic-ai/nomic-embed-text-v1.5", 768,
                          "Multilingual, long context. Apache-2.0."),
}

CHAT_MODELS: dict[str, str] = {
    "qwen2.5-3b":   "ollama:qwen2.5:3b-instruct",
    "llama3.2-3b":  "ollama:llama3.2:3b-instruct",
    "claude-haiku": "anthropic:claude-3-5-haiku-latest",
    "gpt-4o-mini":  "openai:gpt-4o-mini",
    # The model you named — usable as the agent's CHAT model (not for embeddings):
    "minimax-m2":   "huggingface:MiniMaxAI/MiniMax-M2",
}

DEFAULT_EMBEDDING = "all-MiniLM-L6-v2"


def get_embeddings(name: str = DEFAULT_EMBEDDING):
    """Return a LangChain HuggingFaceEmbeddings for the named model.
    Requires: pip install langchain-huggingface "sentence-transformers>=5.2.0".
    The model downloads from Hugging Face on first use and caches locally."""
    if name not in EMBEDDING_MODELS:
        raise ValueError(f"Unknown embedding '{name}'. Options: {list(EMBEDDING_MODELS)}")
    model_id = EMBEDDING_MODELS[name][0]
    from langchain_huggingface import HuggingFaceEmbeddings
    return HuggingFaceEmbeddings(
        model_name=model_id,
        encode_kwargs={"normalize_embeddings": True},  # cosine-ready vectors
    )


def list_models() -> dict:
    """Introspection helper for an admin endpoint / your manual testing."""
    return {
        "embeddings": {k: {"hf_id": v[0], "dim": v[1], "note": v[2]}
                       for k, v in EMBEDDING_MODELS.items()},
        "chat_models": CHAT_MODELS,
        "default_embedding": DEFAULT_EMBEDDING,
    }
