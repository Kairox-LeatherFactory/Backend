"""Importer tests against the real garment workbook (sync build_preview)."""
import os

import pytest

from app.modules.imports.import_engine import build_preview

_FILENAME = "GARMENT_ORDERPRODUCTION_DETAILS.xlsx"
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Resolve the real workbook from (1) an explicit env override, (2) the project's
# ./data folder, (3) the legacy container mount — first one that exists wins.
_CANDIDATES = [
    os.environ.get("IMPORTER_TEST_FILE", ""),
    os.path.join(_PROJECT_ROOT, "data", _FILENAME),
    f"/mnt/project/{_FILENAME}",
]
REAL_FILE = next((p for p in _CANDIDATES if p and os.path.exists(p)), _CANDIDATES[1])
HAVE_FILE = os.path.exists(REAL_FILE)
skip_no_file = pytest.mark.skipif(not HAVE_FILE, reason="real garment file not present")


@skip_no_file
def test_all_sheets_classified():
    preview = build_preview(REAL_FILE)
    # 7 order sheets + 6 production sheets = 13; none unclassified.
    assert len(preview.sheet_report) == 13
    assert all(s["type"] in ("ORDER", "PRODUCTION") for s in preview.sheet_report)


@skip_no_file
def test_no_literal_STYLE_leaks_in_as_data():
    preview = build_preview(REAL_FILE)
    for cp in preview.clients.values():
        names = {line.style.upper() for line in cp.order_lines}
        assert "STYLE" not in names


@skip_no_file
def test_known_client_totals():
    preview = build_preview(REAL_FILE).summary()
    assert preview["clients"]["GGZ"]["pieces_ordered"] == 146
    assert preview["clients"]["NIPAL"]["pieces_ordered"] == 259
    assert preview["clients"]["RICANO"]["pieces_ordered"] == 150


@skip_no_file
def test_deterministic_across_runs():
    sigs = []
    for _ in range(3):
        s = build_preview(REAL_FILE).summary()
        sigs.append(tuple(sorted(
            (k, v["pieces_ordered"], v["order_lines"]) for k, v in s["clients"].items()
        )))
    assert all(sig == sigs[0] for sig in sigs)
