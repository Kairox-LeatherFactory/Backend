# Pass 2 — Money paths (`wages`)

Priority pass. Prior money findings F20–F31, F102, F114 re-verified in `delta-register.md`.

**Standing fact for this pass:** piece-rate payroll cannot execute at all today — see
**F139** in `pass-01` (`production/repository.py:213`). Every finding below sits *behind* that
one; fixing F139 is what makes the rest reachable.

---

## [SEV: BLOCKER] [wages] [app/modules/wages/service.py:572]

**Issue:** `compute_run` calls a method that does not exist — after the run is committed and closed.

**Why it's wrong:** `_name_unrated` calls `self.employees.names_for(list(salary_missing))`.
`EmployeeService` defines `list_all`, `_unique_name`, `create`, `update`, `get`,
`monthly_employees` — there is no `names_for` anywhere in `app/modules/` (grep confirms one hit,
the call site itself). The branch fires whenever an active MONTHLY employee has
`monthly_salary IS NULL` (`service.py:479-481`) — a routine data state, not an edge case.

The ordering is what makes it a blocker. Lines are added and committed at `service.py:501`
(`repository.py:266`), the breakdown at `:502`, and `close_run` at `:503` (`repository.py:270`)
— **all before** `_name_unrated` runs. So the sequence is: money committed → run closed →
`AttributeError` → 500. `compute_run`'s failure handler then calls `delete_run`
(`service.py:392` → `repository.py:273-276`) against a **closed** run whose `wage_line` children
have no `ondelete` (`models.py:109-111`, unlike `wage_line_detail` at `:80-81` which does cascade).
The delete fails on the FK, and the caller is left with a closed, fully-populated,
apparently-paid run plus a 500.

**Correct behavior:** the display-name lookup must not be able to fail the run, and must not run
after the money is frozen.

**Fix sketch:**
```python
names = {e.id: e.name for e in await self.employees.list_all(active_only=False)}
# ...and move _name_unrated above the add_lines/close_run block
```

**Primary:** use the existing `list_all` and hoist the call above `:501`. **Risk:** one extra
full-roster read per run — trivial at factory scale. **Fallback:** wrap `_name_unrated` in
`try/except` returning ids instead of names; strictly worse (silently degrades the report) but
one line, if the window closes.

---

## [SEV: HIGH] [wages] [app/modules/wages/service.py:318,348 vs router.py:163-180]

**Issue:** The closed-run guard is unreachable, and it has made recompute permanently impossible.

**Why it's wrong:** F20's fix added `confirm_closed: bool = False` (`service.py:318`) and the
409 at `:348`. But `confirm_closed` appears in **neither** `wages/router.py` nor
`wages/schemas.py` — `POST /wages/runs/{run_id}/recompute` takes no body and no query parameter,
and calls `recompute_run(run_id, user_name=user.name)` (`router.py:180`). Since `_populate_run`
always ends in `close_run` (`service.py:503`), **every persisted run is CLOSED**, so the endpoint
returns 409 unconditionally. The recompute feature — including the audit-trail machinery at
`repository.py:245` (`stamp_recompute`) — is dead via the API.

This is a regression in capability, not in safety: the money is now over-protected. But it means
a payroll error found before payout has no in-app remedy.

**Correct behavior:** the confirmation must be expressible over HTTP, and gated to MD.

**Fix sketch:**
```python
class RecomputeRequest(BaseModel): confirm_closed: bool = False
# router: body: RecomputeRequest = Body(default_factory=RecomputeRequest)
#         require_roles(UserRole.MANAGING_DIRECTOR) for confirm_closed=True
```

**Primary:** as above. **Risk:** re-opens the exact door F20 closed — must be MD-only and must
write the audit row. **Fallback (2-day cut):** leave recompute 409-ing and document
"payroll corrections are made by closing the run and issuing an adjustment run". Safe, ugly,
zero code.

---

## [SEV: HIGH] [wages] [app/modules/wages/repository.py:252,266,239,270 + :382-430]

**Issue:** One payroll run issues four separate commits; the single-transaction implementation exists and is dead.

**Why it's wrong:** `compute_run` commits at `create_run` (`:252`), `add_lines` (`:266`),
`persist_breakdown` (`:239`) and `close_run` (`:270`). A crash between any two leaves committed
money at `status=OPEN` — invisible to `overlapping_closed_run`, which the prior audit flagged as
F22 and which remains open. The block at `repository.py:382-430` — `create_run_nocommit`,
`add_lines_nocommit`, `persist_breakdown_nocommit`, `clear_lines_nocommit`, plus a `commit`/
`rollback` seam — is a complete, correct single-transaction implementation with **zero callers**
(repo-wide grep for `nocommit|repo.commit|repo.rollback` returns no wages call site).

**Correct behavior:** one run, one transaction — exactly what the dead block implements.

**Fix sketch:** switch `compute_run` to the `*_nocommit` variants and issue one
`await self.repo.commit()` after `close_run`; `except: await self.repo.rollback()`.

**Primary:** wire up the code that is already written and reviewed. **Risk:** a longer-held
transaction on a large run; acceptable at one factory, worth measuring at ten. **Fallback:** none
that is safe — this is the finding that lets a fortnight be paid twice.

---

## [SEV: MED] [wages] [app/modules/wages/service.py:372-376]

**Issue:** The recompute restore path violates the constraint that was just added to protect it.

**Why it's wrong:** On failure, `recompute_run` re-adds the snapshot lines. If `add_lines` already
committed new lines for the same `(run_id, employee_id)` — and it commits at `repository.py:266`
— the restore hits `uq_wage_line_run_emp` (`models.py:107`) and raises, replacing a recoverable
error with an unrecoverable one plus a half-written run.

**Correct behavior:** restore must clear before it re-adds, inside the same transaction.

**Fix sketch:** call `clear_lines_nocommit` before re-adding, and perform both under the single
transaction from the finding above.

**Primary:** fold into the single-transaction fix. **Risk:** none beyond that fix.
**Fallback:** unreachable today (recompute always 409s) — D1, but it must land *with* the
recompute fix, never after it.

---

## [SEV: MED] [wages] [app/modules/wages/service.py:490-495]

**Issue:** Attendance-scaled monthly pay is gated on a setting that does not exist.

**Why it's wrong:** `if getattr(settings, "monthly_wage_requires_attendance", False):` — the
attribute is not a field on `Settings` (it appears nowhere in `app/core/config.py`), so the
`getattr` default makes the branch permanently dead. And because `Settings` is configured
`extra="ignore"` (`config.py:46`), putting `MONTHLY_WAGE_REQUIRES_ATTENDANCE=true` in `.env`
will **not** turn it on either — it is silently discarded. A policy decision (does a monthly
worker who was absent half the period get full pay?) is encoded as unreachable code.

**Correct behavior:** either declare the field and honour it, or delete the branch.

**Fix sketch:** add `monthly_wage_requires_attendance: bool = False` to `Settings` and read it
as a normal attribute.

**Primary:** declare the field, default `False` (preserves today's behaviour exactly).
**Risk:** none at default. **Fallback:** delete the branch and record the policy in
`wages/__init__.py`; do not leave it looking configurable when it is not.

---

## [SEV: MED] [wages] [app/modules/wages/repository.py:195-202]

**Issue:** Run-overlap detection orders by an enum's string value.

**Why it's wrong:** `overlapping_closed_run` matches both OPEN and CLOSED and orders by
`WageRun.status.desc()`. On a string enum that ordering is lexical — `"open" > "closed"` — so an
OPEN clash is reported in preference to a CLOSED one. The docstring says this is intentional, and
today it happens to produce the desired precedence, but it is precedence by alphabet: renaming a
status value silently reverses which conflict the DM is warned about.

**Correct behavior:** order by an explicit `case()` expression that encodes the intended priority.

**Fix sketch:**
```python
.order_by(case((WageRun.status == RunStatus.OPEN, 0), else_=1))
```

**Primary:** as above. **Risk:** none. **Fallback:** D2, with a comment naming the dependency.

---

## Verified-good (no finding)

- **PIECE_RATE / MONTHLY exclusivity holds.** The two branches skip on `is not` tests over the
  same `Employee.wage_type` (`service.py:433-435`, `:467-468`), so an employee can satisfy at
  most one; `uq_wage_line_run_emp` (`models.py:107`) enforces it at the DB as well. F21/F102 are
  genuinely closed — the constraint is in the model **and** in `20260731_wage_line_uniq.py:30`.
- **Date-effective rates are correct.** `repository.py:41-52` takes the latest
  `effective_from <= on`, and the cache key includes `work_date` (`service.py:436-443`), so a
  mid-period rate change prices each day at the rate effective that day. This is the single
  best-implemented thing in the module.
- **Proration math is right and pure.** `proration.py:66-68` splits across month boundaries by
  actual days-in-month. Testable as-is.

**One caveat on the constraint:** `20260731_wage_line_uniq.py:20` gates the duplicate-collapse
step on `bind.dialect.name == "postgresql"`. On any non-PG target carrying pre-existing
duplicates, `create_unique_constraint` at `:29-30` fails. Postgres is the production target, so
this is D2 — but it is why the constraint arrives via `create_all` rather than the migration in
the debug configuration (see `pass-05`).
