"""Importer tests against the real garment workbook (sync build_preview)."""
import os

import pytest

from app.modules.imports.import_engine import build_preview

_FILENAME = "GARMENT_ORDERPRODUCTION_DETAILS.xlsx"
# THREE dirnames: tests/functional/<file> -> tests/ -> backend/. The two-dirname
# version resolved to tests/, so every fixture "did not exist" and this whole
# file skipped silently for however long it has been in the tree.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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


# The workbook holds 7 order sheets and 6 weekly production sheets. Production
# sheets are no longer imported at all, so the only correct reading is 7 ORDER
# and 6 UNKNOWN. They are named individually on purpose: a production sheet that
# flips back to ORDER injects six-figure garbage (its stage columns parse as a
# size band — GGZ-PRODUCTION alone reports 90,097 pieces), and a bare count would
# not notice if one swapped places with an order sheet.
ORDER_SHEETS = {
    "KJ GARMENT ORDER", "GGZ-GARMENT ORDER", "NIPAL GARMENT ORDER",
    "RICANO-GARMENT ORDER", "JP-GARMENT ORDER-1", "JP-GARMENT ORDER-2",
    "NIPAL-NEW GARMENT ORDER",
}
NOT_ORDER_SHEETS = {
    "KJ PRODUCTION", "GGZ-PRODUCTION", "NIPAL PRODUCTION", "RICANO PRODUCTION",
    "JP-PRODUCTION", "NIPAL-NEW PRODUCTION",
}


@skip_no_file
def test_all_sheets_classified():
    preview = build_preview(REAL_FILE)
    assert len(preview.sheet_report) == 13
    by_type = {}
    for s in preview.sheet_report:
        by_type.setdefault(s["type"], set()).add(s["sheet"])
    assert by_type.get("ORDER", set()) == ORDER_SHEETS
    assert by_type.get("UNKNOWN", set()) == NOT_ORDER_SHEETS


@skip_no_file
def test_a_production_sheet_is_never_read_as_an_order():
    """The zero-band hole, pinned.

    `NIPAL-NEW PRODUCTION` has stage columns that are 0 for seven rows, so a run
    of them "reconciles" against an all-zero total — 0+0+0 == 0 — and used to be
    accepted at HIGH confidence. The sheet was classified ORDER and then parsed
    to zero lines, disappearing from the import without a warning.
    """
    preview = build_preview(REAL_FILE)
    report = {s["sheet"]: s["type"] for s in preview.sheet_report}
    assert report["NIPAL-NEW PRODUCTION"] == "UNKNOWN"
    # And the client it belongs to carries only its real order.
    assert preview.clients["NIPAL-NEW"].order_lines
    assert sum(l.total for l in preview.clients["NIPAL-NEW"].order_lines) == 393


@skip_no_file
def test_no_literal_STYLE_leaks_in_as_data():
    preview = build_preview(REAL_FILE)
    for cp in preview.clients.values():
        names = {line.style.upper() for line in cp.order_lines}
        assert "STYLE" not in names


@skip_no_file
def test_known_client_totals():
    preview = build_preview(REAL_FILE).summary()
    # GGZ is the GRAND-TOTAL-caption case: its caption sits inside the size-50
    # column, and profiling the columns over footer rows drops that column, splits
    # the band and reports 65. See the `profile_rows` note in _size_band.py.
    assert preview["clients"]["GGZ"]["pieces_ordered"] == 146
    assert preview["clients"]["NIPAL"]["pieces_ordered"] == 259
    assert preview["clients"]["RICANO"]["pieces_ordered"] == 150
    assert preview["clients"]["KJ"]["pieces_ordered"] == 347
    assert preview["clients"]["JP"]["pieces_ordered"] == 2263
    assert preview["clients"]["NIPAL-NEW"]["pieces_ordered"] == 393


@skip_no_file
def test_deterministic_across_runs():
    sigs = []
    for _ in range(3):
        s = build_preview(REAL_FILE).summary()
        sigs.append(tuple(sorted(
            (k, v["pieces_ordered"], v["order_lines"]) for k, v in s["clients"].items()
        )))
    assert all(sig == sigs[0] for sig in sigs)
