# Supplier PO — API Test Cases

> Stage 5 — supplier purchase orders · module `app/modules/supplier_po` · 114 cases

Request-level test cases for the Stage-5 supplier-PO API: supplier master + CRUD, PO generation from a BOM, the cross-check state machine, send/acknowledge, the unauthenticated tracking + webhook routes, and the production board. Every row is a literal HTTP request with the exact status code and response fields to check.

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
| Positive (POS) | 35 |
| Negative (NEG) | 38 |
| Boundary (BND) | 20 |
| Business Logic (LGC) | 21 |
| **Total** | **114** |

## Endpoints covered

| Group | Endpoints | Role gate | Cases |
|---|---|---|---|
| A · Supplier master import | `POST /suppliers/import/preview · POST /suppliers/import/commit` | DM / MD (mutating) | 9 |
| B · Supplier reads | `GET /suppliers · GET /suppliers/{supplier_id}` | DM / MD / Viewer | 10 |
| C · Supplier create / edit / deactivate | `POST /suppliers · PATCH /suppliers/{id} · DELETE /suppliers/{id} · POST /suppliers/{id}/reactivate` | DM / MD (create, edit) · MD (delete, reactivate) | 14 |
| D · Generate POs from a BOM | `POST /boms/{bom_id}/generate-pos` | DM / MD | 9 |
| E · PO reads | `GET /pos · GET /pos/{po_id}` | DM / MD / Viewer | 7 |
| F · Edit a PO | `PATCH /pos/{po_id}/items` | DM / MD | 17 |
| G · Cross-check state machine | `POST /pos/{po_id}/submit · /approve · /reject · /cancel` | DM / MD (submit, cancel) · Cutting Manager / MD / DM / HR (approve, reject — narrowed by the service) | 18 |
| H · Send and acknowledge | `POST /pos/{po_id}/send · POST /pos/{po_id}/acknowledge` | DM / MD | 8 |
| I · Open tracking (no auth) | `GET /t/o/{token}.gif · GET /t/c/{token}` | NONE — deliberately unauthenticated | 6 |
| J · Provider webhooks (no auth) | `POST /webhooks/ses · POST /webhooks/twilio/whatsapp · POST /webhooks/twilio/voice` | NONE — deliberately unauthenticated | 8 |
| K · Production board | `GET /production-tracking · POST /production-tracking/{tracking_id}/transition` | DM / MD / Viewer (read) · MD / DM / Cutting Manager (transition) | 8 |

## Verify before relying on

- **MD and DM bypass every `require_roles` gate** (`SUPERUSER_ROLES` in `app/modules/users/deps.py`). So a DM passes the MD-only supplier delete/reactivate gate. Any case that says "MD-only" is MD-only *in intent*; verify what the build actually does.
- **Approver routing is enforced in the service, not the router.** `_APPROVERS` admits Cutting Manager / MD / DM / HR, then `PoService._assert_router_role` narrows: leather POs → {Cutting Manager, MD}, accessory/service POs → {MD, DM, HR}. MD short-circuits everything; **DM does not** — a DM approving a leather PO is expected to get 403 `wrong_approver` despite bypassing the router gate.
- **Two error shapes.** Router/`_load` failures are plain `{"detail": "..."}` strings (404s, role 403s, 413, the tracking 400). Service failures are dicts: `{"error": "...", "message"?: "...", ...}`. Assert both shapes.
- **The tracking (`/t/o/…`, `/t/c/…`) and webhook (`/webhooks/…`) routes have no auth dependency at all** — by design, since a supplier's mail client, Twilio and SES call them. Provider signature verification is still marked TODO in the router, so today anyone who can reach those URLs can forge a delivery/bounce event or a supplier acknowledgement. J-L1 covers this.
- A malformed (non-UUID) path parameter returns a generic **422** from FastAPI's own path parsing, before any handler runs — distinct from the 404 raised for a well-formed but nonexistent id.
- `PoBulkPatch.base_revision` is `ge=1`; always take the live `revision` from a fresh `GET /pos/{id}` rather than sending 0.
- PO numbers are minted on the **first** send only, from a financial-year series; `issue_date` and `tracking_token` are set at the same moment.

## A · Supplier master import

`POST /suppliers/import/preview · POST /suppliers/import/commit` · **Role gate:** DM / MD (mutating)

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **A-P1** | POS | High | Preview a supplier workbook<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers/import/preview`<br>`multipart body: file = <the supplier contact + provision workbook>` | 200 with the preview block: contact_rows, provision_rows, suppliers, suppliers_with_phone, suppliers_with_email, history_rows, warnings[], sample_history[] (capped at 25). Nothing is persisted — a follow-up GET /suppliers is unchanged. |
| **A-P2** | POS | High | Commit the same workbook<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers/import/commit`<br>`multipart body: file = <the same workbook>` | 200 with the same preview block plus committed, history_rows and deactivated. Suppliers are upserted, supply history is rebuilt wholesale and an AuditLog row with action SUPPLIER_IMPORT is written. |
| **A-N1** | NEG | High | Wrong role imports<br>_Cutting Manager_ | `POST /api/v1/procurement/suppliers/import/commit — multipart body: file = <workbook>` | {"detail":"Role 'cutting_manager' is not permitted for this action"} — import is DM/MD only. |
| **A-N2** | NEG | Medium | No file part<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers/import/preview with an empty body (no multipart file field)` | 422 — FastAPI's File(...) is required, so the request never reaches the service. |
| **A-B1** | BND | Medium | Upload over the size cap<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers/import/preview — file = <a workbook larger than MAX_UPLOAD_MB (25 MB default)>` | 413 {"detail":"File exceeds the 25 MB limit."} — raised by _read_capped while streaming, before the parser runs. |
| **A-B2** | BND | Medium | Empty or unparseable file<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers/import/preview — file = <0 bytes, or a .txt renamed .xlsx>` | Expect a clean 4xx or a preview with warnings — NOT a 500. The router has no content-type check, so whatever the parser does is the whole defence; record the actual behaviour. |
| **A-L1** | LGC | High | Re-import never blanks a known contact<br>_Direct Manager_ | `PATCH a supplier to add an email/phone by hand`<br>`POST /api/v1/procurement/suppliers/import/commit with a workbook where that supplier has NO email/phone`<br>`GET /api/v1/procurement/suppliers/{supplier_id}` | The hand-entered email/phone survives — commit uses `existing.phone or s.phone`, so an import can fill a blank but never overwrite a known value with nothing. |
| **A-L2** | LGC | High | Absent suppliers are soft-deactivated, never deleted<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers/import/commit with a workbook that omits a previously imported supplier`<br>`GET /api/v1/procurement/suppliers?active=false` | The response's `deactivated` count includes that supplier and it still exists with is_active:false. No row is ever hard-deleted. |
| **A-L3** | LGC | Medium | Supply history is rebuilt wholesale<br>_Direct Manager_ | `Commit workbook v1, note a supplier's history rows via GET /suppliers/{id}`<br>`Commit workbook v2 in which that supplier has fewer article rows`<br>`GET /api/v1/procurement/suppliers/{supplier_id}` | History is cleared and re-inserted from the new file, so rows only present in v1 are gone — history is a mirror of the latest import, not an accumulation. |

## B · Supplier reads

`GET /suppliers · GET /suppliers/{supplier_id}` · **Role gate:** DM / MD / Viewer

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **B-P1** | POS | High | List suppliers<br>_Direct Manager_ | `GET /api/v1/procurement/suppliers` | 200 {"suppliers":[{id,name,phone,email,service,supplier_type,is_active,…}], "count": N}. |
| **B-P2** | POS | Medium | Filter the list<br>_Direct Manager_ | `GET /api/v1/procurement/suppliers?q=leather&service=tanning&active=true&limit=25&offset=0` | 200; every row matches the filters. `count` is the length of THIS page, not the total matching set — don't paginate off it. |
| **B-P3** | POS | High | Read one supplier<br>_Direct Manager_ | `GET /api/v1/procurement/suppliers/{supplier_id}` | 200 — the supplier block enriched with its supply `history` and `open_pos`. |
| **B-P4** | POS | Medium | Office Viewer can read<br>_Office Viewer (9000000004)_ | `GET /api/v1/procurement/suppliers` | 200 — VIEWER is inside the _VIEW read gate, unlike every mutating route in this module. |
| **B-N1** | NEG | Medium | Unknown supplier id<br>_Direct Manager_ | `GET /api/v1/procurement/suppliers/{a well-formed but nonexistent uuid}` | 404 {"detail":"Supplier not found."} |
| **B-N2** | NEG | Medium | Malformed supplier id<br>_Direct Manager_ | `GET /api/v1/procurement/suppliers/not-a-uuid` | 422 from FastAPI's path parsing — the handler never runs, so the shape differs from B-N1's 404. |
| **B-N3** | NEG | High | Role outside the read gate<br>_Cutting Manager_ | `GET /api/v1/procurement/suppliers` | 403 role-not-permitted — Cutting Manager can approve leather POs but cannot browse the supplier master. |
| **B-B1** | BND | Medium | limit=0 and a far offset<br>_Direct Manager_ | `GET /api/v1/procurement/suppliers?limit=0`<br>`GET /api/v1/procurement/suppliers?offset=999999` | 200 with an empty list and count:0 in both cases — limit/offset carry no bounds or validation at all. |
| **B-B2** | BND | Low | Unbounded limit<br>_Direct Manager_ | `GET /api/v1/procurement/suppliers?limit=100000` | 200, accepted — there is no server-side ceiling on `limit`. Flag as a possible DoS/perf gap on a large master. |
| **B-L1** | LGC | Medium | Deactivated suppliers appear unless filtered out<br>_Direct Manager_ | `Deactivate a supplier (DELETE /suppliers/{id})`<br>`GET /api/v1/procurement/suppliers   (no 'active' param)` | The deactivated supplier is still listed — `active` defaults to None, which applies no filter. A picker UI must pass active=true explicitly or it will offer dead vendors. |

## C · Supplier create / edit / deactivate

`POST /suppliers · PATCH /suppliers/{id} · DELETE /suppliers/{id} · POST /suppliers/{id}/reactivate` · **Role gate:** DM / MD (create, edit) · MD (delete, reactivate)

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **C-P1** | POS | High | Create with just a name<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers`<br>`Body: {"name": "New Vendor Pvt Ltd"}` | 201 with the supplier block. Defaults applied: currency INR, payment_terms_days 60, lead_time_days 10, is_active true, email_status "unknown" (no email given). |
| **C-P2** | POS | Medium | Create with contact details<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers`<br>`Body: {"name":"Contactable Vendor","phone":"9876543210","email":"sales@vendor.test","gstin":"33ABCDE1234F1Z5","supplier_type":"leather"}` | 201; email_status "valid" because an email was supplied, whatsapp_phone falls back to `phone`, and state_code is derived from the GSTIN's first two digits (33). |
| **C-P3** | POS | High | Add an email to a contactless vendor<br>_Direct Manager_ | `PATCH /api/v1/procurement/suppliers/{supplier_id}`<br>`Body: {"email":"new.contact@vendor.test"}` | 200; email_status flips to "valid", which is what unblocks the §5 send path for a vendor previously marked invalid by a bounce. |
| **C-P4** | POS | Medium | Fix a GSTIN<br>_Direct Manager_ | `PATCH /api/v1/procurement/suppliers/{supplier_id} — Body: {"gstin":"27ABCDE1234F1Z5"}` | 200; state_code is re-derived (27), which flips later PO totals between CGST+SGST and IGST. |
| **C-P5** | POS | Medium | Deactivate then reactivate<br>_Managing Director_ | `DELETE /api/v1/procurement/suppliers/{supplier_id}`<br>`POST /api/v1/procurement/suppliers/{supplier_id}/reactivate` | {"id":…,"is_active":false} then {"id":…,"is_active":true}. DELETE is a soft deactivate — the row, its history and its POs all survive. |
| **C-N1** | NEG | High | Duplicate supplier name<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers — Body: {"name":"<the exact name of an existing supplier>"}` | 409 {"error":"duplicate_name","name":"…"} |
| **C-N2** | NEG | High | Empty name<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers — Body: {"name":""}`<br>`then POST again with Body: {"name":"   "}` | Both 422, but from different layers: "" fails Pydantic's min_length=1 (generic shape); "   " passes Pydantic and is caught by the service as {"error":"name_required"}. |
| **C-N3** | NEG | Medium | Wrong role creates<br>_Cutting Manager_ | `POST /api/v1/procurement/suppliers — Body: {"name":"Should Not Exist"}` | 403 role-not-permitted. |
| **C-N4** | NEG | Medium | Patch an unknown supplier<br>_Direct Manager_ | `PATCH /api/v1/procurement/suppliers/{a made-up uuid} — Body: {"phone":"9999999999"}` | 404 {"detail":"Supplier not found."} |
| **C-B1** | BND | Medium | Empty patch body<br>_Direct Manager_ | `PATCH /api/v1/procurement/suppliers/{supplier_id} — Body: {}` | 200 and nothing changes — the router drops None fields with exclude_none before the service sees them. |
| **C-B2** | BND | Low | Nonsense numeric terms<br>_Direct Manager_ | `POST /api/v1/procurement/suppliers — Body: {"name":"Odd Terms Vendor","payment_terms_days":-30,"lead_time_days":0}` | 201, accepted — neither field has a ge/le bound in SupplierCreate. Note that 0 is falsy, so lead_time_days:0 is replaced by the default 10 while -30 is stored as-is. Flag both as validation gaps. |
| **C-L1** | LGC | High | A DM can delete despite the MD-only gate<br>_Direct Manager_ | `DELETE /api/v1/procurement/suppliers/{supplier_id} as a DIRECT MANAGER (the route is gated require_roles(MANAGING_DIRECTOR))` | 200, not 403 — DIRECT_MANAGER is in SUPERUSER_ROLES and bypasses every require_roles gate. The MD-only intent of this route is not enforced today; confirm and report. |
| **C-L2** | LGC | Medium | PATCH can never clear a field<br>_Direct Manager_ | `PATCH /api/v1/procurement/suppliers/{supplier_id} — Body: {"email": null}`<br>`GET /api/v1/procurement/suppliers/{supplier_id}` | 200, but the email is unchanged — nulls are stripped by exclude_none AND skipped again in the service loop. There is no way to remove a wrong email through this API; confirm and flag. |
| **C-L3** | LGC | Medium | Duplicate detection is exact-name only<br>_Direct Manager_ | `Create a supplier named "Acme Leathers"`<br>`POST /api/v1/procurement/suppliers — Body: {"name":"acme leathers"}` | Verify whether the second call 409s or creates a second row. The duplicate guard keys on the stored name; if it is case/whitespace sensitive, near-duplicate vendors can be created and will then compete in PO matching. |

## D · Generate POs from a BOM

`POST /boms/{bom_id}/generate-pos` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **D-P1** | POS | High | Generate from a checked BOM with shortfalls<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/generate-pos (BOM approved/locked, inventory check run, at least one short line)` | 200 {"bom_id":…,"purchase_orders":[…]} — one draft PO per matched supplier, carrying only the shortfall lines. |
| **D-P2** | POS | High | Re-generate is idempotent<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/generate-pos a second time` | 200 {"already_generated":true,"purchase_orders":[…]} — the existing POs are returned, no duplicates are created. |
| **D-P3** | POS | Medium | Nothing is short<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/generate-pos on a BOM whose every checked line came back sufficient` | 200 with purchase_orders: [] — a clean "nothing to buy" answer, not an error. |
| **D-N1** | NEG | High | No inventory check yet<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/generate-pos on a BOM that has never been inventory-checked` | 409 {"error":"no_inventory_check","message":"Run the inventory check before generating supplier POs."} — the check is a hard precondition. |
| **D-N2** | NEG | Medium | Unknown BOM<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{a made-up uuid}/generate-pos` | 404 {"detail":"BOM not found."} |
| **D-N3** | NEG | Medium | Wrong role generates<br>_Office Viewer (9000000004)_ | `POST /api/v1/procurement/boms/{bom_id}/generate-pos` | 403 — a viewer can read POs but cannot raise them. |
| **D-B1** | BND | Medium | Generate after cancelling every PO<br>_Direct Manager_ | `Cancel every PO previously generated for the BOM (POST /pos/{po_id}/cancel)`<br>`POST /api/v1/procurement/boms/{bom_id}/generate-pos` | 200 with FRESH purchase orders, not already_generated — cancelled POs are excluded from the "already generated" test, which is the documented re-issue path for a sent PO. |
| **D-L1** | LGC | High | Unmatched lines still produce a PO<br>_Direct Manager_ | `Generate for a BOM containing a material no supplier history matches`<br>`GET /api/v1/procurement/pos?needs_supplier=true` | The line lands on a PO with needs_supplier:true and no supplier_id. It is visible in the needs-supplier worklist and will be refused at submit (G-N1) until a supplier is assigned via PATCH. |
| **D-L2** | LGC | Medium | Only shortfall lines are bought<br>_Direct Manager_ | `Compare the inventory check's lines (GET /boms/{bom_id}/inventory-check) with the generated POs' lines` | Only lines whose status is not `sufficient` AND whose shortfall_qty > 0 appear on a PO; the PO quantity is the shortfall, not the full BOM requirement. |

## E · PO reads

`GET /pos · GET /pos/{po_id}` · **Role gate:** DM / MD / Viewer

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **E-P1** | POS | High | List POs<br>_Direct Manager_ | `GET /api/v1/procurement/pos` | 200 with the PO list — each row carrying status, supplier, needs_supplier, revision and totals. |
| **E-P2** | POS | High | Filter the list<br>_Direct Manager_ | `GET /api/v1/procurement/pos?status=draft`<br>`GET /api/v1/procurement/pos?needs_supplier=true`<br>`GET /api/v1/procurement/pos?bom_id={bom_id}` | 200 each; every row matches the filter. needs_supplier=true is the assignment worklist. |
| **E-P3** | POS | High | Read one PO<br>_Direct Manager_ | `GET /api/v1/procurement/pos/{po_id}` | 200 — full PO view: items[], revision, subtotal, cgst/sgst/igst, gst_mode, round_off, total, status, and the tracking/acknowledgement fields once sent. |
| **E-N1** | NEG | Medium | Unknown PO id<br>_Direct Manager_ | `GET /api/v1/procurement/pos/{a made-up uuid}` | 404 {"detail":"Purchase order not found."} |
| **E-N2** | NEG | Medium | Wrong role reads<br>_Cutting Manager_ | `GET /api/v1/procurement/pos` | 403 — note the asymmetry: a Cutting Manager may be the required APPROVER of a leather PO (G-P2) yet cannot list POs through this endpoint. Worth flagging as a workflow gap. |
| **E-B1** | BND | Medium | Nonsense status filter<br>_Direct Manager_ | `GET /api/v1/procurement/pos?status=banana` | 200 with an empty list — `status` is a free string with no enum validation, so a typo yields "no results" rather than a 422. Flag it. |
| **E-L1** | LGC | Medium | The approver cannot browse what they must approve<br>_Cutting Manager then MD_ | `As Cutting Manager: GET /api/v1/procurement/pos → 403`<br>`As Cutting Manager: POST /api/v1/procurement/pos/{po_id}/approve on a leather PO → 200` | Confirms E-N2: the approval notification link is the only way a Cutting Manager can reach a PO, because the read gate excludes them. File as a finding. |

## F · Edit a PO

`PATCH /pos/{po_id}/items` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **F-P1** | POS | High | Edit qty and unit price<br>_Direct Manager_ | `GET /api/v1/procurement/pos/{po_id} — note 'revision'`<br>`PATCH /api/v1/procurement/pos/{po_id}/items`<br>`Body: {"base_revision": 1, "item_edits":[{"po_item_id":"<uuid>","field":"qty","value":25},{"po_item_id":"<uuid>","field":"unit_price","value":180.5}]}` | 200 {"revision": base_revision+1, "recomputed": {…}} — line amounts, subtotal, tax and total all recomputed in the same response. |
| **F-P2** | POS | Medium | Add a line<br>_Direct Manager_ | `PATCH /api/v1/procurement/pos/{po_id}/items`<br>`Body: {"base_revision": 1, "add_items":[{"description":"Extra buckle","uom":"pcs","qty":100,"unit_price":12}]}` | 200; the new line appears in recomputed.items with the next line number and is included in the totals. |
| **F-P3** | POS | Medium | Remove a line<br>_Direct Manager_ | `PATCH /api/v1/procurement/pos/{po_id}/items — Body: {"base_revision": 1, "remove_item_ids":["<po_item uuid>"]}` | 200; the line is gone from recomputed.items and the totals drop accordingly. |
| **F-P4** | POS | High | Assign the missing supplier<br>_Direct Manager_ | `PATCH /api/v1/procurement/pos/{po_id}/items on a needs_supplier PO`<br>`Body: {"base_revision": 1, "po_edits": {"supplier_id": "<active supplier uuid>"}}` | 200; needs_supplier flips to false, match_method becomes "manual", and no_contact_channel reflects whether that supplier has an email or phone. |
| **F-N1** | NEG | High | Stale revision<br>_Direct Manager (two sessions)_ | `Both sessions GET the PO and note revision N`<br>`Session A: PATCH with base_revision N → succeeds`<br>`Session B: PATCH with base_revision N, sent after A` | A: 200. B: 409 {"error":"stale_revision","current_revision":N+1} — enforced twice: a pre-check and an atomic claim_po_revision at commit, so a true race cannot slip through. |
| **F-N2** | NEG | High | Edit a sent PO<br>_Direct Manager_ | `PATCH /api/v1/procurement/pos/{po_id}/items on a PO whose status is sent / responded / confirmed / escalated` | 409 {"error":"po_locked","message":"A sent PO is frozen — cancel + re-issue."} |
| **F-N3** | NEG | Medium | Unknown line id<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision":1,"item_edits":[{"po_item_id":"<a uuid not on this PO>","field":"qty","value":5}]}` | 422 {"error":"unknown_po_item","po_item_id":"…"} |
| **F-N4** | NEG | Medium | Unsupported field<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision":1,"item_edits":[{"po_item_id":"<uuid>","field":"amount","value":999}]}` | 422 {"error":"unsupported_field","field":"amount"} — only description, color, uom, qty and unit_price are editable; `amount` is derived. |
| **F-N5** | NEG | Medium | Non-numeric quantity<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision":1,"item_edits":[{"po_item_id":"<uuid>","field":"qty","value":"abc"}]}` | 422 {"error":"invalid_value","field":"qty"} — the service's own Decimal coercion, not Pydantic (PoItemEdit.value is typed `object`). |
| **F-N6** | NEG | High | Assign an unknown or inactive supplier<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision":1,"po_edits":{"supplier_id":"<a deactivated supplier's uuid>"}}` | 422 {"error":"unknown_supplier"} — a deactivated supplier is refused exactly like a nonexistent one, so you cannot route a PO to a dead vendor. |
| **F-N7** | NEG | Medium | base_revision 0<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision": 0, "item_edits":[]}` | 422 — PoBulkPatch enforces ge=1. Always copy the live revision from a fresh GET. |
| **F-B1** | BND | Medium | A patch that changes nothing<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision": 1}   (no edits, no adds, no removes)` | 200 and the revision still increments — an empty save burns a revision and will stale-out anyone else holding the old number. Confirm and note. |
| **F-B2** | BND | Medium | Remove a line that isn't on the PO<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision":1,"remove_item_ids":["<a uuid not on this PO>"]}` | 200, silently ignored — unlike item_edits (F-N3), removals of unknown ids raise nothing. Asymmetric; flag it. |
| **F-B3** | BND | Medium | Negative quantity or price<br>_Direct Manager_ | `PATCH …/items — Body: {"base_revision":1,"item_edits":[{"po_item_id":"<uuid>","field":"qty","value":-5}]}` | 200, accepted — the service only coerces to Decimal, it never checks the sign, so a negative line amount flows into the PO total and the PDF. Flag as a money-path validation gap. |
| **F-L1** | LGC | High | Changing supplier resets an approval<br>_Direct Manager_ | `Take a PO to status approved`<br>`PATCH …/items — Body: {"base_revision":<n>,"po_edits":{"supplier_id":"<a different supplier>"}}`<br>`GET /api/v1/procurement/pos/{po_id}` | 200; status is back to "draft" and approved_by/approved_at are cleared — a new supplier may route to a different approver class, so the old approval cannot stand. |
| **F-L2** | LGC | High | GST mode follows the supplier's state<br>_Direct Manager_ | `Assign a supplier whose GSTIN state matches PO_BUYER_STATE_CODE, PATCH any line, read recomputed`<br>`Assign a supplier in a different state, PATCH again, read recomputed` | Intra-state: cgst and sgst populated, igst zero. Inter-state: igst populated, cgst/sgst zero. gst_mode names which was applied and round_off balances the total. |
| **F-L3** | LGC | Medium | Edits are audited before and after<br>_Direct Manager_ | `PATCH …/items with a price change`<br>`Inspect the AuditLog rows (DB or an audit endpoint) for action PO_EDIT` | One PO_EDIT row per patch carrying both the before and after snapshots — the money trail for a changed PO. |

## G · Cross-check state machine

`POST /pos/{po_id}/submit · /approve · /reject · /cancel` · **Role gate:** DM / MD (submit, cancel) · Cutting Manager / MD / DM / HR (approve, reject — narrowed by the service)

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **G-P1** | POS | High | Submit a draft for cross-check<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/submit (status draft, supplier assigned, at least one line)` | 200 {"po_id":…,"status":"pending_approval","notifications_created":N} — N in-app notifications, one per user in the routed approver class. |
| **G-P2** | POS | High | Cutting Manager approves a leather PO<br>_Cutting Manager_ | `POST /api/v1/procurement/pos/{po_id}/approve on a PO whose supplier_type is leather` | 200 {"po_id":…,"status":"approved"} — leather routes to {Cutting Manager, MD}. |
| **G-P3** | POS | High | HR approves an accessory PO<br>_HR / Accounts (9000000005)_ | `POST /api/v1/procurement/pos/{po_id}/approve on a PO whose supplier_type is accessory or service` | 200 approved — accessory/service routes to {MD, DM, HR}. |
| **G-P4** | POS | High | Reject with a reason<br>_Managing Director_ | `POST /api/v1/procurement/pos/{po_id}/reject — Body: {"reason":"Rate is above the last purchase price"}` | 200 {"po_id":…,"status":"rejected","rejection_reason":"Rate is above the last purchase price"} — the reason is stored trimmed. |
| **G-P5** | POS | Medium | Cancel a draft<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/cancel on a pre-send PO` | 200 {"po_id":…,"status":"cancelled"}; the BOM can then be re-generated (D-B1). |
| **G-N1** | NEG | High | Submit without a supplier<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/submit on a needs_supplier PO` | 409 {"error":"needs_supplier","message":"Assign a supplier before submitting."} |
| **G-N2** | NEG | Medium | Submit an empty PO<br>_Direct Manager_ | `Remove every line via PATCH …/items`<br>`POST /api/v1/procurement/pos/{po_id}/submit` | 409 {"error":"empty_po"} |
| **G-N3** | NEG | Medium | Submit from the wrong state<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/submit on a PO already approved / sent / cancelled` | 409 {"error":"not_submittable","current_status":"…"} — only draft and rejected are submittable. |
| **G-N4** | NEG | High | Approve something not awaiting approval<br>_Managing Director_ | `POST /api/v1/procurement/pos/{po_id}/approve on a draft PO` | 409 {"error":"not_pending_approval","current_status":"draft"} — note this is stricter than the BOM's approve, which never re-checks status. |
| **G-N5** | NEG | High | Wrong approver class<br>_HR / Accounts_ | `POST /api/v1/procurement/pos/{po_id}/approve on a LEATHER PO as HR` | 403 {"error":"wrong_approver","message":"This PO routes to ['cutting_manager', 'managing_director']."} — HR passes the router gate and is then refused by the service's routing check. |
| **G-N6** | NEG | High | Reject with no real reason<br>_Managing Director_ | `POST /api/v1/procurement/pos/{po_id}/reject — Body: {"reason":""}`<br>`then Body: {"reason":"   "}` | "" → 422 from Pydantic (min_length=1). "   " → 422 {"error":"reason_required"} from the service. Two shapes, same intent. |
| **G-N7** | NEG | High | Cancel a sent PO<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/cancel on a PO already sent` | 409 {"error":"po_locked","message":"A sent PO cannot be cancelled in place."} — a supplier already has this document; it must be superseded, not silently voided. |
| **G-B1** | BND | Medium | MD approves anything<br>_Managing Director_ | `POST /api/v1/procurement/pos/{po_id}/approve on a leather PO as MD`<br>`and again on an accessory PO` | 200 both times — _assert_router_role returns immediately for MANAGING_DIRECTOR, before any routing is computed. |
| **G-B2** | BND | Medium | Double-click approve<br>_Managing Director_ | `POST …/approve`<br>`POST …/approve again immediately` | First 200, second 409 not_pending_approval — the repeat fails safely rather than re-stamping approved_at. |
| **G-B3** | BND | Low | Very long rejection reason<br>_Managing Director_ | `POST …/reject — Body: {"reason":"<several paragraphs>"}`<br>`GET /api/v1/procurement/pos/{po_id}` | 200; the GET returns the full text untruncated. |
| **G-L1** | LGC | High | Reject → fix → resubmit is a closed loop<br>_MD then DM_ | `POST …/reject with a reason → rejected`<br>`PATCH …/items to correct the rate`<br>`POST …/submit again` | The resubmit succeeds — `rejected` is explicitly submittable, so one rejection does not strand the PO. Confirm the notifications fire again for the approver class. |
| **G-L2** | LGC | High | A DM cannot approve a leather PO<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/approve on a LEATHER PO as a DIRECT MANAGER` | 403 wrong_approver — DM bypasses the router's role gate (SUPERUSER_ROLES) but _assert_router_role only short-circuits for MD, and DM is not in _LEATHER_APPROVERS. This is the one MD-only rule in the module the DM bypass does NOT defeat; confirm it holds. |
| **G-L3** | LGC | Medium | Only the routed class is notified<br>_Direct Manager_ | `Submit a leather PO, note notifications_created`<br>`As Cutting Manager: GET /api/v1/procurement/notifications`<br>`As HR: GET /api/v1/procurement/notifications` | The Cutting Manager (and MD) receive a PO_AWAITING_APPROVAL notification; HR does not. For an accessory PO the reverse holds. Recipients are de-duplicated and the notification's scheduled_for is the escalation deadline. |

## H · Send and acknowledge

`POST /pos/{po_id}/send · POST /pos/{po_id}/acknowledge` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **H-P1** | POS | High | Send an approved PO<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/send (status approved)`<br>`GET /api/v1/procurement/pos/{po_id}` | 200; the PO now has a po_number from the financial-year series, issue_date = today, a tracking_token, a stored PDF, and status "sent". The escalation clock starts here. |
| **H-P2** | POS | High | Record a manual acknowledgement<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/acknowledge`<br>`Body: {"channel":"call","confirmed_qty":100,"notes":"Supplier confirmed by phone"}` | 200 with the PO view; acknowledged_at is stamped and the escalation ladder stops immediately. |
| **H-N1** | NEG | High | Send before approval<br>_Direct Manager_ | `POST /api/v1/procurement/pos/{po_id}/send on a draft or pending_approval PO` | 409 {"error":"not_approved","current_status":"…"} |
| **H-N2** | NEG | Medium | Wrong role sends<br>_Office Viewer_ | `POST /api/v1/procurement/pos/{po_id}/send` | 403 role-not-permitted. |
| **H-B1** | BND | Medium | Send twice<br>_Direct Manager_ | `POST …/send → 200 (status becomes sent)`<br>`POST …/send again` | 409 not_approved with current_status "sent" — re-sending is not an idempotent replay here (unlike the BOM export); to contact the supplier again you go through the escalation ladder. |
| **H-B2** | BND | Medium | Acknowledge twice<br>_Direct Manager_ | `POST …/acknowledge`<br>`POST …/acknowledge again with different notes` | Both 200, but the second is a no-op returning the existing view — _acknowledge returns early once acknowledged_at is set, so the first acknowledgement wins and the later notes are silently dropped. Confirm and note. |
| **H-L1** | LGC | Medium | The PO number is minted once<br>_Direct Manager_ | `Send a PO, note po_number`<br>`Trigger any later state change (acknowledge, escalate) and GET the PO again` | po_number and issue_date never change after the first send — the number is the supplier-facing identity of the document. |
| **H-L2** | LGC | High | Any channel stops the ladder<br>_Direct Manager_ | `Send a PO`<br>`Acknowledge it through ANY route: this endpoint, the WhatsApp webhook, or the voice webhook (J-P2 / J-P3)` | acknowledged_at is set once and the escalation ladder (email → WhatsApp → call) stops, regardless of which channel got there first. |

## I · Open tracking (no auth)

`GET /t/o/{token}.gif · GET /t/c/{token}` · **Role gate:** NONE — deliberately unauthenticated

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **I-P1** | POS | High | The open pixel records an open<br>_No authentication at all_ | `GET /api/v1/procurement/t/o/{tracking_token}.gif   with NO Authorization header` | 200 with content-type image/gif (the 1×1 pixel) and an `open` tracking event recorded against the PO. Sending a token belonging to a sent PO should also move the response status toward opened. |
| **I-P2** | POS | High | A signed click redirects<br>_No authentication at all_ | `GET /api/v1/procurement/t/c/{tracking_token}?u=<wrapped target>&s=<valid signature>`<br>`(disable redirect-following in the client so you can see the 302 itself)` | 302 with a Location header pointing at the unwrapped target URL, and a `click` event recorded. |
| **I-N1** | NEG | High | Click with a bad signature<br>_No authentication at all_ | `GET /api/v1/procurement/t/c/{tracking_token}?u=<target>&s=tampered`<br>`and again with the s parameter omitted entirely` | 400 {"detail":"Invalid tracking link."} both times — the redirect target is HMAC-verified against SECRET_KEY, so this endpoint cannot be used as an open redirect. |
| **I-N2** | NEG | Medium | Pixel with an unknown token<br>_No authentication at all_ | `GET /api/v1/procurement/t/o/00000000000000000000000000000000.gif` | Still 200 with the pixel — an unknown token records nothing but must not 404, or a scanner could enumerate valid tokens by status code. Confirm no information leaks through timing or body either. |
| **I-B1** | BND | Low | The same pixel loaded many times<br>_No authentication at all_ | `GET the same /t/o/{token}.gif five times (as a mail client re-rendering would)` | 200 each time; multiple open events are logged. Decide whether the engagement view should de-duplicate — a Gmail image proxy alone can inflate this. |
| **I-L1** | LGC | High | These routes are exempt from auth by design<br>_No authentication at all_ | `Call both /t/o/… and /t/c/… with no Authorization header, and again with a garbage bearer token` | No 401 in any of the four calls — the opaque per-send token IS the credential. Verify the token is unguessable (po_tracking.new_token) and that neither response exposes PO contents, supplier data or ids. |

## J · Provider webhooks (no auth)

`POST /webhooks/ses · POST /webhooks/twilio/whatsapp · POST /webhooks/twilio/voice` · **Role gate:** NONE — deliberately unauthenticated

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **J-P1** | POS | High | SES delivery and bounce events<br>_No authentication at all_ | `POST /api/v1/procurement/webhooks/ses`<br>`Body: {"tracking_token":"<token>","eventType":"Delivery"}`<br>`then Body: {"tracking_token":"<token>","eventType":"Bounce"}` | {"ok":true} both times; a delivery and a bounce event are logged. The bounce should mark the supplier's email_status invalid, which blocks further email sends until a new address is patched in (C-P3). |
| **J-P2** | POS | High | WhatsApp reply acknowledges the PO<br>_No authentication at all_ | `POST /api/v1/procurement/webhooks/twilio/whatsapp`<br>`Body: {"tracking_token":"<token>","Body":"Confirmed, dispatching Friday"}`<br>`GET /api/v1/procurement/pos/{po_id}` | {"ok":true}; the PO is acknowledged with channel whatsapp and the reply text stored in notes. |
| **J-P3** | POS | High | Voice press-1 acknowledges, press-2 does not<br>_No authentication at all_ | `POST /api/v1/procurement/webhooks/twilio/voice — Body: {"tracking_token":"<token>","Digits":"1"}`<br>`then on another PO — Body: {"tracking_token":"<token2>","Digits":"2"}` | Both return {"ok":true}, but only the Digits "1" call sets acknowledged_at — the handler compares the digit explicitly. |
| **J-N1** | NEG | Medium | Payload with no tracking token<br>_No authentication at all_ | `POST /api/v1/procurement/webhooks/twilio/whatsapp — Body: {"Body":"hello"}` | {"ok":true} and nothing recorded — a missing token is silently ignored, never an error, because a provider retrying on a 4xx would be worse. |
| **J-N2** | NEG | Medium | Body that isn't a JSON object<br>_No authentication at all_ | `POST /api/v1/procurement/webhooks/ses with Body: "just a string"  (or malformed JSON)` | 422 — the handler is typed `payload: dict`, so a non-object body is refused by FastAPI before any processing. |
| **J-B1** | BND | Medium | Unknown token in a webhook<br>_No authentication at all_ | `POST /api/v1/procurement/webhooks/twilio/voice — Body: {"tracking_token":"does-not-exist","Digits":"1"}` | {"ok":true} — acknowledge_by_token returns False for an unknown token and the handler ignores the result. No 404, nothing recorded. |
| **J-B2** | BND | Low | SES payload with the token nested under mail<br>_No authentication at all_ | `POST /api/v1/procurement/webhooks/ses — Body: {"mail":{"tracking_token":"<token>"},"notificationType":"Complaint"}` | {"ok":true} and the event is recorded — the handler falls back to payload["mail"]["tracking_token"] and accepts eventType, notificationType or type as the event name. |
| **J-L1** | LGC | High | Webhook signatures are not verified<br>_No authentication at all_ | `From any client, POST /api/v1/procurement/webhooks/twilio/whatsapp with a valid tracking_token you obtained from a PO`<br>`GET /api/v1/procurement/pos/{po_id}` | The PO is acknowledged even though the request came from nobody in particular — the router's own docstring marks provider signature verification TODO. Anyone able to reach the URL with a token can forge an acknowledgement or a bounce. Confirm and file as a security finding with the token's exposure surface (it travels in every tracked email link). |

## K · Production board

`GET /production-tracking · POST /production-tracking/{tracking_id}/transition` · **Role gate:** DM / MD / Viewer (read) · MD / DM / Cutting Manager (transition)

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **K-P1** | POS | High | Read the board<br>_Direct Manager_ | `GET /api/v1/procurement/production-tracking`<br>`GET /api/v1/procurement/production-tracking?order_id={order_id}` | 200 with one row per tracked style/order — client, order, style and the ladder status (awaiting_bom → bom_approved → inventory_checked → po_raised → po_confirmed → material_ready → released_to_production → in_production → completed). The filtered call returns a subset. |
| **K-P2** | POS | High | The human go/no-go transition<br>_Direct Manager_ | `POST /api/v1/procurement/production-tracking/{tracking_id}/transition`<br>`Body: {"status":"released_to_production"}` | 200 {"id":…,"status":"released_to_production"} — this is the deliberate manual gate; the earlier rungs advance automatically from BOM approval, the inventory check and PO confirmation. |
| **K-N1** | NEG | Medium | Unknown status value<br>_Direct Manager_ | `POST …/transition — Body: {"status":"shipped"}` | 422 {"error":"unknown_status","status":"shipped"} — the target must be one of the ProductionTrackingStatus values. |
| **K-N2** | NEG | Medium | Unknown tracker id<br>_Direct Manager_ | `POST /api/v1/procurement/production-tracking/{a made-up uuid}/transition — Body: {"status":"in_production"}` | 404 {"detail":"Tracker not found."} |
| **K-N3** | NEG | Medium | Wrong role transitions<br>_Stitching Manager (9000000003)_ | `POST …/transition — Body: {"status":"in_production"}` | 403 — the router gate admits MD/DM/Cutting only, and the service repeats the check with the message "Only DM/MD/Cutting may transition the board." |
| **K-N4** | NEG | Low | Empty status string<br>_Direct Manager_ | `POST …/transition — Body: {"status":""}` | 422 from Pydantic (min_length=1), before the unknown_status check runs. |
| **K-B1** | BND | High | Transition backwards down the ladder<br>_Direct Manager_ | `Move a tracker to completed`<br>`POST …/transition — Body: {"status":"po_raised"}` | 200 — manual transitions call _advance with force=True, so the ladder is not monotonic and a finished order can be dragged back to an early rung. Intentional for corrections, but confirm it is what the business wants and that the move is audited. |
| **K-L1** | LGC | Medium | The board mirrors the upstream stages<br>_Direct Manager_ | `Note a style's board status`<br>`Approve its BOM, then run the inventory check, then generate and confirm a PO`<br>`GET /api/v1/procurement/production-tracking after each step` | The row advances on its own through bom_approved → inventory_checked → po_raised → po_confirmed without any manual transition. Only released_to_production requires a human. |

---

Generated from `docs/testcases/_cases/supplier_po.cases.json` by `scripts/build_testcase_collections.js`. Edit the case file and re-run, so this sheet and `supplier_po.postman_collection.json` stay in step.
