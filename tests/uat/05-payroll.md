# UAT 05 — Payroll: the money path

**Roles:** Direct Manager (runs it), HR (reads it), MD
**Prerequisites:** UAT 02 and 04 passed — production logged, attendance recorded
**Business rules:** one line per employee per run · PIECE_RATE and MONTHLY never
both · a closed run is frozen · windows never overlap.

> ⚠️ **Read this before starting.** Three of the six open blockers are in this
> scenario. Piece-rate payroll currently raises an error before it emits any SQL,
> and a run with a null-salary monthly employee crashes *after* committing and
> closing. **Do not run this against a database whose wage data you care about**
> until those are fixed. Steps 6 onward are expected to fail today.

---

## Rates first

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 1 | As DM, set a piece rate for a style × operation, effective from the period start | 200 | ☐ |
| 2 | `GET /wages/rate-sheet` | The rate appears against the right style and operation | ☐ |
| 3 | Set a **second** rate for the same style × operation, effective mid-period | Accepted — rates are date-effective, not replaced | ☐ |
| 4 | `GET /wages/rate-history` | Both rates, with their effective dates | ☐ |
| 5 | Try to set a rate as HR | 403 — rates are DM-only | ☐ |

## Run the fortnight

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 6 | As DM, compute a run for the fortnight just ended | A payload with one line per worker | ☐ |

> ⚠️ **Known blocker (top-10 #1).** Step 6 fails with an internal error for any run
> containing piece-rate work. `production/repository.py:213` groups by a column that
> does not exist. Record and stop, or continue with monthly-only staff.

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 7 | Count the lines against the number of active workers | Exactly one line per worker — never two | ☐ |
| 8 | Find a piece-rate worker's line | Priced per piece; no salary component | ☐ |
| 9 | Find a monthly worker's line | Prorated salary; **not** priced per piece | ☐ |
| 10 | Confirm nobody appears with both | The two are mutually exclusive | ☐ |
| 11 | Check a worker who worked across the step-3 rate change | Days before the change at the old rate, days after at the new one | ☐ |
| 12 | Hand-check one piece-rate line against the paper production card | Figures agree to the paisa | ☐ |
| 13 | Check a worker who joined mid-period | Salary prorated for the days in the window | ☐ |
| 14 | Check the run's status | `CLOSED` — computing freezes it | ☐ |

> ⚠️ **Known blocker (top-10 #2).** If any active MONTHLY employee has a blank
> salary, step 6 crashes *after* the lines are committed and the run is closed —
> leaving a closed, apparently-paid run plus an error. Check for blank salaries
> **before** running. Fix the data, do not retry blindly.

## A closed run is frozen

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 15 | Try to recompute the closed run | **409** — it was already paid against | ☐ |
| 16 | Read the run again | Amounts unchanged | ☐ |
| 17 | Try to compute a run whose window **overlaps** the closed one | 409 naming the clashing run | ☐ |
| 18 | Try a window overlapping an abandoned **OPEN** run | 409 — a half-finished run still holds its window | ☐ |
| 19 | Compute the next, non-overlapping fortnight | Accepted | ☐ |

> ⚠️ Step 15 is correct, but note the audit finding: the `confirm_closed` override
> the error message offers is not wired to any route, so recompute is **permanently**
> 409 via the API. There is currently no in-app way to correct a payroll error —
> close the run and issue an adjustment run instead. (`docs/audit/pass-02-money-paths.md`)

## Bad windows

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 20 | End date before start date | 422 | ☐ |
| 21 | A window ending in the future | 422 — cannot pay for work not yet done | ☐ |
| 22 | A 400-day window | ⚠️ Currently **accepted** and priced. No maximum span exists (`pass-12`). Record the figure | ☐ |

## Who may see money

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 23 | As HR, read the run | 200 | ☐ |
| 24 | As MD, read the run | 200 | ☐ |
| 25 | As a Supervisor, Cutting Manager, Viewer or Client, read the run | **403** for each | ☐ |
| 26 | As HR, try to *start* a run | 403 — HR reads, DM runs | ☐ |
| 27 | As a worker, try any `/wages` route | 403 | ☐ |

## What "pass" means

Every active worker gets exactly one line, priced by their own wage type, at the rate
effective on each day worked, and once closed the numbers never move again.

**Two lines for one worker in one run, or a closed run changing value, is a hard
fail — that is money paid twice.**
