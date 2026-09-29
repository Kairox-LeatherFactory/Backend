# 1. What this service is

Attendance records **who was in the factory on which day**. It matters for two reasons:

1. **Wages.** A monthly worker's pay and a daily worker's day are priced from it.
2. **Production.** A worker must be **present today** before any production can be logged against them.

## The one rule that shapes everything

> **Every attendance write comes from an OPERATOR login: Security, HR, Managing Director or Direct Manager.**

Nobody else can punch, because nobody else on the floor **has** a login. Shop-floor workers have no account at all. They are identified by their **card**, and an operator scans it for them.

There is no such thing here as "a worker checking themselves in".

| Part | File |
|---|---|
| HTTP routes | `app/modules/attendance/router.py` |
| Business rules | `app/modules/attendance/service.py` |
| Corrections | `app/modules/attendance/corrections.py` |
| Tables | `app/modules/attendance/models.py` (`attendance_log`, `shift_config`) |

---

# 2. Location tracking has been removed

This trips up anyone reading older code or an old frontend.

- **No route asks for, validates or stores a position.**
- `lat` and `lon` are still **accepted and ignored** on the bodies that used to require them. They are kept so an older frontend build gets a `201` instead of a `422`.
- `distance_m` in a response is always `null` on new rows. Historical rows keep the distance they were recorded with.
- `location_unverified` in the scan result is always `true`. It no longer means "we could not verify the position" — nothing is verified. It is kept so a frontend still reading the key does not crash.

Check-in and check-out succeed on **identity alone**.

---

# 3. The two doors

Both doors run through the **same** dependency (`require_operator`) and the **same** internal primitives, so they can never drift apart.

## Door 1 — the card scan (primary)

`POST /api/v1/attendance/scan-check-in`

```json
{ "employee_barcode": "EMP-000123", "direction": "in" }
```

- `direction` is `"in"` or `"out"` — **one endpoint does both**. There is no `scan-check-out`.
- The router resolves the card to an employee id **before** any work starts. An unknown card is a **404**; a retired card is a **410** from the barcode service.
- `proxy: true` marks it as the manual fallback (card will not scan, or was forgotten). It changes the recorded `source` from `SELF` to `PROXY`, nothing else.

The answer is what the gate terminal should display:

```json
{
  "employee_id": "…", "employee_name": "Ramesh",
  "work_date": "2026-09-23",
  "check_in_at": "2026-09-23T03:31:00Z", "check_out_at": null,
  "is_late": false, "present_today": true,
  "location_unverified": true
}
```

Times here are **strings**, not objects — the terminal renders them straight back.

## Door 2 — typing them in (fallback)

`POST /api/v1/attendance/proxy/check-in` and `POST /api/v1/attendance/proxy/check-out`

```json
{ "employee_ids": ["3f8c…", "7a1c…"] }
```

- One call can mark **several** people at once. The answer is a **list** of attendance rows.
- It works for **any** wage type.
- An unknown employee id in the list is a **404** for the whole call.

## Door 3 — the operator's own punch

`POST /api/v1/attendance/check-in` and `/check-out` record the **operator's own** arrival and departure. The body is optional: `{}`, `{"lat":…, "lon":…}` (ignored) or no body at all.

The login must be linked to an employee row, otherwise **400** — *"This login is not linked to an employee record."*

---

# 4. The rules the server applies to a punch

| Rule | Behaviour |
|---|---|
| One row per employee per `work_date` | Enforced by a database constraint, not just by code. |
| Check-in is **idempotent** | Scanning in twice on the same day is a no-op. The second scan returns the row that already exists — it is not an error. |
| Check-out with no check-in | **404** — *"No check-in recorded today — scan in first."* |
| The timestamp is the server's | Never the device's. A wrong tablet clock cannot move somebody's shift. |
| `work_date` is the **factory's** calendar day | Computed in the factory timezone, not UTC. A 06:00 shift in India is not yesterday. |

## The three flags

Set from the shift policy (`shift_config`) at punch time:

| Flag | Meaning |
|---|---|
| `is_late` | check-in was after `shift_start + late_grace_minutes`. Compared in **factory local wall-clock time**, not UTC. |
| `is_short` | on check-out: hours worked were **less** than the standard shift length. |
| `is_overtime` | on check-out: hours worked were **more**. |

`is_late` is decided at check-**in**. `is_short` and `is_overtime` can only be known at check-**out**, so they are `false` until then.

> The shift policy has **no HTTP endpoints any more** — the read and the write were both removed. The policy is still applied on every punch and still prices the wage run; it is changed in the database or the seed, not through the API.

---

# 5. `source` — how the row was created

| Value | Meaning |
|---|---|
| `SELF` | a card scan with `proxy: false`, or an operator's own punch |
| `PROXY` | the manual door, or a scan marked `proxy: true` |

`recorded_by_user_id` is **who** made the row — the operator's user id. Every worker row has one, because a worker cannot make their own. It is `null` only on legacy rows written before the operator model existed.

> In a dispute, `recorded_by_user_id` is the field that matters. It was always in the database but was missing from the API for a while, which made the operator trail real and invisible at the same time.

---

# 6. Onboarding a daily worker

`POST /api/v1/attendance/daily-workers` — **DM, HR or MD only** (deliberately *not* Supervisor: putting somebody on the payroll is an authority).

```json
{ "name": "Suresh", "designation": "helper", "phone": null, "daily_rate": 650 }
```

It creates the employee record and a printable card. **No login is created.** `phone` is contact detail only — there is nothing to authenticate with it.

The answer is `{id, name, wage_type}`.

---

# 7. Correcting a mistake

The mistake this exists for is a **swapped card**: Security scans MAJID, but the worker standing there was SALIM. By the time anyone notices, MAJID has a day of cutting logged against his name.

So the fix is **re-allocation**, not deletion.

## `PATCH /attendance/{attendance_id}` — any operator

Every field is optional; what you omit stays as it was.

```json
{ "employee_id": "the-right-person", "reason": "card swapped at gate" }
```

> **Sending a different `employee_id` also moves that day's production events**, in the same transaction. The work really happened — it was filed under the wrong name. If attendance moved and the events did not, attendance would say one name while the cutting log paid another, and wages are computed from the events.

The response tells you how many moved:

```json
{ "attendance_id": "…", "employee_id": "…", "work_date": "2026-09-23",
  "check_in_at": "…", "check_out_at": "…",
  "is_late": false, "is_short": false, "is_overtime": false,
  "production_events_moved": 7,
  "message": "…" }
```

Show `production_events_moved` to the operator. It is the proof that the correction was complete.

Editing the times re-computes `is_late`, `is_short` and `is_overtime`.

**Why any operator can edit:** a gate operator who mis-scans should be able to fix it immediately, without going to find somebody more senior.

## `DELETE /attendance/{attendance_id}` — HR, DM, MD only

For a punch that should never have existed. `reason` is **required** (it is a query parameter, not a body field).

It is **refused with 409** when the worker has production events that day. The work was really done, so the cause is almost certainly a swapped card — and the right fix is to re-allocate, not to delete.

**Why this is narrower than edit:** deleting a punch decides whether somebody is paid for the day at all.

---

# 8. Reading attendance

| Endpoint | Who | What |
|---|---|---|
| `GET /attendance/today` | operators + floor leads | the whole floor's roster for today |
| `GET /attendance/history?employee_id=&start=&end=` | operators + floor leads | one person's history. `employee_id` is **required** (422 without it) |
| `GET /attendance/me` | any logged-in user | the caller's own rows. Defaults to the last 30 days |

**Floor leads** here means Supervisor, Cutting Manager, Lining Manager and Stitching Manager — they need the roster even though they may not write punches. Client and Viewer cannot read attendance at all (**403**).

`GET /attendance/me` returns an **empty list** — not an error — when the login is not linked to an employee row.

---

# 9. How this connects to the rest of the system

```
   Security scans the card
            |
            v
   attendance_log row for today  ──────► production logging checks
            |                            "is this worker present today?"
            |                            no row = the scan is refused
            v
   wage run prices the days
   (monthly: days present; daily: days × rate)
```

Two consequences worth knowing:

- **If the gate did not scan somebody in, the floor cannot log their work.** When a manager reports "the system will not let me log this worker", the first thing to check is today's attendance.
- **Correcting attendance after a wage run is closed does not change the run.** A closed run is a frozen snapshot. See the Wages guide.

---

# 10. Notes for backend developers

- **One dependency for every write** (`require_operator`), so the barcode door and the manual door cannot drift.
- **`_open_or_reject` and `_close` are the only two primitives.** Every door calls them, which is why the scan door behaves identically to the manual one.
- **The `work_date` unique constraint is the real guard.** The idempotent path catches an `IntegrityError`, rolls back and returns the row that won — two terminals scanning the same card at the same instant cannot make two rows.
- **`is_late` converts UTC to factory-local before comparing.** Comparing a UTC timestamp to a local `shift_start` makes everyone late (or nobody).
- **Three endpoints are commented out in place, with their reasons** (`/me/status`, `GET /config`, `PATCH /config`). The service methods behind them are untouched. Do not delete those blocks — the rationale is what stops them being re-added by accident.
