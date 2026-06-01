"""
intelligence — the AI chatbot + forecasting brain.

    forecast.py  pure deterministic engine (schedule, bottleneck, planning) — no model
    tools.py     agent tools: pull DB rows + run forecast -> structured results
    agent.py     LLM-pluggable router (deterministic default; LangGraph drop-in)
    service.py   orchestration (also callable from scheduled report jobs)
    router.py    /chat and /chat/stream endpoints

Design: math is deterministic (exact, cheap); an LLM only routes + phrases.
RAG belongs only to free-text sources (workflow doc), added as one more tool.
"""
