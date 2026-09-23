# 1. What this service is

**Read-only reporting.** Analytics owns **no tables** and never writes anything. Every number it returns is computed live from the production events, the pieces and the rates.

Its job is the **drill-down**:

```
   /analytics/explorer                    client → order → style → piece
          |
          v
   /analytics/orders/{order_id}/tree      the order and its styles
          |
          v
   /analytics/styles/{style_id}/detail    the style and its pieces
          |
          v
   /analytics/pieces/detail               one garment
   /analytics/pieces/{code}/story         one garment's whole life
```

| Part | File |
|---|---|
| HTTP routes | `app/modules/analytics/router.py` |
| Queries and composition | `app/modules/analytics/service.py` |
| Life story + consumption | `app/modules/analytics/barcode_ext.py` |

---

# 2. Three endpoints are gone

| Endpoint | Status | Use instead |
|---|---|---|
| `GET /analytics/overview` | **410 Gone** | `GET /dashboard/direct-manager`, or the per-stage dashboards |
| `GET /analytics/alerts/stage-spread` | **410 Gone** | `GET /dashboard/alerts` |
| `GET /analytics/alerts/freight-risk` | **410 Gone** | `GET /dashboard/alerts` |

**Why they moved:** the bottleneck and the alerts were the only parts of those pages anyone acted on, and a manager should not have to know a separate screen exists to find out their line is blocked. They now sit on the surface every manager already opens, readable by every manager role rather than the DM alone.

**Why 410 and not a deleted route:** a deleted route 404s, and a 404 on a screen that worked yesterday reads — to a frontend, and to a support call — as "the server is broken". **410 says the removal was deliberate**, and the message names the replacement.

---

# 3. Analytics is live; payroll is frozen. They differ on purpose.

This is the single most important thing to understand about this service.

| | Analytics | Wages |
|---|---|---|
| `GET /analytics/employee-rates` | a **live estimate** from today's events and today's rates | — |
| `GET /wages/runs/{id}` | — | the **frozen** document the cash was counted against |

Mid-period they **legitimately disagree**, and neither is wrong:

- analytics counts work logged up to this second;
- a closed wage run counts what it counted when it was computed, priced at the rates in force then.

The response of `/analytics/employee-rates` says so in its own `note` field. **Never present an analytics figure as a payslip.**

That endpoint is **DM, MD and HR only** — it is cost data.

---

# 4. Tenancy

Every endpoint threads `client_scope`: a `client` login is pinned to its own `client_id`; staff read across clients.

> A **cross-tenant id resolves to 404**, not to an empty result. Existence itself is information — telling a buyer "that order exists but is not yours" tells them the factory has another customer with that order number.

---

# 5. The big reads are capped, and they say so

Two endpoints used to return everything the factory had ever made. A single live style runs to **1,425 pieces and about 11,400 production events**.

## `GET /analytics/explorer`

- `include_pieces` defaults to `true`. Pass `false` for the skeleton alone.
- `pieces_per_style` caps the leaves (default 100, max 500).
- Each style always reports its **real** `piece_count`, plus `pieces_truncated` when leaves were cut off.

So the tree stays small and the counts stay true. **Read `piece_count`, never `len(pieces)`.**

## `GET /analytics/styles/{style_id}/detail`

Paged with `limit` / `offset`. The events are fetched **only for the pieces on the page**, while `totals`, `stages` and `store` stay **whole-style** — because those are facts about the style, and a manager reads them as such.

---

# 6. How to read the numbers

## `totals` — measured against what was ORDERED

| Field | Meaning |
|---|---|
| `qty_ordered` | what the client ordered |
| `minted_pieces` | how many barcodes exist |
| `completed` | through the final stage |
| `balance` | `qty_ordered − completed` |
| `not_minted` | ordered but never released |

> **The balance is against what was ordered, not what was minted.** A style nobody has released yet is still owed to the client. A balance that ignored it would report an order as complete while half of it had not been started.

## `stages` — `pending` is the BALANCE at that stage

```
pending = total − completed
```

That is what the floor means: a piece that has finished cutting and is waiting for fusing is **pending at fusing**.

It is deliberately **not** queue depth (upstream completed minus this stage's completed). With queue depth every stage has a different denominator and the columns stop adding up against the order.

## `store` — where the garments physically are

`buckets[]`, plus named counts: `holding_leather`, `holding_lining`, `holding_both`, `received`, `sended`, `in_store`, `awaiting_parts`.

`no_drawer` is **always 0**. There are no drawers. The key is kept only so nothing reading it breaks.

---

# 7. The piece life story

`GET /analytics/pieces/{piece_code}/story` is the barcode feature's headline read: **everything that ever happened to one garment.**

Each stage row carries:

| Field | Meaning |
|---|---|
| `stage`, `stage_label` | what was done |
| `employee_name` | who did it — **`null` for work done by an outside factory** |
| `work_date` | the day the work happened |
| `logged_at` | when it was recorded, which can be later |
| `entered_by` | the login that recorded it |
| `is_rework` | `true` when this stage had already been logged for this garment |
| `leather_consumption_dcm` | recorded at the cut; `null` everywhere else |

Plus `current_stage`, `store_state`, and **`awaiting`** — one sentence saying what the garment is waiting for:

- *"in the store, awaiting lining"*
- *"in the store, awaiting leather"*
- *"awaiting storage"*
- *"awaiting inspection sign-off"*

`awaiting` is `null` when the garment is not waiting on anything nameable.

> `work_date` and `logged_at` are different on purpose. Work done on Friday and entered on Monday is a Friday wage, and both facts are kept.

---

# 8. Consumption vs stock

`GET /analytics/consumption` sums **leather actually consumed** from the cut events, per style.

- `pieces_cut` is the count of **distinct garments** with a leather cut logged.
- `leather_consumed_dcm` is the sum recorded against those events.

It answers "what did this style really cost us in leather", which is not the same as "what does the recipe say it should cost" — that is `/styles/{id}/material-spec/requirement`.

---

# 9. Notes for backend developers

- **This module owns no tables and must never write.** If a figure needs a table, it belongs in the module that owns the data.
- **`employee-rates` pre-loads every rate in one query** and resolves them in Python. The alternative — a scalar subquery per row — is an N-query payroll report, and this is the endpoint a manager refreshes all day.
- **Its rate resolution mirrors `WageRepository.effective_rate` exactly.** If one changes, both must.
- **`_stage_progress` and `_totals` are shared** by the order level and the style level, so a style page is always a strict subset of its order page.
- **The 410 stubs are kept deliberately.** Delete them one release after the frontend stops calling them.
