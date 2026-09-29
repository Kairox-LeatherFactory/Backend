# `scripts/` — what each one is, and which ones write

Run everything from the repo root, as a module: `python -m scripts.<name>`.
Most take `--help`.

---

## Read this before running anything that writes

**`DATABASE_URL` does not redirect the whole process.** `.env` sets
`ASYNC_DATABASE_URL`, and `app/core/config.py::effective_async_url` returns it
*unconditionally* — the sync→async derivation beneath that line never runs while
it is set. So pointing `DATABASE_URL` at a scratch database redirects the **sync**
engine only, and every async code path still goes wherever `ASYNC_DATABASE_URL`
points.

On **2026-09-28** that put 42 employees, 42 employee cards and 8 staff logins into
a live Supabase database during what was meant to be a local test of `seed.py` —
because `seed.py` is sync but its step 6 delegates to the async
`seed_employees.py`. The run printed `SEED COMPLETE`, and its summary said "41
already present" about a table the operator could see was empty, because it was
reading a different database than the one they were looking at.

Every writing script now refuses to start when the two URLs name different
databases (`scripts/_dbguard.py`). **To point one at a scratch database, set
both:**

```bash
DATABASE_URL=sqlite+pysqlite:///./scratch.db \
ASYNC_DATABASE_URL=sqlite+aiosqlite:///./scratch.db \
  python -m scripts.seed --create-all
```

Or unset `ASYNC_DATABASE_URL` and let it be derived. A differing **driver** on the
same host/port/database (`psycopg2` vs `asyncpg`) is normal and does not trip the
guard. `KAIROX_ALLOW_SPLIT_DB=1`, or `--allow-split-db` where the script has an
argument parser, overrides it after printing both URLs.

---

## Generators — safe, no database, re-run after any route change

| Script | Writes | Notes |
|---|---|---|
| `make_postman_per_service.py` | `postman/*.json` + the environment | One collection per service. |
| `make_postman_collection.py` | `postman_collection.json`, `openapi.json` | The combined collection. |
| `make_service_docs.py` | `docs/service/*.docx` (all 34) | Rewrites all 34 every run; a `.docx` is a zip carrying timestamps, so `git status` shows all 34 modified. Revert the ones you did not touch. |
| `export_openapi.py` | `openapi.json` | Just the contract, for `prism mock`. Same bytes as the postman generator writes. |
| `build_testcase_collections.js` | `docs/testcases/*.md`, 5 root collections | `node scripts/build_testcase_collections.js`. Reads `docs/testcases/_cases/*.cases.json`. |
| `md_to_pdf.py` | a PDF beside the `.md` | Headless Chrome, to match the existing PDFs' page breaks. |

The service registry for the first three is **`scripts/docgen/services.py`** — one
list, read by both the Postman and the docs generators. A tag missing from it
would get no collection and no document, silently; `check_coverage()` makes that a
loud failure instead. `docgen/responses.py` holds hand-written response shapes for
the ~40% of routes that declare no `response_model`; every run prints routes that
have neither, and that list should stay empty.

## Read-only inspection

| Script | Notes |
|---|---|
| `db_inspect.py` | Tables and row counts against the configured DB. Writes nothing. |
| `list_logins.py` | `app_user` phones and roles. No password hashes. |
| `importer_smoke.py` | Parses every workbook in `data/` and prints the extraction log. No DB. |

## Smoke tests — no real database needed

| Script | Notes |
|---|---|
| `smoke_test.py` | In-memory SQLite + the live ASGI app: health, login, auth, the DM dashboard, and the two barcode-resolve status codes. **The one to run after any change.** |
| `chatbot_smoke.py` | Every intelligence layer. The LangGraph rung needs `GROQ_API_KEY`. |
| `rag_smoke.py` | Builds a real FAISS index over a temp doc. Needs sentence-transformers. |
| `demo_gemini_bom.py` | Stage 1→2→3 end to end against in-memory SQLite. Needs a live Gemini key. |

## ⚠️ These WRITE to the configured database

All of them are idempotent, and all of them now run the split-database guard
first.

| Script | What it writes |
|---|---|
| `seed.py` | The whole Phase-1 demo dataset from the `data/` spreadsheets: clients, orders, styles, SKUs, the material spec, rates, and (via the two below) people and stock. `--create-all` also does `CREATE TABLE`. |
| `seed_employees.py` | The real 42-person payroll roster + an employee card each + staff logins. The single roster of record. |
| `seed_materials.py` | Suppliers, one lot per material spec across all three floor categories, per-hide sheets, and a lot barcode each. `--dry-run` validates and reports. |
| `load_leather_lots.py` | `data/clean_leather_lots.csv` → suppliers + leather lots + barcodes. |
| `load_trims_inventory.py` | The four trim workbooks → lots + barcodes. A sibling of the above, not a copy. |
| `ensure_roles.py` | **Audit-only by default.** `--apply` creates logins for roles that have none; `--promote` moves one real person to a role. Covers all ten roles that gate a route. |
| `backfill_needs_lining.py` | Recomputes `piece.needs_lining` against current detection rules. |
| `backfill_short_codes.py` | Gives already-minted pieces the compact `PC-…` primary code, keeping the long code as an alias. |

**Seeded credentials are demo credentials.** Management logins are
`9000000000`–`9000000008` with the password equal to the number, plus
`STOREMANAGER`/`STORE`. They are predictable by design and must not survive
contact with a real factory network.

---

## Removed on 2026-09-28

| Gone | Why |
|---|---|
| `scripts.txt` | A 120 KB concatenated dump of the folder, still carrying `bootstrap_drawers.py` for the deleted drawer module. The same species as the `*.txt` dumps already in `.gitignore`. |
| `02_multirow_insert_FINAL.sql` | A spent one-off ("SUCCESSFULLY LOADED") that inserted `material_lot` rows with **no `barcode_registry` rows at all** — lots no scan could resolve, against CLAUDE.md §5/§6. `seed_materials.py` does the same job correctly. |
| `test_wages_e2e.py` | Existed because "`tests/test_wages.py` is stale … the module had no runnable proof it works". There are now ten wage test files across unit/integration/system and `pytest -m money` passes 57 checks. |
| `bootstrap_drawers.py` | Went with the drawer module — there is no drawer pool to bootstrap. CLAUDE.md §9. |
