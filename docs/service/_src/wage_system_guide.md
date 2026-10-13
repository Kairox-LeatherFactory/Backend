# 1. What this service is

Payroll. It does two separate jobs:

1. **Rates** — what one operation on one style pays per piece.
2. **Runs** — computing what everybody is owed for a window of dates, and freezing that as the document of record.

| Part | File |
|---|---|
| HTTP routes | `app/modules/wages/router.py` |
| Business rules | `app/modules/wages/service.py` |
| Queries | `app/modules/wages/repository.py` |
| Tables | `app/modules/wages/models.py` (`rate`, `wage_run`, `wage_line`, `wage_line_detail`) |

---

# 2. Who can do what — visibility is not authority

| | Roles |
|---|---|
| **Read** payroll | HR, Direct Manager, Managing Director |
| **Write** payroll | **Direct Manager, Managing Director only** |

Writing means: setting a rate, starting a run, recomputing one, reopening, closing, deleting.

> **HR reads payslips; it does not decide what a person is paid.** These used to be one gate, so HR could set piece rates, start a run and delete a completed one. If you add a route here, pick the gate by **what the route does**, not by who wants to see the page.

Nobody else can reach any of it. That is deliberate and stricter than the rest of the codebase: `client` is a real login, and **per-piece labour rates are the factory's cost structure**. A client who can see that cutting pays 12.50/pc on their own style is holding your margin during the next price negotiation.

---

# 3. Rates

## No UUIDs on the rate screens

Everything is addressed by **code**: `style_code` (`JP-CLERMONT_VEST`) and `operation_code` (`CUTTING`). The service resolves them.

They are **query parameters, not path segments** — the day somebody imports a style whose name slugs to something containing a slash, a path parameter breaks and a query parameter does not.

## The screen flow

```
GET /wages/orders                          the landing screen: order cards
      |   each card badges "3 of 7 styles priced"
      v
GET /wages/styles?order_number=…           the style cards for that order
      |   each badges rated_operations / total_operations
      v
GET /wages/rate-sheet?style_code=…         every operation with its current rate
      |
      v
POST /wages/rates/bulk                     save the edited column, one transaction
```

> The landing screen is **orders first** on purpose. It used to open straight onto hundreds of style cards from every order at once, with nothing on the card saying which order it belonged to.

The `n of m priced` badge exists so **unpriced styles are visible before a run silently pays zero for them.**

## Rates are date-effective

A rate carries an `effective_from`. A mid-period rate change prices **each day at the rate in force that day** — the run does not pick one rate and apply it to the whole window.

`GET /wages/rate-history?style_code=&operation_code=` is every rate ever set for that cell, newest first.

### The blended rate on a payslip line

If a rate changed mid-period, those pieces were priced at several rates. The stored line shows `amount / pieces` — the **blended** rate actually paid — not "the last rate wins". Otherwise the printed line would read `pieces × rate ≠ amount`, and which rate showed would be nondeterministic.

## Saving

| Call | Use |
|---|---|
| `POST /wages/rates` | one cell |
| `POST /wages/rates/bulk` | a whole edited sheet, in **one transaction**, all codes resolved first |

Prefer bulk. Half a saved rate sheet is worse than none.

---

# 4. Runs — the part to read slowly

## `run_kind` selects the pricing rule. It is not a filter.

| `run_kind` | Pays | Scope |
|---|---|---|
| **`piece`** (default) | PIECE_RATE workers | **`style_code` or `order_number` is REQUIRED.** Dates alone is a **422**. |
| **`monthly`** | salaried staff, priced from the calendar | no scope needed |
| `combined` | legacy — pre-dates the split, paid both populations from one window | |

**Why a piece run must name a style:** a piece wage is earned on a specific garment, so the style *is* the run. Without this, a manager types two dates, presses compute, and pays for **every garment in the factory** in that window.

A monthly run **may** carry `order_number` / `style_code`, but only as a **label** for the payroll screen. The response says so with **`scope_is_label: true`**, and the screen must render it differently — it must not print "CLERMONT payroll" over a sheet that paid every salaried person in the building.

> **A piece run and a monthly run over the same window do not conflict.** Their employee populations are disjoint, so the pair is one complete payroll for that fortnight. **Compute both.**

## PIECE_RATE and MONTHLY are mutually exclusive

**One line per employee per run.** Nobody is ever paid both ways in one run.

## The overlap guard is about pieces, not dates

Two runs conflict only if they share **both** a date window **and** a style. Computing order KJ2451 and order KJ2452 for the same fortnight pays two disjoint sets of garments, and refusing the second would be refusing correct work.

A conflict is a **409 that names the run blocking you and what to do about it.**

## Freeze, or keep it a draft

`freeze` defaults to `true`.

```
freeze: false  ->  OPEN (a draft)     recompute freely, then POST /runs/{id}/close
freeze: true   ->  CLOSED             the document of record
```

---

# 5. The states, and the doors between them

```
                 compute (freeze:false)
                     |
                     v
                   OPEN  ──── close ────►  CLOSED
                   ▲  │                      │
       recompute ──┘  │                      │
       (free)         └────◄─── reopen ──────┘
                             (reason required)
```

| Call | On an OPEN run | On a CLOSED run |
|---|---|---|
| `POST /runs/{id}/recompute` | free | **409** — reopen first (or `confirm_closed: true`, see below) |
| `POST /runs/{id}/close` | freezes it. **Idempotent.** Refuses a run with no lines. | returns it unchanged |
| `POST /runs/{id}/reopen` | — | the only door out of CLOSED. **`reason` is required.** |
| `DELETE /runs/{id}` | deletes it | **refuses** unless `confirm_closed: true` |

## Why reopen is a separate call from recompute

A manager who must press **reopen**, type **why**, then press **recompute** cannot rewrite a paid payslip by mistyping a run id.

`confirm_closed: true` on recompute is the one-call escape hatch. It works, it stamps the recompute — and it **records no reason**, which is exactly why the reopen door exists.

## What recompute keeps and what it changes

It discards the run's lines and rebuilds them from **current** production events and rates, for the **same window and the same scope**. The run keeps its id, window and scope. `recompute_count` increments and the actor is stamped — so a payslip reprinted afterwards is **identifiably a different document** from the one the cash was counted against.

## Deleting

`DELETE /runs/{run_id}` is the escape hatch the overlap 409 has always pointed at.

`create_run` commits an OPEN run **before** any line is written, so a compute that died halfway leaves a committed, empty run permanently occupying that window and blocking every later run over those dates. Deleting clears it.

**A closed run is refused unless you confirm.** It is the document the cash was counted against; deleting it destroys the record of a payment that actually happened. To **change** a frozen run, reopen and recompute — that keeps the audit trail. Deleting is for runs that should never have existed.

---

# 6. Read these before you pay anyone

Every run response carries them.

## `unrated_operations`

Work was done at an operation with **no rate**. Those pieces were counted and paid **nothing**. Set the rates and recompute.

The list is **frozen with the run**, so reading an old run tells you what was unrated *at the time*, not what is unrated today.

## `gap_days`

Days inside the window that no previous run covered. A large number usually means somebody typed the wrong start date.

## `diagnostics` — why the total is what it is

A run that pays nothing has three different causes, and they used to be indistinguishable: an empty window, a population on the wrong wage type, and a full rate sheet dated **after** the work. All three rendered as `total_amount: 0.0` with an empty `lines`.

The diagnostics block accounts for **every piece the window contained** — paid, skipped, unrated or backdated — and `notes` turns whichever bucket is non-zero into a sentence naming the fix. It is present on **every** run, not just empty ones, because "why is this smaller than I expected?" is the same question.

## Backdated pricing

A piece priced from a rate whose `effective_from` is **after** the day it was worked is **paid, and reported**. Fix the `effective_from` and recompute if that was not intended.

---

# 7. Reading a run

| Endpoint | Answers |
|---|---|
| `GET /wages/runs` | run history, **newest computed first**, filterable by kind / order / style / dates / status |
| `GET /wages/runs/{id}` | the frozen run — payslip detail, **no recomputation** |
| `GET /wages/runs/{id}/breakdown` | the run folded **three ways**: per style (with stages and workers nested), per stage factory-wide, per employee |
| `GET /wages/runs/{id}/pieces` | per-piece detail: which garment, which stage, which worker, their **employee barcode**, what that one piece paid |
| `GET /wages/ledger` | every computed run, latest computed first, searchable |

## The breakdown's three tabs cannot disagree

All three folds come off the **same frozen row set**. If the frontend sums them itself, they will disagree with each other and with the run total.

## `amount: null` is not zero

On the per-piece view, a row with `amount: null` carries a `note` explaining why — a monthly worker, or the stage was unrated at run time. **It is deliberately not a zero**, which would read as work worth nothing.

Amounts come from the run's **frozen** rate for that (employee, style, stage) cell. Nothing is re-priced, so a rate corrected after the run closed does not change what the run says it paid.

## The recompute workflow

```
GET /wages/runs?style_code=CLERMONT&date_from=…&date_to=…
      -> read the id off the matching row
POST /wages/runs/{id}/recompute
```

The date filters **overlap-match**, so a fortnight that straddles a month boundary still comes back.

`recompute_count` and `reopen_count` on each row make a run that has been rebuilt or unfrozen visibly different from one that has not.

> The ledger is ordered by **when it was computed**, not by its period. The question the ledger answers is "what did we just run".

---

# 8. Where the money comes from

```
   attendance          production events            rate (date-effective)
       |                      |                          |
       |  monthly: days       |  piece: qty per           |
       |  present in window   |  (employee, style, stage) |
       +----------------------+--------------------------+
                              |
                              v
                        wage_line  (one per employee per run)
                              |
                              v
                     CLOSED = frozen snapshot
```

Two consequences:

- **A wage follows the production event**, so correcting who was recorded on an event (`PATCH /production/events/{id}/reassign`) moves the money automatically. Nothing is stored against the worker directly.
- **Correcting attendance or events after a run is closed does not change that run.** It is frozen. Reopen and recompute if it should.

---

# 9. Notes for backend developers

- **Route order is load-bearing.** Static segments (`/runs/{id}/breakdown`, `/pieces`, `/close`, …) must be declared before `GET /runs/{run_id}`, or FastAPI tries to parse a literal segment as a UUID and 422s.
- **`_PAYROLL_READERS` and `_PAYROLL_WRITERS` are two gates on purpose.** Do not collapse them again.
- **The run's scope is stored as ids**, and the overlap guard resolves both scopes to their real style-id sets. Comparing scope *strings* could not tell whether a style belonged to an order, so any order-scoped run blocked any style-scoped run in the window.
- **A run's own window is excluded from its overlap check**, or recompute would always 409.
- **`unrated_operations` is frozen with the run** (`unrated_snapshot`), not recomputed on read.
- **Page shapes here are the older ones** (`RunPiecePage`, `LedgerPage`), not the `Page[T]` envelope. They are published contracts a screen reads today; they migrate when their consumer is ready.
