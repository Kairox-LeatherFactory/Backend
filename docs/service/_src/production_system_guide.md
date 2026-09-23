# 1. What this service is

This is **the floor's logging surface**. Every piece of work done on a garment — cut, fused, pasted, stitched, finished, inspected, packed — is recorded here.

There is **one write endpoint**: `POST /api/v1/production/log`.

| Part | File |
|---|---|
| HTTP routes | `app/modules/production/router.py` |
| The gates and the write | `app/modules/production/service.py` |
| Corrections | `app/modules/production/corrections.py` |
| Tables | `app/modules/production/models.py` (`piece`, `production_event`, `operation`, `style_operation`) |
| Stages, roles, skills | `app/core/enums_barcode.py` |

---

# 2. The two ideas that make this service make sense

## Idea 1 — the caller NEVER sends a stage

There are no stage buttons on the scan screen. The operator scans a worker and one or more garments, and the server works out what stage this is:

- On a **cut screen** (`LEATHER_CUT` or `LINING_CUT`) the screen fixes the stage.
- Everywhere else (`PIPELINE`) the stage is **inferred from each piece's own history** — the next stage on its chain.

Even the screen is usually not sent: `screen_context` is **derived from the caller's role**. Only DM and MD may override it; any other role's value is ignored.

Why: an operator picking a stage from a list is an operator who will eventually pick the wrong one, and a wrong stage is a wrong wage and a broken sequence.

## Idea 2 — two doors, one shape

| Door | Sends |
|---|---|
| **Barcode** | `actor.employee_barcode` + `targets.piece_barcodes` |
| **Manual** | `actor.employee_id` + `targets.sku_id` + `targets.piece_seqs` |

Both POST **the same body shape** to the same endpoint. The router resolves barcodes to ids, and from then on the service sees **ids only**. That is why the two doors cannot drift apart.

```json
{
  "actor":   { "employee_barcode": "EMP-000123" },
  "targets": { "piece_barcodes": ["PC-23456A", "PC-23456B"] },
  "work_date": "2026-09-23",
  "consumption": { "article": "GOAT SUEDE", "colour": "DARK BROWN", "dcm": 412 },
  "preview": false
}
```

`actor` needs **one** of the two fields (**422** otherwise). `targets` needs `piece_barcodes` **or** (`sku_id` + `piece_seqs`).

---

# 3. The pipeline

**This is the factory flow. It was confirmed by the client and it is not up for reinterpretation.**

```
LEATHER_CUTTING → FUSING → PASTING ─┐
                                     ├─► STORE (merge) ─► LINE_STITCHING
LINING_CUTTING ──────────────────────┘                   → SHELL_STITCHING
                                                          → FINAL_FINISH
                                                          → FINAL_INSPECTION
                                                          → PACKAGE_EXPORT
```

- The two cut paths run **in parallel**. Leather goes on through fusing and pasting; lining goes straight to the store once it is cut.
- **STORE is the merge point** for leather, lining and accessories. It is a **state on the piece**, not a place.
- Nothing reaches LINE_STITCHING until the store has merged and released it.
- `FINAL_INSPECTION` is the quality check. `PACKAGE_EXPORT` is the last stage.

> **Physical drawers are not tracked.** A drawer or bucket is where parts are physically merged, but the drawer itself is not an entity: no drawer scan, no drawer pool, no allocation. The store is states on the garment, and the scan is **employee, then piece — two scans, not three.**

---

# 4. The seven gates

When a batch arrives, each gate runs in turn. **Gate 1 fails the whole request. Gates 2–7 are per piece**, so one bad garment never loses the good ones a manager scanned with it.

| # | Gate | Fails | What it checks |
|---|---|---|---|
| 1 | **ROLE** | **whole request (403)** | May this manager's role log this stage? MD/DM bypass. |
| 2 | **SKILL** | **nothing — a warning** | Is this worker's designation an ordinary one for this stage? |
| 3 | **SEQUENCE** | per piece | Has the piece completed the previous stage on its chain? |
| 4 | **MERGE** | per piece | LINE_STITCHING only: has the store released this garment? |
| 5 | **ASSIGNMENT** | per piece | Does the approved cutting row name **this** cutter? |
| 6 | **REJECTION** | per piece | Is a rejection on this garment waiting for the DM? |
| 7 | **OFFSITE** | per piece | Is the garment out at an outside factory? |

## Gate 1 — role (the only whole-request gate)

The role is wrong for the whole batch, so there is nothing to salvage. There is also a **door gate** in front of it: roles that can never log anything (Viewer, Client, **Store Manager**) get a 403 at the door rather than being led down the path and stopped at the end of it.

> Store Manager is deliberately excluded. Store functions only — the person who owns the store is not the person who owns the line.

## Gate 2 — skill: a WARNING, never a block

The piece is logged **either way**. The result comes back in `skill_warnings[]` with the piece, the worker, their designation and a note.

It is an **audit signal, not permission**. Anyone can be recorded at any stage.

> **The floor is cross-trained**, and the skill table says so: a CUTTER also works FUSING and the lining cut; a TAILOR also pastes. Widen `STAGE_DESIGNATIONS` only when the floor genuinely cross-trains a role — **never to silence a warning somebody found annoying.** A warning that fires on normal work is one nobody reads, and then the gate is worth nothing.

## Gate 3 — sequence (no skipping)

The piece must have completed the previous stage on its chain. The block carries a reason naming **which** stage is missing.

## Gate 4 — merge (LINE_STITCHING only)

The garment must have been **released from the store**. A lined jacket needs leather **and** lining; a leather-only piece (`needs_lining: false`) clears on leather alone.

## Gate 5 — assignment

The approved cutting row names one cutter — that is who the leather was handed to and who the piece rate is paid to. So the **scanned card must be that person**. Without this, any card resolves any approved piece and the wage silently follows the scan instead of the work.

Fix it by having that cutter scan it, or reopen the row and reassign it.

## Gate 6 — an open rejection

A garment somebody has just called defective must not walk on while the DM decides. Otherwise the defect travels down the line, more work is spent on a garment that is going back anyway, and by the time the rejection is approved the piece is three stages past where it was rejected.

## Gate 7 — the garment is not in the building

It was dispatched to an outside factory. Without this gate the system would happily record line-stitching done in-house on a jacket sitting twenty miles away, and nothing would ever contradict it. The vendor's work is logged when the pieces are **booked back in**.

---

# 5. Reading the response

`POST /production/log` returns **201 even when nothing was logged**. Read the buckets, never the status code.

| Field | Meaning |
|---|---|
| `stage` | the stage logged. `"MIXED"` when a PIPELINE batch spanned several. `null` when nothing resolved. |
| `stages`, `stage_by_piece` | the truth for a mixed batch |
| `count_logged`, `logged[]` | what was written |
| `rework[]` | logged as a redo the DM approved — cost stays separable |
| `completed[]` | already past the final stage |
| `not_found[]` | codes that did not resolve |
| `sequence_blocked[]` | gate 3 |
| `merge_blocked[]` | gate 4 |
| `assignment_blocked[]` | gate 5 |
| `rejected_blocked[]` | gate 6 |
| `offsite_blocked[]` | gate 7 |
| `role_blocked[]` | gate 1, per piece, in a mixed batch where other stages were allowed |
| `skill_blocked[]` | kept for old callers — gate 2 no longer blocks |
| **`blocked[]`** | **the one to render**: `{piece, stage, gate, reason}` for every rejection |
| `skill_warnings[]` | gate 2 anomalies, logged anyway |
| `message` | one sentence for the scan screen |
| `store_by_piece` | each piece's store standing after the log |
| `sku_progress` | `{sku_id, stage, total, done, remaining, closed}` for the stage just logged. `closed: true` means stop offering this style for scanning here. `null` on a preview or a mixed batch. |
| `consumption_recorded` | what was decremented |
| `stock_warning` | the cut consumed more than the lot had |
| `cutting_warnings` | notes from the approved cutting rows |
| `preview` | echoes whether this was a dry run |

> **Use `blocked[]`.** The flat lists carry the same pieces with no reason, and exist only so an older frontend keeps working.

## Dry run

`"preview": true` runs every gate and writes **nothing**. Use it to light up the scan screen before the operator commits.

---

# 6. Consumption — only the two cut stages

Only `LEATHER_CUTTING` and `LINING_CUTTING` record material consumption. Stock is decremented **once per batch**, in the same transaction as the events.

> The lot link lives on the **event** — the act of cutting — never on the piece.

## Four ways the quantity is found, in order

1. **Typed** — `consumption.dcm`. Always wins. A manager at the gun is making a deliberate choice.
2. **From the approved cutting row** — per garment. A jacket that took 412 dcm and one that took 370 are both true, and neither is charged the other's number. The batch decrement is their sum.
3. **From the style recipe** — only if you opt in with `use_style_spec: true`.
4. **Nothing** → **422**. The leather ledger and the costing are guarded.

`consumption_source` in the result says which of these was used: `typed`, `cutting_row` or `style_spec`.

## Finding the lot

Send `leather_lot_id` / `lining_lot_id` directly, **or** type `article` + `colour` (+ optional `thickness`) and the router resolves it through the normal lot picker.

| Situation | Result |
|---|---|
| no lot matches the spec | **404** naming the spec — add the delivery first |
| several lots match | **409** listing the candidates — **never a guess** |
| garments approved against different lots | **409** — one scan can spend only one lot |
| garments with different per-piece dcm in their recipes | **422** naming both values — split the batch or type the number |

> `thickness` is **optional and free text** on purpose. It used to be a required dropdown, so a hide whose thickness was not already in the list could not be logged at all — and the floor's answer to that is to pick a wrong value, which is worse than a blank.

If a cut consumes more than the lot had, **the cut is still recorded** — the garment is physically cut — and `stock_warning` reports the shortfall instead of silently driving `on_hand` negative.

---

# 7. `GET /production/piece-state` — the read the scan screen needs

Send `code` (the scanned garment) and `employee_barcode` (the scanned worker), and the server answers whether this scan can go ahead:

| Field | Meaning |
|---|---|
| `current_stage` | where the piece is now |
| `next_stage` | what this scan **would** log — never chosen by hand |
| `ready_to_log` | `true` → POST the log. `false` → show `blockers`. `null` → no worker sent, so the question is unanswered. |
| `blockers[]` | each names its gate and its reason |

It uses **the very same predicates** `POST /log` enforces, so a card the UI opens is a card the log will accept. It also carries the garment's store standing and how many pieces of its SKU are still outstanding at the next stage.

> Call this for **one garment**, not a SKU. A scan screen holds one garment in the operator's hand; a SKU-wide read answers a different question and cannot say anything about that piece.

---

# 8. Correcting a mistake

| Call | Who | Use it when |
|---|---|---|
| `PATCH /production/events/{event_id}/reassign` | MD, DM, HR, Cutting/Lining/Stitching Mgr | the work happened, the **wrong worker** was recorded |
| `DELETE /production/events/{event_id}` | **MD, DM only** | the record should never have existed |

**Reassign** touches neither the stage nor the stock — the garment *was* cut. Only who did it was wrong, and the wage follows automatically because it is **derived** from this row rather than stored against it.

**Delete** removes the record and **returns its stock**. `reason` is required: this is the operation that erases evidence, and why it happened is the only thing that makes it reviewable. It replaces what used to be a DELETE typed straight into the database, which had nowhere to put a reason.

---

# 9. Attendance is a precondition

**A worker must be checked in today** before anything can be logged against them. If the gate did not scan somebody in, the floor cannot log their work.

This is also why the cutting grid returns `present_cutters`: a grid that offered an absent worker would collect the work and then fail at the scan, after the leather was already handed out.

---

# 10. Removed routes

| Route | Status |
|---|---|
| `POST /production/cutting` | **410 Gone.** Pieces mint at breakdown upload, so it has no meaning. |
| `POST /production/scan` | **410 Gone.** Replaced by `POST /production/log`. |

They deliberately answer 410 with a body pointing at `/log`, so a stale frontend fails **loudly** instead of silently.

---

# 11. Notes for backend developers

- **Barcodes are resolved in the router, ids in the service.** Keep it that way — it is what stops the two doors becoming two implementations.
- **The commit belongs to the service.** One transaction per scan: the events, the consumption and the single stock decrement.
- **Gate 2 has no `continue` on purpose.** Adding one turns a warning into a block and changes what the floor is allowed to do.
- **Lazy imports** (`production.service → materials.service`, `→ store`) keep the module graph acyclic. Do not hoist them.
- **The old response buckets are preserved and extended.** A frontend reading only `logged` / `rework` / `not_found` still works.
