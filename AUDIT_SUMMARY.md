# AUDIT_SUMMARY.md — KairoX ERP Phase 1

**Audited** 2026-07-31 · **Ship target** 2026-08-02 (2 days) · **Type** delta re-audit
**Scope** `app/core` + `users, wages, analytics, employees, attendance, drawers, barcode, production, clients, materials, imports`
**Not audited** `bom, procurement, inventory, supplier_po` — seams reported, imports not followed.

Prior audit (2026-07-30) preserved at `docs/audit/AUDIT_SUMMARY-2026-07-30.md` and
`docs/audit/kairox-audit.html`. Per-pass reports: `docs/audit/pass-01…15`.
Prior findings re-verified one by one in `docs/audit/delta-register.md`.

---

## Verdict

**Do not ship on Aug 2 without the six blockers below.** The good news first, because it is real:

- The application imports cleanly (`python -c "import app.main"` succeeds).
- Alembic is single-root, single-head, no placeholders (`alembic heads` → `20260731_wage_line_uniq`).
- `uq_wage_line_run_emp` exists in the model **and** the migration.
- The `Supplier` table/class collision is gone; the test suite went from 93 failures to **53**
  (18 in scope), and the stale `full_test.log` no longer reflects reality.

The problem is that the Jul-31 edits introduced **four new blockers**, and the security set from
yesterday is almost entirely untouched. Net blocker count is unchanged at six — and the money path
is **worse** than it was yesterday:

1. **Piece-rate payroll cannot run at all.** `production/repository.py:213,222` group and order by
   `Piece.style_id`, a column that does not exist. Every piece-rate wage run raises
   `AttributeError` before emitting SQL. Reproduced in `tests/test_wages.py`.
2. **A payroll run 500s *after* committing and closing.** `wages/service.py:572` calls
   `employees.names_for`, which does not exist, on the routine path where a MONTHLY employee has a
   null salary — after `add_lines` and `close_run` have already committed.
3. **The barcode attendance door is 100% broken.** `attendance/router.py:64` reads `body.direction`;
   the field is not on `ScanCheckIn`. Every scan check-in 500s.
4. **Three production GETs 500 on every call.** `production/router.py:71,98,110` pass a
   `client_scope` kwarg the service signatures do not accept.
5. **Any client login can check in any employee.** `attendance/router.py:59` guards only the
   `proxy=True` case; with `proxy=False` a CLIENT or VIEWER token writes attendance for anyone.
   Masked today by #3 — fixing #3 without this makes it live.
6. **The repository is in an unresolved merge.** `git ls-files -u` shows three index stages for
   `imports/load_to_db.py` and add/add for `production/router.py`. What was tested is the working
   tree; what would be committed is undefined.

---

## Findings by module and severity — open as of today

Carried-open from the prior audit, plus new findings raised this run (F139+).

| Module | Blocker | High | Med | Low | Total | Verdict for Aug 2 |
|---|---:|---:|---:|---:|---:|---|
| production | 2 | 4 | 11 | 1 | **18** | **DO NOT SHIP** |
| wages | 1 | 4 | 8 | 1 | **14** | **DO NOT SHIP** |
| attendance | 2 | 4 | 7 | 0 | **13** | **DO NOT SHIP** |
| imports | 1 | 2 | 6 | 0 | **9** | **DO NOT SHIP** |
| core | 0 | 5 | 21 | 3 | **29** | SHIP WITH NAMED RISK |
| users | 0 | 1 | 8 | 2 | **11** | SHIP WITH NAMED RISK |
| materials | 0 | 2 | 8 | 0 | **10** | SHIP WITH NAMED RISK |
| analytics | 0 | 2 | 6 | 0 | **8** | SHIP WITH NAMED RISK |
| drawers | 0 | 1 | 6 | 0 | **7** | SHIP WITH NAMED RISK |
| clients | 0 | 1 | 5 | 1 | **7** | SHIP WITH NAMED RISK |
| barcode | 0 | 0 | 4 | 1 | **5** | SHIP |
| employees | 0 | 0 | 3 | 1 | **4** | SHIP |
| **All** | **6** | **26** | **93** | **10** | **135** | |

**New this run: 39** (6 blocker, 8 high, 24 med, 1 low). **Carried from prior: 96**
(73 open, 9 partial, 4 regressed, 10 reclassified). **Closed since yesterday: 38.**

By deadline: **D0 = 14** (blocks Aug 2), **D1 = 41**, **D2 = 80**.

---

## Top 10, ranked by Aug 2 impact

| # | Finding | Where | Tag |
|---|---|---|---|
| 1 | Piece-rate payroll dies at the query layer | `production/repository.py:213,222` | D0 |
| 2 | A run 500s after committing and closing; cleanup then fails on the FK | `wages/service.py:572`; `wages/models.py:109-111` | D0 |
| 3 | Repository sitting in an unresolved merge | `git ls-files -u` | D0 |
| 4 | Barcode attendance check-in always 500s | `attendance/router.py:64` | D0 |
| 5 | Any client/viewer token can check in any employee | `attendance/router.py:52-61` | D0 |
| 6 | Three production GETs 500 (and are untenanted once fixed) | `production/router.py:71,98,110` | D0 |
| 7 | Geofence bypassed by omitting GPS; the row still counts as present | `attendance/service.py:195-207` | D0 |
| 8 | Unconfigured deploy boots on the shipped JWT key, with `create_all` and leaked tracebacks | `core/config.py:50-51,124,256`; `main.py:144-147,229` | D0 |
| 9 | One payroll run = four commits; the single-transaction code exists and is dead | `wages/repository.py:252,266,239,270` vs `:382-430` | D0 |
| 10 | Fresh DB centres the geofence on 0°N 0°E and blocks the whole floor | `attendance/models.py:63-64` | D0 |

Items 7, 9 and 10 are the ones most likely to be under-rated. #10 in particular needs no attacker
and no unusual data — it is what happens on a clean deploy, and it blocks production logging
factory-wide.

---

## Two-day burn-down

| When | Work | Exit test |
|---|---|---|
| **Jul 31 pm** | Resolve + stage the merge; delete the eight `*_dump.txt` files | `git ls-files -u` empty; working tree == index |
| **Aug 1 am** | Blockers 1, 2, 6: `SKU.style_id` in group/order by; hoist `_name_unrated` above `close_run` and use `list_all`; add `client_scope` to the three service signatures | `pytest tests/test_wages.py` green; a piece-rate run completes; the three GETs return 200 |
| **Aug 1 pm** | Blockers 4, 5, 7: restore `direction` on `ScanCheckIn`; collapse the BOLA guard to one condition; reject GPS-less SELF scans with 422 | CLIENT token 403s on another worker's barcode; a GPS-less scan 422s; a valid scan 201s |
| **Aug 2 am** | Blockers 8, 9, 10: flip `environment`/`debug` defaults; wire `compute_run` to the `*_nocommit` seam + one commit; geofence fails open with a warning when unconfigured | Boot with no `.env` refuses the default key; a killed run leaves no committed lines; a fresh DB accepts a check-in |
| **Aug 2 pm** | Regression sweep + deploy; D1/D2 into a signed risk register | Baseline in-scope failures < 18; register signed |

Two items are a day's work rather than an edit if attempted properly: batching the production log
path (`pass-10`) and extracting the four gates (`pass-08`). **Neither is on this list** — both are
D1/D2 with safe Aug 2 fallbacks.

---

## Scope cuts, if the window closes

Cut in this order. The four that must **never** be cut are #1, #2, #3 and #5 above.

| Cut | Fallback for Aug 2 | Cost |
|---|---|---|
| Production log batching (`pass-10`) | Ship the one-line `IN (...)` piece fetch only | 40 of ~250 round trips saved; rest deferred |
| Analytics role gates (`pass-03`) | Leave as-is — CLIENT is correctly scoped, so this is excessive internal access, not a leak | Internal over-exposure for one release |
| Pagination (`pass-09`) | Default `include_pieces=False` + hard `LIMIT 500` in the three worst queries | Invisible to callers, removes outage risk |
| Rate limiting (`pass-03`) | Enforce at the load balancer, not in code | Config, not code; record the in-app gap |
| Drawers repository (`pass-07`) | Leave; current call order is correct | Structural debt only |
| Gate extraction (`pass-08`) | Test the gates through the service, as today | No behaviour change |

---

## Test baseline

`full_test.log` was stale — it recorded 93 failed / 100 passed against the pre-rename
`Supplier` collision that no longer exists.

**Actual, today:** 53 failed / 153 passed / 1 xfailed. **In scope only** (excluding the seven
BOM/procurement/inventory/supplier_po files): **18 failed / 112 passed**.

Three of those 18 failures are test defects, not app defects, and should not be counted against
the modules:

- `tests/test_attendance_fixes.py.py` — double `.py` extension **aborts collection of the entire
  suite**; every run needs `--ignore` until renamed.
- `tests/test_uat_scenarios.py:189` — imports `Supplier` from `barcode.models`, renamed to
  `MaterialSupplier`.
- `tests/test_final_modules_fixes.py` — reads a file with the platform default encoding;
  `UnicodeDecodeError` on Windows.

The rest map to real findings, chiefly the `Piece.style_id` blocker and tests that log production
without marking attendance first.

---

## Safe to ship Aug 2?

**Per module** — see the table above. In short:

- **SHIP:** `barcode`, `employees`.
- **SHIP WITH NAMED RISK:** `core`, `analytics`, `materials`, `drawers`, `users`, `clients`.
- **DO NOT SHIP:** `production`, `wages`, `attendance`, `imports` — each holds at least one
  blocker that fails on ordinary input, not on an edge case.

**Overall: no, not as it stands.** With the two-day burn-down above completed and verified, yes —
for **one factory on one replica**, with a signed risk register covering rate limiting, backups,
logging, pool settings and the single-replica assumptions in `pass-15`.

For the ten-factory target named in the brief, `pass-15` is a pre-scale milestone that must land
between Aug 2 and Aug 20 — before the second site, not after.

---

*No application code was modified in producing this audit. All fix sketches are proposals.*
