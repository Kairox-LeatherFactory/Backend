# Test Suite Repair — Report

**Scope of edits:** `tests/` folder only. **No `app/` service/module code was modified.**
Where a test could not pass because the *service* is wrong or a feature was disabled, it is
**reported here and marked `xfail`, not patched around.**
**Runner:** `python -m pytest` · **Python:** 3.13 (`.venv`)

### Version history
- **v2 (2026-07-18)** — Rebuilt `tests/test_procurement_stage1.py` (now green); found a **third**
  confirmed service issue (dedup disabled); fixed a real-vision-model env leak into tests. See §2.
- **v1 (2026-07-18)** — Fixed wages / rbac / multi-op / service-smoke + conftest storage; found 2
  service bugs. See §4.

---

## 1. Headline (v2)

```
tests/  (excluding the 3 live-LLM procurement files: stage2, stage2_trigger, stage3)
   → 63 passed, 2 xfailed
```

| | Start | Now |
|---|---|---|
| Runnable suite (all but 3 procurement files) | ~11 failing | **63 passed, 2 xfailed** ✅ |
| `test_procurement_stage1.py` | 2 failing | **14 passed, 1 xfailed** ✅ (v2) |
| Confirmed service issues (report-only) | — | **3** (§3) |
| `stage2` / `stage2_trigger` / `stage3` | failing | **unchanged** — rewritten LLM-sole subsystem (§5) |

The 2 `xfailed` are **not** hidden failures — each is a strict-`xfail` pinned to a specific service
defect (§3.1, §3.3). The moment the service is fixed, the xfail flips to a hard failure and forces the
marker's removal, so the bug can never quietly persist.

---

## 2. What changed in v2 — Stage-1 rebuild (`tests/test_procurement_stage1.py`)

Stage-1 uses a **clean injection seam** (`ProcurementService(db, classifier=fake)`), so it *is*
fixable from tests. Two root causes, both environmental/service — **not** stale test API:

### 2.1 Fix (test-side) — real Gemini **vision** classifier leaking into tests
`test_no_silent_guessing_needs_manual_review` injects a low-confidence (0.45) **text** classifier and
expects `NEEDS_MANUAL_REVIEW`. It was **accepted** instead. Why: on a `needs_manual_review` text
verdict, `validator.validate_document` escalates to the **vision** classifier
(`app/modules/procurement/classifier.py:270`), and the local `.env` **enables vision + ships a Gemini
key** — so `build_vision_classifier()` returns a *live* model that read the real PDF over the network
and confidently accepted it, overriding the injected fake.

The module docstring explicitly states the suite runs on *"INJECTED fakes (no API key)"*, so the env is
leaking a real model in — the same class of leak as the Supabase storage one from v1. **Justified test
fix:** the autouse `_local_storage` fixture now also pins `settings.vision_classifier_enabled = False`,
so the injected text classifier is authoritative (`build_vision_classifier() → None`). This makes the
low-confidence path deterministic and offline, as designed. (Other suites that *want* vision, e.g.
`stage2_trigger::test_extract_order_pdf_via_vision_llm`, re-enable it locally via `monkeypatch`, so they
are unaffected.)

### 2.2 xfail (service issue) — sha256 dedupe/idempotency **disabled in the service**
`test_idempotent_reupload_no_second_llm` re-uploads identical bytes and expects the same `Document` id
and no second classifier call. It fails because **the service turned dedupe off** — see §3.3. The test
asserts the correct production contract, so it is marked `@pytest.mark.xfail(strict=True)` with the
exact file/lines to re-enable. **Justification:** I may not edit the service; the honest test-side
signal for a deliberately-disabled feature is a strict-xfail that self-clears when it is restored.
(The three *targeted* `_handle_existing` dedupe tests still pass — they call the dedupe logic directly,
bypassing the disabled lookup, proving the logic itself is intact.)

**Result:** `test_procurement_stage1.py` → **14 passed, 1 xfailed**.

---

## 3. Confirmed service bugs / disabled features — REPORT ONLY (do not fix in tests)

> You asked for the exact bugs. Here are the three confirmed, each with file:line and the one-line fix.

### 3.1 BUG — `create_order_with_breakdown` crashes on its documented dict input
**`app/modules/clients/repository.py:115`**
```python
code=make_sku_code(order.order_number, st.name, color_name or color_code, size)
#                  ^^^^^^^^^^^^^^^^^^  `order` is a DICT (line 85 uses order.get(...))
```
`order` is typed `dict` and consumed as one everywhere else; line 95 already uses the correct
`co.order_number` (the created `ClientOrder`). Line 115 does attribute access on the dict, so the method
raises `AttributeError` **whenever any SKU line/per_size exists.** The real caller
`app/modules/bom/service.py:1429` passes a dict, so **MD approval that materialises an order breaks in
production.**
**Fix:** `order.order_number` → `co.order_number` (or `order["order_number"]`).
**Test:** `test_service_smoke.py::test_create_order_with_breakdown_service_bug` (strict-xfail).

### 3.2 BUG — `MissingGreenlet` lazy-load in `confirm_cutting`
**`app/modules/bom/service.py:1338`**
```python
for item in bom.items:   # bom.items lazy-loads on the async session → MissingGreenlet
```
`confirm_cutting` iterates `bom.items` after `_load_bom(...)`, but `items` is not eagerly loaded, so the
async session raises `sqlalchemy.exc.MissingGreenlet`. Fails independently of any LLM. This blocks all of
`stage3` and is a real async bug.
**Fix:** eager-load in the load path, e.g. `selectinload(Bom.items)` (mirroring the codebase's own
"use selectinload under async" rule).

### 3.3 DISABLED FEATURE — upload dedupe/idempotency commented out
**`app/modules/procurement/service.py:179-187`**
```python
# ── TESTING PHASE: dedupe/idempotency check disabled ──────────────────
# ... To restore production dedupe behavior, uncomment the block below.
# existing = await self.repo.get_document_by_sha(sha)
# if existing is not None:
#     replay = await self._handle_existing(sub, kind, existing)
#     if replay is not None:
#         return replay
```
A byte-identical re-upload therefore creates a **new `Document`** every time and **re-bills the
classifier** — the §9.5 idempotency contract is off. `_handle_existing` (the dedupe logic itself) is
intact; only the lookup that reaches it is disabled.
**Fix:** uncomment lines 183-187, then delete the strict-xfail on
`test_idempotent_reupload_no_second_llm`.

---

## 4. v1 fixes recap — stale test vs. current API (all green, test-only edits)

| File | Fix |
|---|---|
| `test_wages.py` | `set_rate(RateSet(...))`; `get_run_detail(...)["lines"]`; removed `log_event` → direct `ProductionEvent`; seed `Style.code` |
| `test_rbac.py` | `log_event` removed → RBAC gate exercised via `cut()` / `scan()` |
| `test_multi_operation_employee.py` | direct `ProductionEvent` inserts + `RateSet` + `get_run_detail` |
| `test_service_smoke.py` | `create(EmployeeCreate(...))` (+password for MONTHLY); `create_client(...,order_number)`; `piece_history`→`piece_detail`; dropped removed `production_feed`; isolated the §3.1 bug as strict-xfail |
| `conftest.py` | forced `STORAGE_BACKEND=local` into a temp dir (the `.env` forces Supabase, whose SDK isn't installed → every storage test crashed) |

---

## 5. Still red — `stage2` / `stage2_trigger` / `stage3` (NOT touched further)

These target the BOM procurement **extraction** layer, which was **re-architected to "LLM-sole"**
(`app/modules/bom/extraction.py` header: *"the LLM is the SOLE extractor"*). The deterministic xlsx/grid
parsers the tests assert against are gone, and `generate_bom` **dropped the `extractor=` injection
param** these tests used — so they now hit the **live LLM** (~20 s/call, network/key-dependent) and are
further blocked by the §3.2 `MissingGreenlet`.

Making them pass is a **test-suite rebuild** (fabricate fake-model fixtures at the new
`classifier._init_model` / `extraction._llm_invoke` seam for every assertion) **plus** the §3.2 service
fix — not a test-API touch-up. Unlike Stage-1, Stage-2/3 have **no clean per-call injection seam**. This
is a scoped mini-project per stage file; say the word and I'll take `stage2_trigger` first (it is closest
to Stage-1's shape).

---

## 6. How to reproduce

```bash
cd backend

# Everything that is runnable & fixed — fast, green:
.\.venv\Scripts\python.exe -m pytest tests/ \
  --ignore=tests/test_procurement_stage2.py \
  --ignore=tests/test_procurement_stage2_trigger.py \
  --ignore=tests/test_procurement_stage3.py -q
# -> 63 passed, 2 xfailed

# Stage-1 alone (rebuilt in v2):
.\.venv\Scripts\python.exe -m pytest tests/test_procurement_stage1.py -q
# -> 14 passed, 1 xfailed

# The two strict-xfails (flip to FAIL the moment their service defect is fixed):
.\.venv\Scripts\python.exe -m pytest -q \
  tests/test_service_smoke.py::test_create_order_with_breakdown_service_bug \
  tests/test_procurement_stage1.py::test_idempotent_reupload_no_second_llm
```
