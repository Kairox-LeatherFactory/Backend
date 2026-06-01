"""Validate the RAG vector pipeline (sentence-transformers + FAISS + langchain).
Builds a real FAISS index over a tiny temp workflow doc and runs a query.
Run:  .venv\\Scripts\\python.exe -m scripts.rag_smoke
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.modules.intelligence.rag as rag

DOC = """The leather factory workflow has seven stages.
Cutting cuts the leather panels to the pattern.
Fusing bonds interlining to the shell panels with heat and pressure.
Pasting glues the folded edges before stitching.
Shell stitching assembles the outer body of the garment.
LA and LS are lining assembly and lining stitching.
FF is the final finishing stage: trimming, pressing, and packing.
A delay in Cutting cascades downstream because every later stage is starved of panels.
"""


def main():
    print("=" * 70)
    print("RAG PIPELINE SMOKE TEST (embeddings + FAISS)")
    print("=" * 70)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "Leather_Factory_Workflow.txt")
        with open(path, "w", encoding="utf-8") as f:
            f.write(DOC)

        rag._VECTORSTORE = None  # reset cache
        print("Building FAISS index (downloads all-MiniLM-L6-v2 on first run)...")
        vs = rag.build_index(doc_path=path, embedding_model="all-MiniLM-L6-v2")
        assert vs is not None, "FAILED: index did not build (deps/model issue)"
        print("  [PASS] FAISS index built from workflow doc")

        # Point answer_from_docs at our temp index (already cached in _VECTORSTORE).
        res = rag.answer_from_docs("what happens during the fusing stage?")
        assert res["ok"], f"FAILED: {res}"
        assert res.get("passages"), "FAILED: no passages retrieved"
        top = res["passages"][0]
        print(f"  [PASS] retrieved {len(res['passages'])} passage(s); "
              f"top score={top['score']}")
        print(f"        top passage: {top['text'][:100]!r}")
        ok = "fus" in top["text"].lower()
        print(f"  [{'PASS' if ok else 'WARN'}] top passage is about fusing: {ok}")

    print("=" * 70)
    print("RAG RESULT: pipeline functional (sentence-transformers + FAISS + langchain)")
    print("=" * 70)


if __name__ == "__main__":
    main()
