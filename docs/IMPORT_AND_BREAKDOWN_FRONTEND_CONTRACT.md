# KairoX ERP — Breakdown Import: sizes, price and delivery date

**Generated:** 22 August 2026 · **Base URL:** `/api/v1` · **Auth:** Bearer JWT
**Companion to:** *Accessory Kit & Per-Piece Material Spec* (21 Aug 2026)
**Audience:** the two frontend developers.

---

## 0 · What changed, in one paragraph

The breakdown importer used to recognise sizes from a **maintained list of size
names** (`XS/S/M/L/XL/…` plus "any integer"). That list decided what a size was,
and it was wrong in both directions: a brand shipping Italian 38–62 or Japanese
LL/3L had its columns silently dropped, and any stray integer header — a year, an
item code — was invented into a size. Sizes are now **solved arithmetically**
against the total the sheet already prints, so a new market needs no code change.

At the same time the importer now reads the two commercial facts printed on every
style row that did not previously survive the import at all: **unit price** and
**delivery date**.

**Concretely, three real order sheets that imported as ZERO lines now import
correctly** — both BOGGI sheets (their header row says `STYLE`, not `S.NO`) and
the John Peter consolidated sheets (Italian headers, so the sheet was classified
`UNKNOWN` and skipped without a word).

---

## 1 · What you must change

Nothing is required. Every change below is **additive** on responses you already
read. Two things are worth building, and one is worth showing:

| Priority | Change |
|---|---|
| **Worth building** | Show `unit_price` / `currency` / `delivery_date` on the preview screen, so the human checks them BEFORE commit |
| **Worth building** | Show the `size_confidence` badge and the sheet warnings |
| **Worth showing** | Link clients to the breakdown template (section 5) |

---

## 2 · The endpoints

Unchanged in path, method, auth and request shape:

```
POST /api/v1/imports/preview    multipart: order_number (Form) + file   -> dry run
POST /api/v1/imports/commit     multipart: order_number (Form) + file   -> writes
```

Both are **DM only**. `preview` writes nothing.

### 2.1 · CHANGED · the preview payload

`by_style` gains four keys per style. Everything already there is untouched.

```jsonc
{
  "clients": {
    "FOGLIO1": {
      "order_lines": 2,
      "pieces_ordered": 795,
      "styles": ["BO27P082702", "BO27P082901"],
      "by_style": {
        "BO27P082702": {
          "sizes": { "S": 56, "M": 129, "L": 143, "XL": 92, "XXL": 28, "XXXL": 1 },
          "pieces_ordered": 449,

          "unit_price": 83.0,             // NEW - a number, or null
          "currency": "EUR",              // NEW - ISO code, or null
          "delivery_date": "2026-09-15",  // NEW - ISO date, or null
          "size_confidence": "HIGH"       // NEW - HIGH | MEDIUM
        }
      },
      "warnings": []
    }
  },
  "sheets": [ { "sheet": "Foglio1", "type": "ORDER", "client": "FOGLIO1" } ],
  "total_warnings": 0
}
```

### 2.2 · `size_confidence` — the only new thing worth a UI decision

| Value | Meaning | Render |
|---|---|---|
| `HIGH` | Every data row's size cells summed **exactly** to the total the sheet printed. The band is proven. | Nothing special |
| `MEDIUM` | The sheet printed **no total** to check against, or a printed total did **not** add up. The band is a reasonable reading, not a proven one. | **A visible badge, and show `warnings`.** Let the human commit, but make them look first. |

A `MEDIUM` sheet is not an error and must not block the commit button. It is the
importer saying *"I could not prove this — you look"*. The specific reason is in
`warnings`, in plain sentences, e.g.

> `Row 14: the sheet prints a total of 30 but its size cells sum to 27.`

### 2.3 · Price and delivery, and what to do when rows disagree

Both are printed **per style row**, but a style has several rows (one per
colourway), so the same fact arrives repeatedly and the rows can disagree. The
server resolves this and **tells you it did**:

- **Price** — the first non-empty value wins. A later row that disagrees raises a
  warning naming the style and **both** numbers.
- **Delivery** — the **earliest** date wins, because a ship date is a commitment
  and planning to the later one would miss it. The disagreement is warned about.
- **Currency** — the symbol in the cell, else the order's currency, else the
  client's. If none of the three answers, `currency` is `null` and a warning says
  so. It is never invented.

Surface those warnings next to the style row. They are the only signal that the
sheet the client sent contradicts itself.

### 2.4 · Sizes arrive verbatim

`"XL "`, `"xl"` and `"XL"` are folded to one size — otherwise they would fork
into three SKUs, three codes and three barcodes for the same garment. Nothing
else is translated: `"38"` stays `"38"`, `"XXL/54"` stays `"XXL/54"`. Whatever
the client wrote is what lands in `SKU.size`, in the SKU code and on the barcode
caption, so **do not normalise sizes client-side** — you would desynchronise the
printed label from the database.

Order your size columns by the order the keys arrive in `sizes`, not
alphabetically: `"38","40","42"` sorts correctly by luck, `"S","M","L","XL"` does
not.

---

## 3 · Style gains one field

`Style.delivery_date` (ISO date, nullable) — the ship date printed on that
style's row. **This is the only schema change in this work.**

**It is per STYLE, not per order.** `ClientOrder.delivery_deadline` still exists
and is unchanged; the two answer different questions. One order sheet routinely
carries a main-season style and an outlet style with different ship dates, and
collapsing them onto the order would hide the earlier one.

`null` is ordinary, not an error: plenty of sheets print no delivery column at
all (the John Peter consolidated sheets do not).

---

## 4 · Sheet classification got wider — and one guard

A sheet used to be recognised as an order sheet only if the words `S.NO`,
`STYLE` or `DATE` appeared in its first four columns. An ordinary Italian sheet
headed `Modello | Materiale | Colore | 38 | 40 | …` matched none of them and was
skipped in silence — the whole order simply never arrived.

Sheets are now also recognised **by shape**: a contiguous run of quantity columns
that reconciles against a printed total, on more than one row, under labels that
look like sizes.

**The guard you may see.** A costing workbook has the same arithmetic as a
breakdown, so shape alone is not enough — on the first cut, a costing sheet came
back as a 16,649,667-piece order. Classification therefore also requires the
band's **labels** to be size-shaped: short, and if numeric, under 1000. A run
headed `576000 | 0.05 | 28800` is a cost sheet, not a size range. If a client's
sheet returns `UNKNOWN` and you believe it is an order sheet, that constraint is
the first thing to check.

---

## 5 · The template to send clients

`data/templates/KairoX_Breakdown_Sheet_TEMPLATE.xlsx` — two tabs: a filled
example, and a "HOW TO FILL THIS IN" sheet.

```
 A            B        C               D   E   F   G   H    I     J      K            L
 STYLE        COLOUR   DESCRIPTION  |  S   M   L   XL  XXL  XXXL | TOTAL  PRICE        DELIVERY
 BO27P082702  TAUPE    SUEDE BLOUSON|  56  129 143 92  28   1    |  449   83           2026-09-15
 BO27P082901  BEIGE    SUEDE JACKET |  43  97  112 68  22   4    |  346   93           2026-09-15
 BO27P083104  BLACK    SUEDE GILET  |  12  30  34  18  6    0    |  100   EUR 78,50 cif 2026-10-02
 |___ descriptor ______________|      |___ size band _________|   |___ tail __________________|
```

**Clients do not have to use it.** The importer reads the shape, not the wording:
sizes may be any label, `PRICE` may sit before or after `TOTAL`, `DELIVERY` may
be absent entirely, and blocks may be stacked with different size ranges. The
template is simply the shape that reads most reliably.

The one thing that genuinely matters is that **TOTAL equals the sum of that
row's size cells** — that sum is how the importer proves which columns are sizes.
A `=SUM()` formula is fine; Excel stores the calculated value alongside it.

---

## 6 · Nothing you have today breaks

| Surface | Verdict |
|---|---|
| `POST /imports/preview` and `/commit` | Path, method, auth and request unchanged |
| `by_style` entries | +4 keys, none renamed or removed |
| `sizes` | Same shape. Labels now arrive verbatim from the sheet |
| `warnings` | Same shape — a list of plain sentences. There may be more of them |
| `ClientOrder.delivery_deadline` | Untouched |
| Release / mint path | Untouched |
| SKU codes and barcodes | Unchanged in construction; sizes flow through verbatim as before |

---

## 7 · Known limitations, stated plainly

- **`.xls` (pre-2007) files are not readable.** `openpyxl` supports `.xlsx` only,
  so the Confecciones sheets and most of the CONFEZIONI ORFATTI sheets in
  `data/` cannot be imported at all. This predates the work described here and
  is unchanged by it; supporting them needs a second reader (`xlrd`).
- **A one-size order sheet with unrecognised headers** will not be classified by
  shape, because a single reconciling column is indistinguishable from a costing
  line. It still imports if its header uses a recognised word.
