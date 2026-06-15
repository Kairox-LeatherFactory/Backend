# Stage 2 — BOM Generation Spec: AI Bill-of-Materials from Order + Spec Sheet

**Status:** Draft for MD / stakeholder review
**Scope:** The Stage-2 *AI BOM-generation engine* only — POM extraction &
standardization, the DCM (consumption-per-garment) resolution policy, the cost math,
the editable BOM contract, per-client cross-checks, and the cutting-manager
confirmation gate. **No code, no migrations** are part of this document.
**Builds on:** `_specs/stage-0-foundation.md` (schema §1, LLM routing §4, roles §5)
and `_specs/stage-1-upload-validation.md` (the `submission` pairing + the
`ready_for_stage_2` gate).
**Source of truth for the process:** `data/BOM_Procurement_Workflow_fixed.pdf` /
`data/_wf.txt`, Stage 2 ("AI Analysis & BOM Generation").
**Sample output shape:** `data/BMO-1.pdf` (the "Yardage and Price Quotation").

> **Stack facts (re-confirmed against the code).** The procurement module is
> implemented through Stage 1: `app/modules/procurement/models.py` already defines
> `Bom`/`BomItem`/`SpecSheet`/`Document`/`Submission`/`ClientTemplate`; `enums.py`
> defines `BomItemCategory`, `SpecType`, `ExtractionSource`, `DcmSource` is **not**
> yet present; `core/config.py:73-76` already carries `extraction_model`
> (`gemini:gemini-2.0-flash`) and `extraction_fallback_model`
> (`groq:llama-3.3-70b-versatile`) + `gemini_api_key`/`groq_api_key`. Stage 1's
> `seed_templates.py` + `config/client_templates.yaml` establish the YAML-seeded,
> config-driven registry pattern this stage reuses for extraction adapters and
> cross-check rules. Embeddings remain local HF
> (`app/modules/intelligence/models_catalog.py`).

---

## 0. Context & why this stage exists — the correction at the centre of it

Stage 2 is the engine: it consumes a **completed submission** (Stage 1 emitted
`ready_for_stage_2: true` — both an order sheet and a spec sheet, validated and
clean) and produces the **`bom` + `bom_item` tree** shaped exactly like `BMO-1.pdf`,
then hands off to **Stage 3** (MD approve / lock). It owns extraction, DCM
resolution, costing, the editable draft, and the cutting-manager gate. It does **not**
do the inventory check (Stage 4) or the supplier PO (Stage 5).

### The governing decision: the pattern drives consumption, the spec drives assembly

The earlier Stage-2 framing assumed a spec sheet carries finished dimensions
("L × B") and that **DCM** — *decimetre² of leather consumed per garment*, the single
most important number in the BOM — is derived from those dimensions. **Inspection of
the two real spec sheets in `data/` disproves that.** Neither sheet contains anything
from which consumption can be computed, and they do not even share a shape:

| | **Beau Geste / CRIMIE** (`spec_sheet_1.xlsx`, sheet `CRPL-004`, 67×268, JP) | **The Jackiee / John Peter** (`Jackie-cleint-spec-sheet.xlsx`, sheet `TECH SHEET`, 56×3, IT) |
|---|---|---|
| **Shape** | Numeric **measurement grid**: 14 numbered POMs × sizes S–XXL, each with a grading **pitch** (ピッチ) column | **Pure prose**, 3 columns (field → free text), **zero numeric POMs** |
| **POMs** | ① ウェスト上り (waist, finished) ② ウェスト実寸 (waist, actual) ③ ヒップ位置 (hip height) ④ ヒップ (hip) ⑤ 渡り幅 (thigh width) ⑥ 膝丈 (knee length) ⑦ 膝幅 (knee width) ⑧ 裾幅 (hem width) ⑨ 前股上 (front rise) ⑩ 前股ぐり (front crotch curve) ⑪ 後股ぐり (back crotch curve) ⑫ 内股下 (inseam) ⑬ ベルト幅 (belt width) ⑭ 脇総丈 (outseam) | **None.** PATTERN field says: *"THIS IS A CARRY ON STYLE, follow pattern already used for SS26 named **MIMI-28-09-22-ZZ, in size 42**"* |
| **Also carries** | a 附属 trims block (cords/elastic/zippers + lengths), a materials block (表地 sheep glass **0.6–0.7 mm**, 別布 goat suede, 袋地 polyester taffeta), accessories (YKK zippers, D-ring, leather patch), and — note — a 特記事項 base-pattern reference *"※CR1-O2B5-PL02をベースに作成"* | leather quality (THIN SUEDE), colour (MAGENTA), zipper finish, labelling, pockets, stitching, loop — **all workmanship prose**; order is **22 SMS, all size 42** |
| **L × B?** | **No.** | **No.** |

The conclusion that reshapes this stage: **DCM = pattern-piece area × wastage, not a
finished-garment measurement.** The **spec sheet drives assembly** (which materials,
which trims, which colour, which workmanship); the **pattern drives consumption**
(how much leather a marker actually eats). Crucially, *neither real spec sheet gives
us the consumption number at all* — Jackiee gives us a pattern name, Beau Geste gives
us finished POMs from which area can only be *estimated*. So the heart of this spec is
**not** a measurement-to-DCM formula; it is an **ordered DCM-resolution policy with a
human source-of-truth** (§2) and a **cutting-manager confirmation gate** (§10).

### Decisions (confirmed with the product owner)

1. **DCM is resolved, not computed.** An ordered fallback (template → similar-style →
   AI heuristic → manual), every BOM line stamped with `dcm_source` + `dcm_confidence`
   (§2). The AI area-from-POM formula is the *last* resort, never the default.
2. **A learning DCM memory.** A confirmed cutting-manager DCM is written back to a
   `style_consumption_template` keyed on a **cross-order-stable** style signature
   (Style rows are order-scoped — see §3), so the second order of the same style is a
   template hit, not a re-estimate.
3. **Pattern references are first-class** (both clients carry them) — resolved to a
   `pattern_reference` lookup, not parsed as new POMs (§1, §3).
4. **Extraction is config-driven, like Stage 1.** A per-client *adapter* selected off
   `spec_type` + `client_match_code`; onboarding a client = a config entry, not a code
   branch (§4) — the same posture as `client_template`/`seed_templates.py`.
5. **The editable BOM uses bulk-PATCH + optimistic `revision` locking** (§7), so the
   interdependent recompute (DCM ↔ total ↔ bulk) stays atomic and aligns with the
   Stage-3 lock/`audit_log` model.
6. **No LLM unless required** (stage-0 §4 carries straight through): the Beau Geste
   grid is parsed deterministically (openpyxl); only the Jackiee prose tech pack and
   the handwritten order side spend a Gemini call.

---

## 1. POM extraction & standardization

POMs ("points of measure") are the per-size finished dimensions. They are **not** the
consumption number, but they are (a) the assembly/QC reference, and (b) the input to
the **last-resort** DCM heuristic (§2 Source 3). Stage 2 extracts *all* of them — not
two dimensions — standardizes the client-specific terms, and stores them in queryable
rows.

### 1a. Extraction — Gemini structured output, governed by stage-0 §4

The extraction service runs the stage-0 provider chain **Gemini → Groq → "needs
manual entry"** (reusing `settings.extraction_model` / `extraction_fallback_model`),
never silently guessing, and caches by `document.sha256` so a re-run isn't re-billed.
It emits an **intermediate JSON** validated against a fixed schema before any row is
written:

```jsonc
// intermediate POM-extraction result (validated, then persisted as pom_measurement rows)
{
  "spec_sheet_id": "…",
  "spec_type": "measurement_grid",          // from SpecSheet.spec_type (Stage 1)
  "garment_type_guess": "track_pant",
  "unit": "cm",
  "sizes": ["S","M","L","XL","XXL"],
  "poms": [
    { "pom_code": "WAIST_FINISHED", "source_term": "ウェスト上り",
      "by_size": {"S":72,"M":77,"L":82,"XL":87,"XXL":92}, "pitch": 5,
      "extracted_by": "deterministic", "confidence": 0.99 },
    { "pom_code": "THIGH_WIDTH", "source_term": "渡り幅",
      "by_size": {"S":33,"M":34,"L":35,"XL":36,"XXL":37}, "pitch": 1,
      "extracted_by": "deterministic", "confidence": 0.99 }
    // … all 14
  ],
  "pattern_reference": { "pattern_code": "CR1-O2B5-PL02", "base_size": null,
                         "source_term": "特記事項: ※CR1-O2B5-PL02をベースに作成" },
  "unresolved": []     // terms with no dictionary hit → surfaced for manual mapping, never dropped
}
```

`extracted_by` per POM records *who* produced it (`deterministic` for the openpyxl
grid parse, `gemini`/`groq` for LLM extraction, `manual` for keyed) — the audit trail
the LLM policy requires. `source_term` preserves the **original** label (渡り幅) so a
reviewer can verify the mapping.

### 1b. The POM dictionary — standardizing client-specific terms

A `pom_dictionary` (§3) maps every client/native term to a **standardized POM code**.
Adding a new client term (Italian, a new Japanese synonym, a misspelling) is a
**dictionary row**, not a code change.

| Standard `pom_code` | JP source term | EN/IT synonyms |
|---|---|---|
| `WAIST_FINISHED` | ウェスト上り | waist (finished), girovita |
| `WAIST_ACTUAL` | ウェスト実寸 | waist (actual / relaxed) |
| `HIP_HEIGHT` | ヒップ位置 | hip height / drop |
| `HIP` | ヒップ | hip, fianchi |
| `THIGH_WIDTH` | 渡り幅 | thigh / front-thigh width |
| `KNEE_LENGTH` | 膝丈 | knee length |
| `KNEE_WIDTH` | 膝幅 | knee width |
| `HEM_WIDTH` | 裾幅 | hem / leg opening |
| `FRONT_RISE` | 前股上 | front rise |
| `FRONT_CROTCH` | 前股ぐり | front crotch curve |
| `BACK_CROTCH` | 後股ぐり | back crotch curve |
| `INSEAM` | 内股下 | inseam, interno gamba |
| `BELT_WIDTH` | ベルト幅 | belt / waistband width |
| `OUTSEAM` | 脇総丈 | outseam, side length |

Unmapped terms go to `unresolved[]` and surface for a one-click "map this term" — they
are **never dropped or guessed into an existing code**.

### 1c. Pattern-reference detection (both clients)

When a spec says *"follow existing pattern X in size Y"* the correct action is **not**
to invent POMs — it is to **resolve a `pattern_reference`** (§3) and let DCM come from
the template/similar-style path (§2 Source 1/2). Two real triggers:

- **Jackiee** — explicit: `PATTERN = "…follow pattern already used for SS26 named
  MIMI-28-09-22-ZZ, in size 42"` → `pattern_reference{ pattern_code: "MIMI-28-09-22-ZZ",
  base_size: "42" }`, and the BOM is built entirely off that reference (it has *no* POMs).
- **Beau Geste** — embedded in 特記事項: `"※CR1-O2B5-PL02をベースに作成"` → a
  `pattern_reference{ pattern_code: "CR1-O2B5-PL02" }` *in addition to* its own 14
  POMs. Recording it lets the DCM lookup prefer the base style's confirmed consumption
  over a fresh estimate.

Detection is a per-adapter rule (§4): a regex/keyword over a designated field
(`PATTERN`, 特記事項) plus an LLM fallback for free prose.

### 1d. Prose-heavy specs (Jackiee) → structured fields

The Jackiee tech pack has no grid, so the adapter pulls **assembly** facts from prose
into the existing `SpecSheet.attributes` / `instructions` JSONB (Stage 0 already typed
these as JSONB precisely for this). Enumerated extraction targets:

```jsonc
// SpecSheet.attributes (Jackiee)
{ "leather_quality": "thin suede", "leather_substance_mm": [0.45, 0.50],
  "primary_color": "MAGENTA", "lining": "unlined (dust-stopper on flesh side)",
  "accessories": [ {"type":"zipper","placement":"main closure","spec":"N°5 double slider",
                    "finish":"high shiny light gold","supplied_by":"factory"},
                   {"type":"zipper","placement":"cuffs","spec":"N°5 single slider"},
                   {"type":"chain","placement":"leather loop","finish":"silver/nickel",
                    "supplied_by":"client"} ],
  "labelling": "embossed main label, C.BACK centred", "sms_qty": 22, "sms_size": "42" }
```

`leather_substance_mm` feeds the §8 Jackiee cross-check (0.45–0.50 mm); `accessories`
seed the BOM accessory lines (§5/§6); `supplied_by` distinguishes buyer-supplied
trims (cost 0, like BMO-1's buttons) from factory-purchased.

---

## 2. DCM resolution policy — the spine of Stage 2

DCM is the dm² of each material consumed per garment (BMO-1: Sheep Glass **34.5**,
Goat Suede **2.6**). Because no spec sheet provides it, the engine resolves it through
a **mandatory ordered fallback**. Each material `bom_item` records the **source** and
a **confidence**; a lower-numbered source always wins when available.

```
                       ┌── hit ──► DCM (dcm_source=template,      conf 0.95)   ◄── best
 material line ─► (1) style_consumption_template lookup
                       │  miss
                       ▼
                   (2) similar-style retrieval ── hit ──► DCM (dcm_source=similar_style, conf ~0.7)
                       │  miss
                       ▼
                   (3) AI heuristic: POM area × wastage ─────► DCM (dcm_source=ai_estimate, conf ≤0.5, FLAGGED)
                       │
                       ▼
                   (4) cutting-manager manual confirm ──────► DCM (dcm_source=manual,      conf 1.00)  ◄── source of truth
                                                                     └──► written back into (1)
```

**Source 1 — `style_consumption_template` lookup (the DCM memory).** Keyed on a
cross-order-stable style signature + size + material category (§3). A hit is a
previously *confirmed* consumption for the same physical style/material — the cheapest
and most trustworthy answer. Jackiee's "carry-on" pattern reference resolves here via
`pattern_reference.resolved_template_id`.

**Source 2 — similar-style retrieval.** No exact template, but a *near* one exists.
Two interchangeable implementations, config-flagged:
- *Embedding* — encode `(garment_type, key POMs, material)` with the local HF model
  already in the intelligence module (`models_catalog.py`; no chat provider, no extra
  cost) and take the nearest confirmed template above a similarity floor.
- *Rule-based* — match on `garment_type` + material_category + a POM-distance metric
  (e.g. waist+hip+outseam within a tolerance band).
Result carries the source template id for audit; confidence scales with similarity.

**Source 3 — AI heuristic (last resort, always flagged).** Estimate pattern area from
the POMs via a `garment_type.area_formula` (§3) and apply `default_wastage_pct`. For a
track pant, roughly `area ≈ f(outseam, hip, thigh_width, hem_width) × panels`, then
`DCM = area × (1 + wastage)`. This is the *only* place finished measurements touch
consumption, and it is explicitly an **estimate**: `dcm_source = ai_estimate`,
`dcm_confidence ≤ 0.5`, surfaced in the UI with a warning. It exists so a brand-new
style with no neighbour still produces a *draft* number for the cutting manager to
correct — never a silent final value.

**Source 4 — cutting-manager manual confirmation (source of truth).** The human enters
or overrides the DCM; `dcm_source = manual`, `dcm_confidence = 1.00`. On confirmation
the value is **written back** into `style_consumption_template` (§3, §10) so the memory
*learns* — the next order of that style hits Source 1.

Every material `bom_item` therefore carries `dcm_source ∈ {template, similar_style,
ai_estimate, manual}` and `dcm_confidence`, both visible in the editable BOM and
auditable. Non-material lines (manufacturing, packaging, FOB) have no DCM and leave
these null.

---

## 3. Schema additions (patched into stage-0 §1)

New `procurement` tables (same conventions as `models.py`: `UUIDMixin` +
`TimestampMixin`, portable `GUID`, VARCHAR-of-`.value` enums, JSONB-on-PG/JSON-on-SQLite
via `JSON_VARIANT`, cross-module FKs by table-name string only). Registered in
`alembic/env.py` per stage-0 §3.

### 3a. `pom_dictionary` — term → standard code
| Field | Notes |
|---|---|
| `id`, timestamps | |
| `language` | `ja` / `it` / `en` (matches `client_template.language`) |
| `source_term` | original label — `渡り幅`, `inseam` |
| `pom_code` | standardized code — `THIGH_WIDTH` |
| `garment_type?` FK `garment_type` | optional scoping (a term can mean different POMs per type) |
| `weight` | tie-break when two terms collide |
| **unique** `(language, source_term, garment_type)` | one mapping per term per type |
Seeded idempotently from `config/pom_dictionary.yaml` (same pattern as
`seed_templates.py`).

### 3b. `pom_measurement` — per spec sheet, per size, per POM
| Field | Notes |
|---|---|
| `id`, timestamps | |
| `spec_sheet_id` FK `spec_sheet` | |
| `size` | string, by design (`"M"`, `"42"`) — matches `sku.size` |
| `pom_code` | standardized |
| `value` Numeric(10,2) | the measurement |
| `pitch?` Numeric(6,2) | grading step (Beau Geste ピッチ) |
| `tolerance?` JSONB | ± band when present |
| `source_term` | audit — the original label this row came from |
| `extracted_by` | `ExtractionSource` value (deterministic/gemini/groq/manual) |
| `confidence` Numeric(5,4) | |
| **index** `(spec_sheet_id, size, pom_code)` | |

### 3c. `style_consumption_template` — the DCM memory ⚠ cross-order keying
**The keying nuance, stated explicitly:** `Style` rows are **order-scoped**
(`style.client_order_id` FK in `clients/models.py:78`) — the *same physical style* in
two orders is *two* `Style` rows with two ids. So this memory **must not** key on
`style_id`, or every new order would miss. It keys on a **cross-order-stable
signature**:

| Field | Notes |
|---|---|
| `id`, timestamps | |
| `client_id` FK `client` | |
| `style_signature` | normalized, stable across orders: prefer `style.customer_ref` (`CR1-02F5-PL02`) → else `internal_ref` → else slug(`style.name`) |
| `garment_type` FK `garment_type` | |
| `material_category` | `BomItemCategory` value (main_material, sub_material, lining…) |
| `size` | string |
| `dcm_value` Numeric(12,3) | the confirmed consumption |
| `uom` | `dm²` / `pc` |
| `confirmed_by` FK `app_user` | the cutting manager |
| `confirmed_at` | |
| `source_bom_id?` FK `bom` | provenance of the confirmed value |
| **unique** `(client_id, style_signature, garment_type, material_category, size)` | one confirmed DCM per material per size per style |

### 3d. `pattern_reference` — "follow existing pattern X"
| Field | Notes |
|---|---|
| `id`, timestamps | |
| `client_id` FK `client` | |
| `spec_sheet_id?` FK `spec_sheet` | where it was found |
| `pattern_code` | `MIMI-28-09-22-ZZ`, `CR1-O2B5-PL02` |
| `base_size?` | `"42"` (Jackiee) |
| `resolved_style_id?` FK `style` | matched base style, if found |
| `resolved_template_id?` FK `style_consumption_template` | the DCM source it points at |
| `notes` | the source phrase for audit |

### 3e. `garment_type` — required POMs + area formula per type
| Field | Notes |
|---|---|
| `id`, timestamps | |
| `code` (unique) | `TRACK_PANT`, `JACKET`, … |
| `required_poms` JSONB | list of `pom_code` expected (drives a completeness check) |
| `area_formula` JSONB | the Source-3 heuristic per type (term-weighted panel-area expression) |
| `default_wastage_pct` Numeric(5,2) | applied in Source 3 |
Seeded from `config/garment_types.yaml`.

### 3f. Enum + `bom_item` deltas
- **New enum `DcmSource`** in `procurement/enums.py` (str-enum, VARCHAR column —
  matching the module convention):
  `TEMPLATE="template" | SIMILAR_STYLE="similar_style" | AI_ESTIMATE="ai_estimate" |
  MANUAL="manual"`.
- **`bom_item` gains** `dcm_source` (String(20), nullable — null on non-material lines)
  and `dcm_confidence` (Numeric(5,4), nullable). Patch to stage-0 §1c. Everything else
  on `bom`/`bom_item` already exists.
- **`bom` gains** `cutting_confirmed_by` FK `app_user` + `cutting_confirmed_at` (the §10
  gate) — small additions to the existing header.

---

## 4. Per-client extraction adapters (config-driven, no code branches)

Onboarding a client must be a **config** change, mirroring Stage 1's
`client_template` registry. An **adapter** is selected at runtime off
`(spec_type, client_match_code)` — both already on the `Document`/`SpecSheet` rows from
Stage 1 — and reads a per-client extraction config; the *code* is one generic pipeline.

### 4a. Adapter interface (the four steps every adapter implements)
```
Adapter:
  detect(features)            -> bool          # does this adapter own this sheet?
  extract_poms(sheet)         -> [pom rows]    # [] for prose specs
  extract_attributes(sheet)   -> attributes    # leather/colour/accessories → SpecSheet.attributes
  resolve_pattern_ref(sheet)  -> pattern_ref?  # "follow pattern X" → pattern_reference
  → emits the §1a intermediate JSON (validated before persist)
```
A new client supplies an extraction config (field map, which step is deterministic vs
LLM, pattern-ref field, dictionary `language`) in `config/extraction_adapters.yaml`;
no Python branch. Unknown layouts fall to a `_generic` adapter (LLM-only, stricter
confidence floor) — the same "promote-by-config" path Stage 1 uses.

### 4b. Concrete adapter — Beau Geste (tabular Japanese grid)
- `detect`: `spec_type == measurement_grid` **and** `client_match_code == beau_geste`.
- `extract_poms`: **deterministic openpyxl** parse of sheet `CRPL-004` — the numbered
  rows 1–14 in the 規格寸法 block, size columns S–XXL, ピッチ column → `pom_measurement`
  rows. **No LLM** (stage-0 §4: the grid is machine-readable). The handwritten *order*
  side is the only Beau Geste artifact that escalates to Gemini, and that's Stage 1's
  job, not here.
- `extract_attributes`: the materials block (表地/別布/袋地 + the 0.6–0.7 mm 規格) and
  accessories block.
- `resolve_pattern_ref`: regex 特記事項 for `※(…)をベースに作成` → `CR1-O2B5-PL02`.

### 4c. Concrete adapter — Jackiee (prose Italian + pattern reference)
- `detect`: `spec_type == narrative_techpack` **and** `client_match_code == the_jackie`.
- `extract_poms`: returns `[]` — there are none. (The completeness check against
  `garment_type.required_poms` is therefore satisfied *via the pattern reference*, not
  failed.)
- `extract_attributes`: **Gemini structured output** over the 3-column tech sheet →
  the §1d JSON (leather quality/substance, colour, accessories with `supplied_by`,
  labelling, SMS qty/size).
- `resolve_pattern_ref`: the `PATTERN` field → `MIMI-28-09-22-ZZ`, `base_size 42`.

---

## 5. Sample / total price math (grounded in BMO-1.pdf)

BMO-1 (CRIMIE / CRI 02F5 PL02, FOB **US$107.25**) is the worked reference. The
per-garment "Sample Mass" column = `yardage × unit price`; the bulk line multiplies by
order quantity.

### 5a. Main material — the worked example
```
per-garment  = dcm × unit_price            = 34.5 × 1.80 = 62.1   (BMO-1 "Sample Mass")
bulk_qty     = order_qty × dcm             = 60   × 34.5 = 2,070 dm²
bulk_total   = order_qty × dcm × unit_price= 60 × 34.5 × 1.80     = $3,726
```
(Goat suede sub-material: `2.6 × 1.75 = 4.55` per garment, same shape.)

### 5b. Provenance of every number (extracted vs configured vs computed)
| BOM element | Source | Where it comes from |
|---|---|---|
| `dcm` (Sheep Glass 34.5, Goat Suede 2.6) | **resolved** (§2) | template → similar → ai_estimate → manual; *stamped* `dcm_source`/`dcm_confidence` |
| `unit_price` (1.80, 1.75, 1.5…) | **extracted** | order/quote sheet via Gemini; or client price list |
| POMs / materials / accessories | **extracted** | spec sheet (§1, §4) |
| `wastage_pct`, `area_formula` | **configured** | `garment_type` (§3e) — only used by Source 3 |
| Manufacturing (Cutting & Stitching 30.00), Packaging (3.00), FOB charge (5.00) | **configured** | per-client/garment default cost catalog; overridable per BOM |
| Lining 0.6, Interlining 1.00, Threads 1.00 | **extracted/configured** | spec materials block + catalog defaults |
| `total_cost` per line, `bulk_total`, `garment_fob_price` | **computed** | the math above; `garment_fob_price` = Σ per-garment lines (BMO-1 TOTAL = 107.25) |
| Buyer-supplied accessories (BMO-1 buttons @ price 0) | **extracted** | `supplied_by = client/buyer` → unit_price 0, qty recorded |

Each line maps to an existing `BomItemCategory` (`MAIN_MATERIAL`, `SUB_MATERIAL`,
`LINING`, `INTERLINING`, `THREAD`, `ACCESSORY`, `MANUFACTURING`, `PACKAGING`,
`FOB_CHARGE`) — no new categories needed.

---

## 6. Standard BOM schema — one shape, both clients

Every BOM, regardless of client, has the same columns (already modeled on
`bom`/`bom_item` in `models.py`); the difference is only *how each cell is filled*. The
canonical columns and the per-client mapping:

| Standard column (`bom_item`) | Beau Geste (tabular grid) | Jackiee (pattern-reference prose) |
|---|---|---|
| `category` | from materials/accessories block | from prose accessory/material extraction |
| `name` | `SHEEP GLASS`, `GOAT SUEDE`, `YKK 5MA…` | `THIN SUEDE`, `N°5 zipper double slider`, chain |
| `material_color` | `BLACK` | `MAGENTA` |
| `qty_per_garment` (**DCM**) | §2: Source 1 template, else Source 3 from the 14 POMs | §2: Source 1/2 via `pattern_reference` (no POMs to estimate from) |
| `uom` | `dm²` / `pc` | `dm²` / `pc` |
| `unit_price` | order/quote sheet | order/quote sheet (or client price list) |
| `bulk_qty` | `order_qty × dcm` | `22 (SMS) × dcm` |
| `total_cost` | computed | computed |
| `material_color`, `annotation` | handwritten notes ("take care goat suede") | prose warnings ("avoid mismatching color panels") |
| `dcm_source` / `dcm_confidence` | mostly `ai_estimate`/`template` | mostly `template`/`similar_style` (pattern-driven) |

The key asymmetry the table makes explicit: **Jackiee has no POMs**, so its material
DCM *must* come from the pattern reference (Source 1/2) and can never use Source 3;
**Beau Geste has POMs**, so a brand-new Beau Geste style can fall to Source 3 as a last
resort. Both still pass through the §10 confirmation gate.

---

## 7. Editable BOM — format & server contract

### 7a. Client presentation
- **Mobile:** read-only BOM preview; **tap a cell to edit** one value in a focused
  editor (DCM, unit price, qty). Recompute echoed back from the server.
- **Desktop:** spreadsheet-style **inline editing** across the grid, multi-cell edits
  staged client-side and submitted together.

### 7b. Server contract — RESOLVED: bulk PATCH + optimistic `revision` locking
**Decision (one option, per the prompt):** a single **bulk** endpoint
`PATCH /api/v1/procurement/boms/{bom_id}/items` that accepts the changed cells in one
request, guarded by **optimistic locking on `bom.revision`** (the version token already
on the header). **Not** per-cell PATCH; **not** last-write-wins.

```jsonc
// PATCH /boms/{bom_id}/items
{ "base_revision": 4,                       // the revision the client last read
  "edits": [ {"bom_item_id":"…","field":"dcm","value":35.0},
             {"bom_item_id":"…","field":"unit_price","value":1.85} ] }
// 200 → { "revision": 5, "recomputed": { … full recomputed BOM … } }
// 409 stale_revision → { "current_revision": 6 }  // client refetches, replays
```

**Why this and not the alternatives:**
- *Bulk over per-cell* — a single edit cascades (DCM → line total → `bulk_total` →
  `garment_fob_price`, §9). The recompute must be **atomic**; per-cell PATCH would
  fracture an interdependent change into several non-atomic writes and multiply
  `audit_log` rows for what is one logical edit.
- *Optimistic `revision` over LWW* — Stage 3 lock + the "full revision history"
  requirement already pivot on `bom.revision`; reusing it as the concurrency token is
  free, gives a clean `409` on a stale base, and prevents two managers silently
  clobbering each other. LWW would lose the loser's edit with no signal. Each accepted
  bulk edit increments `revision` and writes one `audit_log` before/after diff.
- A `LOCKED` BOM (Stage 3) rejects all edits → `409 bom_locked`.

---

## 8. Cross-check rules per client (config, not hard-coded)

Validation rules live in **per-client YAML/JSON**, extending the
`client_template`/`seed_templates.py` pattern with a `bom_checks` block — onboarding a
client's checks is config, not a code branch. Each rule yields a non-blocking **flag**
on the draft (surfaced to the cutting manager) unless marked `severity: error`.

```yaml
# config/bom_checks.yaml
- client_code: beau_geste
  checks:
    - { id: leather_thickness, field: spec.substance_mm, range: [0.6, 0.7], severity: warn }   # 規格 column
    - { id: size_pitch_monotonic, kind: pom_pitch_monotonic, severity: warn }                  # grading steps consistent
    - { id: qty_sum, kind: size_qty_sum_equals_total, severity: error }                        # Σ per-size qty == order total
- client_code: the_jackie
  checks:
    - { id: pattern_resolves, kind: pattern_reference_exists, severity: error }                # MIMI-28-09-22-ZZ must resolve
    - { id: leather_substance, field: spec.substance_mm, range: [0.45, 0.50], severity: warn }
```

Rule *kinds* (`pom_pitch_monotonic`, `size_qty_sum_equals_total`,
`pattern_reference_exists`, range checks) are generic engine primitives; the *which/
where* is config. New client → new YAML entry.

---

## 9. Recalculation triggers / dependency graph

Edits cascade along a small DAG. The non-negotiable rule: **an edit to finished
measurements never silently overrides a trustworthy DCM.**

```
 POM edit ─────► re-estimate area (Source 3 ONLY) ─┐
                                                    ├─► proposes a NEW ai_estimate DCM,
 (does NOT touch a template/manual/similar DCM) ────┘   shown as a suggestion, not applied

 DCM edit ─────► recompute line total_cost ─► recompute bulk_total ─► recompute garment_fob_price
            └──► if edited AFTER cutting confirmation → flip dcm_source to manual-pending,
                 clear cutting_confirmed_* → REQUIRES re-confirmation (§10)

 Quantity edit ► recompute bulk_qty (= order_qty × dcm) ─► recompute bulk_total

 unit_price edit ► recompute line total_cost ─► bulk_total ─► garment_fob_price
```

- **POM edit → Source 3 only.** It re-runs the area heuristic and *proposes* a new
  `ai_estimate`, but it must **not** overwrite a `template`/`manual`/`similar_style`
  DCM — those are more trustworthy than any measurement-derived estimate. The proposal
  is surfaced; a human decides.
- **DCM edit → recompute + maybe re-gate.** Recomputes the cost rollup; if the BOM was
  already cutting-confirmed, the edit **invalidates** the confirmation (clears
  `cutting_confirmed_*`, sets the line to manual-pending) and the §10 gate must run
  again.
- **Quantity edit → bulk recompute.** `bulk_qty = order_qty × dcm`; rolls up
  `bulk_total`. Order qty itself comes from the order sheet (per-size SKU quantities).

All recomputation is server-side (the bulk-PATCH response returns the recomputed tree),
never trusted from the client — consistent with the house rule "compute at write time,
never recompute on read."

---

## 10. Cutting-manager confirmation gate

A **mandatory gate before BOM finalization, regardless of `dcm_source`** — even a
`template` (conf 0.95) or `manual` line is explicitly confirmed. It sits **between
Stage-2 generation and Stage-3 MD approval**:

```
 Stage 2 generate ─► DRAFT bom (DCM resolved per §2, flags per §8)
        │
        ▼
 CUTTING MANAGER reviews every material line's DCM + source + confidence
        │   - overrides where needed (Source 4 → dcm_source=manual)
        │   - must act on every ai_estimate line (cannot finalize with an unconfirmed estimate)
        ▼
 CONFIRM  ─► stamp bom.cutting_confirmed_by / _at
            ─► write audit_log (BOM_CUTTING_CONFIRM, before/after)
            ─► BACK-FILL style_consumption_template for each confirmed material line (§2 Source 4, §3c)
        │
        ▼
 Stage 3: MD approve / lock   (the BOM cannot be approved until cutting_confirmed_at is set)
```

- **Role:** maps to the existing `CUTTING_MANAGER` role (no new role); MD may also act
  as superuser per stage-0 §5. The Stage-3 approve endpoint **rejects** a BOM whose
  `cutting_confirmed_at` is null.
- **Learning:** confirmation back-fills `style_consumption_template`, so the *next*
  order of that style is a Source-1 template hit (§2). This is the mechanism that turns
  a one-time human correction into durable factory memory.
- **Re-gate on late edits:** any DCM edit after confirmation clears the stamp (§9) and
  forces re-confirmation — a confirmed number can never be silently changed underneath
  the MD.

---

## 11. Acceptance criteria (using the real `/data` files)

1. **Beau Geste POM extraction.** `data/spec_sheet_1.xlsx` → 14 `pom_measurement` rows
   × 5 sizes, standardized codes (`渡り幅` → `THIGH_WIDTH`, etc.), `extracted_by:
   deterministic`, `source_term` preserved; the 特記事項 base pattern recorded as a
   `pattern_reference` (`CR1-O2B5-PL02`). **No LLM call** on the grid.
2. **Jackiee pattern-reference + prose.** `data/Jackie-cleint-spec-sheet.xlsx` → **zero
   POMs**, a `pattern_reference{ MIMI-28-09-22-ZZ, size 42 }`, and `SpecSheet.attributes`
   populated (THIN SUEDE, MAGENTA, zippers with `supplied_by`, SMS 22 / size 42) via the
   Gemini adapter.
3. **DCM is resolved, never silently computed.** Every material `bom_item` carries a
   `dcm_source` + `dcm_confidence`; an `ai_estimate` line is flagged and cannot reach
   `LOCKED` without cutting confirmation.
4. **Cost math reproduces BMO-1.** Sheep Glass `34.5 × 1.80 = 62.1` per garment;
   `60 × 34.5 × 1.80 = $3,726` bulk; `garment_fob_price` Σ = `107.25`.
5. **Memory learns across orders.** A confirmed Beau Geste DCM is found by a *second*
   order of the same `customer_ref` as a Source-1 template hit — proving the
   cross-order signature keying (not `style_id`).
6. **New client by config.** Adding an `extraction_adapters.yaml` + `bom_checks.yaml`
   entry onboards a third client's spec sheet with no code change.
7. **Editable contract.** A bulk `PATCH …/items` with a stale `base_revision` → `409
   stale_revision`; a fresh one returns the recomputed tree and `revision+1`; a `LOCKED`
   BOM rejects edits.
8. **Cross-checks fire.** Beau Geste `Σ size qty == order total` errors when violated;
   Jackiee unresolved `pattern_reference` errors; out-of-band leather substance warns.
9. **Gate enforced.** Stage-3 approval is refused while `cutting_confirmed_at` is null;
   a post-confirmation DCM edit re-opens the gate.

---

## 12. Review checklist (definition of done — spec only, nothing to run)

- [ ] The pattern-vs-POM correction is the governing decision, cited against **both**
      real spec sheets (Beau Geste 14-POM grid / Jackiee prose + pattern reference).
- [ ] POM extraction standardizes *all* points via `pom_dictionary`, preserves
      `source_term`, and detects pattern references in **both** clients.
- [ ] The DCM policy is an ordered, mandatory fallback (template → similar → ai_estimate
      → manual); every line stamps `dcm_source` + `dcm_confidence`; AI heuristic is last
      resort and always flagged.
- [ ] Schema additions are expressed as stage-0 §1 patches: `pom_dictionary`,
      `pom_measurement`, `style_consumption_template`, `pattern_reference`,
      `garment_type`, the `DcmSource` enum, and `bom_item.dcm_source/dcm_confidence` +
      `bom.cutting_confirmed_*` — with the **cross-order keying** nuance called out.
- [ ] Extraction adapters are config-driven (new client = config), consistent with the
      Stage-1 `client_template` registry; Beau Geste (deterministic grid) and Jackiee
      (Gemini prose) adapters are concrete.
- [ ] Price math is worked on BMO-1 with each number tagged extracted / configured /
      computed.
- [ ] The standard BOM schema maps both client formats onto the same `bom_item` rows.
- [ ] The editable contract picks **one** option (bulk PATCH + optimistic `revision`
      locking) with rationale.
- [ ] Cross-check rules are per-client config; recalculation triggers form a DAG where a
      POM edit never overrides a trustworthy DCM.
- [ ] The cutting-manager confirmation gate is mandatory before finalization regardless
      of `dcm_source`, writes `audit_log`, and back-fills the DCM memory.
- [ ] No code, migrations, or model edits were produced — spec only.
- [ ] Reviewed / signed off by MD / system stakeholder.
