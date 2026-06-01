"""
================================================================================
modules/imports/service.py — Import module facade (kept for API stability)
================================================================================

PURPOSE
    A thin, documented facade over the import pipeline. The real work lives in:
      import_engine.build_preview()  — classify + parse a workbook into a preview
      load_to_db.load_preview()      — write a validated preview (idempotent)

HISTORY / MIGRATION NOTE
    An earlier version exposed import_client_workbook() for a single flat
    spreadsheet (clinet1.xlsx). That layout is now handled by the same
    build_preview/load_preview path used for every workbook, so the bespoke
    function was removed. The router and seed script call the engine directly;
    this module remains as the documented public entry point for the module.
================================================================================
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.modules.imports.import_engine import build_preview
from app.modules.imports.load_to_db import load_preview


def preview_workbook(path: str) -> dict:
    """Dry-run parse of a workbook into a structured, validated summary."""
    return build_preview(path).summary()


def commit_workbook(db: Session, path: str, country_map: dict | None = None) -> dict:
    """Parse then write a workbook to the DB (idempotent). Returns load stats."""
    preview = build_preview(path)
    return {"summary": preview.summary(),
            "written": load_preview(db, preview, country_map=country_map)}
