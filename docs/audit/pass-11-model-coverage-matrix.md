# Pass 11 — Model coverage matrix

models × schemas × repository × service × router × migration, scoped modules only.

Legend: ✅ present · ⚠️ partial · ❌ absent · — not applicable

| Table | Model | Schemas | Repo | Service | Router | Migration |
|---|---|---|---|---|---|---|
| `client` | `clients/models.py:32` | ✅ | ✅ | ✅ | ✅ | baseline `:17` |
| `client_order` | `clients/models.py:54` | ✅ | ✅ | ✅ | ✅ | baseline `:205`, uniq `20260714:16-18` |
| `style` | `clients/models.py:85` | ✅ | ✅ | ✅ | ⚠️ read-only | baseline `:350`, code `20260717:83,127` |
| `style_component` | `clients/models.py:118` | ❌ | ❌ | ❌ | ❌ | baseline `:458` |
| `sku` | `clients/models.py:131` | ✅ | ✅ | ✅ | ⚠️ read-only | baseline `:442` |
| `sku_order_line` | `clients/models.py:155` | ⚠️ | ✅ | ✅ | ❌ | `20260713:25-44` |
| `piece` | `production/models.py:73` | ✅ | ✅ | ✅ | ✅ | `20260712:63-85`, `20260730:159-163` |
| `production_event` | `production/models.py:97` | ✅ | ✅ | ✅ | ✅ | `20260712`, `20260730` |
| `operation` | `production/models.py` | ⚠️ | ✅ | ✅ | ✅ read-only | baseline |
| `operation_access` | `production/models.py` | ❌ | ✅ | ✅ | ❌ | baseline |
| `style_operation` | `production/models.py` | ❌ | ❌ | ❌ | ❌ | baseline |
| `barcode_registry` | `barcode/models.py:42` | ✅ | ✅ | ✅ | ✅ | `20260730:175` |
| `drawer` | `barcode/models.py:68` | ✅ (`drawers/`) | ❌ **no repo** | ✅ | ✅ | `20260730:139` |
| `material_lot` | `barcode/models.py` | ✅ | ✅ | ✅ | ✅ | `20260730:53` |
| `material_reservation` | `barcode/models.py` | ⚠️ | ✅ | ✅ | ❌ | `20260730:66` |
| `material_receipt` | `barcode/models.py` | ✅ | ✅ | ✅ | ✅ | `20260730:87` |
| `material_supplier` | `barcode/models.py:154` | ✅ | ✅ | ✅ | ✅ | `20260730:103` |
| `supplier_order` | `barcode/models.py:172` | ✅ | ✅ | ✅ | ✅ | `20260730:122` |
| `employee` | `employees/models.py` | ✅ | ✅ | ✅ | ✅ | baseline |
| `app_user` | `users/models.py` | ✅ | ✅ | ✅ | ✅ | baseline |
| `attendance_log` | `attendance/models.py:71` | ✅ | ✅ | ✅ | ✅ | baseline |
| `shift_config` | `attendance/models.py` | ✅ | ✅ | ✅ | ✅ | baseline |
| `rate` | `wages/models.py:30` | ✅ | ✅ | ✅ | ✅ | baseline, `20260717_1100` |
| `wage_run` | `wages/models.py` | ✅ | ✅ | ✅ | ✅ | baseline |
| `wage_line` | `wages/models.py:105` | ✅ | ✅ | ✅ | ✅ | uniq `20260731:30` |
| `wage_line_detail` | `wages/models.py:76` | ⚠️ | ✅ | ✅ | ⚠️ nested | baseline |
| `document` | `core/models.py` | ⚠️ | — | ⚠️ | ❌ | baseline |
| `notification` | `core/models.py` | ❌ | — | ❌ (bom only) | ❌ | baseline |
| `audit_log` | `core/models.py` | ❌ | — | ⚠️ write-only | ❌ | baseline |

**No scoped table lacks a migration, and no scoped migration creates a table without a model.**
That is a real improvement over the prior audit and worth stating plainly.

---

## [SEV: MED] [production] [app/modules/production/models.py — `style_operation`]

**Issue:** Three tables are declared, migrated, and completely unreachable.

**Why it's wrong:** `style_operation`, `style_component` (`clients/models.py:118`) and
`notification` (for scoped purposes) have no schema, no repository method, no service call and no
route. They occupy the schema and appear in `Base.metadata`, so Alembic maintains them forever.
`style_operation` in particular looks like the intended per-style operation routing — a reader
would assume the system supports it.

**Correct behavior:** either wire them or drop them.

**Fix sketch:** confirm with the DM whether per-style operation routing is a Phase-2 requirement;
if yes, mark them `# PHASE 2` in the model; if no, drop in a migration.

**Primary:** annotate now, decide after Aug 2. **Risk:** dropping a table the BOM pipeline expects
— check with the Phase-2 owner first. **Fallback:** leave them; the cost is documentation debt,
not runtime.

---

## [SEV: MED] [core] [`audit_log` write-only]

**Issue:** Audit rows are written and never read.

**Why it's wrong:** `barcode/service.py:248-253` and `drawers/service.py` write `AuditLog` rows
for barcode reissue/deactivate and drawer transitions. Nothing reads them — no repository method,
no endpoint, no report. `CLAUDE.md §15` states approval gates must be "hard, audited state
transitions… write an `audit_log` row", and the writes comply. But an audit trail nobody can
query is not yet an audit trail.

Note also that `drawers/service.py:179-194` (`release_nocommit`, the `SENDED → WAITING` recycle)
writes **no** audit row at all, so the one transition that destroys state is the one not recorded.

**Fix sketch:** add `GET /audit?entity_type=&entity_id=` gated to MD/DM, and an `_audit` call in
`release_nocommit`.

**Primary:** the missing write first (one call, D1) — a gap in the trail is worse than an
unqueryable trail. Reader endpoint D2. **Risk:** none. **Fallback:** query the table directly in
SQL when needed; acceptable for one factory, not for ten.

---

## Enum consistency across the matrix

`ProductionStage` — single definition, but restated by hand in `scripts/seed.py:245-261` and
hardcoded at `analytics/barcode_ext.py:131`. `UserRole.manager_roles()` omits `LINING_MANAGER`.
Both detailed in `pass-05`.

`DrawerState` has seven members (`enums_barcode.py:427-433`) and the column default is the raw
string `"waiting"` (`barcode/models.py:82`) rather than the enum member — a typo in that literal
would be accepted by SQLite and rejected by nothing until a state comparison silently failed.
