# Pass 6 — Transactions, atomicity, concurrency, idempotency

The project rule is **commit belongs in the repository**. Actual placement:

| Module | Commits in repo | Commits/flushes outside repo |
|---|---|---|
| production | `repository.py:124,130,157,163` | `service.py:355` (via repo seam — compliant) |
| barcode | `repository.py:133` | `service.py:216` (`db.flush()` **direct**), `:229`, `:244` (via seam) |
| drawers | — (**no repository exists**) | `service.py:47` (`db.commit()` **direct**), `:108`, `:165`, plus `db.refresh` at `:109`, `:166` |
| wages | `repository.py:129,150,178,223,239,245,252,266,270,276` | none |
| materials | — | five raw commits in the service (prior F72) |
| imports | — | `load_to_db.py:293` (sync session), `premint.py:129,142` (flush ×2 per piece) |

---

## [SEV: HIGH] [wages] [app/modules/wages/repository.py:252,266,239,270]

Covered in full in `pass-02` — one payroll run issues four commits, and the correct
single-transaction implementation at `repository.py:382-430` has zero callers. Repeated here
because it is the most consequential transaction defect in the codebase: it is the mechanism by
which a fortnight can be paid twice.

---

## [SEV: HIGH] [materials] [app/modules/materials/service.py:266-269; repository.py:32-33]

**Issue:** Stock movement is an unlocked read-modify-write.

**Why it's wrong:** `get_lot` is a plain `db.get` with no `with_for_update`. Both the decrement
(`service.py:269`) and the receive (`service.py:218`) read `on_hand`, compute in Python, and write
back. Two concurrent cut scans against the same lot each read the same value and each write
`on_hand - d` — one decrement is lost. On a shop floor with two cutting screens open on the same
hide, this is not a theoretical race.

Compounded by the absence of any floor check or DB `CHECK` constraint, so the lost update is also
invisible: `on_hand` simply drifts.

**Correct behavior:** either lock the row for the duration, or make the update atomic in SQL.

**Fix sketch:**
```python
await self.db.execute(
    update(MaterialLot).where(MaterialLot.id == lot_id, MaterialLot.on_hand >= d)
    .values(on_hand=MaterialLot.on_hand - d))
# rowcount == 0  ->  422 insufficient stock
```

**Primary:** the atomic conditional `UPDATE` above — it fixes the race **and** the negative-stock
finding in one statement, with no lock held across the request. **Risk:** the service currently
returns the post-decrement available figure; it needs a follow-up read. **Fallback:**
`with_for_update()` on the lot read — simpler, but serialises concurrent cuts on the same lot.

---

## [SEV: MED] [imports] [app/modules/imports/router.py:89-97]

**Issue:** The import opens a second, synchronous session inside an async request (prior F73).

**Why it's wrong:** `_do_commit_into_order` opens its own `SessionLocal`, decoupled from the
request's `AsyncSession`. The two are separate transactions: a failure after the sync session
commits leaves the order written while the request reports an error. `:94-96` closes without an
explicit rollback on the failure path.

**Correct behavior:** one transaction per request, or an explicitly documented two-phase boundary
with compensation.

**Fix sketch:** wrap the sync work in `try/except: session.rollback(); raise` at minimum.

**Primary:** add the rollback (D0 — two lines, prevents a half-written order). **Risk:** none.
**Fallback:** none; the full async port is a Phase-2 change, the rollback is not.

---

## [SEV: MED] [drawers] [app/modules/drawers/service.py:47]

**Issue:** The drawers service commits the shared session unconditionally (prior F71).

**Why it's wrong:** `repo_commit()` is `await self.db.commit()` on the request session. When
`production/service.py:340-342` calls `release_nocommit` during a `PACKAGE_EXPORT` batch, the
drawer module is operating inside production's transaction — but `store_scan` (`:108`) and
`transition` (`:165`) commit that same session outright. The module has no repository, so there
is no seam at which ownership could be expressed. `drawers/service.py:41-47` documents this
honestly as debt.

**Correct behavior:** the caller that opened the unit of work commits it.

**Fix sketch:** add `drawers/repository.py` with `*_nocommit` writes plus a single `commit()`
seam, mirroring `production/repository.py`.

**Primary:** as above. **Risk:** a new file and three call-site changes — feasible but not
inside two days alongside the blockers. **Fallback (Aug 2):** leave it; the current calls are
correct because production commits last. Record it, and never call a drawers write from inside
another module's transaction without re-reading this.

---

## Idempotency

- **Attendance check-in: correct.** `uq_att_emp_day` plus the `IntegrityError` catch at
  `service.py:309-316` makes a re-tap a true no-op.
- **`mark_arrived`: correct** (`materials/service.py:295-306`).
- **Premint: partially.** The top-up logic (`premint.py:105-113`) is genuinely idempotent for the
  incremental case; the `replace=True` path is broken (pass-01).
- **`decrement_for_cut_nocommit`: not idempotent, and the file contradicts itself.** The module
  header at `materials/service.py:16` claims idempotency; the function docstring at `:259-261`
  correctly disclaims it ("Idempotency is the CALLER's responsibility"). The caller does handle it
  via `fresh_cut` (`production/service.py:346`), so behaviour is right — the header comment is
  wrong and should be deleted.

## Celery

**No Celery task touches any scoped module.** `core/celery.py:24-27` autodiscovers `app.modules.bom`
and `app.modules.production`; `production` has no `tasks.py`, and the only `tasks.py` in the repo
is `bom/tasks.py` (out of scope). So the brief's "Celery retry-safety" item has no in-scope
surface, and Phase-3's `task_always_eager` requirement is unsatisfiable here.

Two structural notes that do land in scope:

- `app/core/beat_schedule.py` is **imported by nothing** (prior F76) — grep across `app/` finds the
  name only in the file itself. Nothing on that schedule ever runs unless a worker is started
  with `-A app.core.beat_schedule`.
- `main.py:163-166` spawns in-process sweepers **per replica** with no lock, while
  `beat_schedule.py:10-12` schedules one of the same jobs every 15 minutes. With N replicas plus
  beat, the same escalation runs N+1 times (prior F77/F131). `celery.py:17-18` sets
  `task_acks_late=True` + `task_reject_on_worker_lost=True`, which redelivers on worker loss —
  safe only for idempotent bodies.

**Primary:** move the sweepers to beat-only with a Redis lock. **Risk:** infrastructure change.
**Fallback (Aug 2):** run exactly one API replica, or set `notification_sweeper_enabled=False`
(`config.py:164`) on all but one. Configuration, not code.
