# 1. What this service is

**Phase 2, the two buying ends of the pipeline: Stage 1 (intake) and Stage 5 (purchase orders).**

Phase 1 tracks garments on the floor and assumes the material is already there. Phase 2 answers the question that comes before that:

> *"A client just sent us an order. What material do we need, do we have it, and if not, who do we buy it from — and have they confirmed?"*

Today that is answered by people, with spreadsheets, phone calls and memory. This is the system that replaces them, and this document covers **the first step and the last**:

```
 ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
 │ STAGE 1  │  │ STAGE 2  │  │ STAGE 3  │  │ STAGE 4  │  │ STAGE 5  │
 │ INTAKE   │─▶│   BOM    │─▶│ APPROVAL │─▶│INVENTORY │─▶│SUPPLIER  │
 │ ★ here   │  │          │  │          │  │  CHECK   │  │ PO ★here │
 └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
  order sheet   bill of       the MD signs   what do we    buy the
  + spec sheet  materials     it off         already have  shortfall
  come in       is generated                 in stock?     and chase it
```

**The stages only work in order.** You cannot check inventory against a BOM you have not generated, and you cannot buy a shortfall you have not measured.

| Module | Stage | Document |
|---|---|---|
| `app/modules/procurement/` | **1 — intake** | **this one** |
| `app/modules/bom/` | 2 and 3 — generation and approval | `BOM_SYSTEM_GUIDE.docx` |
| `app/modules/inventory/` | 4 — the stock check | `INVENTORY_SYSTEM_GUIDE.docx` |
| `app/modules/supplier_po/` | **5 — purchase orders** | **this one** |

All four are mounted under `/api/v1/procurement/…` so the URLs read as one workflow.

> **A longer, three-audience narrative exists** in `docs/KAIROX_PROCUREMENT_SYSTEM_GUIDE.md` (and its PDF), plus `docs/KAIROX_PROCUREMENT_API_REFERENCE.md`. Those cover all five stages including the algorithms.

---

# 2. Stage 1 — Intake

## The flow

```
POST /procurement/submissions                    open a folder
POST /procurement/submissions/{id}/order-sheet   upload the order sheet
POST /procurement/submissions/{id}/spec-sheet    upload the spec sheet
GET  /procurement/submissions/{id}               is it ready for Stage 2?
```

There is also a **one-shot door** — `POST /procurement/upload/order-sheet` and `/upload/spec-sheet` — which opens the submission and uploads in a single call. The submission is created **only if the file passes validation**; a rejected file returns 4xx diagnostics and no submission is visible to the caller. Take the `submission_id` out of the response and use it for the paired upload.

> **Open the submission with a `client_id` if you can.** `POST /submissions` accepts `{"client_id": …}`, and a submission without one is refused later by the Stage-2 order breakdown with a 422. Setting it up front is one field; fixing it later is a re-open.

## The gate

`ready_for_stage_2` is `true` only when **both slots are `accepted`** and **neither has a blocking virus-scan status**. `blocking[]` says in words what is in the way, ready to display:

- `"spec_sheet missing"`
- `"order_sheet rejected"`
- `"spec_sheet scan_status=infected"`

`complete` says both slots are accepted; `ready_for_stage_2` additionally requires the scans to be clean. A scan status of `clean` **or `skipped`** passes — `skipped` is what you get with scanning disabled in development.

## The upload is classified, not trusted

A file uploaded into the order-sheet slot is checked to see whether it **is** an order sheet. The verdict comes back in full, on the `document.validation` block:

| Field | Meaning |
|---|---|
| `status` | `accepted` / `rejected` / `needs_manual_review` / `pending` / `superseded` |
| `classified_as` | what it actually looks like |
| `spec_type`, `client_match` | the sub-kind and which client's layout it resembles |
| `confidence` | 0–1 |
| `method` | `heuristic`, `llm` or `manual` |
| `llm_label`, `llm_model` | what the model said, and which model — the audit trail |
| `signals_expected` | what this kind of document must contain |
| `signals_found` | what the file actually contained |
| `signals_matched` | the overlap |
| `reason_code` | why it was rejected, as a stable code |
| `suggested_fix` | **why it was rejected, in words — show this** |

A rejection is a **4xx with the same diagnostics**, so the screen can tell the user exactly what was missing rather than "upload failed".

## Two gates, and only the ambiguous minority costs money

Classification is a **hybrid**. A cheap heuristic settles structured spreadsheets for free. Only what it cannot settle — scanned or handwritten PDFs with no text layer, unknown layouts, mid-band scores — escalates to the LLM, which runs a provider chain: the primary extraction model, then the fallback, then **`needs_manual_review`**.

> **Nothing is ever silently guessed.** A model answer below the accept threshold returns `needs_manual_review`, not a fabricated classification. And when no provider key is configured at all, escalation degrades to `needs_manual_review` rather than crashing the front door — a model outage never stalls the easy cases, because the heuristic already settled them.

## `?force=true` — the DM overrides a manual review

Both upload endpoints take `force` as a query parameter. `force=true` accepts a `needs_manual_review` document anyway and lets the pipeline proceed — the DM or MD vouches for it.

**Hard rejections are never overridable.** A virus, an unsupported MIME type, an oversized or corrupt file, or a file in the wrong slot is refused with or without `force`.

## The other guards

| Guard | Status | `reason_code` |
|---|---|---|
| unsupported file type | **415** | `unsupported_mime` |
| file too large (`MAX_UPLOAD_MB`, default 25) | **413** | `file_too_large` |
| empty or corrupt | **422** | `empty_or_corrupt` |
| virus detected | **422** | `virus_detected` |
| virus scanner unavailable | **503** | `scanner_unavailable` |
| not an order sheet / not a spec sheet | **422** | `not_an_order_sheet` / `not_a_spec_sheet` |
| wrong slot (a spec sheet posted as an order sheet) | **422** | `wrong_slot` |
| needs a human to look | **422** | `needs_manual_review` |
| the submission is already locked | **409** | `submission_locked` |
| byte-identical file already on record | **409** | `duplicate_content` |

> **Re-uploading the same bytes replays the cached verdict** rather than re-classifying. The `sha256` on the document is what makes that possible. Note the limit: the cache is a true short-circuit only for **the same slot of the same submission**. `Document.sha256` is globally unique, so the same bytes cannot be stored again under another submission — that is the 409.

## The pipeline is quarantine-then-promote

The order of operations matters and is deliberate:

1. **scan the raw bytes.** Infected → 422. Scanner unreachable while required → **503, fail-closed**. Clean or skipped → the bytes go to `quarantine/`.
2. **sniff the true MIME** and extract features — the declared content type is not trusted.
3. **validate identity** (heuristic → LLM).
4. **accepted → promote** `quarantine/` to `submissions/` and return a `storage_url`. **Rejected or needs-review → the quarantined object is deleted** and `storage_url` is `null`.

So an accepted document is always one that was scanned and validated, and no rejected file ever leaves quarantine.

## Reading one document's verdict again

`GET /procurement/submissions/{id}` answers *"is this folder ready"* — a summary. `GET /procurement/submissions/{id}/documents/{document_id}` answers *"what exactly did you decide about **this file**, and why"* — the same `document` block the upload returned, fetched again long after the upload response has gone from the screen.

That matters because the diagnostics above are the whole argument for a rejection, and a 422 is the worst possible place to keep them: the upload screen is usually gone by the time somebody has to ask the client for a better file. **This is the durable copy.**

- The document **must belong to that submission**. A real document id under the wrong submission is a **404**, not somebody else's report — the two ids are checked against each other rather than the second being trusted on its own.
- **DM / MD only**, like the rest of intake.

## The submission lifecycle

`open` → `complete` (both slots accepted and clean) → `consumed` (Stage 2 picked it up). `rejected` is a terminal manual close. `queued` covers the gap where a submission is complete but Stage 2 has not started.

---

# 3. Between the two: what happens in Stages 2–4

Not covered here, but you need to know the shape to build the screen:

1. The submission's order sheet is broken down into **styles** (`POST /submissions/{id}/order-breakdown`, 202 then poll).
2. Each style gets its spec sheet and DXF pattern confirmed, then a **BOM generated** (202 then poll).
3. The **Cutting Manager confirms** the consumption figures; the **MD alone approves** — the DM is refused on purpose.
4. Approving runs the **inventory check**, which produces the **shortfall**.

Stage 5 begins with that shortfall. See `BOM_SYSTEM_GUIDE.docx` and `INVENTORY_SYSTEM_GUIDE.docx`.

---

# 4. Stage 5 — Generating the purchase orders

`POST /procurement/boms/{bom_id}/generate-pos` turns the shortfall into **draft POs, one per matched supplier**.

## Its three preconditions

| Situation | Response |
|---|---|
| the BOM does not exist | **404** |
| no inventory check has run | **409 `no_inventory_check`** |
| POs already exist for this BOM (and are not cancelled) | **200 with `already_generated: true`** and the existing POs — an idempotent replay, not an error |

A BOM with **no shortfall lines** returns an empty `purchase_orders[]` and the message *"No shortfall lines — nothing to order."* That is a success: everything was in stock.

## How a supplier is chosen

The supplier is matched from **what they have historically sold us** — the `supply_history` loaded by the supplier import — deterministically first, and never by a silent guess:

| # | Step | `match_method` | Auto-bound? |
|---|---|---|---|
| 1 | an exact historical supply of that article | `ledger` | yes |
| 2 | an alias rewrite, then step 1 again | `alias` | yes |
| 3 | the category / dominant-mode fallback | `category` | yes, but **ranked as weak** |
| 4 | a fuzzy candidate | — | **no — advisory only** |
| 5 | nothing confident | `null` | **the PO is held** |

Ranking is **recency · frequency · contactability − rate**. A tie, a category-only hit or low confidence sets **`ambiguous: true`** — do not auto-select; surface the shortlist and let the buyer pick. The full shortlist is on the PO as `candidates.ranked`, each entry carrying `supplier_name`, `score`, `txn_count`, `has_contact`, `last_rate` and `last_purchased_at`.

> **Contactability is a ranking term, not a hard filter.** A frequent contactless vendor still surfaces — possibly at the top — and the PO is flagged downstream instead of the vendor being hidden.

## The two flags that are your to-do list

| Flag | Means | Fix |
|---|---|---|
| **`needs_supplier: true`** | no supplier could be resolved for these lines | assign one via `PATCH /pos/{id}/items` |
| **`no_contact_channel: true`** | there *is* a supplier, but they have neither email nor phone | add a contact to the supplier |

Each unresolved line becomes **its own held draft PO**, not one combined one, so assigning a supplier to one never entangles another.

## Defaults on a generated line

Quantity is the shortfall. The unit price and UOM default to the **top-ranked supplier's last rate and unit** where there is one, and fall back to the BOM line's own. Each `po_item` keeps its `bom_item_id` and `inventory_item_id` back-links, so a PO line can always be traced to the BOM line and the stock row that produced it.

GST is computed from configuration: **CGST + SGST** for an intra-state supplier, **IGST** for inter-state, decided by comparing the supplier's `state_code` to the buyer's.

---

# 5. Stage 5 — The purchase-order lifecycle

```
draft ─submit─▶ pending_approval ─approve─▶ approved ─send─▶ sent
                       │                                       │
                       └──reject──▶ rejected ──▶ draft         ├─▶ responded ─▶ confirmed
                                                               └─▶ escalated ─▶ confirmed

  cancelled is reachable from any state BEFORE it is sent.
```

| Call | Effect | Refuses |
|---|---|---|
| `PATCH /pos/{id}/items` | edit lines, add or remove them, change the supplier. Same `base_revision` optimistic lock as the BOM. | **409 `po_locked`** once sent — cancel and re-issue instead; **409 `stale_revision`** |
| `POST /pos/{id}/submit` | ask for approval; notifies the routed approvers | **409 `not_submittable`** unless draft or rejected; **409 `needs_supplier`**; **409 `empty_po`** |
| `POST /pos/{id}/approve` | the decision | **409 `not_pending_approval`**; **403 `wrong_approver`** |
| `POST /pos/{id}/reject` | `reason` required | **422 `reason_required`**; same 409/403 as approve |
| `POST /pos/{id}/send` | number it, render the PDF, email it, start the escalation clock | **409 `not_approved`** |
| `POST /pos/{id}/acknowledge` | record the supplier's confirmation | — |
| `POST /pos/{id}/cancel` | cancel | **409 `po_locked`** once sent |

> **Changing the supplier on a PATCH re-routes the approver and resets any approval.** That is correct — a different vendor is a different decision — and it will surprise a user who thought they were fixing a typo.

## Approval routing depends on what is being bought

This is the least obvious rule in Stage 5, and a 403 is how you will discover it:

| Supplier type | May approve or reject |
|---|---|
| **`leather`** | **Cutting Manager**, MD |
| `accessory`, `service`, or unset | MD, **Direct Manager**, **HR** |

The **MD approves anything**. The router admits all four candidate roles, and the service then enforces the exact routing — so a Direct Manager who can open the screen can still be refused on a leather PO, with **403 `wrong_approver`** naming the roles it does route to. A supplier with no type set is treated as `accessory`.

**HR approving a purchase order is real**, not a bug. It is the only place HR touches procurement, and it is a money approval.

## Send is the point of no return

`POST /pos/{id}/send` does five things in one call:

1. allocates the **financial-year PO number** (April–March)
2. renders the PDF and stores it, **deduplicated by sha256**, using the template for that supplier type
3. routes by contact: a usable email is **emailed** with the PDF attached, a tracking pixel injected and links wrapped; otherwise it short-circuits to the WhatsApp rung; with neither, `no_contact_channel` is set and nothing goes out
4. sets `next_escalation_at` and starts the ladder
5. advances the production board to `po_raised`

An email address that has **hard-bounced** (`email_status: invalid`) is treated as no email at all.

---

# 6. Stage 5 — Chasing the supplier

Once a PO is sent, the system watches for a response.

| Field | Meaning |
|---|---|
| `first_opened_at` | they opened the email (tracking pixel) |
| `first_clicked_at` | they clicked a link in it |
| `current_rung` | how far up the escalation ladder this PO has climbed |
| `next_escalation_at` | when the sweeper will chase next |
| `acknowledged_at` / `acknowledged_channel` | **they confirmed — this stops the ladder** |

## The ladder

One rung per due PO per sweep, at `po_escalation_hours` (**default 5 hours**) apart:

| Rung | Action |
|---|---|
| 0 → 1 | **WhatsApp** the supplier |
| 1 → 2 | **auto-call** them |
| 2 → 3 | **exhausted** — an in-app notice to the buyer; no further automatic contact |

The sweep is DB-idempotent — the `acknowledged_at IS NULL` and `current_rung < 3` guards make a second pass a no-op — so it survives restarts and never double-chases.

**An acknowledgement on any channel stops the ladder immediately**, sets the status to `confirmed`, and advances the board to `po_confirmed`.

## The five routes that are not for your frontend

| Endpoint | Called by |
|---|---|
| `POST /procurement/webhooks/ses` | Amazon SES — delivery, bounce, complaint |
| `POST /procurement/webhooks/twilio/whatsapp` | Twilio — the supplier's reply |
| `POST /procurement/webhooks/twilio/voice` | Twilio — the supplier pressing 1 |
| `GET /procurement/t/o/{token}.gif` | the supplier's mail client fetching the pixel |
| `GET /procurement/t/c/{token}` | the supplier clicking a link (302 redirect) |

They exist for the outside world and carry an **opaque per-send token instead of a session**. The webhooks always answer `{"ok": true}` so the provider does not retry, and the pixel always returns an image — even for an unknown token — so a supplier never sees a broken image in their inbox.

A **hard bounce** flags the address invalid and short-circuits the PO to the WhatsApp rung rather than burning five hours on a dead address. A **complaint** suppresses further email.

## ⚠ Those five routes are gated today, and they must not be

They **declare no auth of their own** — correctly. But the supplier-PO router is mounted with `dependencies=[Depends(block_employees)]`, and that dependency requires a Bearer token. So today:

- a supplier opening the PO email fetches the pixel and gets **401** — the open is never recorded;
- a click gets **401** instead of the 302 — the supplier never reaches the target;
- SES delivery events and Twilio replies get **401** — an acknowledgement by WhatsApp or by pressing 1 is never recorded, and **the chase escalates as though the supplier ignored it**.

Nothing raises an error inside the factory, which is exactly why this survives: the failure is entirely on the supplier's side of the wire, and it looks identical to a supplier who is not responding. **These five routes need mounting outside the locked router, or with an explicit auth exemption.**

---

# 7. Stage 5 — Suppliers

| Call | Note |
|---|---|
| `POST /procurement/suppliers/import/preview` / `/commit` | load the directory **and the purchase history** from a workbook — always preview first |
| `GET /procurement/suppliers` | the directory |
| `GET /procurement/suppliers/{id}` | one supplier **plus their `supply_history` and `open_pos`** |
| `POST` / `PATCH /procurement/suppliers[/{id}]` | create and edit |
| `DELETE /procurement/suppliers/{id}` | **deactivates — never deletes.** Purchase history hangs off the row. Gated at MD — **but see the note below.** |
| `POST /procurement/suppliers/{id}/reactivate` | put them back in service. Same gate. |

> **Those last two say "MD" and admit the DM as well.** They are built with `require_roles(MANAGING_DIRECTOR)`, and `require_roles` waves both superuser roles — MD *and* DM — through every gate. Only `require_exact_roles` takes its allow-list literally, and Stage 5 does not use it anywhere. So deactivating a supplier is **MD or DM** in practice. The BOM approval gate is the one place in Phase 2 that really is one role (see `BOM_SYSTEM_GUIDE.docx` §6). Do not build a screen that promises a DM will be refused here.

Fields that change behaviour rather than just displaying:

| Field | Effect |
|---|---|
| **`has_contact`** | computed as `email or phone`. **Decides whether a PO can be sent at all** — adding a contact to a contactless supplier unblocks the send. |
| **`supplier_type`** | `leather` / `accessory` / `service`. **Drives both the PDF template and the approver routing** (§5). |
| **`state_code`** | drives intra- vs inter-state GST |
| **`email_status`** | `unknown` / `valid` / `invalid`. Set by bounce feedback; `invalid` makes the send skip email. |
| `payment_terms_days`, `lead_time_days`, `currency`, `gstin` | carried onto the PO |

The `supply_history` is the ledger the matcher ranks on: per normalised article, the `txn_count`, `last_purchased_at`, and `last_rate` / `min_rate` / `max_rate`.

---

# 8. The production board — the thread back to Phase 1

`GET /procurement/production-tracking` is one row per style:

| Field | Meaning |
|---|---|
| `status` | the ladder below |
| `po_count` | purchase orders raised for it |
| `po_confirmed_count` | how many suppliers have confirmed |
| `material_ready_at` | when everything needed had arrived |
| `released_at` | when it went to the floor |

```
awaiting_bom → bom_approved → inventory_checked → po_raised → po_confirmed
             → material_ready → released_to_production → in_production → completed
```

Most of those edges are advanced **automatically** as a side-effect of the call that earns them: approving a BOM, running an inventory check, sending the first PO, receiving an acknowledgement. `POST /production-tracking/{id}/transition` is the manual move for the rest.

When `po_confirmed_count` equals `po_count`, the material is on its way. **This is the seam where Phase 2 hands over to Phase 1.**

---

# 9. Background work

Two sweepers run **inside the API process**: BOM notification escalation (2 hours, internal) and supplier-PO escalation (5 hours, external). They share the lifespan loop and each has its own on/off setting.

> They are wrapped in a **Redis single-flight lock**, so exactly one process in the whole fleet does the work each cycle. Escalation **sends email and makes calls**, which is not idempotent from the recipient's side: without the lock, `workers × replicas` copies of every escalation go out per cycle — four from one box at `WEB_CONCURRENCY=4`, and 4×N behind a load balancer. It was also the main thing stopping the API from being scaled out at all.

---

# 10. Who can do what

Read **"plus MD and DM"** into every row that does not say otherwise — they are superusers and pass any ordinary role gate.

| Action | Roles |
|---|---|
| Stage-1 submissions, uploads, the per-document report | **Direct Manager, Managing Director** |
| Generate POs; PO edit, submit, send, cancel, acknowledge | DM, MD |
| **Approve / reject a purchase order** | **routed by supplier type** — leather: Cutting Manager + MD; accessory/service: MD, DM, HR |
| Supplier create, edit, import | DM, MD |
| Deactivate or reactivate a supplier | gated at MD, **but the DM passes too** — see §7 |
| Move the production board on | Cutting Manager, DM, MD |
| **Read-only: POs, suppliers, the production board** | DM, MD, **Viewer** |
| The webhooks and tracking pixels | **should be open** — see the warning in §6 |

> **`Viewer` is a real read role in Phase 2**, and almost nowhere else in the app. It can read purchase orders, the supplier directory, the production board and (in Stage 4) inventory checks. It exists so an accountant can see committed spend without being able to commit any.

---

# 11. Patterns a frontend must handle

| Pattern | Where |
|---|---|
| **Preview before commit** | the supplier import — and the inventory import in Stage 4 |
| **Optimistic locking (`base_revision` → 409 `stale_revision`)** | `PATCH /pos/{id}/items` |
| **Replay, not repeat** | `already_generated: true` on generate-pos; the cached verdict on a duplicate upload |
| **Structured error bodies** | most 4xx here carry `{"error": "…", "message": "…"}` **inside** `detail`, not just a sentence. Read `detail.error` to branch, `detail.message` or `suggested_fix` to display. |
| **A `reason_code` catalogue** | every Stage-1 rejection maps to a stable code with a fixed HTTP status — branch on the code, never on the message text |

---

# 12. Notes for backend developers

- **`presenters.py` in each module is pure serialisation** — ORM rows in, response dict out. No session, no rules, no I/O. Keep shape-only code there so the services stay orchestration.
- **The Stage-1 pipeline touches no session.** All the blocking work — scan, sniff, classify, store — is one synchronous function pushed into a threadpool; the async service persists the result. That is what keeps the event loop free.
- **`supplier_match.py` and `inventory_match.py` are pure and take a candidate pool**, never a session. The set-based fetch lives in the repository; the decision is unit-testable with no database.
- **Cross-module reads go through the owning service's DTO.** Supplier-PO reads the BOM and the inventory shortfall that way, and order/style identity through `clients.service` — never another module's repository or models.
- **`require_exact_roles` exists for the BOM sign-off.** Use `require_roles` for "this role or above"; use the exact form only where separation of duties *is* the requirement.
