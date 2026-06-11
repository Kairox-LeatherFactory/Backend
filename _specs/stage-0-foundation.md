# Stage 0 — Foundation Spec: BOM Procurement Workflow

**Status:** Draft for MD / stakeholder review
**Scope:** Data foundation only — schema, data-source ingestion, migration strategy, LLM
routing policy, auth/roles. **No code, no migrations** are part of this document.
**Source of truth for the process:** `data/BOM_Procurement_Workflow_fixed.pdf` (6 stages).

> **Note on the stack.** There is **no `CLAUDE.md`** in this repo. The stack facts below
> were confirmed by reading `app/core/config.py`, `app/core/database.py`, and `README.md`:
> FastAPI + SQLAlchemy 2.0 **async (asyncpg)** on the Supabase **transaction pooler :6543**
> for the live API; **sync (psycopg2)** on the Supabase **session pooler :5432** for Alembic
> migrations and `scripts/seed.py`; a read-only **`ai_reader`** Postgres role intended for
> the LangChain path.

---

## 0. Context & why this stage exists

This is a **brownfield** modular monolith ("Leather Factory Intelligence Platform"). It
already models production, wages, attendance, a central `app_user` auth table, and a
buyer-order hierarchy `Client → PurchaseOrder → Style → SKU`. It has a **deterministic**
Excel importer (`app/modules/imports`) that already ingests
`GARMENT_ORDERPRODUCTION_DETAILS.xlsx` and `johnpeter.xlsx` **with no LLM**, a LangGraph +
RAG `intelligence` module, and an **empty `procurement` module** (only `__init__.py`).

The BOM Procurement Workflow we are building on top of it: buyer order + spec upload →
AI BOM generation → MD approval/lock → inventory check → supplier PO + escalation →
production release. Stage 0 fixes the foundation so later stages don't churn the schema.

### Decisions (confirmed with the product owner)
1. **PO naming — rename to match the doc.** The existing buyer-order table is renamed to
   **`client_order`** (physical name; `order` is a SQL reserved word, so the domain term
   stays "order" but the table is `client_order`). The **supplier** PO (Stage 5) takes the
   names **`purchase_order` + `po_item`** and lives in the `procurement` module.
2. **Roles — split MD vs DM, add HR.** Add `MANAGING_DIRECTOR` (BOM approver / superuser)
   and `HR`; `DIRECT_MANAGER` becomes the Stage-1 uploader.
3. **RBAC — route-level + ownership scoping** via the existing `require_roles()`
   dependency; CLIENT users are scoped to their own `client_id`. No Postgres RLS.

### Cross-client variance (the reason several columns are flexible)
- **Beau Geste / CRIMIE** (Japanese, `Order-sheet-1.pdf` No. 1579): one style, one colour
  (Black), **letter sizes** S–XXL, **USD**, refs `A32073-44` / `CR1-02F5-PL02`, handwritten
  spec note, **measurement-grid** spec sheet (`spec_sheet_1.xlsx`, 67×268, per-size
  measurements + tolerances, Japanese headers).
- **The Jackie / John Peter** (Italian, `jackiee-order-sheet.pdf`): **many styles** per
  order, **multi-colour-dimension** garments (SUEDE + KNIT + NYLON colour columns in
  `johnpeter.xlsx`), **combined/add-on styles** ("CLERMONT + VEST", "X + DETACH" with split
  prices "66+8"/"39+7"), **EU numeric sizes** 38–62, **EUR**, multiple distribution "lines"
  (LINEA), **narrative tech-pack** spec sheet (`Jackie-cleint-spec-sheet.xlsx`: free-text
  leather quality / pockets / stitching).
- **Khawaja / KJ** (`GARMENT_ORDERPRODUCTION_DETAILS.xlsx`): **mixed size systems in one
  order** (25–31 *and* 46–62), multiple stacked blocks.

Design implications baked in: **size is a string keyed in a child row, never a column**;
**colour lives at the material/BOM-item level** (plus a primary garment colourway on the
SKU); **combined styles are modeled as style components**; **money carries an explicit
currency**; **spec sheets are stored semi-structured (JSONB)** with a `spec_type`
discriminator because the two real shapes share almost no columns.

---

## 1. Table design

**Conventions.** Every table mixes in `UUIDMixin` (GUID PK) + `TimestampMixin`
(`created_at` / `updated_at`) from `app/core/models.py`. Money is `Numeric(12,2)` with a
sibling `currency` CHAR(3); fractional quantities (yardage/dm²) are `Numeric(12,3)`. New
procurement-domain tables live under `app/modules/procurement/` (the file split — e.g. a
dedicated `boms` submodule — is left to implementation); the logical tables are listed
here.

### 1a. Existing tables — changes

| Table | Change | Reason it exists |
|---|---|---|
| `client` | Add `code` (short, unique), `currency` CHAR(3), `default_size_system` (LETTER/EU/MIXED), `brand`/`label`, `contact_email`, `contact_phone`, `address`, `is_active`. | Beau Geste (USD, letter sizes, Japanese) vs Jackie (EUR, EU numeric, brand "THE JACKIE LEATHER") differ on all of these; today `client` only has `name`, `country`. |
| `purchase_order` → **rename to `client_order`** | Rename the table; rename `style.purchase_order_id` → `style.client_order_id`; rename `po_number` → `order_number`. Keep `order_date`, `delivery_deadline`, `sea_cutoff_date`, `ship_mode`. Add `currency`, `agent`/`line` (Jackie "LINEA"), `source_document_id` FK. | Frees the name `purchase_order` for the supplier PO (decision 1). The buyer order = "order" in the doc; `order_number` holds 1579 / Proposta N.2. |
| `style` | Add `season` (2026AW/SS26), `customer_ref` (CR1-02F5-PL02), `internal_ref` (A32073-44), `unit_price` + `currency`, `base_style_id` (self-FK, nullable). | Combined styles ("CLERMONT + VEST") reference a base; refs/season come straight off the sheets. |
| `style_component` **(new child of `style`)** | `style_id` FK, `component_name` ("VEST", "FUR DETACH", "KNIT", "NYLON"), `add_on_price` Numeric, `currency`. | Jackie's split prices "66+8" / "39+7" and "+ DETACH / + VEST" add-ons can't live in a single price column. |
| `sku` | Add `nylon_color`, `knit_color` (the suede/main colour stays in `color_code` / `color_name`). Keep `(style_id, color_code, size)` unique. | `johnpeter.xlsx` carries SUEDE + KNIT + NYLON colour dimensions per line. |
| `app_user` | No column change; `role` enum extended (see §5). | Add MD/HR roles. |
| `employee` | No change. | Already seeded from `employees_detail.xlsx` via `scripts/seed.py`. |

### 1b. New tables — Stage 1 (inputs)

| Table | Key fields | Why it exists |
|---|---|---|
| `document` | `id`, `client_id?`, `client_order_id?`, `kind` enum(ORDER_SHEET, SPEC_SHEET, BOM_QUOTE, SUPPLIER_PO_PDF), `filename`, `mime`, `storage_url` (object storage), `sha256` (unique → dedupe + extraction cache key), `uploaded_by` FK `app_user`, `page_count`. | Stage 1 requires both files submitted together; we persist the raw artifact for audit, re-extraction, and the read-receipt/RAG trail. `sha256` keys the LLM extraction cache (don't re-bill identical uploads). |
| `spec_sheet` | `id`, `client_id`, `style_id?`, `source_document_id` FK, `spec_type` enum(MEASUREMENT_GRID, NARRATIVE_TECHPACK), `season`, `customer_label`, `measurements` JSONB, `attributes` JSONB, `instructions` JSONB, `extracted_by` (deterministic/gemini/groq/manual), `confidence`. | The two real spec sheets share almost no columns: the Japanese one is a numeric grid with tolerances; Jackie's is narrative key→value. JSONB + a discriminator stores both without 200 sparse columns; typed columns only for fields every client has. |

### 1c. New tables — Stage 2/3 (BOM)

| Table | Key fields | Why it exists |
|---|---|---|
| `bom` | `id`, `client_order_id` FK, `style_id` FK, `status` enum(DRAFT, APPROVED, LOCKED), `currency`, `garment_fob_price` Numeric, `bulk_total` Numeric, `order_qty` int, `revision` int, `approved_by` FK `app_user`, `approved_at`, `locked_at`, `source_document_id` FK (BOM_QUOTE). **UNIQUE `(client_order_id, style_id)`** (doc: one BOM per style per order). | Mirrors the BMO-1 header (brand/style/FOB total). `status` drives the Stage-3 gate; lock + revision + approver satisfy the "cryptographically locked, full revision history" requirement (field-level history lives in `audit_log`). |
| `bom_item` | `id`, `bom_id` FK, `category` enum(MAIN_MATERIAL, SUB_MATERIAL, LINING, INTERLINING, THREAD, ACCESSORY, MANUFACTURING, PACKAGING, FOB_CHARGE), `name`, `material_color`, `qty_per_garment` Numeric(12,3), `uom` (dm²/pc/unit), `unit_price` Numeric, `bulk_qty` Numeric, `total_cost` Numeric, `annotation` (handwritten notes e.g. "take care goat suede"), `source_ref`. | Exactly the BMO-1 rows: Sheep Glass 34.5 dm² @1.80, Goat Suede 2.6 @1.75, lining/interlining/thread, buttons (TBC), cutting/stitching, packaging, FOB charge. Per-item `material_color` is where the multi-colour dimension lands. |

### 1d. New tables — Stage 4 (inventory)

| Table | Key fields | Why it exists |
|---|---|---|
| `inventory_item` | `id`, `description` (indexed), `normalized_key` (indexed, for matching), `uom` (KGS/DCM/ROLL/NOS/PCS), `qty_on_hand` Numeric, `rate` Numeric, `category`, `color`, `article_ref`, `is_active`. | The `INVENTORY (1).xlsx` master (1,525 rows: DESCRIPTION/NOM/PCS/RATE), normalized + deduped. Live, mutable stock; the check reads it. |
| `inventory_check` | `id`, `bom_id` FK, `status` enum(RUNNING, COMPLETE), `run_at`, `run_by`. | Stage 4 produces one stock-status report per approved BOM. |
| `inventory_check_line` | `id`, `inventory_check_id` FK, `bom_item_id` FK, `inventory_item_id?` FK, `required_qty` Numeric, `on_hand_qty` Numeric, `shortfall_qty` Numeric, `status` enum(SUFFICIENT, PARTIAL, OUT_OF_STOCK). | The doc's per-line SUFFICIENT/PARTIAL/OUT_OF_STOCK + exact shortfall, forwarded to Stage 5. |

### 1e. New tables — Stage 5 (suppliers + supplier PO)

| Table | Key fields | Why it exists |
|---|---|---|
| `supplier` | `id`, `name` (unique), `phone`, `email`, `service`/`category`, `gstin`, `address`, `currency` (INR default), `payment_terms_days` (60), `lead_time_days` (10), `is_active`. | From `SUPPLIERS_updated (2).xlsx` "Supplier contact info" (1,433 rows) + the PO-form footers (GSTIN, terms). The article→supplier lookup in Stage 5 resolves here. |
| `purchase_order` **(supplier PO)** | `id`, `po_number` ("PO-07(25-26)"), `supplier_id` FK, `bom_id?` FK, `client_order_id?` FK, `buyer_ref` ("PO-JAPAN-902/904-#CRIMIE"), `issue_date`, `delivery_days`, `payment_terms_days`, `currency` (INR), `subtotal`, `cgst`, `sgst`, `round_off`, `total`, `status` enum(DRAFT, SENT, RESPONDED, CONFIRMED, ESCALATED, CANCELLED), `sent_at`, `pdf_document_id?` FK. | Exactly the PAKKAR→supplier PO forms (`suppler-po-form/*.pdf`): Bill-To supplier, invoice #, CGST/SGST 2.5%, totals, 60-day terms. The 40 forms are the template + field source. |
| `po_item` | `id`, `purchase_order_id` FK, `item_no`, `description`, `color`, `uom`, `qty` Numeric, `unit_price` Numeric, `amount` Numeric, `inventory_item_id?` FK, `bom_item_id?` FK. | The PO-form line grid (e.g. 100 GSM Padding-Body 220 m @38); links back to the shortfall line it fulfils. |
| `po_response` | `id`, `purchase_order_id` FK, `channel` enum(EMAIL, CALL), `sent_at`, `responded_at?`, `confirmed_qty?`, `status` enum(PENDING, OPENED, CONFIRMED, NO_RESPONSE), `notes`. | The Stage-5 5-hour-window escalation: email → (no response) → auto-call → record confirmation. |

### 1f. New tables — cross-cutting (Stage 3/5/6)

| Table | Key fields | Why it exists |
|---|---|---|
| `notification` | `id`, `recipient_user_id?` FK, `supplier_id?` FK, `channel` enum(IN_APP, EMAIL, CALL), `type` enum(BOM_AWAITING_REVIEW, PO_DISPATCH, ESCALATION_CALL, MATERIAL_READY), `subject`, `body`, `entity_type`, `entity_id` (polymorphic ref), `status` enum(PENDING, SENT, OPENED, RESPONDED, FAILED), `scheduled_for`, `sent_at`, `opened_at`, `parent_notification_id?` (escalation chain). | Covers every alert in the doc: MD review notice → automail after 5 h → autocall after 5 h (Stage 3); PO email + read-receipt + escalation (Stage 5); material-ready MD alert (Stage 6). `scheduled_for`/`opened_at` drive the 5-hour timers and read-receipt tracking. |
| `audit_log` | `id`, `actor_user_id?` FK, `action` (BOM_APPROVE, BOM_EDIT, BOM_LOCK, PO_SEND, …), `entity_type`, `entity_id`, `before` JSONB, `after` JSONB, `at`. | The doc's "full revision history including all edits, approving identity, timestamp." General-purpose trail; `bom` keeps `approved_by/at` + `revision`, and the field-level diff lives here. |

**FK summary.**
`client → client_order → { style → { style_component, sku }, bom }`;
`bom → { bom_item, inventory_check → inventory_check_line }`;
`inventory_check_line → inventory_item`;
`supplier → purchase_order → { po_item, po_response }`;
`purchase_order → bom`;
`document` is referenced by `client_order` / `spec_sheet` / `bom` / `purchase_order`;
`app_user` is the actor on `document.uploaded_by`, `bom.approved_by`, `audit_log.actor_user_id`,
`notification.recipient_user_id`.

---

## 2. Per-source ingestion classification

Options considered for each `/data` artifact:
**(a)** one-shot import (cleaning script / Supabase UI) · **(b)** Alembic + YAML
idempotent seed · **(c)** factory_boy + Faker dev data · **(d)** pg_dump artifact restored
from object storage.

> **Honest carve-out.** The order sheets, spec sheets, sample BOM, and PO-form PDFs are
> **runtime fixtures / AI-extraction inputs** — *not* seed data. They fit none of (a)–(d)
> and are committed under `tests/fixtures/` instead.

| Source | Classification | Justification |
|---|---|---|
| `INVENTORY (1).xlsx` (1,525 rows) | **(a) one-shot import** + **(d) pg_dump** snapshot | Large real master that the live inventory-check **mutates**; re-seeding every env would clobber live counts. Needs a normalizing importer (dedupe descriptions, drop non-stock rows like "MARCH MONTH USAGE" / "MEMBERSHIP FEE", coerce UOM/Numeric) — too messy for a raw UI paste. pg_dump the cleaned result for reproducible staging/test restores. |
| `SUPPLIERS_updated (2).xlsx` → **"Supplier contact info"** (1,433) | **(a) one-shot import** | Real reference master feeding `supplier`; loaded once, then maintained in-app. |
| `SUPPLIERS_updated (2).xlsx` → **"Suppliers provision"** (1,525 txns) | **(d) pg_dump artifact** (or defer) | Historical purchase transactions; analytics-only, not needed to boot the app. Archive, don't seed. |
| `employees_detail.xlsx` | **(b) idempotent seed** | Small, fixed, belongs in every env. Already loaded by `scripts/seed.py` (Python). Keep that path — it *is* the existing idempotent seed. |
| `GARMENT_ORDERPRODUCTION_DETAILS.xlsx`, `johnpeter.xlsx` | **Existing `imports` pipeline** (deterministic, no LLM) | Structured operational uploads already handled by `POST /api/v1/imports/commit` (verified: 6 clients / 28 styles / 402 SKUs). Not seed, not Faker. |
| `spec_sheet_1.xlsx` (JP grid), `Jackie-cleint-spec-sheet.xlsx` (narrative) | **Runtime fixture** + **(c) factory_boy** for dev | Real files = parsing/extraction fixtures under `tests/fixtures/`. For dev/test volume, synthesize `spec_sheet` rows with factory_boy + Faker. |
| `Order-sheet-1.pdf`, `jackiee-order-sheet.pdf` | **Runtime fixture** + **(c) factory_boy** for dev | Stage-1 inputs to AI vision extraction; not seeded. Faker generates synthetic orders for dev. |
| `BMO-1.pdf` | **Runtime fixture / reference template** | The sample BOM = the AI's target output shape + the BOM/PO PDF render template; drives the `bom`/`bom_item` columns. Not seeded. |
| `suppler-po-form/*.pdf` (40) | **Runtime fixture / template** (+ optional **(a)** for a parsed subset) | Define the `purchase_order`/`po_item` fields and the generated-PO PDF layout. Optionally one-shot-parse a subset to seed historical `supplier`/`purchase_order`; otherwise fixtures. |

**Why a Python + YAML seed over pure Alembic-data:** keep Alembic **schema-only**.
Small fixed reference seeds (operation catalog, `operation_access`, BOM category catalog,
a supplier shortlist) go in an **idempotent YAML loaded by `scripts/seed.py`** (extend the
existing script) — re-runnable, testable, and not entangled with migration history.

---

## 3. Migration strategy

- **Tooling/wiring (unchanged from repo).** Alembic runs via **sync psycopg2 on the
  Supabase session pooler :5432** (`settings.database_url`); the live API uses **asyncpg on
  the transaction pooler :6543** (`settings.async_database_url`). The txn pooler lacks the
  session/prepared-statement features migrations need — hence Alembic stays on :5432.
- **CRITICAL — register new models in `alembic/env.py`.** It currently imports only
  `users, clients, employees, production, wages`. Autogenerate will **silently miss** the
  new procurement / bom / spec / inventory / notification / audit tables unless their
  modules are added to the import block. (It also already omits `analytics`, `intelligence`,
  and `procurement`.)
- **Add a `MetaData` naming convention to `Base` now** (`ix`/`uq`/`ck`/`fk`/`pk`
  templates), before more tables exist. Today `Base` has none, so constraint names are
  autogenerated and downgrades/renames are fragile. Do this as the **first** Stage-0
  migration (it will rewrite some autogenerated constraint names — acceptable while the
  schema is small).
- **Revision naming.** Replace the ad-hoc slugs in the repo (`_2nd`, `_new_changes`,
  `_baseline12`) with a `file_template` in `alembic.ini` such as
  `%%(year)d%%(month).2d%%(day).2d_%%(hour).2d%%(minute).2d_%%(slug)s` and descriptive
  slugs (`add_procurement_tables`, `rename_po_to_client_order`).
- **Reversible vs not.**
  - Plain DDL (create table / add column) → **reversible**; always write `downgrade`.
  - The **rename migration** (`purchase_order` → `client_order` + `style` FK rename, then
    create the new supplier `purchase_order`) → **reversible but high-risk**; do it as **one
    atomic migration** with explicit `op.rename_table` / `op.alter_column`, ordered:
    (1) rename old table & FK column, (2) create the new `purchase_order` / `po_item`. Test
    on a DB snapshot first.
  - **Enum value adds** (`MANAGING_DIRECTOR`, `HR` on the native `user_role` type): Postgres
    `ALTER TYPE … ADD VALUE` is **non-transactional and not reversible** — isolate it in its
    own migration with `downgrade = pass` and a comment. (Keeps consistency with the existing
    native `Enum`; switching to a CHECK-constrained varchar is out of scope for Stage 0.)
  - **Data backfills / inventory normalization → NOT in Alembic.** Keep them in the
    importer/seed so they are re-runnable and testable; any unavoidable data migration is
    guarded and marked irreversible (`downgrade` raises).

---

## 4. LLM routing policy

**Governing rule: _no LLM unless extraction or classification genuinely requires it._**

- **Deterministic-first.** Structured spreadsheets (KJ / JP orders) → the existing
  `imports` parser, **zero LLM**. Exact/normalized string matching, regex, and lookups
  handle ID matching (`CR1-02F5-PL02` ↔ `CRI 02F5 PL02` via normalization) and supplier
  resolution before any model is invoked.
- **LLM only for:** (1) scanned / handwritten / printed PDFs — Beau Geste order, Jackie
  handwritten order, supplier-PO scans; (2) **narrative** spec sheets (Jackie tech pack);
  (3) genuinely ambiguous **classification** (status / fuzzy supplier mapping where
  normalization fails); (4) semantic **cross-document reconciliation** the rules can't do.
- **Providers — Gemini primary, Groq fallback.**
  - **Gemini** (`langchain-google-genai`, add to requirements) is primary for
    **extraction**: multimodal (PDF/image order sheets) and long-context (the 268-column
    Japanese spec grid).
  - **Groq** (`langchain-groq`, already pinned) is the **fallback** for text/structured
    classification and when Gemini errors / hits quota / times out.
  - A small **extraction service** (in `procurement` or `intelligence`) runs a provider
    chain: **Gemini → Groq → structured "needs manual entry"** result. It **never silently
    guesses**; failures surface for human keying. Stage-3 MD approval is the human safety
    net for extraction error.
  - **Structured output** (JSON-schema / function calling) is validated before persisting;
    results are **cached by `document.sha256`** so identical uploads aren't re-billed.
  - Embeddings remain **local HF** (`app/modules/intelligence/models_catalog.py`) — not a
    chat provider.
  - Config additions: `extraction_model` (default `gemini:…`), `extraction_fallback_model`
    (`groq:…`), `GEMINI_API_KEY` (alongside the existing `GROQ_API_KEY`); extend
    `CHAT_MODELS` with Gemini entries.
- **`ai_reader` role.** The LangChain / agent DB path connects as a least-privilege,
  read-only `ai_reader` Postgres role (SELECT on whitelisted tables/views only) via a
  separate `ai_reader_database_url`. **The LLM path never writes** — all persistence goes
  through authenticated API services — so a prompt-injected agent can neither mutate data
  nor read secrets.

---

## 5. Auth & roles

- **Extend `app/core/enums.py::UserRole`:** add `MANAGING_DIRECTOR = "managing_director"`
  and `HR = "hr"`. `DIRECT_MANAGER` is now the **Stage-1 uploader**. Update
  `manager_roles()` only if cutting/stitching scope changes (none expected).
- **Superuser shift.** In `app/modules/users/deps.py::require_roles` (and the matching note
  in `app/core/security.py`), the universal-pass role moves from `DIRECT_MANAGER` to
  **`MANAGING_DIRECTOR`** (MD outranks DM). Recommendation: **MD = superuser; DM =
  operational** (upload + edit drafts, no final approval).
- **Enforcement: route-level + ownership scoping** (decision 3). Keep using
  `require_roles(...)` per endpoint; CLIENT users are filtered to their own `client_id` in
  queries (the existing `app_user.client_id` link). **No Postgres RLS.**
- **Stage → role matrix.**

  | Stage | Action | Allowed |
  |---|---|---|
  | 1 | Upload order + spec | `DIRECT_MANAGER` (+ MD) |
  | 2 | AI BOM generation | system |
  | 3 | **Approve / lock BOM** | **`MANAGING_DIRECTOR` only** |
  | 3 | Edit draft BOM | DM, MD |
  | 4 | Inventory check | system; HR / VIEWER read |
  | 5 | PO dispatch / escalation | system + DM oversight |
  | 6 | Production release / MD alert | system → MD |
  | — | Employees / wages / attendance read | `HR`, MD |
  | — | Own orders + BOM status | `CLIENT` (scoped to `client_id`) |

- **Token shape unchanged** (`sub` / `role` / `name` / `exp` in `core/security.py`).
  Privileged actions (approve, lock, PO send) write an `audit_log` row.
- **Native-enum caveat** (cross-ref §3): adding the two roles is a non-transactional,
  irreversible `ALTER TYPE … ADD VALUE` migration.

---

## 6. Review checklist (definition of done — this is a spec, nothing to run)

- [ ] Every requested table is covered: clients (`client`), orders (`client_order`),
      spec_sheets (`spec_sheet`), boms (`bom`), bom_items (`bom_item`), inventory
      (`inventory_item`), suppliers (`supplier`), purchase_orders (`purchase_order`),
      po_items (`po_item`), notifications (`notification`), audit_log (`audit_log`),
      users/roles (`app_user` + `UserRole`).
- [ ] Cross-client variance (JP letter/USD/grid vs Jackie EU/EUR/narrative/multi-colour/
      add-ons vs KJ mixed-size) is expressible (string sizes, JSONB spec, per-item colour,
      `style_component`, per-row `currency`).
- [ ] Each `/data` artifact has a justified ingestion path; runtime fixtures explicitly
      carved out.
- [ ] Migration section names the `env.py` model-import gap, the naming-convention add, the
      atomic rename, and the irreversible enum/data caveats.
- [ ] LLM policy states the "no LLM unless required" rule, the Gemini→Groq chain, and the
      `ai_reader` read-only path.
- [ ] Role model adds MD + HR, makes MD the approver/superuser, route-level RBAC + client
      ownership.
- [ ] No code, migrations, or model edits were produced — spec only.
- [ ] Reviewed / signed off by MD / system stakeholder.
