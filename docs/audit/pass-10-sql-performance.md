# Pass 10 — SQL performance

---

## [SEV: HIGH] [analytics] [app/modules/analytics/service.py:373-382, 407-427]

**Issue:** Two alert endpoints are unbounded N+1 loops over the whole table.

**Why it's wrong:** `stage_spread_alerts` loads **every** `Style` (`:373`) then issues one query
per style (`:375-382`). `freight_risk` loads **every** `ClientOrder` (`:407`) then **two** queries
per order (`:415-427`). At ten factories these are the first endpoints to take the API down, and
they are on the dashboard — polled, not user-initiated.

**Correct behavior:** one grouped query per endpoint.

**Fix sketch** (stage spread — one query replaces 1 + N):
```sql
SELECT s.id, s.name, o.code, COUNT(DISTINCT p.id)
FROM style s
JOIN sku      ON sku.style_id = s.id
JOIN piece p  ON p.sku_id = sku.id
LEFT JOIN LATERAL (
    SELECT operation_id FROM production_event
    WHERE piece_id = p.id ORDER BY work_date DESC, created_at DESC LIMIT 1
) last ON TRUE
JOIN operation o ON o.id = last.operation_id
GROUP BY s.id, s.name, o.code;
```
In SQLAlchemy: one `select()` with `func.count(distinct(Piece.id))`, grouped by
`Style.id, Operation.code`, replacing the Python loop entirely.

**Primary:** the grouped query above. **Risk:** `LATERAL` is Postgres-specific — the ORM form with
a window function (`row_number() OVER (PARTITION BY piece_id ORDER BY work_date DESC)`) is
portable and equivalent. **Fallback (Aug 2):** add `LIMIT 200` to the outer `Style`/`ClientOrder`
load and a note in the response that the alert list is truncated. Two lines, removes the outage.

---

## [SEV: HIGH] [production] [app/modules/production/service.py:203-210, 306, 154, 170, 342]

**Issue:** `POST /production/log` issues 5–7 queries **per piece**, and the batched form already exists.

**Why it's wrong:** The module's own docstring describes 40-piece trays (`router.py:14-16`). For
each piece in the batch:

| Location | Query |
|---|---|
| `service.py:203-210` | `db.get(Piece, pid)` in a loop — one SELECT per piece |
| `service.py:99` → `repository.py:94-106` | `completed_stage_codes(piece.id)` — a join per piece |
| `service.py:306` → `repository.py:85-92` | `has_event_at_op` — the rework check |
| `service.py:154` → `repository.py:52-59` | `get_operation_by_code(prev.value)` — re-looked-up per piece even though `op_by_stage` is memoised at `:231-239` |
| `service.py:170` → `drawers/service.py:174-176` | `drawer_for_piece` — the merge gate |
| `service.py:342` → `drawers/service.py:179-194` | 2 more at `PACKAGE_EXPORT` |

Plus the router's own per-item resolution before the service is entered: `resolve_piece_id` per
barcode (`router.py:137-139`) and `get_piece_by_sku_seq` per seq (`:142-150`).

A 40-piece tray therefore costs roughly **250–300 round trips**. The batched replacement for the
worst one is already written and unused: `ProductionRepository.piece_ids_done_at_op`
(`repository.py:242-254`), whose docstring says *"Batch form of has_event_at_op — ONE query, no
N+1 on the scan screen"*. It is called only from the read path (`service.py:399,405`).

**Fix sketch:**
```python
pieces = (await self.db.execute(select(Piece).where(Piece.id.in_(pids)))).scalars().all()
done    = await self.repo.piece_ids_done_at_op(pids, op.id)      # already exists
drawers = await drawer_svc.drawers_for_pieces(pids)              # add batch form
```

**Primary:** batch the three hot lookups; `piece_ids_done_at_op` is free. **Risk:** the gate loop
must be restructured to consult pre-fetched maps — a real change to the most safety-critical
function in the app. Do it with the gate tests green, not before. **Fallback (Aug 2):** ship the
one-line `select(...in_(pids))` for the piece fetch (`:203-210`) only — removes 40 of the 250
round trips with no logic change — and defer the rest to D1.

---

## [SEV: MED] [materials] [app/modules/materials/service.py:176-184]

**Issue:** The stock endpoint calls `active_reserved(lot.id)` inside the lot loop.

**Why it's wrong:** `find_lots` (`repository.py:43-61`) has **no LIMIT**, and the loop issues one
reservation query per matching lot. A category-wide stock check on a mature database is
one unbounded scan plus one query per row.

**Fix sketch:**
```python
select(MaterialReservation.lot_id, func.sum(MaterialReservation.qty))
  .where(MaterialReservation.lot_id.in_(lot_ids), MaterialReservation.status == "active")
  .group_by(MaterialReservation.lot_id)
```
One query for all lots, joined in Python.

**Primary:** the grouped query. **Risk:** none. **Fallback:** D1 — plus a `LIMIT` on `find_lots`
regardless.

---

## [SEV: MED] [production] [app/modules/production/models.py:118-119]

**Issue:** Zero eager loading anywhere in production, barcode or drawers (prior F98).

**Why it's wrong:** No `selectinload`, `joinedload` or `lazy=` appears in the three modules.
`ProductionEvent.operation` and `.piece` are default-lazy relationships accessed from an async
session — which either raises `MissingGreenlet` or triggers an implicit IO per attribute.
`clients/repository.py:40-45` shows the project knows how to do this correctly with
`selectinload`; the pattern just was not applied to the hot module.

**Fix sketch:** `selectinload(ProductionEvent.operation)` on `list_events`.

**Primary:** as above, alongside the `response_model` fix in `pass-09`. **Risk:** none.
**Fallback:** D1.

---

## Indexing

**Missing composite index for the hottest query.** `has_event_at_op` / `piece_ids_done_at_op`
filter on `(piece_id, operation_id)`; `piece_counts_by_employee_style_op` filters on `work_date`
and groups by `(employee_id, operation_id, work_date)`. `production_event` carries single-column
indexes only.

**Fix sketch:**
```sql
CREATE INDEX ix_prod_event_piece_op   ON production_event (piece_id, operation_id);
CREATE INDEX ix_prod_event_emp_date   ON production_event (employee_id, work_date);
```

**Primary:** both indexes in one migration. **Risk:** index build on a large table — negligible at
current volume, do it before the table grows. **Fallback:** none needed; this is the cheapest
performance win in the audit and should ship with the F139 fix, since it serves the same query.

**Present and correct:** every FK in the scoped models is indexed, `piece.code`, `sku.code`,
`style.code` and `client_order.order_number` all carry unique indexes, and `uq_att_emp_day`
serves the attendance lookup directly.
