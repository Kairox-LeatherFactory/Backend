# Procurement — API Test Cases

> Stage 1 — upload intake and validation · module `app/modules/procurement` · 74 cases

Request-level test cases for the Stage-1 intake API. Every row is a literal HTTP request (method, path, headers, body) with the exact status code and response fields to check. Ready-made requests: procurement.postman_collection.json.

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
| Positive (POS) | 21 |
| Negative (NEG) | 25 |
| Boundary (BND) | 12 |
| Business Logic (LGC) | 16 |
| **Total** | **74** |

## Endpoints covered

| Group | Endpoints | Role gate | Cases |
|---|---|---|---|
| A · Open a submission | `POST /submissions` | DM / MD | 8 |
| FG · Shared intake gates (order + spec) | `POST /submissions/{id}/order-sheet · POST /submissions/{id}/spec-sheet` | DM / MD | 26 |
| OS · Order-sheet slot | `POST /submissions/{id}/order-sheet · POST /upload/order-sheet` | DM / MD | 12 |
| SS · Spec-sheet slot | `POST /submissions/{id}/spec-sheet · POST /upload/spec-sheet` | DM / MD | 9 |
| ST · Submission status / readiness | `GET /submissions/{submission_id}` | DM / MD | 8 |
| DR · Document report | `GET /submissions/{submission_id}/documents/{document_id}` | DM / MD | 6 |
| G · End-to-end sequences | `chained calls across the endpoints above` | DM / MD | 5 |

## Verify before relying on

- Every endpoint requires `Authorization: Bearer <token>` for a Direct Manager or Managing Director — there is no anonymous access anywhere in this module.
- **Known build note:** the byte-identical re-upload shortcut (instant replay / 409 `duplicate_content`) is disabled in this build — the service marks it "TESTING PHASE". Until it is re-enabled, re-POSTing the same bytes re-runs full validation instead of short-circuiting. OS-B1 / OS-B2 depend on this; confirm current behaviour before filing either as a bug.
- Every 4xx/5xx upload response carries a `reason_code`: unsupported_mime (415), file_too_large (413), empty_or_corrupt (422), virus_detected (422), scanner_unavailable (503), not_an_order_sheet / not_a_spec_sheet / wrong_slot / needs_manual_review (422), submission_locked (409), duplicate_content (409).
- `force=true` only ever overrides `needs_manual_review`. It is ignored for every hard reject.
- The ORDER slot is the only slot that locks, and the only slot that gates Stage-2 readiness. The SPEC slot never locks and never gates on its own.

## A · Open a submission

`POST /submissions` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **A-P1** | POS | High | Open with no client | `POST /submissions`<br>`Authorization: Bearer {{dm_token}}`<br>`Body: {}  (or omitted entirely)` | 201; body {"submission_id":"<uuid>","status":"open"} |
| **A-P2** | POS | Medium | Open with a client_id | `POST /submissions`<br>`Authorization: Bearer {{dm_token}}`<br>`Body: {"client_id": "<known-client-uuid>"}` | 201; same shape as A‑P1 — the client link isn't echoed here, verify via GET status later. |
| **A-P3** | POS | Low | MD opens a submission | `POST /submissions`<br>`Authorization: Bearer {{md_token}}`<br>`Body: {}` | 201, identical shape. |
| **A-N1** | NEG | High | Wrong role | `POST /submissions`<br>`Authorization: Bearer {{other_token}}`<br>`Body: {}` | 403 Forbidden. |
| **A-N2** | NEG | Medium | No/invalid token | `POST /submissions`<br>`(no Authorization header, or an expired/garbage token)`<br>`Body: {}` | 401 Unauthorized. |
| **A-N3** | NEG | Medium | Malformed client_id | `POST /submissions`<br>`Authorization: Bearer {{dm_token}}`<br>`Body: {"client_id": "not-a-uuid"}` | 422 — Pydantic UUID coercion failure. |
| **A-B1** | BND | Low | Many opens in a row | `POST /submissions ×5`<br>`Authorization: Bearer {{dm_token}}`<br>`Body: {}  (fire 5 times back to back)` | Each call returns a distinct submission_id; no error, no rate limit observed at this volume. |
| **A-L1** | LGC | Medium | A client-less submission still accepts uploads | `POST /submissions → {}`<br>`then`<br>`POST /submissions/{id}/order-sheet`<br>`(valid order-sheet file, no client on the submission)` | Upload proceeds and validates normally — client_id only narrows which client_template profile is checked first, it's never a precondition. |

## FG · Shared intake gates (order + spec)

`POST /submissions/{id}/order-sheet · POST /submissions/{id}/spec-sheet` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **FG-P1** | POS | High | Valid PDF, onboarded client | `POST /submissions/{id}/order-sheet`<br>`Authorization: Bearer {{dm_token}}`<br>`Content-Type: multipart/form-data`<br>`file=@order_sheet_valid.pdf` | 201; document.validation.status:"accepted", method:"heuristic", slot filled in the returned submission summary. |
| **FG-P2** | POS | High | Valid XLSX | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_valid.xlsx` | 201, accepted. |
| **FG-P3** | POS | Medium | Valid CSV | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_valid.csv` | 201, accepted. |
| **FG-P4** | POS | Medium | Valid legacy .xls | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_valid.xls  (old BIFF format)` | 201, accepted — read via the xlrd fallback path. |
| **FG-P5** | POS | Medium | Scanned PDF, genuinely valid | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_scanned_valid.pdf  (image-only, no text layer)` | 201 or 422 depending on OCR/LLM outcome — method:"llm" if it escalated and passed; check validation_signals shows OCR text was used, not an instant reject just because there's no text layer. |
| **FG-P6** | POS | Low | Mid-confidence genuine document | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_sparse_but_real.pdf` | Resolves to a real verdict (accepted, rejected, or needs_manual_review) with method:"llm" — not stuck, not a timeout. |
| **FG-N1** | NEG | High | Unsupported file type | `POST /submissions/{id}/order-sheet`<br>`file=@random.docx` | 415; reason_code:"unsupported_mime". |
| **FG-N2** | NEG | High | Disguised file (extension lies) | `POST /submissions/{id}/order-sheet`<br>`file=@renamed_zip_as.pdf  (a .zip's bytes, .pdf filename)` | 415; reason_code:"unsupported_mime" — sniffed content wins over the extension. |
| **FG-N3** | NEG | Medium | Oversized file | `POST /submissions/{id}/order-sheet`<br>`file=@big_file_30mb.pdf` | 413; reason_code:"file_too_large" — returned while streaming, before the pipeline runs. |
| **FG-N4** | NEG | Medium | Zero-byte upload | `POST /submissions/{id}/order-sheet`<br>`file=@empty.pdf  (0 bytes)` | 422; reason_code:"empty_or_corrupt". |
| **FG-N5** | NEG | Medium | Corrupted file | `POST /submissions/{id}/order-sheet`<br>`file=@truncated_corrupt.xlsx` | 422; reason_code:"empty_or_corrupt", no 500. |
| **FG-N6** | NEG | High | EICAR virus test string | `POST /submissions/{id}/order-sheet`<br>`file=@eicar.txt  (the standard EICAR test signature, renamed .pdf/.csv)`<br>`[requires VIRUS_SCAN_ENABLED=true in this environment]` | 422; reason_code:"virus_detected", payload.scan_signature present. Cross-check an AuditLog row with action:"VIRUS_DETECTED" was written (ask backend/DB access if you can't query directly). |
| **FG-N7** | NEG | High | Scanner unreachable | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_valid.pdf`<br>`[with VIRUS_SCAN_ENABLED=true and clamd stopped]` | 503; reason_code:"scanner_unavailable" — confirm no document/storage row was created (fail closed, nothing stored unscanned). |
| **FG-N8** | NEG | Medium | Narrative document quoting real client refs | `POST /submissions/{id}/order-sheet`<br>`file=@process_workflow_writeup.pdf  (prose PDF, section headings, mentions real order numbers)` | 422; reason_code:"not_an_order_sheet", method:"heuristic", signals_found mentions "flowing narrative text" / section headings. |
| **FG-N9** | NEG | High | Right document, wrong slot | `POST /submissions/{id}/order-sheet`<br>`file=@spec_sheet_valid.pdf  (a real SPEC sheet, posted to the ORDER slot)` | 422; reason_code:"wrong_slot", suggested_fix names the correct slot. |
| **FG-N10** | NEG | Medium | Unrelated document | `POST /submissions/{id}/order-sheet`<br>`file=@random_unrelated_spreadsheet.xlsx` | 422; reason_code:"not_an_order_sheet". |
| **FG-B1** | BND | Medium | Exactly at the size limit | `POST /submissions/{id}/order-sheet`<br>`file=@exactly_25mb.pdf` | Not rejected for size — processes normally (only strictly > the cap 413s). |
| **FG-B2** | BND | Low | A few KB over the limit | `POST /submissions/{id}/order-sheet`<br>`file=@25mb_plus_1kb.pdf` | 413; reason_code:"file_too_large". |
| **FG-B3** | BND | Low | PDF with a sliver of text (near the 40-char text-layer bar) | `POST /submissions/{id}/order-sheet`<br>`file=@pdf_stamp_only_text.pdf  (a watermark/stamp, <40 extractable chars)` | Treated as scanned (is_scanned_pdf:true in signals) and escalated via OCR, not scored as if it had a real text layer. |
| **FG-B4** | BND | Medium | Right on the heuristic accept/reject boundary | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_borderline.pdf  (weakly matches the client's fingerprint terms)` | method:"llm" in the response — escalated instead of guessed at the heuristic layer. |
| **FG-B5** | BND | Low | No file extension | `POST /submissions/{id}/order-sheet`<br>`file=@order  (valid PDF bytes, filename has no extension)` | Classified purely on sniffed content — extension-agreement check is skipped when there's no extension. |
| **FG-B6** | BND | Low | Mixed/uppercase extension | `POST /submissions/{id}/order-sheet`<br>`file=@ORDER.PDF` | Recognized the same as lowercase — no unsupported_mime from casing alone. |
| **FG-L1** | LGC | High | "Needs review" is a distinct outcome | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_ambiguous.pdf` | 422; validation.status:"needs_manual_review", validation.can_override:true, override_hint text present — never silently 201'd or silently a hard reject. |
| **FG-L2** | LGC | Low | Client-vocabulary vs. generic-English diagnostics | `Compare two runs of POST /submissions/{id}/order-sheet:`<br>`1) file rejected for an onboarded non-English client`<br>`2) same bad file, submission with NO client onboarded` | validation.signals_expected differs between the two — (1) lists that client's own anchor terms, (2) falls back to the generic English list. |
| **FG-L3** | LGC | Medium | Graceful degrade with no classifier configured | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_borderline.pdf`<br>`[environment with no LLM/classifier key configured]` | 422; validation.status:"needs_manual_review", method:"heuristic" — not a 500, not a hang. |
| **FG-L4** | LGC | Low | Illegible scan degrades safely | `POST /submissions/{id}/order-sheet`<br>`file=@scanned_illegible.pdf  (poor-quality scan, OCR finds nothing)` | 422; validation.status:"needs_manual_review" — not force-guessed accept or reject. |

## OS · Order-sheet slot

`POST /submissions/{id}/order-sheet · POST /upload/order-sheet` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **OS-P1** | POS | High | Accept into a fresh submission | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_valid.pdf` | 201; submission summary shows order_sheet.present:true, ready_for_stage_2:true (order alone is enough — OS‑L1). |
| **OS-P2** | POS | Medium | Correct with a re-upload before consumption | `1) POST /submissions/{id}/order-sheet → accepted (doc A)`<br>`2) POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_corrected.pdf  (different bytes)` | 201 on step 2; GET .../documents/{doc A} now shows validation.status:"superseded". |
| **OS-P3** | POS | Medium | Direct upload, no submission yet | `POST /upload/order-sheet`<br>`Authorization: Bearer {{dm_token}}`<br>`file=@order_sheet_valid.pdf` | 201; response's submission_id is a NEW id — capture it for the paired spec-sheet upload. |
| **OS-P4** | POS | High | Force-accept a needs_manual_review order sheet | `1) POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_ambiguous.pdf → 422 needs_manual_review`<br>`2) POST /submissions/{id}/order-sheet?force=true`<br>`file=@order_sheet_ambiguous.pdf  (SAME file)` | Step 2 → 201; document.validation.status:"accepted", validation_signals.manual_override:true. |
| **OS-N1** | NEG | High | Change the order sheet after consumption | `POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_v2.pdf`<br>`[submission.status already "consumed" — order was claimed by the style breakdown]` | 409; reason_code:"submission_locked". |
| **OS-N2** | NEG | High | force=true on a hard reject | `POST /submissions/{id}/order-sheet?force=true`<br>`file=@random.docx  (hard reject: unsupported_mime)` | Still 415; reason_code:"unsupported_mime" — force is ignored for anything that isn't needs_manual_review. |
| **OS-N3** | NEG | Medium | Wrong role | `POST /submissions/{id}/order-sheet`<br>`Authorization: Bearer {{other_token}}`<br>`file=@order_sheet_valid.pdf` | 403. |
| **OS-N4** | NEG | Low | Unknown submission id | `POST /submissions/00000000-0000-0000-0000-000000000000/order-sheet`<br>`file=@order_sheet_valid.pdf` | 404 "Submission not found." |
| **OS-B1** | BND | Medium | Re-POST the identical file twice | `1) POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_valid.pdf → 201`<br>`2) POST /submissions/{id}/order-sheet`<br>`file=@order_sheet_valid.pdf  (byte-identical)` | Per the build note: currently re-runs full validation again (still 201) rather than an instant replay. Confirm this is the intended current state — the designed behaviour (dedupe enabled) is a short-circuit replay of the cached verdict. |
| **OS-B2** | BND | Low | Same bytes already accepted in the OTHER slot | `1) POST /submissions/{id}/spec-sheet`<br>`file=@shared_bytes.pdf → 201 accepted into SPEC slot`<br>`2) POST /submissions/{id}/order-sheet`<br>`file=@shared_bytes.pdf  (identical bytes)` | Designed behaviour (dedupe enabled): 409, error:"duplicate_content". Currently may instead re-validate per the build note — record whichever you observe. |
| **OS-L1** | LGC | High | Readiness needs ONLY the order slot | `1) POST /submissions/{id}/order-sheet → 201 (spec slot left empty)`<br>`2) GET /submissions/{id}` | Step 2 → status:"complete", ready_for_stage_2:true, spec_sheet.present:false — a spec sheet is not required here. |
| **OS-L2** | LGC | Medium | A spec uploaded first still lands, doesn't gate | `1) POST /submissions/{id}/spec-sheet → 201 (order slot still empty)`<br>`2) GET /submissions/{id}` | Step 2 → spec_sheet.present:true but status:"open", ready_for_stage_2:false — the order slot is the only gate. |

## SS · Spec-sheet slot

`POST /submissions/{id}/spec-sheet · POST /upload/spec-sheet` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **SS-P1** | POS | High | Accept at any time | `POST /submissions/{id}/spec-sheet`<br>`file=@spec_sheet_valid.pdf   (try both before AND after the order slot is filled)` | 201 either way; spec_sheet.present:true. |
| **SS-P2** | POS | Low | Direct upload, no submission yet | `POST /upload/spec-sheet`<br>`file=@spec_sheet_valid.pdf` | 201; new submission_id returned. |
| **SS-P3** | POS | Medium | Force-accept needs_manual_review | `POST /submissions/{id}/spec-sheet?force=true`<br>`file=@spec_sheet_ambiguous.pdf` | 201; validation_signals.manual_override:true. |
| **SS-N1** | NEG | Medium | Wrong role | `POST /submissions/{id}/spec-sheet`<br>`Authorization: Bearer {{other_token}}`<br>`file=@spec_sheet_valid.pdf` | 403. |
| **SS-N2** | NEG | Low | Unknown submission id | `POST /submissions/00000000-0000-0000-0000-000000000000/spec-sheet`<br>`file=@spec_sheet_valid.pdf` | 404. |
| **SS-N3** | NEG | Medium | force=true on a hard reject | `POST /submissions/{id}/spec-sheet?force=true`<br>`file=@virus_eicar.pdf` | Still 422 virus_detected — override never applies to a hard reject. |
| **SS-B1** | BND | High | Upload after the order is already locked | `POST /submissions/{id}/spec-sheet`<br>`file=@spec_sheet_valid.pdf`<br>`[submission.status already "consumed"]` | 201 — spec slot is deliberately never locked, unlike the order slot. |
| **SS-L1** | LGC | Medium | Spec-only submission stays "open" | `1) POST /submissions/{id}/spec-sheet → 201 (no order upload at all)`<br>`2) GET /submissions/{id}` | Step 2 → status:"open", ready_for_stage_2:false, blocking lists "order_sheet missing". |
| **SS-L2** | LGC | Low | spec_type correctly told apart | `1) POST /submissions/{id}/spec-sheet`<br>`file=@spec_measurement_grid.xlsx`<br>`2) separately: POST /submissions/{id2}/spec-sheet`<br>`file=@spec_narrative_techpack.pdf` | Each document's validation.spec_type reflects its own kind (measurement grid vs. narrative tech pack) — check via GET .../documents/{id}. |

## ST · Submission status / readiness

`GET /submissions/{submission_id}` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **ST-P1** | POS | Medium | Fresh, empty submission | `GET /submissions/{id}`<br>`(immediately after opening, nothing uploaded)` | 200; order_sheet.present:false, spec_sheet.present:false, ready_for_stage_2:false, blocking:["order_sheet missing","spec_sheet missing"]. |
| **ST-P2** | POS | High | After order accepted | `GET /submissions/{id}`<br>`(order sheet accepted, spec not uploaded)` | 200; status:"complete", ready_for_stage_2:true, order_sheet.present:true. |
| **ST-P3** | POS | Medium | After both slots accepted | `GET /submissions/{id}`<br>`(both order + spec accepted)` | 200; both slots present:true, still ready. |
| **ST-N1** | NEG | Low | Unknown submission id | `GET /submissions/00000000-0000-0000-0000-000000000000` | 404. |
| **ST-N2** | NEG | Low | Wrong role | `GET /submissions/{id}`<br>`Authorization: Bearer {{other_token}}` | 403. |
| **ST-B1** | BND | Low | Status right after a supersede | `1) accept order doc A`<br>`2) re-upload → doc B accepted, A superseded`<br>`3) GET /submissions/{id}` | Reflects doc B only — superseded doc A doesn't affect the slot state either way. |
| **ST-B2** | BND | Low | Accepted but scan status not clean | `GET /submissions/{id}`<br>`[a slot's doc has validation_status=accepted but scan_status outside clean/skipped — needs a DB/test-fixture setup, ask backend for a seeded case]` | blocking includes an entry like "order_sheet scan_status=...", called out separately from the validation status. |
| **ST-L1** | LGC | Medium | blocking[] is always human-readable | `GET /submissions/{id}`<br>`(run against several submissions in different incomplete states)` | Every non-ready case includes specific plain-text reasons in blocking — never just a bare boolean with no explanation. |

## DR · Document report

`GET /submissions/{submission_id}/documents/{document_id}` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **DR-P1** | POS | Medium | Report for an accepted doc | `GET /submissions/{id}/documents/{doc_id}`<br>`(doc_id of an accepted upload)` | 200; document.validation.status:"accepted", confidence, method, classified_as populated. |
| **DR-P2** | POS | High | Report for a rejected doc | `GET /submissions/{id}/documents/{doc_id}`<br>`(doc_id of a rejected upload)` | 200; validation.status:"rejected", reason_code, signals_expected vs signals_found, suggested_fix text. |
| **DR-N1** | NEG | High | Document belongs to a different submission | `GET /submissions/{submission_B_id}/documents/{doc_from_submission_A}` | 404 "Document not found in this submission." — must NOT leak the document via the wrong submission. |
| **DR-N2** | NEG | Low | Unknown document id | `GET /submissions/{id}/documents/00000000-0000-0000-0000-000000000000` | 404. |
| **DR-N3** | NEG | Low | Wrong role | `GET /submissions/{id}/documents/{doc_id}`<br>`Authorization: Bearer {{other_token}}` | 403. |
| **DR-L1** | LGC | Medium | Superseded doc keeps its own original verdict | `1) doc A accepted, then superseded by doc B`<br>`2) GET /submissions/{id}/documents/{doc A id}` | Still shows doc A's ORIGINAL verdict/signals as recorded at the time — not overwritten to match doc B. |

## G · End-to-end sequences

`chained calls across the endpoints above` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **G-1** | LGC | High | Straight-through happy path | `1) POST /submissions → {}`<br>`2) POST /submissions/{id}/order-sheet, file=@order_sheet_valid.pdf`<br>`3) GET /submissions/{id}` | 201 → 201 → 200 ready, no manual step needed anywhere. |
| **G-2** | LGC | High | Reject → correct → re-upload → accept | `1) POST /submissions → {}`<br>`2) POST /submissions/{id}/order-sheet, file=@bad.docx → 415`<br>`3) POST /submissions/{id}/order-sheet, file=@order_sheet_valid.pdf → 201`<br>`4) GET /submissions/{id} → ready` | A rejected slot is fully recoverable within the SAME submission by re-POSTing the fixed file. |
| **G-3** | LGC | Medium | Pair two direct uploads | `1) POST /upload/order-sheet, file=@order_sheet_valid.pdf → capture submission_id`<br>`2) POST /upload/spec-sheet?...  (NOTE: /upload/spec-sheet does NOT take a submission_id param — instead POST POST /submissions/{captured_id}/spec-sheet, file=@spec_sheet_valid.pdf)` | Both documents land on the SAME submission — verify via GET /submissions/{captured_id} showing both slots present. |
| **G-4** | LGC | Medium | Manual-review override end to end | `1) POST /submissions/{id}/order-sheet, file=@ambiguous.pdf → 422 needs_manual_review`<br>`2) GET /submissions/{id}/documents/{doc_id} → review signals`<br>`3) POST /submissions/{id}/order-sheet?force=true, file=@ambiguous.pdf → 201` | Same submission recovers from "needs review" straight into "accepted" without starting over. |
| **G-5** | LGC | Medium | Locked order forces a fresh submission | `1) [given: submission.status="consumed"]`<br>`2) POST /submissions/{id}/order-sheet, file=@order_sheet_v2.pdf → 409 submission_locked`<br>`3) POST /submissions → {} → new id`<br>`4) POST /submissions/{new_id}/order-sheet, file=@order_sheet_v2.pdf → 201` | The 409 has a clear, actionable path forward — a new submission, not a dead end. |

---

Generated from `docs/testcases/_cases/procurement.cases.json` by `scripts/build_testcase_collections.js`. Edit the case file and re-run, so this sheet and `procurement.postman_collection.json` stay in step.
