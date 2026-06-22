"""
================================================================================
modules/bom/excel_content.py — Excel content layer (openpyxl ONLY)
================================================================================

PURPOSE
    Flatten an arbitrary workbook into a single markdown blob the LLM can read,
    preserving the three things a layout-agnostic extractor needs:
      - every sheet (a workbook may mix order sheets + production logs),
      - exact DISPLAYED values (data_only=True; no formula strings),
      - cell COORDINATES as inline comments (<!-- A9 -->) so the model can cite a
        measurement back to a cell, and merged-cell context where openpyxl exposes it.

PURE + SYNC, never raises. openpyxl is the only dependency (no pandas). On any
failure the function returns "" so the caller falls through to the heuristic.

OUTPUT SHAPE (per sheet)
    ## <sheet_name>
    A1 <!-- A1 --> CRIMIE | G1 <!-- G1 --> 縫製仕様書 | ...
    A2 <!-- A2 --> サンプル納期 | ...

    Only non-empty cells are emitted, each tagged with its coordinate, so the model
    sees both content and position without the noise of thousands of blank cells.

FUNCTION GUIDE
  _col_letter(idx)                A1-style column letter from a 1-based column index.
  xlsx_to_markdown(data) -> str   The single entry the orchestrator calls.
================================================================================
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)


def _col_letter(idx: int) -> str:
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA' (A1-notation column letter)."""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"


def _merged_note(ws) -> str:
    """A compact note of the sheet's merged ranges (best-effort; read_only sheets do
    not expose merges, so this is empty there — never an error)."""
    try:
        ranges = list(getattr(ws, "merged_cells", []).ranges)  # type: ignore[attr-defined]
    except Exception:                                          # noqa: BLE001
        return ""
    if not ranges:
        return ""
    return "<!-- merged: " + ", ".join(str(r) for r in ranges) + " -->"


def xlsx_to_markdown(data: bytes) -> str:
    """Flatten ALL sheets of a workbook to a coordinate-tagged markdown blob. Uses a
    normal (non-read_only) load so merged-cell ranges are available; falls back to a
    read_only load if the first attempt fails. Returns "" on total failure."""
    wb = None
    try:
        import openpyxl

        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        except Exception:                                      # noqa: BLE001
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:                                   # noqa: BLE001
        logger.info("xlsx_to_markdown: unreadable workbook (%s)", exc)
        return ""

    out: list[str] = []
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            out.append(f"## {sheet_name}")
            merged = _merged_note(ws)
            if merged:
                out.append(merged)
            r = 0
            for row in ws.iter_rows(values_only=True):
                r += 1
                cells: list[str] = []
                for c_idx, value in enumerate(row, start=1):
                    if value is None:
                        continue
                    text = str(value).strip()
                    if not text:
                        continue
                    # Newlines inside a cell break the row layout; flatten them.
                    text = text.replace("\r", " ").replace("\n", " / ")
                    coord = f"{_col_letter(c_idx)}{r}"
                    cells.append(f"{coord} <!-- {coord} --> {text}")
                if cells:
                    out.append(" | ".join(cells))
            out.append("")
    except Exception as exc:                                   # noqa: BLE001
        logger.info("xlsx_to_markdown: partial failure (%s)", exc)
    finally:
        try:
            wb.close()
        except Exception:                                      # noqa: BLE001
            pass
    return "\n".join(out).strip()