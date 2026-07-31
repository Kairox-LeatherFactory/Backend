# UAT 06 — Barcode lifecycle: history is sacred

**Role:** HR (with DM/MD)
**Prerequisites:** an employee with production events and wage lines already recorded
**Business rule:** you delete the scannable code, never the person or their record.

---

## Issue

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 1 | Create a new employee | 201, and an employee barcode is returned in the same response | ☐ |
| 2 | Print the card from that code | Caption shows the worker's name | ☐ |
| 3 | Scan it | Resolves to that employee | ☐ |
| 4 | Create a MONTHLY employee | Also provisioned a login | ☐ |
| 5 | Create an employee with a name that already exists | Accepted, disambiguated with an `IN-CHAL` prefix | ☐ |
| 6 | Enter a designation in lower case | Stored UPPERCASE | ☐ |

## Reissue a lost card

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 7 | Note the worker's current code, and their production history | — | ☐ |
| 8 | Reissue their card | A **new** code is minted | ☐ |
| 9 | Scan the **old** code | **410 Gone** | ☐ |
| 10 | Scan the **new** code | Resolves to the same worker | ☐ |
| 11 | Check their production history | Identical to step 7 — nothing lost | ☐ |
| 12 | Check their wage lines | Unchanged | ☐ |
| 13 | Log production with the new card | Accepted | ☐ |

## Deactivate a leaver

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 14 | Note the leaver's history and wage lines | — | ☐ |
| 15 | Deactivate their barcode | Accepted | ☐ |
| 16 | Scan their card | **410 Gone** | ☐ |
| 17 | Confirm the employee row still exists | Present, marked inactive | ☐ |
| 18 | Confirm every production event they logged is still there | Untouched | ☐ |
| 19 | Confirm their historical wage lines are still there | Untouched | ☐ |
| 20 | Try to log production with the retired card | Refused | ☐ |
| 21 | Run a payroll covering a period they worked | Their historical line is still payable | ☐ |

**Steps 18–19 are the point of this scenario.** A leaver's card stops working; their
record does not disappear. If deactivating a worker removes history, that is a hard
fail — the factory loses its production record and its payroll audit trail.

## Other barcode types

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 22 | Scan a piece barcode | `type: PIECE`, with its current stage and what it waits on | ☐ |
| 23 | Scan a drawer barcode | `type: DRAWER`, with its state | ☐ |
| 24 | Scan a material lot barcode | `type: *_LOT`, with available quantity | ☐ |
| 25 | Scan a code that does not exist | 404 | ☐ |
| 26 | Scan an empty / whitespace code | Rejected cleanly, not a 500 | ☐ |
| 27 | Scan the same code in lower case | Resolves — codes are normalised | ☐ |

## Permissions

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 28 | As HR, DM or MD, reissue a card | Accepted | ☐ |
| 29 | As a Supervisor or Cutting Manager, reissue a card | 403 | ☐ |
| 30 | As a **worker**, resolve their own card | Allowed — this is the one barcode route open to them | ☐ |

## Edge

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 31 | Reissue a card for a worker who has **never had one** | ⚠️ Currently mints a first card and writes a "reissue" audit row that misdescribes what happened. Deactivate correctly 404s in the same situation (`docs/audit/pass-01-business-logic.md`). Record | ☐ |

## What "pass" means

A lost card is replaced without touching history, a leaver's card stops resolving
while their record stays whole, and every printed code in the factory resolves
through one lookup with a clear 404 / 410 distinction.
