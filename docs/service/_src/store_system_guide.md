# 1. What this service is

The store is where a garment's **leather**, its **lining** and its **accessory kit** come together, and where it is **released** into line-stitching.

> **The store is a STATE ON THE GARMENT, not a place.** There is no drawer, no box number, no pool of slots. Every fact the old drawer held — leather in, lining in, kit issued, received, sent — is a fact about the **piece**, and it now lives there.

| Part | File |
|---|---|
| HTTP routes | `app/modules/store/router.py` |
| Business rules | `app/modules/store/service.py` |
| Completeness rules | `app/core/kit_rules.py` |
| Entry gate + labels | `app/core/store_display.py` |
| Lining verdict | `app/core/lining_rules.py` |
| Columns | `piece.store_state`, `leather_in`, `lining_in`, `accessories_in`, `store_entered_at`, `store_received_at`, `store_sended_at` |

## Why the drawer went away

There were 200 physical drawers. A style releases 100+ garments, stalls partway down the chain, and the next 50 have nowhere to go. Re-allocating by hand was complicated enough that **in practice it did not happen**, so pieces sat on a "waiting for a drawer" list instead of moving — and the merge gate then refused to line-stitch them, because a piece with no drawer could not be proven complete.

The drawer earned none of that. A state on a garment has **no capacity** and cannot run out.

---

# 2. Two scans, not three

```
   scan the WORKER   ->   scan the GARMENT   ->   done
```

The old flow was worker → **drawer** → piece, with a 409 when the garment was not the one that drawer had been assigned at upload. That third scan existed only to find a numbered box, and that rejection existed only to police an assignment the system had invented. Both are gone.

`POST /api/v1/store/scan`

```json
{ "employee_barcode": "EMP-000123", "piece_barcode": "PC-23456A" }
```

- Barcodes are resolved to ids **in the router**, so the service sees ids only — the same split the production log uses.
- The **login** signs the audit row; the **scanned worker** is recorded on the movement. They are two different people in two different tables: the login says who is accountable, the card says whose hands the part passed through.

---

# 3. `part` — what is being scanned in

`part` is `LEATHER`, `LINING` or `ACCESSORY`. **Leave it out and the server infers it**, in this order:

1. a `LINING_CUTTING` event exists and the lining is not in → **LINING**
2. `PASTING` is done and the leather is not in → **LEATHER**
3. neither is ready → whichever side is empty, so the entry gate can reject it **by name** rather than arbitrarily
4. both already in → **409**, there is nothing left to put in

> Rule 2 asks for **PASTING**, not "any leather-side event". Accepting LEATHER_CUTTING or FUSING is how a piece that was cut but not pasted got stored as leather, and the merge gate then opened on it.

## ACCESSORY is NEVER inferred

That is a safety rule, not a convenience. Inferring LEATHER vs LINING can at worst set the wrong boolean, and a human can undo that. An inferred **ACCESSORY spends stock** — it decrements every accessory lot on the style's recipe — so a wrong guess would move money nothing on the floor asked to move.

**To issue a kit you must send `"part": "ACCESSORY"` explicitly.**

## The entry gate

A part can only be scanned in once its own side is ready:

| Part | Requires |
|---|---|
| LEATHER | `PASTING` is done |
| LINING | `LINING_CUTTING` is done |

---

# 4. The seven states

```
WAITING ──► MERGED ──► HOLDING_LEATHER ─┐
                   └─► HOLDING_LINING ──┴──► HOLDING_BOTH ──► RECEIVED ──► SENDED
```

| State | Meaning |
|---|---|
| `WAITING` | not in the store yet — still on the cut side |
| `MERGED` | expected in the store; nothing has physically arrived (an accessory scan before either part) |
| `HOLDING_LEATHER` | leather stored, lining not yet |
| `HOLDING_LINING` | lining stored, leather not yet |
| `HOLDING_BOTH` | both parts stored |
| `RECEIVED` | confirmed complete — leather + lining + kit as required |
| `SENDED` | released to line-stitching |

The state is **derived from what is physically in**, every time, from `leather_in` and `lining_in`. It is never typed.

> The values are the old drawer values deliberately. The migration copied `drawer.state` straight across, so a garment mid-store kept its exact position and every dashboard filter and analytics bucket kept working.

---

# 5. Complete vs auto-received — two different questions

These look alike and are not. Getting them confused is the classic bug in this area.

## `piece_complete` — "is anything still owed?"

```
leather_in AND (lining_in OR NOT needs_lining) AND (accessories_in OR NOT kit_required)
```

A **leather-only** garment is complete on its leather alone. A style with **no accessory recipe** is complete without a kit.

This is the predicate behind **send**: an unkitted garment must not leave the store.

## `auto_receive_ready` — "may it advance to RECEIVED with nobody confirming?"

```
leather_in AND lining_in AND (accessories_in OR NOT kit_required)
```

**Stricter on purpose**, and the difference is scar tissue. Auto-receive first fired on completeness, which let a piece flagged `needs_lining: false` jump to RECEIVED the moment its leather was stored — over an **empty lining side**, on the strength of a flag known to be wrong for **925 of 1,425 pieces in one live order**.

Two booleans set by two physical scans are not a guess, so auto-receive asks for **both parts literally present**. For a style with no accessory recipe it reduces exactly to `leather_in and lining_in`.

---

# 6. The kit rides on every scan

An ACCESSORY scan returns what it just issued. **Any other scan returns the current checklist anyway.**

Why: the operator standing there with the lining in his hand is the person best placed to fetch the buttons too, and he will not go and look up a second screen to find out they are owed.

`kit_status` values:

| Value | Meaning |
|---|---|
| `NOT_REQUIRED` | the style declares **no accessories at all**. Hide the checklist — this is not "nothing issued yet". |
| others | the garment's standing against its accessory recipe |

---

# 7. Releasing — `POST /store/send`

```json
{ "piece_barcodes": ["PC-23456A", "PC-23456B"] }
```

This is the **only gate LINE_STITCHING has**. Until a garment is `SENDED`, the production log refuses to line-stitch it.

- **Partial accept.** One incomplete garment must never lose the forty complete ones selected with it. The rejected ones come back named, each with what it is still missing.
- **Re-sending an already-sent piece is a no-op**, not an error.
- **The gate is completeness, not a prior RECEIVED.** A garment that is complete has everything it will ever get; requiring a separate RECEIVED tap would only add a click that auto-receive already performs.
- When a complete garment is sent without ever passing through RECEIVED, `store_received_at` is **backfilled** — it *was* complete.

Read `sent`, `not_ready` and `not_found`. `not_ready[]` entries carry `{piece, missing, state, reason}`, and `reason` is a whole sentence ready to display — including *why* the garment takes a lining, when that is what is missing.

---

# 8. Reading the store

| Endpoint | Use |
|---|---|
| `GET /store/pieces` | what is in the store now. Filter by `state` and `style_id`; paged with `limit` / `offset` / `total`. |
| `GET /store/pieces/{piece_code}` | **WHERE IS THIS GARMENT** — the lookup page |
| `GET /store/pieces/{piece_code}/materials` | everything merged into this garment |

## The lookup is deliberately wide

The DM assigns somebody to place garments in the store, and that person has **no DM login**. Under the old routes they had no way to look a barcode up at all. Reading where a garment is tells nobody anything they should not know; refusing it just sends them to find the DM.

**Writing is still narrow:**

| Action | Roles |
|---|---|
| `scan`, `send` | MD, DM, Stitching Manager, **Store Manager** |
| reads | + Cutting, Lining, Supervisor, HR |

A cutting or lining manager **hands the part over**; they do not receive it.

## `next_action` — one sentence for the operator

Every scan response carries it:

- *"Waiting for its leather — scan that in when it is pasted."*
- *"Waiting for its lining — scan that in when it is cut."*
- *"Waiting for its accessories — scan the kit to issue them."*
- *"Complete. Send it to open line-stitching."*
- *"Sent to line-stitching."*

Show it. It is the whole answer to "what do I do with this garment?".

## `GET /store/pieces/{code}/materials` — four blocks

| Block | What it holds |
|---|---|
| `applies` | the recipe for **this colourway at this size** — leather, lining and every accessory line with `issued_qty` and `outstanding` |
| `not_applicable` | the style's **other** lines, each saying why it is not this garment's: `other_sku` (another colourway), `other_size` (an L zip is not an S zip), `zeroed` (this colourway takes none) |
| `issued` | the ledger — what physically went in, from which lot, when, through whose card, **manual corrections included** |
| `consumed` | the leather actually recorded at the cut. It lives on the production event, not the issue ledger, and is merged here so no screen has to know there were two writes |

> Read `not_applicable` when a kit scan says there is nothing to issue while `/material-spec/requirement` shows a full recipe. Both are true: that view is **style-wide**, a kit is issued **per garment**.

---

# 9. Correcting something after the fact

The store does not have an "undo". To record material that actually went into a garment outside its recipe, use **`POST /materials/issues`** — the Store Manager can call it, precisely because the correction is made at the store. See the Materials guide.

---

# 10. Notes for backend developers

- **Every store transition is a read-modify-write**, so the piece row is locked with `SELECT … FOR UPDATE` (`get_piece_for_update`). Two operators scanning the same garment at the same instant — leather into one terminal, lining into another — would both read `HOLDING_NONE`, both compute a single-part state, and the second write would win. The garment would be recorded holding one part while physically holding both, and the merge gate would then block a piece that is actually complete. SQLite ignores row locks, which is fine for single-writer tests; this protects Postgres, where two terminals really are concurrent.
- **One transaction per scan.** Stock movements, ledger rows, the piece's flags and the audit row land together or not at all — a half-issued kit is worse than an unissued one, because nothing downstream can tell them apart.
- **`getattr(part, "value", part)` is not decoration.** Since Python 3.11, `str()` on a `str` enum member returns `"StorePart.LEATHER"`, so an internal caller passing the enum got a 422 telling them to send one of the three values they had just sent.
- **The four pure rules were relocated, not rewritten**: `piece_complete`, `auto_receive_ready`, `STORE_ENTRY_STAGE`, and the lining verdict. Rewriting them would have put the store's hardest-won rules back at risk.
- **The read cache is busted in the same breath as the commit**, not on a timer — a manager who sends a batch expects the dashboard to agree immediately.
