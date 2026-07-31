# UAT 04 — Attendance: the gate in front of everything

**Roles:** Worker (EMPLOYEE), Supervisor, HR
**Prerequisites:** factory lat/lon configured — see the pre-flight
**Business rule:** production cannot be logged for a worker who is not present today.

This is the scenario most likely to fail on a fresh install, and the failure looks
like a hardware problem when it is a configuration one. Do the pre-flight first.

---

## Self check-in (Flow A)

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 1 | A monthly worker logs in on their phone at the factory | Token issued | ☐ |
| 2 | Check in, standing inside the factory | 201; the row records a distance | ☐ |
| 3 | `GET /attendance/me/status` | Shows present, with the check-in time | ☐ |
| 4 | Check in **again** immediately | No second row; the original time is unchanged | ☐ |
| 5 | Check in from the car park / outside the fence | **403** naming the required distance | ☐ |
| 6 | Confirm no row was written for step 5 | Still one row for the day | ☐ |
| 7 | Check out at the end of the shift | Accepted; hours recorded | ☐ |
| 8 | Check out again | Handled cleanly — no duplicate, no crash | ☐ |
| 9 | Try to check out **without** having checked in | 400/404 with a clear message | ☐ |

## Barcode check-in (the floor door)

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 10 | Scan a worker's employee card at the gate terminal | Worker's name and present status | ☐ |

> ⚠️ **Known blocker.** Step 10 returns **500** on every call. The route reads a
> `direction` field the request schema no longer declares
> (`attendance/router.py:64`). The barcode attendance door is entirely down. This is
> top-10 item #4 in the audit. Record and skip to step 13.

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 11 | Scan a **retired** card | 410 Gone — distinct from "never existed" | ☐ |
| 12 | Scan a card that was never issued | 404 | ☐ |

## Supervisor proxy (Flow B)

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 13 | As Supervisor, mark 5 piece-rate workers present, from inside the fence | 201 for all 5; source is PROXY, recorded against you | ☐ |
| 14 | Try to proxy-mark a **monthly** worker | 400 — they have a login and check themselves in | ☐ |
| 15 | Try to proxy-mark from outside the fence | 403 | ☐ |
| 16 | Try to proxy-mark as a Cutting Manager | 403 — not a proxy role | ☐ |

## Permissions

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 17 | As a worker, try to read `GET /attendance/today` (the whole floor) | 403 — a worker sees only themselves | ☐ |
| 18 | As a worker, try to read another worker's history | 403 | ☐ |
| 19 | As HR, read any worker's history | 200 | ☐ |
| 20 | As a worker, try to reach `/employees` or `/wages/runs` | 403 on both | ☐ |

> ⚠️ **Security finding, not visible in a happy-path walk.** A CLIENT or VIEWER
> token can currently check in *any* employee via the barcode door, because the
> guard only covers the `proxy=true` case (`attendance/router.py:59`). It is masked
> today by the step-10 blocker. If you have a client login, try it after that
> blocker is fixed. (`docs/audit/pass-03-security.md`)

## The bridge into production

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 21 | Pick a worker who has **not** checked in; try to log production for them | 400: "mark attendance first" | ☐ |
| 22 | Mark them present, retry | Accepted | ☐ |

## What "pass" means

A worker checks in at the factory and not from home, a second tap changes nothing, a
supervisor can cover the daily-wage line, a worker sees only their own record, and
production is blocked for anyone not clocked in.
