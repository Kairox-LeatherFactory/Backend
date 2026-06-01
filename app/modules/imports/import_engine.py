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
    """Decide if a sheet is an ORDER sheet or a PRODUCTION sheet."""
    # Production sheets contain a 'WEEK PERIOD' header somewhere near the top.
    for r in range(1, min(ws.max_row, 6) + 1):
        for c in (1, 2):
            v = clean_str(ws.cell(r, c).value)
            if v and v.upper() == "WEEK PERIOD":
                return "PRODUCTION"
    # Order sheets contain an 'S.NO' or 'STYLE' / 'Date' header.
    for r in range(1, min(ws.max_row, 6) + 1):
        for c in range(1, 5):
            v = clean_str(ws.cell(r, c).value)
            if v and v.upper() in ("S.NO", "SNO", "STYLE", "DATE"):
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
            warns = list(cp.warnings)
            for card in cp.production_cards:
                warns += [f"[{card.title}] {w}" for w in card.warnings]
            out["clients"][key] = {
                "order_lines": len(cp.order_lines),
                "pieces_ordered": pcs,
                "styles": styles,
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
