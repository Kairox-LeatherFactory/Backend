# UAT 02 — Cutting a tray, and stock moving exactly once

**Role:** Cutting Manager (and a Lining Manager for the parallel path)
**Prerequisites:** UAT 01 passed; a leather lot exists with known on-hand
**Business rule:** the two cut stages are the only ones that consume material, and
a batch decrements stock **once**, not once per piece.

---

## Set up

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 1 | As DM, create a leather lot: article, colour, thickness, dcm | 201, a lot barcode is returned | ☐ |
| 2 | `GET /materials/stock` for that article | `on_hand` = what you entered, `reserved` = 0, `available` = on_hand | ☐ |
| 3 | **Write down the on-hand figure.** You will compare against it twice | — | ☐ |
| 4 | The cutter clocks in (UAT 04 step 1-3) | Present today | ☐ |

## Cut a tray of 10

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 5 | On the leather-cut screen, scan the cutter's employee card | The worker's name appears | ☐ |
| 6 | Scan the leather lot barcode | The lot's article and colour appear | ☐ |
| 7 | Enter consumption per piece in dcm | Accepted | ☐ |
| 8 | Scan 10 piece barcodes from the same SKU | All 10 listed, none rejected | ☐ |
| 9 | Submit | 201, `count_logged: 10`, no blocked lists populated | ☐ |
| 10 | `GET /materials/stock` again | on_hand dropped by **consumption × 10**, once | ☐ |
| 11 | Check one cut piece's history | One event at `LEATHER_CUTTING`, naming the cutter and the lot | ☐ |

> ⚠️ **Check the units.** The system reads your figure as **per piece** and
> multiplies by the number of pieces. If you entered a tray total, stock has just
> dropped 10× too far. The field name does not currently say which it is
> (`docs/audit/pass-01-business-logic.md`).

## Re-scanning must not double-charge

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 12 | Note the on-hand figure now | — | ☐ |
| 13 | Scan the **same 10 pieces** again at leather cutting | Accepted as rework | ☐ |
| 14 | `GET /materials/stock` | **Unchanged** from step 12 — a rework consumes no new hide | ☐ |

## The gates

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 15 | Try to log SHELL_STITCHING as the Cutting Manager | **403**, whole request refused | ☐ |
| 16 | Scan a piece with a worker whose designation is PACKER | That piece is `skill_blocked`; **the others still log** | ☐ |
| 17 | Scan a fresh piece straight at PASTING (skipping fusing) | `sequence_blocked` with a message naming the missing stage | ☐ |
| 18 | Mix a cut scan and a fusing scan in one batch | 422 — consumption cannot be attributed across stages | ☐ |
| 19 | Submit a cut with no lot selected | 422 asking for a material lot | ☐ |
| 20 | Submit a cut with consumption of 0 | 422 | ☐ |
| 21 | Scan a worker who has **not** clocked in | 400 telling you to mark attendance first | ☐ |

**Step 16 is the important one.** One unskilled worker in a tray of 10 must not lose
the other 9. If the whole batch is refused, that is a hard fail.

## The lining path

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 22 | As the **Lining Manager**, create a lining lot | ⚠️ **403 — known gap.** The role cannot create the lots it must consume (`docs/audit/pass-04`). Have the DM create it | ☐ |
| 23 | As the Lining Manager, cut lining for the same 10 pieces | 201; the two cut paths run independently | ☐ |
| 24 | Check a piece's history | Events at both `LEATHER_CUTTING` and `LINING_CUTTING` | ☐ |
| 25 | Try to log leather cutting as the Lining Manager | 403 — the two cut paths belong to different roles | ☐ |

## Stock edges

| # | Do this | Expect | ✅ |
|---|---|---|---|
| 26 | Cut a quantity larger than the lot's remaining on-hand | ⚠️ **Currently accepted — on-hand goes negative.** No floor check exists (`pass-01`, `pass-06`). Record the resulting figure | ☐ |
| 27 | Have two people submit a cut against the **same lot** at the same moment | ⚠️ Known race: one decrement can be lost. Compare on-hand against the two consumptions | ☐ |

## What "pass" means

Ten pieces cut in one scan, stock down by exactly one batch's worth, a rework that
moves no stock, and a bad piece in the tray that costs you only that piece.

Steps 26–27 are expected to behave badly today; record what you see rather than
treating it as a stop.
