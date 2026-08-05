"""
================================================================================
modules/imports/service.py — Import module facade (kept for API stability)
================================================================================

PURPOSE
    A thin, documented facade over the import pipeline. The real work lives in:
      import_engine.build_preview()          — classify + parse a workbook
      load_to_db.load_preview_into_order()    — write a validated preview into a
                                                named client order (idempotent)

HISTORY / MIGRATION NOTE
    The old non-order-scoped load_preview() was retired (F00/F83): every commit
    now targets a specific client order via load_preview_into_order(), so the
    facade requires an order_number. The router calls the engine + loader
    directly today (documented D2 layering debt, CLAUDE.md §13.8); this module
    remains the importable public entry point.
================================================================================
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.imports.import_engine import build_preview
from app.modules.imports.load_to_db import load_preview_into_order


def preview_workbook(path: str) -> dict:
    """Dry-run parse of a workbook into a structured, validated summary."""
    return build_preview(path).summary()


def commit_workbook(db: Session, path: str, *, order_number: str,
                    replace: bool = True) -> dict:
    """Parse then write a workbook into the given client order (idempotent).

    Returns a {"summary", "written"} stats dict.
    """
    preview = build_preview(path)
    written = load_preview_into_order(
        db, preview, order_number=order_number, replace=replace,
    )
    return {"summary": preview.summary(), "written": written}