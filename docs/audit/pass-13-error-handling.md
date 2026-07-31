# Pass 13 — Error handling

---

## Unguarded 500s — the live set

Five paths raise an unhandled exception on ordinary input. All are detailed in earlier passes;
collected here because they are the same defect class and share one root cause: **no test
executes them.**

| Path | Exception | Finding |
|---|---|---|
| `POST /attendance/scan-check-in` | `AttributeError: body.direction` | `pass-09` |
| `GET /production/skus`, `/styles/{id}/progress`, `/skus/{id}/pieces` | `TypeError: unexpected kwarg client_scope` | `pass-01` |
| any piece-rate wage run | `AttributeError: Piece.style_id` | `pass-01` |
| `POST /wages/runs` with a null-salary MONTHLY employee | `AttributeError: names_for` | `pass-02` |
| `POST /imports/commit` re-run | `IntegrityError` on `piece.sku_id` | `pass-01` |

Each returns a 500 through the catch-all at `main.py:219-231`, so the client sees an opaque
failure with a request id and no indication that the request was well-formed.

---

## [SEV: MED] [core] [app/main.py:229-230]

**Issue:** The 500 handler leaks `repr(exc)` into the response body under `debug`.

**Why it's wrong:** `debug` defaults to `True` (`config.py:51`), so an unconfigured deployment
returns Python exception reprs — file paths, SQL fragments, column names — to unauthenticated
callers on any of the five paths above. This is the same default that disables the secret-key
guard and enables `create_all`.

**Correct behavior:** exception detail is logged, never returned.

**Fix sketch:** log `exc_info=True` and return the request id only, regardless of `debug`.

**Primary:** as above. **Risk:** developers lose in-response tracebacks — they are in the log.
**Fallback:** covered by flipping the `debug` default in `pass-03`; do both.

---

## [SEV: MED] [production] [app/modules/production/router.py:156]

**Issue:** A bad `screen_context` value returns 500 instead of 422.

**Why it's wrong:** `ScreenContext(body.screen_context.upper())` raises `ValueError` for any
string not in the enum. The value comes straight from the request body, so a typo from the
frontend — or an older client sending a retired screen name — is reported as a server fault.

**Fix sketch:** type the field as the enum in `LogRequest` so Pydantic rejects it with 422, or
catch `ValueError` and re-raise as `HTTPException(422)`.

**Primary:** type it in the schema — the contract then documents the valid values too.
**Risk:** none. **Fallback:** the try/except; one line.

---

## [SEV: MED] [imports] [app/modules/imports/router.py:94-96]

**Issue:** A failed import closes its session without rolling back.

**Why it's wrong:** `_do_commit_into_order` opens a second synchronous `SessionLocal`; on failure
it reaches `db.close()` with no `rollback()`. Detailed in `pass-06`. In the sync engine the
connection returns to the pool with an open transaction, which the pool then rolls back — so the
data is safe, but the failure is silent and the pool churns.

**Fix sketch:** `except: db.rollback(); raise` before the `finally`.

**Primary:** two lines. **Risk:** none. **Fallback:** none.

---

## [SEV: MED] [attendance] [app/modules/attendance/service.py:255-262]

**Issue:** `proxy_check_out` on a worker who never checked in.

**Why it's wrong:** `_close` is called with no prior `find()`; a worker with no open row produces
either a `None` dereference or a silent no-op depending on `_close`'s internals. Its sibling
returns a clean 400 (prior F121 notes the code should arguably be 404). Inconsistent handling of
the same condition across two routes on the same resource.

**Fix sketch:** check for the open row and 404 if absent, matching `proxy_mark_present`'s
existence check.

**Primary:** as above — folds into the `pass-03` fix for the same function. **Risk:** none.

---

## Verified-good

- **The catch-all handler is correctly written** (`main.py:219-231`): it re-raises
  `StarletteHTTPException` at `:223-224`, so 4xx pass through untouched and only genuine
  unhandled errors are wrapped. This closes prior F120's structural half.
- **Request ids are generated per 500** (`main.py:225-228`) — though they are not in the log
  format string, so correlating a reported id to a log line requires a full-text search
  (`pass-15`).
- **`/ready` degrades correctly**: `SELECT 1` with a 503 on failure (`main.py:278-295`).
- **Domain errors use correct codes throughout**: 409 on drawer conflicts
  (`drawers/service.py:74-78,136-146`), 422 on strict material fields
  (`materials/service.py:95-100`), 410 on retired barcodes (`barcode/service.py:46-48`), 413 on
  oversized upload (`imports/router.py:53-68`). The error *messages* in these paths are
  genuinely good — they name the missing fields and the required distance.

One message-quality exception: authorization failures echo the caller's role
(`users/deps.py:100-103`, prior F122) — see `pass-09`.
