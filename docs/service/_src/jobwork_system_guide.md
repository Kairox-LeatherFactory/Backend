# 1. What this service is

**Job work** is garments sent out of the building to an **outside factory** for one stage, and booked back in afterwards.

Two things make it different from everything else in the system:

1. **A dispatched garment cannot be scanned in-house.** Gate 7 of the production log refuses it. Otherwise the system would happily record line-stitching done here on a jacket sitting twenty miles away, and nothing would ever contradict it.
2. **The returned work is logged against the VENDOR, not an employee.** The garment advances and every progress count includes it — but `employee_id` stays `NULL`, so the event drops out of the wage query on its own. **No wage line is generated for work nobody on our payroll did.**

| Part | File |
|---|---|
| HTTP routes | `app/modules/jobwork/router.py` |
| Business rules | `app/modules/jobwork/service.py` |
| Tables | `app/modules/jobwork/models.py` (`job_work_vendor`, `job_work`, `job_work_piece`) |

**Who:** sending garments out and agreeing what is paid for them is a **DM/MD** decision. Everyone else can read where things are.

---

# 2. The flow

```
  1. POST /jobwork/vendors        register the outside factory (once)
  2. POST /jobwork/dispatch       send garments out for one stage
        -> those pieces stop being scannable in-house
  3. ... the vendor does the work ...
  4. POST /jobwork/{job_id}/receive
        -> the stage is LOGGED against the vendor
        -> the garments become scannable again
  5. GET  /jobwork                what is out, what it costs, what is late
```

---

# 3. Vendors

`POST /jobwork/vendors` — `{name, contact?, note?}`.

> **Registering the same name twice returns the original**, not a 409. A duplicate is not a mistake worth blocking, and a 409 here just leaves somebody unable to dispatch.

`GET /jobwork/vendors` lists them; `active_only` defaults to `true`.

---

# 4. Dispatch

```json
{
  "vendor_id": "…",
  "stage": "LINE_STITCHING",
  "piece_ids": ["…", "…"],
  "expected_back": "2026-10-02",
  "rate_per_piece": 35.0,
  "currency": "INR",
  "note": "second batch"
}
```

- **`rate_per_piece` is optional.** Some work is quoted per piece and some is settled another way; demanding a number nobody has yet would mean the dispatch does not get recorded at all.
- **A garment already out at another vendor is skipped**, not sent, and comes back in `skipped[]`. Two open records claiming one piece would each look satisfied by the other's return.
- `expected_back` is what drives the `overdue` flag.

---

# 5. Receiving

`POST /jobwork/{job_id}/receive`. **The body is optional.** Omit `piece_ids` and everything still out is treated as returned — the ordinary case.

```json
{ "rejected_ids": ["…"], "short_ids": ["…"], "work_date": "2026-10-01" }
```

Three outcomes per garment, and they are kept apart because they lead to **different conversations**:

| Bucket | Meaning | Advances? | Paid? |
|---|---|---|---|
| returned (the default) | the work was done | **yes** — the stage is logged against the vendor | **yes** |
| `rejected_ids` | it came back badly done | no | **no** — a quality matter |
| `short_ids` | it never came back | no | **no** — a loss to chase |

**Neither a rejected nor a short piece is work delivered**, so neither advances and neither is paid for.

## The job's own status

| Status | Meaning |
|---|---|
| `OUT` | everything is still out |
| `PARTIAL` | some came back, some did not |
| `RETURNED` | nothing is still out |

Each garment carries its own status too: `OUT`, `BACK`, `REJECTED`, `SHORT`.

---

# 6. Cost

`cost` is **derived, and only over what came back**. A garment that never returned was never work delivered, so it is never paid for.

```
cost = pieces_back × rate_per_piece
```

It is `null` when no rate was agreed.

---

# 7. The list

`GET /jobwork` — filter by `status`, `vendor_id`, `overdue`.

| Field | Meaning |
|---|---|
| `pieces_out` | how many went out |
| `pieces_back` | how many came back and were paid for |
| `pieces_rejected` | came back badly done |
| `pieces_short` | never came back |
| `overdue` | past `expected_back` with pieces still out |

> **`overdue=true` filters the PAGE**, not the query. The rule is computed per job in Python, not in SQL, so an overdue page can be **shorter** than `limit`. Do not treat a short page as "no more results" — use `offset` to continue.

`offset` exists because a `limit` on its own is a cap, not a pager.

---

# 8. How this connects to production

```
   dispatch        →  piece is OUT      →  production log gate 7 refuses it
                                            "…is out at <vendor> for <stage>
                                             since <date>. Book it back in
                                             before logging anything on it here."

   receive         →  vendor stage logged (employee_id = NULL)
                   →  piece advances, progress counts include it
                   →  wage query ignores it
```

That last line is the point of the whole design: **the garment's history stays complete, and the payroll stays correct.**

---

# 9. Notes for backend developers

- **`response_model` FILTERS the response.** A field the service returns and the model does not declare is silently dropped on the way out, with no error anywhere. `JobWorkService.payload` splices caller-supplied `**extra` into its dict, so **every** extra key any caller passes must also be declared on `JobWorkOut` — `skipped`, from dispatch, is the current example.
- **`employee_id` stays NULL on a vendor event.** That is what keeps the work out of payroll. Do not "fix" it by attributing the stage to whoever booked the pieces back in.
- **A piece may be on only one open job at a time**, which is enforced by skipping at dispatch rather than by a constraint.
