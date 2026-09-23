# 1. What this service is

**Reject and rework.** When somebody finds a defect on a garment, this is where it is recorded, decided on, and — if the Direct Manager agrees — sent back to be redone.

## The gap it closes

> *"A piece completed in Fusing was rejected during Pasting, but there is currently no proper way to send it back to Fusing for rework."*

There was not. The sequence gate treats a completed stage as complete for good; re-logging it writes nothing; and the only way back was an edit typed into the database. So defects were handled by telling somebody, and nothing was ever counted.

| Part | File |
|---|---|
| HTTP routes | `app/modules/production/inspection_router.py` |
| Business rules | `app/modules/production/inspection.py` |
| Table | `piece_inspection` |

---

# 2. Two steps, and the second one is the DM

```
  ANY manager / HR / supervisor          THE DM (or MD)
  finds a defect                          decides
        |                                      |
        v                                      v
  POST /inspections          ->      POST /inspections/{id}/approve
  status: PENDING                    or       /decline
                                             |
                                             v
                          the garment moves (or does not)
```

**Anyone who stands at a stage can see a defect**, so any manager, HR, the store manager or a supervisor may raise a rejection.

**Moving a garment backwards is a different thing.** It re-opens a completed stage, re-orders the line's work and can cost material. So the DM approves before the piece actually moves — and until then the rejection is a **report**, not a movement.

> While a rejection is pending, **the garment cannot move at all.** Gate 6 of the production log refuses it. Otherwise the defect travels down the line, more work is spent on a garment that is going back anyway, and by the time the decision is made the piece is three stages past where it was rejected.

---

# 3. Raising one — `POST /inspections`

The smallest useful call is **the garment and the stage it was seen at**:

```json
{ "piece_barcode": "PC-23456A", "found_at_stage": "PASTING" }
```

## The defaults do the right thing

| Field | Default | Why |
|---|---|---|
| `verdict` | **`REJECT`** | This screen is only opened when somebody has found a defect. A manager who is happy with a garment logs the next stage and carries on — nobody walks to a screen to type "this one is fine". |
| `action` | **`FIX`** | Repair it where it stands. |
| `return_to_stage` | — | Required for a REDO. |

**A `REDO` is never inferred.** Sending a garment backwards has to be asked for by name, and it has to name the stage to go back to.

A `PASS` is still accepted, for a caller that wants to record one deliberately. It is written and **closed immediately** — there is nothing to approve about a garment that is fine.

---

# 4. Who is responsible — and who is deliberately not

The factory asks for this: if a stage was not done properly and the garment is damaged because of it, **that worker is answerable for that piece**.

A free-text reason cannot be counted across a month, produced in a wage conversation, or told apart from a bad hide. So the defect carries a **type**:

| `defect_type` | Names a worker? |
|---|---|
| `WORKMANSHIP` | **Yes** — `responsible_employee_id` and `responsible_stage` are required |
| `PRODUCT_DAMAGE` | **No** — naming somebody is refused |

> **PRODUCT_DAMAGE names nobody, deliberately.** Attributing a flawed hide to the cutter who happened to use it would make the record worse than useless: it would put a defect against a worker who did nothing wrong, and teach the floor to stop reporting damage at all.

The **login** signs the inspection row; the **worker blamed** is `responsible_employee_id`. They are different people in different tables.

## The responsibility report

`GET /inspections/responsibility` counts defects by the worker answerable for them — optionally for one employee.

**Workmanship only.** Product damage names nobody, so counting it there would put a supplier's bad hide onto a person's record.

It is **DM/MD only**, because it is the report that reaches a wage conversation.

---

# 5. The decision — `POST /inspections/{id}/approve` or `/decline`

DM and MD only. An optional `{"note": "..."}` body is recorded with the decision.

| Decision | Result |
|---|---|
| **approve** a `FIX` | settled outright — the piece never moves, so there is nothing to re-open. `resolved_at` is stamped. |
| **approve** a `REDO` | leaves an **open permission** behind (see below) |
| **decline** | the garment stays where it is and carries on |

Deciding an inspection twice is a **409** — "this rejection is already APPROVED; it cannot be decided again."

---

# 6. How a piece actually goes back

**Not by deleting events.** The work happened and the wage was earned; erasing it would take money off somebody for a defect that may not even be theirs.

Instead, an **approved REDO is a standing permission** that the production log reads:

```
  approved REDO on piece X, return_to_stage = FUSING
        |
        v
  the worker scans X again at fusing
        |
        v
  the log sees the permission, writes the event with is_rework = true,
  and marks the rejection RESOLVED — the permission is spent
```

Two consequences worth knowing:

1. **The rework event is flagged**, so its cost stays separable from the original work. Analytics can answer "this style cost X, of which Y was defects".
2. **The permission is spent once.** Without that last step an approved redo would let the same stage be re-logged forever.

---

# 7. The queues

| Endpoint | Returns |
|---|---|
| `GET /inspections` | **open rejections by default** — the DM's queue. Filter with `?status=`. |
| `GET /inspections/pieces/{piece_code}` | one garment's whole inspection history |
| `GET /inspections/responsibility` | defects per worker (DM/MD only) |

`GET /inspections` takes `limit` **and `offset`**. A `limit` on its own is a cap, not a pager: without `offset` a caller can ask for the first 200 rows and has no way to ask for the next 200.

---

# 8. The statuses

| `status` | Meaning |
|---|---|
| `PENDING` | raised, waiting for the DM. **The garment cannot move.** |
| `APPROVED` | the DM agreed. A FIX is settled; a REDO leaves the rework permission open. |
| `DECLINED` | the DM disagreed. The garment carries on. |
| `RESOLVED` | the redo was logged, or the FIX was approved. Nothing outstanding. |

---

# 9. Notes for backend developers

- **Route order matters.** `GET /inspections/responsibility` is declared **before** `GET /inspections/pieces/{code}` so the literal segment wins the match.
- **`rework_target()` is the permission, not a history.** It returns the one stage this garment may redo. The log spends it by marking the inspection RESOLVED.
- **Never resolve a rework by deleting the original event.** The wage is derived from production events; deleting one silently takes money off a worker.
- **`_RAISERS` is wide and `_APPROVERS` is narrow** on purpose. Seeing a defect is everybody's job; reversing the line's work is one person's.
