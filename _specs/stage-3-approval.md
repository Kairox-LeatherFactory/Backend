# Stage 3 — Approval Spec: MD Review, Lock & PDF Export of the BOM

**Status:** Draft for MD / stakeholder review
**Scope:** The Stage-3 *approval stage* only — the in-app "BOM is ready" notification to
MD/DM (with a 2-hour auto-email fallback), the approval **state machine** and its
edge-level RBAC, the **rejection** path, **PDF export** of the approved BOM, and the
**audit trail** that records every edit / approval / rejection. Editing on the approval
screen **reuses the Stage-2 contract verbatim**. **No code, no migrations** are part of
this document.
**Builds on:** `_specs/stage-0-foundation.md` (schema §1, roles §5, audit/notification
tables §1f), `_specs/stage-1-upload-validation.md` (the upload front door), and
`_specs/stage-2-bom-generation.md` (the editable BOM contract §7, the cutting-manager
gate §10, the recompute DAG §9).
**Source of truth for the process:** `data/BOM_Procurement_Workflow_fixed.pdf` /
`data/_wf.txt`, Stage 3 ("MD Approval & Lock"). **Render target:** `data/BMO-1.pdf`
(the "Yardage and Price Quotation").

> **Stack facts (re-confirmed against the code, not stage prose).** Stage 3 is *partly
> wired already*: `app/modules/procurement/bom_service.py` ships `approve_bom(lock=…)`
> (refuses a BOM whose `cutting_confirmed_at` is null), `confirm_cutting(...)` (stamps the
> gate + back-fills the DCM memory), and the editable bulk-PATCH `edit_bom_items(...)` with
> optimistic `revision` locking; the router exposes `POST /boms/{id}/approve`,
> `POST /boms/{id}/confirm-cutting`, `PATCH /boms/{id}/items`, `GET /boms/{id}`.
> `Notification` (channels `in_app`/`email`/`call`, `type=BOM_AWAITING_REVIEW`,
> `scheduled_for`/`opened_at`/`status`/`parent_notification_id`) and `AuditLog`
> (`actor_user_id`/`action`/`before`/`after`/`at`) already exist
> (`procurement/models.py`). **What does NOT yet exist:** `BomStatus` is only
> `DRAFT/APPROVED/LOCKED` (no `ready_for_review`/`rejected`/`exported`); there is **no
> WebSocket** infra (the repo *does* ship SSE — `StreamingResponse` /
> `text/event-stream` in `intelligence/router.py`); there is **no scheduler**
> (APScheduler/Celery); and there is **no PDF-generation library** (`pypdf` is a read-only
> text-layer probe). This spec calls out each delta explicitly.

---

## 0. Context & why this stage exists

Stage 2 hands off a **DRAFT** `bom` + `bom_item` tree that the cutting manager has
reviewed and confirmed (`cutting_confirmed_at` set, the DCM memory back-filled). Stage 3
is the **human authority gate**: the MD (or DM, scoped below) is *notified* that a BOM
awaits review, *reviews and optionally edits* it, then **approves** (which locks it,
freezing the numbers and the revision history) or **rejects** it with a reason. An
approved BOM is **exported to a PDF** shaped like `BMO-1.pdf` and stored as a durable,
self-verifying artifact. The workflow doc's standing requirements land here:

> "…cryptographically locked with a full revision history including all edits, the
> approving identity, and a timestamp." — `data/_wf.txt`, Stage 3

Stage 3 owns: the readiness notification + escalation, the approve/reject/lock state
machine, PDF export, and the audit completeness guarantee. It does **not** do the
inventory check (Stage 4), the supplier PO (Stage 5), or the material-ready alert
(Stage 6).

### Decisions (confirmed with the product owner)
1. **State machine = `draft → ready_for_review → approved | rejected → exported`** with
   explicit per-edge RBAC (§1). `approved` *is* the locked, immutable state; the existing
   `LOCKED` value is the technical immutability flag `approved` sets.
2. **Notification delivery = SSE + persisted row + poll fallback** — no WebSocket
   introduced; every notice is a durable `notification` row regardless of channel (§2).
3. **Escalation = 2 hours, in-app → auto-email** (the product owner's instruction
   *supersedes* the workflow doc's "5 hour" figure): if the MD/DM hasn't *seen* the
   in-app notice within 2 h, an email goes out automatically (§2).
4. **Editing on the approval screen reuses Stage-2's contract** — the same bulk-PATCH +
   optimistic `revision` lock, no duplicated edit/recompute logic (§3).
5. **PDF export = WeasyPrint (HTML/CSS + Jinja2), ReportLab as the pure-Python fallback**;
   it renders the **current persisted** BOM (post-edit source of truth), only from a
   locked BOM, and stores the result as a `Document` (§4).
6. **Sending the PDF to the client = OUT of scope** for Stage 3 (§6) — the BOM/quotation
   is an internal artifact; transmission is a separate concern with no channel in the
   6-stage doc.

---

## 1. The approval state machine

### 1a. States (extend `BomStatus`)

`BomStatus` today is `DRAFT / APPROVED / LOCKED`. Stage 3 **adds** `READY_FOR_REVIEW`,
`REJECTED`, `EXPORTED` (str-enum, VARCHAR column — the module convention; **not** a native
PG enum, so the add stays reversible and SQLite-portable, per stage-0 §3 / `enums.py`).

| State | Meaning | Editable? |
|---|---|---|
| `draft` | Generated by Stage 2; cutting manager has not yet confirmed, or a late DCM edit re-opened the gate. | ✅ (Stage-2 contract) |
| `ready_for_review` | Cutting manager confirmed (`cutting_confirmed_at` set); awaiting MD decision. The MD-notification fires on entry. | ✅ (edits allowed; a DCM edit re-gates → back to `draft`) |
| `approved` | MD approved. **Immutable** — `locked_at` + `approved_by`/`approved_at` stamped; the existing `LOCKED` flag is set so every edit path returns `409 bom_locked`. | ❌ |
| `rejected` | MD rejected with a mandatory reason. Terminal until reopened for rework. | ❌ (must reopen to `draft` first) |
| `exported` | A PDF has been rendered + stored from the approved/locked BOM. | ❌ |

> **On `approved` vs `LOCKED`.** The product owner's state list does not separate "locked"
> as its own user-facing state, and the existing `approve_bom(lock=…)` already conflates
> them in practice. Stage 3 **resolves this**: approval *always* locks (`approve` sets
> `locked_at` and the `LOCKED` immutability semantics). The status surfaced to the UI is
> `approved`; `LOCKED` remains the internal flag the edit guards key off. A separate
> "approve-without-lock" mode is **not** offered — the doc requires the approved BOM to be
> locked.

### 1b. Edges + who may transition each one

RBAC follows stage-0 §5: **MD is the sole approver/rejecter** (and superuser); the
**Cutting Manager** owns the confirm edge (Stage-2 §10); **DM** may edit drafts and reopen
rejections but **cannot** approve.

| Edge | Trigger | Allowed role | Notes |
|---|---|---|---|
| `draft → ready_for_review` | `POST /boms/{id}/confirm-cutting` | **Cutting Manager** (MD superuser) | Existing `confirm_cutting` stamps `cutting_confirmed_*` + back-fills the DCM memory. **Addition (this spec):** it must also flip `status → ready_for_review` and **emit the `BOM_AWAITING_REVIEW` notification** (§2). Today it only stamps the timestamp. |
| `ready_for_review → approved` | `POST /boms/{id}/approve` | **MD only** | Existing `approve_bom`; already refuses (`409 cutting_confirmation_required`) when `cutting_confirmed_at` is null. Sets `approved_by/at` + `locked_at`, status `approved`, writes `BOM_APPROVE` audit. |
| `ready_for_review → rejected` | `POST /boms/{id}/reject` *(new)* | **MD only** | Mandatory `reason`; stamps `rejected_by/at` + `rejection_reason`; writes `BOM_REJECT` audit (reason in `after`). |
| `ready_for_review → draft` | a **DCM edit** via bulk-PATCH | DM, Cutting, MD | The existing `reconfirm_required` path: a DCM edit after confirmation clears `cutting_confirmed_*` (Stage-2 §9). This spec maps that to a **status flip back to `draft`** so the gate must run again — a confirmed number can never change underneath the MD. A non-DCM edit (e.g. `unit_price`) keeps `ready_for_review`. |
| `rejected → draft` | `POST /boms/{id}/reopen` *(new)* | **MD or DM** | Reopen for rework; `revision++`, clears `rejected_*`, status `draft`. Cutting re-confirmation is then required again before the next review. |
| `approved → exported` | `POST /boms/{id}/export` *(new)* | **MD** (or system) | Renders + stores the PDF (§4); status `exported`; writes `BOM_EXPORT` audit. Only valid from `approved`/locked. |
| `approved → *` (edit) | bulk-PATCH | any | **Rejected** — `409 bom_locked` (existing). The approved BOM is frozen. |

```
                 confirm-cutting (Cutting)        approve (MD)         export (MD/system)
  draft ───────────────────────────► ready_for_review ──────────► approved ───────────► exported
   ▲   ▲                                  │      │
   │   │ DCM edit re-gates (any editor)   │      │ reject (MD, +reason)
   │   └──────────────────────────────────┘      ▼
   │            reopen (MD/DM, revision++)     rejected
   └──────────────────────────────────────────────┘
```

### 1c. `bom` column deltas (patch to stage-0 §1c / the live model)

Everything the approve path needs already exists on `bom` (`status`, `approved_by`,
`approved_at`, `locked_at`, `revision`, `cutting_confirmed_*`). Stage 3 **adds**:

| Column | Type | Why |
|---|---|---|
| `rejected_by` | FK `app_user`, null | who rejected (MD). |
| `rejected_at` | `DateTime(tz)`, null | when. |
| `rejection_reason` | `Text`, null | the mandatory reason; also copied into the `BOM_REJECT` audit `after`. |
| `export_document_id` | FK `document`, null | the rendered BOM-quote PDF (`Document.kind = bom_quote`). Distinct from `source_document_id` (the *input* BMO quote). |
| `exported_at` | `DateTime(tz)`, null | when the current export was produced. |

Migration: a plain additive `ALTER TABLE … ADD COLUMN` (reversible; write `downgrade`),
registered via `alembic/env.py` per stage-0 §3. No native-enum `ALTER TYPE` is needed —
`BomStatus` is a VARCHAR.

---

## 2. In-app notification when the BOM is ready (delivery + 2-hour escalation)

When a BOM enters `ready_for_review`, **both the MD (the approver) and the DM (operational
oversight, per stage-0 §5)** must be told — two recipients, **two independent
`notification` rows**, each with its own 2-hour escalation. The requirement is twofold:
deliver an **in-app** notification *now*, and if a recipient hasn't **seen** their notice
within **2 hours**, **auto-email** that recipient.

### 2a. Delivery mechanism — decision

| Dimension | WebSocket | SSE ✅ RECOMMENDED | Polling (only) |
|---|---|---|---|
| **New infra** | A WS layer (ASGI WS routes, connection registry, ping/pong) — none exists today. | **None** — the repo already ships SSE (`StreamingResponse(media_type="text/event-stream")` in `intelligence/router.py`). | None. |
| **Direction** | bidirectional (overkill; notifications are server→client only) | server→client push (exactly the shape needed) | client pull. |
| **Latency** | ms | ~instant on the open stream | up to the poll interval. |
| **Proxy / auth fit** | needs WS upgrade handling; Bearer-token-on-connect is fiddly | plain HTTP + the existing Bearer dependency; survives proxies | trivial. |
| **Resilience** | a dropped socket loses events unless replayed | reconnect + **replay-from-DB** is trivial (see below) | inherently stateless. |

**Recommendation: SSE for the live push, a `notification` row as the durable backbone, and
a plain GET as the polling fallback.** WebSocket is rejected — it adds a whole connection
layer for a one-directional feed the platform already serves over SSE. The non-negotiable
is that **delivery channel is a *view*; the `notification` table is the source of truth** —
SSE and polling both read the same rows, so a missed/closed stream loses nothing.

**Endpoints (new, under `/api/v1/procurement`):**

| Method & path | Purpose | Auth |
|---|---|---|
| `GET /notifications` | List the caller's notifications (unread first); the **poll fallback** + initial load. | any authenticated user (scoped to `recipient_user_id = self`) |
| `GET /notifications/stream` | **SSE** stream of new/updated notifications for the caller (the live push). On connect, replays unsent/unopened rows so a reconnect never drops one. | same |
| `POST /notifications/{id}/open` | Mark a notification **seen** — sets `opened_at`, status `opened`. This is the signal that **cancels** the 2-hour email escalation. | recipient only |

The SSE stream may also set `opened_at` when it actually *delivers* a row to a live
client; `POST …/open` is the explicit acknowledgement from the UI when the MD views the
review screen. Either one stamping `opened_at` cancels escalation.

### 2b. Persistence + the 2-hour auto-email — using the existing columns

Each recipient gets a `notification` row (no schema change — the stage-0 columns already
fit); the MD and the DM get **one row each**, escalated independently:

| Column | Value on the in-app BOM-ready notice |
|---|---|
| `recipient_user_id` | one row for the **MD**, one row for the **DM** (both always created) |
| `channel` | `in_app` |
| `type` | `BOM_AWAITING_REVIEW` |
| `subject` / `body` | "BOM ready for review — {style} / order {order_number}" + a deep link |
| `entity_type` / `entity_id` | `bom` / the bom id (polymorphic ref → the approval screen) |
| `status` | `pending` → `sent` (delivered over SSE or fetched) → `opened` (seen) |
| `scheduled_for` | **`created_at + 2h`** — the escalation deadline |
| `opened_at` | null until seen; set by SSE-delivery or `POST …/open` |
| `parent_notification_id` | null (this is the root of the chain) |

**The escalation rule:** at `scheduled_for`, **iff `opened_at` is still null**, create a
**child** row (`channel = email`, same `type`, `parent_notification_id =` the in-app row)
and send the email. If the MD opened it first, no email is ever sent. This is the literal
"in-app, then email after 2 h if unseen."

### 2c. The timer — a DB-driven in-process sweeper (no Celery/APScheduler)

There is no scheduler in the repo. Rather than introduce one, Stage 3 uses a **single
in-process `asyncio` sweeper task started in `main.py`'s lifespan**, polling every ~60 s:

```
sweep():  # every ~60s, in the app's lifespan task
  due = SELECT * FROM notification
        WHERE channel='in_app' AND type='bom_awaiting_review'
          AND opened_at IS NULL
          AND scheduled_for <= now()
          AND NOT EXISTS (SELECT 1 FROM notification c
                          WHERE c.parent_notification_id = notification.id
                            AND c.channel='email')      -- idempotent: don't double-email
  for n in due:  create email child + send + mark sent
```

- **State lives in the table, not in memory** → the sweeper is **idempotent and survives a
  restart**: a crash mid-window just means the next sweep picks the row up; the
  `NOT EXISTS` guard prevents a second email. No in-memory timer to lose.
- **Single-replica caveat (documented, mirrors the house pattern).** Like the in-process
  login rate-limiter (CLAUDE.md §6/§13.9), one sweeper per process means **>1 API replica
  would double-fire** without coordination. The fix when scaling is a `SELECT … FOR UPDATE
  SKIP LOCKED` claim on due rows (single-flight across replicas) or lifting the sweep into
  an external scheduler/worker. Called out, not built — Stage 3 ships the single-process
  version.
- **Email transport** is a thin `Notifier`/email-backend abstraction (SMTP creds via
  `settings`, blank-defaulted; a no-op/log driver in dev) — mirroring Stage-1's pluggable
  storage backend. Wiring real SMTP is config, not code change.
- **Email send runs in `run_in_threadpool`** if the SMTP client is blocking, per the house
  async rule (CLAUDE.md §3.3).

---

## 3. BOM editing on the approval screen — the **same** Stage-2 contract

The approval screen lets the MD/DM correct a value before approving. This is **not** a new
edit path. It reuses, unchanged:

- `PATCH /api/v1/procurement/boms/{bom_id}/items` — the **bulk** edit endpoint
  (`bom_service.edit_bom_items`), with **optimistic locking on `bom.revision`**
  (`409 stale_revision` on a stale `base_revision`), server-side recompute of the whole
  tree returned in the response, `409 bom_locked` once approved, and one `BOM_EDIT` audit
  row with before/after per accepted bulk edit. (Stage-2 §7.)
- The recompute DAG and the **DCM-edit re-gating** (Stage-2 §9): a DCM edit flips
  `dcm_source → manual`, clears `cutting_confirmed_*`, and (this spec, §1b) flips the
  status back to `draft` — so an edited consumption number forces re-confirmation before
  the MD can approve. The endpoint already returns `reconfirm_required`.

**Stage-3 adds only a status-aware guard, no logic duplication:** edits are accepted in
`draft` / `ready_for_review` / `rejected`; once `approved`/locked, the existing
`bom_locked` 409 stands. The approval-screen "edit" button calls the identical endpoint;
the front end submits the cells it changed against the `revision` it last read.

> **Why no separate "approval edit" endpoint:** a divergent path would re-implement the
> interdependent recompute (DCM → line total → bulk → FOB) and fork the audit trail for
> what is the same logical operation. One editable contract, one recompute, one audit
> shape — across Stage 2 and Stage 3.

---

## 4. PDF export

### 4a. Library — decision

| Option | How a quotation is built | Pros | Cons |
|---|---|---|---|
| **WeasyPrint** ✅ | Jinja2 → **HTML/CSS** → PDF | HTML/CSS is the natural fit for an invoice/quotation grid like BMO-1; pixel control via CSS; the template is reviewable + diffable; trivial to match the "Yardage & Price Quotation" layout. | Native deps (cairo/pango/gdk-pixbuf). **Fine in the Docker/Linux runtime** (where the API actually runs + is tested end-to-end); friction on bare Windows dev. |
| **ReportLab** (fallback) | programmatic canvas / Platypus flowables | Pure-Python, pip-only, **no native deps** (works on Windows dev out of the box); battle-tested. | Layout is imperative Python; reproducing BMO-1's table layout is more verbose + less maintainable than CSS. |
| **fpdf2** | programmatic cells | Pure-Python, tiny. | Lowest-level; weakest at rich tabular layout. |

**Recommendation: WeasyPrint (HTML/CSS + Jinja2) as primary, ReportLab named as the
pure-Python fallback** if the native deps prove painful on a given dev box. Rationale: the
deliverable is a quotation that *looks like* `BMO-1.pdf` — a bordered header (brand, style,
season, FOB total), a line grid (material, colour, DCM, UOM, unit price, per-garment mass,
bulk qty, total), and footer totals. CSS expresses that far more cleanly than imperative
drawing, and the runtime that matters (Docker) has the deps. The add to `requirements.txt`
is `weasyprint` (or `reportlab`); both are pip-installable.

- **Execution:** PDF rendering is CPU/blocking → run in `run_in_threadpool` from the async
  route (the same pattern as the openpyxl/extraction work), so the event loop is never
  blocked.
- **Template:** a single Jinja2 HTML template (`templates/bom_quote.html`) + CSS, fed the
  `_bom_view(bom)` dict the service already produces. Onboarding a client-specific layout
  later is a template change, not a code branch.

### 4b. Handling editable values that differ from the original generation

The central correctness point: **the PDF renders the *current persisted* `bom` /
`bom_item` rows, never the original extraction.** Because every edit goes through the
bulk-PATCH which **recomputes server-side and writes the rows** (Stage-2 §7/§9), the table
*is* the post-edit source of truth — there is no separate "generated" snapshot to drift
from. To make the rendered numbers unambiguous and immutable:

1. **Export is allowed only from `approved`/locked.** A locked BOM rejects all edits
   (`bom_locked`), so the values cannot change after approval — the PDF is a faithful
   render of the frozen, approved figures (the doc's "locked" requirement).
2. The render reads `bom.revision`, `approved_by`, `approved_at`, and stamps them **onto
   the PDF** (revision + approver identity + timestamp) so the artifact itself carries the
   audit identity the workflow doc requires.
3. The rendered PDF is stored as a `Document` (`kind = bom_quote`, `sha256`, `storage_url`
   via the Stage-1 storage backend), linked from `bom.export_document_id`; `exported_at`
   is stamped and status → `exported`.
4. **Idempotent re-export:** re-exporting an unchanged locked BOM yields a byte-identical
   render → same `sha256` → the existing `Document` is returned, no duplicate (mirrors the
   Stage-1 sha256 dedupe). If a `rejected → reopen → edit → re-approve` cycle changed the
   numbers, the new revision renders a *new* PDF (new sha256), and the prior export
   `Document` is retained for audit.

---

## 5. Audit log — every edit, approval, rejection recorded

The `AuditLog` table (actor / `action` / `entity_type` / `entity_id` / `before` / `after`
/ `at`) already exists and is already written by Stage 2 (`BOM_GENERATE`, `BOM_EDIT`,
`BOM_CUTTING_CONFIRM`, `BOM_APPROVE`). Stage 3 guarantees completeness across the approval
lifecycle by enumerating its actions — **no schema change**:

| Action | Written on | `before` / `after` |
|---|---|---|
| `BOM_SUBMIT_FOR_REVIEW` | `confirm-cutting` flips to `ready_for_review` | before: prior status; after: `{status, cutting_confirmed_at, templates_backfilled}` |
| `BOM_EDIT` | each accepted bulk-PATCH (Stage 2 contract, also on the approval screen) | full before/after snapshot incl. per-item `qty_per_garment`/`unit_price`/`total_cost`/`dcm_source` + `revision` |
| `BOM_APPROVE` | MD approve/lock | before: `{status}`; after: `{status: approved, approved_by, approved_at, locked_at, revision}` |
| `BOM_REJECT` | MD reject | before: `{status}`; after: `{status: rejected, rejected_by, rejected_at, rejection_reason}` — **the reason is captured here** |
| `BOM_REOPEN` | reject → draft rework | before: `{status: rejected, revision}`; after: `{status: draft, revision+1}` |
| `BOM_EXPORT` | PDF render + store | after: `{export_document_id, sha256, exported_at, revision}` |

- **Who + when + what changed** is therefore on every mutating edge: `actor_user_id` (the
  authenticated MD/DM/cutting user), `at` (server-side `datetime.now(timezone.utc)` — the
  house rule, never client clocks), and the before/after JSONB diff. The header-level trail
  (`bom.revision`, `approved_by/at`, `locked_at`, `rejected_*`) is the queryable summary;
  the field-level diff lives in `audit_log` (stage-0 §1f).
- **Append-only.** Audit rows are never updated or deleted; a correction is a new row. This
  is the "full revision history including all edits" the doc demands.

---

## 6. Sending the PDF to the client — **OUT of scope** for Stage 3

**Decision: out of scope.** Justification:

- The BOM / "Yardage & Price Quotation" is an **internal** costing artifact (FOB build-up,
  material consumption, supplier-bound shortfalls). The 6-stage workflow doc has **no
  client-facing BOM-delivery step** — the client *sends* the order + spec sheets (Stage 1);
  what flows *back* to the client is governed by the buyer relationship, not this pipeline.
- The platform's `CLIENT` role is **read-only on its own orders** (stage-0 §5 / CLAUDE.md
  §6); there is no outbound client-messaging channel in scope, and exposing internal
  cost/DCM build-up to a buyer is a deliberate business decision, not a default.
- Stage 3's responsibility ends at **export + store + make downloadable** (the PDF
  `Document` is retrievable by authorized internal roles via the storage backend).

**If client transmission is later required**, it slots cleanly onto the existing
primitives without reworking Stage 3: a new `NotificationType` (e.g. `BOM_TO_CLIENT`) +
email channel + the `export_document_id` as the attachment, gated behind an explicit MD
action and an RBAC check — a Stage-6-adjacent concern, not approval.

---

## 7. Acceptance criteria

A reviewer must be able to confirm each:

1. **State machine + RBAC.** A cutting-manager `confirm-cutting` moves a `draft` BOM to
   `ready_for_review` **and** emits a `BOM_AWAITING_REVIEW` notification; only an MD can
   `approve` or `reject`; a DM cannot approve (`403`); approval refuses while
   `cutting_confirmed_at` is null (`409 cutting_confirmation_required`, existing).
2. **Notification persists + delivers.** Entering `ready_for_review` writes **two**
   `notification` rows — one for the MD, one for the DM (`in_app`, `scheduled_for = +2h`);
   each is delivered over `GET /notifications/stream` (SSE) and listed by
   `GET /notifications`; `POST …/open` sets `opened_at`/`status=opened` per recipient.
3. **2-hour escalation, conditional.** With `opened_at` still null at `scheduled_for`, the
   sweeper creates an `email` child (`parent_notification_id` set) and sends it **once**
   (idempotent — a second sweep does not double-send). If the MD opened it before the
   deadline, **no email** is sent.
4. **Editing reuses Stage 2.** An approval-screen edit hits `PATCH /boms/{id}/items` with
   optimistic `revision` locking; a stale `base_revision` → `409 stale_revision`; a DCM
   edit re-opens the cutting gate and flips status back to `draft`
   (`reconfirm_required: true`); a locked BOM rejects edits (`409 bom_locked`).
5. **Rejection captured.** `POST /boms/{id}/reject` requires a reason; it stamps
   `rejected_by/at` + `rejection_reason` and writes a `BOM_REJECT` audit row whose `after`
   contains the reason; `reopen` returns the BOM to `draft` with `revision+1`.
6. **PDF reflects edited values, from the locked BOM only.** Export is refused before
   approval; after lock, the rendered PDF shows the **post-edit** figures (e.g. a DCM the
   cutting manager overrode, not the original `ai_estimate`), carries the revision +
   approver identity + timestamp, is stored as a `Document(kind=bom_quote)` linked via
   `bom.export_document_id`, and a re-export of the unchanged BOM is sha256-idempotent.
7. **Audit completeness.** Every edge (`SUBMIT_FOR_REVIEW`, `EDIT`, `APPROVE`, `REJECT`,
   `REOPEN`, `EXPORT`) produces an `audit_log` row with actor, server-side `at`, and a
   before/after diff; rows are append-only.
8. **Client send stays out.** Stage 3 produces + stores the PDF but does **not** transmit
   it to the client; no client-facing notification or channel is created.

---

## 8. Review checklist (definition of done — spec only, nothing to run)

- [ ] The state machine `draft → ready_for_review → approved | rejected → exported` is
      defined with an explicit per-edge RBAC table; `approved` reconciled with the existing
      `LOCKED` flag; the `BomStatus` + `bom` column deltas are listed as additive,
      reversible migrations.
- [ ] Notification delivery compares WebSocket / SSE / polling and recommends **SSE +
      persisted `notification` row + poll fallback**, with no new WS infra; every notice is
      a durable row.
- [ ] The **2-hour** in-app→email escalation is specified on the existing `notification`
      columns (`scheduled_for`/`opened_at`/`parent_notification_id`), driven by a
      DB-idempotent in-process sweeper, with the single-replica caveat + scale path called
      out (and the doc's "5 hour" explicitly superseded).
- [ ] Approval-screen editing is stated to **reuse the Stage-2 bulk-PATCH + optimistic
      `revision`** contract verbatim (no duplicated edit/recompute logic), adding only a
      status-aware guard and the DCM-edit re-gate to `draft`.
- [ ] PDF export picks a library (**WeasyPrint**, ReportLab fallback) with rationale, a
      template approach, threadpool execution, and — critically — renders the **current
      persisted post-edit** values from a **locked** BOM, stored as a sha256-deduped
      `Document` with revision/approver/timestamp stamped on the artifact.
- [ ] Audit log enumerates every Stage-3 action with actor / timestamp / before-after, is
      append-only, and captures the rejection reason.
- [ ] Sending the PDF to the client is explicitly **out of scope**, justified, with the
      forward-compatible hook noted.
- [ ] No code, migrations, or model edits were produced — spec only.
- [ ] Reviewed / signed off by MD / system stakeholder.
