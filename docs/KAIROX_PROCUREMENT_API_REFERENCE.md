# KairoX ERP — Phase 2
## API Reference

**Every endpoint of the procurement engine**, with purpose, roles, request, response and errors — enough to build the entire frontend against mocks, and enough for a backend developer to use as the contract of record.

**Covers:** Procurement (Stage 1) · BOM (Stage 2/3) · Inventory (Stage 4) · Supplier PO (Stage 5) · Intelligence (Chat) · Auth

**Companion document:** *KairoX Phase-2 System & Business Guide* — the business logic, module architecture and screen design behind these endpoints.

---

## How to use this document

**Frontend developers.** Every endpoint below carries a complete, realistic mock response. Copy them into a mock server (MSW, json-server, or a plain fixtures file) and build the entire UI without a backend. §29 is a ready-made fixture pack with consistent IDs across every endpoint, so one mocked order flows end to end. §30 gives the exact call sequences for each user journey.

**Backend developers.** This is the contract of record: exact paths, exact role gates, exact status codes, exact response keys. Each section names the router file and the service method behind it.

**Everyone.** Endpoints are grouped by stage, in the order a real order travels through them.

---

# Table of Contents

**FOUNDATION**
1. Base URL and versioning
2. Authentication
3. Conventions
4. The error contract
5. The role matrix
6. Endpoint index

**STAGE 1 — INTAKE** (7 endpoints)
7. Submissions and uploads

**STAGE 2/3 — BOM** (23 endpoints)
8. Breakdown and style attachment
9. The BOM tree and its lifecycle
10. Notifications
11. Patterns
12. Admin configuration

**STAGE 4 — INVENTORY** (7 endpoints)
13. Stock master
14. Inventory checks

**STAGE 5 — SUPPLIER PO** (25 endpoints)
15. Suppliers
16. Purchase orders
17. Tracking and webhooks
18. The production board

**INTELLIGENCE** (2 endpoints)
19. Chat

**APPENDICES**
29. The mock data pack
30. Frontend call sequences
31. Quick reference card

---
---

# FOUNDATION

## 1. Base URL and versioning

```
https://<host>/api/v1
```

Every endpoint in this document is prefixed with `/api/v1`. Paths below are written **relative to that prefix** — so `/procurement/submissions` means `https://<host>/api/v1/procurement/submissions`.

Four route groups appear here:

| Group | Prefix | Owned by |
|---|---|---|
| Authentication | `/auth` | `users` module |
| The procurement engine | `/procurement` | Four modules share this prefix by design — Stage 1, 2/3, 4 and 5 |
| Chat | `/chat` | `intelligence` module |
| Health | *(no prefix — `/health`, `/ready`)* | `main.py` |

> **Why four modules share `/procurement`.** The backend was split out of a former monolith, and the URLs were deliberately preserved so no frontend had to change. Do not read anything into the prefix — `/procurement/boms/…` is the BOM module, `/procurement/inventory/…` is the inventory module.

Interactive docs live at `/docs` (Swagger) and `/redoc`.

## 2. Authentication

Self-issued JWT bearer tokens.

### `POST /auth/login`

**Request**
```json
{ "phone": "9876543210", "password": "••••••••" }
```

**Response `200`**
```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9…",
  "token_type": "bearer"
}
```

Send it on every subsequent request:

```
Authorization: Bearer <access_token>
```

### `GET /auth/me`

Returns the current user — use it on app boot to restore the session and to decide which actions to render.

**Response `200`**
```json
{
  "id": "9e1c4a70-0b2d-4c8e-9f11-6a2b3c4d5e6f",
  "name": "Tanveer Ahmed",
  "phone": "9876543210",
  "email": "tanveer@ptexports.com",
  "role": "managing_director",
  "is_active": true
}
```

### `POST /auth/change-password`

**Request**
```json
{ "current_password": "••••••••", "new_password": "••••••••" }
```

**Response `204`** — no body.

### Two exceptions

The following routes are **deliberately unauthenticated** — a supplier's mail client, Amazon SES and Twilio call them, and they carry an opaque per-send token rather than a session:

```
GET  /procurement/t/o/{token}.gif
GET  /procurement/t/c/{token}
POST /procurement/webhooks/ses
POST /procurement/webhooks/twilio/whatsapp
POST /procurement/webhooks/twilio/voice
```

**Do not call these from the frontend.** They are documented in §17 for completeness.

## 3. Conventions

| Aspect | Convention |
|---|---|
| **IDs** | UUID v4, always as a **string** in JSON |
| **Timestamps** | ISO-8601 with timezone: `"2026-09-09T14:32:07.123456+00:00"`. Nullable fields are `null`, never `""` |
| **Dates** | `"2026-09-09"` |
| **Money** | JSON `number`, two decimal places. Computed server-side — **never** send a computed total |
| **Quantities** | JSON `number`, three decimal places |
| **Confidence** | `0.0`–`1.0` |
| **Enums** | Lowercase snake_case strings |
| **Uploads** | `multipart/form-data`, field name **`file`**. Cap `MAX_UPLOAD_MB` (default 25) |
| **Pagination** | `limit` (default 100) + `offset`. Responses carry `count` = **rows in this page**, not the grand total |
| **Empty collections** | `[]`, never `null` |
| **Async operations** | `202 Accepted` + a `task_id`; poll the matching GET |
| **Streaming** | `text/event-stream`. Frames are `data: {json}\n\n`; lines starting with `:` are keep-alives |

**Three rules worth stating explicitly:**

1. **Derived values are always server-computed.** `total_cost`, `bulk_qty`, `bulk_total`, `garment_fob_price`, `available_qty`, `shortfall_qty`, `subtotal`, `cgst`/`sgst`/`igst`, `total` — send the inputs, render what comes back. Do not compute these client-side; you will drift.

2. **`count` is the page size.** No endpoint in Phase 2 returns a grand total. Paginate by fetching until a short page comes back.

3. **Optimistic locking on every edit.** The BOM and PO editors both require `base_revision`. Always send the revision you rendered.

## 4. The error contract

### 4.1 Two shapes

**Shape A — Stage-1 upload rejections.** A rich, flat diagnostic envelope. `error` is at the top level.

```json
{
  "error": "document_validation_failed",
  "reason_code": "not_an_order_sheet",
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "document_fingerprint": {
    "filename": "packing-list.xlsx",
    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "size_bytes": 84213
  },
  "validation": {
    "status": "rejected",
    "reason_code": "not_an_order_sheet",
    "expected_kind": "order_sheet",
    "confidence": 0.31,
    "signals_expected": ["ORDER CONFIRMATION", "TAGLIA", "QTY", "COLORE"],
    "signals_found": ["PACKING LIST", "CARTON", "NET WEIGHT"],
    "closest_client_profile": "BOGGI",
    "method": "llm",
    "suggested_fix": "This looks like a packing list. Upload the order confirmation instead."
  }
}
```

**Shape B — everything else.** FastAPI's `detail` wrapper, containing either a structured object or a plain string.

```json
{ "detail": { "error": "stale_revision", "current_revision": 7 } }
```
```json
{ "detail": "BOM not found." }
```

### 4.2 One parser for both

```js
export function parseError(status, body) {
  if (body?.error) {
    return { code: body.error, reason: body.reason_code, detail: body, status };
  }
  const d = body?.detail;
  if (typeof d === "string") return { code: "message", message: d, status };
  if (d && typeof d === "object") return { code: d.error ?? "unknown", detail: d, status };
  return { code: "unknown_error", status };
}
```

### 4.3 Status codes

| Code | Meaning | Frontend action |
|---|---|---|
| `200` | OK | |
| `201` | Created | |
| `202` | Accepted — queued | Start polling |
| `204` | No content | |
| `302` | Redirect | Tracking links only |
| `400` | Bad request | Invalid tracking signature |
| `401` | Unauthenticated | Redirect to login |
| `403` | Wrong role | Show who *can* act — the body often names them |
| `404` | Not found | |
| `409` | **State conflict** | See §4.4 — the most important family |
| `413` | File too large | Show the cap |
| `415` | Unsupported file type | Show accepted types |
| `422` | Validation failed | Field-level or document-level |
| `500` | Server error | Show `request_id` for support |
| `503` | Dependency unavailable | Virus scanner down — offer retry |

Every `500` carries a correlation id:

```json
{ "detail": "Internal server error", "request_id": "3f9a2b71-…" }
```

**Show that id to the user.** It is how support traces the failure in the logs.

### 4.4 The complete `409` catalogue

| `error` | Endpoint | Meaning | Recovery |
|---|---|---|---|
| `duplicate_content` | Uploads | These exact bytes already exist elsewhere | Upload the correct distinct file |
| `submission_locked` | Uploads | Stage 2 already consumed it | Start a new submission |
| `stale_revision` | BOM/PO edit | Someone else edited first | Reload, re-present, retry |
| `bom_locked` | BOM edit | Approved/locked BOMs reject edits | Read-only |
| `bom_rejected` | BOM edit | Must reopen first | Offer Reopen |
| `cutting_confirmation_required` | BOM approve | Not cutting-confirmed | Disable Approve upfront |
| `not_ready_for_review` | BOM reject | Wrong state | Drive buttons off status |
| `not_rejected` | BOM reopen | Not rejected | Hide Reopen |
| `not_approved` | BOM export / PO send | Wrong state | Hide the action |
| `spec_suggestion_unconfirmed` | Generate BOM | Spec still `suggested` | Disable upfront |
| `bom_not_approved` | Inventory check | BOM not approved | Disable |
| `no_inventory_check` | Generate POs | Check has not run | Offer "run the check" |
| `needs_supplier` | PO submit | No supplier assigned | Open the supplier picker |
| `empty_po` | PO submit | No lines | Disable Submit |
| `not_submittable` | PO submit | Not draft/rejected | Drive off status |
| `not_pending_approval` | PO approve/reject | Wrong state | Drive off status |
| `po_locked` | PO edit / cancel | Sent POs are frozen | Cancel and re-issue |
| `duplicate_name` | Create supplier | Name already exists | Offer the existing supplier |

**Almost every `409` here is avoidable** by driving button state off the status the API already returned.

### 4.5 The `422` catalogue

| `error` | Where |
|---|---|
| `unknown_bom_item` / `unknown_po_item` | The referenced line is not on this record |
| `unsupported_field` | Field not in the editable set |
| `invalid_value` | Not coercible to a number |
| `negative_value` | Negative quantity or price |
| `reason_required` | Reject with an empty reason |
| `name_required` | Create supplier with an empty name |
| `unknown_status` | Board transition to an unknown rung |
| *(Pydantic default)* | Malformed body — `detail` is an array of field errors |

## 5. The role matrix

`MD` Managing Director · `DM` Direct Manager · `CM` Cutting Manager · `HR` · `VW` Viewer

| Endpoint group | MD | DM | CM | HR | VW |
|---|:--:|:--:|:--:|:--:|:--:|
| Submissions + uploads | ✅ | ✅ | | | |
| Breakdown, attachments, generate BOM | ✅ | ✅ | ✅ | ✅ | ✅ |
| Read/edit a BOM, confirm cutting | ✅ | ✅ | ✅ | | |
| Approve / reject / export a BOM | ✅ | | | | |
| Reopen a BOM | ✅ | ✅ | | | |
| Notifications | ✅ | ✅ | ✅ | ✅ | ✅ |
| Upload a pattern | ✅ | ✅ | ✅ | ✅ | ✅ |
| Admin configuration | ✅ | ✅ | | | |
| Inventory sync + run check | ✅ | ✅ | | | |
| Inventory reads | ✅ | ✅ | | | ✅ |
| Supplier reads | ✅ | ✅ | | | ✅ |
| Supplier create / edit / import | ✅ | ✅ | | | |
| Supplier deactivate / reactivate | ✅ | | | | |
| PO reads | ✅ | ✅ | | | ✅ |
| PO generate / edit / submit / send / cancel / acknowledge | ✅ | ✅ | | | |
| PO approve / reject — **leather** | ✅ | | ✅ | | |
| PO approve / reject — **accessory & service** | ✅ | ✅ | | ✅ | |
| Production board — read | ✅ | ✅ | | | ✅ |
| Production board — transition | ✅ | ✅ | ✅ | | |
| Chat | ✅ | ✅ | ✅ | ✅ | ✅ |

Notes:
- **MD is a superuser** for PO approval — they may approve any PO regardless of type.
- Breakdown, attachment and generate-BOM endpoints are gated only by `get_current_user` — any authenticated non-legacy user reaches them. **The UI should still present them as DM/MD tools.**
- The legacy `employee` role is blocked at the router level for every route in this document.

## 6. Endpoint index

### Stage 1 — Intake (7)
| Method | Path | Roles |
|---|---|:--:|
| `POST` | `/procurement/submissions` | DM MD |
| `POST` | `/procurement/submissions/{submission_id}/order-sheet` | DM MD |
| `POST` | `/procurement/upload/order-sheet` | DM MD |
| `POST` | `/procurement/submissions/{submission_id}/spec-sheet` | DM MD |
| `POST` | `/procurement/upload/spec-sheet` | DM MD |
| `GET` | `/procurement/submissions/{submission_id}` | DM MD |
| `GET` | `/procurement/submissions/{submission_id}/documents/{document_id}` | DM MD |

### Stage 2/3 — BOM (23)
| Method | Path | Roles |
|---|---|:--:|
| `POST` | `/procurement/submissions/{submission_id}/order-breakdown` | any |
| `GET` | `/procurement/submissions/{submission_id}/order-breakdown` | any |
| `POST` | `/procurement/order-styles/{order_style_id}/attachments` | any |
| `POST` | `/procurement/order-styles/{order_style_id}/generate-bom` | any |
| `GET` | `/procurement/boms/{bom_id}` | CM MD DM |
| `PATCH` | `/procurement/boms/{bom_id}/items` | CM MD DM |
| `POST` | `/procurement/boms/{bom_id}/confirm-cutting` | CM MD DM |
| `POST` | `/procurement/boms/{bom_id}/approve` | MD |
| `POST` | `/procurement/boms/{bom_id}/reject` | MD |
| `POST` | `/procurement/boms/{bom_id}/reopen` | DM MD |
| `POST` | `/procurement/boms/{bom_id}/export` | MD |
| `GET` | `/procurement/notifications` | any |
| `GET` | `/procurement/notifications/stream` | any |
| `POST` | `/procurement/notifications/{notification_id}/open` | any |
| `POST` | `/procurement/patterns` | any |
| `PUT` | `/procurement/admin/dxf-yields/{species}` | DM MD |
| `POST` | `/procurement/admin/fabric-roles` | DM MD |
| `GET` | `/procurement/admin/cost-catalog` | DM MD |
| `PUT` | `/procurement/admin/cost-catalog/{garment_code}` | DM MD |
| `GET` | `/procurement/admin/checks` | DM MD |
| `PUT` | `/procurement/admin/checks/{client_code}` | DM MD |
| `GET` | `/procurement/admin/pom-dictionary` | DM MD |
| `POST` | `/procurement/admin/pom-dictionary` | DM MD |

### Stage 4 — Inventory (7)
| Method | Path | Roles |
|---|---|:--:|
| `POST` | `/procurement/inventory/preview` | DM MD |
| `POST` | `/procurement/inventory/commit` | DM MD |
| `GET` | `/procurement/inventory/items` | DM MD VW |
| `POST` | `/procurement/boms/{bom_id}/inventory-check` | DM MD |
| `GET` | `/procurement/boms/{bom_id}/inventory-check` | DM MD VW |
| `GET` | `/procurement/inventory-checks/{check_id}` | DM MD VW |
| `GET` | `/procurement/inventory-checks` | DM MD VW |

### Stage 5 — Supplier PO (25)
| Method | Path | Roles |
|---|---|:--:|
| `POST` | `/procurement/suppliers/import/preview` | DM MD |
| `POST` | `/procurement/suppliers/import/commit` | DM MD |
| `GET` | `/procurement/suppliers` | DM MD VW |
| `GET` | `/procurement/suppliers/{supplier_id}` | DM MD VW |
| `POST` | `/procurement/suppliers` | DM MD |
| `PATCH` | `/procurement/suppliers/{supplier_id}` | DM MD |
| `DELETE` | `/procurement/suppliers/{supplier_id}` | MD |
| `POST` | `/procurement/suppliers/{supplier_id}/reactivate` | MD |
| `POST` | `/procurement/boms/{bom_id}/generate-pos` | DM MD |
| `GET` | `/procurement/pos` | DM MD VW |
| `GET` | `/procurement/pos/{po_id}` | DM MD VW |
| `PATCH` | `/procurement/pos/{po_id}/items` | DM MD |
| `POST` | `/procurement/pos/{po_id}/submit` | DM MD |
| `POST` | `/procurement/pos/{po_id}/approve` | CM MD DM HR |
| `POST` | `/procurement/pos/{po_id}/reject` | CM MD DM HR |
| `POST` | `/procurement/pos/{po_id}/send` | DM MD |
| `POST` | `/procurement/pos/{po_id}/cancel` | DM MD |
| `POST` | `/procurement/pos/{po_id}/acknowledge` | DM MD |
| `GET` | `/procurement/t/o/{token}.gif` | **none** |
| `GET` | `/procurement/t/c/{token}` | **none** |
| `POST` | `/procurement/webhooks/ses` | **none** |
| `POST` | `/procurement/webhooks/twilio/whatsapp` | **none** |
| `POST` | `/procurement/webhooks/twilio/voice` | **none** |
| `GET` | `/procurement/production-tracking` | DM MD VW |
| `POST` | `/procurement/production-tracking/{tracking_id}/transition` | MD DM CM |

### Intelligence (2)
| Method | Path | Roles |
|---|---|:--:|
| `POST` | `/chat` | any |
| `POST` | `/chat/stream` | any |

**Total: 64 endpoints** (+ 3 auth, + 2 health).

---
---

# STAGE 1 — INTAKE

*Router:* `app/modules/procurement/router.py` · *Service:* `ProcurementService`

## 7. Submissions and uploads

---

### 7.1 `POST /procurement/submissions`

**Open an empty upload batch.**

Creates the container that pairs an order sheet with a spec sheet. The client may be unknown at this point — it is derived from the order sheet during Stage 2 — so `client_id` is optional.

**Roles:** DM · MD

**Request body** *(optional — may be omitted entirely)*
```json
{ "client_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7" }
```

**Response `201`**
```json
{
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "status": "open"
}
```

**Frontend note.** You do not have to call this first. The two `/upload/*` endpoints create a submission automatically when a valid file arrives. Use this endpoint when you want to show an empty two-slot workspace before the user picks any file.

---

### 7.2 `POST /procurement/submissions/{submission_id}/order-sheet`
### 7.3 `POST /procurement/submissions/{submission_id}/spec-sheet`

**Upload and validate a document into a slot.**

Runs the full pipeline: size gate → virus scan → MIME sniff → classification → persist. Accepted documents fill the slot and supersede whatever was there before. Rejected documents still create a `Document` row, so re-uploading the same bytes replays the verdict for free.

**Roles:** DM · MD

**Path:** `submission_id` (UUID)

**Query:** `force` (boolean, default `false`) — accept a `needs_manual_review` document anyway. **Hard rejections are never overridable.**

**Request:** `multipart/form-data`, field `file`

```
POST /api/v1/procurement/submissions/a3f2b8c1-…/order-sheet
Content-Type: multipart/form-data; boundary=----X

------X
Content-Disposition: form-data; name="file"; filename="ORDER BOGGI SS27.xlsx"
Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet

<bytes>
------X--
```

**Response `201`**
```json
{
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "document": {
    "id": "b1c2d3e4-5f60-7182-93a4-b5c6d7e8f901",
    "kind": "order_sheet",
    "filename": "ORDER BOGGI SS27.xlsx",
    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "size_bytes": 92416,
    "page_count": null,
    "storage_url": "s3://kairox-docs/submissions/a3f2b8c1/e3b0c442.xlsx",
    "validation": {
      "status": "accepted",
      "classified_as": "order_sheet",
      "spec_type": null,
      "client_match": "BOGGI",
      "confidence": 0.94,
      "method": "heuristic",
      "llm_label": null,
      "llm_model": null,
      "signals_matched": ["ORDER CONFIRMATION", "TAGLIA", "COLORE", "QTY"],
      "signals_expected": ["ORDER CONFIRMATION", "TAGLIA", "QTY", "COLORE"],
      "signals_found": ["ORDER CONFIRMATION", "TAGLIA", "COLORE", "QTY", "STAGIONE"],
      "reason_code": null,
      "suggested_fix": null
    },
    "scan_status": "clean"
  },
  "submission": {
    "order_sheet": { "present": true,  "validation_status": "accepted" },
    "spec_sheet":  { "present": false, "validation_status": null },
    "complete": false,
    "ready_for_stage_2": false,
    "blocking": ["spec_sheet missing"]
  }
}
```

**Errors**

| Code | `reason_code` | When |
|---|---|---|
| `413` | `file_too_large` | Over `MAX_UPLOAD_MB` |
| `415` | `unsupported_mime` | Not an accepted type |
| `422` | `empty_or_corrupt` | Unparseable |
| `422` | `virus_detected` | Infected — also writes an audit row |
| `422` | `not_an_order_sheet` / `not_a_spec_sheet` | Wrong document kind |
| `422` | `wrong_slot` | Right kind, wrong slot |
| `422` | `needs_manual_review` | Inconclusive — **retry with `?force=true`** |
| `409` | `duplicate_content` | These bytes already exist elsewhere |
| `409` | `submission_locked` | Stage 2 already consumed this submission |
| `503` | `scanner_unavailable` | Virus scanner down — retry |
| `404` | — | Submission not found |

**A `422` rejection** (shape A):
```json
{
  "error": "document_validation_failed",
  "reason_code": "not_an_order_sheet",
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "document_fingerprint": {
    "filename": "packing-list.xlsx",
    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "sha256": "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "size_bytes": 84213
  },
  "validation": {
    "status": "rejected",
    "reason_code": "not_an_order_sheet",
    "expected_kind": "order_sheet",
    "confidence": 0.31,
    "signals_expected": ["ORDER CONFIRMATION", "TAGLIA", "QTY", "COLORE"],
    "signals_found": ["PACKING LIST", "CARTON", "NET WEIGHT"],
    "closest_client_profile": "BOGGI",
    "method": "llm",
    "suggested_fix": "This looks like a packing list. Upload the order confirmation instead."
  }
}
```

**A `409 duplicate_content`:**
```json
{
  "error": "duplicate_content",
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "document_fingerprint": {
    "filename": "ORDER BOGGI SS27.xlsx",
    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "size_bytes": 92416
  },
  "conflict": {
    "attempted_slot": "spec_sheet",
    "existing_kind": "order_sheet",
    "existing_validation_status": "accepted",
    "same_submission": true,
    "existing_submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
    "suggested_fix": "This file is already accepted as the other slot of this submission; upload the correct, distinct sheet for this slot."
  }
}
```

**Frontend notes**

- The `validation` block is the whole rejection screen. Render `signals_expected` and `signals_found` **side by side** — the difference is the explanation.
- Show a **Force accept** button *only* when `reason_code == "needs_manual_review"`.
- Re-uploading an identical file is safe and cheap — it replays the cached verdict.
- `blocking` is a plain-language checklist. Render it directly.

---

### 7.4 `POST /procurement/upload/order-sheet`
### 7.5 `POST /procurement/upload/spec-sheet`

**Upload without an existing submission.**

Identical to 7.2/7.3 except that a **new submission is created only if the file passes validation**. A rejected file returns diagnostics with no submission created.

**Roles:** DM · MD · **Query:** `force` · **Request:** `multipart/form-data`, field `file`

**Response `201`** — identical shape to 7.2, with a freshly minted `submission_id`.

> **Important:** capture the returned `submission_id` and use it for the paired upload. Otherwise you create two orphaned single-slot submissions.

---

### 7.6 `GET /procurement/submissions/{submission_id}`

**Submission status and the Stage-2 readiness gate.**

Poll this after each upload to drive the workspace.

**Roles:** DM · MD

**Response `200`**
```json
{
  "order_sheet": { "present": true, "validation_status": "accepted" },
  "spec_sheet":  { "present": true, "validation_status": "accepted" },
  "complete": true,
  "ready_for_stage_2": true,
  "blocking": []
}
```

**Not ready:**
```json
{
  "order_sheet": { "present": true,  "validation_status": "accepted" },
  "spec_sheet":  { "present": false, "validation_status": "needs_manual_review" },
  "complete": false,
  "ready_for_stage_2": false,
  "blocking": ["spec_sheet needs_manual_review"]
}
```

`ready_for_stage_2` is true when **both** slots are `accepted` **and** neither has a blocking `scan_status`. `blocking` also carries entries like `"order_sheet scan_status=infected"`.

**Errors:** `404` submission not found.

---

### 7.7 `GET /procurement/submissions/{submission_id}/documents/{document_id}`

**The full validation report for one document.**

Use it to show details for a superseded document, or to re-open a rejection without re-uploading.

**Roles:** DM · MD

**Response `200`**
```json
{
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "document": {
    "id": "b1c2d3e4-5f60-7182-93a4-b5c6d7e8f901",
    "kind": "order_sheet",
    "filename": "ORDER BOGGI SS27.xlsx",
    "mime": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "size_bytes": 92416,
    "page_count": null,
    "storage_url": "s3://kairox-docs/submissions/a3f2b8c1/e3b0c442.xlsx",
    "validation": {
      "status": "accepted",
      "classified_as": "order_sheet",
      "spec_type": null,
      "client_match": "BOGGI",
      "confidence": 0.94,
      "method": "heuristic",
      "llm_label": null,
      "llm_model": null,
      "signals_matched": ["ORDER CONFIRMATION", "TAGLIA", "COLORE", "QTY"],
      "signals_expected": ["ORDER CONFIRMATION", "TAGLIA", "QTY", "COLORE"],
      "signals_found": ["ORDER CONFIRMATION", "TAGLIA", "COLORE", "QTY", "STAGIONE"],
      "reason_code": null,
      "suggested_fix": null
    },
    "scan_status": "clean"
  }
}
```

**Errors:** `404` — document not found, or it does not belong to this submission.

---
---
# STAGE 2/3 — BOM

*Router:* `app/modules/bom/router.py` · *Services:* `BomService`, `NotificationService`

## 8. Breakdown and style attachment

---

### 8.1 `POST /procurement/submissions/{submission_id}/order-breakdown`

**Queue extraction of the order document into a style list.**

Returns immediately. AI extraction takes 30–200 seconds and runs in a Celery worker. Atomically claims the submission so two clicks cannot both enqueue.

**Roles:** any authenticated user

**Request:** no body

**Response `202` — freshly queued**
```json
{
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "status": "queued",
  "task_id": "c7f21a90-3b4c-5d6e-7f80-91a2b3c4d5e6"
}
```

**Response `202` — already extracted**
```json
{ "submission_id": "a3f2b8c1-…", "status": "already_ready", "task_id": null }
```

**Response `202` — a worker is mid-flight**
```json
{ "submission_id": "a3f2b8c1-…", "status": "already_processing", "task_id": null }
```

**Errors**

| Code | Body | When |
|---|---|---|
| `409` | `"submission not ready: order document not accepted, or already claimed"` | The order slot is not `accepted`, or someone else claimed it |
| `404` | — | Submission not found |

**Frontend handling**

| `status` | Do |
|---|---|
| `queued` | Start polling 8.2 every 3–5s |
| `already_processing` | Start polling 8.2 — someone else started it |
| `already_ready` | Fetch 8.2 once; do not poll |

---

### 8.2 `GET /procurement/submissions/{submission_id}/order-breakdown`

**Read the breakdown, or its progress.**

**Roles:** any authenticated user

**Response `200` — ready**
```json
{
  "submission_id": "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  "status": "ready",
  "styles": [
    {
      "id": "d4e5f607-1829-3a4b-5c6d-7e8f90a1b2c3",
      "style_signature": "CLERMONT",
      "style_name": "CLERMONT",
      "material": "SHEEP GLASS",
      "qty": 60,
      "per_size_qty": { "46": 10, "48": 20, "50": 20, "52": 10 },
      "colors": [
        {
          "color_key": "BLACK",
          "color_label": "NERO / BLACK",
          "qty": 40,
          "per_size_qty": { "46": 6, "48": 14, "50": 14, "52": 6 },
          "warnings": []
        },
        {
          "color_key": "COGNAC",
          "color_label": "COGNAC",
          "qty": 20,
          "per_size_qty": { "46": 4, "48": 6, "50": 6, "52": 4 },
          "warnings": []
        }
      ],
      "warnings": [],
      "spec_document_id": "e5f60718-293a-4b5c-6d7e-8f90a1b2c3d4",
      "spec_match_status": "suggested",
      "pattern_reference_id": "f6071829-3a4b-5c6d-7e8f-90a1b2c3d4e5",
      "dxf_match_status": "suggested",
      "bom_id": null
    },
    {
      "id": "07182930-4b5c-6d7e-8f90-a1b2c3d4e5f6",
      "style_signature": "CARNABY",
      "style_name": "CARNABY",
      "material": "GOAT SUEDE",
      "qty": 40,
      "per_size_qty": { "48": 15, "50": 15, "52": 10 },
      "colors": [
        {
          "color_key": "TAUPE",
          "color_label": "TAUPE",
          "qty": 40,
          "per_size_qty": { "48": 15, "50": 15, "52": 10 },
          "warnings": []
        }
      ],
      "warnings": ["size_column_ambiguous"],
      "spec_document_id": null,
      "spec_match_status": "none",
      "pattern_reference_id": "1829304b-5c6d-7e8f-90a1-b2c3d4e5f607",
      "dxf_match_status": "confirmed",
      "bom_id": null
    }
  ],
  "warnings": []
}
```

**Response `200` — still working**
```json
{ "submission_id": "a3f2b8c1-…", "status": "processing", "styles": [], "warnings": [] }
```

**Response `200` — nothing yet**
```json
{ "submission_id": "a3f2b8c1-…", "status": "not_started", "styles": [], "warnings": [] }
```

**Field reference**

| Field | Meaning |
|---|---|
| `style_signature` | The cross-order-stable key. Drives the DCM memory |
| `qty` / `per_size_qty` | Style totals. `per_size_qty` sums to `qty` |
| `colors[]` | The colour dimension. Each colour's `per_size_qty` sums to its own `qty`; the colours sum to the style |
| `spec_match_status` | `none` \| `suggested` \| **`confirmed`** — a `suggested` spec **blocks BOM generation** |
| `dxf_match_status` | Same vocabulary. Never blocks — a missing DXF only lowers DCM confidence |
| `warnings[]` | Extraction uncertainties, e.g. `size_column_ambiguous`, `qty_mismatch` |
| `bom_id` | Null until generated — **this is your "done" signal** when polling generation |

---

### 8.3 `POST /procurement/order-styles/{order_style_id}/attachments`

**Confirm or override the suggested spec and pattern for one style.**

Synchronous — two column writes, no AI. This is the human click that turns `suggested` into `confirmed`.

**Roles:** any authenticated user

**Request — accept both current suggestions**
```json
{}
```

**Request — override the spec with a specific document**
```json
{ "spec_document_id": "aabbccdd-1122-3344-5566-778899aabbcc" }
```

**Request — detach the spec**
```json
{ "clear_spec": true }
```

**Request — a mixed action**
```json
{
  "spec_document_id": "aabbccdd-1122-3344-5566-778899aabbcc",
  "clear_dxf": true
}
```

**Body reference**

| Field | Type | Effect |
|---|---|---|
| `spec_document_id` | UUID? | Bind this document → `confirmed` |
| `pattern_reference_id` | UUID? | Bind this pattern → `confirmed` |
| `clear_spec` | bool | Detach → `none` |
| `clear_dxf` | bool | Detach → `none` |

**Rules**
- An explicit id **overrides** any suggestion and confirms it.
- Omitting an id **accepts the current suggestion** (if any) and confirms it.
- `clear_x` and `x_id` together is a contradiction → `422`.

**Response `200`** — the full updated style, identical in shape to one element of 8.2's `styles[]`:
```json
{
  "id": "d4e5f607-1829-3a4b-5c6d-7e8f90a1b2c3",
  "style_signature": "CLERMONT",
  "style_name": "CLERMONT",
  "material": "SHEEP GLASS",
  "qty": 60,
  "per_size_qty": { "46": 10, "48": 20, "50": 20, "52": 10 },
  "colors": [ /* … */ ],
  "warnings": [],
  "spec_document_id": "aabbccdd-1122-3344-5566-778899aabbcc",
  "spec_match_status": "confirmed",
  "pattern_reference_id": null,
  "dxf_match_status": "none",
  "bom_id": null
}
```

**Errors**

| Code | `error` | When |
|---|---|---|
| `404` | `order_style_not_found` | Unknown style |
| `422` | *(Pydantic)* | `clear_spec` with `spec_document_id`, or `clear_dxf` with `pattern_reference_id` |

---

### 8.4 `POST /procurement/order-styles/{order_style_id}/generate-bom`

**Queue BOM generation for one confirmed style.**

Preconditions are checked **before** queuing, so you get a `4xx` immediately rather than a task failure later.

**Roles:** any authenticated user · **Request:** no body

**Response `202` — queued**
```json
{
  "order_style_id": "d4e5f607-1829-3a4b-5c6d-7e8f90a1b2c3",
  "status": "queued",
  "bom_id": null,
  "task_id": "9a8b7c6d-5e4f-3021-a1b2-c3d4e5f60718"
}
```

**Response `202` — already generated (idempotent replay)**
```json
{
  "order_style_id": "d4e5f607-…",
  "status": "already_generated",
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "task_id": null
}
```

**Errors**

| Code | `error` | When | Fix |
|---|---|---|---|
| `404` | `order style not found` | Unknown style | — |
| `422` | `spec suggestion is unconfirmed — confirm or clear it before generating` | `spec_match_status == "suggested"` | Call 8.3 first |

> **The rule:** a style may be generated with **no** spec (`none` → the BOM carries a `spec_pending` warning). It may **not** be generated with a spec nobody confirmed.

**Polling for completion.** There is no dedicated status endpoint. Poll 8.2 and watch for `bom_id` on that style to become non-null.

---

## 9. The BOM tree and its lifecycle

---

### 9.1 `GET /procurement/boms/{bom_id}`

**The editable BOM tree.**

**Roles:** Cutting Manager · MD · DM

**Response `200`**
```json
{
  "id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "draft",
  "revision": 1,
  "currency": "USD",
  "order_qty": 60,
  "garment_fob_price": 107.25,
  "bulk_total": 6435.00,
  "cutting_confirmed_at": null,
  "approved_at": null,
  "rejection_reason": null,
  "export_document_id": null,
  "exported_at": null,
  "items": [
    {
      "id": "aa000001-0000-0000-0000-000000000001",
      "category": "main_material",
      "name": "SHEEP GLASS",
      "material_color": "BLACK",
      "qty_per_garment": 34.500,
      "uom": "dm2",
      "unit_price": 1.80,
      "bulk_qty": 2070.000,
      "total_cost": 62.10,
      "dcm_source": "template",
      "dcm_confidence": 0.9500,
      "annotation": null
    },
    {
      "id": "aa000001-0000-0000-0000-000000000002",
      "category": "sub_material",
      "name": "GOAT SUEDE",
      "material_color": "BLACK",
      "qty_per_garment": 2.600,
      "uom": "dm2",
      "unit_price": 2.10,
      "bulk_qty": 156.000,
      "total_cost": 5.46,
      "dcm_source": "ai_estimate",
      "dcm_confidence": 0.5000,
      "annotation": "estimated from POM area — confirm at cutting"
    },
    {
      "id": "aa000001-0000-0000-0000-000000000003",
      "category": "lining",
      "name": "VISCOSE LINING",
      "material_color": "BLACK",
      "qty_per_garment": 1.200,
      "uom": "mtr",
      "unit_price": 3.40,
      "bulk_qty": 72.000,
      "total_cost": 4.08,
      "dcm_source": "similar_style",
      "dcm_confidence": 0.7000,
      "annotation": null
    },
    {
      "id": "aa000001-0000-0000-0000-000000000004",
      "category": "thread",
      "name": "POLY THREAD 40/2",
      "material_color": "BLACK",
      "qty_per_garment": 120.000,
      "uom": "mtr",
      "unit_price": 0.01,
      "bulk_qty": 7200.000,
      "total_cost": 1.20,
      "dcm_source": null,
      "dcm_confidence": null,
      "annotation": null
    },
    {
      "id": "aa000001-0000-0000-0000-000000000005",
      "category": "accessory",
      "name": "YKK ZIP #5 60CM",
      "material_color": "BLACK",
      "qty_per_garment": 1.000,
      "uom": "pcs",
      "unit_price": 1.41,
      "bulk_qty": 60.000,
      "total_cost": 1.41,
      "dcm_source": null,
      "dcm_confidence": null,
      "annotation": null
    },
    {
      "id": "aa000001-0000-0000-0000-000000000006",
      "category": "manufacturing",
      "name": "CUTTING + STITCHING",
      "material_color": null,
      "qty_per_garment": 1.000,
      "uom": null,
      "unit_price": 30.00,
      "bulk_qty": 60.000,
      "total_cost": 30.00,
      "dcm_source": null,
      "dcm_confidence": null,
      "annotation": null
    },
    {
      "id": "aa000001-0000-0000-0000-000000000007",
      "category": "packaging",
      "name": "POLYBAG + CARTON",
      "material_color": null,
      "qty_per_garment": 1.000,
      "uom": null,
      "unit_price": 3.00,
      "bulk_qty": 60.000,
      "total_cost": 3.00,
      "dcm_source": null,
      "dcm_confidence": null,
      "annotation": null
    },
    {
      "id": "aa000001-0000-0000-0000-000000000008",
      "category": "fob_charge",
      "name": "FOB CHARGE",
      "material_color": null,
      "qty_per_garment": 1.000,
      "uom": null,
      "unit_price": 5.00,
      "bulk_qty": 60.000,
      "total_cost": 5.00,
      "dcm_source": null,
      "dcm_confidence": null,
      "annotation": null
    }
  ]
}
```

**Field reference**

| Field | Meaning |
|---|---|
| `revision` | **Send this as `base_revision` on every edit** |
| `order_qty` | Units ordered — the multiplier for every bulk figure |
| `garment_fob_price` | Σ of every line's `total_cost` — the per-garment FOB price |
| `bulk_total` | Σ of every line's bulk amount = `order_qty × garment_fob_price` |
| `qty_per_garment` | **For material lines this IS the DCM** |
| `bulk_qty` | `order_qty × qty_per_garment` — **this is what the inventory check calls `required_qty`** |
| `total_cost` | `qty_per_garment × unit_price` — per-garment, not bulk |
| `dcm_source` | `template` \| `dxf` \| `similar_style` \| `ai_estimate` \| `provisional` \| `manual`. **Null for non-material lines** |
| `dcm_confidence` | 0–1. Highlight anything below 0.70 |
| `annotation` | Free text from the generator — usually why an estimate was used |

**Errors:** `404` — `"BOM not found."`

---

### 9.2 `PATCH /procurement/boms/{bom_id}/items`

**Bulk-edit BOM lines under an optimistic revision lock.**

**Roles:** Cutting Manager · MD · DM

**Request**
```json
{
  "base_revision": 1,
  "edits": [
    { "bom_item_id": "aa000001-0000-0000-0000-000000000002", "field": "dcm",        "value": 3.1 },
    { "bom_item_id": "aa000001-0000-0000-0000-000000000003", "field": "unit_price", "value": 3.6 }
  ]
}
```

**Body reference**

| Field | Type | Notes |
|---|---|---|
| `base_revision` | int ≥ 1 | The revision you rendered |
| `edits` | array, min 1 | |
| `edits[].bom_item_id` | UUID | Must belong to this BOM |
| `edits[].field` | string | **Only** `dcm`, `qty_per_garment`, `unit_price` |
| `edits[].value` | number | Must be ≥ 0 |

`dcm` and `qty_per_garment` are the same column — `dcm` is the friendlier name for material lines.

**Response `200`**
```json
{
  "revision": 2,
  "reconfirm_required": false,
  "recomputed": {
    "id": "11223344-5566-7788-99aa-bbccddeeff00",
    "status": "draft",
    "revision": 2,
    "currency": "USD",
    "order_qty": 60,
    "garment_fob_price": 108.25,
    "bulk_total": 6495.00,
    "cutting_confirmed_at": null,
    "approved_at": null,
    "rejection_reason": null,
    "export_document_id": null,
    "exported_at": null,
    "items": [
      {
        "id": "aa000001-0000-0000-0000-000000000002",
        "category": "sub_material",
        "name": "GOAT SUEDE",
        "material_color": "BLACK",
        "qty_per_garment": 3.100,
        "uom": "dm2",
        "unit_price": 2.10,
        "bulk_qty": 186.000,
        "total_cost": 6.51,
        "dcm_source": "manual",
        "dcm_confidence": 1.0000,
        "annotation": "estimated from POM area — confirm at cutting"
      }
    ]
  }
}
```

**Three behaviours to build for:**

1. **Editing a quantity on a material line stamps `dcm_source: "manual"`, confidence `1.0000`.** The badge changes colour in the response — render the returned tree, do not patch locally.
2. **`reconfirm_required: true`** means the BOM had been cutting-confirmed and your quantity edit **revoked it**. The status in `recomputed` drops to `"draft"`. Show a prominent banner.
3. **`recomputed` is the complete new tree.** Replace your local state with it.

**Errors**

| Code | `error` | Extra | Meaning |
|---|---|---|---|
| `409` | `stale_revision` | `current_revision` | Reload and retry |
| `409` | `bom_locked` | | Approved/locked — read-only |
| `409` | `bom_rejected` | | Reopen it first |
| `422` | `unknown_bom_item` | `bom_item_id` | Not on this BOM |
| `422` | `unsupported_field` | `field` | Not in the editable set |
| `422` | `invalid_value` | `bom_item_id`, `field`, `value` | Not a number |
| `422` | `negative_value` | `bom_item_id`, `field`, `value` | Below zero |
| `404` | | | BOM not found |

```json
{ "detail": { "error": "stale_revision", "current_revision": 3 } }
```

---

### 9.3 `POST /procurement/boms/{bom_id}/confirm-cutting`

**The cutting-manager gate — and the learning step.**

Moves the BOM to `ready_for_review`, **writes every confirmed material quantity into the consumption memory**, records DXF yield observations, and notifies the MD and DM.

**Roles:** Cutting Manager (MD/DM bypass as superusers) · **Request:** no body

**Response `200`**
```json
{
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "ready_for_review",
  "cutting_confirmed_at": "2026-09-09T11:42:03.881204+00:00",
  "templates_backfilled": 3,
  "notifications_created": 2
}
```

| Field | Meaning |
|---|---|
| `templates_backfilled` | Material lines written into the DCM memory. **Surface this** — it is the system getting smarter |
| `notifications_created` | Usually 2 (MD + DM). Each carries its own 2-hour escalation clock |

**Errors**

| Code | `error` | Extra |
|---|---|---|
| `409` | `invalid_state_for_confirmation` | `current_status` — only `draft` or `ready_for_review` are allowed |
| `404` | | BOM not found |

---

### 9.4 `POST /procurement/boms/{bom_id}/approve`

**MD approval — the moment the order becomes real.**

Materialises the Client → Order → Style → SKU hierarchy, advances the production board, and **runs the inventory check**.

**Roles:** **MD only**

**Request** *(optional)*
```json
{ "lock": false }
```

`lock: true` sets status to `locked` instead of `approved`. Functionally identical; `locked` states intent more strongly.

**Response `200`**
```json
{
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "approved",
  "inventory_check_id": "cc001122-3344-5566-7788-99aabbccddee"
}
```

**`inventory_check_id` may be `null`** if the check failed. The BOM is still approved — the check is best-effort. Offer a "run the check" action when it is null.

**Errors**

| Code | `error` | Meaning |
|---|---|---|
| `409` | `cutting_confirmation_required` | `cutting_confirmed_at` is null. **Disable Approve upfront** |
| `404` | | BOM not found |

```json
{
  "detail": {
    "error": "cutting_confirmation_required",
    "message": "The cutting manager must confirm the BOM before approval."
  }
}
```

**Frontend note.** `inventory_check_id` is your navigation target — route straight to the check result. This is the natural hand-off point in the UI.

---

### 9.5 `POST /procurement/boms/{bom_id}/reject`

**MD rejection with a mandatory reason.**

**Roles:** **MD only**

**Request**
```json
{ "reason": "Goat suede DCM looks 40% high against the CR1-02F5 template. Please re-check." }
```

**Response `200`**
```json
{
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "rejected",
  "rejection_reason": "Goat suede DCM looks 40% high against the CR1-02F5 template. Please re-check."
}
```

**Errors**

| Code | `error` | Extra |
|---|---|---|
| `422` | `reason_required` | Empty or whitespace-only |
| `409` | `bom_locked` | An approved BOM cannot be rejected |
| `409` | `not_ready_for_review` | `current_status` |
| `404` | | BOM not found |

---

### 9.6 `POST /procurement/boms/{bom_id}/reopen`

**Reopen a rejected BOM for editing.**

Bumps the revision and **clears the cutting confirmation** — the whole chain restarts properly.

**Roles:** DM · MD · **Request:** no body

**Response `200`**
```json
{
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "draft",
  "revision": 2
}
```

**Errors:** `409 not_rejected` (with `current_status`) · `404`

**Frontend note.** Warn the user that reopening clears the cutting confirmation and the BOM will need re-confirming.

---

### 9.7 `POST /procurement/boms/{bom_id}/export`

**Render and store the BOM as a PDF.**

Deduplicated by sha256. Re-exporting an already-exported BOM returns the existing document rather than making a second one.

**Roles:** **MD only** · **Request:** no body

**Response `200`**
```json
{
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "exported",
  "export_document_id": "dd112233-4455-6677-8899-aabbccddeeff",
  "sha256": "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
  "mime": "application/pdf",
  "storage_url": "s3://kairox-docs/exports/11223344/5891b5b5.pdf"
}
```

**A note on `mime`.** The renderer falls back WeasyPrint → ReportLab → raw HTML, so `mime` can be `text/html` and the extension `.html` if neither PDF library is available. **Branch on `mime`; do not assume PDF.**

**Errors:** `409 not_approved` · `404`

---

## 10. Notifications

---

### 10.1 `GET /procurement/notifications`

**The caller's notification rows.**

**Roles:** any authenticated user · **Query:** `unread_only` (bool, default `false`)

**Response `200`**
```json
{
  "notifications": [
    {
      "id": "ee223344-5566-7788-99aa-bbccddeeff00",
      "type": "bom_review",
      "channel": "in_app",
      "subject": "BOM ready for review — CLERMONT (BOG-SS27-001)",
      "body": "The cutting manager has confirmed the BOM for CLERMONT. Please review and approve.",
      "entity_type": "bom",
      "entity_id": "11223344-5566-7788-99aa-bbccddeeff00",
      "status": "sent",
      "scheduled_for": "2026-09-09T13:42:03.881204+00:00",
      "sent_at": "2026-09-09T11:42:04.102331+00:00",
      "opened_at": null
    },
    {
      "id": "ff334455-6677-8899-aabb-ccddeeff0011",
      "type": "po_awaiting_approval",
      "channel": "in_app",
      "subject": "PO draft awaiting cross-check",
      "body": "A supplier PO is awaiting your cross-check approval. http://localhost:3000/pos/…",
      "entity_type": "purchase_order",
      "entity_id": "99887766-5544-3322-1100-ffeeddccbbaa",
      "status": "pending",
      "scheduled_for": "2026-09-09T15:10:00.000000+00:00",
      "sent_at": null,
      "opened_at": null
    }
  ],
  "unread": 2
}
```

| Field | Meaning |
|---|---|
| `type` | `bom_review` · `po_awaiting_approval` · `po_dispatch` · `po_escalation_exhausted` |
| `entity_type` + `entity_id` | **Your deep link** |
| `scheduled_for` | When the email escalation fires if nobody opens it |
| `opened_at` | Non-null **cancels** the escalation |
| `unread` | Badge count |

---

### 10.2 `GET /procurement/notifications/stream`

**Server-Sent Events stream of the caller's notifications.**

Emits the unseen backlog on connect (marking each `sent`), then polls every 10 seconds, sending `: keep-alive` comment frames when idle.

**Roles:** any authenticated user · **Response:** `200 text/event-stream`

```
data: {"id":"ee223344-…","type":"bom_review","channel":"in_app","subject":"BOM ready for review — CLERMONT","body":"…","entity_type":"bom","entity_id":"11223344-…","status":"sent","scheduled_for":"2026-09-09T13:42:03+00:00","sent_at":"2026-09-09T11:42:04+00:00","opened_at":null}

: keep-alive

: keep-alive

data: {"id":"ff334455-…","type":"po_awaiting_approval",…}

```

```js
const es = new EventSource("/api/v1/procurement/notifications/stream",
                           { withCredentials: true });
es.onmessage = (e) => addNotification(JSON.parse(e.data));
es.onerror   = () => { es.close(); startPolling(); };
```

Frames are the same objects 10.1 returns. **The table is the source of truth** — a dropped connection loses nothing; poll 10.1 to resync.

---

### 10.3 `POST /procurement/notifications/{notification_id}/open`

**Mark a notification opened — this cancels the email escalation.**

**Roles:** any authenticated user (recipient only) · **Request:** no body

**Response `200`**
```json
{
  "id": "ee223344-5566-7788-99aa-bbccddeeff00",
  "type": "bom_review",
  "channel": "in_app",
  "subject": "BOM ready for review — CLERMONT (BOG-SS27-001)",
  "body": "The cutting manager has confirmed the BOM for CLERMONT. Please review and approve.",
  "entity_type": "bom",
  "entity_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "opened",
  "scheduled_for": "2026-09-09T13:42:03.881204+00:00",
  "sent_at": "2026-09-09T11:42:04.102331+00:00",
  "opened_at": "2026-09-09T12:01:55.400129+00:00"
}
```

**Errors:** `403` — not the recipient · `404`

> **Do not skip this call.** It is the only thing that stops the escalation email. Fire it when the user actually views the notification.

---

## 11. Patterns

---

### 11.1 `POST /procurement/patterns`

**Upload a DXF cutting pattern for background parsing.**

Stored immediately; parsing (pieces, areas, fabrics, sizes) runs in Celery.

**Roles:** any authenticated user

**Query:** `style_signature` (string, optional) — link the pattern to a style

**Request:** `multipart/form-data`, field `file` (a `.dxf`)

**Response `202`**
```json
{
  "job_id": "7a6b5c4d-3e2f-1009-8877-665544332211",
  "channel": "pattern:CLERMONT"
}
```

`channel` is a suggested subscription key if you wire realtime push. There is no polling endpoint for the job itself — the parsed pattern surfaces as a `pattern_reference_id` suggestion on the next breakdown, and improves DCM confidence on the next BOM generation.

---

## 12. Admin configuration

Six configuration sets, hot-editable. Every write also refreshes the in-memory `config_store` snapshot, so changes take effect **without a restart**.

**Roles for all of §12:** DM · MD

---

### 12.1 `PUT /procurement/admin/dxf-yields/{species}`

**Set the leather yield factor for a species.**

`species` ∈ `sheep` · `goat` · `calf` · `_default`

**Request**
```json
{ "factor": 1.18, "note": "Revised after Q2 2026 cutting-floor observations" }
```

**Response `200`**
```json
{ "species": "sheep", "factor": 1.18 }
```

---

### 12.2 `POST /procurement/admin/fabric-roles`

**Map a raw DXF fabric label to a BOM role.**

**Request**
```json
{
  "label": "PELLE PRINCIPALE",
  "role": "main",
  "category": "main_material",
  "is_leather": true
}
```

**Response `200`** — echoes the body:
```json
{
  "label": "PELLE PRINCIPALE",
  "role": "main",
  "category": "main_material",
  "is_leather": true
}
```

---

### 12.3 `GET /procurement/admin/cost-catalog`

**The default cost lines per garment code.**

**Response `200`**
```json
{
  "JACKET": [
    { "category": "manufacturing", "name": "CUTTING + STITCHING", "uom": null,   "unit_price": 30.00, "qty_per_garment": 1.0 },
    { "category": "packaging",     "name": "POLYBAG + CARTON",    "uom": null,   "unit_price": 3.00,  "qty_per_garment": 1.0 },
    { "category": "fob_charge",    "name": "FOB CHARGE",          "uom": null,   "unit_price": 5.00,  "qty_per_garment": 1.0 }
  ],
  "VEST": [
    { "category": "manufacturing", "name": "CUTTING + STITCHING", "uom": null,   "unit_price": 18.00, "qty_per_garment": 1.0 },
    { "category": "packaging",     "name": "POLYBAG",             "uom": null,   "unit_price": 1.50,  "qty_per_garment": 1.0 }
  ]
}
```

---

### 12.4 `PUT /procurement/admin/cost-catalog/{garment_code}`

**Replace the whole cost catalogue for a garment code.** `garment_code` is uppercased server-side.

**Request**
```json
{
  "lines": [
    { "category": "manufacturing", "name": "CUTTING + STITCHING", "uom": null, "unit_price": 32.00, "qty_per_garment": 1 },
    { "category": "packaging",     "name": "POLYBAG + CARTON",    "uom": null, "unit_price": 3.20,  "qty_per_garment": 1 },
    { "category": "fob_charge",    "name": "FOB CHARGE",          "uom": null, "unit_price": 5.00,  "qty_per_garment": 1 }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `category` | string | A `BomItemCategory` value |
| `name` | string | The BOM line label |
| `uom` | string? | |
| `unit_price` | number? | ≥ 0 |
| `qty_per_garment` | number | > 0, default `1` |

**Response `200`**
```json
{ "garment_code": "JACKET", "lines": 3 }
```

> **This is a full replace, not a merge.** Send every line you want to keep.

---

### 12.5 `GET /procurement/admin/checks`

**Per-client BOM sanity rules.**

**Response `200`**
```json
[
  {
    "client_code": "BOGGI",
    "rules": [
      { "id": "dcm-range-main", "kind": "range", "severity": "warn",  "field": "main_material.qty_per_garment", "range": [20.0, 55.0], "params": null },
      { "id": "fob-floor",      "kind": "range", "severity": "error", "field": "garment_fob_price",             "range": [40.0, 400.0], "params": null }
    ]
  }
]
```

---

### 12.6 `PUT /procurement/admin/checks/{client_code}`

**Replace the whole rule set for a client.**

**Request**
```json
{
  "rules": [
    { "id": "dcm-range-main", "kind": "range", "severity": "warn",  "field": "main_material.qty_per_garment", "range": [20.0, 55.0] },
    { "id": "fob-floor",      "kind": "range", "severity": "error", "field": "garment_fob_price",             "range": [40.0, 400.0] }
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `id` | string | Stable rule id |
| `kind` | string | Rule family, e.g. `range` |
| `severity` | string | `warn` (default) \| `error` |
| `field` | string? | Dotted path to the checked value |
| `range` | `[lo, hi]`? | For range rules |
| `params` | object? | Rule-specific extras |

**Response `200`**
```json
{ "client_code": "BOGGI", "rules": 2 }
```

---

### 12.7 `GET /procurement/admin/pom-dictionary`

**Every native-term → standard-POM mapping.**

**Response `200`**
```json
[
  { "source_term": "torace",  "pom_code": "CHEST",    "language": "it", "garment_type_id": null, "weight": 1 },
  { "source_term": "胸囲",     "pom_code": "CHEST",    "language": "ja", "garment_type_id": null, "weight": 1 },
  { "source_term": "spalle",  "pom_code": "SHOULDER", "language": "it", "garment_type_id": null, "weight": 1 },
  { "source_term": "袖ぐり",   "pom_code": "SHOULDER", "language": "ja", "garment_type_id": "aa11bb22-cc33-dd44-ee55-ff6677889900", "weight": 2 }
]
```

---

### 12.8 `POST /procurement/admin/pom-dictionary`

**Add or update one mapping.**

**Request**
```json
{
  "source_term": "lunghezza",
  "pom_code": "BODY_LENGTH",
  "language": "it",
  "garment_type_code": "JACKET",
  "weight": 1
}
```

| Field | Type | Notes |
|---|---|---|
| `source_term` | string | The native term as it appears on spec sheets |
| `pom_code` | string | The standard code |
| `language` | string? | **Inferred from the term if omitted** (CJK → `ja`) |
| `garment_type_code` | string? | Null applies to any garment type |
| `weight` | int | Default `1`. Higher wins on ambiguity |

**Response `200`**
```json
{
  "source_term": "lunghezza",
  "pom_code": "BODY_LENGTH",
  "language": "it",
  "garment_type_id": "aa11bb22-cc33-dd44-ee55-ff6677889900"
}
```

`garment_type_id` is `null` if `garment_type_code` was omitted or did not resolve.

---
---
# STAGE 4 — INVENTORY

*Router:* `app/modules/inventory/router.py` · *Service:* `InventoryService`

## 13. Stock master

---

### 13.1 `POST /procurement/inventory/preview`

**Dry-run a stock spreadsheet. Writes nothing.**

Always call this before 13.2 and show the result.

**Roles:** DM · MD · **Request:** `multipart/form-data`, field `file` (`.xlsx`)

**Response `200`**
```json
{
  "raw_count": 412,
  "kept": 389,
  "dropped": 23,
  "warnings": [
    "row 118: blank description — dropped",
    "row 204: qty could not be parsed ('N/A') — treated as 0",
    "12 rows merged into 5 keys by normalization"
  ],
  "rows": [
    {
      "normalized_key": "sheep glass black",
      "description": "SHEEP GLASS BLACK",
      "uom": "dm2",
      "qty_on_hand": 3400.0,
      "rate": 1.75,
      "color": "BLACK",
      "lots": 3
    },
    {
      "normalized_key": "goat suede black",
      "description": "GOAT SUEDE BLACK",
      "uom": "dm2",
      "qty_on_hand": 100.0,
      "rate": 2.05,
      "color": "BLACK",
      "lots": 1
    },
    {
      "normalized_key": "ykk zip 5 60cm black",
      "description": "YKK ZIP #5 60CM BLACK",
      "uom": "pcs",
      "qty_on_hand": 240.0,
      "rate": 1.35,
      "color": "BLACK",
      "lots": 2
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `raw_count` | Rows read from the sheet |
| `kept` / `dropped` | After normalisation and validation |
| `warnings[]` | Human-readable, row-numbered. **Render all of them** |
| `rows[].normalized_key` | The matching key. Multiple sheet rows can collapse into one |
| `rows[].lots` | How many sheet rows merged into this key |

---

### 13.2 `POST /procurement/inventory/commit`

**Apply the spreadsheet to the stock master.**

Idempotent upsert on `normalized_key`. **The sheet wins on quantity.** Keys absent from the sheet are **soft-deactivated**, never deleted.

**Roles:** DM · MD · **Request:** `multipart/form-data`, field `file`

**Response `200`** — the preview block plus two counters:
```json
{
  "raw_count": 412,
  "kept": 389,
  "dropped": 23,
  "warnings": ["row 118: blank description — dropped"],
  "rows": [ /* … same shape as preview … */ ],
  "committed": 389,
  "deactivated": 7
}
```

| Field | Meaning |
|---|---|
| `committed` | Rows inserted or updated |
| `deactivated` | Existing keys **absent from this sheet**, now `is_active: false` |

> **Warn about `deactivated` before the user commits.** A partial sheet will silently deactivate everything it omits. This is the single most destructive action in Stage 4.

---

### 13.3 `GET /procurement/inventory/items`

**The paged stock master.**

**Roles:** DM · MD · Viewer

**Query:** `search` (string, matches description/key) · `limit` (default 100) · `offset` (default 0)

**Response `200`**
```json
{
  "items": [
    {
      "id": "b0001111-2222-3333-4444-555566667777",
      "description": "SHEEP GLASS BLACK",
      "normalized_key": "sheep glass black",
      "uom": "dm2",
      "qty_on_hand": 3400.0,
      "rate": 1.75,
      "color": "BLACK",
      "is_active": true
    },
    {
      "id": "b0002222-3333-4444-5555-666677778888",
      "description": "GOAT SUEDE BLACK",
      "normalized_key": "goat suede black",
      "uom": "dm2",
      "qty_on_hand": 100.0,
      "rate": 2.05,
      "color": "BLACK",
      "is_active": true
    }
  ],
  "count": 2
}
```

> **`count` is the rows in this page**, not the grand total. Paginate until a short page arrives.
>
> **Note:** this endpoint returns `qty_on_hand` only. Reserved and available are computed **per BOM** inside a check — there is no global "available" figure.

---

## 14. Inventory checks

---

### 14.1 `POST /procurement/boms/{bom_id}/inventory-check`

**Run (or re-run) the stock check for an approved BOM.**

Releases this BOM's prior reservations, re-matches every stockable line, and re-reserves. One transaction, candidate rows locked.

**Roles:** DM · MD · **Request:** no body

**Response `200`**
```json
{
  "inventory_check_id": "cc001122-3344-5566-7788-99aabbccddee",
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "status": "complete",
  "run_at": "2026-09-09T12:15:44.220118+00:00",
  "summary": {
    "badge": "out_of_stock",
    "lines_total": 6,
    "sufficient": 3,
    "partial": 1,
    "out_of_stock": 2,
    "flags": { "unmatched": 1, "uom_mismatch": 0 },
    "shortfall_value": 1247.60,
    "currency": "INR"
  },
  "lines": [
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000001",
      "category": "main_material",
      "name": "SHEEP GLASS",
      "material_color": "BLACK",
      "required_qty": 2070.0,
      "uom": "dm2",
      "matched": {
        "inventory_item_id": "b0001111-2222-3333-4444-555566667777",
        "description": "SHEEP GLASS BLACK",
        "method": "key",
        "uom": "dm2"
      },
      "on_hand_qty": 3400.0,
      "available_qty": 2400.0,
      "reserved_for_this_bom": 2070.0,
      "shortfall_qty": 0.0,
      "status": "sufficient",
      "flags": [],
      "suggestion": null
    },
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000002",
      "category": "sub_material",
      "name": "GOAT SUEDE",
      "material_color": "BLACK",
      "required_qty": 156.0,
      "uom": "dm2",
      "matched": {
        "inventory_item_id": "b0002222-3333-4444-5555-666677778888",
        "description": "GOAT SUEDE BLACK",
        "method": "key",
        "uom": "dm2"
      },
      "on_hand_qty": 100.0,
      "available_qty": 100.0,
      "reserved_for_this_bom": 100.0,
      "shortfall_qty": 56.0,
      "status": "partial",
      "flags": [],
      "suggestion": null
    },
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000003",
      "category": "lining",
      "name": "VISCOSE LINING",
      "material_color": "BLACK",
      "required_qty": 72.0,
      "uom": "mtr",
      "matched": null,
      "on_hand_qty": 0.0,
      "available_qty": 0.0,
      "reserved_for_this_bom": 0.0,
      "shortfall_qty": 72.0,
      "status": "out_of_stock",
      "flags": ["suggestion", "unmatched"],
      "suggestion": {
        "inventory_item_id": "b0003333-4444-5555-6666-777788889999",
        "description": "VISCOSE LINING FABRIC BLACK",
        "score": 0.67
      }
    },
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000004",
      "category": "thread",
      "name": "POLY THREAD 40/2",
      "material_color": "BLACK",
      "required_qty": 7200.0,
      "uom": "mtr",
      "matched": {
        "inventory_item_id": "b0004444-5555-6666-7777-888899990000",
        "description": "POLYESTER THREAD 40/2 BLACK",
        "method": "alias",
        "uom": "mtr"
      },
      "on_hand_qty": 50000.0,
      "available_qty": 44000.0,
      "reserved_for_this_bom": 7200.0,
      "shortfall_qty": 0.0,
      "status": "sufficient",
      "flags": [],
      "suggestion": null
    },
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000005",
      "category": "accessory",
      "name": "YKK ZIP #5 60CM",
      "material_color": "BLACK",
      "required_qty": 60.0,
      "uom": "pcs",
      "matched": {
        "inventory_item_id": "b0005555-6666-7777-8888-99990000aaaa",
        "description": "YKK ZIP #5 60CM BLACK",
        "method": "key",
        "uom": "pcs"
      },
      "on_hand_qty": 240.0,
      "available_qty": 240.0,
      "reserved_for_this_bom": 60.0,
      "shortfall_qty": 0.0,
      "status": "sufficient",
      "flags": [],
      "suggestion": null
    },
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000007",
      "category": "packaging",
      "name": "POLYBAG + CARTON",
      "material_color": null,
      "required_qty": 60.0,
      "uom": null,
      "matched": null,
      "on_hand_qty": 0.0,
      "available_qty": 0.0,
      "reserved_for_this_bom": 0.0,
      "shortfall_qty": 60.0,
      "status": "out_of_stock",
      "flags": ["unmatched"],
      "suggestion": null
    }
  ],
  "excluded": [
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000006",
      "name": "CUTTING + STITCHING",
      "category": "manufacturing"
    },
    {
      "bom_item_id": "aa000001-0000-0000-0000-000000000008",
      "name": "FOB CHARGE",
      "category": "fob_charge"
    }
  ]
}
```

**Summary reference**

| Field | Meaning |
|---|---|
| `badge` | **The worst line status.** `out_of_stock` > `partial` > `sufficient` |
| `lines_total` | Stockable lines checked (excludes `excluded[]`) |
| `flags` | How many lines carry each flag |
| `shortfall_value` | Σ (shortfall × stock rate). **Always `INR`** — independent of the BOM's currency |

**Line reference**

| Field | Meaning |
|---|---|
| `required_qty` | The BOM line's `bulk_qty` — `order_qty × qty_per_garment` |
| `matched` | The primary matched stock row, or `null`. `method` ∈ `key` \| `alias` \| `manual` |
| `on_hand_qty` | Summed across all matched lots, unit-converted |
| `available_qty` | `on_hand` − reservations held by **other** BOMs |
| `reserved_for_this_bom` | What this check just claimed = `min(required, available)` |
| `shortfall_qty` | `required − reserved`. **This is exactly what Stage 5 orders** |
| `flags[]` | `unmatched` · `uom_mismatch` · `suggestion` |
| `suggestion` | A fuzzy candidate — **advisory only, never applied** |

**`excluded[]`** lists `manufacturing` and `fob_charge` lines. **Always render it**, greyed with an explanation, or the user will wonder why lines are missing.

**Errors**

| Code | `error` | Meaning |
|---|---|---|
| `409` | `bom_not_approved` | Only `approved` / `locked` / `exported` BOMs can be checked |
| `404` | | BOM not found |

---

### 14.2 `GET /procurement/inventory-checks/{check_id}`
### 14.3 `GET /procurement/boms/{bom_id}/inventory-check`

**Read a stored check.** 14.2 by check id; 14.3 fetches the latest for a BOM.

**Roles:** DM · MD · Viewer

**Response `200`** — the same envelope as 14.1, with one difference:

> **`available_qty` equals `on_hand_qty` on a stored read.** The live available figure is not persisted; `reserved_for_this_bom` is recovered as `required − shortfall`. Only a fresh run (14.1) returns the true live `available_qty`.

Show the `run_at` timestamp so the user knows how fresh the data is, and offer a "re-run" action.

**Errors:** `404` — no check found for this id/BOM.

---

### 14.4 `GET /procurement/inventory-checks`

**The grouped dashboard: client → order → style.**

**Roles:** DM · MD · Viewer · **Query:** `client_id` (UUID?) · `order_id` (UUID?)

**Response `200`**
```json
{
  "clients": [
    {
      "client_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
      "client_name": "BOGGI MILANO",
      "orders": [
        {
          "client_order_id": "3a4b5c6d-7e8f-9001-1223-3445566778899",
          "order_number": "BOG-SS27-001",
          "styles": [
            {
              "style_id": "4b5c6d7e-8f90-0112-2334-4556677889900",
              "style_name": "CLERMONT",
              "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
              "inventory_check_id": "cc001122-3344-5566-7788-99aabbccddee",
              "badge": "out_of_stock",
              "shortfall_lines": 3,
              "checked_at": "2026-09-09T12:15:44.220118+00:00"
            },
            {
              "style_id": "5c6d7e8f-9001-1223-3445-566778899001",
              "style_name": "CARNABY",
              "bom_id": "22334455-6677-8899-aabb-ccddeeff0011",
              "inventory_check_id": "dd112233-4455-6677-8899-aabbccddeeff",
              "badge": "sufficient",
              "shortfall_lines": 0,
              "checked_at": "2026-09-09T12:22:10.554902+00:00"
            }
          ]
        }
      ]
    }
  ],
  "totals": {
    "boms_checked": 2,
    "fully_sufficient": 1,
    "with_shortfall": 1
  }
}
```

A client whose id could not be resolved appears with `client_id: null` and `client_name: null` under the key `"unknown"`.

---
---

# STAGE 5 — SUPPLIER PO

*Router:* `app/modules/supplier_po/router.py` · *Services:* `PoService`, `SupplierService`, `ProductionTrackingService`

## 15. Suppliers

---

### 15.1 `POST /procurement/suppliers/import/preview`

**Dry-run a supplier spreadsheet.**

Parses both the contact sheet and the provision (purchase) ledger. Writes nothing.

**Roles:** DM · MD · **Request:** `multipart/form-data`, field `file`

**Response `200`**
```json
{
  "contact_rows": 84,
  "provision_rows": 1362,
  "suppliers": 79,
  "suppliers_with_phone": 71,
  "suppliers_with_email": 44,
  "history_rows": 612,
  "warnings": [
    "row 42: supplier name blank — skipped",
    "8 provision rows had no parseable date — first/last purchase left null"
  ],
  "sample_history": [
    {
      "supplier": "S.N. TRADERS",
      "article": "sheep glass black",
      "mode": "leather",
      "txn_count": 14,
      "last_rate": 1.72
    },
    {
      "supplier": "AL-AMEEN LEATHERS",
      "article": "goat suede",
      "mode": "leather",
      "txn_count": 9,
      "last_rate": 2.01
    },
    {
      "supplier": "ZIP WORLD",
      "article": "ykk zip 5 60cm",
      "mode": "accessory",
      "txn_count": 22,
      "last_rate": 1.30
    }
  ]
}
```

`sample_history` holds at most **25** rows — a preview, not the full set.

---

### 15.2 `POST /procurement/suppliers/import/commit`

**Apply the supplier spreadsheet.**

Three actions: upsert suppliers (**enrich, never blank** — an existing phone or email is not wiped by an omitting sheet), **rebuild the supply history wholesale**, and soft-deactivate suppliers absent from the sheet.

**Roles:** DM · MD · **Request:** `multipart/form-data`, field `file`

**Response `200`** — the preview block plus three counters:
```json
{
  "contact_rows": 84,
  "provision_rows": 1362,
  "suppliers": 79,
  "suppliers_with_phone": 71,
  "suppliers_with_email": 44,
  "history_rows": 612,
  "warnings": [],
  "sample_history": [ /* … */ ],
  "committed": 79,
  "deactivated": 3
}
```

> The supply history is **replaced entirely**, not merged. A partial sheet destroys matching evidence for everything it omits.

---

### 15.3 `GET /procurement/suppliers`

**The supplier directory.**

**Roles:** DM · MD · Viewer

**Query:** `q` (search) · `service` (filter) · `active` (bool) · `limit` (100) · `offset` (0)

**Response `200`**
```json
{
  "suppliers": [
    {
      "id": "e0001111-2222-3333-4444-555566667777",
      "name": "S.N. TRADERS",
      "phone": "+919840012345",
      "email": "sales@sntraders.in",
      "service": "Leather supply",
      "gstin": "33AABCS1429B1ZP",
      "address": "12 Anna Salai, Chennai 600002",
      "currency": "INR",
      "payment_terms_days": 60,
      "lead_time_days": 10,
      "is_active": true,
      "email_status": "valid",
      "supplier_type": "leather",
      "state_code": "33",
      "whatsapp_phone": "+919840012345",
      "has_contact": true
    },
    {
      "id": "e0002222-3333-4444-5555-666677778888",
      "name": "ZIP WORLD",
      "phone": "+912266778899",
      "email": null,
      "service": "Zips and trims",
      "gstin": "27AACZW1234K1Z5",
      "address": "Andheri East, Mumbai 400069",
      "currency": "INR",
      "payment_terms_days": 45,
      "lead_time_days": 7,
      "is_active": true,
      "email_status": "unknown",
      "supplier_type": "accessory",
      "state_code": "27",
      "whatsapp_phone": "+912266778899",
      "has_contact": true
    }
  ],
  "count": 2
}
```

| Field | Meaning |
|---|---|
| `supplier_type` | **Drives PO approval routing** — `leather` → Cutting Manager; else MD/DM/HR |
| `state_code` | First two GSTIN digits. **Drives intra vs inter-state GST** |
| `email_status` | `invalid` (bounced) means email is skipped entirely on send |
| `has_contact` | `email OR phone`. False → any PO for them is `no_contact_channel` |

---

### 15.4 `GET /procurement/suppliers/{supplier_id}`

**One supplier with their purchase history and open POs.**

**Roles:** DM · MD · Viewer

**Response `200`**
```json
{
  "id": "e0001111-2222-3333-4444-555566667777",
  "name": "S.N. TRADERS",
  "phone": "+919840012345",
  "email": "sales@sntraders.in",
  "service": "Leather supply",
  "gstin": "33AABCS1429B1ZP",
  "address": "12 Anna Salai, Chennai 600002",
  "currency": "INR",
  "payment_terms_days": 60,
  "lead_time_days": 10,
  "is_active": true,
  "email_status": "valid",
  "supplier_type": "leather",
  "state_code": "33",
  "whatsapp_phone": "+919840012345",
  "has_contact": true,
  "supply_history": [
    {
      "normalized_description": "sheep glass black",
      "raw_description": "SHEEP GLASS BLACK 0.6-0.8MM",
      "mode": "leather",
      "uom": "dm2",
      "txn_count": 14,
      "last_purchased_at": "2026-07-11",
      "last_rate": 1.72,
      "min_rate": 1.58,
      "max_rate": 1.89
    },
    {
      "normalized_description": "goat suede black",
      "raw_description": "GOAT SUEDE BLACK",
      "mode": "leather",
      "uom": "dm2",
      "txn_count": 6,
      "last_purchased_at": "2026-05-02",
      "last_rate": 2.05,
      "min_rate": 1.95,
      "max_rate": 2.20
    }
  ],
  "open_pos": [
    { "id": "99887766-5544-3322-1100-ffeeddccbbaa", "po_number": "PO-07(25-26)", "status": "sent", "total": 5842.40 }
  ]
}
```

`supply_history` is the evidence behind supplier ranking — render it on the supplier-selection panel.

**Errors:** `404` — `"Supplier not found."`

---

### 15.5 `POST /procurement/suppliers`

**Create a supplier.** The "none of these — add a new vendor" path from the PO supplier picker.

**Roles:** DM · MD

**Request**
```json
{
  "name": "NEW LEATHER CO",
  "phone": "+919000011111",
  "email": "sales@newleather.in",
  "service": "Leather supply",
  "gstin": "33AAACN1234A1Z9",
  "address": "45 Periamet, Chennai 600003",
  "currency": "INR",
  "payment_terms_days": 45,
  "lead_time_days": 12,
  "supplier_type": "leather",
  "whatsapp_phone": "+919000011111"
}
```

Only `name` is required. Defaults applied server-side: `currency` `"INR"`, `payment_terms_days` `60`, `lead_time_days` `10`. `state_code` is derived from the GSTIN.

**Response `201`** — the full supplier block (same shape as 15.3's rows).

**Errors:** `422 name_required` · `409 duplicate_name` (with `name`)

---

### 15.6 `PATCH /procurement/suppliers/{supplier_id}`

**Update a supplier.** Send only the fields you are changing — omitted fields are untouched.

**Roles:** DM · MD

**Request**
```json
{ "email": "purchase@sntraders.in", "supplier_type": "leather" }
```

Accepts the same fields as 15.5, all optional.

**Response `200`** — the updated supplier block.

**Errors:** `404`

---

### 15.7 `DELETE /procurement/suppliers/{supplier_id}`

**Soft-deactivate.** Never a hard delete — history is preserved.

**Roles:** **MD only**

**Response `200`**
```json
{ "id": "e0002222-3333-4444-5555-666677778888", "is_active": false }
```

**Errors:** `404`

---

### 15.8 `POST /procurement/suppliers/{supplier_id}/reactivate`

**Reverse a deactivation.**

**Roles:** **MD only** · **Request:** no body

**Response `200`**
```json
{ "id": "e0002222-3333-4444-5555-666677778888", "is_active": true }
```

**Errors:** `404`

---

## 16. Purchase orders

---

### 16.1 `POST /procurement/boms/{bom_id}/generate-pos`

**Turn the inventory shortfall into supplier POs.**

Matches each shortfall line to a supplier, groups by supplier into one PO each, and holds unmatched lines as `needs_supplier` drafts.

**Roles:** DM · MD · **Request:** no body

**Response `200`**
```json
{
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "resolved": 1,
  "needs_supplier": 1,
  "purchase_orders": [
    {
      "id": "99887766-5544-3322-1100-ffeeddccbbaa",
      "po_number": null,
      "status": "draft",
      "revision": 1,
      "supplier_id": "e0001111-2222-3333-4444-555566667777",
      "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
      "client_order_id": "3a4b5c6d-7e8f-9001-1223-3445566778899",
      "buyer_ref": "#BOG-SS27-001",
      "issue_date": null,
      "delivery_days": 10,
      "payment_terms_days": 60,
      "currency": "INR",
      "gst_mode": "INTRA",
      "subtotal": 114.80,
      "cgst": 6.89,
      "sgst": 6.89,
      "igst": 0.0,
      "round_off": 0.42,
      "total": 129.00,
      "needs_supplier": false,
      "no_contact_channel": false,
      "match_method": "ledger",
      "candidates": {
        "ranked": [
          {
            "supplier_id": "e0001111-2222-3333-4444-555566667777",
            "supplier_name": "S.N. TRADERS",
            "score": 0.91,
            "txn_count": 6,
            "has_contact": true,
            "last_rate": 2.05,
            "last_purchased_at": "2026-05-02"
          },
          {
            "supplier_id": "e0003333-4444-5555-6666-777788889999",
            "supplier_name": "AL-AMEEN LEATHERS",
            "score": 0.64,
            "txn_count": 3,
            "has_contact": true,
            "last_rate": 2.18,
            "last_purchased_at": "2025-11-20"
          }
        ]
      },
      "approved_at": null,
      "rejected_at": null,
      "rejection_reason": null,
      "sent_at": null,
      "pdf_document_id": null,
      "first_opened_at": null,
      "first_clicked_at": null,
      "current_rung": 0,
      "next_escalation_at": null,
      "acknowledged_at": null,
      "acknowledged_channel": null,
      "items": [
        {
          "id": "f0001111-2222-3333-4444-555566667777",
          "item_no": 1,
          "description": "GOAT SUEDE",
          "color": "BLACK",
          "uom": "dm2",
          "qty": 56.0,
          "unit_price": 2.05,
          "amount": 114.80,
          "inventory_item_id": "b0002222-3333-4444-5555-666677778888",
          "bom_item_id": "aa000001-0000-0000-0000-000000000002"
        }
      ],
      "supplier": {
        "id": "e0001111-2222-3333-4444-555566667777",
        "name": "S.N. TRADERS",
        "email": "sales@sntraders.in",
        "phone": "+919840012345",
        "gstin": "33AABCS1429B1ZP",
        "address": "12 Anna Salai, Chennai 600002",
        "supplier_type": "leather",
        "email_status": "valid"
      }
    },
    {
      "id": "88776655-4433-2211-00ff-eeddccbbaa99",
      "po_number": null,
      "status": "draft",
      "revision": 1,
      "supplier_id": null,
      "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
      "client_order_id": "3a4b5c6d-7e8f-9001-1223-3445566778899",
      "buyer_ref": "#BOG-SS27-001",
      "issue_date": null,
      "delivery_days": 10,
      "payment_terms_days": 60,
      "currency": "INR",
      "gst_mode": "INTER",
      "subtotal": 244.80,
      "cgst": 0.0,
      "sgst": 0.0,
      "igst": 29.38,
      "round_off": -0.18,
      "total": 274.00,
      "needs_supplier": true,
      "no_contact_channel": false,
      "match_method": null,
      "candidates": {
        "method": "fuzzy",
        "ranked": [
          {
            "supplier_id": "e0004444-5555-6666-7777-888899990000",
            "supplier_name": "TEXTILE HOUSE",
            "score": 0.41,
            "txn_count": 2,
            "has_contact": true,
            "last_rate": 3.30,
            "last_purchased_at": "2025-08-14"
          },
          {
            "supplier_id": "e0005555-6666-7777-8888-99990000aaaa",
            "supplier_name": "CHENNAI LININGS",
            "score": 0.38,
            "txn_count": 5,
            "has_contact": false,
            "last_rate": 3.10,
            "last_purchased_at": "2025-06-02"
          }
        ],
        "suggestion": "TEXTILE HOUSE",
        "ambiguous": true
      },
      "approved_at": null,
      "rejected_at": null,
      "rejection_reason": null,
      "sent_at": null,
      "pdf_document_id": null,
      "first_opened_at": null,
      "first_clicked_at": null,
      "current_rung": 0,
      "next_escalation_at": null,
      "acknowledged_at": null,
      "acknowledged_channel": null,
      "items": [
        {
          "id": "f0002222-3333-4444-5555-666677778888",
          "item_no": 1,
          "description": "VISCOSE LINING",
          "color": "BLACK",
          "uom": "mtr",
          "qty": 72.0,
          "unit_price": 3.40,
          "amount": 244.80,
          "inventory_item_id": null,
          "bom_item_id": "aa000001-0000-0000-0000-000000000003"
        }
      ]
    }
  ]
}
```

**Response `200` — already generated (idempotent)**
```json
{
  "bom_id": "11223344-…",
  "already_generated": true,
  "purchase_orders": [ /* the existing POs */ ]
}
```

**Response `200` — nothing to order**
```json
{
  "bom_id": "11223344-…",
  "purchase_orders": [],
  "message": "No shortfall lines — nothing to order."
}
```

**Errors**

| Code | `error` | Meaning |
|---|---|---|
| `409` | `no_inventory_check` | Run the check first |
| `404` | | BOM not found |

**Frontend notes**

- **`needs_supplier: true` is the blocker.** Those POs cannot be submitted until a supplier is assigned via 16.4.
- **`candidates.ambiguous: true`** means the system deliberately refused to choose. Show *"too close to call — please pick one"*.
- **`candidates.ranked[]`** is your comparison table: score, transaction count, last rate, last purchase date, contactable.
- A PO without a supplier has **no `supplier` key** in the response — guard for it.
- `match_method` is `ledger` \| `alias` \| `category` \| `null`. Show it as a provenance chip; `category` is a weaker signal than `ledger`.

---

### 16.2 `GET /procurement/pos`

**List purchase orders.**

**Roles:** DM · MD · Viewer

**Query:** `status` · `needs_supplier` (bool) · `bom_id` (UUID) · `supplier_id` (UUID)

**Response `200`**
```json
{
  "purchase_orders": [ /* full PO objects — same shape as 16.1 */ ],
  "count": 2
}
```

> **Build `?needs_supplier=true` as its own prominent tab.** Those are the POs blocking the pipeline.

---

### 16.3 `GET /procurement/pos/{po_id}`

**One purchase order in full.**

**Roles:** DM · MD · Viewer

**Response `200`** — the full PO object. A **sent** PO looks like this:
```json
{
  "id": "99887766-5544-3322-1100-ffeeddccbbaa",
  "po_number": "PO-07(25-26)",
  "status": "escalated",
  "revision": 3,
  "supplier_id": "e0001111-2222-3333-4444-555566667777",
  "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
  "client_order_id": "3a4b5c6d-7e8f-9001-1223-3445566778899",
  "buyer_ref": "#BOG-SS27-001",
  "issue_date": "2026-09-09",
  "delivery_days": 10,
  "payment_terms_days": 60,
  "currency": "INR",
  "gst_mode": "INTRA",
  "subtotal": 114.80,
  "cgst": 6.89,
  "sgst": 6.89,
  "igst": 0.0,
  "round_off": 0.42,
  "total": 129.00,
  "needs_supplier": false,
  "no_contact_channel": false,
  "match_method": "ledger",
  "candidates": { "ranked": [ /* … */ ] },
  "approved_at": "2026-09-09T13:02:11.004000+00:00",
  "rejected_at": null,
  "rejection_reason": null,
  "sent_at": "2026-09-09T13:15:40.220000+00:00",
  "pdf_document_id": "aa998877-6655-4433-2211-00ffeeddccbb",
  "first_opened_at": "2026-09-09T14:02:18.900000+00:00",
  "first_clicked_at": null,
  "current_rung": 1,
  "next_escalation_at": "2026-09-09T23:15:40.220000+00:00",
  "acknowledged_at": null,
  "acknowledged_channel": null,
  "items": [ /* … */ ],
  "supplier": { /* … */ }
}
```

**Money reference**

| Field | Meaning |
|---|---|
| `gst_mode` | `INTRA` → render **CGST + SGST**. `INTER` → render **IGST**. **Never both** |
| `round_off` | Can be negative |
| `total` | `subtotal + taxes + round_off` |

**Tracking reference**

| Field | Meaning |
|---|---|
| `sent_at` | Dispatch time |
| `first_opened_at` | The supplier's mail client fetched the pixel |
| `first_clicked_at` | They clicked a wrapped link |
| `current_rung` | `0` email · `1` WhatsApp sent · `2` call placed · `3` exhausted |
| `next_escalation_at` | Next rung fires here. **Null once acknowledged or exhausted** |
| `acknowledged_at` / `acknowledged_channel` | The ladder stopped, and how |
| `no_contact_channel` | No email and no phone — a human must handle it |

**Errors:** `404` — `"Purchase order not found."`

---

### 16.4 `PATCH /procurement/pos/{po_id}/items`

**Bulk-edit a PO under an optimistic revision lock.**

Same contract as the BOM editor, plus header edits, additions and removals.

**Roles:** DM · MD

**Request — assign a supplier to a `needs_supplier` PO**
```json
{
  "base_revision": 1,
  "po_edits": { "supplier_id": "e0004444-5555-6666-7777-888899990000" }
}
```

**Request — a mixed edit**
```json
{
  "base_revision": 2,
  "item_edits": [
    { "po_item_id": "f0001111-2222-3333-4444-555566667777", "field": "unit_price", "value": 1.98 },
    { "po_item_id": "f0001111-2222-3333-4444-555566667777", "field": "qty",        "value": 60 }
  ],
  "po_edits": { "delivery_days": 14, "buyer_ref": "#BOG-SS27-001-R2" },
  "add_items": [
    {
      "description": "GOAT SUEDE TRIM",
      "color": "BLACK",
      "uom": "dm2",
      "qty": 12,
      "unit_price": 2.05,
      "bom_item_id": null,
      "inventory_item_id": null
    }
  ],
  "remove_item_ids": ["f0009999-8888-7777-6666-555544443333"]
}
```

**Body reference**

| Field | Type | Notes |
|---|---|---|
| `base_revision` | int ≥ 1 | Required |
| `item_edits[]` | array | `{po_item_id, field, value}` |
| `item_edits[].field` | string | **Only** `description`, `color`, `uom`, `qty`, `unit_price` |
| `po_edits` | object? | **Only** `supplier_id`, `buyer_ref`, `delivery_days`, `payment_terms_days` |
| `add_items[]` | array | New lines |
| `remove_item_ids[]` | UUID array | Lines to drop |

All four change groups are optional; send only what you need.

**Response `200`** — the full recomputed PO object with an incremented `revision`.

> **Changing `supplier_id` re-routes the approver and resets any approval.** If the PO was `pending_approval` or `approved`, it returns to a state requiring fresh approval. Warn the user.

**Errors**

| Code | `error` | Extra |
|---|---|---|
| `409` | `po_locked` | A sent PO is frozen — cancel and re-issue |
| `409` | `stale_revision` | `current_revision` |
| `422` | `unknown_po_item` | `po_item_id` |
| `422` | `unsupported_field` | `field` |
| `422` | `invalid_value` | `field` |
| `404` | | PO not found |

---

### 16.5 `POST /procurement/pos/{po_id}/submit`

**Submit for cross-check approval.**

Notifies the routed approvers in-app.

**Roles:** DM · MD · **Request:** no body

**Response `200`**
```json
{
  "po_id": "99887766-5544-3322-1100-ffeeddccbbaa",
  "status": "pending_approval",
  "notifications_created": 2
}
```

**Errors**

| Code | `error` | Meaning |
|---|---|---|
| `409` | `not_submittable` | Only `draft` or `rejected` (with `current_status`) |
| `409` | `needs_supplier` | Assign a supplier first |
| `409` | `empty_po` | No lines |
| `404` | | PO not found |

---

### 16.6 `POST /procurement/pos/{po_id}/approve`

**Cross-check approval, routed by material type.**

**Roles:** Cutting Manager · MD · DM · HR *(the router admits all four; the service enforces the exact routing)*

**Routing:** `leather` → Cutting Manager or MD. `accessory` / `service` / unset → MD, DM or HR. **MD may approve anything.**

**Request:** no body

**Response `200`**
```json
{ "po_id": "99887766-5544-3322-1100-ffeeddccbbaa", "status": "approved" }
```

**Errors**

| Code | `error` | Meaning |
|---|---|---|
| `409` | `not_pending_approval` | Wrong state (with `current_status`) |
| `403` | `wrong_approver` | **The message names the roles that can** |
| `404` | | PO not found |

```json
{
  "detail": {
    "error": "wrong_approver",
    "message": "This PO routes to ['cutting_manager', 'managing_director']."
  }
}
```

**Frontend note.** Parse the role list out of `message` and show *"Ask a Cutting Manager or the MD to approve this."*

---

### 16.7 `POST /procurement/pos/{po_id}/reject`

**Reject with a mandatory reason.** Same role routing as 16.6.

**Request**
```json
{ "reason": "Rate is 18% above the last purchase from this vendor. Renegotiate." }
```

**Response `200`**
```json
{
  "po_id": "99887766-5544-3322-1100-ffeeddccbbaa",
  "status": "rejected",
  "rejection_reason": "Rate is 18% above the last purchase from this vendor. Renegotiate."
}
```

**Errors:** `422 reason_required` · `409 not_pending_approval` · `403 wrong_approver` · `404`

---

### 16.8 `POST /procurement/pos/{po_id}/send`

**Allocate the PO number, render the PDF, dispatch, and start the escalation clock.**

**Roles:** DM · MD · **Request:** no body

**Response `200` — emailed**
```json
{
  "po_id": "99887766-5544-3322-1100-ffeeddccbbaa",
  "po_number": "PO-07(25-26)",
  "status": "sent",
  "channel": "email",
  "no_contact_channel": false,
  "pdf_document_id": "aa998877-6655-4433-2211-00ffeeddccbb"
}
```

**Response `200` — no email; the sweeper will WhatsApp them**
```json
{
  "po_id": "88776655-4433-2211-00ff-eeddccbbaa99",
  "po_number": "PO-08(25-26)",
  "status": "sent",
  "channel": null,
  "no_contact_channel": false,
  "pdf_document_id": "bb887766-5544-3322-1100-ffeeddccbbaa"
}
```

**Response `200` — no contact at all**
```json
{
  "po_id": "77665544-3322-1100-ffee-ddccbbaa9988",
  "po_number": "PO-09(25-26)",
  "status": "sent",
  "channel": null,
  "no_contact_channel": true,
  "pdf_document_id": "cc776655-4433-2211-00ff-eeddccbbaa99"
}
```

| Field | Meaning |
|---|---|
| `po_number` | Allocated **at send**, financial-year scoped |
| `channel` | `"email"` if emailed; `null` otherwise |
| `no_contact_channel` | **`true` means nobody was contacted.** Show a red banner: this needs a manual call |
| `pdf_document_id` | The stored PO PDF |

**Errors:** `409 not_approved` (with `current_status`) · `404`

---

### 16.9 `POST /procurement/pos/{po_id}/cancel`

**Cancel a pre-send PO.**

**Roles:** DM · MD · **Request:** no body

**Response `200`**
```json
{ "po_id": "99887766-5544-3322-1100-ffeeddccbbaa", "status": "cancelled" }
```

**Errors:** `409 po_locked` — *"A sent PO cannot be cancelled in place."* · `404`

---

### 16.10 `POST /procurement/pos/{po_id}/acknowledge`

**Manually record a supplier's confirmation — and stop the escalation ladder.**

Use when the supplier confirms by a route the system cannot see: a phone call the buyer made, an email reply, a WhatsApp to a personal number.

**Roles:** DM · MD

**Request**
```json
{
  "channel": "call",
  "confirmed_qty": 56,
  "notes": "Confirmed by Rajesh on a call at 16:20. Delivery in 8 days."
}
```

| Field | Type | Notes |
|---|---|---|
| `channel` | string | Default `"manual"`. Typically `manual` · `call` · `whatsapp` · `email` |
| `confirmed_qty` | number? | What the supplier actually committed to |
| `notes` | string? | Free text |

**Response `200`** — the full PO object with `status: "confirmed"`, `acknowledged_at` set, `next_escalation_at: null`.

**Idempotent** — acknowledging an already-acknowledged PO returns it unchanged.

**Errors:** `404`

---

## 17. Tracking and webhooks

> **These five routes are unauthenticated and are not called by the frontend.** A supplier's mail client, Amazon SES and Twilio call them. Documented for completeness and for backend review.

---

### 17.1 `GET /procurement/t/o/{token}.gif`

**Email open pixel.** Returns a 1×1 GIF (`image/gif`) and stamps `first_opened_at`. Always `200`, even for an unknown token — a tracking pixel must never leak whether a token is real.

### 17.2 `GET /procurement/t/c/{token}`

**Click redirect.** Query: `u` (wrapped target), `s` (HMAC signature). Verifies the signature, records the click, then `302`s to the target. **`400`** on a bad or missing signature.

### 17.3 `POST /procurement/webhooks/ses`

**Amazon SES delivery / bounce / complaint events.**

```json
{
  "eventType": "Bounce",
  "tracking_token": "9f2a7c14e0b34d5f8a1b2c3d4e5f6071",
  "bounce": { "bounceType": "Permanent" }
}
```

Response `200 {"ok": true}`. **A hard bounce marks the supplier's email `invalid`, fails the response record, and sets `next_escalation_at` to now** — the ladder skips straight to WhatsApp.

### 17.4 `POST /procurement/webhooks/twilio/whatsapp`

```json
{ "tracking_token": "9f2a7c14e0b34d5f8a1b2c3d4e5f6071", "Body": "Confirmed, dispatching Monday" }
```

Response `200 {"ok": true}`. Acknowledges the PO on channel `whatsapp`.

### 17.5 `POST /procurement/webhooks/twilio/voice`

```json
{ "tracking_token": "9f2a7c14e0b34d5f8a1b2c3d4e5f6071", "Digits": "1" }
```

Response `200 {"ok": true}`. Acknowledges **only** when `Digits == "1"`.

---

## 18. The production board

---

### 18.1 `GET /procurement/production-tracking`

**The nine-rung board, one row per (order, style).**

**Roles:** DM · MD · Viewer · **Query:** `client_id` (UUID?) · `order_id` (UUID?)

**Response `200`**
```json
{
  "trackers": [
    {
      "id": "d0001111-2222-3333-4444-555566667777",
      "client_order_id": "3a4b5c6d-7e8f-9001-1223-3445566778899",
      "order_number": "BOG-SS27-001",
      "client_name": "BOGGI MILANO",
      "style_id": "4b5c6d7e-8f90-0112-2334-4556677889900",
      "style_name": "CLERMONT",
      "bom_id": "11223344-5566-7788-99aa-bbccddeeff00",
      "status": "po_raised",
      "po_count": 2,
      "po_confirmed_count": 0,
      "material_ready_at": null,
      "released_at": null
    },
    {
      "id": "d0002222-3333-4444-5555-666677778888",
      "client_order_id": "3a4b5c6d-7e8f-9001-1223-3445566778899",
      "order_number": "BOG-SS27-001",
      "client_name": "BOGGI MILANO",
      "style_id": "5c6d7e8f-9001-1223-3445-566778899001",
      "style_name": "CARNABY",
      "bom_id": "22334455-6677-8899-aabb-ccddeeff0011",
      "status": "material_ready",
      "po_count": 1,
      "po_confirmed_count": 1,
      "material_ready_at": "2026-09-09T16:40:02.118000+00:00",
      "released_at": null
    }
  ],
  "count": 2
}
```

| Field | Meaning |
|---|---|
| `status` | One of the nine rungs |
| `po_count` / `po_confirmed_count` | **A natural progress bar.** `material_ready` is reached when they are equal and non-zero |
| `material_ready_at` | Stamped when every PO confirmed |
| `released_at` | Stamped on the human go/no-go |

---

### 18.2 `POST /procurement/production-tracking/{tracking_id}/transition`

**Manually move a tracker.** The human go/no-go for `released_to_production`, and the correction path for a stuck row.

**Roles:** MD · DM · Cutting Manager

**Request**
```json
{ "status": "released_to_production" }
```

Valid values: `awaiting_bom` · `bom_approved` · `inventory_checked` · `po_raised` · `po_confirmed` · `material_ready` · `released_to_production` · `in_production` · `completed`

**Response `200`**
```json
{ "id": "d0001111-2222-3333-4444-555566667777", "status": "released_to_production" }
```

**Errors**

| Code | Body | Meaning |
|---|---|---|
| `403` | `"Only DM/MD/Cutting may transition the board."` | Wrong role |
| `422` | `{"error": "unknown_status", "status": "…"}` | Not a valid rung |
| `404` | `"Tracker not found."` | |

> **A manual transition may move backwards.** It is the correction path for when the system and reality disagree. Every transition is audited. Confirm backwards moves in the UI.

---
---

# INTELLIGENCE

*Router:* `app/modules/intelligence/router.py` · *Service:* `IntelligenceService`

## 19. Chat

---

### 19.1 `POST /chat`

**Ask a question about the factory. Get the exact answer.**

**Roles:** any authenticated user

**Request**
```json
{ "question": "Is CLERMONT on schedule?", "use_llm": false }
```

| Field | Type | Notes |
|---|---|---|
| `question` | string | Natural language |
| `use_llm` | bool | Default `false`. `true` uses the LangGraph agent **if `CHAT_MODEL` is configured**; otherwise it silently falls back to the deterministic router |

**Response `200`**
```json
{
  "answer": "CLERMONT is behind schedule. 60 ordered, 22 produced, 38 remaining with 9 working days to the deadline. Your recent rate is 2.1 pieces/day but you need 4.2 pieces/day. Consider adding capacity or extending the deadline.",
  "tool": "schedule_status",
  "data": {
    "style": "CLERMONT",
    "ordered": 60,
    "produced": 22,
    "remaining": 38,
    "deadline": "2026-09-22",
    "working_days_left": 9,
    "recent_daily_rate": 2.1,
    "required_daily_rate": 4.2,
    "on_track": false,
    "shortfall_days": 9
  }
}
```

**Bottleneck example**
```json
{
  "answer": "The bottleneck is PASTING. 41 pieces are queued there against a throughput of 12/day — a 3.4-day backlog. The next-worst stage, LINE_STITCHING, holds 14.",
  "tool": "bottleneck",
  "data": {
    "stage": "PASTING",
    "queued": 41,
    "throughput_per_day": 12,
    "backlog_days": 3.4,
    "runner_up": { "stage": "LINE_STITCHING", "queued": 14 }
  }
}
```

**Overview example**
```json
{
  "answer": "4 active orders across 3 clients. 7 styles in production, 2 at risk. 1,840 pieces ordered, 612 completed.",
  "tool": "overview",
  "data": {
    "active_orders": 4,
    "clients": 3,
    "styles_in_production": 7,
    "styles_at_risk": 2,
    "total_ordered": 1840,
    "total_produced": 612
  }
}
```

**No match**
```json
{
  "answer": "I could not match that to a style or a known question. Try naming a style, or ask about the bottleneck or the factory overview.",
  "tool": null,
  "data": null
}
```

| Field | Meaning |
|---|---|
| `answer` | Prose, ready to display |
| `tool` | Which tool answered: `schedule_status` · `bottleneck` · `plan` · `overview` · `search_workflow_docs` · `null` |
| `data` | **The exact numbers.** Render as a structured card beneath the prose — this is the trustworthy part |

---

### 19.2 `POST /chat/stream`

**The same answer, streamed word-by-word for a typing effect.**

**Roles:** any authenticated user · **Request:** identical to 19.1 · **Response:** `200 text/event-stream`

```
data: {"delta":"CLERMONT "}

data: {"delta":"is "}

data: {"delta":"behind "}

data: {"delta":"schedule. "}

data: {"done":true,"tool":"schedule_status","data":{"style":"CLERMONT","ordered":60,"produced":22,"remaining":38,"deadline":"2026-09-22","working_days_left":9,"recent_daily_rate":2.1,"required_daily_rate":4.2,"on_track":false}}

```

```js
async function askStreaming(question, onDelta, onDone) {
  const res = await fetch("/api/v1/chat/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ question, use_llm: false }),
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop();
    for (const frame of frames) {
      if (!frame.startsWith("data: ")) continue;      // skip keep-alive comments
      const payload = JSON.parse(frame.slice(6));
      if (payload.done) onDone(payload); else onDelta(payload.delta);
    }
  }
}
```

Note: `EventSource` cannot POST, so use `fetch` + a stream reader here. The notification stream (10.2) is a GET and works with `EventSource`.

---
---

# APPENDICES

## 29. The mock data pack

Every ID below is used consistently across every example in this document. Drop these into your mock server and **one order flows end to end**.

### 29.1 The scenario

> **BOGGI MILANO** orders 100 leather garments for SS27 across two styles.
> **CLERMONT** (60 units) is short on goat suede and lining. **CARNABY** (40 units) is fully in stock.
> CLERMONT generates two POs — one resolved to S.N. TRADERS, one held for a supplier decision.

### 29.2 Identity constants

```js
export const IDS = {
  // people
  user_md:            "9e1c4a70-0b2d-4c8e-9f11-6a2b3c4d5e6f",
  user_dm:            "8d0b3960-1a2c-3b4d-5e6f-708192a3b4c5",
  user_cutting:       "7c9a2850-0912-2a3b-4c5d-6e7f8091a2b3",

  // client & order
  client:             "7c9e6679-7425-40de-944b-e07fc1f90ae7",  // BOGGI MILANO
  client_order:       "3a4b5c6d-7e8f-9001-1223-3445566778899",  // BOG-SS27-001

  // stage 1
  submission:         "a3f2b8c1-4d5e-6f70-8192-a3b4c5d6e7f8",
  doc_order_sheet:    "b1c2d3e4-5f60-7182-93a4-b5c6d7e8f901",
  doc_spec_sheet:     "e5f60718-293a-4b5c-6d7e-8f90a1b2c3d4",

  // stage 2 — styles
  order_style_clermont: "d4e5f607-1829-3a4b-5c6d-7e8f90a1b2c3",
  order_style_carnaby:  "07182930-4b5c-6d7e-8f90-a1b2c3d4e5f6",
  pattern_clermont:     "f6071829-3a4b-5c6d-7e8f-90a1b2c3d4e5",
  pattern_carnaby:      "1829304b-5c6d-7e8f-90a1-b2c3d4e5f607",
  style_clermont:       "4b5c6d7e-8f90-0112-2334-4556677889900",
  style_carnaby:        "5c6d7e8f-9001-1223-3445-566778899001",

  // stage 2/3 — BOMs
  bom_clermont:       "11223344-5566-7788-99aa-bbccddeeff00",
  bom_carnaby:        "22334455-6677-8899-aabb-ccddeeff0011",
  bom_export_doc:     "dd112233-4455-6677-8899-aabbccddeeff",

  // BOM items (CLERMONT)
  item_sheep_glass:   "aa000001-0000-0000-0000-000000000001",
  item_goat_suede:    "aa000001-0000-0000-0000-000000000002",
  item_lining:        "aa000001-0000-0000-0000-000000000003",
  item_thread:        "aa000001-0000-0000-0000-000000000004",
  item_zip:           "aa000001-0000-0000-0000-000000000005",
  item_manufacturing: "aa000001-0000-0000-0000-000000000006",
  item_packaging:     "aa000001-0000-0000-0000-000000000007",
  item_fob:           "aa000001-0000-0000-0000-000000000008",

  // stage 4
  inv_sheep_glass:    "b0001111-2222-3333-4444-555566667777",
  inv_goat_suede:     "b0002222-3333-4444-5555-666677778888",
  inv_lining:         "b0003333-4444-5555-6666-777788889999",
  inv_thread:         "b0004444-5555-6666-7777-888899990000",
  inv_zip:            "b0005555-6666-7777-8888-99990000aaaa",
  check_clermont:     "cc001122-3344-5566-7788-99aabbccddee",
  check_carnaby:      "dd112233-4455-6677-8899-aabbccddeeff",

  // stage 5 — suppliers
  sup_sn_traders:     "e0001111-2222-3333-4444-555566667777",  // leather, TN (33)
  sup_zip_world:      "e0002222-3333-4444-5555-666677778888",  // accessory, MH (27)
  sup_al_ameen:       "e0003333-4444-5555-6666-777788889999",  // leather
  sup_textile_house:  "e0004444-5555-6666-7777-888899990000",  // accessory
  sup_chennai_lining: "e0005555-6666-7777-8888-99990000aaaa",  // no contact

  // stage 5 — POs
  po_resolved:        "99887766-5544-3322-1100-ffeeddccbbaa",  // S.N. TRADERS
  po_needs_supplier:  "88776655-4433-2211-00ff-eeddccbbaa99",  // held
  po_item_suede:      "f0001111-2222-3333-4444-555566667777",
  po_item_lining:     "f0002222-3333-4444-5555-666677778888",
  po_pdf_doc:         "aa998877-6655-4433-2211-00ffeeddccbb",
  tracking_token:     "9f2a7c14e0b34d5f8a1b2c3d4e5f6071",

  // notifications
  notif_bom_review:   "ee223344-5566-7788-99aa-bbccddeeff00",
  notif_po_approval:  "ff334455-6677-8899-aabb-ccddeeff0011",

  // board
  track_clermont:     "d0001111-2222-3333-4444-555566667777",
  track_carnaby:      "d0002222-3333-4444-5555-666677778888",
};
```

### 29.3 The consistent numbers

**CLERMONT BOM** — order_qty 60, FOB $107.25, bulk total $6,435.00

| Line | Category | DCM | UOM | Rate | Bulk qty | Line cost | DCM source |
|---|---|---:|---|---:|---:|---:|---|
| SHEEP GLASS | main_material | 34.500 | dm2 | 1.80 | 2070.000 | 62.10 | template (0.95) |
| GOAT SUEDE | sub_material | 2.600 | dm2 | 2.10 | 156.000 | 5.46 | **ai_estimate (0.50)** |
| VISCOSE LINING | lining | 1.200 | mtr | 3.40 | 72.000 | 4.08 | similar_style (0.70) |
| POLY THREAD 40/2 | thread | 120.000 | mtr | 0.01 | 7200.000 | 1.20 | — |
| YKK ZIP #5 60CM | accessory | 1.000 | pcs | 1.41 | 60.000 | 1.41 | — |
| CUTTING + STITCHING | manufacturing | 1.000 | — | 30.00 | 60.000 | 30.00 | — |
| POLYBAG + CARTON | packaging | 1.000 | — | 3.00 | 60.000 | 3.00 | — |
| FOB CHARGE | fob_charge | 1.000 | — | 5.00 | 60.000 | 5.00 | — |

**CLERMONT inventory check** — badge `out_of_stock`, 6 lines, 3 sufficient · 1 partial · 2 out of stock

| Line | Required | On hand | Available | Reserved | Shortfall | Status | Flags |
|---|---:|---:|---:|---:|---:|---|---|
| SHEEP GLASS | 2070.0 | 3400.0 | 2400.0 | 2070.0 | 0.0 | sufficient | — |
| GOAT SUEDE | 156.0 | 100.0 | 100.0 | 100.0 | **56.0** | partial | — |
| VISCOSE LINING | 72.0 | 0.0 | 0.0 | 0.0 | **72.0** | out_of_stock | suggestion, unmatched |
| POLY THREAD | 7200.0 | 50000.0 | 44000.0 | 7200.0 | 0.0 | sufficient | — |
| YKK ZIP #5 | 60.0 | 240.0 | 240.0 | 60.0 | 0.0 | sufficient | — |
| POLYBAG + CARTON | 60.0 | 0.0 | 0.0 | 0.0 | **60.0** | out_of_stock | unmatched |

*Excluded: CUTTING + STITCHING (manufacturing), FOB CHARGE (fob_charge).*

**The two POs**

| PO | Supplier | Lines | GST mode | Subtotal | Tax | Total | Flag |
|---|---|---|---|---:|---:|---:|---|
| `po_resolved` | S.N. TRADERS (TN, 33) | GOAT SUEDE 56 × 2.05 | **INTRA** | 114.80 | CGST 6.89 + SGST 6.89 | 129.00 | — |
| `po_needs_supplier` | *(none)* | VISCOSE LINING 72 × 3.40 | INTER | 244.80 | IGST 29.38 | 274.00 | **`needs_supplier`, `ambiguous`** |

Buyer state code is `33` (Tamil Nadu), GST rate 12%.

### 29.4 A minimal MSW handler set

```js
import { http, HttpResponse } from "msw";
import { IDS } from "./ids";
const V1 = "/api/v1";

export const handlers = [
  http.post(`${V1}/auth/login`, () =>
    HttpResponse.json({ access_token: "mock.jwt.token", token_type: "bearer" })),

  http.get(`${V1}/auth/me`, () =>
    HttpResponse.json({ id: IDS.user_md, name: "Tanveer Ahmed",
      phone: "9876543210", email: "tanveer@ptexports.com",
      role: "managing_director", is_active: true })),

  http.post(`${V1}/procurement/submissions`, () =>
    HttpResponse.json({ submission_id: IDS.submission, status: "open" },
                      { status: 201 })),

  http.get(`${V1}/procurement/submissions/:id`, () =>
    HttpResponse.json({
      order_sheet: { present: true, validation_status: "accepted" },
      spec_sheet:  { present: true, validation_status: "accepted" },
      complete: true, ready_for_stage_2: true, blocking: [] })),

  http.get(`${V1}/procurement/submissions/:id/order-breakdown`, () =>
    HttpResponse.json(BREAKDOWN_READY)),          // §8.2

  http.get(`${V1}/procurement/boms/:id`, () =>
    HttpResponse.json(BOM_CLERMONT)),             // §9.1

  http.patch(`${V1}/procurement/boms/:id/items`, async ({ request }) => {
    const body = await request.json();
    if (body.base_revision !== 1) {
      return HttpResponse.json(
        { detail: { error: "stale_revision", current_revision: 2 } },
        { status: 409 });
    }
    return HttpResponse.json({ revision: 2, reconfirm_required: false,
                               recomputed: BOM_CLERMONT_V2 });
  }),

  http.post(`${V1}/procurement/boms/:id/approve`, () =>
    HttpResponse.json({ bom_id: IDS.bom_clermont, status: "approved",
                        inventory_check_id: IDS.check_clermont })),

  http.get(`${V1}/procurement/boms/:id/inventory-check`, () =>
    HttpResponse.json(CHECK_CLERMONT)),           // §14.1

  http.post(`${V1}/procurement/boms/:id/generate-pos`, () =>
    HttpResponse.json(GENERATED_POS)),            // §16.1

  http.get(`${V1}/procurement/pos`, () =>
    HttpResponse.json({ purchase_orders: [PO_RESOLVED, PO_NEEDS_SUPPLIER],
                        count: 2 })),

  http.get(`${V1}/procurement/production-tracking`, () =>
    HttpResponse.json(BOARD)),                    // §18.1
];
```

### 29.5 Mocking the async flows

**Breakdown** — flip status after a few polls so you can build the real spinner:

```js
let polls = 0;
http.get(`${V1}/procurement/submissions/:id/order-breakdown`, () => {
  polls += 1;
  if (polls < 4) {
    return HttpResponse.json({ submission_id: IDS.submission,
                               status: "processing", styles: [], warnings: [] });
  }
  return HttpResponse.json(BREAKDOWN_READY);
});
```

**Notifications SSE:**

```js
http.get(`${V1}/procurement/notifications/stream`, () => {
  const stream = new ReadableStream({
    start(controller) {
      const enc = new TextEncoder();
      controller.enqueue(enc.encode(`data: ${JSON.stringify(NOTIF_BOM_REVIEW)}\n\n`));
      const t = setInterval(() => controller.enqueue(enc.encode(": keep-alive\n\n")), 10000);
      setTimeout(() => { clearInterval(t); controller.close(); }, 60000);
    },
  });
  return new HttpResponse(stream, { headers: { "Content-Type": "text/event-stream" } });
});
```

**Every error state worth building for:**

```js
export const ERRORS = {
  staleRevision:      [409, { detail: { error: "stale_revision", current_revision: 3 } }],
  bomLocked:          [409, { detail: { error: "bom_locked",
                                        message: "A locked/approved BOM rejects all edits." } }],
  cuttingRequired:    [409, { detail: { error: "cutting_confirmation_required",
                                        message: "The cutting manager must confirm the BOM before approval." } }],
  specUnconfirmed:    [422, { detail: "spec suggestion is unconfirmed — confirm or clear it before generating" }],
  noInventoryCheck:   [409, { detail: { error: "no_inventory_check",
                                        message: "Run the inventory check before generating supplier POs." } }],
  needsSupplier:      [409, { detail: { error: "needs_supplier",
                                        message: "Assign a supplier before submitting." } }],
  wrongApprover:      [403, { detail: { error: "wrong_approver",
                                        message: "This PO routes to ['cutting_manager', 'managing_director']." } }],
  poLocked:           [409, { detail: { error: "po_locked",
                                        message: "A sent PO is frozen — cancel + re-issue." } }],
  docRejected:        [422, { error: "document_validation_failed",
                              reason_code: "not_an_order_sheet", /* … §4.1 … */ }],
  serverError:        [500, { detail: "Internal server error",
                              request_id: "3f9a2b71-8c4d-4e5f-a012-b3c4d5e6f708" }],
};
```

---

## 30. Frontend call sequences

### 30.1 Intake to breakdown

```
POST   /procurement/submissions                                  → submission_id
POST   /procurement/submissions/{id}/order-sheet   (multipart)   → 201 | 4xx diagnostics
POST   /procurement/submissions/{id}/spec-sheet    (multipart)   → 201 | 4xx diagnostics
GET    /procurement/submissions/{id}                             → ready_for_stage_2?
   └── if false: show `blocking`, let the user re-upload
POST   /procurement/submissions/{id}/order-breakdown             → 202 queued
GET    /procurement/submissions/{id}/order-breakdown   ⟳ 3–5s    → until status="ready"
```

### 30.2 Style to BOM

```
   per style shown in the breakdown:
POST   /procurement/order-styles/{id}/attachments                → confirm spec + DXF
POST   /procurement/order-styles/{id}/generate-bom               → 202 queued
GET    /procurement/submissions/{sid}/order-breakdown  ⟳ 3–5s    → until this style's bom_id ≠ null
GET    /procurement/boms/{bom_id}                                → render the editor
```

### 30.3 Edit and confirm

```
GET    /procurement/boms/{id}                                    → note `revision`
PATCH  /procurement/boms/{id}/items   {base_revision, edits[]}    → new revision + recomputed tree
   └── 409 stale_revision → GET again, re-present, retry
POST   /procurement/boms/{id}/confirm-cutting                    → ready_for_review
```

### 30.4 Approval to inventory

```
GET    /procurement/notifications                                → the MD sees the review notice
POST   /procurement/notifications/{id}/open                      → cancels the email escalation
GET    /procurement/boms/{id}                                    → review
POST   /procurement/boms/{id}/approve   {lock:false}             → approved + inventory_check_id
GET    /procurement/inventory-checks/{inventory_check_id}        → the check result
   └── if inventory_check_id is null:
       POST /procurement/boms/{id}/inventory-check
POST   /procurement/boms/{id}/export                             → the client PDF
```

### 30.5 Shortfall to sent PO

```
POST   /procurement/boms/{id}/generate-pos                       → resolved + needs_supplier counts
GET    /procurement/pos?bom_id={id}                              → the list
   for each needs_supplier PO:
GET    /procurement/suppliers?q=…                                → the picker
PATCH  /procurement/pos/{po}/items  {base_revision, po_edits:{supplier_id}}
   for each PO:
POST   /procurement/pos/{po}/submit                              → pending_approval
POST   /procurement/pos/{po}/approve                             → approved   (routed role)
POST   /procurement/pos/{po}/send                                → sent + po_number + PDF
GET    /procurement/pos/{po}                          ⟳ 30–60s   → watch the tracking timeline
POST   /procurement/pos/{po}/acknowledge  {channel:"call"}       → confirmed (manual path)
```

### 30.6 Release to production

```
GET    /procurement/production-tracking?order_id={id}            → the board
   when a style reads material_ready:
POST   /procurement/production-tracking/{tracking_id}/transition
       {status:"released_to_production"}                          → Phase 1 takes over
```

---

## 31. Quick reference card

### The five 409s that gate everything

| Gate | Blocks | Unblock with |
|---|---|---|
| `spec_suggestion_unconfirmed` | Generate BOM | `POST /order-styles/{id}/attachments` |
| `cutting_confirmation_required` | Approve BOM | `POST /boms/{id}/confirm-cutting` |
| `bom_not_approved` | Inventory check | `POST /boms/{id}/approve` |
| `no_inventory_check` | Generate POs | `POST /boms/{id}/inventory-check` |
| `needs_supplier` | Submit PO | `PATCH /pos/{id}/items` with `po_edits.supplier_id` |

### The four numbers that flow through everything

```
  BOM        bom_item.bulk_qty       = order_qty × qty_per_garment
                     │
  CHECK      required_qty            = bom_item.bulk_qty
             shortfall_qty           = required − min(required, available)
                     │
  PO         po_item.qty             = shortfall_qty
             po_item.amount          = qty × unit_price
```

### The two lock messages

- **`stale_revision`** → GET, re-present, retry. Never force.
- **`po_locked` / `bom_locked`** → the record is frozen. Cancel and re-issue.

### The three "the system is asking you" moments

| Signal | Where | Meaning |
|---|---|---|
| `spec_match_status: "suggested"` | Breakdown | *"I found a spec. Is it the right one?"* |
| `flags: ["suggestion"]` | Inventory check | *"This might be the stock item. Confirm?"* |
| `candidates.ambiguous: true` | PO generation | *"Two suppliers are too close to call. You pick."* |

**Each one is a deliberate refusal to guess.** Give each a first-class UI affordance — they are where the system's accuracy actually comes from.

---

*KairoX ERP — Phase 2 API Reference. Companion document: **KairoX Phase-2 System & Business Guide**.*
