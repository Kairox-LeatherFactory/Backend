# 1. What this service is

**Phase 2, Stages 2 and 3 — the bill of materials.**

A BOM answers one question, for one style:

> *"To make this garment, what material do we need, how much of each, and what does it cost?"*

Today a human answers it by reading a spec sheet and a pattern and typing a spreadsheet. This service generates that spreadsheet, lets the people who know better correct it, and then makes the sign-off a **hard, audited state transition** rather than an email saying "looks fine".

It is the middle of a five-stage pipeline:

```
 STAGE 1        STAGE 2 + 3          STAGE 4        STAGE 5
 INTAKE   ──▶   THIS SERVICE   ──▶   INVENTORY ──▶  SUPPLIER PO
 order +        generate the BOM,    what do we     buy the
 spec sheet     then approve it      already have?  shortfall
```

| Document | Covers |
|---|---|
| `PROCUREMENT_SYSTEM_GUIDE.docx` | Stage 1 (intake) and Stage 5 (purchase orders) |
| **this document** | **Stages 2 and 3 — `app/modules/bom/`** |
| `INVENTORY_SYSTEM_GUIDE.docx` | Stage 4 (the stock check) |

All of them are mounted under `/api/v1/procurement/…` so the URLs read as one workflow even though the code is four modules.

> **A longer, three-audience narrative exists** in `docs/KAIROX_PROCUREMENT_SYSTEM_GUIDE.md` and `docs/KAIROX_PROCUREMENT_API_REFERENCE.md`. This document is the working guide for the BOM half.

---

# 2. The shape of the work

A BOM is **not** generated from the submission. It is generated **per style**, and a real order sheet carries several styles. So there are three steps before generation can even start, and each one exists because the step after it refuses to guess.

```
1. BREAKDOWN    POST /submissions/{id}/order-breakdown    202, then poll
                  -> the order sheet becomes a list of STYLES
2. ATTACHMENTS  POST /order-styles/{id}/attachments        synchronous
                  -> a human confirms WHICH spec sheet and WHICH DXF pattern
3. GENERATE     POST /order-styles/{id}/generate-bom       202, then poll
                  -> the DRAFT bom + bom_item tree
```

## Step 1 — the order breakdown

`POST /procurement/submissions/{submission_id}/order-breakdown` enqueues extraction of the submission's **accepted order document** and returns **202** immediately. Extraction is a real Gemini call taking 30–200 seconds, so it never runs on the request path.

Poll `GET …/order-breakdown`. Its `status` is one of:

| `status` | Means |
|---|---|
| `not_started` | nothing has been queued yet |
| `processing` | claimed; the worker is running |
| `ready` | `styles[]` is populated — render the selection UI |

The POST is **idempotent in both directions**: rows already written come back as `already_ready` and nothing re-runs; a claim held but not yet finished comes back as `already_processing` and nothing is double-enqueued.

Two pre-flight refusals happen **before** the claim, so you get a real status code instead of a silent worker failure:

- the submission has no `client_id` → **422**, telling you to open the submission with `POST /submissions {"client_id": …}` first
- the order document is not accepted, or the submission is already claimed → **409**

> **Why the 422 matters.** That failure used to happen deep inside the Celery worker, after a 202 had already been returned. It was visible only in worker logs or by polling for a `ready` that never came.

## Step 2 — say which spec and which pattern

A BOM is generated **from** a spec sheet and a DXF pattern, and the system only ever **suggests** which ones. `POST /procurement/order-styles/{order_style_id}/attachments` is where a human confirms that pairing — or overrides it — for **one style**.

It is **synchronous**. A couple of column writes, no LLM, nothing to poll.

| Field | Does |
|---|---|
| `spec_document_id` | pin the spec sheet |
| `pattern_reference_id` | pin the DXF pattern |
| `clear_spec` / `clear_dxf` | drop a suggestion instead of confirming it |

Ids passed explicitly are **validated to exist and to belong to the same client** — a spec sheet from another buyer is refused rather than costed.

> **Why `clear_*` exists at all.** `generate-bom` refuses to run against a **dangling suggestion** — a spec the system guessed and nobody ruled on. So there have to be two ways to settle it: confirm it, or clear it. Without the second, a wrong suggestion could only be resolved by accepting it.

## Step 3 — generate

`POST /procurement/order-styles/{order_style_id}/generate-bom` returns **202** with a `task_id`. Three preconditions are checked **before** queuing, so the operator gets the 4xx immediately:

| Condition | Response |
|---|---|
| the style does not exist | **404** |
| the style already has a BOM | **200-shaped 202** with `status: "already_generated"` and the `bom_id` — an idempotent replay, not an error |
| `spec_match_status` is still `suggested` | **422** — confirm or clear it first (step 2) |

Poll `GET /procurement/order-styles/{order_style_id}/bom`:

- `status: "not_started"`, `bom: null` — generation has not linked a BOM yet
- `status: "ready"` — `bom` carries the full editable tree

## The DXF pattern

`POST /procurement/patterns` uploads a DXF and returns **202** with a `job_id` and a `channel`. It does **not** return the `pattern_reference_id` you need for step 2 — poll `GET /procurement/patterns` (filterable by `style_signature` and `client_id`) until the row you just uploaded appears, and take its `id`.

A DM or MD may name the `client_id` explicitly on the upload. A `client`-role caller is pinned to their own.

---

# 3. What a BOM line carries

A BOM is a header plus `items[]`. Beyond the obvious fields, two matter most for review.

| Field | Meaning |
|---|---|
| `category` | one of nine line types — see below |
| `name`, `material_color` | the material |
| `qty_per_garment` | **for a material line, this IS the DCM** (dm² per garment) |
| `uom` | the unit |
| `unit_price` | price per unit |
| `bulk_qty` | `order_qty × qty_per_garment` |
| `total_cost` | `qty_per_garment × unit_price` — the per-garment cost of this line |
| `dcm_source` | **where the consumption figure came from** |
| `dcm_confidence` | **0–1. The low-confidence lines are the ones to check first.** |
| `annotation` | free text from generation |

Header rollups:

| Field | Meaning |
|---|---|
| `garment_fob_price` | `Σ line.total_cost` — the cost of one garment |
| `bulk_total` | `order_qty × garment_fob_price` |
| `revision` | **the optimistic lock — you must send this back to edit** |

## The nine categories

`main_material`, `sub_material`, `lining`, `interlining`, `thread`, `accessory`, `manufacturing`, `packaging`, `fob_charge`.

Two facts follow from that list:

- **Only the first four get DCM-resolved.** They are leather AREA materials measured in dm². Threads, accessories, manufacturing, packaging and the FOB charge carry a given quantity and price, with `qty_per_garment` of 1 and the cost in `unit_price`.
- **`manufacturing` and `fob_charge` are excluded from the Stage-4 inventory check.** They are services and charges; there is no stock of them. Stage 4 reports them in an `excluded[]` block rather than calling them out of stock.

A buyer-supplied accessory carries `unit_price` 0 — it is in the BOM because it is in the garment, not because we pay for it.

---

# 4. Where the DCM comes from — the spine of Stage 2

DCM is the dm² of each material consumed per garment. **No spec sheet states it.** So the engine resolves it through a mandatory ordered fallback, and **a lower-numbered source always wins when it is available**:

| # | Source | `dcm_source` | `dcm_confidence` |
|---|---|---|---|
| 1 | the style consumption template — the DCM memory | `template` | **0.95** |
| 2 | the DXF pattern | `dxf` | **0.88** |
| 3 | a similar style | `similar_style` | **0.70** |
| 4 | the POM area heuristic — last resort, flagged | `ai_estimate` | **0.50** |
| — | nothing resolved: quantity 0, flagged, awaiting a human | `provisional` | — |
| 5 | a human typed it | `manual` | **1.00** |

Two things about this table are the whole design:

**The AI estimate is never the default.** It is source 4 of 5, it is stamped, and it is flagged for cutting review. It is also the *only* place a finished measurement touches a consumption figure. A garment type with no POMs on file can never use it at all.

**Source 1 is a memory that the confirmation writes.** When the cutting manager confirms a BOM, every material line's confirmed DCM is written back as a consumption template keyed on the **style signature**. The second order of the same physical style then resolves at 0.95 instead of 0.50. The signature is deliberately **not** the `style_id` — style rows are order-scoped, so it keys on the buyer's stable `customer_ref`, then `internal_ref`, then a slug of the name.

---

# 5. Editing — the optimistic lock, and who may write which field

`PATCH /procurement/boms/{bom_id}/items` sends `base_revision` — the `revision` you last read — plus a list of edits.

```
{ "base_revision": 3,
  "edits": [ {"bom_item_id": "…", "field": "unit_price", "value": 12.5} ] }
```

**Only three fields are editable at all:** `dcm`, `qty_per_garment` and `unit_price`. Anything else is a **422 `unsupported_field`**.

## The per-role narrowing

| Role | May edit |
|---|---|
| Direct Manager, Managing Director | `dcm`, `qty_per_garment`, `unit_price` |
| **Cutting Manager** | **`unit_price` only** |

A cutting manager attempting `dcm` gets a **403 `field_not_permitted_for_role`**, carrying `allowed_fields` so the screen can grey out the right boxes. It is a 403 and not a 422 on purpose: the request is well formed, the caller simply may not make that change.

> **Why.** The cutting manager's job on this screen is to check the BOM and sign it off. Before this narrowing, they could also rewrite the consumption figures the DM had prepared — and then confirm their own numbers.

## The refusals

| Situation | Response |
|---|---|
| the BOM is approved, locked or exported | **409 `bom_locked`** |
| the BOM was rejected | **409 `bom_rejected`** — reopen it first |
| somebody else edited it meanwhile | **409 `stale_revision`**, carrying `current_revision` — reload and re-apply |
| unknown `bom_item_id` | **422 `unknown_bom_item`** |
| a value that is not a number, or is negative | **422 `invalid_value` / `negative_value`** |

On success you get `{revision, recomputed, reconfirm_required}`. `recomputed` is the **whole re-costed tree** — the server recomputes, it never trusts a client-sent total.

## `reconfirm_required` — read this flag

If the edit changed a **DCM on a material line** and the BOM had **already been confirmed for cutting**, the confirmation is **torn up**: `cutting_confirmed_at` and `cutting_confirmed_by` are cleared and the status drops back to `draft`. `reconfirm_required: true` says so.

That is correct and it will surprise a user. A signature applies to the numbers that were signed; changing a consumption figure invalidates it, and the MD's approval gate waits on a live confirmation. **Show it.** Otherwise a BOM silently leaves the MD's queue.

---

# 6. Stage 3 — approval, and the separation of duties

```
draft ──confirm-cutting──▶ ready_for_review ──approve──▶ approved ──export──▶ exported
                                   │                        (or locked)
                                   └──reject──▶ rejected ──reopen──▶ draft
```

| Call | Who | Note |
|---|---|---|
| `POST /boms/{id}/confirm-cutting` | **Cutting Manager** (DM/MD bypass) | confirms the consumption figures. `draft` → `ready_for_review`. |
| `POST /boms/{id}/approve` | **Managing Director ONLY** | refuses a BOM whose cutting is unconfirmed. Body `{"lock": true}` → `locked`. |
| `POST /boms/{id}/reject` | **MD only** | `reason` is required |
| `POST /boms/{id}/reopen` | DM / MD | `rejected` → `draft`, `revision` + 1 |
| `POST /boms/{id}/export` | **MD only** | renders the PDF |

> **The Direct Manager is refused on approve, reject and export — on purpose.** The DM prepares and edits the BOM; the person who prepares a financial document must not be the person who approves it. This is the one place in KairoX where the DM's usual superuser bypass does not apply (`require_exact_roles`). The MD passes every gate in the system.

**Deploy note:** that gate needs a real `MANAGING_DIRECTOR` login to exist. Run `python scripts/ensure_roles.py --apply` on an environment whose seeded staff include no MD.

## `confirm-cutting` is a signature, not a toggle

Posting it twice on the same BOM is a **409 `already_confirmed`**, carrying who confirmed it and when. There are exactly two ways off a confirmation: an edit that changes a DCM (§5), or `reopen` after a rejection.

> Before that refusal, a second POST re-stamped the confirmation, re-ran the template back-fill, re-recorded the DXF yield observations, and raised **a second round of review notifications** at an MD who already had the BOM in their queue.

Its response is worth reading, not discarding:

| Field | Meaning |
|---|---|
| `templates_backfilled` | how many consumption templates this confirmation wrote — the DCM memory for the next order |
| `notifications_created` | how many reviewers were notified |
| `warnings` | `["no_style_signature"]` when the BOM's identity resolved to nothing, so no template could be keyed and the back-fill was skipped. **The confirmation still succeeds** — refusing the cutting manager's sign-off over an un-writable cache entry would be far worse. |

## `approve` does four things, not one

`POST /boms/{id}/approve` is the single busiest call in Phase 2. In one request it:

1. **materialises the breakdown** — creates the Client → Order → Style → SKU tree from the BOM's parsed order snapshot and back-links it. This is the row Phase 1 will track garments against.
2. sets the status to `approved` (or `locked`) and stamps the approver.
3. **advances the production board** to `bom_approved`.
4. **runs the Stage-4 inventory check** and returns its `inventory_check_id`.

Steps 3 and 4 are best-effort side-effects: they are logged and rolled back on failure, and they never fail the approval. **`inventory_check_id` can legitimately come back `null`** — the BOM is still approved; run `POST /boms/{id}/inventory-check` yourself.

## Approve and reject are a matched pair

Both require the BOM to be in `ready_for_review`. Anything else is a 409 naming the current status:

| Situation | Response |
|---|---|
| cutting not confirmed | **409 `cutting_confirmation_required`** |
| the BOM was rejected | **409 `bom_rejected`**, carrying `rejection_reason` |
| any other non-review status | **409 `not_ready_for_review`**, carrying `current_status` and `approved_at` |

> Before this, approve checked *only* the confirmation stamp and never the status — so an already-approved BOM could be re-approved (re-running the inventory check and the board advance), a just-rejected BOM could be approved without anyone reopening it, and an exported one could be approved again.

## Export is idempotent — read `replay`

`POST /boms/{id}/export` renders the PDF, stores it (deduplicated by sha256), and moves the BOM to `exported`. Calling it again **replays**:

| Field | First export | Replay |
|---|---|---|
| `replay` | `false` | **`true`** |
| `exported_at` | now | **the ORIGINAL timestamp** |
| `export_document_id`, `sha256` | the new document | the same document |
| audit row | `BOM_EXPORT` written | **none written** |

**Do not render a replay as a second export.** Export is only allowed from `approved`, `locked` or `exported` — anything else is **409 `not_approved`**.

---

# 7. Notifications

When a BOM reaches `ready_for_review`, the reviewers are notified. The `notification` table is the source of truth; delivery is a view over it.

| Call | Use |
|---|---|
| `GET /procurement/notifications` | the list; `unread_only` optional |
| `GET /procurement/notifications/stream` | **Server-Sent Events** — a live push, re-polling every 10s, with keep-alive comments |
| `POST /procurement/notifications/{id}/open` | mark it read |

- **Both the MD and the DM get their own row**, each with its own independent escalation clock (`notify_dm_on_review`, default on).
- Each row carries `scheduled_for = now + bom_review_escalation_hours` (**default 2 hours**).
- **`POST …/open` is what stops the escalation.** An unopened notice is chased by a background sweeper, which emails the recipient. Only the recipient may open their own row.
- A missed or closed SSE stream **loses nothing** — the plain GET reads the same rows, and a reconnecting stream emits the unseen backlog first.

The sweeper is DB-driven and idempotent (a NOT-EXISTS guard), so it survives restarts and never double-emails. See §9.

---

# 8. The admin reference data (`/procurement/admin/*`)

A generated BOM is only as good as five tables of reference data, and they used to be **hardcoded** — editable only by a redeploy. These endpoints make them editable at runtime. **Every one is DM/MD.**

| Endpoint | Holds |
|---|---|
| `PUT /admin/dxf-yields/{species}` | the yield factor for a hide species — `sheep`, `goat`, `calf`, `lamb`, and a `_default` |
| `POST /admin/fabric-roles` | the CAD lexicon: which fabric label in a DXF means which role |
| `GET` / `PUT /admin/cost-catalog[/{garment_code}]` | the default cost lines for a garment code |
| `GET` / `PUT /admin/checks[/{client_code}]` | the per-client BOM validation rules |
| `GET` / `POST /admin/pom-dictionary` | measurement-term → POM-code mappings |

## Three things about these that will bite

**A `PUT` replaces a code's whole line set; it does not merge.** `PUT /admin/cost-catalog/{garment_code}` sends *every* line that garment code should have. Sending one line leaves it with one line.

**A write takes effect immediately, without a restart — and that is engineered, not incidental.** The fabric lexicon is read **synchronously on the hot path**, inside BOM generation. A live database read there would block the event loop, so this config lives in a **process-level snapshot**: warmed once at startup and pushed in directly by these admin writes. A TTL re-read is the backstop for a second replica's write, so on more than one replica a change can lag by that TTL. **Do not add a sixth reference table by reading it live on that path.**

**An empty table means DEFAULTS, not zero.** A fresh database falls back to the built-in values, so an unseeded system produces a sane BOM rather than one costed at nothing. `GET /admin/cost-catalog` returning values therefore proves **nothing** about whether anybody has configured it.

## `GET /admin/pom-dictionary` shows which mappings are still a guess

Its rows carry `status` and `confidence`, so an admin can tell an **LLM-suggested** mapping that still needs review from one a human confirmed. Without those two fields the dictionary read like settled reference data when half of it was a machine's guess.

---

# 9. Who can do what

Read **"plus MD and DM"** into every row except the one that says otherwise — they are superusers and pass any ordinary `require_roles` gate. Only `require_exact_roles` refuses them.

| Action | Roles |
|---|---|
| Read a BOM and its items; edit `unit_price`; confirm cutting | **Cutting Manager** (+ DM/MD) |
| Edit `dcm` / `qty_per_garment` | **DM / MD only** — the cutting manager gets a 403 |
| **Approve / reject / export a BOM** | **Managing Director ONLY — the DM is refused** |
| Reopen a rejected BOM | DM, MD |
| All five `/admin/*` reference tables | DM, MD |
| Notifications, patterns, order breakdown, attachments, `generate-bom` | **any logged-in user** |

> ⚠ **Several Stage-2 writes are gated only by "logged in".** `POST /order-styles/{id}/attachments`, `POST …/generate-bom`, `POST /patterns` and `POST …/order-breakdown` take a token and **check no role** — a `supervisor` or `security` login can trigger BOM generation today. That looks like an omission rather than a decision, since everything around them is DM/MD. **Do not design a screen around it**; assume it will tighten to DM/MD.

---

# 10. Patterns a frontend must handle

| Pattern | Where |
|---|---|
| **202 and poll** | order breakdown, BOM generation, pattern upload |
| **Server-Sent Events** | `GET /procurement/notifications/stream` |
| **Optimistic locking (`base_revision` → 409 `stale_revision`)** | `PATCH /boms/{id}/items` |
| **Replay, not repeat** | `already_generated` on generate-bom; `replay: true` on export |
| **Structured error bodies** | most 409s and 422s here carry `{"error": "…", "message": "…"}` **inside** `detail`, not just a sentence. Read `detail.error` to branch, `detail.message` to display. |

---

# 11. Notes for backend developers

- **Generation is ONE transaction.** `generate_for_order` + `generate_bom` form a single unit of work: rows are staged with `db.add()` / `flush()` and committed exactly once at the end, so a mid-generation failure leaves nothing half-written. The repository read helpers used on that path **must not commit** — if any does, the guarantee breaks.
- **`generate_bom` is an orchestrator over private steps** (`_extract_spec`, `_resolve_and_persist_poms`, `_spec_attributes`, `_resolve_pattern`, `_build_items`) in the **same class, session and transaction**. Splitting them into separate service classes would fragment that unit of work — that is why they are not.
- **`costing.py` is pure math**, no DB and no I/O, reused by generation, by the bulk-PATCH recompute, and by supplier-PO totals. Compute at write time, return the recomputed tree, never recompute on read and never trust a client-sent total.
- **Beware `repo.save()` expiring `bom.items`.** `save()` ends in `db.refresh(bom)`; `Bom.items` is mapped `cascade="all, delete-orphan"`, and `"all"` includes refresh-expire. Touching an item attribute afterwards triggers a lazy load on an async session and kills the request with `MissingGreenlet`. `confirm_cutting` snapshots the four fields it needs as plain values **before** saving, for exactly this reason. `expire_on_commit=False` does **not** cover it — it is `refresh()`, not the commit, doing the expiring.
- **Cross-module reads go through the owning service, never its repository.** Inventory reads a BOM via `BomService.get_bom_dto`; this service reads clients via `ClientService`. Keep it that way.
