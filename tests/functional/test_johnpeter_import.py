"""
================================================================================
tests/test_johnpeter_import.py — Import service against REAL data/johnpeter.xlsx
================================================================================
johnpeter.xlsx is the "7th flat-format client": a single flat sheet
(Date | Style | Suede Colour | Article | sizes...). It is parsed by the generic
imports parser (no johnpeter-specific branch) and, in seed.py, loaded straight
into the Client -> ClientOrder -> Style -> SKU graph.

These tests cover, against the real workbook (skipped if absent):
  1. imports SERVICE preview       (preview_workbook / build_preview)
  2. the ORDER-sheet parser        (parse_order_sheet)
  3. a full DB load into `clients` + read-back via ClientService / AnalyticsService

KNOWN-GOOD TOTALS (computed from the shipped file):
    76 order lines · 17 distinct styles · 1425 pieces · 0 warnings
    first line: CARNABY / PINE GREEN GOAT SUEDE / {S:24,M:53,L:52,XL:23} = 152
================================================================================
"""
import os

import openpyxl
import pytest

from app.modules.analytics.service import AnalyticsService
from app.modules.clients import models as cm
from app.modules.clients.service import ClientService
from app.modules.imports.import_engine import build_preview
from app.modules.imports.parse_orders import parse_order_sheet
from app.modules.imports.service import preview_workbook

# THREE dirnames: tests/functional/<file> -> tests/ -> backend/. The two-dirname
# version resolved to tests/, so every fixture "did not exist" and this whole
# file skipped silently for however long it has been in the tree.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
JP_FILE = os.environ.get("JOHNPETER_TEST_FILE") or os.path.join(
    _PROJECT_ROOT, "data", "johnpeter.xlsx")
HAVE_FILE = os.path.exists(JP_FILE)
skip_no_file = pytest.mark.skipif(not HAVE_FILE, reason="data/johnpeter.xlsx not present")

EXPECTED_LINES = 76
EXPECTED_STYLES = 17
EXPECTED_PIECES = 1425


# ─────────────────────────────────────────── 1. imports SERVICE (sync, no DB)
@skip_no_file
def test_johnpeter_preview_service():
    summary = build_preview(JP_FILE).summary()
    # flat single sheet -> one client key derived from the sheet name
    assert len(summary["clients"]) == 1
    (data,) = summary["clients"].values()
    assert data["pieces_ordered"] == EXPECTED_PIECES
    assert data["order_lines"] == EXPECTED_LINES
    assert len(data["styles"]) == EXPECTED_STYLES
    assert summary["total_warnings"] == 0

    facade = preview_workbook(JP_FILE)          # thin service wrapper
    assert facade["total_warnings"] == 0
    assert sum(c["pieces_ordered"] for c in facade["clients"].values()) == EXPECTED_PIECES


# ─────────────────────────────────────────── 2. the parser itself
@skip_no_file
def test_johnpeter_parser():
    wb = openpyxl.load_workbook(JP_FILE, data_only=True)
    lines, warnings, verdict = parse_order_sheet(wb.active)
    assert verdict == "ORDER"
    assert warnings == []
    assert len(lines) == EXPECTED_LINES
    assert len({l.style for l in lines}) == EXPECTED_STYLES
    assert sum(l.total for l in lines) == EXPECTED_PIECES

    first = lines[0]
    assert first.style == "CARNABY"
    assert first.color == "PINE GREEN"        # suede colour column
    assert first.article == "GOAT SUEDE"      # article column (distinct field)
    assert first.sizes == {"S": 24, "M": 53, "L": 52, "XL": 23}
    assert first.total == 152


# ─────────────────────────────── 3. full DB load + read-back through services
@skip_no_file
@pytest.mark.asyncio
async def test_johnpeter_load_into_clients(db):
    """Mirror seed.py's flat-order load into the async clients graph, then verify
    the numbers survive round-trip through ClientService / AnalyticsService."""
    wb = openpyxl.load_workbook(JP_FILE, data_only=True)
    lines, _, _ = parse_order_sheet(wb.active)

    client = cm.Client(name="John Peter", country="Italy")
    db.add(client); await db.flush()
    order = cm.ClientOrder(client_id=client.id, order_number="JOHNPETER-PO")
    db.add(order); await db.flush()

    styles: dict[str, cm.Style] = {}
    skus: dict[tuple, cm.SKU] = {}          # (style, colour, size) -> SKU (dedupe + sum)
    for line in lines:
        style = styles.get(line.style)
        if style is None:
            style = cm.Style(client_order_id=order.id, name=line.style,
                             article=line.article, production_status="RELEASED")
            db.add(style); await db.flush()
            styles[line.style] = style
        for size, qty in line.sizes.items():
            key = (line.style, line.color or "NA", size)
            existing = skus.get(key)
            if existing is None:
                sku = cm.SKU(style_id=style.id, color_code=line.color or "NA",
                             color_name=line.color, size=size, qty_ordered=qty)
                db.add(sku); skus[key] = sku
            else:
                existing.qty_ordered += qty
    await db.commit()

    assert len(styles) == EXPECTED_STYLES
    assert sum(s.qty_ordered for s in skus.values()) == EXPECTED_PIECES

    # read back through the public service layer
    cs = ClientService(db)
    loaded_clients = await cs.list_clients()
    assert [c.name for c in loaded_clients] == ["John Peter"]

    carnaby = styles["CARNABY"]
    carnaby_skus = await cs.get_skus_for_style(carnaby.id)
    assert sum(s.qty_ordered for s in carnaby_skus) == 152    # first style total

    # analytics aggregate must see every ordered piece
    overview = await AnalyticsService(db).factory_overview()
    assert overview["clients"] == 1
    assert overview["styles"] == EXPECTED_STYLES
    assert overview["total_pieces_ordered"] == EXPECTED_PIECES
