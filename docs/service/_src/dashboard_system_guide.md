# 1. What this service is

The **manager screens**. One call per screen, and everything that screen needs comes back in it.

There are five screens:

| Screen | Endpoint | For |
|---|---|---|
| Cutting | `GET /dashboard/cutting` | the Cutting Manager |
| Lining | `GET /dashboard/lining` | the Lining Manager |
| Stitching | `GET /dashboard/stitching` | the Stitching Manager |
| Store | `GET /dashboard/store` | the Store Manager |
| Direct Manager | `GET /dashboard/direct-manager` | the DM — the whole factory |

Plus `GET /dashboard/alerts`, which every manager sees.

> **This service is READ-ONLY.** No request bodies, no writes, no state transitions. It owns **no tables** — every query reads other modules' data.

| Part | File |
|---|---|
| HTTP routes | `app/modules/dashboard/router.py` |
| Composition | `app/modules/dashboard/service.py` |
| Queries | `app/modules/dashboard/repository.py` |
| Response shapes | `app/modules/dashboard/schemas.py` |
| Cache | `app/core/cache.py` |

---

# 2. One call per screen, on purpose

`GET /dashboard/cutting` runs about **11 grouped queries**. `GET /dashboard/direct-manager` runs about **15**. They come back as one composite object.

Why not many small endpoints: a screen assembled from twelve calls is twelve chances for one to fail, twelve loading spinners, and twelve chances for the blocks to disagree with each other.

## The DM dashboard is composed from the other four

`GET /dashboard/direct-manager` is built from the same aggregates the four stage dashboards use. So the DM's screen and `/cutting`, `/lining`, `/stitching`, `/store` **can never disagree**. Drill into any of them for stage detail.

---

# 3. Caching — and why it is busted on write, not on a timer

The composite dashboards are **cached**. They are the heaviest reads in the application, and several managers keep them open all day.

> The cache is invalidated **the instant anyone logs a production event or a store scan** — not after a TTL expires.

That distinction matters on a factory floor. A cutting manager who logs a cut and then looks at the dashboard expects to see it. A 60-second TTL means a manager watching a screen that does not change concludes the scan did not work, and scans again.

---

# 4. Who can see these screens

| Role | Access |
|---|---|
| MD, DM | everything (superusers) |
| HR, Supervisor | everything |
| Cutting / Lining / Stitching / Store Manager | everything |
| **Client** | **no** — 403 |
| Viewer | no |

A client token is **not** admitted to floor dashboards. They expose worker names and floor data.

## Tenancy

Every handler carries `client_scope`, exactly as production and analytics do: `None` for staff (who read across clients), the caller's own `client_id` for a client login. A cross-tenant id yields an **empty** result, never another client's data.

---

# 5. The alerts — `GET /dashboard/alerts`

This replaced the standalone Analytics Overview and Risk Alerts screens, which now answer **410 Gone**. The bottleneck and the alerts were the only parts of those pages anyone acted on, and a manager should not have to know a separate screen exists to find out their line is blocked.

Three blocks, **most actionable first**:

| Block | What it is |
|---|---|
| `bottleneck` | **the deepest queue** — the stage with the most work waiting in front of it |
| `stage_spread` | where each downstream stage lags the leather cut. The gap is true **WIP in flight** — cut but not yet arrived at stage X |
| `freight_risk` | orders approaching their **sea cut-off**. Missing it means air freight, which is the single biggest margin event in the business. |

> **`bottleneck` is not "the first unfinished stage".** The first unfinished stage is wherever the line happens to have got to. That is not a constraint and it is not actionable. The deepest queue is.

`today` can be overridden on the query string, which is what makes the freight-risk window testable.

---

# 6. Drilling down

Every composite screen has the same two kinds of drill-down.

## By worker

```
GET /dashboard/cutting/employees/{employee_id}
GET /dashboard/lining/employees/{employee_id}
GET /dashboard/stitching/employees/{employee_id}
```

Which pieces that worker worked, with the stage and when they were last touched.

## By garment — the piece trace

**Five paths, one body.** The trace is the same object whichever dashboard you came from:

```
GET /dashboard/pieces/{piece_code}             the canonical one
GET /dashboard/cutting/pieces/{piece_code}
GET /dashboard/lining/pieces/{piece_code}
GET /dashboard/store/pieces/{piece_code}
GET /dashboard/stitching/pieces/{piece_code}
```

They exist separately so each screen can link to "its own" trace route, but there is **one implementation** — they cannot drift.

Each history row carries the leather consumption recorded at that cut event. **A lining cut logged without a measurement still appears, with a null consumption** — that is the honest answer, not a zero.

---

# 7. The consumption grids

```
GET /dashboard/cutting/consumption
GET /dashboard/lining/consumption
```

Per-piece material consumption.

> **Only two stages record consumption** — `LEATHER_CUTTING` and `LINING_CUTTING`. Asking for a consumption grid on any other stage is a **422**, not a silently empty list. A question with no answer should say so.

---

# 8. The store screens

| Endpoint | Answers |
|---|---|
| `GET /dashboard/store` | the composite (about 4 grouped queries). Filter by `style_id` and `state`. |
| `GET /dashboard/store/garments/{piece_id}` | one garment in the store, **plus who cut its leather and its lining** |
| `GET /dashboard/store/garments/{piece_id}/movement` | that garment's movement history, from the audit trail |
| `GET /dashboard/store/traceability` | **who cut what** — filterable by piece code, style or material type |

> The two `garments/{piece_id}` routes are keyed by the **piece id**, not a code, because they read frozen history for audit. They are also the two routes that replaced the old drawer detail and movement screens.

---

# 9. The Direct Manager screen

`GET /dashboard/direct-manager` returns, in one call: overall production, department performance, the stage pipeline and its bottleneck, production rate, quality, attendance, store send/receive, per-order progress and the 14-day trend.

Its drill-downs:

| Endpoint | Answers |
|---|---|
| `GET /dashboard/direct-manager/orders/{order_id}` | an order's complete journey across every stage, **with the stage currently holding the largest backlog** |
| `GET /dashboard/direct-manager/styles/{style_id}` | the same for one style |
| `GET /dashboard/direct-manager/pieces/{piece_code}` | the piece trace |

## Read `meta.unsupported`

Several quality and costing figures come back **`null` by design** — no table backs them yet. `meta.unsupported` names them.

**Do not render a null there as a zero.** "We do not measure this yet" and "this is zero" are different statements, and only one of them is true.

---

# 10. Notes for backend developers

- **This module owns no tables**, so there is no `dashboard.models` to add to the model-import block in `main.py`. That was verified before it was registered — a missed model import is the schema-drift trap.
- **The cache key includes `scope` and any filter.** A client-scoped read must never be served a staff-scoped cache entry.
- **`_validated_stage` rejects a consumption stage that records no consumption** rather than returning an empty list.
- **The four stage dashboards and the DM dashboard share their aggregates.** If you add a figure to one, add it where they all read it, not in the composer.
- **`client_scope` is on every handler**, even where no client can currently reach the route, so the same code serves a future scoped read without a second path.
