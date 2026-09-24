# 1. What this service is

This service does two jobs.

1. **Login.** It checks a phone number and a password and gives back a token. Every other screen in KairoX needs that token.
2. **Logins list.** It creates and lists the user accounts (we call them *logins*).

> One sentence to remember: **a shop-floor worker has NO login.** Only office and manager people get an account. Workers are known by their barcode card, and somebody else enters their attendance for them.

## Where the code is

| Part | File |
|---|---|
| HTTP routes | `app/modules/users/router.py` |
| Business rules | `app/modules/users/service.py` |
| Database table | `app/modules/users/models.py` (table `app_user`) |
| Request / response shapes | `app/modules/users/schemas.py` |
| Token check + role check | `app/modules/users/deps.py` |
| Token make / read | `app/core/security.py` |
| Login speed limit | `app/core/throttle.py` |

---

# 2. The login flow, step by step

```
  USER                    FRONTEND                    BACKEND
   |                         |                           |
   | types phone + password  |                           |
   |------------------------>|                           |
   |                         | POST /api/v1/auth/login   |
   |                         |-------------------------->|
   |                         |                           | 1. is this IP asking too often?  -> 429
   |                         |                           | 2. find user by phone
   |                         |                           | 3. check password (bcrypt)
   |                         |                           | 4. make a JWT token (24 hours)
   |                         |    200 + token + role     |
   |                         |<--------------------------|
   | sees the right home page|                           |
   |<------------------------|                           |
```

### What the frontend must do after login

1. Save `access_token` (for example in memory or secure storage).
2. Send it on **every** later request as a header:
   `Authorization: Bearer <access_token>`
3. Read `role` from the login response and open the home screen for that role.
4. If `must_change_password` is `true`, force the change-password screen first.

### The token

- It is a **JWT**, signed by this server with HS256. Nothing else issues it.
- It lives for **24 hours** (`access_token_expire_minutes = 1440`).
- Inside it: `sub` (the user id), `role`, `name`, `exp` (expiry).
- There is **no refresh token**. When it expires, the user logs in again. Treat any **401** as "session over, go back to the login screen".

### Login is speed-limited — and only login

- **10 tries per minute per IP address** by default (`login_rate_limit_attempts`, `login_rate_limit_window_seconds`). Over that: **429**.
- No other endpoint is rate-limited. A barcode terminal doing 200 scans in a burst is a manager working through a trolley, not an attack.
- If the Redis cache is down, the limit **allows** the request. A cache outage must not lock the whole factory out.

### Two things that make wrong guesses safe

- A wrong phone number and a wrong password take **the same time** to answer. The server hashes a dummy password when the user does not exist, so nobody can discover which phone numbers are registered by timing the reply.
- The error message is the same for both cases: `Incorrect username or password`. **Never** show the user a different message for "no such user".

---

# 3. Roles

There are 13 roles. `role` is a fixed list (a real Postgres enum named `user_role`) — you cannot invent a new one from the API.

| Role | Value sent in JSON | What this person does |
|---|---|---|
| Managing Director | `managing_director` | Owner. Superuser. Passes every gate in the system. |
| Direct Manager | `direct_manager` | Runs operations: uploads breakdowns, manages clients and users, runs payroll. |
| Cutting Manager | `cutting_manager` | Logs leather cutting. |
| Lining Manager | `lining_manager` | Logs the lining cut. |
| Stitching Manager | `stitching_manager` | Logs every stage after the cut. |
| Store Manager | `store_manager` | Runs the store: scans parts in, releases garments to stitching. |
| Supervisor | `supervisor` | Reads the floor roster. |
| HR | `hr` | Employees, wages, attendance. |
| Security | `security` | Gate operator. Scans employee cards in and out. |
| Merchandiser | `merchandiser` | Client-facing coordinator. **Note:** no route gives this role anything yet. It can log in and will get 403 everywhere. |
| Client | `client` | External buyer, read-only on their own orders. No route creates this login today. |
| Viewer | `viewer` | Read-only office staff, for example the accountant. |
| Employee | `employee` | **LEGACY. Never created any more.** Old rows may still exist; they are blocked from every manager screen. |

## The two superusers

`managing_director` and `direct_manager` pass **every** normal role gate, even when the endpoint does not list them. So when a document says "Allowed roles: Store Manager", read it as "Store Manager, plus MD and DM".

There is one exception. A few endpoints use a **separation-of-duties** gate (BOM approve / reject / export). There the **Direct Manager is refused on purpose**, because the person who prepares a BOM must not be the person who approves it. The MD still passes.

---

# 4. Who may create which login

Creating a login is **not** the same as being allowed to call `POST /users`. Two checks run, one after the other:

1. **The door check (role gate).** Only `direct_manager`, `hr` and `managing_director` can call the endpoint at all. Anyone else gets **403**.
2. **The authority check (in the service).** You may only create a login **at or below your own authority**. Otherwise HR — whose job is employee admin — could create a Managing Director account and log into it.

| The caller is… | …may create these roles |
|---|---|
| Managing Director | every role that is allowed to have a login (all of the table above except `employee`) |
| Direct Manager | HR, Supervisor, Cutting Manager, Lining Manager, Stitching Manager, Store Manager, Security, Merchandiser, Client, Viewer |
| HR | Supervisor, Viewer, Security, Merchandiser, Cutting Manager, Lining Manager, Stitching Manager, Store Manager |

If you ask for a role outside your list you get **403** with a message that names exactly which roles you *are* allowed to create — show that message to the user.

If you ask for `employee` you get **422**, no matter who you are. Workers have no system access. Create them on `/employees` instead.

---

# 5. Passwords

| Rule | Detail |
|---|---|
| Storage | bcrypt, cost 12. The hash never leaves the database and is never in any response. |
| Default password | If the account is created without a password, the password becomes **the phone number** and `must_change_password` is set to `true`. |
| Minimum length | A new password must be at least **8 characters** (checked on change-password). |
| Cannot reuse the phone | Changing your password to your own phone number is refused with **400**. This stops the forced change putting the weak default straight back. |
| Change your own password | `POST /auth/change-password` with the current and the new one. It answers **204** (success, no body). |
| Forgotten password | There is **no** self-service reset in Phase 1. HR / DM / MD creates a new login or the DBA resets it. |

---

# 6. What happens on every other request

Every protected route runs the same two steps before your code runs:

1. `get_current_user` — read the `Authorization: Bearer <token>` header, verify the signature, load the user row, check `is_active`. Bad, missing, expired token, or a deactivated user → **401**.
2. `require_roles(...)` — check the role. Wrong role → **403**.

Most routers are also wrapped in `block_employees`, which refuses the legacy `employee` role. `auth`, `users`, `attendance` and `GET /barcode/resolve` are deliberately **not** wrapped, because the attendance screen must stay reachable.

### For the frontend: what each status means

| Status | What happened | What the screen should do |
|---|---|---|
| 401 | No token, bad token, expired token, or the account was switched off | Log out and show the login screen |
| 403 | Logged in, but the wrong role | Show "you do not have permission", do **not** log out |
| 409 | Phone or email already registered | Mark that box in the form |
| 422 | A field is missing or wrong, or you asked for a role that gets no login | Mark the field; show `detail` |
| 429 | Too many login attempts from this IP | "Too many attempts, please wait a minute" |

---

# 7. The `app_user` table

| Column | Meaning |
|---|---|
| `id` | UUID, made by the application (never by the database) |
| `name` | Display name |
| `phone` | **The login identifier.** Unique. |
| `email` | Optional, unique when present |
| `password_hash` | bcrypt hash. Never returned by any endpoint. |
| `role` | One of the 13 values. Stored as a real Postgres enum. |
| `is_active` | `false` shuts the login out immediately — the next request gets 401 |
| `must_change_password` | `true` forces the change-password screen |
| `employee_id` | Set only for **staff** who also have an employee record (for their own attendance and wages). Never sent in a request body. |
| `client_id` | Set only for a `client` login, so they see only their own orders |

> **For backend developers:** `role` is a **native Postgres enum**. Adding a role to the Python enum is not enough — a migration must add the label to the database type, and it must be the **member NAME in uppercase** (`'STORE_MANAGER'`), not the lowercase value. This has bitten the project three times. See `CLAUDE.md` §13.

---

# 8. Things that surprise people

- **`username` is a phone number.** Not an email. The field is called `username` because that is the standard OAuth2 name.
- **There is no `GET /users/{id}`, no update, no delete.** Phase 1 has list and create only. To disable a login, set `is_active = false` in the database.
- **`POST /users` does not take an `employee_id`.** An employee is not a user. When a *staff* member needs both an employee record and a login, that is created on the `/employees` path in one transaction, and the system makes the link itself.
- **There is no route that creates a `client` login.** It was removed on purpose: a buyer does nothing inside Phase 1, and with no door to create the role, the cross-tenant scoping code can never be reached. The implementation is still there, commented out, for Phase 2.
- **The user list is paged.** `GET /users` returns the standard page envelope — `{items, total, limit, offset, count, has_more}` — not a bare array. `limit` is 1–200 and defaults to 50; ask for the next page with `offset = offset + limit`.
