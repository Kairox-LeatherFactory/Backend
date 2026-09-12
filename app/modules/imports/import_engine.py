"""
import_engine.py — Orchestrates the whole import.

Pipeline:
  1. parse each sheet ONCE; the parse returns its own verdict (ORDER / UNKNOWN)
  2. group by client
  3. build a structured PREVIEW with all warnings  (the dry-run)
  4. on commit, load the preview into the database (idempotently)

The PREVIEW is returned to the user first. Nothing is written until commit().

ONLY ORDER SHEETS ARE IMPORTED. Weekly production/progress sheets used to be
parsed into "production cards" that seeded Operation and Rate rows; that path is
gone. Anything that is not an order sheet is reported and skipped.
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
import openpyxl
from app.modules.imports.parse_orders import parse_order_sheet, OrderLine


# A client is identified by the prefix of its sheet names, e.g.
# "KJ GARMENT ORDER" + "KJ PRODUCTION" -> client key "KJ".
def _client_key(sheet_name: str) -> str:
    name = sheet_name.upper()
    # for marker in (" GARMENT ORDER", "-GARMENT ORDER", " PRODUCTION", "-PRODUCTION"):
    #     if marker in name:
    #         return name.split(marker)[0].strip(" -")
    return name.strip()


# NO `_sheet_type` ANY MORE — THE PARSE IS THE CLASSIFICATION.
#
# There used to be a `_sheet_type(ws)` here that solved the entire sheet just to
# answer "is this an order sheet?", after which `parse_order_sheet` solved it a
# second time to extract from it. Two passes, and — because each applied its own
# thresholds — two answers that could disagree. `NIPAL-NEW PRODUCTION` was
# classified ORDER by the first pass and parsed to zero lines by the second, so a
# whole sheet disappeared from the import without one warning.
#
# `parse_order_sheet` now returns its verdict alongside its lines, so there is one
# pass, one set of thresholds, and no gap between deciding and doing. The word
# check that used to sit at the top of this function lives inside the parser as
# `_is_header_row`, where it already had to exist to split stacked blocks.


@dataclass
class ClientPreview:
    key: str
    order_lines: list = field(default_factory=list)       # list[OrderLine]
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
            out["clients"][key] = {
                "order_lines": len(cp.order_lines),
                "pieces_ordered": pcs,
                "styles": styles,
                "by_style": by_style,
                "warnings": warns,
            }
            out["total_warnings"] += len(warns)
        self.total_warnings = out["total_warnings"]
        return out


def build_preview(source) -> ImportPreview:
    """Parse the whole workbook into a structured, validated preview. No DB writes.

    `source` is a path or any seekable file-like object — the router hands it the
    upload's own spooled stream rather than copying it to a temp file first.
    """
    wb = openpyxl.load_workbook(source, data_only=True)
    preview = ImportPreview()

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        key = _client_key(sheet_name)
        cp = preview.clients.setdefault(key, ClientPreview(key=key))

        # ONE parse. Its verdict is the sheet's type — they cannot disagree,
        # because they are the same answer.
        lines, warns, verdict = parse_order_sheet(ws)
        preview.sheet_report.append(
            {"sheet": sheet_name, "type": verdict, "client": key})

        if verdict == "ORDER":
            cp.order_lines.extend(lines)
            cp.warnings.extend(f"[{sheet_name}] {w}" for w in warns
                               if not w.startswith("OK:"))
        else:
            # Not an order sheet — a spec sheet, a costing sheet, a weekly
            # production sheet. Say which sheet and why, then skip it.
            cp.warnings.append(f"[{sheet_name}] not an order sheet; skipped")
            cp.warnings.extend(f"[{sheet_name}] {w}" for w in warns
                               if not w.startswith("OK:"))

    return preview