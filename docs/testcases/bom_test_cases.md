# BOM — API Test Cases

> Stage 2/3 — BOM generation, review and approval · module `app/modules/bom` · 152 cases

API test cases in the order the process actually runs — intake breakdown → style attachment & pattern → BOM generation → the BOM review/approval lifecycle → notifications → admin setup → full regression. Ready-made requests: bom.postman_collection.json.

## Connection

| | |
|---|---|
| Base URL | `http://127.0.0.1:8000` |
| Module prefix | `/api/v1/procurement` |
| Login | `POST /api/v1/auth/login` with `{"username": phone, "password": phone}` |
| Auth header | `Authorization: Bearer <token>` |
| Docs | `/docs` (Swagger) |

### Demo accounts (phone = password)

| Role | Phone | Collection variable |
|---|---|---|
| Managing Director | `9000000000` | `{{md_token}}` |
| Direct Manager | `9000000001` | `{{dm_token}}` |
| Cutting Manager | `9000000002` | `{{cm_token}}` |
| Stitching Manager | `9000000003` | `{{other_token}}` |
| Office Viewer | `9000000004` | `{{viewer_token}}` |
| HR / Accounts | `9000000005` | `{{hr_token}}` |

## Case mix

| Category | Count |
|---|---|
| Positive (POS) | 43 |
| Negative (NEG) | 46 |
| Boundary (BND) | 29 |
| Business Logic (LGC) | 34 |
| **Total** | **152** |

## Endpoints covered

| Group | Endpoints | Role gate | Cases |
|---|---|---|---|
| A · Open & view a BOM | `GET /boms/{bom_id}` | — | 9 |
| B · Edit line items in the grid | `PATCH /boms/{bom_id}/items` | — | 22 |
| C · Confirm Cutting | `POST /boms/{bom_id}/confirm-cutting` | — | 9 |
| D · MD Review Panel — Approve | `POST /boms/{bom_id}/approve` | — | 8 |
| E · MD Review Panel — Reject | `POST /boms/{bom_id}/reject` | — | 12 |
| F · MD Review Panel — Reopen | `POST /boms/{bom_id}/reopen` | — | 6 |
| G · MD Review Panel — Export | `POST /boms/{bom_id}/export` | — | 9 |
| H · Full-flow regression scenarios | `Chained calls across the endpoints above.` | — | 3 |
| I · Order Breakdown (Stage 2 intake) | `POST + GET /submissions/{submission_id}/order-breakdown` | — | 10 |
| J · Order Style Attachments | `POST /order-styles/{order_style_id}/attachments` | — | 9 |
| K · Generate BOM from Order Style | `POST /order-styles/{order_style_id}/generate-bom` | — | 8 |
| L · Notifications | `GET /notifications · GET /notifications/stream · POST /notifications/{id}/open` | — | 10 |
| M · Pattern Upload | `POST /patterns (multipart/form-data)` | — | 7 |
| N · Admin — DXF Yields | `PUT /admin/dxf-yields/{species}` | — | 6 |
| O · Admin — Fabric Roles | `POST /admin/fabric-roles` | — | 4 |
| P · Admin — Cost Catalog | `GET /admin/cost-catalog · PUT /admin/cost-catalog/{garment_code}` | — | 8 |
| Q · Admin — BOM Checks | `GET /admin/checks · PUT /admin/checks/{client_code}` | — | 6 |
| R · Admin — POM Dictionary | `GET + POST /admin/pom-dictionary` | — | 6 |

## Verify before relying on

- No list/search-BOMs endpoint exists — every BOM case needs a `bom_id` you already have (from seed data or a prior response).
- There is no "Pattern Maker" role in the codebase. For wrong-role cases use any seeded role outside cutting_manager / direct_manager / managing_director — e.g. Lining Manager or Office Viewer.
- `direct_manager` currently bypasses every MD-only gate (approve/reject/export/reopen, and every `/admin/*` route) via the superuser policy.
- **Error shapes are inconsistent**: role checks, 401s and most router-level pre-checks return `{"detail": "..."}`; most lifecycle errors raised in the service layer return `{"error": "...", "message"?: "..."}`. Check both.
- A non-UUID path parameter returns a generic 422 from FastAPI's path parsing, before any handler runs — distinct from a 404 for a well-formed but nonexistent id.
- `base_revision` is `ge=1`; always substitute the BOM's real current revision from a fresh GET (the old checked-in example shipped 0, which always fails).
- **D-L2 / E-L1:** approve only checks that `cutting_confirmed_at` is set — it never re-checks status, and reject never clears it. Approving a rejected-but-previously-confirmed BOM is expected to return 200 today; confirm and flag.
- **Q-B1:** a check rule with a single-element `range` array passes schema validation then crashes the handler with an unhandled IndexError → HTTP 500. Confirmed defect.
- **R-L1:** an unrecognized `garment_type_code` is not rejected — it silently falls back to "applies to any garment type".
- The admin cost-catalog and checks GETs read a process-local cache that is not refreshed on a TTL despite the comments — under multiple workers, PUT-then-GET can appear not to have taken effect.

## A · Open & view a BOM

`GET /boms/{bom_id}`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **A-P1** | POS | High | Cutting Manager opens a Draft BOM<br>_Cutting Manager (9000000002)_ | `GET /api/v1/procurement/boms/{bom_id}` | Full BOM view JSON: {id, status:"draft", revision, garment_fob_price, bulk_total, items:[...]}. (cf. GB-P1) |
| **A-P2** | POS | Medium | MD opens the same BOM<br>_Managing Director (9000000000)_ | `GET /api/v1/procurement/boms/{bom_id}` | Same 200 view — MD is a superuser and bypasses the cutting_manager role gate. |
| **A-P3** | POS | Low | Direct Manager opens the BOM<br>_Direct Manager (9000000001)_ | `GET /api/v1/procurement/boms/{bom_id}` | Same 200 view — DM is also in the superuser bypass set. |
| **A-N1** | NEG | Medium | Open a BOM that no longer exists<br>_Any allowed role_ | `GET /api/v1/procurement/boms/{a made-up or deleted uuid}` | {"detail":"BOM not found."} |
| **A-N2** | NEG | High | Wrong role tries to open a BOM<br>_Lining Manager (9000000006) or Office Viewer (9000000004)_ | `GET /api/v1/procurement/boms/{bom_id}` | {"detail":"Role 'lining_manager' is not permitted for this action"} |
| **A-N3** | NEG | Medium | Session expired<br>_Any role, expired/invalid token_ | `GET /api/v1/procurement/boms/{bom_id} — Authorization: Bearer {expired or malformed token}` | 401 Unauthorized. Verify the exact detail text against get_current_user in app/modules/users/deps.py before asserting it literally. |
| **A-B1** | BND | Medium | BOM with zero line items<br>_Cutting Manager_ | `GET /api/v1/procurement/boms/{bom_id} (a BOM whose spec extraction produced no material lines)` | 200 with items:[]; garment_fob_price and bulk_total are 0, not null/NaN. |
| **A-L1** | LGC | Low | Exported BOM still fully viewable<br>_Cutting Manager / MD / DM_ | `GET /api/v1/procurement/boms/{bom_id} (status = exported)` | 200 with status:"exported", export_document_id and exported_at populated. |
| **A-L2** | LGC | Medium | Rejected BOM shows the reason<br>_Cutting Manager / MD / DM_ | `GET /api/v1/procurement/boms/{bom_id} (status = rejected)` | 200 with status:"rejected" and rejection_reason populated with the MD's text. |

## B · Edit line items in the grid

`PATCH /boms/{bom_id}/items`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **B-P1** | POS | High | Edit Unit Price on a Draft BOM<br>_Cutting Manager_ | `PATCH /api/v1/procurement/boms/{bom_id}/items`<br>`Body: {"base_revision": <current revision from GET>, "edits":[{"bom_item_id":"<uuid>","field":"unit_price","value":12.50}]}` | {"revision": base_revision+1, "recomputed": {...}, "reconfirm_required": false} (cf. EI-P1) |
| **B-P2** | POS | High | Edit DCM on a leather line<br>_Cutting Manager_ | `PATCH .../items — edits:[{"bom_item_id":"<main-material line>","field":"dcm","value":<n>}]` | 200; recomputed.items[].dcm_source flips to "manual" for that line. |
| **B-P3** | POS | Medium | Edit several cells, save once<br>_Cutting Manager_ | `PATCH .../items — edits array with 3 entries (different bom_item_id/field/value) in one call` | 200, single call; recomputed reflects all 3 changes together. |
| **B-P4** | POS | Low | Edit a BOM that's Ready for Review but not yet confirmed<br>_Cutting Manager_ | `PATCH .../items on a BOM with status = ready_for_review` | 200 — _EDITABLE_STATES includes both draft and ready_for_review. |
| **B-P5** | POS | Low | Edit the DCM cell specifically (not the Qty alias)<br>_Cutting Manager_ | `PATCH .../items — field:"dcm" instead of "qty_per_garment"` | 200, identical effect — the service writes both fields to the same qty_per_garment column. |
| **B-N1** | NEG | High | Leave Unit Price blank<br>_Cutting Manager_ | `PATCH .../items — edits:[{"bom_item_id":"...","field":"unit_price"}] with "value" omitted or null` | 422 Pydantic validation error — "value" is a required field on each edit. |
| **B-N2** | NEG | High | Negative quantity<br>_Cutting Manager_ | `PATCH .../items — {"field":"qty_per_garment","value":-5}` | {"error":"negative_value","bom_item_id":...,"field":"qty_per_garment","value":-5} (cf. EI-N7) |
| **B-N3** | NEG | Medium | Non-numeric text in a numeric cell<br>_Cutting Manager_ | `PATCH .../items — {"field":"unit_price","value":"abc"}` | 422 — fails FastAPI's float coercion before it reaches the service layer (generic Pydantic error shape, not the custom invalid_value error). |
| **B-N4** | NEG | High | Two testers edit the same BOM at once<br>_Cutting Manager (two sessions)_ | `Both GET the BOM, note revision N`<br>`Session A: PATCH .../items {"base_revision":N,...} → succeeds`<br>`Session B: PATCH .../items {"base_revision":N,...} on a different line → sent after A` | Session A: 200. Session B: {"error":"stale_revision","current_revision":N+1} — enforced twice (pre-check and an atomic claim_revision at commit). (cf. EI-N8/N12) |
| **B-N5** | NEG | High | Try to edit an Approved/Locked/Exported BOM<br>_Cutting Manager_ | `PATCH .../items on a BOM with status in {approved, locked, exported}` | {"error":"bom_locked","message":"A locked/approved BOM rejects all edits."} |
| **B-N6** | NEG | Medium | Try to edit a Rejected BOM<br>_Cutting Manager_ | `PATCH .../items on a BOM with status = rejected` | {"error":"bom_rejected","message":"Reopen the rejected BOM before editing."} |
| **B-N7** | NEG | Medium | Wrong role reaches the edit screen<br>_Lining Manager / Office Viewer_ | `PATCH /api/v1/procurement/boms/{bom_id}/items` | 403 role-not-permitted, same shape as A-N2. |
| **B-B1** | BND | Medium | Qty set to exactly 0<br>_Cutting Manager_ | `PATCH .../items — {"field":"qty_per_garment","value":0}` | 200 — 0 is the legal floor, only negative values are rejected. |
| **B-B2** | BND | Medium | Unreasonably large price<br>_Cutting Manager_ | `PATCH .../items — {"field":"unit_price","value":999999999}` | 200, accepted with no ceiling — flag as a possible missing sanity check server-side too. |
| **B-B3** | BND | Low | Smallest possible edit<br>_Cutting Manager_ | `PATCH .../items — edits array with exactly one entry` | 200 — a single-item batch is a valid request. |
| **B-B4** | BND | Low | Change the same cell twice before saving<br>_Cutting Manager_ | `Not separately API-testable — the client only ever sends the final chosen value in one PATCH call; there is no server-side "history" of unsent edits to probe.` | Confirm only that a single PATCH with value:15 stores 15 — nothing else to assert at the API layer. |
| **B-B5** | BND | Low | High-precision price<br>_Cutting Manager_ | `PATCH .../items — {"field":"unit_price","value":12.123456789}`<br>`GET /api/v1/procurement/boms/{bom_id}` | PATCH → 200. Follow-up GET → unit_price still shows full precision, not rounded. |
| **B-L1** | LGC | Low | Non-material lines don't get the "Manual" stamp<br>_Cutting Manager_ | `PATCH .../items — edit a Thread or Accessory category line's qty_per_garment` | 200; that line's dcm_source is untouched — the manual stamp only applies to main/sub-material, lining and interlining categories. |
| **B-L2** | LGC | High | Editing DCM after Confirm Cutting re-opens the gate<br>_Cutting Manager_ | `POST .../confirm-cutting (status → ready_for_review)`<br>`PATCH .../items — edit "dcm" or "qty_per_garment" on a material line` | 200; response has "reconfirm_required": true, recomputed.status:"draft", cutting_confirmed_at cleared. (cf. EI-L2) |
| **B-L3** | LGC | Medium | A price-only edit does NOT re-open the gate<br>_Cutting Manager_ | `On the same confirmed BOM, PATCH .../items — edit only "unit_price"` | 200; "reconfirm_required": false, status stays ready_for_review. |
| **B-L4** | LGC | Medium | Totals stay in sync with edits<br>_Cutting Manager_ | `PATCH .../items — raise "qty_per_garment" on one line` | 200; recomputed.garment_fob_price and recomputed.bulk_total both reflect the change in the same response. |
| **B-L5** | LGC | Low | Revision counter never skips<br>_Cutting Manager_ | `PATCH .../items three times in a row, each time using the "revision" returned by the previous call as the next base_revision` | Each response's "revision" is exactly +1 over the last — no skips, no repeats. |

## C · Confirm Cutting

`POST /boms/{bom_id}/confirm-cutting`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **C-P1** | POS | High | Confirm a Draft BOM<br>_Cutting Manager_ | `POST /api/v1/procurement/boms/{bom_id}/confirm-cutting (status = draft, material lines filled)` | {"bom_id":..., "status":"ready_for_review", "cutting_confirmed_at":..., "templates_backfilled":N, "notifications_created":N} (cf. CC-P1) |
| **C-P2** | POS | Low | Re-click Confirm Cutting on an already-confirmed BOM<br>_Cutting Manager_ | `POST .../confirm-cutting again (status already = ready_for_review)` | 200 again — draft and ready_for_review are both allowed states; cutting_confirmed_at is refreshed. |
| **C-N1** | NEG | Medium | Try on an Approved/Locked/Exported BOM<br>_Cutting Manager_ | `POST .../confirm-cutting on status in {approved, locked, exported}` | {"error":"invalid_state_for_confirmation","current_status":...,"message":"Cutting confirmation is only allowed on a draft BOM."} |
| **C-N2** | NEG | Medium | Try on a Rejected BOM<br>_Cutting Manager_ | `POST .../confirm-cutting on status = rejected` | Same invalid_state_for_confirmation error, current_status:"rejected". |
| **C-N3** | NEG | Medium | Wrong role<br>_Lining Manager / Office Viewer_ | `POST /api/v1/procurement/boms/{bom_id}/confirm-cutting` | 403 role-not-permitted. |
| **C-B1** | BND | Low | BOM with only non-material lines<br>_Cutting Manager_ | `POST .../confirm-cutting on a BOM with only thread/accessory/packaging lines` | 200, "templates_backfilled": 0 — nothing to remember, but no error. |
| **C-B2** | BND | Medium | Confirm while a material line's DCM is still 0<br>_Cutting Manager_ | `Leave a material line's qty_per_garment at 0`<br>`POST .../confirm-cutting anyway` | 200 — the endpoint doesn't block on a provisional 0; worth flagging as a possible product-level gap. |
| **C-L1** | LGC | Medium | Confirmed values become the "remembered" template<br>_Cutting Manager_ | `POST .../confirm-cutting, note the confirmed qty_per_garment for a style/size`<br>`Generate a new BOM for the same style/size (endpoint not confirmed by source read — likely under a /specs or /styles route; locate it before testing)` | New BOM's matching material line pre-fills from the confirmed value instead of starting blank. |
| **C-L2** | LGC | Low | Reviewer notification carries the right context<br>_Cutting Manager then MD_ | `POST .../confirm-cutting on a named style/order`<br>`Fetch the MD's notification (endpoint not covered by this report — check /api/v1 for a notifications route)` | Notification text names the correct style and order number, not a generic message. |

## D · MD Review Panel — Approve

`POST /boms/{bom_id}/approve`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **D-P1** | POS | High | Approve a confirmed BOM<br>_Managing Director_ | `POST /api/v1/procurement/boms/{bom_id}/approve — body {} (status = ready_for_review, cutting_confirmed_at set)` | {"bom_id":..., "status":"approved", "inventory_check_id":...} (cf. AP-P1) |
| **D-P2** | POS | Medium | Approve and Lock<br>_Managing Director_ | `POST .../approve — body {"lock": true}` | {"status":"locked", ...} instead of "approved". |
| **D-P3** | POS | Medium | Direct Manager approves<br>_Direct Manager_ | `POST .../approve — body {}` | 200 — DM is in the superuser bypass set, so this succeeds even though the endpoint is nominally MD-only. (cf. AP-L5) |
| **D-N1** | NEG | High | Try to Approve before cutting is confirmed<br>_Managing Director_ | `POST .../approve on a BOM where cutting_confirmed_at is null` | {"error":"cutting_confirmation_required","message":"The cutting manager must confirm the BOM before approval."} |
| **D-N2** | NEG | Medium | Cutting Manager tries to reach the review panel<br>_Cutting Manager_ | `POST /api/v1/procurement/boms/{bom_id}/approve` | 403 — cutting_manager isn't in the MD-or-DM gate and has no bypass here. |
| **D-B1** | BND | Medium | Double-click / re-approve an already-Approved BOM<br>_Managing Director_ | `POST .../approve again on a BOM already status = approved` | 200 again — approve_bom only checks cutting_confirmed_at, not current status, so a repeat call succeeds without error. |
| **D-L1** | LGC | Medium | Approve still completes if the inventory check fails behind the scenes<br>_Managing Director_ | `POST .../approve on a BOM likely to trip an inventory-check edge case` | 200 "approved" regardless — the inventory check runs best-effort and its failure is swallowed/logged, not surfaced as a request error; inventory_check_id may come back null. |
| **D-L2** | LGC | High | [Verify] Approve shouldn't be reachable on a Rejected BOM<br>_Managing Director_ | `Get a BOM to status = rejected that was cutting-confirmed beforehand (cutting_confirmed_at is not cleared by reject)`<br>`POST /api/v1/procurement/boms/{bom_id}/approve` | Per source: approve_bom() never re-checks bom.status — only cutting_confirmed_at. Expect 200 "approved" even though status is "rejected". This is a real backend gap, not UI-only — confirm and report as a finding regardless of what any UI does to hide the button. |

## E · MD Review Panel — Reject

`POST /boms/{bom_id}/reject`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **E-P1** | POS | High | Reject with a reason<br>_Managing Director_ | `POST /api/v1/procurement/boms/{bom_id}/reject — {"reason":"Leather grade mismatch vs. spec"} (status = ready_for_review)` | {"bom_id":..., "status":"rejected", "rejection_reason":"Leather grade mismatch vs. spec"} (cf. RJ-P1) |
| **E-P2** | POS | Low | Direct Manager rejects<br>_Direct Manager_ | `POST .../reject — {"reason":"..."}` | 200 — DM bypass, same as MD. |
| **E-N1** | NEG | High | Try to confirm with no reason<br>_Managing Director_ | `POST .../reject — {"reason":""} or the "reason" key omitted entirely` | 422 — Pydantic's min_length=1 rejects an empty string / missing field. |
| **E-N2** | NEG | Medium | Reason is only spaces<br>_Managing Director_ | `POST .../reject — {"reason":"   "}` | Passes Pydantic (3 chars, non-empty) but caught server-side: {"error":"reason_required","message":"A rejection reason is required."} |
| **E-N3** | NEG | Medium | Try to Reject a Draft BOM<br>_Managing Director_ | `POST .../reject on status = draft` | {"error":"not_ready_for_review","current_status":"draft"} — reject is only valid from ready_for_review. |
| **E-N4** | NEG | Medium | Try to Reject an Approved/Locked/Exported BOM<br>_Managing Director_ | `POST .../reject on status in {approved, locked, exported}` | {"error":"bom_locked","message":"An approved BOM cannot be rejected."} |
| **E-N5** | NEG | Low | Try to Reject a BOM that's already Rejected<br>_Managing Director_ | `POST .../reject on status = rejected` | {"error":"not_ready_for_review","current_status":"rejected"} |
| **E-N6** | NEG | Medium | Cutting Manager tries to Reject<br>_Cutting Manager_ | `POST /api/v1/procurement/boms/{bom_id}/reject` | 403 role-not-permitted. |
| **E-B1** | BND | Low | 1-character reason<br>_Managing Director_ | `POST .../reject — {"reason":"x"}` | 200, accepted. |
| **E-B2** | BND | Low | Reason with extra spaces<br>_Managing Director_ | `POST .../reject — {"reason":" wrong color "}`<br>`GET /api/v1/procurement/boms/{bom_id}` | POST → 200. GET's rejection_reason should read "wrong color" trimmed — the source read confirmed reject_bom checks reason.strip() for emptiness but did not confirm whether the stored value itself is trimmed or raw; verify this directly against the GET response. |
| **E-B3** | BND | Low | Very long reason<br>_Managing Director_ | `POST .../reject — {"reason":"<several paragraphs>"}`<br>`GET /api/v1/procurement/boms/{bom_id}` | Both calls 200; GET's rejection_reason returns the full text, not truncated. |
| **E-L1** | LGC | Medium | Cutting-confirmed state silently survives a rejection<br>_Managing Director_ | `POST .../reject on a BOM that had already passed confirm-cutting`<br>`GET /api/v1/procurement/boms/{bom_id}` | POST → 200 rejected. GET afterward: cutting_confirmed_at is still populated — reject_bom does not clear it. This is the same underlying fact that makes D-L2 possible; confirm both together. |

## F · MD Review Panel — Reopen

`POST /boms/{bom_id}/reopen`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **F-P1** | POS | High | Reopen a Rejected BOM<br>_Direct Manager or MD_ | `POST /api/v1/procurement/boms/{bom_id}/reopen (status = rejected)` | {"bom_id":..., "status":"draft", "revision": <previous+1>} (cf. RO-P1) |
| **F-N1** | NEG | Medium | Try to Reopen a BOM that isn't Rejected<br>_Direct Manager or MD_ | `POST .../reopen on status in {draft, ready_for_review, approved, locked, exported}` | {"error":"not_rejected","current_status":...} |
| **F-N2** | NEG | Medium | Cutting Manager tries to Reopen<br>_Cutting Manager_ | `POST /api/v1/procurement/boms/{bom_id}/reopen` | 403 — cutting_manager has no bypass on this gate (only direct_manager/managing_director are allowed). |
| **F-B1** | BND | Low | Double-click Reopen<br>_Direct Manager or MD_ | `POST .../reopen`<br>`POST .../reopen again immediately` | First call: 200 → draft. Second call: 409 {"error":"not_rejected","current_status":"draft"} — sequential calls fail safely rather than double-incrementing; true simultaneous requests may race, worth trying both ways. |
| **F-L1** | LGC | High | Approve is correctly re-blocked after Reopen<br>_Direct Manager/MD then MD_ | `POST .../reopen`<br>`POST .../approve on the same bom_id, without re-running confirm-cutting` | Reopen clears cutting_confirmed_at, so approve returns {"error":"cutting_confirmation_required",...} — the D-L2 loophole is closed once reopen actually runs. |
| **F-L2** | LGC | Low | Grid is fully editable immediately after Reopen<br>_Direct Manager/MD then Cutting Manager_ | `POST .../reopen`<br>`PATCH .../items immediately after` | 200 — draft is an editable state, no residual lock from the prior rejected status. |

## G · MD Review Panel — Export

`POST /boms/{bom_id}/export`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **G-P1** | POS | High | Export an Approved BOM<br>_Managing Director_ | `POST /api/v1/procurement/boms/{bom_id}/export (status = approved)` | {"bom_id":..., "status":"exported", "export_document_id":..., "sha256":..., "mime":..., "storage_url":...} (cf. EX-P1) |
| **G-P2** | POS | Medium | Export a Locked BOM<br>_Managing Director_ | `POST .../export (status = locked)` | Same result — status → "exported". |
| **G-P3** | POS | Medium | Re-export an already-Exported BOM<br>_Managing Director_ | `POST .../export again (status already = exported)` | 200 with the same export_document_id and sha256 — idempotent short-circuit, no regeneration. |
| **G-P4** | POS | Low | Direct Manager exports<br>_Direct Manager_ | `POST .../export` | 200 — DM bypass, same as MD. |
| **G-N1** | NEG | Medium | Try to Export a Draft/Ready-for-Review/Rejected BOM<br>_Managing Director_ | `POST .../export on status in {draft, ready_for_review, rejected}` | {"error":"not_approved","message":"Export is only allowed from an approved/locked BOM."} |
| **G-N2** | NEG | Medium | Cutting Manager tries to Export<br>_Cutting Manager_ | `POST /api/v1/procurement/boms/{bom_id}/export` | 403 role-not-permitted. |
| **G-B1** | BND | Low | Two BOMs that export to an identical document<br>_Managing Director_ | `POST .../export on BOM #1`<br>`POST .../export on BOM #2 (same underlying content)` | Both 200; the "sha256" values match and the export dedupes to the same stored file. |
| **G-L1** | LGC | Medium | Exported PDF carries the right approval details<br>_Managing Director_ | `POST .../export`<br>`GET the "storage_url" from the response and download the PDF` | The PDF itself (not the JSON) shows the approver's name, approval date, style name and order number — this needs a binary fetch + manual/PDF-text check, not just a JSON assertion. |
| **G-L2** | LGC | Low | "Exported at" doesn't move on a repeat export<br>_Managing Director_ | `POST .../export, then GET /api/v1/procurement/boms/{bom_id} and note exported_at`<br>`POST .../export again later, GET the BOM again` | exported_at is identical across both GETs — the idempotent re-export path doesn't touch the timestamp. |

## H · Full-flow regression scenarios

`Chained calls across the endpoints above.`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **H-1** | LGC | High | Happy path, start to finish<br>_Cutting Manager → MD_ | `Login Cutting Manager → GET /boms/{bom_id} → PATCH /boms/{bom_id}/items`<br>`POST /boms/{bom_id}/confirm-cutting`<br>`Login MD → POST /boms/{bom_id}/approve`<br>`POST /boms/{bom_id}/export → GET the storage_url` | Status walks draft → ready_for_review → approved → exported with a 200 (or the expected success shape) at every step and no 4xx along the golden path. |
| **H-2** | LGC | High | Reject → Reopen → resubmit<br>_MD → DM → Cutting Manager → MD_ | `Login MD → POST /boms/{bom_id}/reject {"reason":"..."} (status must be ready_for_review)`<br>`Login DM → POST /boms/{bom_id}/reopen`<br>`Login Cutting Manager → PATCH /boms/{bom_id}/items → POST /boms/{bom_id}/confirm-cutting`<br>`Login MD → POST /boms/{bom_id}/approve` | Every call 200 in sequence; the BOM is fully re-usable after one rejection cycle — no 409 anywhere in this chain if done in order. |
| **H-3** | LGC | High | Two testers, same BOM, same moment<br>_Cutting Manager (two sessions)_ | `Both sessions GET /boms/{bom_id}, note the same revision N`<br>`Fire both PATCH /boms/{bom_id}/items calls (different bom_item_id/field) with base_revision:N within a few seconds of each other` | Exactly one PATCH returns 200; the other returns 409 stale_revision. Neither edit is silently dropped or silently merged. |

## I · Order Breakdown (Stage 2 intake)

`POST + GET /submissions/{submission_id}/order-breakdown`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **I-P1** | POS | High | Claim a COMPLETE submission for breakdown<br>_Any authenticated role_ | `POST /api/v1/procurement/submissions/{submission_id}/order-breakdown (submission status = COMPLETE, no OrderStyle rows yet)` | {"submission_id":..., "status":"queued", "task_id":...} — claims the submission (status → CONSUMED) and enqueues bom.build_order_breakdown_for_submission. |
| **I-P2** | POS | High | Read the breakdown once it's ready<br>_Any authenticated role_ | `GET /api/v1/procurement/submissions/{submission_id}/order-breakdown (after the Celery task has completed)` | {"status":"ready", "styles":[{style_signature, style_name, qty, per_size_qty, colors[], spec_match_status, dxf_match_status, ...}]} |
| **I-P3** | POS | Medium | Poll while the breakdown is still processing<br>_Any authenticated role_ | `Immediately after I-P1's POST, GET the same submission's order-breakdown before the worker finishes` | {"status":"processing", "styles":[]} — submission status is already CONSUMED but no OrderStyle rows exist yet. |
| **I-N1** | NEG | High | Claim a submission that isn't COMPLETE<br>_Any authenticated role_ | `POST .../order-breakdown on a submission still OPEN/QUEUED/REJECTED (order or spec doc not yet accepted)` | Plain-string detail (not the {"error","message"} dict shape): {"detail":"submission not ready: order document not accepted, or already claimed"} |
| **I-N2** | NEG | Medium | Claim a nonexistent submission_id<br>_Any authenticated role_ | `POST /api/v1/procurement/submissions/{a made-up uuid}/order-breakdown` | Same 409 as I-N1, not a 404 — breakdown_state() treats a missing row the same as "not_started", so a bogus id and a genuinely-not-ready submission are indistinguishable from this response alone. Confirm and note as a gap. |
| **I-N3** | NEG | Low | Read the breakdown for a nonexistent submission_id<br>_Any authenticated role_ | `GET /api/v1/procurement/submissions/{a made-up uuid}/order-breakdown` | 200 {"status":"not_started","styles":[]} — no 404 for a bad id on the GET side either. |
| **I-B1** | BND | Medium | Re-POST while a claim is already processing<br>_Any authenticated role_ | `POST .../order-breakdown a second time on a submission already claimed (status CONSUMED, no styles yet)` | {"status":"already_processing"} — still 202, no second Celery task enqueued. |
| **I-B2** | BND | Low | Re-POST once styles already exist<br>_Any authenticated role_ | `POST .../order-breakdown once OrderStyle rows already exist for this submission` | {"status":"already_ready"} — still HTTP 202 even though nothing new happens, which reads oddly for an "already done" reply; worth a UX note if you're also reviewing the contract. |
| **I-L1** | LGC | Medium | Idempotent replay doesn't re-extract<br>_Any authenticated role_ | `If the breakdown task is somehow re-triggered on a submission that already has OrderStyle rows, it short-circuits` | Existing rows are replayed with warnings:["replayed"] instead of a fresh Gemini extraction — hard to trigger via HTTP alone; treat as a code-read confirmation rather than a black-box test. |
| **I-L2** | LGC | Medium | Spec/DXF auto-suggestion by name match<br>_Any authenticated role_ | `GET .../order-breakdown for a submission whose style name unambiguously matches one existing accepted spec document for that client` | That style's spec_match_status = "suggested" with spec_document_id populated. A style with 2+ ambiguous name matches instead has spec_match_status = "none" (falls back to manual). |

## J · Order Style Attachments

`POST /order-styles/{order_style_id}/attachments`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **J-P1** | POS | High | Confirm an explicit spec document<br>_Any authenticated role_ | `POST /api/v1/procurement/order-styles/{order_style_id}/attachments`<br>`Body: {"spec_document_id": "<uuid>"}` | 200 OrderStyleOut with spec_document_id set and spec_match_status = "confirmed". |
| **J-P2** | POS | High | Confirm an explicit DXF pattern<br>_Any authenticated role_ | `POST .../attachments — Body: {"pattern_reference_id": "<uuid>"}` | 200; dxf_match_status = "confirmed". |
| **J-P3** | POS | Medium | Promote an existing suggestion with no explicit id<br>_Any authenticated role_ | `POST .../attachments — Body: {} on a style whose spec_match_status is already "suggested"` | 200; the previously-suggested spec_document_id is promoted to spec_match_status = "confirmed" without you having to resend the id. |
| **J-N1** | NEG | High | Nonexistent order_style_id<br>_Any authenticated role_ | `POST /api/v1/procurement/order-styles/{a made-up uuid}/attachments` | {"error":"order_style_not_found","order_style_id":...} — dict shape, distinct from the plain-string 404 used by generate-bom (K-N1). |
| **J-N2** | NEG | Medium | clear_spec and spec_document_id together<br>_Any authenticated role_ | `POST .../attachments — Body: {"clear_spec": true, "spec_document_id": "<uuid>"}` | 422 Pydantic validation error — "clear_spec and spec_document_id are mutually exclusive". |
| **J-N3** | NEG | Medium | clear_dxf and pattern_reference_id together<br>_Any authenticated role_ | `POST .../attachments — Body: {"clear_dxf": true, "pattern_reference_id": "<uuid>"}` | 422, same mutual-exclusion validator for the DXF pair. |
| **J-B1** | BND | High | Explicit id that doesn't actually exist<br>_Any authenticated role_ | `POST .../attachments — Body: {"spec_document_id": "<a syntactically valid but nonexistent uuid>"}` | 200, accepted with no validation — despite the endpoint's own docstring claiming ids are "validated to exist (and to belong to the same client)", the service assigns it blindly. The failure only surfaces later, inside generate-bom, as spec_bytes_missing (see K-B1). Confirm and file as a finding against the docstring. |
| **J-L1** | LGC | Medium | clear_spec detaches the link<br>_Any authenticated role_ | `POST .../attachments — Body: {"clear_spec": true}` | 200; spec_document_id → null, spec_match_status → "none". |
| **J-L2** | LGC | Low | No client ownership check on attachment ids<br>_Any authenticated role_ | `Confirm a spec_document_id or pattern_reference_id that belongs to a different client than the order style` | Accepted with no error — there is no cross-client ownership check on this endpoint at all. Combine with J-B1 as one finding: neither existence nor ownership is validated here. |

## K · Generate BOM from Order Style

`POST /order-styles/{order_style_id}/generate-bom`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **K-P1** | POS | High | Generate a BOM for a style with no bom_id yet<br>_Any authenticated role_ | `POST /api/v1/procurement/order-styles/{order_style_id}/generate-bom (bom_id currently null; spec_match_status is "confirmed" or "none", never "suggested")` | 202 StyleBomAccepted — the exact literal "status" value on this fresh-queue branch isn't nailed down by source alone; capture it from a real response and note it here. |
| **K-P2** | POS | High | Generated BOM is reachable afterward<br>_Any authenticated role then Cutting Manager_ | `Wait for the Celery task to finish (order_style.bom_id becomes non-null)`<br>`GET /api/v1/procurement/boms/{that bom_id}` | 200 — the newly generated BOM view, status "draft". |
| **K-P3** | POS | Medium | Re-POST once a BOM already exists for the style<br>_Any authenticated role_ | `POST .../generate-bom again on a style whose bom_id is already set` | {"status":"already_generated","bom_id":...} — still 202, no duplicate BOM created. |
| **K-N1** | NEG | High | Nonexistent order_style_id<br>_Any authenticated role_ | `POST /api/v1/procurement/order-styles/{a made-up uuid}/generate-bom` | {"detail":"order style not found"} — plain-string shape, unlike J-N1's dict shape for the same logical "not found" case on a sibling endpoint. Worth flagging as an inconsistency. |
| **K-N2** | NEG | High | Unconfirmed spec suggestion blocks generation<br>_Any authenticated role_ | `POST .../generate-bom on a style whose spec_match_status is still "suggested" (never confirmed via J-P1/J-P3)` | {"detail":"spec suggestion is unconfirmed — confirm or clear it before generating"} — plain string, router-level pre-check. |
| **K-B1** | BND | High | Confirmed spec has no stored bytes<br>_Any authenticated role_ | `Confirm a spec_document_id that exists but has no retrievable bytes (or the garbage id from J-B1)`<br>`POST .../generate-bom` | The router's synchronous pre-checks pass, so you still get 202 — the spec_bytes_missing {"error":"spec_bytes_missing","message":"..."} error only happens inside the async Celery task and is NOT visible in this HTTP response. You cannot assert this failure from a single request; you need to poll the order style / BOM afterward or watch the realtime channel. |
| **K-L1** | LGC | Medium | DXF pattern pre-fills the generated BOM<br>_Any authenticated role_ | `Upload a current DXF pattern for the style's signature via POST /patterns (section M) first`<br>`POST .../generate-bom for that style` | The generated BOM's material lines carry DXF-derived quantities/consumption from the current PatternExtraction row. Without a prior pattern upload, the BOM falls back to bare line-seeds with no DXF-derived qty. Cross-check against C-L1 in the BOM section — this is the mechanism behind it. |
| **K-L2** | LGC | Low | Router vs. service error-shape inconsistency<br>_Any authenticated role_ | `Compare K-N1/K-N2 (plain "detail" string, from router.py pre-checks) against the equivalent errors raised inside generate_bom_for_style() in the service layer (dict {"error",...} shape)` | Confirmed inconsistency in the same logical error family across the sync/async paths of the same endpoint — file as an API-contract finding, not a test failure. |

## L · Notifications

`GET /notifications · GET /notifications/stream · POST /notifications/{id}/open`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **L-P1** | POS | High | List notifications<br>_Any authenticated role_ | `GET /api/v1/procurement/notifications` | {"notifications":[{id,type,channel,subject,body,entity_type,entity_id,status,scheduled_for,sent_at,opened_at}], "unread": N} — scoped to the caller, in-app channel only, newest-unread-first, hard limit 100. |
| **L-P2** | POS | Medium | Filter to unread only<br>_Any authenticated role_ | `GET /api/v1/procurement/notifications?unread_only=true` | 200; every returned item has opened_at: null. |
| **L-P3** | POS | High | Open a notification you own<br>_Any authenticated role_ | `POST /api/v1/procurement/notifications/{notification_id}/open (notification's recipient_user_id = caller)` | 200; opened_at populated, status → "opened". |
| **L-P4** | POS | Medium | Consume the live SSE stream<br>_Any authenticated role_ | `curl -N -H "Authorization: Bearer {token}" {base_url}/api/v1/procurement/notifications/stream` | text/event-stream response: one "data: {...}\n\n" frame per currently-unread notification immediately on connect (each flips PENDING→SENT), then ": keep-alive\n\n" comment frames roughly every 10s, indefinitely, until the client disconnects. The -N flag is required or curl buffers the whole thing. |
| **L-N1** | NEG | Medium | Open a nonexistent notification<br>_Any authenticated role_ | `POST /api/v1/procurement/notifications/{a made-up uuid}/open` | {"detail":"Notification not found."} |
| **L-N2** | NEG | High | Open someone else's notification<br>_Any two different authenticated roles_ | `As user B, POST /api/v1/procurement/notifications/{a notification whose recipient is user A}/open` | {"detail":"Not your notification."} — this is the one endpoint in the whole module with an actual per-row ownership check. |
| **L-B1** | BND | Low | Open the same notification twice<br>_Any authenticated role_ | `POST .../open`<br>`POST .../open again on the same id` | First call: 200, opened_at stamped. Second call: 200, unchanged — no error, no re-stamp; genuinely idempotent. |
| **L-B2** | BND | Medium | Second SSE connection doesn't replay already-sent items<br>_Any authenticated role_ | `Open one SSE stream, let it drain the unread backlog (flips them PENDING→SENT)`<br>`Open a second SSE stream for the same user without opening those notifications first` | The second connection only emits genuinely new notifications, not the already-SENT-but-unopened backlog. Meanwhile GET /notifications?unread_only=true still lists them, since it filters on opened_at not status — the two views can transiently disagree. Document this rather than treating it as a bug in your test. |
| **L-L1** | LGC | Low | SSE needs a streaming-capable client<br>_n/a_ | `Try GET .../notifications/stream with a plain Postman "Send" or a curl call without -N` | The request hangs / appears to buffer indefinitely rather than returning — this is a test-tooling requirement (use curl -N or Postman's streaming/SSE support), not an API defect. |
| **L-L2** | LGC | Medium | 2-hour escalation is currently broken end-to-end<br>_n/a_ | `Do not build a test expecting an unopened notification to trigger an escalation email after 2 hours` | NotificationService.escalate_overdue() referenced by the Celery Beat task doesn't exist (AttributeError), and run_escalations() itself references undefined names (NameError) due to commented-out setup lines. The feature described in the module's own docstring does not currently work — confirm the Beat task fails rather than expecting it to succeed. |

## M · Pattern Upload

`POST /patterns (multipart/form-data)`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **M-P1** | POS | High | Upload a DXF pattern<br>_Any authenticated role_ | `POST /api/v1/procurement/patterns?style_signature=SP12345 — multipart body: file = <valid .dxf>` | {"job_id": "<celery task id>", "channel": "pattern:SP12345"} — 202, async DXF parse enqueued. |
| **M-P2** | POS | Medium | Uploaded pattern feeds BOM generation<br>_Any authenticated role_ | `Wait for the parse task to finish`<br>`POST /order-styles/{a style with the same style_signature}/generate-bom (section K)` | The generated BOM's material lines reflect this pattern's fabric matrix / DXF yields — cross-check with K-L1. |
| **M-N1** | NEG | Medium | style_signature sent as a form field instead of a query param<br>_Any authenticated role_ | `POST /api/v1/procurement/patterns — multipart body: file=<.dxf> AND a form field named style_signature (no query string)` | Still 202, but style_signature is bound as a query parameter in the route signature (no Form(...) marker) — a form-field value is not picked up, so it resolves to None and "channel" comes back as the literal string "pattern:None". Confirm this exact behavior before relying on it. |
| **M-N2** | NEG | Medium | Non-DXF file accepted at the HTTP layer<br>_Any authenticated role_ | `POST /api/v1/procurement/patterns?style_signature=SP1 — multipart body: file = <a .txt or .jpg>` | 202, accepted with no content-type/extension check — it always names the stored object <uuid>.dxf regardless of actual content. Failure only happens later inside the async DXF parser, invisibly to this HTTP response. |
| **M-B1** | BND | Medium | No file-size cap on this endpoint<br>_Any authenticated role_ | `POST /api/v1/procurement/patterns with a very large file (tens of MB+)` | Accepted regardless of size at the HTTP/application layer — unlike the 25MB cap other upload endpoints in procurement/router.py enforce. Whatever limit you hit in practice is likely coming from infrastructure (reverse proxy / body-size limit), not this handler. |
| **M-B2** | BND | Medium | Duplicate re-upload for the same style_signature<br>_Any authenticated role_ | `POST /api/v1/procurement/patterns?style_signature=SP1 with identical file bytes twice in a row` | The repository code's own docstring claims a (style_signature, sha256) unique constraint turns a duplicate into a clean 409/no-op "caught upstream" — but no such catch exists anywhere in the module. Expect either an unhandled DB error surfacing as a generic Celery retry/failure, or a silent duplicate row, not the documented clean no-op. File the actual observed behavior as a finding. |
| **M-L1** | LGC | Medium | New upload supersedes the prior current pattern<br>_Any authenticated role_ | `Upload a DXF for style_signature SP1, wait for it to become current`<br>`Upload a second, different DXF for the same SP1 + same client`<br>`POST /order-styles/{a style with signature SP1}/generate-bom` | The second pattern becomes is_current=True and the first is superseded (is_current=False); the generated BOM should reflect the newer pattern's data, not the first upload's. |

## N · Admin — DXF Yields

`PUT /admin/dxf-yields/{species}`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **N-P1** | POS | Medium | Set a species' yield factor<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/dxf-yields/COWHIDE`<br>`Body: {"factor": 1.35, "note": "updated per supplier spec"}` | {"species":"COWHIDE","factor":1.35} |
| **N-N1** | NEG | Medium | Zero factor<br>_Direct Manager or MD_ | `PUT .../admin/dxf-yields/COWHIDE — Body: {"factor": 0}` | 422 — schema requires factor gt=0. |
| **N-N2** | NEG | Medium | Negative factor<br>_Direct Manager or MD_ | `PUT .../admin/dxf-yields/COWHIDE — Body: {"factor": -2}` | 422, same constraint. |
| **N-N3** | NEG | Medium | Wrong role<br>_Cutting Manager_ | `PUT /api/v1/procurement/admin/dxf-yields/COWHIDE — Body: {"factor": 1.2}` | 403 — cutting_manager isn't in the DM/MD gate for any /admin/* route. |
| **N-B1** | BND | Low | Unusual species string<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/dxf-yields/LEATHER-GRADE-A%2F2024 — Body: {"factor": 1.0}` | 200, accepted — species is a raw path string with no enum/regex constraint; any value silently creates a new row. Worth flagging as a possible data-quality gap. |
| **N-L1** | LGC | Low | Repeat PUT upserts, doesn't duplicate<br>_Direct Manager or MD_ | `PUT .../admin/dxf-yields/COWHIDE twice with different factor values` | Second call updates the existing row in place. There's no GET for this resource to confirm directly — verify indirectly via a BOM generation that uses this species' yield, or a direct DB check. |

## O · Admin — Fabric Roles

`POST /admin/fabric-roles`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **O-P1** | POS | Medium | Upsert a fabric role<br>_Direct Manager or MD_ | `POST /api/v1/procurement/admin/fabric-roles`<br>`Body: {"label":"Full-grain cowhide","role":"main_material","category":"leather","is_leather":true}` | 200, echoes the input with status:"confirmed". |
| **O-N1** | NEG | Medium | Wrong role<br>_Cutting Manager_ | `POST /api/v1/procurement/admin/fabric-roles` | 403. |
| **O-B1** | BND | Low | Arbitrary role/category strings<br>_Direct Manager or MD_ | `POST .../admin/fabric-roles — Body: {"label":"Test","role":"banana","category":"unknown"}` | 200, accepted — no server-side vocabulary check on "role"/"category". Flag as a data-integrity gap if that's unintended. |
| **O-L1** | LGC | Low | Repeat POST with the same label upserts<br>_Direct Manager or MD_ | `POST .../admin/fabric-roles twice with the same "label" but different "role"/"category"` | Second call updates the existing row keyed on "label" rather than creating a duplicate. |

## P · Admin — Cost Catalog

`GET /admin/cost-catalog · PUT /admin/cost-catalog/{garment_code}`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **P-P1** | POS | Medium | Read the cost catalog<br>_Direct Manager or MD_ | `GET /api/v1/procurement/admin/cost-catalog` | 200 {"<GARMENT_CODE>":[{category,name,uom,unit_price,qty_per_garment}], "_default":[...]} — served from a process-local cache, not a live DB read. |
| **P-P2** | POS | Medium | Replace a garment code's cost lines<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/cost-catalog/SH100`<br>`Body: {"lines":[{"category":"thread","name":"Poly thread","uom":"cone","unit_price":3.5,"qty_per_garment":2}]}` | {"garment_code":"SH100","lines":1} |
| **P-N1** | NEG | Medium | Negative unit price<br>_Direct Manager or MD_ | `PUT .../admin/cost-catalog/SH100 — a line with "unit_price": -1` | 422 — schema requires unit_price ge=0. |
| **P-N2** | NEG | Medium | Zero qty_per_garment<br>_Direct Manager or MD_ | `PUT .../admin/cost-catalog/SH100 — a line with "qty_per_garment": 0` | 422 — schema requires qty_per_garment gt=0 (default is 1, but an explicit 0 still fails). |
| **P-N3** | NEG | Medium | Wrong role on GET or PUT<br>_Cutting Manager_ | `GET /api/v1/procurement/admin/cost-catalog`<br>`PUT /api/v1/procurement/admin/cost-catalog/SH100` | 403 on both. |
| **P-B1** | BND | Medium | Empty lines array doesn't restore defaults<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/cost-catalog/SH100 — Body: {"lines": []}`<br>`GET /api/v1/procurement/admin/cost-catalog` | PUT → 200. Follow-up GET shows SH100's entry as an empty array, not a fallback to "_default" — clearing is not the same as resetting, confirm this explicitly. |
| **P-B2** | BND | Low | garment_code casing is normalized on write, not on read<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/cost-catalog/sh100 (lowercase)`<br>`GET /api/v1/procurement/admin/cost-catalog` | The server uppercases the code before storing (key becomes "SH100") — look for it under the uppercase key, not the lowercase one you sent. |
| **P-L1** | LGC | Low | Multi-worker cache staleness<br>_n/a_ | `If your test environment runs multiple API worker processes, PUT on one request then immediately GET on another` | The documented TTL-based cache refresh is not actually implemented — a PUT served by one worker may not be visible via GET on another until that worker restarts. If GET-after-PUT flakes intermittently, this is why, not a bug in your test sequencing. |

## Q · Admin — BOM Checks

`GET /admin/checks · PUT /admin/checks/{client_code}`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **Q-P1** | POS | Medium | Read check rules<br>_Direct Manager or MD_ | `GET /api/v1/procurement/admin/checks` | 200 [{"client_code":..., "checks":[...]}] — cached read. |
| **Q-P2** | POS | Medium | Replace a client's check rules<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/checks/CL01`<br>`Body: {"rules":[{"id":"r1","kind":"range","severity":"warn","field":"unit_price","range":[0,500]}]}` | 200, rules replaced for that client_code. |
| **Q-N1** | NEG | Medium | Wrong role<br>_Cutting Manager_ | `GET or PUT /api/v1/procurement/admin/checks[...]` | 403. |
| **Q-B1** | BND | High | Single-element range array crashes the handler<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/checks/CL01`<br>`Body: {"rules":[{"id":"r1","kind":"range","field":"unit_price","range":[5.0]}]}` | Schema validation passes (list[float] has no length constraint) but the handler unconditionally indexes range[0] and range[1] — a one-element array raises an unhandled IndexError. Expect a raw 500, not a clean 4xx. This is a confirmed defect — reproduce it and file a bug, don't just log it as informational. |
| **Q-B2** | BND | Low | range omitted entirely<br>_Direct Manager or MD_ | `PUT .../admin/checks/CL01 — a rule with no "range" key at all` | 200, accepted — both range bounds stored as null, no crash (the IndexError in Q-B1 only fires when "range" is truthy but short, not when it's absent). |
| **Q-L1** | LGC | Low | client_code casing is NOT normalized (asymmetric with cost-catalog)<br>_Direct Manager or MD_ | `PUT /api/v1/procurement/admin/checks/cl01 — Body: {"rules":[...]}`<br>`PUT /api/v1/procurement/admin/checks/CL01 — Body: {"rules":[...]} (different rules)` | These create/update two distinct rows rather than colliding — unlike cost-catalog's garment_code (P-B2), this endpoint does not uppercase client_code. Confirm and flag the inconsistency between the two admin endpoints. |

## R · Admin — POM Dictionary

`GET + POST /admin/pom-dictionary`

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **R-P1** | POS | Medium | Read the POM dictionary<br>_Direct Manager or MD_ | `GET /api/v1/procurement/admin/pom-dictionary` | 200 [{"source_term":...,"pom_code":...,"language":...,"garment_type_id":...,"weight":...}], ordered by source_term — a live DB read, unlike cost-catalog/checks which are cached. |
| **R-P2** | POS | Medium | Add a term mapping<br>_Direct Manager or MD_ | `POST /api/v1/procurement/admin/pom-dictionary`<br>`Body: {"source_term":"chest width","pom_code":"CHW","language":"en","garment_type_code":"SHIRT","weight":2}` | 200; note the request field is "garment_type_code" but the response field is "garment_type_id" — different names for related data, don't assume symmetry when asserting the response. |
| **R-N1** | NEG | Medium | Wrong role<br>_Cutting Manager_ | `GET or POST /api/v1/procurement/admin/pom-dictionary` | 403. |
| **R-B1** | BND | Low | Zero or negative weight<br>_Direct Manager or MD_ | `POST .../admin/pom-dictionary — Body: {"source_term":"x","pom_code":"X","weight":0}` | 200, accepted — no ge/le bound on "weight" in the schema. Flag as a possible missing sanity check, same spirit as B-B2 in the BOM section. |
| **R-L1** | LGC | High | Unrecognized garment_type_code fails silently<br>_Direct Manager or MD_ | `POST .../admin/pom-dictionary — Body: {"source_term":"y","pom_code":"Y","garment_type_code":"TYPO_CODE"}` | 200, accepted — a garment_type_code that doesn't resolve via a lookup does NOT error; it silently falls back to garment_type_id: null ("applies to any garment type"). An operator typo here is indistinguishable from a deliberate "any garment type" mapping. Confirmed gap — worth filing, since it silently broadens scope rather than failing loudly. |
| **R-L2** | LGC | Low | Repeat POST for the same key upserts<br>_Direct Manager or MD_ | `POST .../admin/pom-dictionary twice with the same (language, source_term, garment_type_code) but a different "weight"` | Second call updates the existing mapping in place (keyed on language + source_term + garment_type_id) rather than creating a duplicate. |

---

Generated from `docs/testcases/_cases/bom.cases.json` by `scripts/build_testcase_collections.js`. Edit the case file and re-run, so this sheet and `bom.postman_collection.json` stay in step.
