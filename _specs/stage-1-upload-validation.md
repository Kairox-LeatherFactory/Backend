# Stage 1 — Upload & Validation Spec: Order Sheet + Spec Sheet Submission

**Status:** Draft for MD / stakeholder review
**Scope:** The Stage-1 *secure document submission* only — paired upload, format/identity
validation, virus scan, dedupe, and storage of the two source artifacts so they are ready
for the Stage-2 AI engine. **No code, no migrations** are part of this document.
**Builds on:** `_specs/stage-0-foundation.md` (schema, LLM routing policy, roles).
**Source of truth for the process:** `data/BOM_Procurement_Workflow_fixed.pdf` / `data/_wf.txt`,
Stage 1 ("MD/Direct Manager Submission").

> **Stack facts (re-confirmed against the code, not stage-0 prose).** The repo runs **plain
> PostgreSQL in Docker** (`postgresql+psycopg2://factory@…:5432`, `app/core/config.py`) and
> has **migrated off Supabase Auth** — it mints its own HS256 JWTs (`app/core/security.py`,
> CLAUDE.md). There is currently **no object-storage client, bucket config, or Supabase SDK**
> wired. `Document.storage_url` (`app/modules/procurement/models.py:89`) is already a
> backend-agnostic string. This spec therefore introduces a **storage abstraction** with
> Supabase Storage as the *production driver only* — see §6 — so the repo keeps no hard
> Supabase dependency and the SQLite/in-memory test path is preserved.

---

## 0. Context & why this stage exists

Stage 1 is the **front door** of the BOM Procurement Workflow. The workflow doc is explicit
and unusually strict about it:

> "The MD/Direct Manager is presented with a dedicated upload screen designed to accept two
> mandatory documents simultaneously… **Both documents must be submitted together in a single
> transaction. The system will not proceed to AI analysis until both files are present and
> validated for format compliance.** Accepted formats include PDF, XLSX, and CSV."
> — `data/_wf.txt`, Stage 1

So Stage 1 owns four jobs, and **only** these four (extraction is Stage 2):

1. **Pair** an Order Sheet and a Spec Sheet into one submission (they arrive as separate
   uploads but gate Stage 2 together).
2. **Validate identity** — answer the hard question *"is this file actually an order sheet /
   spec sheet?"* before a single token is spent on extraction.
3. **Make the artifacts safe and durable** — virus scan, size/MIME limits, dedupe, and
   persist to object storage with a full audit trail.
4. **Gate Stage 2** — emit a `ready_for_stage_2` signal only when both slots are present,
    validated, and clean.
  
### The hard problem
There is no single "order sheet format." Across the three real clients in `/data` the same
logical document looks completely different:

| Client | Order sheet | Spec sheet | Language / sizes / currency |
|---|---|---|---|
| **Beau Geste / CRIMIE** (Japan) | `Order-sheet-1.pdf` — buyer PDF, **handwritten** annotations, No. 1579, refs `A32073-44` / `CR1-02F5-PL02` | `spec_sheet_1.xlsx` — **measurement grid** (67×268), per-size measurements + tolerances, **Japanese headers** | JP headers · **letter** S–XXL · **USD** |
| **The Jackie / John Peter** (Italy) | `jackiee-order-sheet.pdf` — **many styles**, multi-colour, combined styles, "LINEA" lines | `Jackie-cleint-spec-sheet.xlsx` — **narrative tech pack**, free-text leather/pockets/stitching | IT · **EU numeric** 38–62 · **EUR** |
| **Khawaja / KJ** | `GARMENT_ORDERPRODUCTION_DETAILS.xlsx` — **mixed size systems** in one order, stacked blocks | — | EN · **mixed** 25–31 & 46–62 |

…and the prompt requires onboarding **new** clients ("Australia", "Jackiee", …) **without
code changes**. A naive "look for the word ORDER" check would reject the Japanese handwritten
PDF and the Italian tech pack on day one. The validation design (§3–§4) is the core of this
stage.

### Decisions (confirmed with the product owner)
1. **Storage = pluggable backend, Supabase as the prod driver.** Local-disk/MinIO in dev +
   tests; Supabase Storage bucket in prod. `Document.storage_url` stays backend-agnostic. (§6)
2. **Pairing = a `submission` (upload-batch) row.** The first upload mints a `submission_id`;
   the second references it. The `client_order` does **not** exist yet at upload — it is
   *derived from the order sheet in Stage 2* — so we pair on the submission, never on an
   order id. (§2, §5)
3. **Validation = hybrid (heuristic gate → LLM only on ambiguous).** The literal embodiment
   of stage-0 §4's "no LLM unless extraction/classification genuinely requires it." (§3)
4. **Registry = config-driven.** A `client_template` registry (DB rows seeded from YAML) holds
   each client's fingerprints; new clients are onboarded by adding an entry. (§4)
5. **Virus scan = real ClamAV, gated by an env flag** (`VIRUS_SCAN_ENABLED`). ON in
   staging/prod (reject on hit, fail closed if `clamd` is unreachable); OFF in dev records
   `scan_status = skipped`. (§7)

---

## 1. Position in the workflow (non-goals)

```
   [Stage 1 — THIS SPEC]                              [Stage 2 — separate spec]
   ┌───────────────────────────────────────────┐      ┌─────────────────────────┐
   DM/MD ─ upload order sheet ─┐                 │      │                         │
                               ├─► submission ──►│ gate │─► AI extraction ─►  BOM │
   DM/MD ─ upload spec  sheet ─┘   (pair+validate│ open │   (Gemini→Groq)         │
                                    +scan+store) │      │                         │
   └───────────────────────────────────────────┘      └─────────────────────────┘
```

**In scope:** pairing, identity validation, virus scan, size/MIME limits, dedupe, storage,
the Stage-2 readiness gate, and the diagnostics returned on rejection.

**Out of scope (Stage 2+):** field extraction, BOM generation, cross-document reconciliation,
size/qty parsing, spec_sheet JSONB population. Stage 1 *classifies* a spec sheet as
`MEASUREMENT_GRID` vs `NARRATIVE_TECHPACK` (a cheap by-product of validation) but does **not**
read the measurements — that is Stage 2's `extracted_by` work.

---

## 2. Endpoints

All under `/api/v1/procurement`. Auth: `require_roles(DIRECT_MANAGER, MANAGING_DIRECTOR)`
(stage-0 §5; MD bypasses as superuser). Multipart `UploadFile`, mirroring the existing
`imports` handler pattern (`app/modules/imports/router.py`): the **sync** parse/sniff work
(openpyxl, libmagic, pdf text-layer probe, clamd) runs in `run_in_threadpool` so the async
event loop is never blocked.

> **On "paired by order_id."** The prompt asks for the two uploads to be "paired by order_id."
> Because the order does not exist until Stage 2 parses the order sheet, the pairing key at
> Stage 1 is the **`submission_id`** — the surrogate that *becomes* the `client_order` link in
> Stage 2. Everywhere the prompt says "order_id," read "submission_id."

| Method & path | Purpose | Body / params | Returns |
|---|---|---|---|
| `POST /submissions` | Open an empty submission (optional — the first document upload auto-opens one if none is supplied). | `{ client_id? }` (client may be unknown; left null) | `201 { submission_id, status: "open" }` |
| `POST /submissions/{submission_id}/order-sheet` | Upload + validate the **order sheet** slot. | `multipart: file` | `201`/`200` submission + document envelope (§5) |
| `POST /submissions/{submission_id}/spec-sheet` | Upload + validate the **spec sheet** slot. | `multipart: file` | `201`/`200` submission + document envelope (§5) |
| `GET /submissions/{submission_id}` | Submission status + the Stage-2 readiness gate. | — | `200` submission status (§5) |
| `GET /submissions/{submission_id}/documents/{document_id}` | Full per-document validation report (diagnostics, signals, confidence). | — | `200` document envelope |

**Two separate slot endpoints, one shared submission.** This satisfies "separate, paired":
each document type has its own endpoint and validation profile, but both write into the same
`submission` row, so the pairing is explicit and the completeness gate is trivial to compute.

**Completeness gate.** `GET /submissions/{id}` returns `ready_for_stage_2: true` **iff** both
slots are filled with documents whose `validation_status = accepted` **and** `scan_status ∈
{clean, skipped}`. Only then may Stage 2 be triggered (Stage-2 triggering itself is out of
scope here). A submission with one slot accepted and the other rejected/missing reports
`ready_for_stage_2: false` with the blocking reason.

---

## 3. The validation problem — "is this actually an order/spec sheet?"

The decision must hold across structured spreadsheets (KJ/JP — trivially machine-readable),
born-digital PDFs (Jackie — has a text layer), and **scanned/handwritten** PDFs (Beau Geste —
no usable text layer, Japanese, pen annotations). Three approaches were considered.

### (a) Pure heuristic — header keywords, sheet structure, MIME sniff, regex
Sniff the true MIME with libmagic (not the client-supplied content-type); enforce an
extension/MIME allowlist; then fingerprint the content:
- **XLSX/CSV:** read sheet names + the first N header rows (openpyxl); score against a
  weighted keyword set (`ORDER`, `QTY`, `PCS`, `SIZE`, `DELIVERY`, `STYLE`, size tokens
  `S/M/L/XL` or `38–62`, currency symbols), check grid shape (a wide numeric grid with a
  tolerance column ⇒ `MEASUREMENT_GRID`; tall free-text key→value ⇒ `NARRATIVE_TECHPACK`).
- **PDF:** probe for a text layer (pypdf/pdfplumber). If present, run the same keyword/regex
  scoring + reference-pattern regexes (`A32073-44`, `CR1-02F5-PL02`, `No.\s*\d{3,5}`,
  `Proposta N`). If **absent** (scanned), the heuristic has almost nothing to go on.

*Cost:* effectively free (CPU only). *Latency:* ~10–80 ms. *Accuracy:* excellent on the
structured KJ/JP Excel; **fails the Beau Geste handwritten/scanned PDF** (no text layer) and
is **brittle on novel client layouts** — a new client's headers in a new language score zero,
causing false rejects. High maintenance: every new format risks a regex tweak (a code change,
which the prompt forbids).

### (b) LLM classifier — Gemini with a structured-output schema
Send every uploaded file (PDF pages as images for the multimodal path, or extracted
text/sheet dump) to Gemini with a fixed JSON schema and let the model decide:

```jsonc
// structured-output schema (validated before trust)
{
  "is_order_sheet": true|false,
  "is_spec_sheet":  true|false,
  "doc_kind":   "order_sheet" | "spec_sheet" | "bom_quote" | "other",
  "spec_type":  "measurement_grid" | "narrative_techpack" | null,
  "client_guess": "beau_geste" | "the_jackie" | "kj" | null,
  "confidence": 0.0-1.0,
  "evidence":   ["No.1579 order number", "size breakdown S:4 M:17 …", "JP buyer header"],
  "reject_reason": null | "no order quantities or SKU grid found; looks like a process doc"
}
```

*Cost:* one Gemini call **per upload** — bills even for the structured XLSX the heuristic
nails for free. *Latency:* ~1–4 s (multimodal PDF is the slow path). *Accuracy:* best on
scanned/handwritten/novel formats; reads Japanese and free-text tech packs natively; but it
**over-trusts** (a confident wrong answer on an adversarial file) and a model outage stalls
the *entire* front door, including the easy cases.

### (c) Hybrid — cheap heuristic gate, LLM only on ambiguous files  ✅ RECOMMENDED
Run (a) first and act on its confidence band:

```
                       ┌─ score ≥ HIGH ───────────────► ACCEPT  (method=heuristic, no LLM)
 file ─► MIME/size/AV ─┤  LOW < score < HIGH  ─► Gemini ─► ACCEPT / REJECT (method=llm)
   gate                │  (also: scanned PDF, no text layer, or no client profile matched)
                       └─ score ≤ LOW ────────────────► REJECT  (method=heuristic, diagnostics)
```

- The **structured spreadsheets** (KJ, JP, and any future Excel client) clear the HIGH bar on
  the free heuristic — **zero LLM cost, ms latency**.
- Only the **genuinely ambiguous** minority — scanned/handwritten PDFs (Beau Geste), unknown
  layouts, mid-band scores — escalate to **Gemini → Groq → "needs manual review"**, the exact
  provider chain already configured in `app/core/config.py:73-76` and governed by stage-0 §4.
- Results are **cached by `Document.sha256`** so a re-upload of the same file is never
  re-classified or re-billed.

### Decision table

| Dimension | (a) Pure heuristic | (b) LLM every file | (c) Hybrid ✅ |
|---|---|---|---|
| **Cost / upload** | ~$0 | 1 Gemini call always | ~$0 for the common structured case; 1 call only on the ambiguous minority |
| **Latency (typical)** | 10–80 ms | 1–4 s | 10–80 ms common path; 1–4 s on escalation |
| **Scanned/handwritten PDF (Beau Geste)** | ❌ fails (no text layer) | ✅ | ✅ (escalates) |
| **Novel client format** | ❌ false-reject until regex patched (code change) | ✅ | ✅ (escalates; registry entry then promotes it to the free path) |
| **Structured XLSX (KJ/JP)** | ✅ | ✅ but needlessly billed | ✅ free |
| **Resilience to model outage** | ✅ (none used) | ❌ whole door stalls | ✅ easy cases unaffected; ambiguous → manual fallback |
| **Adversarial / wrong-doc** | medium (clear signals) | over-trusts | best (two independent gates) |
| **Alignment with stage-0 §4** | partial | violates "no LLM unless required" | **exact match** |

**Recommendation: (c) hybrid.** It is cheapest in the common case, robust in the hard case,
degrades gracefully under model failure, and is the literal implementation of the platform's
standing "no LLM unless genuinely required" rule. The heuristic and the LLM are two
*independent* gates, which also makes adversarial misclassification least likely.

**Never silently guess (stage-0 §4).** When both heuristic and LLM land below the accept
threshold, the file is **not** force-classified — it is rejected with diagnostics (§5) or, for
mid-confidence order/spec-shaped files, flagged `needs_manual_review` so a human keys the
classification. The Stage-3 MD approval remains the ultimate safety net.

---

## 4. Per-client template registry (config-driven, no code changes)

Validation profiles live in a **`client_template` registry**, not in `if client == "beau_geste"`
branches. Seeded from a versioned YAML file via the existing `scripts/seed.py` (the same
idempotent-seed pattern stage-0 §2 mandates) into a DB table the validator reads at request
time. **Onboarding a new client = add one registry entry; deploy nothing.**

### Registry entry shape (per client × doc-kind)

```yaml
# config/client_templates.yaml  (seeded, idempotent, hot-reloadable)
- client_code: beau_geste
  display_name: "Beau Geste / CRIMIE"
  language: ja
  size_system: letter            # stage-0 SizeSystem enum
  currency: USD
  doc_kind: order_sheet
  accepted_mime: [application/pdf]
  expected_layout: scanned_pdf   # hints the validator to escalate straight to the LLM
  anchors:                       # weighted keyword/phrase signals (any language)
    - { any: ["株式会社ボージェスト", "BEAU GESTE", "CRIMIE"], weight: 3 }
    - { any: ["No.", "ORDER", "発注"], weight: 1 }
    - { any: ["S", "M", "L", "XL", "XXL"], weight: 1, kind: size_tokens }
  fingerprints:                  # regexes that strongly identify this client's docs
    - 'CR\d-?0?2F5[ -]?PL0?2'    # customer ref CR1-02F5-PL02 / CRI 02F5 PL02
    - 'A\d{5}-\d{2}'             # internal ref A32073-44
    - 'No\.?\s*\d{3,5}'          # order number 1579
  thresholds: { accept_high: 0.80, reject_low: 0.30 }

- client_code: the_jackie
  display_name: "The Jackie / John Peter"
  language: it
  size_system: eu
  currency: EUR
  doc_kind: order_sheet
  accepted_mime: [application/pdf, text/csv, <xlsx>]
  expected_layout: digital_pdf
  anchors:
    - { any: ["THE JACKIE", "JOHN PETER", "LINEA", "Proposta"], weight: 3 }
    - { any: ["38","40","42","44","46","48","50","52","54","56","58","60","62"], weight: 1, kind: size_tokens }
  fingerprints: [ 'Proposta\s*N', 'LINEA\s*\d+' ]
  thresholds: { accept_high: 0.75, reject_low: 0.30 }

- client_code: _generic            # fallback profile — universal order-sheet signals only
  display_name: "Unknown client (generic)"
  doc_kind: order_sheet
  anchors:
    - { any: ["ORDER","QTY","QUANTITY","PCS","SIZE","DELIVERY","STYLE","SKU"], weight: 1 }
  thresholds: { accept_high: 0.85, reject_low: 0.40 }   # stricter — no client prior
```

Spec-sheet entries carry the same shape plus a `spec_type` hint (`measurement_grid` vs
`narrative_techpack`) and grid-shape signals (`min_cols`, `has_tolerance_column`,
`free_text_ratio`).

### How a profile is selected & used
1. **Match:** score the file's fingerprints/anchors against every active profile; pick the
   best client match. If none clears a minimal bar, fall back to the **`_generic`** profile
   (stricter thresholds, no client prior).
2. **Score & decide:** the matched profile's weighted anchors + grid signals produce the
   confidence used by the §3 hybrid bands. `expected_layout: scanned_pdf` tells the validator
   to skip the futile text-layer pass and escalate straight to Gemini.
3. **Promote:** a newly onboarded format first arrives as "ambiguous" and is LLM-classified;
   adding its registry entry then **promotes it to the free heuristic path** for all future
   uploads — improving accuracy and *lowering* cost over time, with no deploy.

**Why DB rows seeded from YAML (not pure YAML, not hardcoded):** the validator needs to read
profiles at request time without a redeploy; the registry is small reference data; seeding
from a versioned YAML keeps it reviewable in git *and* idempotently re-loadable per stage-0
§2. New profiles can also be inserted via an admin path later without touching the file.

---

## 5. Contracts — request, success, rejection, idempotency

### 5a. Success — `POST …/order-sheet` (HTTP 201; 200 on idempotent re-hit)

```jsonc
{
  "submission_id": "0a9f…",
  "document": {
    "id": "7c12…",
    "kind": "order_sheet",
    "filename": "Order-sheet-1.pdf",
    "mime": "application/pdf",
    "sha256": "e3b0c442…",
    "size_bytes": 482113,
    "page_count": 1,
    "storage_url": "supabase://procurement-documents/submissions/0a9f…/order-sheet/e3b0c442….pdf",
    "validation": {
      "status": "accepted",
      "classified_as": "order_sheet",
      "spec_type": null,
      "client_match": "beau_geste",
      "confidence": 0.91,
      "method": "llm",                 // heuristic | llm | manual
      "signals_matched": ["fingerprint:CR1-02F5-PL02", "anchor:CRIMIE", "size_tokens:S,M,L,XL,XXL"]
    },
    "scan_status": "clean"             // clean | skipped | infected | error
  },
  "submission": {
    "order_sheet":  { "present": true,  "validation_status": "accepted" },
    "spec_sheet":   { "present": false, "validation_status": null },
    "complete": false,
    "ready_for_stage_2": false,
    "blocking": ["spec_sheet missing"]
  }
}
```

### 5b. Rejection — useful diagnostics (HTTP 422; never a bare boolean)

The rejection body must tell the uploader **why** and **what to do** — the prompt's "useful
diagnostics on rejection":

```jsonc
{
  "error": "document_validation_failed",
  "submission_id": "0a9f…",
  "document_fingerprint": { "filename": "BOM_Procurement_Workflow_fixed.pdf",
                            "mime": "application/pdf", "sha256": "9f86d0…", "size_bytes": 557643 },
  "validation": {
    "status": "rejected",
    "reason_code": "not_an_order_sheet",   // see reason_code catalog below
    "expected_kind": "order_sheet",
    "confidence": 0.12,
    "signals_expected": ["order number", "quantity/size grid", "SKU or style refs", "delivery date"],
    "signals_found": ["prose paragraphs", "section headings 'Stage 1'..'Stage 6'"],
    "closest_client_profile": "_generic",
    "method": "heuristic",
    "suggested_fix": "This looks like a process/specification narrative, not a buyer order sheet. Upload the client's order sheet (PDF/XLSX/CSV) containing per-size quantities and an order number."
  }
}
```

**`reason_code` catalog:** `unsupported_mime`, `file_too_large`, `empty_or_corrupt`,
`virus_detected`, `scanner_unavailable`, `not_an_order_sheet`, `not_a_spec_sheet`,
`wrong_slot` (a spec sheet uploaded to the order-sheet endpoint), `needs_manual_review`
(order/spec-shaped but below accept threshold), `submission_locked` (Stage 2 already consumed
it). Each maps to a stable HTTP status (`415` unsupported MIME, `413` too large, `422`
identity failures, `409` locked/slot conflicts, `503` scanner unavailable when fail-closed).

### 5c. Idempotency on re-upload
- **`Document.sha256` is UNIQUE** (existing model). Re-uploading a **byte-identical** file →
  `200` with the **existing** document; **no** new row, **no** re-classification, **no** second
  LLM bill (classification result is cached on the document, keyed by sha256 per stage-0 §4).
- Re-uploading a **different** file into a slot of an **open** submission **supersedes** the
  prior document in that slot (old doc kept for audit, marked `superseded`, new validation
  runs). This is how a user fixes a wrong upload.
- Re-uploading into a submission already **consumed/locked** by Stage 2 → `409
  submission_locked`. Corrections after that go through a new submission (Stage-2/3 revision
  flow, out of scope here).

### 5d. File-size & MIME limits
- **Allowlist:** `application/pdf`, the XLSX mime
  (`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`), `text/csv` — matching
  the workflow doc's "PDF, XLSX, and CSV." MIME is determined by **content sniff (libmagic)**,
  not the client header; a mismatch between extension and sniffed type is itself a reject
  (`unsupported_mime`).
- **Size cap:** `MAX_UPLOAD_MB` (config; default **25 MB** — scanned multi-page PDFs are the
  large case). Enforced by **streaming the body and aborting past the cap** — closing the gap
  flagged in CLAUDE.md §13.4 / stage-0 (the existing `imports` handler reads the whole file
  unbounded). Oversize → `413 file_too_large`.
- Zero-byte / unparseable → `422 empty_or_corrupt`.

---

## 6. Storage — pluggable backend, Supabase as the prod driver

`Document.storage_url` stays a backend-agnostic URI. A thin `StorageBackend` interface
(`put(key, bytes) → url`, `get(key)`, `delete(key)`, `presign(key)`) selected by config:

| `STORAGE_BACKEND` | Driver | Where |
|---|---|---|
| `local` (default in dev/tests) | local filesystem under `LOCAL_STORAGE_DIR` | preserves the SQLite/in-memory test path; no external dep |
| `minio` / `s3` | S3-compatible client | self-hosted staging, consistent with Dockerized Postgres |
| `supabase` | Supabase Storage (service-key, server-side) | **production** |

New config keys (added to `app/core/config.py`, all blank-defaulted except backend):
`storage_backend` (`local`), `local_storage_dir`, `supabase_url`, `supabase_service_key`,
`supabase_bucket`, `max_upload_mb` (`25`), `virus_scan_enabled` (`true`), `clamd_host`/`clamd_port`.

### Bucket / key layout (identical across backends)
Bucket (prod): **`procurement-documents`** (private; access only via the API using the service
key — never a public URL). Keys are **submission-scoped** because the client may be unknown at
upload time:

```
procurement-documents/
├── quarantine/<submission_id>/<kind>/<sha256><ext>   ← lands here first; scanned in place
└── submissions/<submission_id>/
    ├── order-sheet/<sha256><ext>                     ← moved here only after scan = clean
    └── spec-sheet/<sha256><ext>
```

`<sha256>` as the object name gives free dedupe and makes the URL self-verifying. The `local`
backend mirrors the same prefix tree under `LOCAL_STORAGE_DIR`. A file is written to
`quarantine/` first; only a **clean** (or `skipped`) scan promotes it to `submissions/`. The
DB `storage_url` is written only after promotion, so a half-uploaded/infected file is never
referenced by an accepted document.

---

## 7. Virus scan (ClamAV, env-gated)

- **Flag:** `VIRUS_SCAN_ENABLED` (config). Default **ON** in staging/prod; the dev `.env` can
  set it **off** to skip scanning when no `clamd` is running — exactly the "test the scan path,
  then disable when not needed" posture the product owner asked for.
- **ON path:** stream upload → `quarantine/` → scan via `clamd` (sidecar in
  `docker-compose.yml`).
  - **Clean** → promote to `submissions/`, continue to validation, `scan_status = clean`.
  - **Positive hit** → **reject** (`422 virus_detected`), delete the quarantined object, write
    an `audit_log` row; never promoted, never validated.
  - **Scanner unreachable** → **fail closed**: `503 scanner_unavailable`, nothing stored. (No
    "scan later" — an unscanned file never reaches `submissions/`.)
- **OFF path (dev):** skip scanning, store directly, `scan_status = skipped`. The completeness
  gate treats `skipped` as acceptable (§2) so dev can exercise the full flow.
- **Test hook:** the **EICAR** test string must be rejected with `virus_detected` when the flag
  is ON (acceptance §9).

---

## 8. Data-model deltas for Stage 1

Stage-0 already defined `Document` and `SpecSheet`. Stage 1 adds the pairing row, the registry,
and a few validation columns on `Document`. (Listed as the design; migrations are a later
implementation task, registered in `alembic/env.py` per stage-0 §3.)

### 8a. New table — `submission` (the upload batch / pairing key)
| Field | Notes |
|---|---|
| `id` (GUID PK, UUIDMixin + TimestampMixin) | the `submission_id` returned on first upload |
| `client_id?` FK `client` | nullable — client often unknown until Stage-2 parse |
| `created_by` FK `app_user` | the DM/MD uploader |
| `status` enum `SubmissionStatus` | `OPEN` → `COMPLETE` (both slots accepted+clean) → `CONSUMED` (Stage 2 picked it up) / `REJECTED` |
| `order_document_id?` FK `document` | the order-sheet slot |
| `spec_document_id?` FK `document` | the spec-sheet slot |
| `client_order_id?` FK `client_order` | **filled in Stage 2** — resolves "paired by order_id" once the order exists |

This is the clean home for the stage-0 note that `Document` deliberately has no
`client_order_id`: the pairing lives here, and the order link is resolved later in one place.

### 8b. New columns on `Document`
`submission_id` FK `submission`; `size_bytes`; `validation_status` (`PENDING/ACCEPTED/REJECTED/
SUPERSEDED`); `classified_kind`; `classified_spec_type?`; `classification_method`
(`heuristic/llm/manual`); `classification_confidence` Numeric(5,4); `client_match_code`;
`scan_status` (`clean/skipped/infected/error`); `validation_signals` JSONB (the matched/missing
signals returned in the envelope). (`kind`, `sha256`, `mime`, `page_count`, `storage_url`,
`uploaded_by` already exist.)

### 8c. New table — `client_template` (the registry, §4)
`id`, `client_code` (unique with `doc_kind`), `display_name`, `doc_kind`, `language`,
`size_system`, `currency`, `expected_layout`, `accepted_mime` JSONB, `anchors` JSONB,
`fingerprints` JSONB, `spec_type_hint?`, `thresholds` JSONB, `is_active`. Seeded idempotently
from `config/client_templates.yaml`.

### 8d. New enums (procurement `enums.py`)
`SubmissionStatus`, `ValidationStatus`, `ScanStatus`, `ClassificationMethod`, `RejectReason`.
Stored as VARCHAR-of-`.value` per the module's existing string-enum convention
(`app/modules/procurement/enums.py`), **not** native PG enums.

---

## 9. Acceptance criteria

A reviewer must be able to confirm each, using the real `/data` files:

1. **Rejects a random PDF with diagnostics.** Uploading `data/BOM_Procurement_Workflow_fixed.pdf`
   (a process narrative, not an order) to `…/order-sheet` → `422`, `reason_code:
   not_an_order_sheet`, with `signals_expected`/`signals_found` and a human `suggested_fix`.
2. **Accepts all real sample formats:**
   - `data/Order-sheet-1.pdf` → `accepted`, `classified_as: order_sheet`, `client_match:
     beau_geste` (scanned ⇒ `method: llm`).
   - `data/jackiee-order-sheet.pdf` → `accepted`, `order_sheet`, `client_match: the_jackie`.
   - `data/spec_sheet_1.xlsx` → `accepted`, `spec_sheet`, `spec_type: measurement_grid`
     (`method: heuristic`, no LLM).
   - `data/Jackie-cleint-spec-sheet.xlsx` → `accepted`, `spec_sheet`, `spec_type:
     narrative_techpack`.
3. **New client onboarded with no code change.** Adding a `client_template` YAML entry (e.g.
   an "Australia" profile) and re-seeding lets that client's order sheet validate on the free
   heuristic path; no source file is edited or redeployed.
4. **Pairing & gate.** Order + spec uploaded under one `submission_id` →
   `GET /submissions/{id}` reports `complete: true`, `ready_for_stage_2: true`. With only one
   slot filled → `false` plus the blocking reason.
5. **Idempotent re-upload.** Re-uploading a byte-identical file returns the same `document.id`,
   creates no duplicate row, and triggers no second Gemini call (sha256 cache hit).
6. **Limits enforced.** A >`MAX_UPLOAD_MB` file → `413 file_too_large`; a `.docx`/`.png` →
   `415 unsupported_mime` (by content sniff, not extension).
7. **Virus gate.** With `VIRUS_SCAN_ENABLED=true`, an EICAR test file → `422 virus_detected`,
   object quarantined+deleted, `audit_log` row written. With the flag off, upload proceeds with
   `scan_status: skipped`.
8. **No silent guessing.** A borderline order-shaped file scoring between the thresholds is
   **not** auto-accepted — it returns `needs_manual_review`, not a fabricated classification.
9. **Stays in lane.** Stage 1 stores and classifies only; it does **not** populate
   `spec_sheet.measurements`/`attributes` or create a `client_order`/`bom` (Stage 2+).

---

## 10. Review checklist (definition of done — spec only, nothing to run)

- [ ] Separate order-sheet & spec-sheet endpoints, paired by `submission_id`, with a
      `ready_for_stage_2` completeness gate; DM/MD-gated; sync work in a threadpool.
- [ ] The three validation approaches (heuristic / LLM / hybrid) are compared on cost, latency,
      and accuracy, and **(c) hybrid** is recommended and justified against stage-0 §4.
- [ ] The per-client template registry is config-driven (YAML-seeded DB rows); onboarding a new
      client (Australia/Jackiee/…) demonstrably needs **no code change**; a `_generic` fallback
      exists.
- [ ] Success and rejection response shapes are specified; rejection carries useful diagnostics
      (expected vs found signals, confidence, suggested fix, `reason_code`).
- [ ] Idempotency on re-upload (sha256 dedupe + classification cache), supersede-on-correction,
      and lock-after-Stage-2 are defined.
- [ ] Virus scan = real ClamAV gated by `VIRUS_SCAN_ENABLED` (reject on hit, fail closed if
      unreachable; `skipped` when off); file-size + content-sniffed MIME limits enforced.
- [ ] Storage = pluggable backend with the Supabase bucket/prefix layout documented and the
      repo's "no hard Supabase dependency" / SQLite test path preserved; quarantine→promote flow.
- [ ] Acceptance criteria reference the actual `/data` files and cover: random-PDF reject,
      all three formats accepted, new-client-by-config, idempotency, limits, virus gate.
- [ ] No code, migrations, or model edits were produced — spec only.
- [ ] Reviewed / signed off by MD / system stakeholder.
