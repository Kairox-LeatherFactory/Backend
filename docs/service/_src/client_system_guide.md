# 1. What this service is

This is the **top of the data tree**. Everything the factory makes hangs off a client.

```
Client                    the buyer            e.g. "BOGGI"
 └── ClientOrder          one order sheet      order_number "NB-B-1"
      └── Style           one garment design   "CLERMONT", article "GOAT SUEDE"
           └── SKU        colour + size + qty  "DARK BROWN / 46, 120 pcs"
                └── Piece ONE physical garment  seq 1..120
```

Read that picture again — it explains most of KairoX. **The piece is the tracked unit.** Its code is the barcode printed on the garment.

This service owns the first four levels. The **pieces are created by the Breakdown Import service**, not here.

| Part | File |
|---|---|
| HTTP routes | `app/modules/clients/router.py` |
| Business rules | `app/modules/clients/service.py` |
| Database queries | `app/modules/clients/repository.py` |
| Tables | `app/modules/clients/models.py` (`client`, `client_order`, `style`, `sku`) |
| Code makers | `app/modules/clients/utlis.py` |

---

# 2. How a client gets into the system

## Step 1 — create the client

`POST /api/v1/clients` — **Direct Manager only** (MD passes too).

It needs three things: `name`, `country` (optional) and `order_number`.

> **Important:** creating a client also creates its **first order** in the same call. There is no "client with no order" state. That is why `order_number` is required in the body.

The response is the client **plus** the order that was minted for it:

```json
{
  "id": "3f8c...", "name": "BOGGI", "country": "IT",
  "code": null, "currency": null, "default_size_system": null,
  "is_active": true,
  "order_number": "NB-B-1",
  "order_id": "7a1c..."
}
```

`order_number` is **globally unique across every client**. If it is already used anywhere, you get **409** with the message *"Order number 'X' already exists. Choose a unique one."* Show that on the order-number box.

## Step 2 — add more orders later

`POST /api/v1/clients/{client_id}/orders` — Direct Manager only.

Only `order_number` is required. The other fields are order-level facts that matter later:

| Field | Why it matters |
|---|---|
| `order_date` | reporting |
| `delivery_deadline` | the date the analytics freight-risk alert counts back from |
| `sea_cutoff_date` | the last day the order can still go by sea |
| `ship_mode` | `"sea"` or `"air"`. Default `"sea"`. |
| `currency`, `agent`, `line` | commercial detail carried on the order |

The response of this call always has `"styles": []` — a brand-new order has no styles yet. Styles arrive with the **breakdown upload**, not from this endpoint.

## Step 3 — styles and SKUs

You do **not** create styles or SKUs through this service. They are created when the Direct Manager uploads the **breakdown sheet** (see the Breakdown Import guide). After that upload they appear inside `GET /clients/{client_id}/orders`, nested under their order.

---

# 3. The codes (read this before you build any screen)

Codes are **deterministic**: the same order + style + colour + size always produces the same code. That is what makes a re-import safe — it matches what is already there instead of creating a duplicate.

| Level | How the code is built | Example |
|---|---|---|
| Style | `ORDER-STYLE` or `ORDER-STYLE-ARTICLE` | `JP-CLERMONT_VEST-GOAT_SUEDE` |
| SKU | `style_code-COLOUR-SIZE` | `JP-CLERMONT_VEST-GOAT_SUEDE-DARK_BROWN-46` |
| Piece | `sku_code-seq` with 3 digits | `JP-CLERMONT_VEST-GOAT_SUEDE-DARK_BROWN-46-001` |

Rules the maker follows: everything is UPPERCASE, and any run of characters that is not a letter or a number becomes a single `_`.

> **Never split a code by position.** The article segment is left out when the style has no article, so a code has a variable number of `-` parts. Codes are matched **whole** or **by prefix**. `code.split("-")[2]` will break.

The style code is always the exact **prefix** of every SKU code under it, and the SKU code is the prefix of every piece code. That is how a manager holding a printed traveller can read the style off the front of it.

---

# 4. Deactivate, do not delete

This is the rule that trips people up the most.

| You want to… | Do this |
|---|---|
| Hide a client who has finished trading | `PATCH /clients/{id}` with `{"is_active": false}` |
| Remove a client typed in by mistake | `DELETE /clients/{id}` |

`GET /clients` hides inactive clients by default. Pass `include_inactive=true` to see them.

## When DELETE is refused

A delete is refused with **409** if the client has any record of **real work**. An order or a style on its own is *not* real work — those cascade away with the client, because creating a client always makes an order and a breakdown upload always makes styles.

These are the blockers, each counted and named in the error message:

| Blocker | Meaning |
|---|---|
| `piece` | garments were minted with printed barcodes |
| `production_event` | production was logged |
| `rate` | a wage rate card exists on one of their styles |
| `style_operation` | a style/operation routing row exists |
| `wage_line_detail` | a wage was already paid against one of their styles |
| `supplier_po_line` | a Phase-2 purchase-order line points at a style |

## Ask before you press

`GET /clients/{id}/deletable` runs **exactly the same check** and changes nothing. Use it to grey out the Delete button and say why:

```json
{
  "client_id": "3f8c...",
  "deletable": false,
  "blockers": [
    {"table": "piece", "count": 240, "what": "garment(s) minted with printed barcodes"}
  ],
  "alternative": "PATCH /clients/3f8c... {\"is_active\": false}"
}
```

When `deletable` is `true`, `blockers` is `[]` and `alternative` is `null`.

---

# 5. Editing a client

`PATCH /clients/{id}` is a **partial** update, Direct Manager only. Only the fields you actually send are written.

Three states, three different requests — do not mix them up:

| What you send | What happens |
|---|---|
| field not in the body | left exactly as it was |
| field sent as `null` | cleared |
| field sent with a value | overwritten |

- `name` cannot be set to an empty string → **400**.
- `code` must be unique across all clients → **409** if another client already uses it.
- You **cannot** edit order fields here. An order is edited through its own endpoints, and `order_number` is globally unique, so it must never travel on a client edit.

---

# 6. Client-role logins see only themselves

A login whose role is `client` is pinned to its own `client_id` on every read in this service:

| Endpoint | What a `client` login gets |
|---|---|
| `GET /clients` | only their own row — and `total` counts only that row |
| `GET /clients/{id}` | 403 if the id is not theirs |
| `GET /clients/{id}/orders` | 403 if the id is not theirs |
| `GET /clients/styles` | only their styles, even if they pass another client's `order_number` |

The narrowing happens **inside the database query**, not as a filter on the result. That matters for paging: filtering after the page is built would return one row next to a `total` that counted every other customer.

> Today **no endpoint creates a `client` login**, so this scoping is not reachable in practice. It is kept and tested because Phase 2 may open a client portal.

---

# 7. Lists and paging

`GET /clients`, `GET /clients/{id}/orders` and `GET /clients/styles` all return the standard page envelope:

```json
{ "items": [...], "total": 318, "limit": 50, "offset": 0,
  "count": 50, "has_more": true }
```

- `limit` is between 1 and 200, default 50.
- Next page: `offset = offset + limit`.
- `total` is the size of the whole result, not of this page — use it for "showing 1–50 of 318".
- `has_more` is calculated by the server, so no client has to get that off-by-one right.

`GET /clients/styles` is the **style picker** feed: a light list for dropdowns, filtered by `order_number` and/or `client_id`.

---

# 8. Notes for backend developers

- **Route order in `router.py` is load-bearing.** FastAPI matches in registration order, so `GET /clients/styles` must be declared **before** `GET /clients/{client_id}`. Otherwise `"styles"` is read as a UUID and the request 422s instead of reaching the styles handler. Every `/{client_id}` route is deliberately at the bottom of the file. Keep it that way.
- **Uniqueness is checked twice on purpose.** A friendly look-up first (so the message is readable), and the database constraint as the real guarantee. The pre-check races; the `IntegrityError` catch is what makes it correct.
- **`POST /clients/{id}/orders` builds its response by hand** instead of reading `order.styles`. A fresh order has no styles, and touching that relationship would trigger a lazy load that is not safe in async SQLAlchemy.
- **Code makers live in `utlis.py` only.** `service.py` re-exports them. There used to be a second identical copy, and two importers each used a different one — so a format change would have landed in half the system.
