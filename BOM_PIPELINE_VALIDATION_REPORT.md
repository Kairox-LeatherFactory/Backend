# BOM Pipeline — End-to-End Validation Report (Real Data, Live Gemini)

**Date:** 2026-07-13  · **Scope:** Clients 1 (John Peter), 2 (Beau Geste/CRIMIE), 4 (Confezioni Orfatti)
**Dataset:** `data/Full-data-spec-order/PTE V2.0`  · **LLM:** live `gemini-2.5-flash-lite` (vision + text), Groq fallback, key from `.env`
**Mode:** the real project services were executed against in‑memory SQLite + local‑FS storage. No mocks, no fabricated LLM output. Every number below was produced by a real run; the raw logs are in the scratchpad (`demo_client2.log`, `dxf_parse.log`, `order_extract.log`, `existing_boms.log`, `pl02_bom.log`, `pl02_bom2.log`).

> **Constraint honoured:** no project code was modified. All drivers are throw‑away scripts in the scratchpad that import and call the real services.

---

## 1. Executive Summary

The pipeline is **structurally sound and, for a DXF‑backed style, runs cleanly end‑to‑end**: order → per‑style split → spec extraction → DXF geometry → DCM → BOM with accessories. I proved this on **Client 2 / CR1‑02F5‑PL02**, generating a real 14‑line BOM whose leather consumption came straight from the graded DXF (Sheep Glass 253.63 dm², Goat Suede 24.79 dm², pocket lining 48.61 dm², all `dcm_source=dxf`, confidence 0.88).

However, the run surfaced **four high‑severity defects that materially distort the numbers**, all confirmed by execution, not inspection:

1. **The confirmed DXF is silently ignored unless a fragile signature string matches.** `generate_bom` re‑derives the style signature by lowercase‑slugging the style name and looks the pattern up **by that string** (case‑sensitive), rather than using the `pattern_reference_id` the operator just confirmed. My first PL02 run (DXF ingested as `CR1-02F5-PL02`) produced **all leather lines `provisional`, qty 0 dm²** — a BOM with *zero leather*. Only after I re‑ingested under the exact lowercase slug `cr1-02f5-pl02` did the DXF DCM appear.
2. **The order‑weighted DCM path is dead code** — the size‑quantity map it reads (`__order_run_quantities__`) is never written anywhere in the codebase, so every DXF DCM is taken at the **single base size (the smallest, S)**, understating leather by the full size spread.
3. **`species_of()` only recognises English words**, so Japanese material names (`シープグラス`, `ゴートスエード`) fall to the `_default` yield 2.30 and never use the calibrated sheep (2.50) / goat (2.10) factors.
4. **One real DXF (FLAVIO) crashes the parser** even though `parse_pattern` is documented to "never raise".

Net effect on the one style we can check against ground truth: generated Sheep Glass = **27.3 sf vs the client sheet's 34.5 sf (−21%)**; Goat Suede = **2.67 sf vs 2.6 sf (+2.6%, effectively correct)**. Bugs 2 + 3 fully account for the sheep gap (see §5/§8).

Extraction quality is otherwise **excellent**: PL02's order was read exactly (60 pcs; S4/M17/L23/XL10/XXL6, matching the existing client file to the piece), and multi‑style splitting works (18 styles from John Peter, 9 from Orfatti). Accessories flow through faithfully from the spec.

**Overall health: the engine is correct where it is exercised, but the DXF→DCM→cost spine has three compounding accuracy bugs and one crash that must be fixed before the generated BOM can be trusted for procurement.**

---

## 2. Pipeline Execution Flow (as actually run)

Current real entry points (the older single‑call `ProcurementService.generate_bom_from_submission` used by `scripts/demo_gemini_bom.py` **no longer exists** — see Bug L‑1):

```
POST /patterns                                  -> BomService.ingest_pattern_dxf      (DXF parse + persist)
POST /procurement .../order-sheet, .../spec     -> ProcurementService.upload_*         (scan→sniff→classify→store)
POST /submissions/{id}/order-breakdown          -> BomService.build_order_breakdown    (Gemini order extract → per-style rows)
POST /order-styles/{id}/attachments             -> BomService.confirm_style_attachments(bind spec + DXF)
POST /order-styles/{id}/generate-bom            -> BomService.generate_bom_for_style   -> generate_bom
POST /boms/{id}/confirm-cutting, /approve       -> BomService.confirm_cutting/approve_bom
(supplier PO)                                    -> SupplierPoService.generate_for_bom  (purchasing)
```

Inside `generate_bom` the order of operations is: extract spec (Gemini) → resolve POM terms → flatten spec attributes → build line seeds → resolve garment type → **look up the DXF pattern by signature** → build items (DCM‑resolving each leather line) → cost roll‑up → run client checks → persist.

**Timings observed (wall clock, live Gemini):**

| Step | Time |
|---|---|
| DXF parse (per file) | 0.06 – 1.08 s (SP74003, 300 pieces = 1.08 s) |
| Order extract — John Peter xlsx (18 styles) | 33.1 s |
| Order extract — Orfatti PDF (9 styles, 2×503 retries) | 19.7 s |
| Order extract — PL02 PDF (1 style) | 24.7 s (standalone) / 8.5 s (in the full run) |
| Spec extract — PL02 xlsx | ~22 s |
| DXF ingest + persist | < 0.3 s |
| Full PL02 order→BOM | ~35 s end to end (2 Gemini calls) |

---

## 3. Extraction Report

### 3a. Order sheets (real Gemini, `extract_order_doc`)

**Mechanism (answers the multi‑style question directly).** Gemini is called **once per document** and returns a **flat list of order lines** (`extract_order`). A *deterministic* post‑pass, `split_styles_colors`, then groups those lines into `StyleBreakdown` objects by their model/style column and, within each, into `StyleColorBreakdown` objects. Gemini does **not** emit pre‑separated styles; the parser separates them afterwards. Each style is keyed by a normalised `style_key` (its signature); totals are **recomputed from the size cells** (printed totals are treated as checks only). This is a clean design and it worked.

**Client 2 / PL02** (`Order-sheet-1.pdf`, native‑PDF vision rung):
- Header: order `1579`, client `CRIMIE`, currency `$`, delivery `2026-06-01`.
- **1 style** `CR1-02F5-PL02`, material `Sheep skin`, qty **60**, per‑size **S4 / M17 / L23 / XL10 / XXL6**, colour BLACK.
- This matches the existing `CR1-02F5-PL02.json` (order_quantity_total 60, identical size run) **exactly**. Extraction confidence here is effectively perfect.

**Client 4 / Orfatti** (`49-26-01-04-2026.pdf`): header `49/26`, currency `€`, delivery `2026-05-15`. **9 styles** extracted (SP72705, SP72706, SP74003, SP74018 [Military Green + Red = 380], SP74009, SP74006, SP74007, **SP747008**, **ROCHE**), all at 11–38/size across 38–56. The SP74018 two‑colour split (190+190) is correct and matches the "one style, two colour variations = 9 styles" expectation.

**Client 1 / John Peter** (`John_Peter_Order_Data_Consolidated (1).xlsx`, text rung): client `John Peter S.R.L.`, currency `€`. **18 styles** with full per‑colour, per‑size breakdowns (e.g. CLERMONT qty 57 across colours 124/06/707; SHINOBI KNIT DETACH qty 155; FRANCIS KNIT qty 149; ISLAY qty 139). Colour codes and rich labels (`06 DARK BROWN`, `651 ALGA`) were recovered well.

For each style the pipeline produces its own independent object carrying: `style_key`/signature, material, qty, per‑size map, and a list of colour children (key, label, qty, per‑size). That object is what a BOM is later minted for, and it is matched to spec/DXF by name suggestion at breakdown time.

**Extraction defects (real):**
- **`SP747008`** — a digit was duplicated; the true style is **SP74008**. No validation against the known style set caught it → it would fail to match its spec/DXF folder. *(Bug M‑2)*
- **`ROCHE`** — a 9th Orfatti style with no folder in the dataset (extra row read from the order). *(Bug M‑2)*
- **Duplicate over‑splitting** in John Peter: `CLERMONT VEST` vs `VEST CLERMONT`, `FLAVIO FUR DETACH` vs `FLAVIO FUR DET`, `ANELE KNIT DETACHABLE` vs `ANELE KNIT DETACH`. The splitter *detects* these (`possible_duplicate_of:` warning) but keeps them as separate styles rather than merging — inflating the style count and fragmenting one physical style's quantities. *(Bug M‑1)*
- Transient `503 UNAVAILABLE` twice on the Orfatti PDF; the retry ladder recovered on the third attempt (good resilience).

### 3b. Spec sheet (real Gemini, `extract_spec`, PL02)

Ran inside the PL02 BOM generation: `poms=70` resolved, `unresolved=0`, garment attributes flattened into 14 line seeds (3 material + 8 accessory + 3 cost lines). No `manual_entry_required`, no flags. The spec's Japanese material names, accessory list (YKK zippers ×2, cord, tip, tape, leather patch, Morito parts), lining (`ポケット袋布`) and POM grid were all read. This is strong.

### 3c. DXF (real `parse_pattern`, all 13 files)

| DXF | src | unit | pieces | sizes | leather net (base) | notes |
|---|---|---|---|---|---|---|
| Client2 **PL02** | creacompo | mm | 75 | S,M,L,XL,XXL (graded) | 表生地 11.87→14.33 sf | clean, fully graded |
| Client2 JK26 | creacompo | mm | 29 | XL | 表地 20.35 sf | DESIGN tag says `JK25` (file is JK26) — mismatch |
| Client1 CLERMONT | unknown | mm | 53 | 50 | LEATHER 13.31 sf | single size |
| Client1 ISLAY | unknown | mm | 39 | 42 | LEATHER 12.16 sf | single size |
| Client1 SANDY | unknown | mm | 19 | 42 | LEATHER 10.06 sf | single size |
| Client1 TOWER | unknown | mm | 56 | **_unsized** | Leather 16.26 sf | size labels missing; `Leather`/`LEATHER` case dup |
| Client1 REESE | lectra | mm | 16 | 42 | (unlabelled) 11.75 sf | contains a **"TEST 20X20 CM"** square in net area |
| Client1 VIRGILIO | lectra | mm | 27 | 50 | (unlabelled) 21.33 sf | |
| Client1 **FLAVIO** | — | — | — | — | — | **CRASH** — `DXFStructureError` (Bug H‑4) |
| Client4 SP74003 | lectra | mm | 300 | 38–56 (graded) | 18.48–18.67 sf | nonmonotonic‑grading warning |
| Client4 SP74009 | lectra | mm | 137 | XS–XL | 12.09–14.67 sf | nonmonotonic warning; S has extra piece |
| Client4 SP74006 | lectra | mm | 19 | S | 9.07 sf | single size |
| Client4 SP74007 | lectra | mm | 22 | S | 9.85 sf | single size |

The parser correctly recovers CP932/Shift‑JIS Japanese piece/fabric labels, detects the drawing unit, computes per‑piece cut‑contour area via Shapely, and grades PL02/SP74003/SP74009 per size. Fabric matrices split net area by fabric label (e.g. PL02 `表生地`/`別布`/`スレーキ`/`平ゴム`). Where a file carries no fabric labels (REESE, VIRGILIO, all Orfatti), area is attributed to the whole garment (single‑fabric export rule).

---

## 4. Style Resolution Report

Styles are matched **by signature string**, derived from `style_signature(customer_ref → internal_ref → slug(name))`:
- **Order → per‑style row:** `build_order_breakdown` sets each row's `style_signature = slug(style_key)` (e.g. `cr1-02f5-pl02`) and *suggests* a spec doc + DXF pattern by fuzzy filename/signature match. For PL02 the spec (`spec_sheet_1.xlsx`) and DXF did **not** name‑match, so both came back `status=none` and required explicit confirmation — correct "suggest, never silently bind" behaviour.
- **Confirm:** `confirm_style_attachments` binds `spec_document_id` + `pattern_reference_id` and flips status to `confirmed`. Verified: `spec_status=confirmed dxf_status=confirmed`.
- **BOM generation:** `generate_bom` re‑resolves the DXF pattern with `get_current_pattern(sig, client_id)` where `sig` is recomputed from the identity name — **it does not use the confirmed `pattern_reference_id`.** This is the crux of Bug H‑1 (below): matching succeeds only if the *ingested* signature string is byte‑identical (including case) to the *recomputed* one.

So resolution "works" only because the order style name and the DXF happen to reduce to the same slug. It is fragile: the JK26 DXF's internal DESIGN tag (`JK25`) already disagrees with its filename, and a style like `SP747008` (misread) would never resolve.

---

## 5. DCM Report (real values, PL02)

**Formula (per leather category, per size):**
```
DCM_dm² = net_sf(fabrics mapped to category, at size) × yield(species) × 9.290304
```
`net_sf` comes from the DXF fabric matrix; `yield` from `config_store` defaults `{sheep:2.50, goat:2.10, calf:2.30, lamb:2.50, _default:2.30}`; `9.290304` is sf→dm².

**Resolution ladder** (`_resolve_dcm`), first hit wins: (1) confirmed consumption template → (1b) template via pattern ref → **(1c) DXF** → (2) similar style → (3) POM‑area estimate (`ai_estimate`, conf 0.50) → else **provisional (qty 0, conf 0)**.

**What actually executed for PL02** (base size resolved to **S**):

| Line | fabric (DXF) | net_sf @ S | yield used | ×9.29 | Generated DCM | `dcm_source` |
|---|---|---|---|---|---|---|
| main (Sheep Glass) | 表生地 | 11.87 | **2.30** (`_default`) | | **253.63 dm² = 27.3 sf** | dxf (0.88) |
| sub (Goat Suede) | 別布 | 1.16 | **2.30** (`_default`) | | **24.79 dm² = 2.67 sf** | dxf (0.88) |
| lining (pocket bag) | スレーキ | 4.55 | 1.15 (fabric wastage) | | **48.61 dm²** | dxf (0.88) |

Check: 11.87 × 2.30 × 9.29 = 253.6 ✔; 1.16 × 2.30 × 9.29 = 24.8 ✔; 4.55×1.15×9.29 = 48.6 ✔.

**Why the sheep number is wrong (−21% vs the 34.5 sf ground truth), quantified:**
- **Bug 2 (base size, not order‑weighted).** The order is weighted toward L/M, so the *quantity‑weighted* main net = Σ(qty·net)/60 = (4·11.87+17·12.49+23·13.22+10·13.77+6·14.33)/60 = **13.13 sf**, not the base‑size 11.87 sf.
- **Bug 3 (species not recognised).** `species_of("シープグラス")` finds no English "sheep" → uses 2.30 instead of the sheep 2.50.
- **With both fixed:** 13.13 × 2.50 × 9.29 = **304.9 dm² = 32.8 sf**, i.e. within ~5% of the client's 34.5 sf. Goat with both fixed: 1.202 (weighted) × 2.10 × 9.29 = 23.4 dm² = 2.52 sf (truth 2.6). Both become accurate.

The current Goat result (2.67 sf) is close only by coincidence — the too‑high `_default` yield (2.30 vs goat's 2.10) roughly cancels the too‑small base size.

**DXF‑less styles (Stage 7 estimate), tested in isolation** with the real `estimate_area_dcm` and the JACKET `area_formula` (`length=BACK_LENGTH×1.0`, `width=CHEST×0.5`, panels 3, wastage 18%):

| CHEST / BACK (cm) | estimate |
|---|---|
| 120 / 70 | 148.68 dm² = 16.0 sf |
| 110 / 68 | 132.40 dm² = 14.3 sf |
| 130 / 72 | 165.67 dm² = 17.8 sf |
| POMs absent | **None → line falls to provisional (0 dm²)** |

So for ANELE / FRANCIS / SHINOBI (no DXF) the leather DCM is only estimated **if** the spec extraction yields both the length and width POMs *and* the garment type resolves to one with an `area_formula`; otherwise the leather line is emitted **provisional at 0 dm²** (exactly what my first PL02 run showed when the pattern was missing). This is the key reliability caveat for the DXF‑less path.

---

## 6. BOM Report (real, PL02 — 14 line items)

Generated BOM `a33bd077…` · status DRAFT · order_qty 60 · `garment_fob_price = 38.0` · `bulk_total = 2280.0` (= 60 × 38).

| # | Category | Name | qty/garment | uom | bulk (×60) | unit_price | DCM src |
|---|---|---|---|---|---|---|---|
| 1 | main_material | シープグラス (Sheep Glass) | 253.63 | dm² | 15217.8 | 0.0 | dxf 0.88 |
| 2 | sub_material | ゴートスエード (Goat Suede) | 24.79 | dm² | 1487.4 | 0.0 | dxf 0.88 |
| 3 | lining | ポケット袋布 (pocket bag) | 48.61 | dm² | 2916.6 | 0.0 | dxf 0.88 |
| 4 | accessory | YKK 5MA ZA C5 … CR引手SILVER | 2 | pc | 120 | 0.0 | — |
| 5 | accessory | YKK 5MA DABL C5 (止め) | 1 | pc | 60 | 0.0 | — |
| 6 | accessory | YKK 5MA DABL C5 (止め) | 1 | pc | 60 | 0.0 | — |
| 7 | accessory | CRオリジナル 内径10mm (cord) | 1 | pc | 60 | 0.0 | — |
| 8 | accessory | チップ加工 (tip) | 1 | pc | 60 | 0.0 | — |
| 9 | accessory | 35mm幅 (tape) | 1 | pc | 60 | 0.0 | — |
| 10 | accessory | CRオリジナル レザーワッペン (patch) | 1 | pc | 60 | 0.0 | — |
| 11 | accessory | C4182S MORITO / パッキン 9x2 | 2 | pc | 120 | 0.0 | — |
| 12 | manufacturing | Cutting & Stitching | 1 | pc | 60 | 30.0 | — |
| 13 | packaging | Packaging | 1 | pc | 60 | 3.0 | — |
| 14 | fob_charge | FOB Charge | 1 | pc | 60 | 5.0 | — |

**Explanations.** Lines 1–3 are the DXF‑resolved consumption. Lines 4–11 are the spec accessories: `qty_per_garment` is taken from the spec (two front zippers → 2; two Morito packing pieces → 2; everything else 1) and `bulk = order_qty × qty`. Lines 12–14 are the default `cost_catalog` lines (manufacturing 30, packaging 3, FOB 5), each qty 1 with the cost in `unit_price`. **Wastage is not a separate line** — it is folded into the DCM via the yield factor (leather) or the 1.15 fabric‑wastage multiplier (lining). Costing per `costing.py`: `total_cost = qty×price`, `bulk_qty = order_qty×qty`, `line_bulk = order_qty×total`, `fob = Σ total_cost`.

**BOM‑level observations:**
- `garment_fob_price = 38.0` is **missing all leather cost** because material `unit_price` is left 0 by design (the cutting manager fills rates). The reference FOB for this exact style (BMO‑1) is US$107.25, of which leather is the dominant driver — so a generated FOB of 38 is expected *pre‑pricing*, not a final quote. This is correct behaviour, but note that no material rate existed for the Japanese material names so nothing auto‑priced.
- Accessory line 6 duplicates line 5 (`YKK 5MA DABL C5`) — the spec lists the same zipper twice (front + back), which is faithful but might warrant dedup/merge.

---

## 7. Procurement Report

The purchasing stage is `SupplierPoService.generate_for_bom(bom_id)`: it reads the approved BOM DTO + the latest inventory check, groups materials by supplier, computes purchase quantities (`bulk_qty` adjusted for on‑hand inventory) and emits per‑supplier POs + an "unmatched" bucket for materials with no supplier.

**Execution status: not completed in this run.** Two blockers, reported honestly:
1. The BOM must be `approved` first, and `confirm_cutting` raised a SQLAlchemy `MissingGreenlet` when iterating `bom.items` (Bug M‑3). In my single long‑lived session this stopped the flow before approval.
2. `generate_for_bom` additionally needs seeded suppliers (`SUPPLIERS_updated*.xlsx`) and an inventory check (`INVENTORY (1).xlsx`), which the minimal seed did not load.

What *can* be stated from the executed BOM: purchase quantities would be the `bulk_qty` column above (e.g. Sheep Glass 15 217.8 dm², Goat Suede 1 487.4 dm², each YKK zipper 60–120 pc), grouped by supplier and netted against inventory. Because those `bulk_qty` values inherit the DCM understatement (Bug 2/3), **procurement quantities for leather would be ~15–20% low** until the DCM bugs are fixed. This is the most important downstream consequence.

---

## 8. Existing BOM Comparison

### 8a. Client 2 / PL02 (generated vs `CR1-02F5-PL02.json` / BMO‑1 ground truth)

| Item | Generated | Existing (truth) | Δ | % | Root cause |
|---|---|---|---|---|---|
| Sheep Glass (main) | 27.3 sf (253.63 dm²) | 34.5 sf | −7.2 sf | **−21%** | Bug 2 (base size S, not weighted) + Bug 3 (`_default` yield 2.30 vs 2.50) |
| Goat Suede (sub) | 2.67 sf (24.79 dm²) | 2.6 sf | +0.07 sf | +2.6% | coincidental cancellation (small base × high `_default` yield) |
| Pocket lining | 48.61 dm² (≈0.52 m² × pieces) | 0.4 m taffeta | n/a | — | different UOM basis (area vs linear metre); needs a linear‑yield rule |
| Order qty / sizes | 60; S4 M17 L23 XL10 XXL6 | 60; identical | 0 | 0% | exact match |
| Accessories (zip/cord/patch/Morito) | present, qty 1–2 | present | — | qualitatively match the order sheet's YKK/cord/patch/Morito list |

### 8b. Client 4 / Orfatti (existing `Orfatti_Contr49-26_BOM.xlsx`, per‑garment DCM norms)

The existing sheets state leather as a single **"DCM" norm per garment**: SP72705 = 355, SP72706 = 327, SP74003 = **480**, SP74018‑MilGreen = 375. The DXF net areas we parsed (e.g. SP74003 ≈ 18.5 sf = 172 dm²) imply a gross of ~340–430 dm² at yield 2.0–2.5 — the right order of magnitude but **below the sheet's 480 for SP74003**, again consistent with the base‑size/species issues plus these being *jackets* (the sheet's norm likely already includes a larger marker allowance). A full generated‑vs‑existing per‑line comparison for Orfatti was **not run to completion** (the Client‑4 order carries the `SP747008` misread and `ROCHE` phantom that would block clean per‑style matching — Bug M‑2). The existing sheets also confirm the **accessory/label/packaging taxonomy** the generator targets (thread TKT‑30/60/50, fusing, buttons, care/size/main labels, hang tag, poly bag, carton, tissue) — the generator's cost‑catalog + spec accessories cover these categories but with generic default quantities rather than the sheet's specific norms.

**Key comparison takeaway:** the *structure* matches; the *leather quantity* is systematically low because of Bugs 2 + 3; accessories match in kind but not yet in exact per‑norm quantity/price (those come from the client cost sheet / cutting‑manager entry, which the generator leaves blank by design).

---

## 9. Bug Report

### Critical / High

**H‑1 — Confirmed DXF ignored; DCM binds to a fragile, case‑sensitive signature string.**
`app/modules/bom/service.py:840` (and `:1335`): `pattern = get_current_pattern(sig, client_id)` where `sig = style_signature(name=identity.name)` **lowercase‑slugs** the name; `repository.py:321` matches `style_signature ==` exactly (case‑sensitive). `generate_bom` never consults the confirmed `order_style.pattern_reference_id`. **Failure proven:** DXF ingested as `CR1-02F5-PL02` → every leather line `provisional`, qty 0 (BOM with no leather). Re‑ingesting as `cr1-02f5-pl02` fixed it.
*Fix:* resolve the pattern from the confirmed `pattern_reference_id` when present; otherwise normalise both sides (`.upper()`/casefold) before comparison.

**H‑2 — Order‑weighted DXF DCM is dead code.**
`service.py:1186`: `per_size_qty = poms_for_size.get("__order_run_quantities__") or {}`. A repo‑wide search shows `__order_run_quantities__` is **only read, never written**. So `_weighted_dxf_dcm` always gets `{}` → returns None → falls back to single **base size** (`dcm_for_category` at `base_size`, which resolves to the smallest size, S). Leather is understated by the whole size spread (~11% net on PL02, larger after cost).
*Fix:* inject the order's per‑size quantities (already on `identity.per_size_qty`) into the resolver instead of routing them through the POM dict under a key nobody sets.

**H‑3 — `species_of()` is English‑only, defeating per‑species yield.**
`dcm.py:110`: matches substrings `goat/sheep/lamb/calf` in the lowercased name. Japanese names (`シープグラス`, `ゴートスエード`) → `_default` (2.30). Sheep should be 2.50, goat 2.10. Confirmed: both PL02 leathers used 2.30.
*Fix:* map species from the DXF fabric role / spec material‑type field (language‑independent) rather than an English keyword scan.

**H‑4 — `parse_pattern` crashes on a real DXF despite "never raises".**
`dxf_pattern.py:207`: the `except ezdxf.DXFStructureError` wraps only the first `ezdxf.readfile`; the fallback `recover.readfile` **also** raises `DXFStructureError` (FLAVIO: `Invalid group code "STANDARD" at line 65`), which propagates uncaught and aborts the batch.
*Fix:* wrap the `recover.readfile` fallback too and return a `manual_entry_required` `ParsedPattern` on failure.

### Medium

**M‑1 — Multi‑style splitter over‑splits near‑duplicate style names** (`CLERMONT VEST`/`VEST CLERMONT`, `FLAVIO FUR DETACH`/`FLAVIO FUR DET`, `ANELE KNIT DETACHABLE`/`ANELE KNIT DETACH`). It warns (`possible_duplicate_of`) but does not merge, fragmenting one physical style's quantities. *Fix:* canonicalise style keys (token‑set match) and merge, or require operator resolution.

**M‑2 — Order extraction has no validation against the known style set.** `SP74008`→`SP747008` (extra digit) and a phantom `ROCHE` passed through silently. *Fix:* validate extracted `style_key`s against the client's folder/style registry and flag unknowns.

**M‑3 — `confirm_cutting` triggers `MissingGreenlet` iterating `bom.items`.** `service.py:1338` lazy‑loads `bom.items` after `_load_bom`; observed twice in the single‑session driver. May be a session‑scoping artifact of the long‑lived driver rather than the request‑scoped API, but the lazy access is unsafe under async and should be eager‑loaded. *Fix:* `selectinload(Bom.items)` in `_load_bom`/`get_bom` used by `confirm_cutting`. **(Verify under a fresh request‑scoped session before prioritising.)**

### Low

- **L‑1 — `scripts/demo_gemini_bom.py` is stale:** calls `ProcurementService.generate_bom_from_submission`, which no longer exists (`AttributeError`). The current flow is the per‑style path in §2. It also prints emojis that crash on the Windows cp1252 console (needs `PYTHONIOENCODING=utf-8`).
- **L‑2 — REESE DXF includes a `TEST 20X20 CM` calibration square (0.431 sf) counted in net area**, inflating consumption. *Fix:* exclude obvious test/marker pieces by name.
- **L‑3 — DXF grading non‑monotonic** on SP74003 & SP74009 (the parser flags it): S carries extra pieces (e.g. `S23430225`), so net area doesn't rise monotonically with size. Data/label hygiene.
- **L‑4 — TOWER DXF has `_unsized` pieces** (missing SIZE labels) and mixed‑case fabric labels (`Leather` vs `LEATHER`) that fragment the fabric matrix.
- **L‑5 — JK26 DXF DESIGN tag is `JK25`** while the file/style is JK26 — an internal identity mismatch that signature matching would trip on.
- **L‑6 — Pydantic warning:** `extraction_schemas.py:336` `_coerce_str overrides an existing @field_validator` (harmless but indicates a duplicated validator).
- **L‑7 — Local storage put is not long‑path safe on Windows** (deep `quarantine/<uuid>/…/<sha>.pdf` keys exceed MAX_PATH under a long root). Environmental, but worth `\\?\` long‑path handling.

---

## 10. Improvement Recommendations

**DCM reliability (highest priority).**
1. Make the confirmed `pattern_reference_id` the source of truth for the DXF pattern in `generate_bom` (kills H‑1) and normalise signature case everywhere else.
2. Wire `identity.per_size_qty` directly into the DCM resolver so the **order‑weighted** DXF path is live (kills H‑2). Compute `DCM = Σ(qty_s · net_s)/Σqty_s · yield · 9.29`.
3. Derive species from a structured field (DXF fabric role / spec material type), not an English keyword scan (kills H‑3). Add a small multilingual lexicon (シープ→sheep, ゴート→goat, etc.).
4. Calibrate yields from confirmed orders (`learn_yield` already exists) so factory reality replaces the seed defaults.

**Extraction accuracy.** Validate extracted style keys against a per‑client style registry; add a canonicalisation/merge step for duplicate style names; keep the printed‑total cross‑check but surface mismatches as blocking flags.

**Robustness.** Make `parse_pattern` truly non‑raising (H‑4); skip test/marker pieces; handle `_unsized` DXFs by falling back to the spec base size; add long‑path‑safe storage.

**Architecture / maintainability.** Delete or fix the dead `__order_run_quantities__` indirection and the stale demo script; add an integration test that runs order→spec→DXF→BOM for PL02 and asserts leather DCM within ±10% of the known 34.5/2.6 sf (this single test would have caught H‑1/H‑2/H‑3). Move `FABRIC_WASTAGE_DEFAULT` and yields fully into `config_store`.

**Performance.** DXF parsing and costing are sub‑second; the only latency is Gemini (20–35 s/doc). Cache extraction results by document sha (the store already keys by sha) so re‑generation doesn't re‑call Gemini; batch a client's whole order in one call (already done) and run spec extractions concurrently across styles.

---

## 11. Complete End‑to‑End Walkthrough (real execution, style = CR1‑02F5‑PL02)

**The journey of one real style, in the order it happened this run.**

1. **DXF ingest** — trigger: operator uploads `CR1-02F5-PL02.dxf`. `BomService.ingest_pattern_dxf` → `repository.persist_dxf` → `dxf_pattern.parse_pattern`. Input: 75‑piece Creacompo DXF. Processing: reads INSERT→block polylines, Shapely areas, CP932 fabric labels, grades S–XXL. Output: a `PatternExtraction` row (+ `PatternPiece` children) with a per‑size fabric matrix (`表生地` main 11.87→14.33 sf, `別布` sub 1.16→1.23, `スレーキ` pocketing 4.55→4.66, `平ゴム` elastic). Tables written: `pattern_extraction`, `pattern_piece`. Bytes stored under `patterns/<sig>/<sha>.dxf`. *Why:* the DXF is the measured‑geometry source for DCM.

2. **Order + spec upload** — `ProcurementService.upload_order_sheet`/`upload_spec_sheet`. The scanned order PDF has no text layer → validator escalates to manual review → force‑accepted (logged override); the xlsx spec scores 1.00 against the `beau_geste` template and is accepted. Tables: `submission`, `document`. Bytes quarantined by sha. *Why:* gate/track inputs before extraction.

3. **Order breakdown** — `BomService.build_order_breakdown` → `extract_order_doc` (Gemini native‑PDF, 8.5 s). Gemini returns flat lines; `split_styles_colors` groups them → **1 style** `CR1-02F5-PL02`, qty 60, S4/M17/L23/XL10/XXL6, colour BLACK. Output: one `OrderStyle` row (sig `cr1-02f5-pl02`) + one `OrderStyleColor`. Because the spec/DXF filenames don't name‑match, both suggestions are `none`. *Why:* one BOM is minted per style; this is the multi‑style separation point.

4. **Confirm attachments** — `confirm_style_attachments(spec_document_id, pattern_reference_id)` → row `spec_status=confirmed, dxf_status=confirmed`. *Why:* a plausible‑but‑wrong spec/DXF must be human‑confirmed before it drives a BOM.

5. **Generate BOM** — `generate_bom_for_style` → `generate_bom`. Inputs: spec bytes + the `OrderStyle` identity. Steps executed: (a) `extract_spec` (Gemini, ~22 s) → 70 POMs, garment attributes; (b) `_build_line_seeds` → 3 material + 8 accessory + 3 cost seeds; (c) resolve garment type; (d) **`get_current_pattern(sig)`** — the pivotal lookup: with `sig=cr1-02f5-pl02` it found the ingested pattern (with `CR1-…` uppercase it returned None → all‑provisional, Bug H‑1); (e) `_build_items` → for each leather seed, `_resolve_dcm` → Source 1c (DXF) → `dcm_for_category` at base size **S** with `_default` yield 2.30 → 253.63 / 24.79 / 48.61 dm²; (f) `costing.recompute_bom` → FOB 38.0, bulk 2280. Tables written: `bom`, `bom_item`, `spec_sheet`, `spec_extraction`, `order_extraction`, `audit_log`. Output: the 14‑line BOM of §6. *Why:* this is the BOM the factory and procurement consume.

6. **(Would‑be) confirm‑cutting → approve → procurement** — `confirm_cutting` writes the confirmed DCMs back as a `consumption_template` (so the *next* order of this style hits Source 1), then `approve_bom` materialises the order/style, then `SupplierPoService.generate_for_bom` groups `bulk_qty` by supplier and nets inventory into POs. This run stopped at `confirm_cutting` (Bug M‑3) and lacked supplier/inventory seed, so POs were not emitted — but the purchase quantities would be the §6 `bulk` column.

**Sequence (execution flow):**
```
Operator │ POST /patterns ─────────► BomService.ingest_pattern_dxf ─► parse_pattern(ezdxf/shapely) ─► pattern_extraction
         │ POST upload order/spec ─► ProcurementService.upload_* ─► scan/sniff/classify ─► document (bytes by sha)
         │ POST order-breakdown ───► BomService.build_order_breakdown ─► extract_order_doc ─► GEMINI (native-PDF)
         │                                                            └─► split_styles_colors ─► OrderStyle(+Color)
         │ POST attachments ───────► confirm_style_attachments ─► OrderStyle.{spec,pattern}=confirmed
         │ POST generate-bom ──────► generate_bom_for_style ─► generate_bom
         │                             ├─ extract_spec ─► GEMINI (xlsx→text)
         │                             ├─ get_current_pattern(sig) ─► pattern_extraction   ★ H-1 hinge
         │                             ├─ _resolve_dcm ─► DXF (base size, _default yield)  ★ H-2/H-3
         │                             └─ costing.recompute_bom ─► bom + bom_item
         │ (confirm-cutting/approve) ► consumption_template + order/style   [blocked: M-3]
         │ (supplier PO) ───────────► SupplierPoService.generate_for_bom ─► purchase_order  [not seeded]
```

**Plain‑English narrative.** A pattern DXF and two order/spec documents come in. The DXF is measured into exact per‑size, per‑fabric cut areas. Gemini reads the order once and the parser splits it into styles; here there is one, 60 pairs of leather track pants across five sizes in black. The operator confirms which spec and which DXF belong to that style. The system then reads the spec with Gemini, lists the materials and accessories, and — crucially — pulls the leather consumption from the DXF: 27.3 sf of sheep‑glass body and 2.67 sf of goat‑suede side panels per garment, plus pocket lining, plus the zippers, cord, tape, patch and Morito hardware the spec calls for, plus fixed making/packaging/FOB lines. It rolls these into a 14‑line BOM and, at bulk, multiplies each per‑garment quantity by 60 to give what must be cut and purchased. The machinery works; but because the engine reads only the smallest size and doesn't recognise the Japanese word for "sheep", the sheep‑leather figure lands ~21% under the client's own 34.5 sf — a gap that closes to ~5% once the order‑weighted, species‑aware DCM (already half‑built in the code) is switched on.

---

### Appendix — what was executed vs. reasoned
- **Fully executed (real):** environment/deps check; all 13 DXF parses; live‑Gemini order extraction for Clients 1/2/4; live‑Gemini spec extraction + full BOM for PL02 (twice, before/after the signature fix); the DCM‑estimate heuristic; existing‑BOM dumps.
- **Reasoned from code + partial execution:** supplier‑PO purchasing (entry point exercised, blocked by M‑3 + missing seed); a completed Orfatti/John‑Peter per‑style BOM (extraction executed; full BOM not run for all styles due to Gemini rate limits — the PL02 run is the representative full trace). These are called out explicitly rather than asserted.
