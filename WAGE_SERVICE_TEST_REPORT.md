# Wages Module — Test Report

**Scope:** the `wages` module only (`app/modules/wages/`).
**Method:** an end-to-end harness driving the **real** `WageService` + `WageRepository` (and the
`clients` / `employees` / `production` services they compose) against a fresh in-memory SQLite DB.
**Harness:** [`scripts/test_wages_e2e.py`](scripts/test_wages_e2e.py) — run with `python -m scripts.test_wages_e2e`.
**Date:** 2026-07-18 · **Python:** 3.13 (`.venv`)

---

## 1. Verdict

> ✅ **The wages service is working correctly.** All 10 behavioural scenarios pass, each mapped to a
> documented "command"/contract inside a wages file. Every number matches the intended contract
> (Carnaby card = 152 × 80 = ₹12,160; monthly independent of production; guards fire on bad windows).

> ⚠️ **The module's shipped test file `tests/test_wages.py` is stale and cannot run** against the
> current code. That is a *test* problem, not a *service* problem — reported in §5, **not fixed**
> (per instruction: the wage service and its behaviour are correct as-is).

| Area | Result |
|------|--------|
| Live behaviour of the service | **10 / 10 scenarios PASS** |
| Shipped `tests/test_wages.py` | **2 / 2 FAIL** — stale API (see §5) |
| Files under `app/modules/wages/` modified | **None** |

---

## 2. Methodology

- **In-memory SQLite** (`sqlite+aiosqlite://`, `StaticPool`), schema built from `Base.metadata` — the
  same pattern as `tests/conftest.py` and `scripts/smoke_test.py`. No Postgres/Docker required.
- Each scenario gets a **fresh database** so a `CLOSED` run in one test can't leak into another's
  overlap check.
- Rates are set through the real `WageService.set_rate(...)` / `set_rates_bulk(...)` using the style's
  **code** (`"CARNABY"`), exercising the code→id resolution path the frontend relies on.
- Production data is inserted by adding `ProductionEvent` rows **directly via the session**, because
  the old qty-based `ProductionService.log_event()` entry point is commented out (see §5). A test
  harness writing the DB directly is not a layering claim about the app.
- Fixture factory: `Client → ClientOrder(PO-1579) → Style(CARNABY, 152 pcs across 2 SKUs)`,
  operations `CUTTING`/`PASTING`, one `PIECE_RATE` cutter and one `MONTHLY` tailor (salary 18,000).

---

## 3. Per-file verification

Each wages file documents its intended behaviour in its header docstring (its "command"). Below is
that contract vs. what the harness actually observed.

| File | Documented contract | Observed | Status |
|------|--------------------|----------|:------:|
| **`proration.py`** | full calendar month → `salary`; consecutive splits tile to one salary; Feb pays a full month; inverted → `0.0` | `full=18000`, `9000+9000=18000`, `feb=18000`, `inverted=0.0` | ✅ |
| **`models.py`** | `Rate` effective-dated; `WageRun`/`WageLine` a **frozen** snapshot; `wage_type` stored as a snapshot string | re-read of a `CLOSED` run identical; `wage_type` column holds `'piece_rate'`/`'monthly'` | ✅ |
| **`repository.py`** | `effective_rate` = latest rate ≤ work_date; `list_runs` totals aggregated in SQL; transactions owned here | mid-period change priced per-day (₹1,800); `list_runs` total ₹30,160 matches compute | ✅ |
| **`schemas.py`** | no UUIDs on the rate surface; `RateBulkSet` rejects duplicate `operation_code` | codes resolve; duplicate-op `RateBulkSet` rejected before any write | ✅ |
| **`service.py`** | THE fork on `wage_type`; monthly never double-paid; unrated work surfaced not zeroed; hand-typed window guards | all forks correct; guard holds; unrated surfaced; 422/422/409 fire | ✅ |
| **`router.py`** | role-gated reads; `POST /runs` = COMMAND, `GET /runs/{id}` = QUERY; route order load-bearing | role gates reference valid enums (`MANAGING_DIRECTOR`, `HR`); command/query shapes distinct | ✅ (imports + wiring verified; HTTP layer not driven — see §6) |

---

## 4. Scenario results (actual output)

```
[PASS] S1  proration.prorate_monthly
       full=18000.0; split 9000.0+9000.0=18000.0; feb=18000.0; inverted=0.0
[PASS] S2  set_rate + rate_sheet (code-in)
       CUTTING=80.0 read back via code; PASTING unpriced; missing_rate_count=1
[PASS] S3  set_rates_bulk + duplicate guard
       bulk saved=2; duplicate-op rejected=True
[PASS] S4  effective-dated rate (mid-period change)
       Cutter1 10@80 + 10@100 = 1800.0 (expected 1800); run_total incl. monthly = 19800.0
[PASS] S5  compute_run piece-rate + monthly
       Cutter1=12160.0 (152x80), pieces=152; Monthly1=18000.0 pieces=0; run_total=30160.0
[PASS] S6  monthly logger not double-paid (THE guard)
       Monthly1 logged 400 pcs -> 1 line, wage_type=monthly, amount=18000.0, pieces=0
[PASS] S7  unrated operation surfaces
       Cutter1 paid=4000.0 (50x80); unrated CARNABY/PASTING unpaid_pieces=55; run_total incl. monthly = 22000.0
[PASS] S8  window guards 422/422/409
       inverted->422; future->422; overlap-closed->409
[PASS] S9  freeze + stored wage_type
       status=closed; total re-read=30160.0; stored wage_types=['monthly', 'piece_rate']
[PASS] S10 list_runs aggregation
       1 run listed; total_amount=30160.0 matches compute; total_pieces=152
------------------------------------------------------------------------------
RESULT: 10/10 scenarios passed
```

| # | Contract tested | Key numbers | ✔ |
|---|-----------------|-------------|:--:|
| S1 | Monthly proration math | full=18000, split tiles to 18000, Feb=18000, inverted=0 | ✅ |
| S2 | `set_rate` (code-in) + `rate_sheet` | CUTTING=80 read back; missing_rate_count=1 | ✅ |
| S3 | `set_rates_bulk` + duplicate guard | saved=2; duplicate op rejected | ✅ |
| S4 | Effective-dated rate | 10@80 + 10@100 = **1,800** (per-day pricing) | ✅ |
| S5 | `compute_run` piece + monthly | Cutter1 **12,160** (152×80); Monthly1 **18,000**; run **30,160** | ✅ |
| S6 | **Double-pay guard** | monthly tailor logs 400 pcs → **1** line, 18,000, 0 pieces | ✅ |
| S7 | Unrated work surfaced | Cutter1 paid 4,000; 55 PASTING pcs flagged unpaid, not zeroed | ✅ |
| S8 | Window guards | inverted→**422**, future→**422**, overlap-closed→**409** | ✅ |
| S9 | Freeze + stored type | re-read identical (30,160); `wage_type` stored as plain strings | ✅ |
| S10 | `list_runs` aggregation | SQL totals (30,160 / 152 pcs) match the computed run | ✅ |

> Note on S4/S7 during development: the first run "failed" at 19,800 / 22,000 because the *harness*
> initially asserted against `run.total_amount`, which correctly **includes the monthly tailor's
> 18,000**. The assertions were corrected to check the cutter's own line. This confirms — rather than
> contradicts — that `compute_run` always emits the monthly line alongside piece-rate work.

---

## 5. Findings — what needs to change (report only, no wage-service edits)

These are **outside** `app/modules/wages/`; none were fixed.

### 5.1 `tests/test_wages.py` is stale — both tests error before asserting anything

Confirmed by running `pytest tests/test_wages.py -q` → **2 failed**:

- `test_carnaby_wage_matches_card`
  ```
  TypeError: WageService.set_rate() takes 2 positional arguments but 5 were given
  ```
  The test calls `ws.set_rate(carnaby.id, ops["CUTTING"].id, 80, date(2026,3,1))`, but the current
  signature is `set_rate(self, body: RateSet)` where `RateSet` carries
  `style_code / operation_code / rate / effective_from`. It also reads `run.lines`, but `compute_run`
  now returns a **summary dict**, not an object with `.lines`.

- `test_pieces_do_not_need_to_conserve`
  ```
  AttributeError: 'ProductionService' object has no attribute 'log_event'
  ```
  See 5.2.

  **Recommended fix (test file, not the service):** rewrite it to the current API — `set_rate(RateSet(...))`,
  read lines via `get_run_detail(run["id"])["lines"]`, and insert `ProductionEvent` rows directly (as
  the harness does) or through whatever replaces `log_event`. The passing harness in
  `scripts/test_wages_e2e.py` is a ready template.

### 5.2 `ProductionService.log_event` is commented out — no service-level qty entry point

At [`app/modules/production/service.py:216`](app/modules/production/service.py#L216) the entire
`log_event(...)` method is commented out (superseded by the piece-based `cut()` / `scan()` flow). Two
consequences for wages:

- the old wage tests that relied on it can no longer run (5.1);
- there is currently **no service method** to insert a plain qty-based production event, so any
  wage test must reach into the ORM directly. If qty-based entry is still wanted, either restore a
  thin `log_event` or document `cut()`/`scan()` as the sole path. **This is a `production` decision,
  not a wages one** — flagged here only because it blocks wage testing.

### 5.3 No defects found inside the wage service

The `compute_run` fork, the double-pay guard, effective-dated pricing, the unrated-work warning, the
freeze/re-read snapshot, and the 422/409 window guards all behave exactly as their docstrings claim.
No change recommended to any file under `app/modules/wages/`.

---

## 6. Coverage notes & limits

- The harness exercises `service.py` + `repository.py` + `proration.py` + `models.py` + `schemas.py`
  through their real code paths. `router.py` was verified structurally (imports resolve; role gates
  reference valid `UserRole` members; command/query response shapes are distinct) but **not driven
  over HTTP** — RBAC enforcement (CLIENT/VIEWER blocked from rate & payroll reads) and FastAPI route
  ordering would need an ASGI/`httpx` test to assert end-to-end. Recommended as a follow-up if you
  want the auth gate covered.
- Piece **non-conservation** (Cutting 152 vs Pasting 155) is a `production` concern; the wage engine
  correctly prices whatever is logged per `(employee, style, operation, day)` without assuming
  conservation (demonstrated indirectly by S7, where CUTTING and PASTING are priced independently).

---

## 7. How to reproduce

```bash
cd backend
.\.venv\Scripts\python.exe -m scripts.test_wages_e2e     # → RESULT: 10/10 scenarios passed
.\.venv\Scripts\python.exe -m pytest tests/test_wages.py -q   # → 2 failed (documents §5.1)
```
