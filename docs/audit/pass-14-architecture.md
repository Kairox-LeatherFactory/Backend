# Pass 14 — Architecture

---

## [SEV: BLOCKER] [process] [`git ls-files -u`]

**Issue:** The repository is in an unresolved merge state.

**Why it's wrong:** `git ls-files -u` reports three index stages for
`app/modules/imports/load_to_db.py` and add/add stages for `app/modules/production/router.py`.
Conflict markers were hand-removed from the working tree but the resolution was never staged. The
merge residue is still visible as duplicated imports and dead code (`pass-08`).

This is a blocker for a reason that is not about code quality: **what has been tested is the
working tree, and what would be committed is not yet defined.** Any `git commit` from this state
either stages a resolution nobody has reviewed, or fails. A deploy built from this repository is
built from an ambiguous source.

**Correct behavior:** review both files, `git add` them, commit the resolution as its own commit
before any further work.

**Fix sketch:**
```bash
git diff --stat HEAD -- app/modules/imports/load_to_db.py app/modules/production/router.py
git add app/modules/imports/load_to_db.py app/modules/production/router.py
git commit -m "Resolve merge in imports loader and production router"
```

**Primary:** resolve and stage first, before any fix in this audit. **Risk:** the staged content
must match the tested working tree exactly — diff before adding. **Fallback:** none. This is
step one of the burn-down.

---

## [SEV: HIGH] [core] [app/core/models.py:113-115,141-143]

**Issue:** `core` depends on two Phase-2 feature modules, inverting the stated import direction.

**Why it's wrong:** The project rule is that `core` is imported by modules, never the reverse.
`core/models.py:113-115` declares an FK to `submission.id` (**procurement**) and `:141-143` to
`supplier.id` (**supplier_po**), with a string-named relationship to `Submission` at `:127-129`.
The consequence is concrete, not theoretical: the Phase-1 schema cannot be created without the
Phase-2 modules (`pass-05`), which is why the test harness fails on `create_all`.

**Correct behavior:** cross-phase references are nullable, unconstrained ids or use `use_alter`.

**Fix sketch:** `use_alter=True` on both, matching `clients/models.py:71-76`.

**Primary / Risk / Fallback:** as `pass-05`.

---

## Import-direction seams into out-of-scope modules

Recorded, not followed, per the brief:

| Scoped location | Reaches into |
|---|---|
| `main.py:64-67` | model imports of `procurement`, `bom`, `inventory`, `supplier_po` |
| `main.py:104,120` | `bom.notification_service`, `supplier_po.po_service` sweepers |
| `main.py:154-157` | lifespan imports `bom.config_store` |
| `core/models.py:113-115,141-143` | `procurement`, `supplier_po` FKs |
| `clients/service.py:118-126` | docstring: "the public entry point `bom.service` calls at MD approval" |
| `core/celery.py:24-27` | autodiscovers `app.modules.bom` |

The `clients/service.py:118-126` seam is the **good** one and worth protecting: it is exactly the
"Phase-1 manual and Phase-2 auto-generated paths converge on ONE contract" design `CLAUDE.md §15`
calls for. `create_order_with_breakdown` is shaped so a BOM-generated breakdown flows through the
same validation and storage surface as a DM's upload. That seam should not be disturbed by any
fix in this audit.

The `main.py` and `core/models.py` seams are the bad ones: they make Phase 1 undeployable without
Phase 2.

---

## [SEV: MED] [all] [duplication]

Three live duplications, each already cited:

1. `_slug` / `make_sku_code` / `sku_label` — `clients/service.py:21-41` **and**
   `clients/utlis.py:2-22`, with different importers on each (`pass-08`). Highest risk of the
   three: it feeds a uniquely-indexed column.
2. The drawer completeness rule — `drawers/service.py:96` and `:141`, same expression twice.
3. `_assert_no_production_history` — `load_to_db.py:172-193` (dead) and inline at `:220-236`
   (live).

**Primary:** collapse each to one definition. **Risk:** none. **Fallback:** D1; add a test pinning
the two SKU-code implementations to the same output first, so drift fails loudly even if the
collapse slips.

---

## [SEV: MED] [repo hygiene] [`*_dump.txt`]

**Issue:** Six committed source dumps, ~3,650 lines, holding stale copies of live files.

**Why it's wrong:** `analytics_dump.txt`, `materials_dump.txt`, `imports_dump.txt`,
`clients_dump.txt`, `attendance_dump.txt`, `production_dump.txt`, `barcode_dump.txt`,
`core_dump.txt` are full-source snapshots checked into the tree beside the code they copy. They
are actively misleading: `attendance_dump.txt:433` still declares the `direction` field whose
absence causes the blocker in `pass-09`, and `barcode_dump.txt:179-183` still declares
`class Supplier` with `__tablename__ = "supplier"`, the collision that was renamed away. Grep and
IDE search hit them, and they read as source.

**Fix sketch:** delete them; add `*_dump.txt` to `.gitignore`.

**Primary:** delete. **Risk:** none — they are copies. **Fallback:** none; this is a 30-second fix
that removes a standing source of wrong answers. Do it in the same commit as the merge resolution.

---

## Layer separation — scorecard

| Rule | Compliance |
|---|---|
| Router = HTTP only | ✅ except `attendance/router.py:50-61` (authorization logic inline) and `employees/router.py:32-40` |
| Service = business logic | ✅ except `analytics` (builds its own queries), `drawers` + `materials` (direct ORM) |
| Repository = all DB access + txn | ✅ in 8 modules; ❌ `drawers` (none), ⚠️ `materials` (bypassed) |
| Import direction acyclic | ✅ — the documented lazy imports work; `core → modules` is the one violation |
| DTOs at boundaries | ⚠️ `analytics` returns raw dicts (`pass-09`) |

**SOLID / dead code:** the largest single architectural problem is not a violated principle but
volume of code that reads as live and is not — `mint_pieces`, `_empty_result`, the wages
`*_nocommit` block, `imports/service.py`, `_assert_no_production_history`, the pre-barcode
schemas, `beat_schedule.py`, and the eight dump files. A new engineer joining for the Aug 20 phase
would form a materially wrong model of this system from reading it, which is why the prior audit's
F125 is worth more than its MED severity suggests.
