# Pass 7 — Repository layer

---

## [SEV: HIGH] [production] [app/modules/production/repository.py:196-228]

The broken wage aggregate — `GROUP BY Piece.style_id` on a column that does not exist. Full
finding in `pass-01` (F139). It belongs to this pass too: it is a repository method that was
never executed, in a repository with no test covering it.

---

## [SEV: MED] [drawers] [app/modules/drawers/] and [materials] [app/modules/materials/]

**Issue:** Two modules have no repository; their services talk to the ORM directly (prior F105, F107).

**Why it's wrong:** `drawers` has no `models.py` and no `repository.py` — the `Drawer` table is
declared in `barcode/models.py:68`, and `drawers/service.py` issues its own `select()`/`commit()`
(self-documented at `:41-47`). `materials/repository.py` exists but the service bypasses it for
five raw commits. The stated layering rule — repository = all DB access + transactions — holds in
`production`, `wages`, `clients`, `attendance`, `employees`, `users`, `barcode`, and is simply
absent in these two.

**Correct behavior:** every module reaches the DB through its own repository.

**Fix sketch:** add `drawers/repository.py` (lookups + `*_nocommit` writes + one `commit()`), and
route the material service's writes through the repository that already exists.

**Primary:** as above. **Risk:** two files and ~10 call sites; not a 2-day change with blockers
open. **Fallback:** D1/D2 — behaviour is correct today, this is structural debt that makes the
transaction findings in `pass-06` hard to fix cleanly.

---

## [SEV: MED] [wages] [app/modules/wages/repository.py:382-430]

**Issue:** A complete, correct single-transaction API exists and has zero callers.

**Why it's wrong:** `create_run_nocommit`, `add_lines_nocommit`, `persist_breakdown_nocommit`,
`clear_lines_nocommit`, and the `commit`/`rollback` seam are all implemented. A repo-wide grep for
`nocommit|repo.commit|repo.rollback` finds no wages call site. Forty-eight lines of reviewed,
tested-looking code that does nothing, sitting next to the committing variants that cause the
money bug. A maintainer reading the file would reasonably conclude the single-transaction path is
in use.

**Correct behavior:** either wire it up (`pass-02`) or delete it.

**Fix sketch:** wire it up — that *is* the fix for the four-commit finding.

**Primary:** wire up. **Risk:** none; it is what the code was written for. **Fallback:** if the
window closes, add a `# DEAD — see AUDIT F-` header so nobody assumes it is live.

---

## [SEV: MED] [barcode] [app/modules/barcode/repository.py:53-76]

**Issue:** Code minting is `ORDER BY code DESC LIMIT 1` with no DB sequence (prior F16/F79/F99).

**Why it's wrong:** `_next_code` reads the current maximum and increments in Python. The unique
index is the only guard against a collision (`:8-14`, `:61-62`), and there is no retry — so two
concurrent employee creations race and one 500s. The scan also grows with the barcode table: it
is an index scan today, but the ordering is lexical on a string code, so a format change
(e.g. crossing a digit-width boundary) silently returns the wrong maximum.

**Correct behavior:** allocate from a DB sequence, or catch the unique violation and retry.

**Fix sketch:** wrap the insert in a savepoint; on `IntegrityError`, re-read and retry once.

**Primary:** retry-on-conflict (works on both Postgres and SQLite). **Risk:** none.
**Fallback:** D1 — employee creation is low-frequency and HR-driven, so the race is unlikely
before Aug 2. The same pattern recurs in `premint.py:97,163` for drawer codes (`pass-01`).

---

## [SEV: MED] [all] [rollback placement]

**Issue:** Four modules never roll back a failed transaction (prior F74, still open).

**Why it's wrong:** The only explicit rollbacks in the scoped code are
`attendance/service.py:311` (the `IntegrityError` retry — correct) and
`wages/repository.py:430` (dead). Everywhere else, a failure mid-write leaves the session dirty
and relies on `get_db`'s teardown. FastAPI's dependency teardown does close the session, so this
is not a leak — but a caught-and-handled exception inside a service continues with an
unrolled-back session, and the next write joins a poisoned transaction.

**Fix sketch:** `try/except: await self.repo.rollback(); raise` around each service's write
sequence.

**Primary:** as above. **Risk:** none. **Fallback:** D1 for `wages` and `imports` (where money and
order data are at stake); D2 elsewhere.

---

## Session hygiene — verified good

- No connection leaks found: `get_db` (`core/database.py:112`) uses `async with`, and every
  router depends on it rather than constructing sessions.
- No missing `async` on any repository method in the scoped modules — every DB call is awaited.
  The one non-async wrapper is in the **service** layer (`clients/service.py:52-53`, prior F86).
- No duplicate queries within a repository, though `production/repository.py:242-254`
  (`piece_ids_done_at_op`, the batched form) is used only by the read path while the write path
  uses the per-piece `has_event_at_op` — see `pass-10`.
