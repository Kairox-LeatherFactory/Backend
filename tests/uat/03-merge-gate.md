# UAT 03 — The drawer, the merge, and the line-stitch gate

**Roles:** Cutting Manager, Lining Manager, Direct Manager, Stitching Manager
**Prerequisites:** UAT 02 passed — pieces cut on both paths
**Business rule:** a lined jacket cannot be line-stitched until leather **and**
lining are both in its drawer and the DM has released it. Completeness, not sequence.

---

## Store-scan: drawer first, then piece

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 1 | Scan a **drawer** barcode, then the piece that belongs in it, part = LEATHER | Accepted; drawer moves to `HOLDING_LEATHER` | ☐ |
| 2 | Scan the same drawer, then a piece that belongs to a **different** drawer | **409** — the merge map is the authority | ☐ |
| 3 | Scan the drawer, then its piece, part = LINING | Drawer moves to `HOLDING_BOTH` | ☐ |
| 4 | Do steps 1–3 in the other order (lining first, then leather) | Drawer reports `HOLDING_LINING`, then `HOLDING_BOTH` — not "leather" | ☐ |

Step 4 catches a real past defect: a lining-first scan used to report the drawer as
holding leather.

## The DM release

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 5 | As the Stitching Manager, try to line-stitch the piece now | `merge_blocked` — the drawer is not SENDED | ☐ |
| 6 | As the DM, mark the drawer **RECEIVED** | Accepted (it holds both parts) | ☐ |
| 7 | Try to line-stitch again | Still `merge_blocked` — RECEIVED is not SENDED | ☐ |
| 8 | As the DM, mark the drawer **SENDED** | Accepted | ☐ |
| 9 | Line-stitch the piece | 201, logged | ☐ |

## Completeness rules

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 10 | Take a drawer holding **only leather** for a lined piece; DM marks RECEIVED | Refused — incomplete | ☐ |
| 11 | Take a **leather-only** piece (`needs_lining = false`), leather stored, DM marks RECEIVED | **Accepted** — complete on leather alone | ☐ |
| 12 | Try `SENDED` on a drawer that is not RECEIVED | 409 | ☐ |
| 13 | Try to move a `SENDED` drawer back to `RECEIVED` | 409 — no backward transitions | ☐ |
| 14 | Try a part store-scan into a drawer already `RECEIVED` | 409 | ☐ |

Step 11 is the one testers most often get wrong. A leather-only garment is complete
without lining; blocking it would stop half the floor.

## Recycling

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 15 | Advance the piece through shell stitching, final finish, inspection | Each accepted in order | ☐ |
| 16 | Log `PACKAGE_EXPORT` | 201 | ☐ |
| 17 | Check the drawer | Back to `WAITING` — recycled and reusable | ☐ |
| 18 | Confirm the drawer's **code** did not change | Static code, recycling state | ☐ |

## Rework

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 19 | Re-log line stitching on a piece that already has it | Accepted as rework | ☐ |
| 20 | Check the piece's history | Both events present; nothing overwritten | ☐ |

> ⚠️ Rework currently bypasses the merge gate as well as the sequence gate
> (`docs/audit/pass-01`). A piece can be re-line-stitched while its drawer sits at
> `HOLDING_LEATHER`. Try it at step 19 with an incomplete drawer and record the result.

## What "pass" means

A piece scanned into the wrong drawer is refused, a lined piece cannot be
line-stitched until both parts are in and the DM has released it, a leather-only
piece is not held up, and the drawer comes back to WAITING after export.
