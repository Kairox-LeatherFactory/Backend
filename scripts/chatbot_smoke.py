"""
================================================================================
scripts/chatbot_smoke.py — End-to-end smoke test of the Intelligence chatbot
================================================================================
Seeds a small but realistic factory dataset into an in-memory SQLite DB, then
exercises every chatbot layer and prints a pass/fail report:

  1. tools.py            — each factory tool returns structured numbers
  2. agent.py            — DeterministicRouter routes questions -> tools (no key)
  3. models_catalog.py   — model registry introspection
  4. rag.py              — answer_from_docs is non-fatal when no doc is present
  5. langgraph_agent.py  — REAL LangGraph ReAct agent driven by the Groq model
                           (CHAT_MODEL + GROQ_API_KEY from .env)

Run:  .venv\\Scripts\\python.exe -m scripts.chatbot_smoke
================================================================================
"""
import asyncio
import os
import sys
from datetime import date, timedelta

# Make `import app...` work when run as a module or a file.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Use an isolated in-memory DB for this smoke test (set before importing app).
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import app.core.database as dbmod
from app.core.database import Base
from app.core.config import settings
from app.core.enums import WageType

# Import models so metadata is complete.
from app.modules.users import models as _u   # noqa
from app.modules.clients import models as cm
from app.modules.employees import models as em
from app.modules.production import models as pm
from app.modules.wages import models as _w   # noqa

from app.modules.intelligence import tools
from app.modules.intelligence.agent import DeterministicRouter
from app.modules.intelligence.models_catalog import list_models
from app.modules.intelligence.rag import answer_from_docs


TODAY = date.today()
PASS, FAIL = "\033[92mPASS\033[0m", "\033[91mFAIL\033[0m"
_results = []


def check(name: str, ok: bool, detail: str = ""):
    _results.append(ok)
    mark = PASS if ok else FAIL
    print(f"  [{mark}] {name}" + (f"  — {detail}" if detail else ""))


async def seed(db):
    """A single client/PO/style 'CARNABY', ordered 200, behind schedule, with a
    clear CUTTING->FF bottleneck in the last 14 days."""
    client = cm.Client(name="Carnaby Co", country="UK"); db.add(client); await db.flush()
    po = cm.PurchaseOrder(
        client_id=client.id, po_number="PO-CARNABY-01",
        order_date=TODAY - timedelta(days=40),
        delivery_deadline=TODAY + timedelta(days=7),   # tight -> behind schedule
    ); db.add(po); await db.flush()
    style = cm.Style(purchase_order_id=po.id, name="CARNABY"); db.add(style); await db.flush()
    sku = cm.SKU(style_id=style.id, color_code="57", color_name="PINE GREEN",
                 size="M", qty_ordered=200); db.add(sku); await db.flush()

    ops = {}
    for i, code in enumerate(["CUTTING", "FUSING", "PASTING", "SHELL", "LA", "LS", "FF"], start=1):
        op = pm.Operation(code=code, label=code.title(), sequence=i)
        db.add(op); ops[code] = op
    await db.flush()

    worker = em.Employee(name="MD Afzal", designation="MULTI", wage_type=WageType.PIECE_RATE)
    db.add(worker); await db.flush()

    # Production over the last 12 days: CUTTING fast (~16/day), FF slow (~8/day).
    # => FF is the bottleneck and the style is behind (only ~96 of 200 finished).
    for d in range(1, 13):
        wd = TODAY - timedelta(days=d)
        db.add(pm.ProductionEvent(sku_id=sku.id, operation_id=ops["CUTTING"].id,
                                  employee_id=worker.id, work_date=wd, qty=16))
        db.add(pm.ProductionEvent(sku_id=sku.id, operation_id=ops["FF"].id,
                                  employee_id=worker.id, work_date=wd, qty=8))
    await db.commit()
    return style


async def main():
    print("=" * 78)
    print("CHATBOT SMOKE TEST")
    print("=" * 78)

    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    dbmod.async_engine = engine
    dbmod.AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with dbmod.AsyncSessionLocal() as db:
        await seed(db)

        # ── 1. tools.py ────────────────────────────────────────────────────
        print("\n[1] tools.py — structured factory tools")
        ov = await tools.tool_overview(db)
        check("tool_overview", ov["ok"] and ov["styles"] == 1 and ov["total_ordered"] == 200,
              f"{ov['total_finished']}/{ov['total_ordered']} finished ({ov['pct_complete']}%)")
        ss = await tools.tool_schedule_status(db, "carnaby")
        check("tool_schedule_status", ss["ok"], ss.get("headline", ""))
        bn = await tools.tool_bottleneck(db)
        check("tool_bottleneck", bn["ok"], f"flagged={[f['stage'] for f in bn.get('flagged', [])]}")
        pl = await tools.tool_plan(db)
        check("tool_plan", pl["ok"] and len(pl["plan"]) >= 1, f"{len(pl['plan'])} style(s) planned")

        # ── 2. agent.py (deterministic router, no key) ─────────────────────
        print("\n[2] agent.py — DeterministicRouter (no model)")
        det = DeterministicRouter()
        for q in ["Is CARNABY on schedule?",
                  "Where is the bottleneck?",
                  "Give me a factory overview",
                  "Plan production, CARNABY leather is late."]:
            r = await det.answer(db, q)
            check(f'Q: "{q}"', bool(r.get("answer")), r["answer"][:90])

        # ── 3. models_catalog.py ───────────────────────────────────────────
        print("\n[3] models_catalog.py — registry")
        cat = list_models()
        check("list_models", "embeddings" in cat and "chat_models" in cat,
              f"{len(cat['embeddings'])} embeddings, {len(cat['chat_models'])} chat models")

        # ── 4. rag.py (non-fatal without a doc) ─────────────────────────────
        print("\n[4] rag.py — answer_from_docs (graceful)")
        rag = answer_from_docs("what happens during the fusing stage?")
        check("answer_from_docs returns a dict (non-fatal)", isinstance(rag, dict),
              rag.get("note", "ok"))

        # ── 5. langgraph_agent.py with the REAL Groq model ─────────────────
        print(f"\n[5] langgraph_agent.py — REAL Groq model: CHAT_MODEL={settings.chat_model!r}")
        spec = settings.chat_model or None
        has_key = bool(os.environ.get("GROQ_API_KEY"))
        check("GROQ_API_KEY present in environment", has_key)
        if not (spec and has_key):
            check("Groq agent run", False, "CHAT_MODEL / GROQ_API_KEY not set — skipping live call")
        else:
            from app.modules.intelligence.langgraph_agent import run_langgraph, load_chat_model
            model = load_chat_model(spec)
            check("load_chat_model resolved a real model", model is not None,
                  type(model).__name__ if model else "None (fell back to fake)")
            for q in ["Is CARNABY on schedule?", "Where is the production bottleneck?"]:
                try:
                    r = await run_langgraph(db, q, model_spec=spec)
                    used_real = r.get("data", {}).get("model") != "fake"
                    tool_called = r.get("tool")
                    ok = bool(r.get("answer")) and used_real
                    check(f'Groq Q: "{q}"', ok,
                          f"tool={tool_called} | model={r['data'].get('model')} | "
                          f"answer={r['answer'][:120]}")
                except Exception as e:
                    check(f'Groq Q: "{q}"', False, f"{type(e).__name__}: {e}")

    await engine.dispose()

    print("\n" + "=" * 78)
    passed, total = sum(_results), len(_results)
    print(f"RESULT: {passed}/{total} checks passed")
    print("=" * 78)
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
