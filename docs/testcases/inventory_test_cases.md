# Inventory — API Test Cases

> Stage 4 — inventory master + per-BOM stock check · module `app/modules/inventory` · 44 cases

Request-level test cases for the Stage-4 inventory API: the stock master sync (dry-run preview and idempotent commit), the stock list, and the per-BOM inventory check that reserves stock and produces the shortfall lines Stage 5 turns into supplier POs.

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
| Positive (POS) | 12 |
| Negative (NEG) | 13 |
| Boundary (BND) | 9 |
| Business Logic (LGC) | 10 |
| **Total** | **44** |

## Endpoints covered

| Group | Endpoints | Role gate | Cases |
|---|---|---|---|
| A · Stock master sync | `POST /inventory/preview · POST /inventory/commit` | DM / MD | 11 |
| B · Stock list | `GET /inventory/items` | DM / MD / Viewer | 8 |
| C · Run the inventory check | `POST /boms/{bom_id}/inventory-check` | DM / MD | 15 |
| D · Read checks | `GET /inventory-checks/{check_id} · GET /inventory-checks · GET /boms/{bom_id}/inventory-check` | DM / MD / Viewer | 10 |

## Verify before relying on

- **MD and DM bypass every `require_roles` gate** (`SUPERUSER_ROLES` in `app/modules/users/deps.py`). Use the Cutting Manager or Office Viewer for wrong-role cases on the mutating routes; the Office Viewer is *inside* the read gate, so use the Cutting Manager for wrong-role reads.
- Two error shapes: `{"detail": "..."}` for 404s, role 403s and the 413 upload cap; `{"error": "...", "message": "..."}` for the service's own refusals (`bom_not_approved`).
- The check runs only on an **approved, locked or exported** BOM — `_APPROVED_STATES`. A draft or ready-for-review BOM is refused.
- Only stockable categories are checked: main material, sub material, lining, interlining, thread, accessory, packaging. Anything else (labour, overhead, …) is carried as `excluded` rather than being matched.
- A re-run first **releases** that BOM's previous reservations (reason `recheck`), then re-claims — so re-running never double-counts the BOM against itself, but reservations held by *other* BOMs do reduce what this one can see.
- A fuzzy match is only ever a **suggestion**: flags carry `suggestion` + `unmatched` and `matched_method` stays null. Only an exact normalized key or a curated alias binds automatically.
- There is no endpoint to release a reservation, confirm a suggestion or set a manual match — everything here is driven by re-running the check. Treat that as a coverage gap, not a missing test.

## A · Stock master sync

`POST /inventory/preview · POST /inventory/commit` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **A-P1** | POS | High | Preview a stock workbook<br>_Direct Manager_ | `POST /api/v1/procurement/inventory/preview`<br>`multipart body: file = <the stock master workbook>` | 200 with the preview block — parsed row count, warnings and samples. Nothing is written: a follow-up GET /inventory/items is unchanged. |
| **A-P2** | POS | High | Commit the stock workbook<br>_Direct Manager_ | `POST /api/v1/procurement/inventory/commit`<br>`multipart body: file = <the same workbook>` | 200 with the preview block plus committed: N and deactivated: M. Items are upserted on normalized_key with description, uom, qty_on_hand, rate and color. |
| **A-N1** | NEG | High | Wrong role commits<br>_Cutting Manager_ | `POST /api/v1/procurement/inventory/commit — multipart body: file = <workbook>` | 403 role-not-permitted — the sync is DM/MD only. |
| **A-N2** | NEG | Medium | A viewer cannot commit<br>_Office Viewer (9000000004)_ | `POST /api/v1/procurement/inventory/commit — multipart body: file = <workbook>` | 403 — the Office Viewer can read the stock list (B-P3) but is outside the mutating gate. This pair is the clearest read/write split in the module. |
| **A-N3** | NEG | Medium | No file part<br>_Direct Manager_ | `POST /api/v1/procurement/inventory/preview with an empty body` | 422 — File(...) is required. |
| **A-B1** | BND | Medium | Upload over the cap<br>_Direct Manager_ | `POST /api/v1/procurement/inventory/preview — file = <larger than MAX_UPLOAD_MB (25 MB default)>` | 413 {"detail":"File exceeds the 25 MB limit."} — raised while streaming by this module's own _read_capped, independent of the Stage-1 upload machinery. |
| **A-B2** | BND | Medium | Empty or unparseable workbook<br>_Direct Manager_ | `POST /api/v1/procurement/inventory/preview — file = <0 bytes>`<br>`then file = <a .txt renamed .xlsx>` | Expect a clean 4xx or a preview with warnings and zero rows — NOT a 500. There is no content-type check on this route, so the parser is the only guard; record what actually happens. |
| **A-B3** | BND | High | Commit an empty but valid workbook<br>_Direct Manager_ | `POST /api/v1/procurement/inventory/commit with a structurally valid workbook containing NO data rows`<br>`GET /api/v1/procurement/inventory/items` | committed: 0 and deactivated: <every previously active key> — because `deactivate_keys_not_in(keep)` runs with an empty keep-set. One wrong file can retire the entire stock master. Confirm and flag loudly; this is the highest-blast-radius call in the module. |
| **A-L1** | LGC | High | Commit is idempotent<br>_Direct Manager_ | `POST /api/v1/procurement/inventory/commit twice with the identical file`<br>`GET /api/v1/procurement/inventory/items?limit=1000` | The second commit reports the same `committed` count and deactivated: 0; no duplicate items appear — the upsert keys on normalized_key. |
| **A-L2** | LGC | High | Missing keys are deactivated, not deleted<br>_Direct Manager_ | `Commit workbook v1`<br>`Commit workbook v2 which omits one previously present item`<br>`GET /api/v1/procurement/inventory/items?search=<that item>` | `deactivated` counts it and the row still exists (inactive) — history and any reservations against it survive. Confirm whether the list endpoint hides inactive items by default. |
| **A-L3** | LGC | Medium | Preview and commit agree<br>_Direct Manager_ | `POST /inventory/preview with a file and note the row count and warnings`<br>`POST /inventory/commit with the same file` | commit's preview block matches preview's exactly, and `committed` equals the previewed row count — the dry run is trustworthy as a go/no-go. |

## B · Stock list

`GET /inventory/items` · **Role gate:** DM / MD / Viewer

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **B-P1** | POS | High | List stock items<br>_Direct Manager_ | `GET /api/v1/procurement/inventory/items` | 200 {"items":[{id,normalized_key,description,uom,qty_on_hand,rate,color,…}], "count": N}. |
| **B-P2** | POS | Medium | Search the master<br>_Direct Manager_ | `GET /api/v1/procurement/inventory/items?search=cowhide&limit=25&offset=0` | 200; every row matches the search term. `count` is this page's length, not the total. |
| **B-P3** | POS | Medium | Office Viewer can read stock<br>_Office Viewer (9000000004)_ | `GET /api/v1/procurement/inventory/items` | 200 — VIEWER is inside the _VIEW gate. |
| **B-N1** | NEG | Medium | Role outside the read gate<br>_Cutting Manager_ | `GET /api/v1/procurement/inventory/items` | 403 role-not-permitted — worth noting that the manager who consumes the stock cannot query it here. |
| **B-N2** | NEG | Low | No token<br>_Unauthenticated_ | `GET /api/v1/procurement/inventory/items with no Authorization header` | 401 — unlike the supplier-PO tracking routes, nothing in this module is anonymous. |
| **B-B1** | BND | Medium | limit=0 and a far offset<br>_Direct Manager_ | `GET /api/v1/procurement/inventory/items?limit=0`<br>`GET /api/v1/procurement/inventory/items?offset=999999` | 200 with an empty list both times — limit and offset have no bounds or validation. |
| **B-B2** | BND | Low | Unbounded limit<br>_Direct Manager_ | `GET /api/v1/procurement/inventory/items?limit=1000000` | 200, accepted — no server-side ceiling. Flag as a perf gap on a large master. |
| **B-L1** | LGC | Medium | Are deactivated items listed?<br>_Direct Manager_ | `Commit a workbook that drops an item (A-L2)`<br>`GET /api/v1/procurement/inventory/items?search=<that item>` | Determine from the response whether inactive rows are included — there is no `active` filter on this endpoint, unlike GET /suppliers. If they are listed with no status flag, a picker cannot tell live stock from retired stock; file that. |

## C · Run the inventory check

`POST /boms/{bom_id}/inventory-check` · **Role gate:** DM / MD

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **C-P1** | POS | High | Check an approved BOM<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/inventory-check (BOM status approved)` | 200 with the check: a header plus one line per stockable BOM item carrying required_qty, on_hand_qty, shortfall_qty, status (sufficient / partial / out_of_stock), matched_method and flags. Active reservations are created for whatever could be claimed. |
| **C-P2** | POS | Medium | Check a locked or exported BOM<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/inventory-check on a BOM whose status is locked, then on one that is exported` | 200 both times — _APPROVED_STATES covers approved, locked and exported. |
| **C-P3** | POS | High | Re-run the check<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/inventory-check`<br>`Commit a stock file that raises the on-hand quantity`<br>`POST /api/v1/procurement/boms/{bom_id}/inventory-check again` | 200; the second check reflects the new stock and the BOM's own earlier reservations do not count against it — they are released with reason `recheck` before re-claiming. |
| **C-N1** | NEG | High | Check an unapproved BOM<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/inventory-check on a BOM in draft or ready_for_review` | 409 {"error":"bom_not_approved","message":"Inventory check runs only on an approved/locked BOM."} |
| **C-N2** | NEG | Medium | Check a rejected BOM<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/inventory-check on a rejected BOM` | 409 bom_not_approved — rejected is not in _APPROVED_STATES either. |
| **C-N3** | NEG | Medium | Unknown BOM<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{a made-up uuid}/inventory-check` | 404 {"detail":"BOM not found."} |
| **C-N4** | NEG | Medium | Wrong role runs the check<br>_Office Viewer_ | `POST /api/v1/procurement/boms/{bom_id}/inventory-check` | 403 — running a check writes reservations, so it sits behind the mutating gate. |
| **C-B1** | BND | Medium | A BOM with no stockable lines<br>_Direct Manager_ | `POST /api/v1/procurement/boms/{bom_id}/inventory-check on a BOM whose lines are all non-stockable categories` | 200 with no checked lines and everything reported as excluded — no error, and generate-pos on it should then yield an empty PO list. |
| **C-B2** | BND | Medium | A line whose required quantity is 0<br>_Direct Manager_ | `Leave a stockable BOM line at qty_per_garment 0`<br>`POST /api/v1/procurement/boms/{bom_id}/inventory-check` | That line cannot be `sufficient` — the sufficiency test requires required > 0 — so a zero-requirement line lands on out_of_stock (or partial) with shortfall 0. Confirm, and flag it: a 0-qty line reads as a stock problem when it is really an unfilled BOM cell. |
| **C-B3** | BND | High | UOM mismatch with no conversion<br>_Direct Manager_ | `Have a BOM line in a UOM that has no conversion to the matched stock item's UOM (e.g. sqft vs. metres)`<br>`POST /api/v1/procurement/boms/{bom_id}/inventory-check` | The line carries flags containing `uom_mismatch`, on_hand_qty 0 and status out_of_stock — an unconvertible lot is treated as no stock rather than silently compared. Verify no reservation was made against that lot. |
| **C-B4** | BND | Medium | Exactly enough stock<br>_Direct Manager_ | `Set a stock item's qty_on_hand exactly equal to the BOM line's requirement`<br>`POST /api/v1/procurement/boms/{bom_id}/inventory-check` | status sufficient with shortfall_qty 0 — the comparison is available >= required, so the exact boundary passes rather than falling to partial. |
| **C-L1** | LGC | High | Another BOM's reservation reduces what you can claim<br>_Direct Manager_ | `Run the check on BOM A so it reserves most of a lot`<br>`Run the check on BOM B, which needs the same material`<br>`Compare BOM B's on_hand_qty with its available/shortfall` | BOM B sees the lot's on-hand but an `available` reduced by A's active reservation, so it reports partial or out_of_stock with a real shortfall. This is the whole point of soft reservations — two BOMs cannot both spend the same leather. |
| **C-L2** | LGC | High | A fuzzy candidate never auto-binds<br>_Direct Manager_ | `Have a BOM line whose name only loosely resembles a stock description (no exact key, no alias)`<br>`POST /api/v1/procurement/boms/{bom_id}/inventory-check` | flags contain `suggestion` and `unmatched`, the suggestion payload names the candidate, matched_method is null and no stock is claimed. A human must curate an alias before it counts — verify the line is short (not silently satisfied). |
| **C-L3** | LGC | Medium | An alias binds where the raw key does not<br>_Direct Manager_ | `Note a line that comes back unmatched`<br>`Add a curated alias mapping that BOM term to the stock key`<br>`POST /api/v1/procurement/boms/{bom_id}/inventory-check again` | matched_method becomes `alias`, the line matches the lot and stock is reserved — the documented path from an unmatched line to a satisfied one. |
| **C-L4** | LGC | High | The check is Stage 5's precondition<br>_Direct Manager_ | `On a freshly approved BOM, POST /api/v1/procurement/boms/{bom_id}/generate-pos → 409 no_inventory_check`<br>`POST /api/v1/procurement/boms/{bom_id}/inventory-check`<br>`POST /api/v1/procurement/boms/{bom_id}/generate-pos again` | The PO generation that was refused now succeeds and buys exactly the lines this check marked short. This is the seam between Stage 4 and Stage 5 — run it end to end at least once. |

## D · Read checks

`GET /inventory-checks/{check_id} · GET /inventory-checks · GET /boms/{bom_id}/inventory-check` · **Role gate:** DM / MD / Viewer

| ID | Type | Prio | Case | Request | Expected result |
|---|---|---|---|---|---|
| **D-P1** | POS | High | Read one check by id<br>_Direct Manager_ | `GET /api/v1/procurement/inventory-checks/{check_id}` | 200 — the rebuilt check view with every line, its status, flags and matched item. |
| **D-P2** | POS | High | The dashboard<br>_Direct Manager_ | `GET /api/v1/procurement/inventory-checks`<br>`GET /api/v1/procurement/inventory-checks?order_id={order_id}` | 200 — the latest check per BOM, grouped with client and order identity; the filtered call narrows to one order. |
| **D-P3** | POS | High | Latest check for a BOM<br>_Direct Manager_ | `GET /api/v1/procurement/boms/{bom_id}/inventory-check` | 200 — the same view as D-P1 for that BOM's most recent check. |
| **D-P4** | POS | Low | Office Viewer can read checks<br>_Office Viewer (9000000004)_ | `GET /api/v1/procurement/inventory-checks` | 200 — reads are open to VIEWER across this module. |
| **D-N1** | NEG | Medium | Unknown check id<br>_Direct Manager_ | `GET /api/v1/procurement/inventory-checks/{a made-up uuid}` | 404 {"detail":"Inventory check not found."} |
| **D-N2** | NEG | High | A BOM that was never checked<br>_Direct Manager_ | `GET /api/v1/procurement/boms/{bom_id}/inventory-check on a BOM with no check` | 404 {"detail":"No inventory check for this BOM yet."} — a distinct message from D-N1, and the signal a UI should read as "run the check" rather than "something broke". |
| **D-N3** | NEG | Medium | Malformed id<br>_Direct Manager_ | `GET /api/v1/procurement/inventory-checks/not-a-uuid` | 422 from FastAPI's path parsing, before the handler runs. |
| **D-N4** | NEG | Medium | Wrong role reads a check<br>_Cutting Manager_ | `GET /api/v1/procurement/inventory-checks` | 403 role-not-permitted. |
| **D-L1** | LGC | High | "Latest" really is the latest<br>_Direct Manager_ | `Run a check, note its id from the response`<br>`Run the check again`<br>`GET /api/v1/procurement/boms/{bom_id}/inventory-check` | The returned check id is the SECOND run's, not the first — and the older check is still readable by id via D-P1, so the history of what was decided is preserved. |
| **D-L2** | LGC | Medium | The dashboard shows one row per BOM<br>_Direct Manager_ | `Run checks on several BOMs, re-running one of them twice`<br>`GET /api/v1/procurement/inventory-checks` | Exactly one row per BOM, carrying the latest run — the board is a current-state view, not a log. |

---

Generated from `docs/testcases/_cases/inventory.cases.json` by `scripts/build_testcase_collections.js`. Edit the case file and re-run, so this sheet and `inventory.postman_collection.json` stay in step.
