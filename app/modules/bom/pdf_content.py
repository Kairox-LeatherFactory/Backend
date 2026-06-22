"""
================================================================================
modules/bom/pdf_content.py — PDF content layer (PyMuPDF / fitz)
================================================================================

PURPOSE
    Turn raw PDF bytes into the input the LLM/heuristic extractors actually read.
    A single decision lives here: does this PDF have a usable TEXT layer, or is it
    a scan/photo that needs to go to the model as page IMAGES?

PURE + SYNC. No DB, no async, never raises. PyMuPDF (fitz) does the work; if fitz
is missing or the file is unrenderable, every function degrades to "" / [] / True
(treat-as-scanned) so the caller's vision/heuristic fallback fires instead of an
exception. This matches CLAUDE.md's "no extraction path ever raises".

WHY A DENSITY CHECK, NOT just "any text?"
    Both real client order PDFs (Beau Geste handwritten, Jackiee) return ZERO chars
    from PyMuPDF — they are image-only. But a poorly-OCR'd or partially-tagged PDF
    can leak a few stray glyphs. `pdf_is_scanned` therefore measures chars-PER-PAGE
    against a configurable floor (settings.pdf_text_density_min, default 40), so a
    faint text layer does not wrongly route a scan through the text path (risk #1).

FUNCTION GUIDE
  pdf_extract_text(data) -> str
      All-page text via PyMuPDF. "" on any failure (missing fitz / corrupt file).
  pdf_page_count(data) -> int
      Page count (0 on failure).
  pdf_is_scanned(data) -> bool
      True when chars-per-page is below the density floor (or fitz can't read it).
  pdf_render_pages(data, dpi, max_pages) -> list[bytes]
      First `max_pages` pages as PNG bytes (200-300 DPI). [] when fitz/render fails.
  pdf_to_llm_input(data) -> ("text", str) | ("images", list[bytes])
      THE single entry the orchestrator calls: usable text layer -> ("text", ...);
      scanned/unreadable -> ("images", [...]); both empty -> ("images", []) so the
      caller emits manual_entry_required rather than guessing.
================================================================================
"""
from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)

# Fallback floor if settings doesn't define one (kept here so this module has no hard
# dependency on a new config field landing first).
_DEFAULT_DENSITY_MIN = 40


def _density_min() -> int:
    return int(getattr(settings, "pdf_text_density_min", _DEFAULT_DENSITY_MIN))


def pdf_extract_text(data: bytes) -> str:
    """Extract the full text layer with PyMuPDF. Returns "" if fitz is unavailable or
    the document is unreadable — the caller then treats it as a scan."""
    try:
        import fitz  # PyMuPDF
    except Exception:                                   # noqa: BLE001
        logger.info("PyMuPDF (fitz) unavailable -> no PDF text layer")
        return ""
    try:
        chunks: list[str] = []
        with fitz.open(stream=data, filetype="pdf") as doc:
            for page in doc:
                try:
                    chunks.append(page.get_text("text") or "")
                except Exception:                       # noqa: BLE001
                    continue
        return "\n".join(chunks).strip()
    except Exception as exc:                            # noqa: BLE001
        logger.info("pdf_extract_text failed (%s) -> treat as scanned", exc)
        return ""


def pdf_page_count(data: bytes) -> int:
    try:
        import fitz

        with fitz.open(stream=data, filetype="pdf") as doc:
            return int(doc.page_count)
    except Exception:                                   # noqa: BLE001
        return 0


def pdf_is_scanned(data: bytes) -> bool:
    """A scanned/image-only PDF has (near-)no extractable text. Measured as
    chars-per-page below the configured floor; an unreadable file counts as scanned."""
    pages = pdf_page_count(data)
    text = pdf_extract_text(data)
    if pages <= 0:
        return True
    chars_per_page = len(text) / pages
    scanned = chars_per_page < _density_min()
    logger.info("pdf_is_scanned: pages=%d chars=%d chars/page=%.1f floor=%d -> %s",
                pages, len(text), chars_per_page, _density_min(), scanned)
    return scanned


def pdf_render_pages(data: bytes, dpi: int | None = None,
                     max_pages: int | None = None) -> list[bytes]:
    """Render the first `max_pages` pages to PNG bytes (for the vision model). Returns
    [] when fitz is missing or rendering fails — caller treats [] as 'cannot see it'."""
    dpi = int(dpi or settings.ocr_dpi)
    max_pages = int(max_pages or settings.vision_max_pages)
    try:
        import fitz
    except Exception:                                   # noqa: BLE001
        return []
    pages: list[bytes] = []
    try:
        with fitz.open(stream=data, filetype="pdf") as doc:
            zoom = dpi / 72.0
            matrix = fitz.Matrix(zoom, zoom)
            for page in doc[: max(1, max_pages)]:
                try:
                    pix = page.get_pixmap(matrix=matrix, alpha=False)
                    pages.append(pix.tobytes("png"))
                except Exception:                       # noqa: BLE001
                    continue
    except Exception:                                   # noqa: BLE001
        return []
    return pages


def pdf_to_llm_input(data: bytes) -> tuple[str, str] | tuple[str, list[bytes]]:
    """Single routing entry. ("text", <layer>) for a digital PDF with a usable text
    layer; ("images", <pngs>) for a scan (or when PyMuPDF text extraction failed but
    rasterisation works); ("images", []) when even rasterisation fails -> the caller
    emits an empty result + manual_entry_required."""
    if not pdf_is_scanned(data):
        text = pdf_extract_text(data)
        if text:
            return ("text", text)
        # density said "not scanned" but text came back empty (edge) -> fall to images.
    return ("images", pdf_render_pages(data))