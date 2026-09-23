# 1. What this service is

This is the **roster** — every person on the payroll. Workers on the floor, and staff in the office.

The single most important idea:

> **A shop-floor worker has NO login.** No phone needed, no email, no password, no `app_user` row. They are a payroll and production identity with a **scannable card**, and nothing more. Somebody else (Security, HR, MD or DM) scans that card to check them in.

A worker is not a user. A user is not a worker. They are two different tables joined only when a **staff** member happens to be both.

| Part | File |
|---|---|
| HTTP routes | `app/modules/employees/router.py` |
| Business rules | `app/modules/employees/service.py` |
| Table | `app/modules/employees/models.py` (`employee`) |
| Request/response shapes | `app/modules/employees/schemas.py` |
| Card reissue / deactivate | `app/modules/barcode/router.py` (mounted under `/employees`) |

---

# 2. Creating a person

`POST /api/v1/employees` — HR, Direct Manager or Managing Director.

There are **two kinds of create**, and one field decides which: `role`.

## A. A worker (the common case) — omit `role`

```json
{ "name": "Ramesh", "designation": "shell tailor", "wage_type": "piece_rate" }
```

That is the whole body. No phone, no email, no password.

- Sending a `password` without a `role` is a **422**: *"password is only accepted when a staff login is created — workers do not log in"*.
- Sending `"role": "employee"` is a **422**. That role is legacy and is never assignable.
- `wage_type` does **not** create a login. A MONTHLY worker is still just a worker, paid differently. (This used to auto-create an `employee` login. That was removed.)

## B. A staff member — pass `role`

```json
{ "name": "Priya", "designation": "supervisor", "wage_type": "monthly",
  "role": "supervisor", "phone": "9000000123", "password": "firstpass123" }
```

- `phone` **and** `password` are then required (**422** naming the missing one).
- The login is minted in the **same transaction** as the employee row, and `must_change_password` is set to `true`.
- Allowed roles here: **HR, Supervisor, Cutting Manager, Lining Manager, Stitching Manager, Security, Merchandiser**.
- **Direct Manager and Managing Director cannot be created here** — they are created through user-creation. **422**.
- You may only grant a role you are allowed to grant (the same authority table as `POST /users`). Otherwise **403**.

## Every new person gets a card

Whichever path you took, the response contains `employee_barcode` — for example `EMP-000123`. It is minted in the same transaction, so a worker can never exist without a card. **Print the card straight from this response.**

```json
{
  "id": "…", "name": "Ramesh", "designation": "SHELL_TAILOR",
  "wage_type": "piece_rate", "is_active": true,
  "phone": null, "email": null, "role": null,
  "employee_barcode": "EMP-000123",
  "user_created": false, "login_phone": null
}
```

`user_created` tells the screen whether a login was made; `login_phone` repeats the phone to show on the "give them these credentials" panel.

---

# 3. Two rules that are not preferences

## Rule 1 — designations are always UPPERCASE

`shell tailor`, `Shell-Tailor` and ` SHELL TAILOR ` all become **`SHELL_TAILOR`**. Spaces, hyphens and slashes become `_`.

This is not tidiness. Production's **skill gate** does a set-membership test on the designation. Three spellings of one job title means three different skills and a gate that checks nothing.

The catalogue: `CUTTER`, `LINING_CUTTER`, `FUSER`, `PASTER`, `LINE_TAILOR`, `SHELL_TAILOR`, `FINISHER`, `INSPECTOR`, `PACKER`, `TAILOR`, `HELPER`, `SUPERVISOR`, `TRIMMER`, `CHEMICAL_TECHNICIAN`, `SECURITY`, `MERCHANDISER`, `STITCHING_INSTRUCTOR`, `QC_INSPECTOR`.

> An **unknown** designation is not rejected. It is upper-cased and kept. The seeded data carries job titles nobody has catalogued yet, and failing `create` on an unrecognised title would block HR. The skill gate treats an uncatalogued designation as unknown and **lets it pass** — it tightens as HR backfills the vocabulary.

## Rule 2 — names are unique

Two people called `RAMESH` is a wage sent to the wrong envelope: a manager picks the wrong row from a dropdown and the piece money lands with the wrong person.

On a collision the **new** employee is stored with a prefix:

| Attempt | Stored as |
|---|---|
| `RAMESH` is free | `RAMESH` |
| `RAMESH` is taken | `IN-CHAL RAMESH` |
| both taken | `IN-CHAL-2 RAMESH` |

Comparison ignores case and extra spaces. **The existing row is never renamed** — it is already printed on wage slips and referenced inside closed payroll runs.

After about 100 collisions on one name you get a **409** asking for a distinct name to be entered by hand.

---

# 4. Who can see salary

`monthly_salary` is returned **only to HR, Direct Manager and Managing Director**. Everyone else gets the same row without that field. The server swaps the response model — it is not something the frontend has to hide.

The **whole roster is internal**: a `client` or `viewer` login gets **403** on both the list and the single read. Redacting salary alone was not enough, because the row still carries phone and email.

| Caller | `GET /employees` and `GET /employees/{id}` |
|---|---|
| HR, DM, MD | full row **including** `monthly_salary` |
| Cutting / Lining / Stitching / Store Manager, Supervisor, Security | row without salary |
| Client, Viewer | **403** |

---

# 5. Editing and removing

## Edit — `PATCH /employees/{employee_id}` (HR, DM, MD)

Only these fields can be written here: `name`, `designation`, `wage_type`, `monthly_salary`, `phone`, `email`, `is_active`. Anything else is a **422** that names the rejected fields.

- A changed `designation` is re-normalised to uppercase.
- A changed `name` re-runs the uniqueness check (and may come back with an `IN-CHAL` prefix).
- Passing `role` + `password` **grants a staff login** to an existing employee. It needs a phone on the record (**422** otherwise), the employee must not already have a login (**409**), and you must be allowed to grant that role (**403**).

## Remove — `DELETE /employees/{employee_id}` (DM or MD only — **not** HR)

It is a **soft delete**. Two things happen in one transaction:

1. `is_active` becomes `false`.
2. The employee's **card is retired**, so scanning it answers **410 Gone**.

Nothing else is touched. Every production event and every wage line stays exactly where it is.

```json
{ "employee_id": "…", "active": false, "history_preserved": true }
```

Calling it again on an already-inactive person answers `{"employee_id": "…", "active": false, "already_inactive": true}` — the two cases are genuinely different, and the screen can say which happened.

> A hard delete does not exist, by design. It would orphan wage lines inside **closed** payroll runs — documents the cash was counted against.

---

# 6. The card

The card code is on **every** roster row (`employee_barcode`), because the roster screen is also the barcode screen: you click a person to reissue or retire their card, and you have to see which card you are retiring.

`employee_barcode` is `null` when the card was retired (a leaver) or was never issued. A retired code is not scannable, so showing it would invite a scan that answers 410.

Reissue and deactivate live on `PATCH /employees/{employee_id}/barcode` — the same path, but it belongs to the **Barcode** service. See that guide, or the Barcode API Reference. It is included in this service's Postman collection because that is the screen the person is standing on.

| Action | When | Effect |
|---|---|---|
| `{"action": "reissue"}` | card lost or damaged | old code retired, new one minted, history untouched |
| `{"action": "deactivate"}` | worker leaves | code retired; the person and all their records remain |

---

# 7. Lists and paging

`GET /employees` returns the standard page envelope: `{items, total, limit, offset, count, has_more}`. `limit` is 1–200, default 50.

`active_only` defaults to `true`. Pass `active_only=false` to include leavers.

The card codes for the **whole page** are fetched in one extra query, not one per row.

---

# 8. Notes for backend developers

- **The employee, the login and the barcode are one transaction.** `repo.create()` only flushes; the commit happens at the end of `service.create()`. If the login fails, the barcode and the employee roll back with it.
- **`role` is not a column on `employee`.** It lives on `app_user`. `EmployeeRead.role` has nothing to read off the ORM row, so it is filled in explicitly (`login_role_for`) — including on the response to the very PATCH that just granted the login.
- **`update()` enumerates its writable fields on purpose.** A blanket `setattr` loop over the patch would make `is_active` and `monthly_salary` writable by anything that reaches the method.
- **The name walk is one query.** `colliding_names()` returns every name the new one could collide with, and the rest is in-memory string work. It used to run one SELECT per candidate — up to 100 round trips for a common name.
- **The uniqueness race is guarded by the database.** The unique index on `lower(name)` is the real protection; the walk exists to produce a *meaningful* name rather than a 409.
