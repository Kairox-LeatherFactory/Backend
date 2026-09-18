# KairoX — Postman collections

**One file per service.** Import the whole folder (Postman → Import → Folder),
or just the one you are working on.

```
python scripts/make_postman_per_service.py
```

Re-run that after changing any route. **Do not edit these files by hand** — they
are generated from the app's own `/openapi.json`, and a hand-edited collection is
a second copy of the contract that drifts the moment a route changes. A stale
collection is worse than none: it fails in ways that look like server bugs.

---

## Start here

1. Import **`user.postman_collection.json`** and run **Login**.
2. Copy `access_token` from the response.
3. Paste it into the `token` variable of whichever collection you are using.
   Every request inherits bearer auth from the collection, so you do this once.
4. `base_url` defaults to `http://127.0.0.1:8000`.

Optional query parameters are included but **disabled**, so every request runs
as-is the first time.

---

## What is in each file

| File | Requests | What it is |
|---|--:|---|
| `user` | 6 | Login and the logins themselves. **Start here.** |
| `employee` | 6 | The roster, one worker, and the card. Workers get **no login**. |
| `attendance` | 14 | The gate scan, the manual fallback, and the corrections. |
| `client` | 8 | Buyers, their orders, and the styles under them. |
| `import` | 9 | Breakdown preview → commit → edit → **release** (the mint). |
| `barcode` | 10 | `resolve` is every scan's front door. |
| `material` | 24 | Lots, stock, receiving with per-hide sheets, the style recipe. |
| `cutting` | 11 | The grid that replaced the cutting manager's spreadsheet. |
| `production` | 11 | `POST /production/log` — the whole floor's logging surface. |
| `store` | 5 | Employee + piece, then send. Replaces the drawer. |
| `inspection` | 6 | Reject and rework, against the **responsible** stage. |
| `jobwork` | 5 | Garments at an outside factory, and what they cost. |
| `wage` | 16 | Rates, runs, the ledger. A closed run is frozen. |
| `dashboard` | 22 | The per-role manager screens. All reads. |
| `analytics` | 10 | Overview, explorer, alerts, one garment's life story. |
| `drawer` | 2 | **WITHDRAWN** — the file explains what replaced it. |

---

## Three things the collections will not tell you

**Check the body, not the status code.** Several endpoints accept partially:
`POST /production/log` returns `200` with `logged: []` and a populated
`sequence_blocked` / `merge_blocked` / `offsite_blocked`; `POST /store/send`
returns `200` with `count_sent: 0` and everything in `not_ready`. One bad piece
never loses the good ones scanned with it, which is deliberate — and it means a
green tick in Postman does not mean the work was recorded.

**`{{employee_id}}` is a worker, not a login.** Shop-floor workers have no
`app_user` row at all. Anywhere a request wants the *actor*, it wants the login
from your token; anywhere it wants the *worker*, it wants the employee id or the
barcode on their card. They are different tables and mixing them is the mistake
that has produced a 500 here before.

**Some corrections carry their arguments in the query string**, not the body:
`PATCH /production/events/{id}/reassign?employee_id=&reason=` and
`DELETE /attendance/{id}?reason=`. The generated requests already have them as
disabled query params — enable and fill them.
