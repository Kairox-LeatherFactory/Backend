# Pass 5 — Data integrity / DB / migrations

---

## [SEV: HIGH] [core] [app/core/models.py:113-115]

**Issue:** A core table carries an FK into an out-of-scope module, so the scoped schema cannot be built alone.

**Why it's wrong:** `document.submission_id` references `submission.id`, which lives in the
**procurement** module. `Base.metadata.create_all` over scoped models alone fails with
`NoReferencedTableError: Foreign key associated with column 'document.submission_id' could not
find table 'submission'` — reproduced in the baseline against `tests/conftest.py:70`. The Phase-1
modules are therefore not independently deployable or testable; `core` (which by the project's own
rule is imported *by* modules, never the reverse) has a hard dependency on a Phase-2 table.

This is the **seam finding** the brief asked for: the boundary between Phase 1 and Phase 2 is not
clean at the schema level.

**Correct behavior:** core owns no FK into a feature module.

**Fix sketch:** make it `use_alter=True` with a nullable column, as `clients/models.py:71-76`
already does for `source_document_id`, or drop the FK and keep the id as an unconstrained
reference.

**Primary:** `use_alter=True` (matches existing precedent in the repo). **Risk:** needs a
migration; on Postgres `use_alter` emits a separate `ALTER TABLE`. **Fallback:** import the
procurement models in any process that builds the schema — which is what production does today —
and record the coupling. This is what makes it HIGH not BLOCKER: it works in the deployed app.

---

## [SEV: MED] [alembic] [alembic/versions/20260730_barcode.py:24]

**Issue:** The barcode migration imports the Postgres-native UUID and labels it as the portable type.

**Why it's wrong:**
```python
from sqlalchemy.dialects.postgresql import UUID as GUID   # the project's portable UUID type
```
The comment is false: the project's portable type is `app.core.models.GUID`
(`core/models.py:45-65`), which degrades to `CHAR(32)` off Postgres. This migration cannot run
against SQLite, while `core/models.py:12-15` and `core/database.py:26-27` both promise a SQLite
path. `20260717_1000_style_code.py` has the same import.

**Correct behavior:** migrations use the portable type, or the SQLite promise is withdrawn.

**Fix sketch:** `from app.core.models import GUID`.

**Primary:** use the portable type. **Risk:** none on Postgres — the emitted DDL is equivalent.
**Fallback:** D2 — Postgres is the only migration target today; the mismatch matters when CI adds
a SQLite migration check.

---

## [SEV: MED] [alembic] [alembic/versions/__pycache__/]

**Issue:** Compiled artifacts survive for six deleted revisions.

**Why it's wrong:** `__pycache__` holds `.pyc` files for `20260622_1006_sync_missing_columns`,
`20260626_1035_merge_migration_heads`, `20260626_1055_new_baseline`,
`20260714_1000_client_order_number_unique`, `20260717_1100_drop_employee_daily_rate`, and a
corrupt name `20260713_0900_sku_order _ine.cpython-313.pyc` — none of which have a `.py`. Any
database whose `alembic_version` row holds one of those ids fails `alembic upgrade head` with
"Can't locate revision". The presence of `..._merge_migration_heads` confirms the chain was once
branched and has been rewritten in place — so a staging DB stamped before the rewrite is exactly
the at-risk case.

**Correct behavior:** `alembic/versions/__pycache__` is not tracked, and any environment stamped
at a rewritten revision is re-stamped deliberately.

**Fix sketch:** add `alembic/versions/__pycache__/` to `.gitignore`, delete the directory, and
`SELECT version_num FROM alembic_version` on every environment before deploying.

**Primary:** as above — the **verification query is the important half**. **Risk:** none.
**Fallback:** none needed; this is a 5-minute check that prevents a failed deploy.

---

## [SEV: MED] [wages] [alembic/versions/20260731_wage_line_uniq.py:20]

**Issue:** The duplicate-collapse step is Postgres-gated but the constraint creation is not.

**Why it's wrong:** `if bind.dialect.name == "postgresql":` guards the collapse; the
`create_unique_constraint` at `:29-30` runs unconditionally. On any non-PG target carrying
pre-existing duplicate wage lines, the migration fails at the constraint. Postgres is production,
so this is contained — but it is why, in the `debug=True` configuration, the constraint arrives
via `create_all` rather than through this migration.

**Fix sketch:** make the collapse dialect-agnostic (a correlated `DELETE` works on both), or gate
both halves identically.

**Primary:** dialect-agnostic collapse. **Risk:** none. **Fallback:** D2.

---

## Constraint / relationship inventory (scoped tables)

Verified present and correct:

| Constraint | Where |
|---|---|
| `uq_piece_sku_seq` on `(sku_id, seq)` | `production/models.py:75` |
| `piece.code` unique + indexed | `production/models.py:77` |
| `uq_sku_identity` on `(style_id, color_code, size)` | `clients/models.py:132-134` |
| `client_order.order_number` unique (global) | `clients/models.py:56` — deliberate, `schemas.py:30` |
| `uq_att_emp_day` on `(employee_id, work_date)` | `attendance/models.py:71-72`, with `IntegrityError` retry at `service.py:309-316` |
| `uq_wage_line_run_emp` | `wages/models.py:107` + migration `:30` |
| `uq_wage_line_detail` | `wages/models.py:76-79` |
| `uq_rate` on `(style_id, operation_id, effective_from)` | `wages/models.py:32-34` |
| `uq_barcode_code` | `barcode/models.py:45-47` |
| `uq_drawer_code` | `20260730_barcode.py:152` |

Gaps worth naming:

- **No `CHECK (on_hand >= 0)`** on `material_lot` (`20260730_barcode.py:74`) — the DB-level half
  of the negative-stock finding (pass-01 / prior F14).
- **`piece.drawer_id` and `drawer.current_piece_id` are both indexed, neither unique**
  (`production/models.py:93-94`, `barcode/models.py:83-84`) — nothing at the DB level enforces
  one piece per drawer; the merge map is enforced only in `drawers/service.py:74-78` (prior F11).
- **`wage_line.wage_run_id` has no `ondelete`** (`models.py:109-111`) while its sibling
  `wage_line_detail` cascades (`:80-81`) — the asymmetry that makes `delete_run` fail (pass-02).
- **`piece.sku_id` has no `ondelete`** and `SKU` declares no `pieces` relationship
  (`clients/models.py:126-146`) — the re-import failure (pass-01).
- **`Client.name` is non-unique** (`clients/models.py:33`) yet `load_to_db.py:25` uses it as the
  natural key for get-or-create; a duplicate name splits an import or raises
  `MultipleResultsFound`.
- **No soft-delete convention** (prior F70): `is_active` exists on `Employee` (`models.py:38`)
  and `User` (`models.py:55`) and nowhere else; nothing filters on it consistently.

## Enum consistency

`ProductionStage` now has exactly one definition (`enums_barcode.py:80-154`) — prior F01 closed.
Two derived restatements remain and are **not** driven by the enum:

- `scripts/seed.py:245-261` — a hand-written legacy→canonical alias map with no entry mapping to
  `FINAL_INSPECTION` or `PACKAGE_EXPORT`, so rates for those two stages can never be seeded from
  the production cards.
- `analytics/barcode_ext.py:131` — a hardcoded `"LEATHER_CUTTING"` string literal, where
  `analytics/service.py:366-367` correctly uses the enum.

`UserRole.manager_roles()` (`core/enums.py:51-54`) returns only `{CUTTING_MANAGER,
STITCHING_MANAGER}` — it omits `LINING_MANAGER` (`enums.py:43`). Any caller using it to enumerate
floor managers silently excludes the lining line.
