"""
import_engine.py — Orchestrates the whole import.

Pipeline:
  1. classify each sheet (ORDER vs PRODUCTION vs UNKNOWN) and group by client
  2. parse each sheet with the right parser
  3. build a structured PREVIEW with all warnings  (the dry-run)
  4. on commit, load the preview into the database (idempotently)

The PREVIEW is returned to the user first. Nothing is written until commit().
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import openpyxl
from app.modules.imports.excel_reader import clean_str
from app.modules.imports.parse_orders import parse_order_sheet, OrderLine
from app.modules.imports.parse_production import parse_production_sheet, ProductionCard


# A client is identified by the prefix of its sheet names, e.g.
# "KJ GARMENT ORDER" + "KJ PRODUCTION" -> client key "KJ".
def _client_key(sheet_name: str) -> str:
    name = sheet_name.upper()
    for marker in (" GARMENT ORDER", "-GARMENT ORDER", " PRODUCTION", "-PRODUCTION"):
        if marker in name:
            return name.split(marker)[0].strip(" -")
    return name.strip()


def _sheet_type(ws) -> str:
    scan_rows = min(ws.max_row, 20)   # or just ws.max_row for small sheets
    for r in range(1, scan_rows + 1):
        for c in (1, 2):
            v = clean_str(ws.cell(r, c).value)
            if v and v.upper() == "WEEK PERIOD":
                return "PRODUCTION"
    for r in range(1, scan_rows + 1):
        for c in range(1, 5):
            v = clean_str(ws.cell(r, c).value)
            if v and v.upper() in ("S.NO", "SNO", "STYLE", "DATE"):
                return "ORDER"

    # THE WORDS ARE NOT THE SHEET. A perfectly ordinary Italian order sheet
    # headed `Modello | Materiale | Colore | 38 | 40 | …` says none of those four
    # tokens, so it used to be classified UNKNOWN and skipped in silence — the
    # whole order simply never arrived. A breakdown sheet is recognisable by its
    # SHAPE instead: somewhere on it a contiguous run of quantity columns adds up
    # to a printed total. If that shape is there, it is an order sheet, whatever
    # language it is written in.
    # PROVEN, NOT MERELY PLAUSIBLE. Classification demands HIGH confidence: a
    # contiguous run of columns that reconciles against a printed total on every
    # data row. MEDIUM — "these columns look like a band but nothing on the sheet
    # confirms it" — is not enough to decide what a DOCUMENT is.
    #
    # That distinction is what keeps a costing workbook out. `SAMPLE COSTING
    # SHEET FOR SIR.xlsx` offers plenty of two-column runs that add up somewhere,
    # and on shape alone it came back as a 16,649,667-piece order; none of them
    # reconciles across the sheet, so all of them are MEDIUM. Every genuine order
    # sheet here — BOGGI, John Peter, the legacy multi-block workbook — is HIGH.
    #
    # A sheet whose header DOES name itself (S.NO / STYLE / DATE above) never
    # reaches this branch, so a human-recognisable order sheet is still accepted
    # at MEDIUM. Only the shape-only path has to meet the higher bar.
    from app.modules.imports._size_band import find_header_row
    header_row, band = find_header_row(ws)
    # AND IT MUST HOLD FOR MORE THAN ONE ROW. A single row that happens to add
    # up is a coincidence — a costing sheet reconciles 576000 = 48000 x 12 on one
    # line and nothing else. A breakdown repeats its shape down the page, so two
    # reconciling rows is the difference between a layout and an accident.
    if (header_row is not None and band.confidence == "HIGH"
            and len(band.size_cols) >= 2 and band.reconciled_rows >= 2):
        return "ORDER"
    return "UNKNOWN"


@dataclass
class ClientPreview:
    key: str
    order_lines: list = field(default_factory=list)       # list[OrderLine]
    production_cards: list = field(default_factory=list)   # list[ProductionCard]
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportPreview:
    clients: dict = field(default_factory=dict)            # key -> ClientPreview
    sheet_report: list = field(default_factory=list)       # per-sheet classification
    total_warnings: int = 0

    def summary(self) -> dict:
        out = {"clients": {}, "sheets": self.sheet_report, "total_warnings": 0}
        for key, cp in self.clients.items():
            pcs = sum(l.total for l in cp.order_lines)
            styles = sorted({l.style for l in cp.order_lines})

            # Per-style size breakdown (e.g. {"S": 40, "M": 53, "L": 52, "XL": 7})
            # instead of only a collapsed grand total. Lines for the same style
            # (continuation rows, multiple colours, etc.) are summed together.
            by_style: dict[str, dict] = {}
            for line in cp.order_lines:
                entry = by_style.setdefault(line.style, {
                    "sizes": {}, "pieces_ordered": 0,
                    # THE COMMERCIAL FACTS, SHOWN BEFORE COMMIT. They are the two
                    # numbers on the sheet a human can actually check at a glance
                    # — a mis-read price or ship date is far more expensive than
                    # a mis-read quantity, and far less obvious afterwards.
                    "unit_price": None, "currency": None, "delivery_date": None,
                    # MEDIUM here means the size band could not be proved against
                    # a printed total. The reason is in `warnings`.
                    "size_confidence": "HIGH",
                })
                for size, qty in line.sizes.items():
                    entry["sizes"][size] = entry["sizes"].get(size, 0) + qty
                entry["pieces_ordered"] += line.total
                # Same precedence the loader applies, so the preview shows what
                # the commit will actually write: price first-wins, delivery
                # earliest-wins.
                if entry["unit_price"] is None and line.unit_price is not None:
                    entry["unit_price"] = float(line.unit_price)
                    entry["currency"] = line.currency
                if line.delivery_date is not None:
                    prev = entry["delivery_date"]
                    entry["delivery_date"] = (
                        line.delivery_date.isoformat() if prev is None
                        else min(prev, line.delivery_date.isoformat()))
                if line.size_confidence != "HIGH":
                    entry["size_confidence"] = line.size_confidence

            warns = list(cp.warnings)
            for card in cp.production_cards:
                warns += [f"[{card.title}] {w}" for w in card.warnings]
            out["clients"][key] = {
                "order_lines": len(cp.order_lines),
                "pieces_ordered": pcs,
                "styles": styles,
                "by_style": by_style,
                "production_cards": len(cp.production_cards),
                "warnings": warns,
            }
            out["total_warnings"] += len(warns)
        self.total_warnings = out["total_warnings"]
        return out


def build_preview(path: str) -> ImportPreview:
    """Parse the whole workbook into a structured, validated preview. No DB writes."""
    wb = openpyxl.load_workbook(path, data_only=True)
    preview = ImportPreview()

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        stype = _sheet_type(ws)
        key = _client_key(sheet_name)
        preview.sheet_report.append({"sheet": sheet_name, "type": stype, "client": key})

        cp = preview.clients.setdefault(key, ClientPreview(key=key))
        if stype == "ORDER":
            lines, warns = parse_order_sheet(ws)
            cp.order_lines.extend(lines)
            cp.warnings.extend(f"[{sheet_name}] {w}" for w in warns
                               if not w.startswith("OK:"))
        elif stype == "PRODUCTION":
            cards, warns = parse_production_sheet(ws)
            cp.production_cards.extend(cards)
            cp.warnings.extend(f"[{sheet_name}] {w}" for w in warns)
        else:
            cp.warnings.append(f"[{sheet_name}] could not classify sheet; skipped")

    return preview