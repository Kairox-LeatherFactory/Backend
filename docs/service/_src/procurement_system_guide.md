# 1. What this service is

**Phase 2 — everything that has to happen before the factory floor can start.**

Phase 1 tracks garments on the floor and assumes the material is already there. Phase 2 answers one question:

> *"A client just sent us an order. What material do we need, do we have it, and if not, who do we buy it from — and have they confirmed?"*

Today that is answered by people, with spreadsheets, phone calls and memory. This service turns it into a system.

> **A longer, three-audience version of this document already exists**: `docs/KAIROX_PROCUREMENT_SYSTEM_GUIDE.md` (and its PDF), plus `docs/KAIROX_PROCUREMENT_API_REFERENCE.md`. This guide is the working summary; go there for the full narrative and the algorithms.

---

# 2. The five stages

```
 ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐
 │ STAGE 1  │  │ STAGE 2  │  │ STAGE 3  │  │ STAGE 4  │  │ STAGE 5  │
 │ INTAKE   │─▶│   BOM    │─▶│ APPROVAL │─▶│INVENTORY │─▶│SUPPLIER  │
 │          │  │          │  │          │  │  CHECK   │  │   PO     │
 └──────────┘  └──────────┘  └──────────┘  └──────────┘  └──────────┘
  order sheet   bill of       the MD signs   what do we    buy the
  + spec sheet  materials     it off         already have  shortfall
  come in       is generated                 in stock?     and chase it
```

**They only work in order.** You cannot check inventory against a BOM that has not been generated, and you cannot buy a shortfall you have not measured. That is why the Postman folders are ordered by stage rather than alphabetically.

| Module | Stage | Code |
|---|---|---|
| Procurement | 1 — intake | `app/modules/procurement/` |
| BOM | 2 and 3 — generation and approval | `app/modules/bom/` |
| Inventory | 4 — the stock check | `app/modules/inventory/` |
| Supplier PO | 5 — purchase orders | `app/modules/supplier_po/` |

All four are mounted under `/api/v1/procurement/…` so they read as one workflow.

---

# 3. Stage 1 — Intake

## The flow

```
POST /procurement/submissions                    open a folder
POST /procurement/submissions/{id}/order-sheet   upload the order sheet
POST /procurement/submissions/{id}/spec-sheet    upload the spec sheet
GET  /procurement/submissions/{id}               is it ready for Stage 2?
```

There is also a **one-shot door** — `POST /procurement/upload/order-sheet` and `/upload/spec-sheet` — which opens the submission and uploads in a single call.

## The gate

`ready_for_stage_2` is `true` only when **both slots are `accepted`** and **neither has a blocking virus-scan status**. `blocking[]` says in words what is in the way, ready to display:

- `"spec_sheet missing"`
- `"order_sheet rejected"`
- `"spec_sheet scan_status=infected"`

## The upload is classified, not trusted

A file uploaded into the order-sheet slot is checked to see whether it **is** an order sheet. The verdict comes back in full:

| Field | Meaning |
|---|---|
| `validation.status` | `accepted` / `rejected` / `needs_manual_review` |
| `classified_as` | what it actually looks like |
| `confidence` | 0–1 |
| `method` | `heuristic`, `llm` or `manual` |
| `signals_expected` | what this kind of document must contain |
| `signals_found` | what the file actually contained |
| `signals_matched` | the overlap |
| `reason_code` | why it was rejected, as a code |
| `suggested_fix` | **why it was rejected, in words — show this** |

A rejection is a **422 with the same diagnostics**, so the screen can tell the user exactly what was missing rather than "upload failed".

## The other guards

| Guard | Status |
|---|---|
| unsupported file type | **415** |
| file too large | **413** |
| empty or corrupt | **422** |
| virus detected | **422** |
| virus scanner unavailable | **503** |
| wrong slot (a spec sheet posted as an order sheet) | **422** |
| the submission is already locked | **409** |
| byte-identical file already uploaded | **409** |

> **Re-uploading the same bytes replays the cached verdict** rather than re-classifying. The `sha256` on the document is what makes that possible.

---

# 4. Stage 2 — The BOM

Generation is **asynchronous**. You ask for it, you get a `202`, and you poll.

```
POST /procurement/order-styles/{order_style_id}/generate-bom   -> 202 queued
GET  /procurement/order-styles/{order_style_id}/bom            -> poll
       status: "not_started"  ->  keep polling
       status: "ready"        ->  bom: { … }
```

The same pattern applies to the order breakdown (`POST/GET …/order-breakdown`) and to a DXF pattern upload (`POST /procurement/patterns` → `202` with a `job_id` and a `channel`).

## What a BOM line carries

Beyond the obvious (material, colour, quantity per garment, unit price, totals), two fields matter for review:

| Field | Meaning |
|---|---|
| `dcm_source` | where the consumption figure came from — for example the DXF pattern |
| `dcm_confidence` | 0–1. **The low-confidence lines are the ones to check first.** |

## Editing — the optimistic lock

`PATCH /procurement/boms/{bom_id}/items` must send `base_revision`, the `revision` you last read.

- If somebody else edited it meanwhile, you get **409** carrying `current_revision`. Reload and re-apply.
- On success the BOM comes back with `revision` incremented.

That is what stops two people silently overwriting each other on a costing document.

---

# 5. Stage 3 — Approval, and the separation of duties

```
draft ──confirm-cutting──► ready_for_review ──approve──► approved ──► exported
                                   │                        │
                                   └────reject──► rejected ─┴─reopen──► draft
                                                                  (also: locked)
```

| Call | Who | Note |
|---|---|---|
| `POST /boms/{id}/confirm-cutting` | **Cutting Manager** | confirms the consumption figures. Moves `draft` → `ready_for_review`. |
| `POST /boms/{id}/approve` | **Managing Director ONLY** | refuses a BOM whose cutting has not been confirmed. `lock: true` → `locked`. |
| `POST /boms/{id}/reject` | **MD only** | `reason` required |
| `POST /boms/{id}/reopen` | DM / MD | back to `draft` |
| `POST /boms/{id}/export` | **MD only** | renders the document |

> **The Direct Manager is refused on approve, reject and export — on purpose.** The DM prepares and edits the BOM; the person who prepares a financial document must not be the person who approves it. This is the one place in KairoX where the DM's usual superuser bypass does not apply (`require_exact_roles`). The MD passes every gate in the system.

**Export is idempotent.** Calling it twice does not export twice: the second call replays the same document with `replay: true` and the **original** `exported_at`. Do not render a replay as a second export.

## Notifications

When a BOM is waiting for review, the approver gets a notification.

| Call | Use |
|---|---|
| `GET /procurement/notifications` | the list, `unread_only` optional |
| `GET /procurement/notifications/stream` | **Server-Sent Events** — a live push |
| `POST /procurement/notifications/{id}/open` | mark it read |

> **Marking it read is what stops the escalation.** An unopened notification is chased by a background sweeper, which eventually escalates it **by email**. That is why the sweeper runs under a fleet-wide lock — see §8.

---

# 6. Stage 4 — The inventory check

## First, load the warehouse

```
POST /procurement/inventory/preview   parse the spreadsheet, write nothing
POST /procurement/inventory/commit    the same parse, written
GET  /procurement/inventory/items     the master
```

The preview says `raw_count`, `kept`, `dropped` and a `warnings[]` naming the rows it could not use. **Always preview first** — the commit overwrites the master the check runs against.

## Then, run the check

```
POST /procurement/boms/{bom_id}/inventory-check    run it
GET  /procurement/boms/{bom_id}/inventory-check    re-read the last one
GET  /procurement/inventory-checks                 the dashboard
GET  /procurement/inventory-checks/{check_id}      one stored check
```

**The GET does not recompute.** It re-renders what the check said when it ran. That is deliberate: a purchase decision must be traceable to the numbers it was made on.

## Reading a line

| Field | Meaning |
|---|---|
| `required_qty` | what the BOM needs |
| `matched` | the inventory item it matched, and **how** (`method`). `null` = unmatched |
| `on_hand_qty` | on the shelf |
| `available_qty` | free to promise |
| `reserved_for_this_bom` | held for this BOM |
| `shortfall_qty` | **what must be bought** |
| `status` | `sufficient` / `partial` / `out_of_stock` |
| `flags` | `unmatched`, `uom_mismatch` |

Two flags deserve attention on the screen:

- **`unmatched`** — the material could not be found in the master at all. It is not "out of stock"; nobody knows.
- **`uom_mismatch`** — it matched, but the units disagree. The number may be meaningless until somebody looks.

## The badge

`summary.badge` is the **worst** line in the BOM: `out_of_stock` beats `partial` beats `sufficient`. Use it for the row on the dashboard; use the counts for the detail.

`shortfall_value` is shortfall × rate, summed — the money the shortfall represents.

## Reservations

A check **reserves** what it can. A reservation is `active` (counted against `available`), `released` (freed) or `consumed` (physically issued). That is how two BOMs cannot both be promised the same stock.

---

# 7. Stage 5 — Purchase orders

## Generating

`POST /procurement/boms/{bom_id}/generate-pos` turns the shortfall into draft POs, **one per matched supplier**.

The supplier is matched from **what they have historically sold us** — the `supply_history` on each supplier, loaded by the supplier import. When no supplier can be matched, the PO is still created with:

- **`needs_supplier: true`** — nobody is on it yet; somebody must choose.
- **`no_contact_channel: true`** — there is a supplier but they have neither email nor phone, so it cannot be sent.

Both are the screen's to-do list.

## The lifecycle

```
draft ─submit─► pending_approval ─approve─► approved ─send─► sent
                       │                                       │
                       └──reject──► rejected ──► draft         ├─► responded ─► confirmed
                                                               └─► escalated ─► confirmed

  cancelled is reachable from any state before it is sent.
```

| Call | Effect |
|---|---|
| `PATCH /pos/{id}/items` | edit lines. **409 `po_locked` once it is sent** — cancel and re-issue instead. Uses the same `base_revision` lock as the BOM. |
| `POST /pos/{id}/submit` | ask for approval |
| `POST /pos/{id}/approve` / `/reject` | the decision (`reason` required to reject) |
| `POST /pos/{id}/send` | emails it, generates the PDF, starts the escalation clock |
| `POST /pos/{id}/acknowledge` | record the supplier's confirmation |
| `POST /pos/{id}/cancel` | cancel |

## Chasing the supplier

Once a PO is sent, the system watches for a response:

| Field | Meaning |
|---|---|
| `first_opened_at` | they opened the email (tracking pixel) |
| `first_clicked_at` | they clicked a link in it |
| `current_rung` | how far up the escalation ladder this PO has climbed |
| `next_escalation_at` | when the sweeper will chase next |
| `acknowledged_at` / `acknowledged_channel` | **they confirmed — this stops the ladder** |

The chase runs over three channels: **email**, then **WhatsApp**, then a **voice call**. The replies arrive through webhooks:

| Endpoint | Called by |
|---|---|
| `POST /procurement/webhooks/ses` | Amazon SES (delivery, bounce, complaint) |
| `POST /procurement/webhooks/twilio/whatsapp` | Twilio |
| `POST /procurement/webhooks/twilio/voice` | Twilio |
| `GET /procurement/t/o/{token}.gif` | the supplier's mail client (the pixel) |
| `GET /procurement/t/c/{token}` | the supplier clicking a link (302 redirect) |

> **None of those five are for your frontend.** They exist for the outside world. The webhooks always answer `{"ok": true}` so the provider does not retry, and the pixel always returns an image — even for an unknown token — so a supplier never sees a broken image in their inbox.

## Suppliers

| Call | Note |
|---|---|
| `POST /procurement/suppliers/import/preview` / `/commit` | load the directory and the purchase history from a workbook |
| `GET /procurement/suppliers` | the directory |
| `GET /procurement/suppliers/{id}` | one supplier **plus their supply history and open POs** |
| `POST` / `PATCH` | create and edit |
| `DELETE` | **deactivates — never deletes.** Purchase history hangs off the row. |
| `POST /suppliers/{id}/reactivate` | put them back in service |

`has_contact` on a supplier row is the field that decides whether a PO can be sent at all. **Adding a contact to a contactless supplier unblocks the send.**

---

# 8. The production board — the thread back to the floor

`GET /procurement/production-tracking` is one row per style:

| Field | Meaning |
|---|---|
| `po_count` | purchase orders raised for it |
| `po_confirmed_count` | how many suppliers have confirmed |
| `material_ready_at` | when everything needed had arrived |
| `released_at` | when it went to the floor |

When `po_confirmed_count` equals `po_count`, the material is on its way. `POST /production-tracking/{id}/transition` moves a row on.

This is the seam where Phase 2 hands over to Phase 1.

---

# 9. Background work

Two sweepers run **inside the API process**: BOM notification escalation and supplier-PO escalation.

> They are wrapped in a **Redis single-flight lock**, so exactly one process in the whole fleet does the work each cycle. Escalation **sends email**, which is not idempotent from the recipient's side: without the lock, `workers × replicas` copies of every escalation go out per cycle — four from one box at `WEB_CONCURRENCY=4`, and 4×N behind a load balancer. It was also the main thing stopping the API from being scaled out at all.

Both can be switched off with their settings flags.

---

# 10. Who can do what

| Action | Roles |
|---|---|
| Stage-1 uploads, submissions | Direct Manager, Managing Director |
| Confirm cutting on a BOM | **Cutting Manager** |
| Approve / reject / export a BOM | **Managing Director only** — the DM is refused |
| Everything else in Stages 2–5 | DM and MD |
| Admin config (cost catalogue, checks, DXF yields, POM dictionary) | DM and MD |

---

# 11. Patterns a frontend must handle

| Pattern | Where |
|---|---|
| **202 and poll** | BOM generation, order breakdown, pattern upload |
| **Server-Sent Events** | `GET /procurement/notifications/stream` |
| **Optimistic locking (`base_revision` → 409)** | BOM items, PO items |
| **Replay, not repeat** | BOM export (`replay: true`), duplicate uploads (cached verdict) |
| **Structured 409/422 bodies** | many errors here carry `{"error": "...", ...}` inside `detail`, not just a sentence — read `detail.error` |

---

# 12. Notes for backend developers

- **`presenters.py` in each module is pure serialisation** — ORM rows in, response dict out. No session, no rules, no I/O. Keep shape-only code there so the services stay orchestration.
- **Inventory keys everything to a `bom_id`.** That is exactly why it is a different module from Phase-1 `material`, and why the two must not be merged (`CLAUDE.md` §12).
- **The Phase-1 → Phase-2 bridge, when it comes, is a connection and not a rewrite:** the BOM's inventory check will read stock, and the physical stock it reads against **is** the Phase-1 material lots.
- **`require_exact_roles` exists for the BOM sign-off.** Use `require_roles` for "this role or above"; use the exact form only where separation of duties *is* the requirement.
