# Stage 5 — Supplier PO Spec: Matching, PO Generation, Send, Tracking & Escalation

**Status:** Draft for MD / stakeholder review
**Scope:** The Stage-5 *supplier purchase-order* stage only — resolving each shortfall
line to a supplier, generating the PO form (one template per supplier type), the
pre-send cross-check approval, the editable PO contract, the email send + bounce
handling, open/read tracking, the WhatsApp→call escalation ladder, the order/style
production-tracking board, and supplier CRUD. It consumes the Stage-4 shortfall lines
and hands a confirmed-material signal to **Stage 6** (material-ready MD alert). **No
code, no migrations** are part of this document.
**Builds on:** `_specs/stage-0-foundation.md` (schema §1e/§1f, roles §5, LLM policy §4),
`_specs/stage-1-upload-validation.md` (pluggable storage backend, the config-seeded YAML
registry pattern), `_specs/stage-3-approval.md` (the bulk-PATCH + optimistic-`revision`
edit contract, the SSE notification + in-process sweeper, the `Notifier`/email backend
abstraction, WeasyPrint PDF export), and `_specs/stage-4-inventory.md` (the `normalized_key`
matcher, the `material_alias` promote-by-config loop, the shortfall lines this stage acts on).
**Source of truth for the process:** `data/BOM_Procurement_Workflow_fixed.pdf` /
`data/_wf.txt`, Stage 5 ("Supplier PO & Escalation"). **Supplier data:**
`data/SUPPLIERS_updated (2).xlsx`. **PO-form reference:** the 40 PDFs in
`data/suppler-po-form/`.

> **Stack facts (re-confirmed against the code, not stage prose).** The Stage-5 tables
> already exist in `app/modules/procurement/models.py`: `Supplier` (`name` unique,
> `phone`, `email`, `service`, `gstin`, `address`, `currency` INR, `payment_terms_days`
> 60, `lead_time_days` 10, `is_active`), `PurchaseOrder` (`po_number`, `supplier_id`,
> `bom_id?`, `client_order_id?`, `buyer_ref`, `issue_date`, `delivery_days`,
> `payment_terms_days`, `currency`, `subtotal`/`cgst`/`sgst`/`round_off`/`total`,
> `status`, `sent_at`, `pdf_document_id?`), `PoItem` (`item_no`, `description`, `color`,
> `uom`, `qty`, `unit_price`, `amount`, `inventory_item_id?`, `bom_item_id?`) and
> `PoResponse` (`channel`, `sent_at`, `responded_at?`, `confirmed_qty?`, `status`,
> `notes`). Enums `POStatus` (`draft/sent/responded/confirmed/escalated/cancelled`),
> `POResponseChannel` (`email/call`), `POResponseStatus` (`pending/opened/confirmed/
> no_response`), `NotificationChannel` (`in_app/email/call`), `NotificationType`
> (`po_dispatch/escalation_call/...`) are defined in `enums.py`. The Stage-3
> `Notification` + sweeper + `Notifier` email abstraction + WeasyPrint export pattern are
> reused verbatim. **What does NOT yet exist:** the supplier importer; the
> supplier↔material history index that powers matching (§1); a `revision`/cross-check
> approval columns on `purchase_order` (§3, §4); a WhatsApp/voice transport; the
> open-tracking pixel/link infra (§6); the `po_tracking_event` table (§6); the
> `production_tracking` board (§8); and the PO-form Jinja templates (§2). Each delta is
> called out explicitly and listed in §10.

---

## 0. Context & why this stage exists

Stage 4 hands off, per approved+locked BOM, a set of **shortfall lines** —
`inventory_check_line` rows with `status ∈ {partial, out_of_stock}`, an exact
`shortfall_qty`, the originating `bom_item_id`, and (when matched) an `inventory_item_id`.
Stage 5 turns each shortfall into a **supplier purchase order** that PAKKAR TANVEER
EXPORTS (the factory) sends to the right vendor, then chases until the vendor
acknowledges. It owns six jobs:

1. **Resolve** each shortfall line to the correct supplier (§1).
2. **Generate** a PO form per supplier, picking the right template for the supplier type
   (§2).
3. **Cross-check** the draft PO before send — leather → cutting manager, accessory →
   MD/DM/HR (§3) — with the same editable contract as the BOM (§4).
4. **Send** the PO by email with bounce handling and attachments (§5).
5. **Track** opens/reads and **escalate** when the supplier goes quiet: 5 h no open →
   WhatsApp, no read → auto-call (§6, §7).
6. **Track** the order/style forward through production and expose supplier CRUD (§8, §9).

Stage 5 does **not** approve/lock the BOM (Stage 3), run the inventory check (Stage 4),
post goods-receipt stock increments, or fire the final material-ready MD alert (Stage 6 —
Stage 5 only emits the *confirmed-material* signal that Stage 6 consumes).

### The hard problem — the supplier master is real, dirty, and mostly contactless

`data/SUPPLIERS_updated (2).xlsx` has two sheets and they tell two different stories:

| Sheet | Shape | What it is | The catch |
|---|---|---|---|
| **`Supplier contact info`** | 1,433 × 4 — `COMPANY NAME · PHONE · EMAIL · SERVICE` | the contact directory feeding `supplier` | **sparse:** only **65** rows carry a usable phone, **45** an email, ~**115** a (free-text, 49-distinct) `SERVICE`. Most rows are `NULL`/blank. A naive "look up the supplier's email and send" fails for ~97% of rows. |
| **`Suppliers provision`** | 1,524 txns × `DATE·MONTH·MODE·BUYER·BUYER PO.NO·PO.NO·COMPANY NAME·DESCRIPTION·NOM·PCS·QTY·RATE·AMOUNT·CGST·SGST` | the **purchase ledger** — every historical buy | the **real article→supplier evidence**: 193 distinct suppliers, `MODE ∈ {MATERIALS 586, LEATHER 366, SERVICE 166, JOB WORK 110}`, e.g. `BISMI LEATHERS → SHEEP NAPPA BLACK`, `GLOBAL EXIM → GOAT SUEDE COLOUR:…`, `M.V.P.N.K → TAFFTA LINING CLOTH`, `GATEWAY ENTERPRISES → BUTTON / D-S TISSUE TAPE`. |

The conclusion that shapes §1: **the contact sheet alone cannot drive matching** (the
`SERVICE` column is noisy free text and is blank for most vendors). The trustworthy
signal is the **provision ledger** — "who have we actually bought this exact article
from, how often, how recently, at what rate." Matching is therefore a *historical-supply*
lookup keyed on the same `normalized_key` Stage 4 already computes, with the contact sheet
supplying the send address — and a first-class handling of the very common case
**"matched a supplier but we have no email/phone for them."**

### Decisions (confirmed with the product owner)

1. **Supplier matching = deterministic historical-ledger lookup first** (the provision
   sheet, normalized like Stage 4), `SERVICE`/`MODE` category as the fallback, an advisory
   fuzzy candidate that is **never auto-bound**, and a **manual pick** when ambiguous —
   the same "no LLM unless required, never a silent guess" posture as stage-0 §4 / Stage 4
   §5 (§1).
2. **One PO template per supplier type** (leather vs accessory/material vs service/jobwork),
   as Jinja2/WeasyPrint templates selected by a config registry — onboarding a new supplier
   type is a template file + a registry row, not a code branch (§2). Reuses the Stage-3
   WeasyPrint export machinery.
3. **Pre-send cross-check is a mandatory gate** with material-type-routed approver
   (leather→Cutting Manager; accessory→MD/DM/HR), a small state machine bolted onto the
   existing `POStatus`, and the Stage-3 SSE-notification + sweeper escalation (§3).
4. **The editable PO reuses the BOM's contract verbatim** — bulk-PATCH + optimistic
   `revision` lock, server-side recompute of GST/total, `409 po_locked` once sent (§4).
5. **Email = a pluggable provider behind the Stage-3 `Notifier` abstraction; recommend
   Amazon SES** for cost + deliverability, with bounce/complaint handling via SNS
   webhooks; templating + the PO PDF as an attachment (§5).
6. **Open tracking = self-hosted pixel + link-wrapping**, not a third-party SaaS, after a
   Mailtrack/HubSpot/Streak/Yesware comparison — to avoid per-seat lock-in and keep
   read-state in our own `po_tracking_event` table (§6).
7. **Escalation ladder = email → (5 h no open) WhatsApp → (no read) auto-call**, on a
   Twilio-backed transport, driven by the existing DB-idempotent sweeper, and **stopped the
   instant the supplier acknowledges** by any channel (§7). The doc's "5 hour" figure is
   honored here (unlike Stage 3, which the product owner reset to 2 h for the *internal* BOM
   notice; the *external* supplier window stays 5 h).
8. **Order/style production tracking = one `production_tracking` row per style** advancing
   through an explicit status ladder, system-driven on each stage transition + manually
   nudgeable by DM/Cutting/MD (§8).

---

## 1. Supplier matching — shortfall line → supplier

### 1a. The input and the keys

A shortfall row carries: `bom_item` `name` (`SHEEP GLASS`), `material_color` (`BLACK`),
the size context (the BOM is single-style; sizes roll up into `bulk_qty`, so size is **not**
a matching key for the supplier — leather/trims are bought by article+colour, not by
garment size), `category` (`BomItemCategory`: `MAIN_MATERIAL`/`LINING`/`ACCESSORY`/…), and
(when Stage 4 matched stock) the `inventory_item` with its `normalized_key`/`article_ref`.

Two reference structures are built once, at ingest, from `SUPPLIERS_updated (2).xlsx`:

- **`supplier`** ← `Supplier contact info` (name, phone, email, service) — the send target.
- **`supplier_supply_history`** ← `Suppliers provision` (new table, §1d): for each
  `(supplier, normalized_description)` the `mode` (LEATHER/MATERIALS/…), `txn_count`,
  `last_purchased_at`, `last_rate`, `min/max_rate`. This is the matching index, normalized
  with the **same** `normalized_key` function Stage 4 uses on inventory descriptions, so a
  BOM term, an inventory row, and a historical buy all collapse to one comparable key.

### 1b. The ordered matching rules (deterministic-first, never a silent guess)

```
 shortfall line ─► (1) exact normalized_key on supplier_supply_history ─ hit(s) ─► rank → best supplier
                       │  miss
                       ▼
                   (2) material_alias rewrite (Stage-4 synonym table) then re-try (1) ─ hit ─► rank
                       │  miss
                       ▼
                   (3) category/MODE fallback: suppliers whose history MODE or contact
                       SERVICE matches the bom_item.category bucket ─ hit ─► rank (weaker)
                       │  miss / >1 plausible
                       ▼
                   (4) advisory fuzzy/embedding candidate ─► surfaced as suggestion, NOT auto-bound
                       │
                       ▼
                   (5) no confident match ─► UNRESOLVED: PO held in draft, flagged
                       needs_supplier, a human picks from the supplier list
```

- **(1) Exact historical supply.** Join the shortfall's `normalized_key` (run
  `bom_item.name` + `material_color` through the Stage-4 normalizer) to
  `supplier_supply_history.normalized_description`. A hit means "we have bought this exact
  article before, from these vendors." This is the cheapest and most defensible answer.
- **(2) Alias rewrite.** Reuse Stage-4's `material_alias` (`SHEEP GLASS → SHEEP NAPPA`) to
  rewrite the term, then re-run (1). Confirming a fuzzy suggestion writes an alias row →
  promotes the pair to the free path next time (the same promote-by-config loop as Stage
  1/2/4).
- **(3) Category fallback.** No exact article history, but the line's `category` maps to a
  `MODE`/`SERVICE` bucket (`MAIN_MATERIAL`/`SUB_MATERIAL` → `LEATHER`; `LINING`/`INTERLINING`/
  `THREAD`/`ACCESSORY`/`PACKAGING` → `MATERIALS`; with finer `SERVICE` hints `ZIPS`,
  `LINING`, `BUTTONS`, `RIBS`, `LABELS`, `DYE`…). Returns category-capable suppliers, ranked
  lower — the human is told this is a category guess, not an article match.
- **(4) Advisory fuzzy.** A token-similarity / local-HF-embedding candidate
  (`intelligence/models_catalog.py`, no chat-LLM cost) may *propose* a supplier, but it is
  **never auto-applied** — it appears as a `suggestion` the buyer confirms.
- **(5) Unresolved.** No confident match → the PO line is held; the PO stays `draft` with a
  `needs_supplier` flag; the buyer picks a supplier from the §9 list. Never a fabricated
  vendor.

### 1c. Ranking & the ambiguity fallback (the "which of several" decision)

Path (1)/(2) commonly returns **several** suppliers (e.g. `SHEEP NAPPA BLACK` was bought
from `BISMI LEATHERS` *and* `MP-COSTCO`). The default pick is scored, not arbitrary:

```
score(supplier) =  w_recency · recency(last_purchased_at)          # most recent buy wins
                 + w_frequency · log(1 + txn_count)                # proven, repeat vendor
                 + w_contactable · has_email_or_phone(supplier)    # we can actually reach them
                 − w_rate · normalized(last_rate)                  # cheaper, tie-break only
```

- **Contactability is a ranking term, not a hard filter** — a frequent vendor with no email
  still surfaces (top, even), but the PO is flagged `no_contact_channel` so §5/§7 route it to
  WhatsApp/call or manual rather than silently failing an email send.
- **Ties / low-confidence** (scores within a band, or only category-fallback hits) →
  **do not auto-select**: the PO is created `draft` with `needs_supplier`, the ranked
  candidates attached, and the buyer chooses. This is the "fallback when ambiguous" the
  prompt asks for — surface the shortlist, never guess the vendor on a costed order.
- **One PO per supplier, not per line.** Multiple shortfall lines that resolve to the same
  supplier are **grouped into one PO** (matching the real forms — PO-12 carries several JP
  line items to one vendor). A single BOM's shortfalls thus fan out into *N* POs, one per
  distinct resolved supplier; each `po_item` keeps its `bom_item_id`/`inventory_item_id`
  back-link (Stage-4 handoff).

### 1d. New table — `supplier_supply_history` (the matching index)

Same conventions as `models.py` (`UUIDMixin`+`TimestampMixin`, portable `GUID`, VARCHAR
str-enums, FK by table-name string). Built by the §9 supplier importer from the provision
sheet; refreshed on re-import. Registered in `alembic/env.py` per stage-0 §3.

| Field | Notes |
|---|---|
| `id`, timestamps | |
| `supplier_id` FK `supplier` | resolved from `COMPANY NAME` (get-or-create, normalized) |
| `normalized_description` (indexed) | the provision `DESCRIPTION`, run through the Stage-4 normalizer — the join key |
| `raw_description` | verbatim, for audit |
| `mode` | `LEATHER/MATERIALS/SERVICE/JOB WORK` (the provision `MODE`) → the §1b category bucket |
| `uom` | the provision `NOM` (DCM/KGS/ROLL/NOS…) — seeds the PO line UOM default |
| `txn_count` | how many times this `(supplier, article)` pair appears |
| `last_purchased_at` / `first_purchased_at` | recency for ranking |
| `last_rate` / `min_rate` / `max_rate` | seeds the PO `unit_price` default + a sanity band |
| **index** `(normalized_description)`, `(supplier_id)` | the matcher's two read paths |

> **Why a derived table, not a live scan of the provision sheet:** the ledger is 1,524
> rows today and grows; matching runs per shortfall line per approval. A pre-aggregated,
> indexed `(supplier, article)` table makes the match an index probe (Stage-4 §7 logic),
> keeps the raw ledger as an archival pg_dump (stage-0 §2 classified "Suppliers provision"
> as a (d) pg_dump artifact), and is idempotently rebuildable on each supplier re-import.

---

## 2. PO form generation — one template per supplier type

### 2a. What the real forms dictate

All 40 `suppler-po-form/*.pdf` share one skeleton (extracted from PO-01, PO-22): a fixed
**buyer block** (PAKKAR TANVEER EXPORTS, NO.1 Hyder Garden…, Chennai-600012, GST
`33AAGPA0428C1ZO`, the two cell numbers + `tanveer@ptexports.com`), a **Bill-To supplier
block** (name + address + phone + GSTIN + email), `P/O Invoice # : NN/25-26`, `DATE`, a
**buyer-ref tag** (`#STEWART`, `#KJ-SAMPLE`, `#CRIMIE ORDER` — links the PO to the buyer
order/style), a **line grid**, a **tax footer** (`Invoice Subtotal · CGST 6% · SGST 6% ·
Other · TOTAL`, all INR), and a fixed **terms** block (send 2 invoice copies; delivery 10
days; payment 60 days).

The **one part that varies by supplier type is the line-grid UOM column header and the
material vocabulary**:

| Supplier type | Real example | UOM column header | Line vocabulary |
|---|---|---|---|
| **Leather** | PO-15 INDIMICA, GLOBAL EXIM, BISMI | `DCM` / `SQF` | hide/skin articles, colour-led, rate per dcm² |
| **Accessory / material** | PO-01 M.V.P.N.K (NYLON FUSING), PO-02 SUSIL PLASTIC, PO-06 JAF | `Mtrs` / `NOS` / `ROLL` / `KGS` | fusing, zippers, buttons, tapes, linings, padding |
| **Service / job-work** | PO-22 ASTRAL (SPRAY CHEMICAL), embossing, quilting | `NOS` / lump-sum | a charge line, often qty 1 |

So **"one template per supplier type"** is real and small: the differences are the UOM
header label, the default UOM, and which material vocabulary/columns show — everything else
(buyer block, tax footer, terms, signatory) is shared boilerplate.

### 2b. Where templates live & how new ones are added

Reusing the Stage-3 export stack (WeasyPrint = Jinja2 HTML/CSS → PDF, ReportLab fallback,
rendered in `run_in_threadpool`):

- **A shared base template** `templates/po/_base.html` holds the buyer block, tax footer,
  terms, and signatory — the invariant 90% of the form.
- **Per-type child templates** extend it: `templates/po/leather.html`,
  `templates/po/accessory.html`, `templates/po/service.html` — each only overrides the
  line-grid block (UOM header + columns) and the default UOM.
- **A config registry** `config/po_templates.yaml` (seeded idempotently like the Stage-1/2/4
  YAML registries, into a small `po_template` table or read directly) maps
  `supplier_type → template_name + default_uom + gst_mode`. **Onboarding a new supplier
  type = add a child template file + one registry row; no code branch** — the same
  promote-by-config posture as every prior stage.
- **Template selection** at generate time: from the resolved supplier's dominant
  `supply_history.mode` (or an explicit `supplier.type` once curated), look up the registry
  → render that child template with the `_po_view(po)` dict the service builds.

### 2c. The tax/total math (grounded in the real forms, made config-driven)

The forms show **CGST 6% + SGST 6%** (12% intra-state, Tamil Nadu — both supplier and
buyer in TN), *not* the 2.5% the stage-0 prose guessed. Because some suppliers are
out-of-state (inter-state → **IGST 12%** instead of CGST+SGST) the rate/mode must be
**config + per-supplier**, never hard-coded:

```
subtotal = Σ (po_item.qty × unit_price)
gst_mode = INTRA  (supplier_state == buyer_state, TN)  → cgst = sgst = subtotal × rate/2   # 6% + 6%
         | INTER  (supplier_state != buyer_state)      → igst = subtotal × rate            # 12%
round_off = round(subtotal + taxes) − (subtotal + taxes)
total    = subtotal + taxes + round_off
```

`gst_rate` (default 12) and the intra/inter decision (from `supplier.gstin` state code vs
buyer `33…` = TN) live in config; `purchase_order` already has `cgst`/`sgst`/`round_off`/
`total` and gains `igst` + `gst_mode` (§10). All amounts server-computed at write time
(house rule), echoed by the editable-PO recompute (§4).

### 2d. PO numbering

`po_number` follows the real `NN(25-26)` / `NN/25-26` financial-year sequence. A
per-financial-year monotonic counter (FY runs Apr–Mar, matching the provision sheet's
`APRIL-2025` months) issues the next `NN`, formatted `PO-{NN:02d}({FY})`. The counter is
allocated **at send time, not draft time** (drafts may be discarded; the real numbered
forms are issued POs), under a row lock to avoid duplicate numbers across replicas.

---

## 3. Pre-send cross-check workflow (material-type-routed approval)

### 3a. The gate and its routing

A PO is **never emailed straight from draft**. It passes a mandatory cross-check whose
approver depends on the PO's material type — the prompt's explicit rule:

| PO material type | Cross-check approver | Why |
|---|---|---|
| **Leather** (main/sub material) | **Cutting Manager** | leather is the costliest, consumption-sensitive buy; the cutting manager already owns DCM confirmation (Stage-2 §10) and knows the real yield. |
| **Accessory / material / service** | **MD / DM / HR** (any one) | trims, linings, packaging, services — operational/commercial sign-off, not a cutting-yield question. |

A mixed-supplier PO is impossible by construction (§1c groups by supplier, and a supplier
is leather *or* accessory by `mode`), so each PO routes to exactly one approver class. MD
is superuser and may approve any PO (stage-0 §5).

### 3b. State machine (extends the existing `POStatus`)

`POStatus` today is `draft/sent/responded/confirmed/escalated/cancelled`. Stage 5 inserts
the cross-check states **`pending_approval`, `approved`, `rejected`** (VARCHAR str-enum
add — reversible, SQLite-portable, no native `ALTER TYPE`, per stage-0 §3 / `enums.py`):

```
                 submit (buyer/system)        approve (router-correct role)        send
  draft ───────────────────────────► pending_approval ──────────────────► approved ──────► sent ──► responded ──► confirmed
   ▲   ▲                                  │      │                                              │
   │   │ edit (DCM/qty/price) keeps draft │      │ reject (+reason)                             │ escalate (§7)
   │   └──────────────────────────────────┘      ▼                                              ▼
   │            reopen (revision++)            rejected                                      escalated ──► confirmed
   └─────────────────────────────────────────────┘
 (cancelled is reachable from any pre-send state)
```

| Edge | Trigger | Allowed role | Notes |
|---|---|---|---|
| `draft → pending_approval` | `POST /pos/{id}/submit` | buyer (DM) / system | requires a resolved supplier (no `needs_supplier`) and ≥1 line; emits the cross-check notification (§3c). |
| `pending_approval → approved` | `POST /pos/{id}/approve` | **Cutting Mgr** (leather) **or MD/DM/HR** (accessory); MD always | role checked against the PO's routed type; stamps `approved_by/at`; writes `PO_APPROVE` audit. |
| `pending_approval → rejected` | `POST /pos/{id}/reject` | same routed approver | mandatory `reason`; `PO_REJECT` audit. |
| `rejected → draft` / `pending_approval → draft` | edit or `reopen` | buyer/DM | a price/qty/supplier edit returns it to `draft`, `revision++`; re-approval required. |
| `approved → sent` | `POST /pos/{id}/send` (or auto on approve, config) | buyer / system | allocates `po_number`, renders PDF, sends email (§5), starts the escalation clock (§7); `PO_SEND` audit. |
| `* → cancelled` | `POST /pos/{id}/cancel` | DM / MD | pre-send only; releases nothing on its own but Stage-4 reservations are released by the BOM lifecycle, not the PO. |

### 3c. Notification flow (reuses Stage-3 infrastructure exactly)

On `draft → pending_approval`, a `notification` row is written for the routed approver(s)
— `channel=in_app`, `type=PO_AWAITING_APPROVAL` (new `NotificationType` value), `entity_type=
purchase_order`, `entity_id=po.id`, `scheduled_for = created_at + 2h`. It is delivered over
the **existing** `GET /notifications/stream` SSE + listed by `GET /notifications`; `POST
/notifications/{id}/open` cancels its escalation. If unseen at `scheduled_for`, the
**same DB-idempotent in-process sweeper** (stage-3 §2c) creates an `email` child and mails
the approver. No new infra — leather POs notify the cutting manager, accessory POs notify
MD/DM/HR, each with the 2-hour in-app→email fallback. (This is the *internal* approver
nudge; the *external* supplier escalation in §7 is the separate 5-hour ladder.)

---

## 4. Editable PO form — the same contract as the BOM

The cross-check screen lets the approver fix a line (wrong supplier, wrong rate, split a
qty) before sending. This is **not** a new edit path — it reuses the Stage-2/3 BOM contract
verbatim:

- **`PATCH /api/v1/procurement/pos/{po_id}/items`** — a single **bulk** edit endpoint
  accepting changed cells in one request, guarded by **optimistic locking on
  `purchase_order.revision`** (the version token — a new column, §10, mirroring
  `bom.revision`). `409 stale_revision` on a stale `base_revision`; the server recomputes
  `amount`/`subtotal`/GST/`round_off`/`total` and returns the full recomputed PO; one
  `PO_EDIT` audit row (before/after) per accepted bulk edit.
- **Editable fields:** `po_item.description`, `color`, `uom`, `qty`, `unit_price`
  (→ recompute `amount` → subtotal → tax → total), add/remove lines, and the PO-level
  `supplier_id` (re-resolving `needs_supplier`), `buyer_ref`, `delivery_days`,
  `payment_terms_days`. A `supplier_id` change re-routes the §3 approver class and resets
  any prior approval (`revision++`, back to `draft`).
- **Lock semantics:** once `status ∈ {sent, responded, confirmed, escalated}`, every edit
  path returns **`409 po_locked`** — a sent PO is frozen exactly as an approved BOM is
  (`bom_locked`). A correction after send is a `cancel + new PO` or a supplier-side amend,
  not an in-place edit.

> **Why reuse, not fork:** a divergent PO editor would re-implement the interdependent
> recompute (qty×price → subtotal → CGST/SGST/IGST → round-off → total) and fork the audit
> trail for what is the same logical operation as a BOM edit. One editable contract, one
> recompute shape, one audit shape — across BOM and PO.

---

## 5. Email send — provider, bounce handling, templating, attachments

### 5a. Provider choice

| Provider | Cost (transactional) | Deliverability / features | Lock-in | Verdict |
|---|---|---|---|---|
| **Amazon SES** ✅ | ~$0.10 / 1,000 emails — cheapest at any volume | strong deliverability; **bounce/complaint/delivery events via SNS**; DKIM/SPF/DMARC; attachments up to 40 MB (raw MIME) | low — plain SMTP/HTTPS API, standard MIME; the repo already leans AWS-adjacent (S3-compatible storage option, Stage 1 §6) | **recommended** |
| **Resend** | ~$20/mo for 50k then usage | excellent DX, React/HTML templates, webhooks for bounces | medium — proprietary API/templates | good fallback if SES setup friction is high |
| **SendGrid** | free 100/day then ~$20/mo | mature, template editor, event webhook | medium — proprietary | viable but pricier than SES at the factory's low volume |

**Recommendation: Amazon SES.** The PO volume is low (tens/month) and SES is by far the
cheapest with first-class **programmatic bounce/complaint feedback** — which §5b needs — and
the least lock-in (it's effectively authenticated SMTP + SNS notifications). It slots behind
the **existing Stage-3 `Notifier`/email-backend abstraction** (`app/modules/procurement/
notifier.py`): SES is one driver, the dev no-op/log driver another, selected by config
(`EMAIL_BACKEND=ses|smtp|log`, `AWS_SES_*` keys, blank-defaulted) — wiring a real provider is
config, not a code change, and the SQLite/dev path stays offline. Send runs in
`run_in_threadpool` (house async rule).

### 5b. Bounce / complaint handling

- **SES → SNS → webhook** `POST /api/v1/procurement/webhooks/ses` (signature-verified)
  receives `Bounce` / `Complaint` / `Delivery` events.
- A **hard bounce** marks the `po_response`/`notification` row `status=failed`, flags the
  `supplier.email` as `invalid` (a `supplier.email_status` column, §10), and **immediately
  promotes the PO to the WhatsApp rung of the escalation ladder (§7)** rather than waiting
  out the 5-hour window — a dead address shouldn't burn 5 hours.
- A **complaint** suppresses further email to that address and alerts the buyer.
- **Soft bounce** → one retry on the sweeper, then treat as hard.
- The very common **"supplier has no email on file"** case (§1c, ~97% of the master) is the
  same branch: no send is attempted; the PO is flagged `no_contact_channel` and routed
  straight to WhatsApp/call or held for the buyer to add a contact (§9 supplier edit).

### 5c. Templating & attachments

- The **email body** is a Jinja2 HTML template (`templates/email/po_dispatch.html`) + a
  plain-text alternative (multipart/alternative) — supplier name, PO number, buyer-ref,
  a line summary, delivery/payment terms, and a polite "please confirm" CTA.
- The **PO PDF (§2) is attached** (the rendered WeasyPrint document, stored as a
  `Document(kind=supplier_po_pdf)` and linked via `purchase_order.pdf_document_id`).
- The **open-tracking pixel + wrapped links** (§6) are injected into the HTML body at send.
- `Message-ID` and the per-send `tracking_token` are persisted on the `po_response` so
  inbound SNS events and pixel hits correlate back to the exact send.

---

## 6. Open tracking — pixel + link wrapping (self-hosted)

### 6a. Build vs buy

| Option | Mechanism | Cost | Lock-in / fit |
|---|---|---|---|
| **Mailtrack** | Gmail extension, pixel | per-seat/mo | ❌ Gmail-client-bound; tracks the *human's* outbox, not the app's server sends; no API to our DB |
| **HubSpot** | CRM + email tracking | $$$ per seat | ❌ heavy CRM lock-in for one feature; overkill |
| **Streak** | Gmail CRM, pixel | per-seat/mo | ❌ same Gmail-bound limitation as Mailtrack |
| **Yesware** | sales-email tracking | per-seat/mo | ❌ sales-rep tool; client-side; no server integration |
| **Self-hosted pixel + link-wrap** ✅ | a 1×1 GIF endpoint + redirect endpoints we own | ~$0 (a route + a table) | ✅ read-state lands in **our** `po_tracking_event`; drives the §7 ladder directly; no per-seat fee; no third party sees supplier data |

**Recommendation: self-hosted.** Every SaaS option above is a **client-side, per-seat,
Gmail-bound** tool built for a salesperson watching their own outbox — none of them can feed
"was PO-07 opened?" into the app's escalation sweeper, and all charge per user with real
lock-in. The need here is a *server-side, app-owned* read signal that the escalation state
machine can query. A self-hosted pixel + link-wrap is a few routes and one table, keeps
supplier engagement data in our own DB (a privacy/ownership win), and costs nothing per
message.

### 6b. Mechanism

- **Open pixel:** a unique `GET /api/v1/procurement/t/o/{tracking_token}.gif` 1×1
  transparent GIF embedded in the email HTML. A hit stamps the `po_response.opened_at`
  (first hit) and appends a `po_tracking_event(type=open, ip, user_agent, at)` row.
- **Link wrapping:** any link in the body (e.g. a "view PO / confirm" link) is rewritten to
  `GET /api/v1/procurement/t/c/{tracking_token}?u=<signed-target>`; the redirect endpoint
  records a `click` event (a stronger "read" signal than an open) then 302s to the target.
- **Honest caveat (documented):** image-proxy/prefetch (Gmail caches pixels; some clients
  block images) makes opens **probabilistic** — an open can be a false positive (proxy
  prefetch) or false negative (images off). A **click** is the reliable signal. The ladder
  (§7) treats *open* as the soft trigger (→ WhatsApp) and reserves the strong action
  (auto-call) for *no read at all*, and **any inbound acknowledgement** (a reply, a portal
  confirm-click, a WhatsApp reply) is the authoritative stop — so a missed pixel only ever
  *over*-escalates by one cheap rung, never under-delivers.

### 6c. New table — `po_tracking_event`

| Field | Notes |
|---|---|
| `id`, timestamps | |
| `purchase_order_id` FK `purchase_order` | |
| `po_response_id?` FK `po_response` | the specific send this event belongs to |
| `tracking_token` (indexed) | the per-send opaque token in the pixel/links |
| `event_type` | `open` / `click` / `delivery` / `bounce` / `complaint` |
| `channel` | `email` / `whatsapp` / `call` (so the table is the unified engagement log) |
| `ip`, `user_agent`, `meta` JSONB | forensic detail; `meta` holds SNS/Twilio payload refs |
| `at` | server-side `datetime.now(timezone.utc)` |

`purchase_order` gains `tracking_token` + `first_opened_at`/`first_clicked_at` convenience
stamps (§10); the per-send detail lives in this event table.

---

## 7. Escalation ladder — email → WhatsApp → auto-call

### 7a. The ladder and its timers

The doc's supplier-chase requirement, made concrete (the **5-hour** external window is kept,
per the product owner, distinct from Stage-3's 2-hour internal nudge):

```
 PO sent (email) ─► wait 5h
        │  opened?      ── yes ─► wait for reply/confirm (no further auto-escalation; reminder only)
        │  no open
        ▼
 RUNG 1: WhatsApp message (Twilio) ─► wait 5h
        │  read/replied? ── yes ─► STOP (acknowledged)
        │  no read
        ▼
 RUNG 2: auto-call (Twilio Voice, TTS reads PO ref) ─► log outcome
        │  answered/confirmed ─► STOP (acknowledged)
        │  no answer
        ▼
 RUNG 3: surface to the buyer (in-app PO_ESCALATION_EXHAUSTED) — human takes over
```

- **Hard-bounce / no-email** short-circuits straight to RUNG 1 (§5b).
- Each rung writes a `po_response` row (`channel ∈ {email, whatsapp, call}`) + a
  `po_tracking_event`, and flips `POStatus → escalated` on first escalation.

### 7b. Transport choice — Twilio for both WhatsApp and Voice

| Need | Option | Verdict |
|---|---|---|
| WhatsApp | **Twilio WhatsApp API** vs **Meta WhatsApp Cloud API (direct)** | **Twilio** — one vendor + one SDK already needed for Voice; Meta Cloud API is cheaper per message but adds a second integration, template-approval workflow, and Business-Manager setup. At factory volume the per-message delta is negligible; **one transport (Twilio) for WhatsApp *and* Voice** wins on simplicity. Meta-direct is the documented cost-optimization if volume grows. |
| Auto-call | **Twilio Voice** (TTS via TwiML `<Say>` reading the PO number + a "press 1 to confirm" gather) | the call outcome (answered/confirmed/no-answer/voicemail) posts back to a `POST /webhooks/twilio/voice` status callback → `po_response`. |

Both behind a thin transport abstraction (like the email `Notifier`): `EscalationTransport`
with `send_whatsapp(to, body)` / `place_call(to, twiml)`; a dev **log/no-op driver** so the
ladder is testable offline; real Twilio selected by config (`TWILIO_*` keys, blank-defaulted).
Sends run in `run_in_threadpool`.

### 7c. The driver — the existing DB-idempotent sweeper (no new scheduler)

The ladder is driven by the **same in-process `asyncio` sweeper** Stage 3 introduced —
extended, not duplicated. State lives entirely in `notification`/`po_response`/`purchase_order`
rows so it is **idempotent and restart-safe**:

```
sweep_po_escalations():  # every ~60s, in main.py lifespan (alongside the BOM sweep)
  due = SELECT po WHERE status IN ('sent','escalated')
          AND next_escalation_at <= now()
          AND acknowledged_at IS NULL
          AND current_rung < 3
  for po in due:
     advance one rung (whatsapp → call → exhausted), write po_response + tracking_event,
     set next_escalation_at = now() + 5h, current_rung++   # one rung per sweep, never double
```

- `purchase_order` gains `current_rung`, `next_escalation_at`, `acknowledged_at`,
  `acknowledged_channel` (§10). The `NOT … acknowledged` + `current_rung < 3` guards make a
  second sweep a no-op — the same idempotency proof as Stage 3's email guard.
- **Single-replica caveat (documented, mirrors Stage 3 / the login limiter):** one sweeper
  per process double-fires under >1 replica; the fix is `SELECT … FOR UPDATE SKIP LOCKED` on
  due POs or an external worker. Called out, not built.

### 7d. How the ladder stops (the acknowledgement contract)

The ladder **halts the instant the supplier acknowledges by *any* channel** — the prompt's
"how to stop it once acknowledged":

| Stop signal | Source | Effect |
|---|---|---|
| Email reply / portal confirm-click | inbound mail parse or the §6 link redirect | `acknowledged_at` stamped, `acknowledged_channel=email`, `POStatus → confirmed` |
| WhatsApp reply | Twilio inbound webhook | `acknowledged_channel=whatsapp`, status `confirmed` |
| Call "press 1 to confirm" / agent logs it | Twilio Voice gather callback | `acknowledged_channel=call`, status `confirmed` |
| Buyer marks it confirmed manually | `POST /pos/{id}/acknowledge` (DM/MD) | manual override; same stamp |

Once `acknowledged_at` is set, the sweeper's `acknowledged_at IS NULL` filter excludes the
PO forever — no further WhatsApp or calls. Acknowledgement optionally captures
`confirmed_qty`/expected-ship-date onto the `po_response` for the §8 production board. A
single PO confirmed at RUNG 0 (opened + replied) never reaches WhatsApp; the ladder is
strictly "escalate only while silent."

---

## 8. Order / style tracking through production

### 8a. What it is

A single board answering "where is every style in the pipeline?" — from BOM through PO to
material-in to production release. It stitches the procurement stages (this module) to the
existing `production` event stream (Stage 6 territory) **without** procurement touching the
production repository (cross-module reads go through `production.service` / `clients.service`
per CLAUDE.md §3.2).

### 8b. New table — `production_tracking` (one row per style/order)

| Field | Notes |
|---|---|
| `id`, timestamps | |
| `client_order_id` FK `client_order`, `style_id` FK `style` | the tracked unit |
| `bom_id?` FK `bom` | the BOM driving it |
| `status` | `ProductionTrackingStatus` (str-enum, §8c) |
| `po_count` / `po_confirmed_count` | rollup of this style's POs (from §1c grouping) |
| `material_ready_at?` | when all shortfalls are confirmed/received → the Stage-6 trigger |
| `released_at?` | when released to the shop floor |
| `updated_by?` FK `app_user` | who last advanced it (system or human) |
| **unique** `(client_order_id, style_id)` | one tracker per style per order |

### 8c. Status ladder & transitions

```
 AWAITING_BOM ─► BOM_APPROVED ─► INVENTORY_CHECKED ─► PO_RAISED ─► PO_CONFIRMED
   ─► MATERIAL_READY ─► RELEASED_TO_PRODUCTION ─► IN_PRODUCTION ─► COMPLETED
```

| Transition | Driver | Who can update |
|---|---|---|
| `→ BOM_APPROVED` | Stage-3 approve edge | system |
| `→ INVENTORY_CHECKED` | Stage-4 check completes | system |
| `→ PO_RAISED` | first PO for the style sent (§3 `approved → sent`) | system |
| `→ PO_CONFIRMED` | **all** the style's POs `confirmed` (§7d) | system |
| `→ MATERIAL_READY` | all shortfalls confirmed/received → emits the Stage-6 `MATERIAL_READY` MD alert | system (Stage-6 owns the alert) |
| `→ RELEASED_TO_PRODUCTION` | MD/DM releases | **MD / DM** (manual) |
| `→ IN_PRODUCTION` / `→ COMPLETED` | derived from `production_event` progress via `production.service` | system (read-through) + MD/DM manual override |

- **System-driven by default, human-overridable.** Each procurement stage advances the
  tracker as a service→service side-effect; MD/DM can manually nudge `RELEASED_TO_PRODUCTION`
  (a real human go/no-go) and override a stuck status. Every manual change writes a
  `PRODUCTION_TRACKING_UPDATE` audit row (actor + before/after).
- **Read-only roles:** `VIEWER`/`CLIENT` (scoped to own orders) see the board; only
  DM/MD/Cutting can transition. The board reuses the Stage-4 grouped-dashboard shape
  (client → order → style → badge) so the FE renders one consistent procurement overview.
- New `ProductionTrackingStatus` enum (VARCHAR str-enum, module convention) + the
  `production_tracking` table; additive reversible migration, registered in `alembic/env.py`.

---

## 9. Supplier CRUD — add, edit, soft-delete, audit

### 9a. Ingestion (the first "create" is the importer)

A recurring, idempotent importer mirroring Stage-4's inventory sync (preview + commit, in
`run_in_threadpool`):

| Method & path (under `/api/v1/procurement`) | Purpose | Auth |
|---|---|---|
| `POST /suppliers/import/preview` | parse `SUPPLIERS_updated (2).xlsx` → normalized supplier rows + the rebuilt `supplier_supply_history`; warnings (NULL phones, blank emails, free-text services), no writes | DM / MD |
| `POST /suppliers/import/commit` | upsert `supplier` (keyed on normalized `name`) + rebuild `supplier_supply_history` from the provision sheet | DM / MD |

Upsert rule mirrors Stage-4 §2c: contact fields **sheet-wins-if-present, else keep**
(never blank a known email with a sheet gap); rows absent from a re-import **soft-deactivate**
(`is_active=False`), never hard-delete (open POs/history reference them). `NULL`/blank
phone/email are stored as null + flagged so §1c/§5b can route around them.

### 9b. CRUD endpoints

| Method & path | Purpose | Auth |
|---|---|---|
| `GET /suppliers?q=&service=&active=` | searchable/paged list (the §1c picker + admin view) | DM / MD / VIEWER (read) |
| `GET /suppliers/{id}` | one supplier + its supply history + open POs | DM / MD / VIEWER |
| `POST /suppliers` | **add** a supplier (the §1c "no match → add new vendor" path) | DM / MD |
| `PATCH /suppliers/{id}` | **edit** (add the missing email/phone, fix GSTIN, set type, payment terms) | DM / MD |
| `DELETE /suppliers/{id}` | **soft-delete** → `is_active=False` (never physical delete) | MD |
| `POST /suppliers/{id}/reactivate` | undo a soft-delete | MD |

### 9c. Rules & audit

- **Dedupe / integrity:** `supplier.name` is already `unique`; create/edit normalize the
  name and also warn on a near-duplicate GSTIN (the same vendor entered twice). Editing a
  contact onto a previously contactless matched vendor immediately unblocks §5 email send /
  clears `no_contact_channel`.
- **Soft-delete only:** a supplier referenced by any `purchase_order` or
  `supplier_supply_history` row can never be hard-deleted (FK integrity + audit). Soft-delete
  hides it from the §1c matcher (excluded from ranking) but preserves history.
- **Audit:** every mutation writes an `audit_log` row — `SUPPLIER_CREATE` / `SUPPLIER_EDIT`
  / `SUPPLIER_DEACTIVATE` / `SUPPLIER_REACTIVATE`, actor + server-side `at` + before/after
  JSONB. Append-only, consistent with the Stage-3 audit guarantee.

---

## 10. Schema deltas for Stage 5 (patch to stage-0 §1e/§1f)

Stage-0 already defined `supplier`, `purchase_order`, `po_item`, `po_response`,
`notification`, `audit_log`. Stage 5 adds (all additive, reversible, VARCHAR-enum
migrations registered in `alembic/env.py` per stage-0 §3 — **no native `ALTER TYPE`**):

- **New tables:** `supplier_supply_history` (§1d), `po_tracking_event` (§6c),
  `production_tracking` (§8b), and (optional) `po_template` if the §2b registry is a table
  rather than read-from-YAML.
- **`purchase_order` gains:** `revision` Int (optimistic-lock token, §4), `igst`
  Numeric(12,2) + `gst_mode` String(10) (§2c), `created_by` FK `app_user`, the cross-check
  stamps `approved_by`/`approved_at`/`rejected_by`/`rejected_at`/`rejection_reason` (§3b),
  the tracking stamps `tracking_token`/`first_opened_at`/`first_clicked_at` (§6c), and the
  escalation state `current_rung`/`next_escalation_at`/`acknowledged_at`/`acknowledged_channel`
  (§7c).
- **`supplier` gains:** `email_status` String(20) (`valid`/`invalid`/`unknown`, set by §5b
  bounce feedback), `supplier_type` String(20) (curated leather/accessory/service, defaulting
  from the dominant history `mode`), `state_code` String(2) (the GSTIN state → §2c intra/inter
  GST), and a `whatsapp_phone` String(50) (often the same as `phone`).
- **`po_response` gains:** `tracking_token` (correlates to §6 events) — `channel` already
  carries email/call and now also `whatsapp`.
- **Enum additions (VARCHAR, reversible):**
  - `POStatus` += `PENDING_APPROVAL`, `APPROVED`, `REJECTED` (§3b).
  - `POResponseChannel` += `WHATSAPP` (§7).
  - `NotificationChannel` += `WHATSAPP`; `NotificationType` += `PO_AWAITING_APPROVAL`,
    `PO_ESCALATION_WHATSAPP`, `PO_ESCALATION_EXHAUSTED` (`PO_DISPATCH`/`ESCALATION_CALL`
    already exist).
  - New `ProductionTrackingStatus` (§8c).
- **New audit actions** (strings, no schema change to `audit_log`): `PO_GENERATE`,
  `PO_EDIT`, `PO_SUBMIT_FOR_APPROVAL`, `PO_APPROVE`, `PO_REJECT`, `PO_SEND`, `PO_ESCALATE`,
  `PO_ACKNOWLEDGE`, `PO_CANCEL`, `SUPPLIER_CREATE`/`EDIT`/`DEACTIVATE`/`REACTIVATE`,
  `PRODUCTION_TRACKING_UPDATE`.

---

## 11. Acceptance criteria (using the real `data/` files)

A reviewer must be able to confirm each:

1. **Matching is deterministic-first, never a silent guess.** A shortfall `SHEEP NAPPA
   BLACK` resolves (via `supplier_supply_history`) to `BISMI LEATHERS` ranked above
   `MP-COSTCO` by recency/frequency; `GOAT SUEDE` → `GLOBAL EXIM`; `TAFFTA LINING` →
   `M.V.P.N.K`. An article with no history/alias hit yields a `needs_supplier` PO with a
   ranked shortlist — not a fabricated vendor. A frequent vendor with no email still ranks
   but the PO is flagged `no_contact_channel`.
2. **One PO per supplier, grouped.** Multiple shortfall lines of one BOM resolving to the
   same supplier produce **one** PO with multiple `po_item` rows, each keeping its
   `bom_item_id`/`inventory_item_id` link.
3. **Template per supplier type.** A leather PO renders the `DCM`/`SQF` grid; an accessory
   PO (e.g. M.V.P.N.K NYLON FUSING) renders the `Mtrs`/`NOS` grid — both off the shared base
   (buyer block, `CGST 6% + SGST 6%`, 10-day delivery / 60-day terms). Adding a new
   supplier-type template is a file + a `po_templates.yaml` row, no code change.
4. **GST math reproduces the forms.** Intra-state `subtotal 6,500 → CGST 390 + SGST 390 →
   TOTAL 7,280` (PO-01); an inter-state supplier produces `IGST` instead, driven by config +
   the supplier's GSTIN state code.
5. **Cross-check routes by material type.** A leather PO requires **Cutting Manager**
   approval (MD superuser); an accessory PO requires **MD/DM/HR**; the wrong role gets `403`;
   approval emits/uses the SSE `PO_AWAITING_APPROVAL` notification with the 2-hour in-app→email
   fallback.
6. **Editable PO = the BOM contract.** `PATCH /pos/{id}/items` with a stale `base_revision`
   → `409 stale_revision`; a fresh edit recomputes GST/total and bumps `revision`; a `sent`
   PO rejects edits (`409 po_locked`).
7. **Email + bounce.** A PO sends via the SES driver with the PDF attached; an SES hard-bounce
   webhook flags `supplier.email_status=invalid` and short-circuits the PO to the WhatsApp
   rung; a supplier with no email never attempts a send (`no_contact_channel`).
8. **Open tracking is self-hosted.** The pixel hit stamps `first_opened_at` + a
   `po_tracking_event(open)`; a wrapped-link click records `click`; no third-party SaaS is
   used; the probabilistic-open caveat is handled (a click is authoritative).
9. **Escalation ladder + stop.** No open in 5 h → a WhatsApp `po_response`/event (Twilio
   driver); still no read → an auto-call rung; **any** acknowledgement (email reply / WhatsApp
   reply / call confirm / manual) stamps `acknowledged_at` and the sweeper never escalates that
   PO again; the sweep is idempotent (a second pass double-fires nothing).
10. **Production tracking.** A style advances `BOM_APPROVED → INVENTORY_CHECKED → PO_RAISED →
    PO_CONFIRMED → MATERIAL_READY`; system drives the procurement edges, MD/DM manually
    release; every manual change is audited; `VIEWER`/`CLIENT` read-only.
11. **Supplier CRUD + audit.** Import is idempotent (second commit = identical rows; absent
    rows soft-deactivate); add/edit/soft-delete each write a `SUPPLIER_*` audit row; a
    soft-deleted supplier is excluded from matching but its history/POs survive; editing a
    contact onto a matched vendor unblocks email send.

---

## 12. Review checklist (definition of done — spec only, nothing to run)

- [ ] Supplier matching is grounded in the **real two-sheet** structure (sparse
      `Supplier contact info` + the `Suppliers provision` ledger), picks **historical-ledger
      lookup → alias → category/MODE → advisory fuzzy → manual**, ranks ties by
      recency/frequency/contactability, and defines the **ambiguity fallback** (surface the
      shortlist, never guess); the `supplier_supply_history` index is specified.
- [ ] One PO template per supplier type (leather / accessory / service) is justified from the
      real forms (the varying UOM header), lives as shared-base + per-type Jinja templates +
      a config registry, reuses the Stage-3 WeasyPrint stack, and new types onboard by config;
      GST math (CGST+SGST vs IGST) is config-driven and matches the forms (12%, not 2.5%).
- [ ] The pre-send cross-check is a mandatory, **material-type-routed** gate (leather→Cutting,
      accessory→MD/DM/HR) as a state machine extending `POStatus`, with the Stage-3 SSE
      notification + 2-hour internal sweeper flow.
- [ ] The editable PO **reuses the BOM bulk-PATCH + optimistic `revision`** contract verbatim
      (server recompute, `409 stale_revision`/`409 po_locked`), no forked edit logic.
- [ ] Email picks a provider (**SES**) with cost/lock-in reasoning behind the existing
      `Notifier` abstraction; bounce/complaint handling (SNS webhook → flag + short-circuit)
      and the no-email branch; HTML+text templating; the PO PDF attached.
- [ ] Open tracking compares Mailtrack/HubSpot/Streak/Yesware/self-hosted and recommends
      **self-hosted pixel + link-wrap** (cost, lock-in, app-owned read-state) with the
      `po_tracking_event` table and the probabilistic-open caveat.
- [ ] The escalation ladder (email → 5 h → WhatsApp → no read → auto-call) is a state machine
      on the DB-idempotent sweeper, with **Twilio** for both WhatsApp + Voice (Meta-direct
      noted as the cost option), the single-replica caveat, and an explicit **stop-on-any-
      acknowledgement** contract.
- [ ] Order/style production tracking is a `production_tracking` table with an explicit status
      ladder, system-driven + MD/DM-overridable transitions, audited, respecting the
      cross-module service rule.
- [ ] Supplier CRUD covers idempotent import, add/edit, **soft-delete only**, dedupe, and a
      `SUPPLIER_*` audit trail.
- [ ] Schema deltas listed as additive, reversible, VARCHAR-enum migrations registered in
      `alembic/env.py`; no native `ALTER TYPE`; the existing `supplier`/`purchase_order`/
      `po_item`/`po_response` columns reused, new columns/tables/enums enumerated.
- [ ] No code, migrations, or model edits were produced — spec only.
- [ ] Reviewed / signed off by MD / system stakeholder.
