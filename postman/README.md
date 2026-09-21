# KairoX — Postman collections

**One file per service, plus one shared environment.** Import the whole folder
(Postman → Import → Folder), or just the one you are working on.

```
python scripts/make_postman_per_service.py
```

Re-run that after changing any route. **Do not edit these files by hand** — they
are generated from the app's own `/openapi.json`, and a hand-edited collection is
a second copy of the contract that drifts the moment a route changes. A stale
collection is worse than none: it fails in ways that look like server bugs.

Last regenerated against **224 live endpoints across 25 tags** — every route the
app serves is in here, and nothing in here is a route the app no longer serves.

---

## Start here

1. Import the folder, then **select the `KairoX · local` environment**
   (top-right dropdown).
2. Open **`user` → Login**, put in a phone and password, send it.
3. That is the whole setup. Login's test script stores `access_token` for you,
   and every request in every collection inherits bearer auth from it.

**Selecting the environment is not optional if you use more than one
collection.** A collection variable is only visible to the collection that set
it, so a login run in `user` leaves the other seventeen on an empty token and
every request in them 401s — which reads as a broken auth guard rather than a
missing paste. The environment is the shared slot the token goes into. With no
environment selected, login still works, but it authorises `user` alone.

`base_url` defaults to `http://127.0.0.1:8000` and lives in the same environment.

Optional query parameters are included but **disabled**, so every request runs
as-is the first time. File uploads are real form-data rows — click **Select
Files** on the `file` row.

---

## What is in each file

### Phase 1 — the floor

| File | Requests | What it is |
|---|--:|---|
| `user` | 5 | Login and the logins themselves. **Start here.** |
| `employee` | 6 | The roster, one worker, and the card. Workers get **no login**. |
| `attendance` | 11 | The gate scan, the manual fallback, and the corrections. |
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

### Phase 2 and the app itself

| File | Requests | What it is |
|---|--:|---|
| `procurement` | 65 | The auto-generation pipeline, foldered **by stage**: intake → BOM → inventory → supplier PO. |
| `system` | 5 | `/health`, `/ready`, `/`, and the chatbot. No token needed. |

`procurement`'s folders are in stage order rather than alphabetical order on
purpose: the stages only work in order, and you cannot check inventory against a
BOM you have not generated yet. Sorting the folders would put Stage 4 above
Stage 2/3 and read as if that were the running order.

---

## Four things the collections will not tell you

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

**The big list reads are paged now, and some counts are not.** `limit` / `offset`
are present but disabled on the list endpoints, defaulting to 50 rows. Where a
header count would mislead if it were page-scoped it stays collection-wide — on
`GET /production/skus/{id}/pieces` the `total` / `done` / `pending` / `closed`
counts are SKU-wide while `blocked` is a page figure and says so in
`blocked_scope`. Do not add them up across pages.

---

## Routes that were removed

These were in an earlier copy of this folder and are **gone from the app**
(removed 2026-09-19). They are commented out in their routers with the reason,
not deleted. If you have an old collection imported, delete it rather than
reporting the 404s:

| Gone | Why, and what to use |
|---|---|
| `GET /api/v1/attendance/me/status` | The live shift countdown. **No replacement** — the only people who hold a login are gate operators, and the floor it would suit has no login to see it with. `GET /api/v1/attendance/me` still returns a staff login's own history. |
| `GET` + `PATCH` `/api/v1/attendance/config` | Shift policy is configured once, server-side. The write went with the read on purpose: an edit form that cannot load its current values overwrites policy with whatever the frontend held. The policy still applies on every punch and in the wage run. |
| `POST /api/v1/users/clients` | The only way to mint a `CLIENT` login, and Phase 1 gives a buyer nothing to do inside the app. Removing the door means the cross-tenant scoping branches cannot be reached at all. `POST /api/v1/clients` creates the client *record* — that is a different thing and still exists. |
| `/api/v1/drawers/*` | Unrouted, not renamed. Use `store.postman_collection.json`; the `drawer` file's description maps each old call to its replacement. |
