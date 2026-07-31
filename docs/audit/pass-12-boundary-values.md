# Pass 12 — Boundary values

---

## [SEV: HIGH] [attendance] [app/modules/attendance/models.py:63-64; repository.py:18-22]

**Issue:** A fresh database centres the geofence on 0°N 0°E and rejects every genuine check-in.

**Why it's wrong:** `ShiftConfig.factory_lat` / `factory_lon` default to `0.0`, and
`get_config()` auto-creates that row on first read. Until a DM sets the coordinates via
`PATCH /attendance/config`, `within_geofence` (`geofence.py:28-30`) compares every worker's real
position against Null Island with a 100 m radius — so **every** check-in 403s, and because
production logging requires attendance (`production/service.py:176-182`), the entire factory floor
is blocked on day one.

This is the single highest-probability first-day failure in the system, and it needs no attacker
and no unusual data — just a fresh deploy.

**Correct behavior:** an unconfigured geofence should fail open with a loud warning, not closed.

**Fix sketch:**
```python
if cfg.factory_lat == 0.0 and cfg.factory_lon == 0.0:
    logger.warning("Geofence unconfigured — allowing check-in"); return None
```

**Primary:** fail open + warn until configured. **Risk:** a deploy that forgets to configure runs
without a fence — which the warning and the `/config` screen make visible. **Fallback:** seed the
factory coordinates in `scripts/seed.py` and make `PATCH /attendance/config` part of the
go-live checklist. Zero code, but it depends on a human remembering.

---

## [SEV: MED] [materials] [app/modules/materials/service.py:266-269]

Negative stock — no floor check, no DB `CHECK`, no lock. Detailed in `pass-01` and `pass-06`;
the atomic conditional `UPDATE` in `pass-06` closes the boundary and the race together.

Related: **over-reservation drives available negative** (prior F111). `available = on_hand −
reserved` (`service.py:150-155`) with nothing bounding `reserved` against `on_hand`, so a
reservation larger than stock yields a negative available that the shortfall calculation then
clamps at zero (`:197`) — hiding the inconsistency rather than surfacing it.

---

## [SEV: MED] [production] [app/modules/production/service.py:279-290]

**Issue:** Consumption quantity is bounded but batch size is not (prior F113).

**Why it's wrong:** `consumption_qty > 0` is validated at cut stages, but the number of targets in
a single `POST /production/log` is unbounded. A malformed or malicious request with 100,000 piece
ids drives the per-piece N+1 loop from `pass-10` into a multi-minute transaction holding row locks
on `material_lot`.

**Fix sketch:** `Targets` gets `max_length=200` on its list fields in `production/schemas.py`.

**Primary:** a Pydantic bound. **Risk:** a legitimately large tray gets rejected — 200 is well
above the documented 40. **Fallback:** none needed; this is one annotation.

---

## [SEV: MED] [wages] [app/modules/wages/service.py:278-315]

**Issue:** Payroll window edges are only partially validated (prior F114).

**Why it's wrong:** `_validate_window` checks ordering and overlap, but nothing rejects a window
in the far future, a window spanning years, or `period_start == period_end` on a non-working day.
`prorate_monthly` (`proration.py:66-68`) will happily price a 400-day "fortnight".
`analytics/service.py:462-464` shows the same gap — only `end < start` is rejected on the
employee-rate report, with no maximum span.

**Fix sketch:** reject spans over 62 days and `period_end > today + 1 day`.

**Primary:** two bounds in `_validate_window`. **Risk:** a legitimate back-pay run over a long
period gets blocked — make the bound overridable by MD. **Fallback:** D1.

---

## [SEV: MED] [attendance] [app/modules/attendance/service.py:103-108]

**Issue:** A malformed shift time stops attendance for the entire factory; a night shift computes lateness against the wrong day (prior F109, F110).

**Why it's wrong:** `ShiftConfig` times are parsed per request; one bad value raises for every
worker, not just one. And a shift crossing midnight has `work_date` derived from the local
calendar day (`service.py:293`), so a 22:00–06:00 shift books the second half against the
following day and computes `is_late` against the wrong shift start.

**Fix sketch:** validate on write in `PATCH /attendance/config`, and derive `work_date` from
shift start rather than wall-clock date when `shift_end < shift_start`.

**Primary:** validate-on-write (D1, cheap, prevents the outage). Night-shift handling D2 unless
the factory runs nights — **confirm this with the DM**, because if it does, this is D0.
**Risk:** none. **Fallback:** document "day shift only" as a Phase-1 limitation.

---

## Other boundaries checked

| Case | Result |
|---|---|
| Negative / zero material qty at lot creation | ✅ rejected 422 (`materials/service.py:109-112`) |
| Zero consumption at cut | ✅ rejected 422 (`materials/service.py:266-267`) |
| Duplicate SKU | ✅ `uq_sku_identity` (`clients/models.py:132-134`) |
| Empty / unknown barcode | ✅ 404, retired → 410 (`barcode/service.py:38-52`) |
| Invalid enum in path/body | ✅ Pydantic 422; `ScreenContext(...)` at `production/router.py:156` raises `ValueError` → **500, not 422** |
| Invalid UUID | ✅ 422 from the path converter; `get_current_user` rejects a non-UUID `sub` (`deps.py:36-61`) |
| Order number > 50 chars | ❌ **silently truncated** (prior F115, `clients/models.py:56` `String(50)`, no validation) |
| Password > 72 bytes | ❌ silently truncated by bcrypt (prior F112) |
| Duplicate employee name | ✅ prefixed `IN-CHAL` (`employees/service.py`) |
| Cross-company data | ⚠️ tenancy is per-`client_id` and correct in analytics; **absent in the three broken production GETs** (`pass-01`) |
