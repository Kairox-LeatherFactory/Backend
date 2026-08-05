# Pass 1 — Business logic / manufacturing flow

Scope: `production`, `barcode`, `drawers` (+ `imports/premint.py`, the mint path).
Prior findings re-verified in `delta-register.md`. This pass records **new** findings, F139+.

---

## [SEV: BLOCKER] [production] [app/modules/production/repository.py:213,222]

**Issue:** The piece-rate wage aggregate groups and orders by `Piece.style_id`, a column that does not exist.

**Why it's wrong:** `piece_counts_by_employee_style_op` is the sole source of piece-rate wage
rows — `production/service.py:442` → `wages/service.py:433-443`. The `SELECT` correctly reads
`SKU.style_id` (`repository.py:205`), but the `GROUP BY` (`:213`) and `ORDER BY` (`:222`) name
`Piece.style_id`. `Piece` has no `style_id` column (`production/models.py:71-94`) and is not in
the `FROM`. Constructing the statement raises `AttributeError` before any SQL is emitted, so
**every piece-rate payroll run 500s**. Reproduced in the baseline suite:
`tests/test_wages.py`, `tests/test_rbac.py`, `tests/test_service_smoke.py` all fail with
`AttributeError: type object 'Piece' has no attribute 'style_id'` at `repository.py:213`.

This is a **regression** — the prior audit recorded this query as merely redundant (F81).

**Correct behavior:** Group and order by the same expression the `SELECT` projects, `SKU.style_id`.

**Fix sketch:**
```python
.group_by(ProductionEvent.employee_id, SKU.style_id, ProductionEvent.operation_id, ProductionEvent.work_date)
.order_by(ProductionEvent.employee_id, SKU.style_id, ProductionEvent.operation_id, ProductionEvent.work_date)
```

**Primary:** the two-line swap above. **Risk:** none — it restores the documented intent and the
`SELECT` already proves which column is meant. **Fallback:** none needed; do not ship without it.

---

## [SEV: BLOCKER] [production] [app/modules/production/router.py:71,98,110]

**Issue:** Three GET endpoints pass a `client_scope` keyword the service methods do not accept.

**Why it's wrong:** `router.py:71,98,110` call `list_sku_options(..., client_scope=scope)`,
`style_progress(style_id, client_scope=scope)` and `list_pieces_for_sku(..., client_scope=scope)`.
The signatures at `production/service.py:438`, `:435` and `:376-378` declare no such parameter
and no `**kwargs`. Every call raises `TypeError` → 500. `GET /production/sku-options`,
`/production/styles/{id}/progress` and `/production/pieces` are dead.

The tenancy intent is real and correct — the router computed the scope — but it never reaches a
query, so these three endpoints would also be **untenanted** the moment the TypeError is fixed
by deleting the kwarg. Fix the signature, not the call site.

**Correct behavior:** The service accepts `client_scope` and applies it as a predicate through the
`SKU → Style → ClientOrder.client_id` join, matching `analytics/service.py:169-170`.

**Fix sketch:**
```python
async def style_progress(self, style_id, *, client_scope: uuid.UUID | None = None):
    ...  # join ClientOrder; if client_scope is not None: stmt = stmt.where(ClientOrder.client_id == client_scope)
```

**Primary:** add the parameter and the predicate to all three. **Risk:** the join adds a table to
three read queries; measure on the largest order. **Fallback (2-day cut):** add the parameter and
have it 403 for `CLIENT` tokens rather than filtering — closes the 500 and the exposure, defers
the join.

---

## [SEV: HIGH] [imports] [app/modules/imports/premint.py:36-41; app/modules/imports/load_to_db.py:243]

**Issue:** Re-committing an order raises an FK integrity error instead of the advertised idempotent replace.

**Why it's wrong:** `premint.py:36-41` states that deleting a SKU cascades its pieces. It does not:
`piece.sku_id` is created with no `ondelete` (`20260712_1000_piece_tracking.py`), and `SKU`
declares no `pieces` relationship (`clients/models.py:126-146`) — only `order_lines` (`:146`).
The replace path deletes SKUs at `load_to_db.py:243`, and the guard above it
(`load_to_db.py:225-236`) only checks for `ProductionEvent`, not `Piece`. Since the pre-mint
inversion moved minting to **upload** time, every previously-committed order has pieces even with
zero production. `POST /imports/commit` is documented as idempotent (`router.py:126`); it 500s.

**Correct behavior:** Either the guard counts `Piece` as production history and refuses the
replace with a 409, or the replace deletes pieces (and their barcode/drawer rows) first.

**Fix sketch:**
```python
# load_to_db.py, in the existing guard block
if db.scalar(select(func.count(Piece.id)).join(SKU).where(SKU.style_id.in_(style_ids))):
    raise ValueError("Order already has minted pieces — replace refused.")
```

**Primary:** refuse with 409 (data-safe, one query). **Risk:** re-uploading a corrected breakdown
now needs an explicit delete path the UI does not have. **Fallback:** ship the 409 for Aug 2 and
add a DM-only `force_replace` that cascades pieces + barcodes + drawers post-deploy.

---

## [SEV: MED] [production] [app/modules/production/service.py:306-316]

**Issue:** A rework pass bypasses both the sequence gate and the merge gate.

**Why it's wrong:** `is_rework = await self.repo.has_event_at_op(piece.id, op.id)` at `:306`; when
truthy, control skips to `:319-320` and neither `_sequence_ok` (`:308-311`) nor `_merge_ok`
(`:313-316`) runs. Rework is deliberately permissive, but this means **any** piece that has ever
been logged at an operation can be re-logged there regardless of drawer state — a lined jacket can
be re-line-stitched while its drawer sits at `HOLDING_LEATHER`.

**Correct behavior:** Rework should bypass the *sequence* gate (that is the point) but still
respect the *merge* gate, which asserts physical completeness, not order.

**Fix sketch:** move the `_merge_ok` check above the `is_rework` branch so it runs unconditionally
for `LINE_STITCHING`.

**Primary:** as above. **Risk:** a genuine rework on a recycled drawer (already back to `WAITING`)
would now block — needs a `PACKAGE_EXPORT`-completed exemption. **Fallback:** D2; log a warning
rather than block, so the floor is visible without new stoppages.

---

## [SEV: MED] [production] [app/modules/production/service.py:330,348]

**Issue:** `consumption.dcm` has two readings, and the code uses both.

**Why it's wrong:** The event row stores the un-multiplied figure (`ev_qty = consumption_qty`,
`:330`) while stock decrements `consumption_qty × fresh_cut` (`:348`). Per-event and total agree
only if the caller meant *per piece*. Nothing in `production/schemas.py` says which it is, so a
cutting manager entering a tray total silently multiplies the decrement by the tray size.

**Correct behavior:** name the field for its unit and validate it.

**Fix sketch:** rename to `dcm_per_piece` in `Consumption` and document it in the field
description; reject values that exceed a plausible per-piece ceiling.

**Primary:** rename + description (contract-only, no logic change). **Risk:** a frontend field-name
change inside the 2-day window. **Fallback:** keep the name, add the description and a sanity
bound; D1.

---

## [SEV: MED] [imports] [app/modules/imports/premint.py:97,163]

**Issue:** Drawer codes are allocated from an in-memory counter snapshotted once per import.

**Why it's wrong:** `drawer_seq` is read at `:97` and incremented in Python at `:163`. Two imports
running concurrently read the same base and collide on `uq_drawer_code` (`20260730_barcode.py:152`),
failing the whole transaction. Same read-then-insert shape the prior audit flagged for barcodes
(F16/F79/F99), in a second place.

**Correct behavior:** allocate from a DB sequence, or retry on the unique violation.

**Fix sketch:** wrap the per-drawer insert in a savepoint and retry once on `IntegrityError`,
re-reading `max(seq)`.

**Primary:** retry-on-conflict. **Risk:** none material at current volume (imports are DM-driven
and rare). **Fallback:** D2 — document "one import at a time" as an operating rule for Aug 2.

---

## [SEV: MED] [drawers] [app/modules/drawers/service.py:106]

**Issue:** `store_scan` has a branch that assigns `WAITING`, a state that means "empty".

**Why it's wrong:** `:99-106` sets the drawer state from the two `_in` flags; the final `else` at
`:106` yields `WAITING`. It is unreachable today because the branch above always sets one flag
`True` first — but it encodes "a scan can empty a drawer", which is not a transition the state
machine has. A future edit to the flag logic makes it live silently.

**Correct behavior:** the fallthrough should be unreachable-by-construction or raise.

**Fix sketch:** replace the final `else` with `raise AssertionError("store_scan cannot empty a drawer")`.

**Primary:** as above. **Risk:** none. **Fallback:** D2.

---

## [SEV: MED] [barcode] [app/modules/barcode/service.py:222-229]

**Issue:** Reissuing a card for an employee who has none silently mints a first card.

**Why it's wrong:** `reissue_employee_barcode` treats a missing card as `old = None` (`:222-223`)
and proceeds to mint. Its sibling `deactivate_employee_barcode` correctly 404s in the same
situation (`:237-240`). "Reissue" is an audited lifecycle event meaning *this worker lost their
card*; performing it on someone who never had one writes an `EMPLOYEE_BARCODE_REISSUE` audit row
that misdescribes what happened.

**Correct behavior:** 404 when there is no active card, matching deactivate.

**Fix sketch:** `if not old: raise HTTPException(404, "No active card to reissue.")`

**Primary:** as above. **Risk:** HR loses the accidental "mint first card" path — but that is what
employee creation is for (`employees/service.py:110`). **Fallback:** D1.

---

## [SEV: LOW] [production] [app/modules/production/repository.py:108-133; service.py:366-373; router.py:111]

**Issue:** Three pieces of dead code on the mint/log path read as live.

**Why it's wrong:** `mint_pieces` (`repository.py:108-133`) is the pre-inversion mint with a
*different* code format (`f"{prefix}-{seq:03d}"`, `:120`) than the live one
(`premint.py:122`) and has no callers; it is also the only repository method that both flushes
and commits mid-function (`:124`, `:130`). `_empty_result` (`service.py:366-373`) has no callers.
`router.py:111` is unreachable after the `return` at `:109-110`. A maintainer reading
`mint_pieces` would conclude pieces are minted at cutting — the exact confusion this audit's
own brief carried in.

**Correct behavior:** delete, or move behind an explicit `# DEAD:` marker with a removal ticket.

**Fix sketch:** delete all three; the 410 stub at `router.py:171-176` already documents the
retired path.

**Primary:** delete. **Risk:** none — no callers. **Fallback:** D2.

---

## Verified-good (no finding)

- The four gates are correctly tiered: role is whole-request (`service.py:241-243`), skill,
  sequence and merge are per-piece (`:264-273`, `:308-311`, `:313-316`) — one bad piece does not
  lose the batch, as designed.
- Stock decrements **once per batch**, not per piece (`service.py:344-353`), and only for
  first-time cuts — F03 is genuinely closed.
- The drawer completeness rule correctly distinguishes lined from unlined pieces
  (`drawers/service.py:96`), and `RECEIVED`→`SENDED`→`WAITING` guards are all present
  (`:136-155`, `:179-194`).
- Zero raw SQL / `text()` in the three modules — no SQLi surface here.
