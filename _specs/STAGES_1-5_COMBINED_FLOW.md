# BOM Procurement Workflow — Stages 1→5 Combined Flow (Developer Guide)

> **Audience:** new engineers who need to understand the whole procurement pipeline in one read.
> **What this is:** the end‑to‑end story of how a buyer order becomes a costed BOM, gets approved,
> is checked against stock, and turns into supplier purchase orders — with **every database table,
> its fields, the business logic, and exactly when/where each table is read and written.**
> **Pairs with:** the per‑stage specs (`stage-0..5-*.md`) for rationale, and `CLAUDE.md` for house rules.

---

## 0. The big picture in one paragraph

A **Direct Manager (DM)** uploads two files — an **order sheet** and a **spec sheet** — that are paired,
identity‑validated, virus‑scanned, and stored (**Stage 1**). When both are present and clean, an **AI
engine** extracts measurements, resolves how much leather each garment consumes (**DCM**), prices every
line, and produces an editable **BOM** shaped like the sample `BMO-1.pdf` (**Stage 2**). The **cutting
manager** confirms the consumption numbers, then the **Managing Director (MD)** reviews, edits if needed,
and **approves + locks** the BOM, which is exported to a PDF (**Stage 3**). Approval auto‑fires an
**inventory check** that matches every material line to warehouse stock, reserves what's available, and
emits a **shortfall** per line (**Stage 4**). Each shortfall is resolved to a **supplier** from purchase
history, grouped into one **purchase order** per supplier, cross‑checked, sent by email, and chased
through a **WhatsApp → call escalation ladder** until the supplier acknowledges (**Stage 5**). A
production‑tracking board follows each style the whole way through.

```
 STAGE 1            STAGE 2              STAGE 3            STAGE 4              STAGE 5
 upload+validate ─► AI BOM generation ─► MD approve+lock ─► inventory check  ─► supplier POs
 (procurement)      (bom)                (bom)              (inventory)          (supplier_po)
 submission         bom + bom_item       status machine     inventory_check      purchase_order
 document           pom_measurement      audit_log          + lines + reserve    + po_item
 client_template    spec_sheet           notification       shortfall lines ────► escalation ladder
                    dcm memory           PDF export                                production_tracking
```

---

## 1. Module map — who owns which stage

The codebase is a **modular monolith**. Each domain is a self‑contained module
(`models / repository / service / router / schemas / enums`). The **one hard rule**: cross‑module access
goes through another module's `service.py` — never its `repository`, `models`, or `router`.

| Stage | Module | Folder | Owns |
|---|---|---|---|
| 1 — Upload & Validation | **procurement** | `app/modules/procurement/` | `submission`, `client_template`; pairing, identity validation, virus scan, dedupe, storage |
| 2 — BOM Generation | **bom** | `app/modules/bom/` | `bom`, `bom_item`, `spec_sheet`, POM tables, DCM memory; extraction, DCM resolution, costing |
| 3 — Approval | **bom** | `app/modules/bom/` | the BOM state machine, notifications, audit, PDF export |
| 4 — Inventory Check | **inventory** | `app/modules/inventory/` | `inventory_item`, `inventory_check(_line)`, `inventory_reservation`, alias/UOM tables |
| 5 — Supplier PO | **supplier_po** | `app/modules/supplier_po/` | `supplier`, `purchase_order`, `po_item`, `po_response`, tracking, escalation, `production_tracking` |
| cross‑cutting | **core** | `app/core/` | `document`, `notification`, `audit_log` tables; `storage.py`, `notifier.py`, `security.py` |

**Why `document` / `notification` / `audit_log` live in `core`:** all four procurement modules read and
write them (Stage 1 stores Documents; Stage 3 exports a Document + writes Notifications; every stage
writes AuditLog). Hosting them in `core` means a module can persist one without importing another module's
models — preserving the strict boundary.

---

## 2. The role model (who may do what)

Roles live in `app/core/enums.py::UserRole`. The procurement‑relevant ones:

| Role | In the workflow |
|---|---|
| `MANAGING_DIRECTOR` (MD) | **Superuser + sole BOM approver.** Approves/rejects BOMs, approves accessory POs, releases to production. Bypasses every role gate. |
| `DIRECT_MANAGER` (DM) | **Stage‑1 uploader + operational oversight.** Uploads documents, edits draft BOMs/POs, manages suppliers/inventory. Cannot give final BOM approval. |
| `CUTTING_MANAGER` | Confirms BOM DCM (Stage‑2 gate); approves **leather** POs (Stage‑5 cross‑check). |
| `HR` | Approves accessory POs (with MD/DM); reads employee/attendance data. |
| `CLIENT` | Read‑only, scoped to their own `client_id`. |
| `VIEWER` | Read‑only across dashboards. |

Enforcement = route‑level `require_roles(...)` + ownership scoping (CLIENT filtered to own `client_id`).
Every privileged action (approve, lock, send) writes an `audit_log` row.

---

## 3. Complete database catalog

Conventions for every table: GUID primary key (`id`) + `created_at`/`updated_at` (mixins in
`core/models.py`); money is `Numeric(12,2)`, fractional qty is `Numeric(12,3)`; enums are stored as the
string `.value` in a `VARCHAR` (never native PG enums) so they stay reversible and SQLite‑portable;
semi‑structured data is `JSON_VARIANT` (JSONB on Postgres, JSON on SQLite). **Cross‑module foreign keys
are declared by table‑name string only** — no Python import of the other module.

### 3.1 Cross‑cutting tables (`core/models.py`)

**`document`** — every uploaded or rendered artifact.
| Field | Meaning |
|---|---|
| `client_id?` | owning client (often null until Stage 2 resolves it) |
| `kind` | ORDER_SHEET / SPEC_SHEET / BOM_QUOTE / SUPPLIER_PO_PDF |
| `filename`, `mime`, `page_count`, `size_bytes` | file metadata |
| `storage_url` | backend‑agnostic URI (written **only after** a clean scan promotes the file) |
| `sha256` (unique) | content hash → dedupe **and** the LLM‑extraction cache key |
| `uploaded_by` | the DM/MD |
| `submission_id` | membership FK back to the Stage‑1 batch |
| `validation_status` | PENDING / ACCEPTED / REJECTED / SUPERSEDED |
| `classified_kind`, `classified_spec_type`, `classification_method`, `classification_confidence`, `client_match_code` | Stage‑1 validation result |
| `scan_status` | clean / skipped / infected / error |
| `validation_signals` JSON | matched/missing signals returned in the API envelope |

**`notification`** — every alert (BOM‑ready, PO‑approval, PO‑dispatch, escalation, material‑ready).
| Field | Meaning |
|---|---|
| `recipient_user_id?` / `supplier_id?` | who it's for (an internal user or a supplier) |
| `channel` | IN_APP / EMAIL / CALL / WHATSAPP |
| `type` | BOM_AWAITING_REVIEW / PO_AWAITING_APPROVAL / PO_DISPATCH / ESCALATION_CALL / … |
| `subject`, `body` | rendered message |
| `entity_type`, `entity_id` | polymorphic ref (e.g. `bom`/`<bom id>`) → deep link |
| `status` | PENDING → SENT → OPENED → RESPONDED / FAILED |
| `scheduled_for` | escalation deadline (`created_at + 2h`) |
| `opened_at` | set when seen — **cancels** escalation |
| `parent_notification_id?` | chains a child (e.g. the email that follows an unseen in‑app notice) |

**`audit_log`** — append‑only "who did what when" with a before/after JSON diff.
`actor_user_id`, `action` (BOM_APPROVE, PO_SEND, …), `entity_type`, `entity_id`, `before`, `after`, `at`
(server‑side UTC). Rows are never updated or deleted.

### 3.2 Stage 1 tables (`procurement/models.py`)

**`submission`** — the upload‑batch that **pairs** an order sheet with a spec sheet.
`client_id?`, `created_by`, `status` (OPEN → COMPLETE → CONSUMED/REJECTED), `order_document_id?`,
`spec_document_id?` (the two **current slot pointers**), `client_order_id?` (filled in Stage 2).
The pairing key is the `submission_id` (the order does not exist yet at upload).

**`client_template`** — a per‑client × doc‑kind **validation profile**, seeded from
`config/client_templates.yaml`. `client_code`+`doc_kind` (unique), `language`, `size_system`, `currency`,
`expected_layout`, `accepted_mime` JSON, `anchors` JSON (weighted keyword signals), `fingerprints` JSON
(regexes), `grid_signals`, `thresholds`, `is_active`. Onboarding a new client = add one row, no code change.

### 3.3 Stage 2/3 tables (`bom/models.py`)

**`spec_sheet`** — a parsed spec sheet. Two real shapes (Japanese measurement grid vs Italian narrative
tech pack) share almost no columns, so the data lives in JSON behind a `spec_type` discriminator.
`client_id`, `style_id?`, `source_document_id?`, `spec_type`, `season`, `customer_label`,
`measurements` JSON, `attributes` JSON, `instructions` JSON, `extracted_by`, `confidence`.

**`bom`** — the BMO‑1 header. **UNIQUE `(client_order_id, style_id)`** — one BOM per style per order.
| Field group | Fields |
|---|---|
| identity | `client_order_id`, `style_id`, `currency`, `order_qty`, `source_document_id?` |
| money | `garment_fob_price` (Σ per‑garment lines), `bulk_total` |
| state machine | `status` (DRAFT → READY_FOR_REVIEW → APPROVED/REJECTED → EXPORTED), `revision` |
| cutting gate | `cutting_confirmed_by`, `cutting_confirmed_at`, `garment_type_id?`, `dcm_base_size?` |
| approval | `approved_by`, `approved_at`, `locked_at` |
| rejection | `rejected_by`, `rejected_at`, `rejection_reason` |
| export | `export_document_id?`, `exported_at` |

**`bom_item`** — one BMO‑1 line.
`bom_id`, `category` (MAIN_MATERIAL / SUB_MATERIAL / LINING / INTERLINING / THREAD / ACCESSORY /
MANUFACTURING / PACKAGING / FOB_CHARGE), `name`, `material_color`, `qty_per_garment` (**the DCM**),
`uom`, `unit_price`, `bulk_qty` (= `order_qty × qty_per_garment`), `total_cost`, `annotation`,
`source_ref`, `dcm_source` (template / similar_style / ai_estimate / manual), `dcm_confidence`.

**DCM support tables:**
- **`garment_type`** — per‑type config: `code`, `required_poms` JSON, `area_formula` JSON,
  `default_wastage_pct` (only used by the last‑resort Source‑3 estimate). Seeded from YAML.
- **`pom_dictionary`** — maps a native term (渡り幅, inseam, girovita) → a standard `pom_code`.
  `language`, `source_term`, `pom_code`, `garment_type_id?`, `weight`; unique `(language, source_term,
  garment_type_id)`. A new client term is a row.
- **`pom_measurement`** — one standardized POM, per spec sheet, per size. `spec_sheet_id`, `size`,
  `pom_code`, `value`, `pitch?`, `tolerance?` JSON, `source_term`, `extracted_by`, `confidence`;
  unique `(spec_sheet_id, size, pom_code)`.
- **`style_consumption_template`** — the **DCM memory**. Keys on a **cross‑order‑stable**
  `style_signature` (NOT `style_id`, which is order‑scoped) so the *second* order of a style is a hit.
  `client_id`, `style_signature`, `garment_type_id?`, `material_category`, `size`, `dcm_value`, `uom`,
  `confirmed_by`, `confirmed_at`, `source_bom_id?`; unique on the 5‑tuple. Back‑filled by the cutting gate.
- **`pattern_reference`** — "follow existing pattern X in size Y". `client_id`, `spec_sheet_id?`,
  `pattern_code`, `base_size?`, `resolved_style_id?`, `resolved_template_id?`, `notes`.

### 3.4 Stage 4 tables (`inventory/models.py`)

**`inventory_item`** — the normalized, deduped INVENTORY master (live, mutable stock).
`description`, `normalized_key` (the matching key), `uom`, `qty_on_hand`, `rate`, `category`, `color`,
`article_ref`, `is_active`.

**`inventory_check`** — one stock‑status report per approved BOM. `bom_id`, `status` (RUNNING/COMPLETE),
`run_at`, `run_by`.

**`inventory_check_line`** — per‑BOM‑item result. `inventory_check_id`, `bom_item_id`,
`inventory_item_id?` (null when unmatched), `required_qty`, `on_hand_qty`, `shortfall_qty`,
`status` (SUFFICIENT / PARTIAL / OUT_OF_STOCK), `matched_method` (key/alias/manual), `flags` JSON
(unmatched / uom_mismatch / suggestion).

**`inventory_reservation`** — a **soft** stock allocation. `available = qty_on_hand − Σ active
reservations`; never mutates `qty_on_hand`. `inventory_item_id`, `bom_id`, `inventory_check_line_id?`,
`qty`, `status` (ACTIVE/RELEASED/CONSUMED), `released_at?`, `released_reason?`.

**`material_alias`** — curated BOM‑term → inventory‑key synonym (e.g. `SHEEP GLASS → SHEEP NAPPA`).
Seeded from YAML; confirming a fuzzy suggestion writes one. unique `(bom_term)`.

**`uom_conversion`** — unit reconciliation: `from_uom`, `to_uom`, `factor`; unique `(from_uom, to_uom)`.

### 3.5 Stage 5 tables (`supplier_po/models.py`)

**`supplier`** — the vendor directory. `name` (unique), `phone`, `email`, `service`, `gstin`, `address`,
`currency` (INR), `payment_terms_days` (60), `lead_time_days` (10), `is_active`, `email_status`
(valid/invalid/unknown), `supplier_type` (leather/accessory/service), `state_code` (GSTIN state →
intra/inter GST), `whatsapp_phone`.

**`supplier_supply_history`** — the **matching index**, pre‑aggregated from the purchase ledger.
`supplier_id`, `normalized_description` (same normalizer as Stage 4 — the join key), `raw_description`,
`mode` (LEATHER/MATERIALS/SERVICE/JOB WORK), `uom`, `txn_count`, `first/last_purchased_at`,
`last/min/max_rate`; unique `(supplier_id, normalized_description)`.

**`purchase_order`** — the supplier PO (PAKKAR → supplier).
| Field group | Fields |
|---|---|
| identity | `po_number` (allocated **at send**), `supplier_id?`, `bom_id?`, `client_order_id?`, `buyer_ref`, `issue_date`, `delivery_days`, `payment_terms_days`, `currency` |
| money | `subtotal`, `cgst`, `sgst`, `igst`, `gst_mode` (INTRA/INTER), `round_off`, `total` |
| matching | `needs_supplier`, `no_contact_channel`, `match_method`, `candidates` JSON |
| state machine | `status` (DRAFT → PENDING_APPROVAL → APPROVED → SENT → RESPONDED → CONFIRMED / ESCALATED / REJECTED / CANCELLED), `revision` |
| approval | `created_by`, `approved_by`, `approved_at`, `rejected_by`, `rejected_at`, `rejection_reason` |
| send + tracking | `sent_at`, `pdf_document_id?`, `tracking_token`, `first_opened_at`, `first_clicked_at` |
| escalation | `current_rung`, `next_escalation_at`, `acknowledged_at`, `acknowledged_channel` |

**`po_item`** — a PO line. `purchase_order_id`, `item_no`, `description`, `color`, `uom`, `qty`,
`unit_price`, `amount`, `inventory_item_id?`, `bom_item_id?` (the back‑links to the shortfall it fulfils).

**`po_response`** — one send/contact attempt. `purchase_order_id`, `channel` (email/whatsapp/call),
`sent_at`, `responded_at?`, `confirmed_qty?`, `status` (PENDING/OPENED/CONFIRMED/NO_RESPONSE), `notes`,
`tracking_token`, `message_id`.

**`po_tracking_event`** — the unified engagement log. `purchase_order_id`, `po_response_id?`,
`tracking_token`, `event_type` (open/click/delivery/bounce/complaint), `channel`, `ip`, `user_agent`,
`meta` JSON, `at`.

**`production_tracking`** — one row per style/order advancing through the production ladder.
`client_order_id`, `style_id`, `bom_id?`, `status` (AWAITING_BOM → BOM_APPROVED → INVENTORY_CHECKED →
PO_RAISED → PO_CONFIRMED → MATERIAL_READY → RELEASED_TO_PRODUCTION → IN_PRODUCTION → COMPLETED),
`po_count`, `po_confirmed_count`, `material_ready_at?`, `released_at?`, `updated_by?`;
unique `(client_order_id, style_id)`.

---

## 4. Stage‑by‑stage flow — business logic + read/write points

The notation **R:** = tables read, **W:** = tables written.

### Stage 1 — Upload & Validation  *(module: procurement)*

**Goal:** pair an order sheet + spec sheet, prove each file *is* what it claims, make it safe and durable,
and open the gate to Stage 2.

**Endpoints** (all under `/api/v1/procurement`, `require_roles(DIRECT_MANAGER, MANAGING_DIRECTOR)`):
`POST /submissions`, `POST /submissions/{id}/order-sheet`, `POST /submissions/{id}/spec-sheet`,
`GET /submissions/{id}`, `GET /submissions/{id}/documents/{doc_id}`.

**Flow for one slot upload:**
1. **Stream + size guard** — abort past `MAX_UPLOAD_MB`.   → `413` on oversize.
2. **Sniff true MIME** (libmagic) and enforce the allowlist (PDF/XLSX/CSV).   → `415` on mismatch.
3. **Hash (sha256)** for dedupe + cache.   **R:** `document` (by sha256). A byte‑identical re‑upload
   returns the existing row, no re‑classification, no second LLM bill.
4. **Quarantine‑store** the bytes (`quarantine/...`) via `core/storage.py`.
5. **Virus scan** (ClamAV, env‑gated). Clean/skipped → **promote** to `submissions/...`; infected →
   `422 virus_detected`, delete object, **W:** `audit_log`.
6. **Identity validation** — the **hybrid gate**: cheap heuristic first (score the file's anchors/
   fingerprints against the matched `client_template`); HIGH score → accept (`method=heuristic`,
   no LLM); LOW → reject with diagnostics; the ambiguous middle (or a scanned PDF with no text layer)
   escalates to **Gemini → Groq → needs_manual_review**.   **R:** `client_template`.
7. **Persist** the document + its slot.   **W:** `document` (status, classification, scan, signals),
   `submission` (slot pointer + status).

**Completeness gate:** `GET /submissions/{id}` returns `ready_for_stage_2: true` **iff** both slots are
ACCEPTED and `scan_status ∈ {clean, skipped}`.

### Stage 2 — BOM Generation  *(module: bom)*

**Goal:** turn a complete submission into a costed, editable `bom` + `bom_item` tree, then hold it at the
cutting‑manager gate.

**The governing idea:** *the spec sheet drives assembly* (which materials/trims/colours/workmanship);
*the pattern drives consumption*. Neither real spec sheet contains the consumption number, so **DCM is
resolved, not computed.**

**Flow:**
1. **Extraction** (`extraction.py`) — per‑client *adapter* chosen off `(spec_type, client_match_code)`.
   Beau Geste's numeric grid is parsed deterministically (openpyxl, **no LLM**); Jackiee's prose tech
   pack uses Gemini. Emits validated JSON.   **R:** `document`, `client_template`.
   **W:** `spec_sheet` (typed cols + JSON), `pom_measurement` (standardized via `pom_dictionary`),
   `pattern_reference`.
2. **DCM resolution** (`dcm.py`) — an **ordered fallback**, each line stamped `dcm_source`+`dcm_confidence`:
   `(1) style_consumption_template` (a previously confirmed value — best) → `(2) similar‑style retrieval`
   → `(3) AI heuristic` (POM area × wastage, conf ≤ 0.5, **flagged**) → `(4) manual` (the cutting
   manager, conf 1.0).   **R:** `style_consumption_template`, `pom_measurement`, `garment_type`,
   `pattern_reference`.
3. **Costing** (`costing.py`) — `per_garment = dcm × unit_price`; `bulk_qty = order_qty × dcm`;
   `total_cost`, `bulk_total`, `garment_fob_price = Σ per‑garment lines` (BMO‑1 = 107.25).
   **W:** `bom`, `bom_item`.
4. **Cross‑checks** (`checks.py`) — per‑client YAML rules (Σ size qty = order total; pattern resolves;
   leather substance band). Non‑blocking flags unless `severity: error`.
5. **Editable BOM** — `PATCH /boms/{id}/items` = **bulk** edit with **optimistic `revision` locking**
   (`409 stale_revision`). One edit cascades (DCM → line total → bulk → FOB) and is recomputed
   server‑side, atomically.   **W:** `bom`, `bom_item`, `audit_log` (BOM_EDIT).
6. **Cutting gate** — `POST /boms/{id}/confirm-cutting` (CUTTING_MANAGER). Stamps
   `cutting_confirmed_*`, flips `status → ready_for_review`, **back‑fills the DCM memory**, emits the
   BOM_AWAITING_REVIEW notification.   **W:** `bom`, `style_consumption_template`, `notification`,
   `audit_log` (BOM_CUTTING_CONFIRM).

### Stage 3 — Approval  *(module: bom)*

**Goal:** the human authority gate — notify, review/edit, approve+lock or reject, export the PDF, and
guarantee a complete audit trail.

**State machine:** `draft → ready_for_review → approved | rejected → exported`. Approval **always
locks** (the `LOCKED` flag = the immutability semantics the edit guards key off).

| Edge | Endpoint | Role | Writes |
|---|---|---|---|
| draft → ready_for_review | `confirm-cutting` | Cutting Mgr | `bom`, `notification` (×2: MD + DM), `audit_log` |
| ready_for_review → approved | `POST /boms/{id}/approve` | **MD only** | `bom` (approved_by/at, locked_at), `audit_log` (BOM_APPROVE); **auto‑fires Stage 4** |
| ready_for_review → rejected | `POST /boms/{id}/reject` | **MD only** | `bom` (rejected_*), `audit_log` (BOM_REJECT, reason in `after`) |
| ready_for_review → draft | a **DCM** bulk‑PATCH | DM/Cutting/MD | clears `cutting_confirmed_*` → re‑gate |
| rejected → draft | `POST /boms/{id}/reopen` | MD/DM | `bom` (revision++), `audit_log` (BOM_REOPEN) |
| approved → exported | `POST /boms/{id}/export` | MD/system | renders PDF, **W:** `document` (BOM_QUOTE), `bom` (export_document_id, exported_at), `audit_log` |

**Notification + 2‑hour escalation:** entering `ready_for_review` writes **two** `notification` rows
(MD, DM), each `scheduled_for = +2h`. Delivery = **SSE** (`GET /notifications/stream`) + the durable row
+ a polling `GET /notifications`; `POST /notifications/{id}/open` stamps `opened_at` and **cancels**
escalation. A single in‑process **asyncio sweeper** (started in `main.py` lifespan, ~60s) finds unseen
‑and‑due rows and creates an `email` child (idempotent via a `NOT EXISTS` guard).

**PDF export** renders the **current persisted, post‑edit** BOM (only from a locked BOM), stamps
revision + approver + timestamp onto the artifact, stores it as a sha256‑deduped `document`.

### Stage 4 — Inventory Check  *(module: inventory)*

**Goal:** for every stockable BOM line, answer "do we have it, and how much must we still buy?" — and
**reserve** what's available so concurrent approvals don't double‑spend.

**Ingestion** (recurring, idempotent): `POST /inventory/preview` (dry‑run normalize + dedupe) and
`POST /inventory/commit` (upsert). Normalization drops banner/ledger noise, coerces types, computes
`normalized_key`, and **dedups multi‑lot rows** (Σ qty, weighted rate). Re‑sync rule: **sheet wins on
`qty_on_hand`** (safe because reservations are a *separate* ledger); rows absent from the new sheet are
**soft‑deactivated**, never deleted.   **R/W:** `inventory_item`.

**The check** (auto‑fired by `bom_service.approve_bom`, or `POST /boms/{id}/inventory-check`), in **one
transaction** with `SELECT … FOR UPDATE` on matched stock:
1. Release this BOM's prior reservations (idempotent re‑run).   **W:** `inventory_reservation`.
2. **Match** each stockable line (`MANUFACTURING`/`FOB_CHARGE` excluded): `(1) normalized_key` →
   `(2) material_alias` → `(3) advisory fuzzy` (suggestion only) → `(4) unmatched` (OUT_OF_STOCK,
   `flag=unmatched`). Never a silent guess.   **R:** `inventory_item`, `material_alias`, `uom_conversion`.
3. **Compute** per line: `required = bulk_qty`; `on_hand = Σ matched qty`;
   `available = on_hand − Σ active reservations(other BOMs)`; `reserved = min(required, available)`;
   `shortfall = required − reserved`; `status = SUFFICIENT/PARTIAL/OUT_OF_STOCK`.
4. **Write** the report + claims.   **W:** `inventory_check`, `inventory_check_line`,
   `inventory_reservation` (ACTIVE), `audit_log` (INVENTORY_CHECK_RUN).

**Unit edge case:** a stock UOM that can't be converted is written `OUT_OF_STOCK` + `uom_mismatch` —
conservative, never a false "sufficient".

**Output:** per‑check result (`GET /inventory-checks/{id}`) and a grouped client→order→style dashboard
(`GET /inventory-checks`). The `partial`/`out_of_stock` lines are the **input to Stage 5**.

### Stage 5 — Supplier PO  *(module: supplier_po)*

**Goal:** turn each shortfall into a supplier PO, get it cross‑checked, send it, and chase the supplier
until they acknowledge.

1. **Supplier matching** (`supplier_match.py`) — deterministic‑first, never a silent guess:
   `(1) exact normalized_key on supplier_supply_history` → `(2) material_alias` rewrite + retry →
   `(3) category/MODE fallback` → `(4) advisory fuzzy` → `(5) unresolved` (`needs_supplier`, ranked
   shortlist attached). Ranking scores recency + frequency + contactability − rate.
   **R:** `supplier_supply_history`, `supplier`, `material_alias`.
2. **Grouping** — multiple shortfall lines resolving to the **same supplier** become **one PO** with many
   `po_item` rows; a BOM fans out into N POs.   **W:** `purchase_order`, `po_item`.
3. **PO generation** — one Jinja/WeasyPrint **template per supplier type** (leather / accessory /
   service). GST is config‑driven: intra‑state → CGST+SGST (6%+6%); inter‑state → IGST (12%).
   `po_costing.py` computes `subtotal/cgst/sgst/igst/round_off/total`.
4. **Cross‑check gate** — material‑type‑routed: **leather → Cutting Manager**, **accessory → MD/DM/HR**.
   `draft → pending_approval → approved`. Reuses the Stage‑3 SSE notification + sweeper.
   **W:** `purchase_order` (status, approved_*), `notification`, `audit_log`.
5. **Editable PO** — `PATCH /pos/{id}/items`, the **same** bulk‑PATCH + optimistic `revision` contract
   as the BOM; `409 po_locked` once sent.
6. **Send** (`approved → sent`) — allocates `po_number` (FY counter, at send time), renders + attaches
   the PDF, sends via the pluggable `Notifier` (SES recommended), injects the open‑pixel + wrapped links,
   starts the escalation clock.   **W:** `purchase_order`, `document` (SUPPLIER_PO_PDF), `po_response`,
   `audit_log` (PO_SEND).
7. **Tracking** (`po_tracking.py`) — self‑hosted pixel (`/t/o/{token}.gif`) + link‑wrapping
   (`/t/c/{token}`). A hit stamps `first_opened_at`/`first_clicked_at` + appends a `po_tracking_event`.
8. **Escalation ladder** (`escalation.py`, same sweeper): email → (5h no open) **WhatsApp** → (no read)
   **auto‑call** → surface to buyer. A hard bounce short‑circuits straight to WhatsApp. **Any**
   acknowledgement (reply/click/call‑confirm/manual) stamps `acknowledged_at` and the sweeper excludes
   the PO forever.   **W:** `po_response`, `po_tracking_event`, `purchase_order` (rung/ack), `notification`.
9. **Production tracking** (`production_tracking_service.py`) — each procurement edge advances the
   style's `production_tracking` row; MD/DM manually release to the floor. Every manual change audited.

**Supplier CRUD + import:** `POST /suppliers/import/{preview,commit}` (idempotent; rebuilds
`supplier_supply_history`); `GET/POST/PATCH /suppliers`, soft‑delete only. Every mutation writes a
`SUPPLIER_*` audit row.

---

## 5. Cross‑cutting machinery

| Concern | Where | How it works |
|---|---|---|
| **Object storage** | `core/storage.py` | `StorageBackend` interface (put/get/delete/exists/move/presign); drivers `local` (default), `s3/minio`, `supabase`. quarantine→promote flow; sha256 as object name = free dedupe. Singleton via `get_storage()`. |
| **Email** | `core/notifier.py` | `EmailBackend` interface; drivers `log` (default), `noop`, `smtp`, `ses`. `send()` never raises (a failure marks the notification FAILED). Singleton via `get_notifier()`. |
| **Auth/RBAC** | `core/security.py` | bcrypt password hash/verify; HS256 JWT mint/decode (`{sub, role, name, exp}`); process‑local login rate‑limiter. `get_current_user`/`require_roles` live in `modules/users/deps.py`. |
| **Notifications + timers** | `core/models.py::Notification` + the `main.py` sweeper | one durable row per recipient; SSE is a *view*, the table is the source of truth; `scheduled_for`/`opened_at` drive 2h (internal) and 5h (supplier) escalation; idempotent + restart‑safe. |
| **Audit** | `core/models.py::AuditLog` | append‑only before/after diff on every privileged edge. |
| **Promote‑by‑config loop** | `client_template`, `pom_dictionary`, `material_alias`, `supplier_supply_history`, PO templates | ambiguous items first go through the LLM/fuzzy path; confirming one writes a config/registry row that **promotes** the pair to the free deterministic path next time — accuracy up, cost down, no deploy. |

---

## 6. Two invariants worth memorizing

1. **No LLM unless extraction/classification genuinely requires it, and never a silent guess.** Structured
   spreadsheets parse deterministically for free; only scanned/handwritten/novel/prose inputs spend a
   Gemini→Groq call; anything below threshold is surfaced for a human, never fabricated. This rule is the
   same in Stage 1 (doc identity), Stage 2 (extraction + DCM), Stage 4 (stock matching), Stage 5
   (supplier matching).
2. **Compute at write time, never recompute on read; freeze on lock.** Every cost/shortfall/total is
   computed server‑side and persisted; a locked BOM and a sent PO reject all edits (`409 *_locked`); the
   exported PDF is a faithful render of the frozen rows. Concurrency is guarded by optimistic `revision`
   tokens and DB‑level `FOR UPDATE` where money or stock is at stake.
```
