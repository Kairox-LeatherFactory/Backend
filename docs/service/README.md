# KairoX — per-service documentation

**Two Word documents for every service**, 17 services, 34 files.

| File | What it is | Who writes it |
|---|---|---|
| `<SERVICE>_SYSTEM_GUIDE.docx` | How the service actually works: the flow, the rules, the states, the traps | **A human**, in `_src/<service>_system_guide.md` |
| `<SERVICE>_API_REFERENCE.docx` | Every endpoint: what it does, who may call it, every field in and out, an example you can paste, and the errors it returns | **Generated** from the running app |

Build both:

```bash
python scripts/make_service_docs.py              # everything
python scripts/make_service_docs.py store wage   # just these services
python scripts/make_service_docs.py --api        # references only
python scripts/make_service_docs.py --guide      # guides only
```

No database needed. It takes a few seconds. **Re-run it after changing routes.**

---

## Why one of them is generated and the other is not

A **request body is a contract**. Typed by hand it becomes a second copy that
drifts the day somebody adds a field — and a wrong contract is worse than none,
because the frontend developer builds against it, gets a 422, and files a bug on
a route that was never wrong. So every path, field, type, default and example in
the reference is read out of the app itself.

A **flow is not in the schema**. Why the store is a state on the garment and not
a place, why a closed wage run is never recomputed, which gate fails first — no
generator can know that. Those are written down in `_src/` and rendered to Word
next to the reference.

The **role gate** sits in between. OpenAPI does not publish it, but the app knows
it: `require_roles(*allowed)` returns a closure, and the allowed tuple is read
back off the live dependency graph. So "who can call it" is true, not guessed.

---

## What lives where

```
docs/service/
├── *_SYSTEM_GUIDE.docx        ← the deliverables
├── *_API_REFERENCE.docx       ←
└── _src/
    └── <service>_system_guide.md   ← edit THIS, then rebuild

scripts/
├── make_service_docs.py       the entry point
└── docgen/
    ├── services.py            THE service registry (shared with Postman)
    ├── introspect.py          reads the truth out of the running app
    ├── apiref.py              builds one API-reference document
    ├── responses.py           hand-written shapes for routes with no response_model
    ├── errors.py              every HTTPException a service can raise
    └── word.py                the .docx writer + a Markdown subset renderer
```

**To change a system guide, edit the Markdown in `_src/` and rebuild.** Never
edit a `.docx` by hand — the next build overwrites it.

---

## The two things to keep honest

**1. A tag with no service is documented nowhere.** `scripts/docgen/services.py`
maps OpenAPI tags to services, and **both** this generator and
`scripts/make_postman_per_service.py` read it. Adding a router with a new tag and
forgetting the registry would silently leave it out of the docs *and* out of
Postman, so `check_coverage()` makes that a loud failure on every run.

**2. About 40% of the routes declare no `response_model`.** They return a plain
dict the service builds, so OpenAPI publishes nothing for them and a reference
that says "no body" would be wrong. Those shapes are read out of the service code
and written down in `scripts/docgen/responses.py`, and the documents label them
as hand-written so nobody mistakes them for machine-checked truth.

Every run prints the routes that have **neither** — that list should stay empty.
The real fix is a `response_model` on the route: every entry that disappears from
`responses.py` because someone added one is a win.

---

## The services

| Service | Covers |
|---|---|
| `USER` | Login, tokens, roles, the logins themselves |
| `CLIENT` | Buyers, orders, styles, SKUs |
| `IMPORT` | Breakdown upload → commit → release (the mint) |
| `BARCODE` | The registry, resolve, printing, the employee card |
| `EMPLOYEE` | The roster. Workers get **no login** |
| `ATTENDANCE` | The gate scan, the manual fallback, corrections |
| `MATERIAL` | Lots, stock, arrivals, suppliers, the per-style recipe |
| `CUTTING` | The grid that replaced the cutting manager's spreadsheet |
| `PRODUCTION` | `POST /production/log` — the whole floor's logging surface |
| `STORE` | The merge, as a state on the garment |
| `INSPECTION` | Reject and rework |
| `JOBWORK` | Garments sent to an outside factory |
| `WAGE` | Rates, runs, the ledger |
| `DASHBOARD` | The per-role manager screens |
| `ANALYTICS` | Read-only reporting and the piece life story |
| `PROCUREMENT` | Phase 2 — intake → BOM → approval → inventory → supplier PO |
| `SYSTEM` | Health, readiness, root, the chatbot |

Deeper Phase-2 material lives beside this folder:
`docs/KAIROX_PROCUREMENT_SYSTEM_GUIDE.md` and
`docs/KAIROX_PROCUREMENT_API_REFERENCE.md`.
