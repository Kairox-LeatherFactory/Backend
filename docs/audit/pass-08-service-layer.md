# Pass 8 — Service layer

---

## [SEV: HIGH] [production] [app/modules/production/service.py:107-173]

**Issue:** The four gates are methods on a service, not extractable predicates (prior F128).

**Why it's wrong:** `_assert_role` (`:107-127`), `_skill_ok` (`:129-146`), `_sequence_ok`
(`:148-160`) and `_merge_ok` (`:162-173`) are the highest-value business rules in the system and
each is bound to `self.repo` and `self.db`. Testing "may a CUTTING_MANAGER log SHELL_STITCHING?"
requires a database session. The two rules that *were* extracted — `wages/proration.py` and
`attendance/geofence.py` — are the two that are trivially testable, which proves the pattern works
and just was not applied here.

`_skill_ok` also fails open twice (`:135` unknown/multi-stage designation, `:137-138`
uncatalogued designation), which is deliberate (HR backfills) but untested and undocumented at
the API boundary.

**Correct behavior:** the decision is a pure function of (role, stage, designation, completed
stages, drawer state); the service supplies the data.

**Fix sketch:** move the predicates to `production/rules.py` taking plain values, and have the
service fetch-then-call.

**Primary:** extract. **Risk:** a mechanical refactor across ~70 lines, during a blocker fix —
not both in two days. **Fallback (Aug 2):** test them through the service with a session, which
is what the existing `tests/test_gates_and_stages.py` does. Extraction is a Phase-2 improvement.

---

## [SEV: MED] [clients] [app/modules/clients/service.py:52-53]

**Issue:** A `def` returns the coroutine of an `async` repository method (prior F86).

**Why it's wrong:** `get_order_by_number` is declared `def`, not `async def`, and returns the
un-awaited coroutine of an async repo call. It works at `imports/router.py:83` only because that
caller awaits the result. Any caller treating it as synchronous gets a coroutine object, and the
underlying query never runs — silently, with no error.

**Fix sketch:** `async def` + `return await self.repo...`.

**Primary:** as above; check the two call sites. **Risk:** none. **Fallback:** none — this is a
one-line correctness fix.

---

## [SEV: MED] [clients] [app/modules/clients/service.py:21-41 vs utlis.py:2-22]

**Issue:** The SKU code generator exists twice, and the two copies have different importers.

**Why it's wrong:** `_slug`, `make_sku_code` and `sku_label` are duplicated verbatim between
`clients/service.py:21-41` and `clients/utlis.py:2-22` (filename typo original).
`load_to_db.py:57` imports `make_sku_code` from the **service** copy; `clients/repository.py:18`
imports from **utlis**. `sku.code` carries a unique index (`clients/models.py:140`), so the two
copies drifting apart means the importer and the repository disagree about a uniquely-constrained
value.

**Fix sketch:** delete the copy in `service.py`, import from `utlis.py` everywhere (or rename the
module and update both).

**Primary:** single definition. **Risk:** none today — the copies are identical. **Fallback:**
D1, but add a test asserting both produce the same output for a fixed input, so drift fails loudly.

---

## [SEV: MED] [imports] [app/modules/imports/service.py:14-17,33-43]

**Issue:** The module's declared facade has no callers (prior F83).

**Why it's wrong:** `imports/service.py` is documented as the module's entry point, and
`imports/router.py:33-34` imports `load_preview_into_order` and `build_preview` directly from
`load_to_db` and `import_engine`, bypassing it. The file acknowledges this as "D2 layering debt".
The router therefore holds business decisions (which parser, which session) that the facade was
created to own.

**Fix sketch:** route the two endpoints through `ImportsService`, or delete the facade.

**Primary:** delete it — the facade adds a layer nobody uses and the router is thin enough.
**Risk:** loses the Phase-2 seam the file was written for. **Fallback:** keep it and wire the
router through it; ~10 lines, D1.

---

## [SEV: MED] [imports] [app/modules/imports/load_to_db.py:17,21,19,202,172-193,83-170]

**Issue:** Merge residue is still present as duplicated and dead code.

**Why it's wrong:** The repository is in an unresolved merge state (`git ls-files -u` shows three
index stages for this file). Markers were hand-removed but never staged, and the residue shows:
`make_style_code` imported twice (`:17` and `:21`); `ProductionEvent` imported at module level
(`:19`) and again inside a function (`:202`); `_assert_no_production_history` (`:172-193`) is dead
and its body duplicated verbatim inline at `:220-236`; an 88-line commented-out `load_preview()`
block at `:83-170` containing further conflict-era annotations.

**Correct behavior:** resolve the merge, stage it, delete the residue.

**Fix sketch:** `git add` both files after review; delete the dead function and the commented
block; keep one import of each symbol.

**Primary:** resolve and stage. **Risk:** the working tree is what has been tested — verify the
staged content matches it exactly before committing. **Fallback:** none. Shipping from an
unresolved merge is the process equivalent of shipping untested code.

---

## Fat-service check

`ProductionService` (452 lines) and `WageService` (614 lines) are the two large ones. Neither is
doing HTTP or SQL construction — both delegate correctly to repositories — so the size is
business complexity, not layering violation. `AnalyticsService` (599 lines) builds its own
queries inline rather than through a repository, which is consistent with it being read-only and
owning no tables, but it means no analytics query is reusable or independently testable.

**Dependency direction is correct.** The documented lazy imports
(`production.service → materials.service` at `service.py:347`, `→ drawers.service`, and
`drawers.service → production.models`) are all inside the methods that use them, keeping the
module graph acyclic. Do not hoist them — `CLAUDE.md §15` is right about this and the code
complies.
