# UAT — factory acceptance checklists

Human checklists, not code. Each scenario is one real thing the factory does, walked
end to end by a person on the floor with the app open.

The automated layers (`tests/unit`, `tests/integration`, `tests/system`) prove the
logic. These prove the **flow** — that a cutting manager with a scanner in one hand
and a tray of hides in the other can actually get through their morning.

## Scenarios

| # | File | Flow | Owner |
|---|---|---|---|
| 1 | [01-breakdown-upload.md](01-breakdown-upload.md) | Order sheet → breakdown → pieces + barcodes minted | Direct Manager |
| 2 | [02-cutting-and-stock.md](02-cutting-and-stock.md) | Cut a tray, consume leather, stock drops once | Cutting Manager |
| 3 | [03-merge-gate.md](03-merge-gate.md) | Leather + lining into a drawer, DM release, line-stitch | Cutting / Lining / DM |
| 4 | [04-attendance.md](04-attendance.md) | Self check-in, proxy marking, the geofence | Worker / Supervisor |
| 5 | [05-payroll.md](05-payroll.md) | Fortnight run, one line per worker, closed = frozen | Direct Manager / HR |
| 6 | [06-barcode-lifecycle.md](06-barcode-lifecycle.md) | Lost card reissue, leaver deactivation, history kept | HR |

## How to run a session

1. **Fresh database.** Run the migrations, then `scripts/seed.py`. A UAT on a
   database with leftover state proves nothing.
2. **Set the factory coordinates first.** `PATCH /attendance/config` with the real
   lat/lon. Skip this and *every* check-in fails and *every* production scan is
   blocked — see the pre-flight below.
3. **One tester per role.** Do not run the whole thing as the MD; the MD bypasses
   the role gate, so a full-MD pass proves none of the permissions work.
4. Tick each step. On a failure, record the **step number, what you saw, and the
   request id** from the error body, then keep going — later steps often still work.

## Pre-flight (do this before scenario 1)

| ✅ | Check | Why |
|---|---|---|
| ☐ | `alembic upgrade head` completes | The schema must come from migrations, not `create_all` |
| ☐ | `GET /ready` returns 200 | Confirms the DB connection, not just that the process is up |
| ☐ | Factory lat/lon set via `PATCH /attendance/config` | Defaults are `0.0, 0.0`; the fence then sits ~1,900 km away and refuses everyone |
| ☐ | `SECRET_KEY` is not the shipped default | Otherwise every token in the session is forgeable |
| ☐ | At least one employee of each designation exists | The skill gate needs CUTTER, LINING_CUTTER, PASTER, TAILOR, FINISHER, PACKER |

## Known blockers — expect these to stop you

As of the 2026-07-31 audit, six blockers are open. If UAT is run **before** they are
fixed, these scenarios will fail at a known point. That is the audit's finding, not
your mistake — record it and move on.

| Blocker | Stops you at | Scenario |
|---|---|---|
| Barcode check-in 500s (`attendance/router.py:64`) | Any scan check-in | 4 |
| Piece-rate payroll raises `AttributeError` | Computing any run | 5 |
| Payroll 500s after closing when a salary is null | Computing a run with monthly staff | 5 |
| Three production GETs 500 | The SKU picker / progress screens | 2 |
| Re-importing an order 500s | Re-uploading a corrected breakdown | 1 |
| Any client login can check in any worker | Not visible in a happy-path walk | 4 |

Full detail: `AUDIT_SUMMARY.md` and `docs/audit/`.

## Recording a result

```
Scenario:  02 — Cutting and stock
Tester:    ____________________   Role: CUTTING_MANAGER   Date: __________
Result:    ☐ Pass   ☐ Pass with notes   ☐ Fail
Failed at step: ____   Request id: ____________________
Notes:
```
