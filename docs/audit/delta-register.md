# Delta register — prior audit (2026-07-30) re-verified against 2026-07-31

Prior report: `docs/audit/AUDIT_SUMMARY-2026-07-30.md` + `docs/audit/kairox-audit.html`.
124 detailed finding blocks were recoverable from the prior HTML (the prior summary counted
132 findings across 139 pass entries; the residual are table-only cross-references without
their own block — they are covered by the block they reference).

**Status vocabulary**

| Status | Meaning |
|---|---|
| `FIXED` | verified closed, with the FILE:LINE that closes it |
| `OPEN` | still present, re-cited at today's lines |
| `PARTIAL` | the mechanism was added but does not fully close the finding |
| `REGRESSED` | fixed, then broken again — or the fix introduced a new defect |

**Headline:** of 124 prior findings, **38 FIXED**, **9 PARTIAL**, **4 REGRESSED**, **73 OPEN**.
The Jul-31 burn-down closed the two "cannot start / cannot migrate" blockers and the wage
uniqueness gap. It did **not** close the security set, and it introduced four new blockers
(see `pass-01`, `pass-02`, `pass-03` — F139–F146).

---

## Blockers (14 prior)

| ID | Title | Status | Evidence today |
|---|---|---|---|
| F00 | Two dead imports stop the application at startup | **FIXED** | no `attendance.barcode` or `load_preview` import remains; `python -c "import app.main"` succeeds |
| F01 | Star-import silently replaces the stage vocabulary | **FIXED** | `app/core/enums.py:133` is an explicit named import; the wildcard is gone and `:125-132` documents the removal |
| F02 | Every production scan 500s against a seeded database | **FIXED** | `scripts/seed.py:105-121` derives operation codes from `ProductionStage.leather_chain()`; access rows derived from `STAGE_ROLE_ACCESS` at `:127-136` |
| F18 | Retirement only enforced for employee barcodes | **FIXED** | `barcode/service.py:38-52` `_get_active_or_410` applies 404/410 to **any** type; used at `:60`, `:86` |
| F20 | A closed payroll run can be recomputed | **REGRESSED** | guard added at `wages/service.py:348`, but `confirm_closed` is wired to no route or schema → recompute now returns 409 **unconditionally**. Over-corrected: see **F143** |
| F22 | Interrupted run leaves committed money invisible | **OPEN** | `compute_run` still issues 4 separate commits (`wages/repository.py:252,266,239,270`); the "one run, one transaction" block at `repository.py:382-430` has zero callers |
| F30 | Dead reassignment of role-reader dependencies | **OPEN** | `wages/router.py:157-160` still reassigns `_RATE_READERS`/`_PAYROLL_READERS` after every `Depends()` has captured the originals from `:50-54` |
| F32 | Tokens may be signed with the shipped default key | **PARTIAL** | validator added at `core/config.py:247-273`, but `is_local_dev` (`:256`) exempts `environment == "local" and debug`, and both default that way (`:50-51`). An unconfigured deploy still boots on `"dev-only-insecure-change-me-in-prod"` (`:124`) |
| F33 | A client login can read every other client's orders | **FIXED** | `client_scope` predicate applied at `analytics/service.py:169-170, 224-225, 314-315, 370-372, 405-406` and `barcode_ext.py:55-59` |
| F34 | Geofence bypassed by omitting GPS | **OPEN** | `attendance/service.py:195-207` — with no `lat`/`lon` the fence never runs; the only cost is an unvalidated free-text `reason` (`schemas.py:22`). The row still satisfies `is_present_today` (`service.py:332-335`) |
| F46 | CORS origins hardcoded incl. a third-party preview domain | **OPEN** | `main.py:194-201` — 3 `localhost` origins + `frontend-rust-pi-23.vercel.app`, `allow_credentials=True`, not read from settings |
| F59 | Migration ships a literal placeholder parent | **FIXED** | `20260724_01_july28_fixes.py:9` = `20260717_1100_drop_daily_rate` |
| F103 | Three modules ship a misnamed `init.py` | **OPEN** | `app/modules/{barcode,drawers,materials}/init.py` still present; no `__init__.py` in any of the three |
| F128 | Pure logic rarely extracted, so most is untestable | **OPEN** | only `wages/proration.py` and `attendance/geofence.py` are pure; the four production gates remain methods on `ProductionService` (`service.py:107-173`) |

## Highs (28 prior) — status roll-up

**FIXED (11):** F03 (rework double-decrement — `production/service.py:293,306-318,346` now decrements only for `fresh_cut`), F07 (`DrawerState.HOLDING_LINING` added, `enums_barcode.py:430`), F35 & F49 (no duplicate/shadowing attendance route; `/today` gated at `router.py:191-193`), F37 (`employees/router.py:32-40` — roster denied to CLIENT/VIEWER, salary only to HR/DM/MD), F50 (SUPERVISOR is now in the proxy role set, `attendance/router.py:84,95`), F52 (`materials/router.py:30-31` `_LOT_WRITERS`), F60 (single root, single head), F61, F84 (`analytics/service.py:38` inherits `BarcodeAnalyticsMixin`), F102 (unique index present).

**OPEN (15):** F04, F05, F06 (gate fail-open paths unchanged at `production/service.py:135-138,148-160`), F23, F24, F36, F51, F70, F81, F98 (zero `selectinload`/`joinedload` in production/barcode/drawers), F108, F114, F115, F122, F129 (`main.py:144-147` still the only reliable schema path), F130, F133.

**PARTIAL (2):** F21 (constraint now exists in `wages/models.py:107` **and** `20260731_wage_line_uniq.py:30` — but the pre-collapse step at `:20` is Postgres-gated, so the migration fails on any non-PG target carrying duplicates), F71.

## Med / Low (82 prior) — status roll-up

**FIXED (~19):** including F58 (lining-manager access now seeded, `scripts/seed.py:124-129`), F62, F64, F65, F104 (`barcode/models.py:165` renamed to `material_supplier`, resolving the class/table collision that dominated the stale test log), F90, F95, F116.

**OPEN (~59):** the bulk. Notable ones that remain exactly as reported:

| ID | Today's citation |
|---|---|
| F10 / F11 | `drawers/service.py:179-194` writes no audit row; `piece.drawer_id` and `drawer.current_piece_id` are still two unconstrained links (`production/models.py:93-94`, `barcode/models.py:83-84`) |
| F14 | `materials/service.py:266-269` — no floor check, no `CHECK (on_hand >= 0)` in `20260730_barcode.py:74`, no row lock in `repository.py:32-33` |
| F16 / F79 / F99 | `barcode/repository.py:53-76` still `ORDER BY code DESC LIMIT 1` with no DB sequence |
| F17 | `production/router.py:149` still `continue`s past unresolvable seqs |
| F57 | 7 of 10 analytics endpoints carry no `require_roles` (`analytics/router.py:49,62,72,81,109,117,127,135`) |
| F72 / F74 / F87 | commit/rollback placement unchanged |
| F76 | `app/core/beat_schedule.py` is imported by nothing (grep across `app/` finds the name only in the file itself) |
| F77 / F131 | `main.py:163-166` in-process sweepers, one set per replica, no lock |
| F93 | zero `limit`/`offset` on any scoped list endpoint |
| F105 / F107 | `drawers` still has no `models.py`/`repository.py`; `materials` has no `__init__.py` |
| F125 / F127 | six `*_dump.txt` source dumps still committed (~3,650 lines) |
| F135 / F136 / F137 / F138 | no DR procedure; `/health` still a bare liveness probe (`main.py:271-274`); `docs_url`/`redoc_url` unconditional (`main.py:188-189`); pool settings at library defaults |

**REGRESSED (3, besides F20):**

| ID | What changed | Evidence |
|---|---|---|
| F81 | The "redundant" wage aggregate was edited and is now **broken**, not merely redundant | `production/repository.py:213,222` group/order by `Piece.style_id`, a column that does not exist — see **F139** |
| F94 | The declared-vs-runtime contract gap moved into `attendance/schemas.py`: the router now reads a field the schema no longer declares | `attendance/router.py:64` — see **F141** |
| F86 | Still a non-async wrapper returning a coroutine; the new `imports` seam widened it | `clients/service.py:52-53` |

---

## What the burn-down actually bought

The Jul-31 work closed the two "the app does not start / the schema does not build" blockers
and the wage-uniqueness gap — real progress on items 1, 2 and 5 of the prior top-10.

It did **not** touch items 6 (secret key), 9 (geofence) or the analytics role gaps, and the
edits made to production and wages introduced **four new blockers** (F139, F140, F141, F142)
plus two new highs (F143, F146). Net blocker count is **unchanged at 6**; the money path is
**worse** than it was yesterday, because piece-rate payroll now fails at the query layer
before any of yesterday's guards are reached.
