"""
================================================================================
modules/intelligence/rag.py — Document RAG over the workflow doc (the ONE place
RAG belongs)
================================================================================

PURPOSE
    Your numeric questions are answered by the deterministic tools — NOT RAG.
    RAG is correct for ONE thing: free-text policy/process questions against the
    workflow document (Leather_Factory_Workflow.docx), e.g. "what happens at the
    fusing stage?" or "why does a cutting delay cascade?".

DESIGN (vector search + post-filter, as you asked)
    1. Split the doc into chunks.
    2. Embed chunks with an open-source model (models_catalog.get_embeddings).
    3. Index in FAISS (in-process ANN vector store — no server needed at this
       scale; swap for a managed vector DB only when the corpus is large).
    4. Retrieve top-k by cosine similarity, then POST-FILTER on a score
       threshold (non-indexed filter) so weak matches are dropped instead of
       padding the answer with irrelevant text.
    5. Expose answer_from_docs() as a tool the agent can call alongside the math
       tools — RAG is ONE tool, not the core mechanism.

LAZY + CACHED
    The index builds on first query and is cached in-process, so embeddings load
    once. Requires:
        pip install langchain-huggingface "sentence-transformers>=5.2.0" \
                    langchain-community faiss-cpu
    If those aren't installed, answer_from_docs returns a clear, non-fatal note
    so the rest of the chatbot keeps working.
================================================================================
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_VECTORSTORE = None          # cached FAISS index
_DEFAULT_DOC = "/mnt/project/Leather_Factory_Workflow.docx"
_SCORE_THRESHOLD = 0.35      # post-filter: drop matches weaker than this (cosine)


def _load_doc_text(path: str) -> str:
    """Extract plain text from .docx (python-docx) or .txt/.md."""
    p = Path(path)
    if not p.exists():
        return ""
    if p.suffix.lower() == ".docx":
        try:
            from docx import Document
            return "\n".join(par.text for par in Document(str(p)).paragraphs if par.text.strip())
        except Exception as e:
            logger.warning("docx read failed: %s", e)
            return ""
    return p.read_text(encoding="utf-8", errors="ignore")


def build_index(doc_path: str = _DEFAULT_DOC, embedding_model: str = "all-MiniLM-L6-v2"):
    """Build (and cache) the FAISS index from the workflow doc. Returns the
    vector store, or None if dependencies/text are unavailable."""
    global _VECTORSTORE
    if _VECTORSTORE is not None:
        return _VECTORSTORE
    text = _load_doc_text(doc_path)
    if not text:
        logger.warning("No document text at %s — RAG disabled.", doc_path)
        return None
    try:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        from langchain_community.vectorstores import FAISS
        from app.modules.intelligence.models_catalog import get_embeddings

        chunks = RecursiveCharacterTextSplitter(
            chunk_size=600, chunk_overlap=80
        ).split_text(text)
        emb = get_embeddings(embedding_model)
        _VECTORSTORE = FAISS.from_texts(chunks, emb)
        logger.info("RAG index built: %d chunks via %s", len(chunks), embedding_model)
        return _VECTORSTORE
    except Exception as e:
        logger.warning("RAG index build failed (deps missing?): %s", e)
        return None


def answer_from_docs(question: str, k: int = 4) -> dict:
    """Retrieve relevant workflow-doc passages for a free-text question.
    Vector (ANN) search + score post-filter. Returns matched snippets; the agent
    then phrases them. Non-fatal if RAG deps aren't installed."""
    vs = build_index()
    if vs is None:
        return {"ok": False,
                "note": "Document RAG is not available (install langchain-community, "
                        "faiss-cpu, sentence-transformers). Numeric questions still work."}
    # similarity_search_with_score returns (doc, distance); lower = closer for
    # FAISS L2 on normalized vectors. Convert to a similarity and post-filter.
    hits = vs.similarity_search_with_score(question, k=k)
    passages = []
    for doc, dist in hits:
        sim = 1.0 / (1.0 + float(dist))     # monotonic map distance->(0,1]
        if sim >= _SCORE_THRESHOLD:          # POST-FILTER (non-indexed)
            passages.append({"text": doc.page_content, "score": round(sim, 3)})
    if not passages:
        return {"ok": True, "passages": [],
                "note": "No sufficiently relevant passage found in the workflow doc."}
    return {"ok": True, "passages": passages}
