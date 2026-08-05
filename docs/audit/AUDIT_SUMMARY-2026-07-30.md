# AUDIT_SUMMARY.md — KairoX ERP pre-deploy audit

**Audited** 2026-07-30 · **Deploy target** 2026-08-02 · **Tree** `55ca2ea` + 24 modified, 8 untracked
**Scope** `app/core` + `users, wages, analytics, employees, attendance, drawers, barcode, production, clients, materials, imports`
**Not audited** `bom, procurement, inventory, supplier_po` — seams reported (F104, F126), imports not followed.

Full report with all 132 findings, code excerpts and fix sketches: `docs/audit/kairox-audit.html`
(published as an artifact). This file is the git-tracked summary.

---

## Verdict

**Do not deploy today.** Three independent, verified reasons:

1. **The application cannot be imported.** `app/main.py:92` imports `app.modules.attendance.barcode`,
   which does not exist. `app/modules/imports/router.py:31` imports `load_preview`, commented out at
   `app/modules/imports/load_to_db.py:83`. `app/modules/barcode/models.py:154` declares a `supplier`
   table already declared by `supplier_po`.
2. **The schema cannot be built by Alembic.** `alembic/versions/20260724_01_july28_fixes.py:10` ships
   the literal placeholder `down_revision = "<CURRENT_HEAD>"`, and `fiber_role_bom_item.py` sets
   `down_revision = None`, creating a second root. Two roots, two heads. The only working path to a
   schema today is `create_all()` under `DEBUG=true` (`app/main.py:145-147`).
3. **Payroll can pay the same fortnight twice.** No `CLOSED` guard on recompute
   (`app/modules/wages/service.py:329-336`), no `UNIQUE(wage_run_id, employee_id)` on `wage_line`,
   and a run that crashes mid-population leaves committed money at `status=OPEN`, invisible to the
   overlap check that filters on `CLOSED` (`repository.py:189`).

## Findings by module and severity

| Module | Blocker | High | Med | Low | Total | Verdict for Aug 2 |
|---|---:|---:|---:|---:|---:|---|
| core | 6 | 5 | 20 | 5 | **36** | DO NOT DEPLOY |
| production | 2 | 5 | 12 | 0 | **19** | DO NOT DEPLOY |
| wages | 3 | 4 | 6 | 1 | **14** | DO NOT DEPLOY |
| attendance | 2 | 4 | 5 | 0 | **11** | DO NOT DEPLOY |
| users | 0 | 0 | 8 | 2 | **10** | DEPLOY |
| materials | 0 | 2 | 7 | 0 | **9** | DEPLOY WITH NAMED RISK |
| drawers | 0 | 3 | 5 | 0 | **8** | DEPLOY WITH NAMED RISK |
| analytics | 1 | 1 | 4 | 0 | **6** | DO NOT DEPLOY |
| clients | 0 | 2 | 3 | 1 | **6** | DEPLOY WITH NAMED RISK |
| imports | 0 | 2 | 3 | 0 | **5** | DEPLOY WITH NAMED RISK |
| barcode | 1 | 0 | 2 | 1 | **4** | DEPLOY WITH NAMED RISK |
| employees | 0 | 1 | 2 | 1 | **4** | DEPLOY WITH NAMED RISK |
| **All** | **15** | **29** | **77** | **11** | **132** | |

By deadline tag: **D0 = 28** (blocks Aug 2), **D1 = 55** (fix in window), **D2 = 49** (post-deploy).

The pass sections contain 139 entries; 7 are cross-references of a finding recorded in an earlier
pass and are excluded from every count above.

## Top 10, ranked

| # | Finding | Where |
|---|---|---|
| 1 | Application cannot be imported (3 causes) | `main.py:92`, `imports/router.py:31`, `barcode/models.py:154` |
| 2 | Alembic cannot build the schema — placeholder parent + two roots | `20260724_01_july28_fixes.py:10`, `fiber_role_bom_item.py` |
| 3 | A closed payroll run can be silently recomputed | `wages/service.py:329-336` |
| 4 | An interrupted run pays the fortnight twice | `wages/service.py:361`, `repository.py:189` |
| 5 | Nothing enforces one wage line per employee per run | `wages/models.py:99-105` |
| 6 | Production JWTs may be signed with the shipped default key | `core/config.py:123` |
| 7 | A client login can read every other client's orders (6 endpoints) | `analytics/service.py:160,210,295,352,382` |
| 8 | Any employee can read the whole floor's attendance | `attendance/router.py:128` shadows `:171` |
| 9 | The geofence is bypassed by omitting GPS from the request | `attendance/service.py:169-172` |
| 10 | Every production scan 500s against a seeded database | `production/service.py:218-222`, `scripts/seed.py:97-104` |

## 3-day burn-down

| When | Work | Findings | Exit test |
|---|---|---|---|
| Jul 31 am | Delete dead imports; rename the colliding table; rebase migrations to one root/head | F00, F104, F59, F60 | `import app.main` succeeds; `alembic upgrade head` runs on an empty DB |
| Jul 31 pm | Reconcile the stage vocabulary (two `ProductionStage` enums, seeded operation codes) | F01, F02, F58, F61 | A seeded DB accepts a scan at every stage in `leather_chain()` |
| Aug 1 am | Close the money path: status guard, unique constraint, single transaction | F20, F21, F22, F23 | Recompute on a closed run returns 409; a killed run leaves no committed lines |
| Aug 1 pm | Close the exposure set: secret key, analytics tenancy, shadowed route, GPS | F32, F33, F34, F35, F49, F37 | CLIENT token 404s on another client's order; EMPLOYEE token 403s on `/attendance/today` |
| Aug 2 am | Regression sweep: rework double-decrement, negative stock, order materialisation | F03, F14, F116 | A re-posted cut scan does not move `on_hand` twice |
| Aug 2 pm | Deploy; D1/D2 recorded in a named-risk register | — | Register signed off |

Two items are genuinely a day's work rather than an edit: the migration rebase (17 files, two roots,
a branch) and the analytics tenancy scoping (six endpoints). If either slips, see the scope-cut
table in the full report — the four things that must never be cut are F20, F21, F22 and F32.

## Method and limits

- Every `.py` file in scope was read in full, plus the 17 Alembic revisions, `scripts/seed.py` and
  `tests/conftest.py`. Every claim carries a `FILE:LINE` citation.
- Route tables were built by introspecting each `APIRouter`'s registered dependencies, not by
  reading decorators — that is the only reason the shadowed route (F35) was found.
- The boot failures and the missing mixin (F84) were confirmed by executing the import.
- **The application does not run**, so nothing was verified against a live server. No HTTP
  reproduction steps appear in the report and none should be inferred.
- The working tree is uncommitted and moving; line numbers are accurate as of 2026-07-30.
- No runtime security testing: no fuzzing, no dependency CVE scan, no penetration testing.

## Phase status

- **Phase 1 (audit): complete.** No application code was modified. `app/`, `alembic/` and `scripts/`
  are untouched — verifiable with `git status`.
- **Phase 2 (gate): awaiting approval.**
- **Phase 3 (tests): not started.** Gated on approval. Tests will go into `tests/` only, extending
  the seven existing files that cover this ground rather than duplicating them, in the priority the
  brief sets: money paths → integrity → read paths.
