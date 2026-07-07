"""
================================================================================
modules/bom/excel_content.py — Excel content layer (openpyxl ONLY)
================================================================================

PURPOSE
    Flatten an arbitrary workbook into (a) a single markdown blob the LLM can
    read, preserving sheet/coordinate/merge context, and (b) the workbook's
    EMBEDDED IMAGES so a vision model can read the parts that live in pictures
    (garment sketches, handwritten notes, technical diagrams, colour swatches).

WHY IMAGES MATTER (the improvement)
    Real client spec/costing workbooks carry the spec inside images, not just
    cells. Examples from the ground-truth files:
      - Beau Geste CRPL-004 spec:        2 embedded images (garment sketch + a
                                         secondary-process instruction diagram)
      - CLEREMONT WINTER SUEDE techpack: 32 embedded images on one TECH SHEET —
                                         the spec is *mostly* visual
      - SAMPLE COSTING SHEET:            one style photo per costing tab
    The old text-only flatten dropped all of these silently, so the LLM saw a
    sparse grid and missed material/colour/construction detail that was only
    ever drawn. We now surface both text and images; the caller (extraction.py)
    routes to the vision model when images are present.

PURE + SYNC, never raises. openpyxl is the only hard dependency (no pandas).
Pillow is used opportunistically to transcode exotic image formats (WDP/EMF)
that vision models can't read into PNG; if Pillow is absent those images are
simply skipped rather than sent as bytes the model will reject.

OUTPUT
    xlsx_to_markdown(data) -> str
        Backwards-compatible: the coordinate-tagged markdown blob only.
    xlsx_to_llm_input(data) -> tuple[str, list[bytes]]
        (markdown, [image_bytes...]). Images are PNG/JPEG bytes, capped in count
        and per-image size so a picture-heavy workbook can't blow the payload.

FUNCTION GUIDE
  _col_letter(idx)                A1-style column letter from a 1-based index.
  _merged_note(ws) -> str         Compact note of a sheet's merged ranges.
  _image_bytes(img) -> bytes|None Robustly pull raw bytes from an openpyxl image.
  _normalize_image(raw, fmt)      Return web-safe (png/jpeg) bytes or None.
  extract_images(data) -> list[bytes]   All usable embedded images, capped.
  xlsx_to_markdown(data) -> str         Text flatten (unchanged behaviour).
  xlsx_to_llm_input(data) -> (str, list[bytes])   Text + images, one workbook load.
================================================================================
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

# Vision models accept png/jpeg reliably. Everything else we try to transcode,
# and skip if we can't. WDP (HD Photo) and EMF/WMF turn up in Office exports.
_WEB_SAFE = {"png", "jpg", "jpeg"}
# Bounds so a 32-image techpack can't produce a multi-MB base64 payload.
_MAX_IMAGES = 12
_MAX_IMAGE_BYTES = 4 * 1024 * 1024          # 4 MB per image, pre-base64
_MAX_TOTAL_IMAGE_BYTES = 16 * 1024 * 1024   # 16 MB across the workbook


def _col_letter(idx: int) -> str:
    """1 -> 'A', 26 -> 'Z', 27 -> 'AA' (A1-notation column letter)."""
    letters = ""
    while idx > 0:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(65 + rem) + letters
    return letters or "A"


def _merged_note(ws) -> str:
    """A compact note of the sheet's merged ranges (best-effort; read_only sheets
    do not expose merges, so this is empty there — never an error)."""
    try:
        ranges = list(getattr(ws, "merged_cells", []).ranges)  # type: ignore[attr-defined]
    except Exception:                                          # noqa: BLE001
        return ""
    if not ranges:
        return ""
    return "<!-- merged: " + ", ".join(str(r) for r in ranges) + " -->"


def _image_bytes(img) -> tuple[bytes, str] | None:
    """Pull (raw_bytes, format) from an openpyxl image object across versions.

    openpyxl has exposed the payload as `_data` (callable), `ref` (a BytesIO or
    path), and occasionally a plain `data` attribute over its releases. Try each;
    return None if none yield bytes. `format` is a lowercased extension guess.
    """
    fmt = str(getattr(img, "format", "") or "").lower().lstrip(".") or "png"
    # 1) _data() — the common private accessor
    data_attr = getattr(img, "_data", None)
    try:
        if callable(data_attr):
            raw = data_attr()
            if raw:
                return bytes(raw), fmt
        elif isinstance(data_attr, (bytes, bytearray)):
            return bytes(data_attr), fmt
    except Exception:                                          # noqa: BLE001
        pass
    # 2) ref — BytesIO-like or a filesystem path inside the zip wrapper
    ref = getattr(img, "ref", None)
    try:
        if hasattr(ref, "read"):
            ref.seek(0)
            raw = ref.read()
            if raw:
                return bytes(raw), fmt
        elif isinstance(ref, (bytes, bytearray)):
            return bytes(ref), fmt
    except Exception:                                          # noqa: BLE001
        pass
    return None


def _normalize_image(raw: bytes, fmt: str) -> bytes | None:
    """Return web-safe image bytes (png/jpeg) or None.

    png/jpeg pass through untouched. Anything else (wdp/emf/wmf/bmp/tiff/gif) is
    transcoded to PNG via Pillow when available; if Pillow is missing or the
    format is undecodable, the image is dropped (better than sending bytes the
    vision model will reject and error on)."""
    if not raw or len(raw) > _MAX_IMAGE_BYTES:
        return None
    if fmt in _WEB_SAFE:
        return raw
    try:
        from PIL import Image  # optional dependency
    except Exception:                                          # noqa: BLE001
        logger.info("excel image: %s not web-safe and Pillow absent — skipping", fmt)
        return None
    try:
        with Image.open(io.BytesIO(raw)) as im:
            im = im.convert("RGB")
            buf = io.BytesIO()
            im.save(buf, format="PNG")
            return buf.getvalue()
    except Exception as exc:                                   # noqa: BLE001
        logger.info("excel image: could not transcode %s (%s) — skipping", fmt, exc)
        return None


def _load_workbook(data: bytes):
    """Load a workbook for content extraction. Non-read_only so merges AND images
    are exposed; falls back to read_only on failure. Returns the workbook or None."""
    try:
        import openpyxl
    except Exception as exc:                                   # noqa: BLE001
        logger.info("xlsx: openpyxl unavailable (%s)", exc)
        return None
    try:
        return openpyxl.load_workbook(io.BytesIO(data), data_only=True)
    except Exception:                                          # noqa: BLE001
        try:
            # read_only does NOT expose _images, but text still flows.
            return openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        except Exception as exc:                               # noqa: BLE001
            logger.info("xlsx_to_*: unreadable workbook (%s)", exc)
            return None


def _markdown_from_wb(wb) -> str:
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
                    text = text.replace("\r", " ").replace("\n", " / ")
                    coord = f"{_col_letter(c_idx)}{r}"
                    cells.append(f"{coord} <!-- {coord} --> {text}")
                if cells:
                    out.append(" | ".join(cells))
            # Note in-text when a sheet carries pictures, so the (text) model
            # knows visual detail exists even on the no-vision fallback path.
            n_imgs = len(getattr(ws, "_images", []) or [])
            if n_imgs:
                out.append(f"<!-- {n_imgs} embedded image(s) on this sheet -->")
            out.append("")
    except Exception as exc:                                   # noqa: BLE001
        logger.info("xlsx_to_markdown: partial failure (%s)", exc)
    return "\n".join(out).strip()


def _images_from_wb(wb) -> list[bytes]:
    images: list[bytes] = []
    total = 0
    try:
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            for img in list(getattr(ws, "_images", []) or []):
                got = _image_bytes(img)
                if not got:
                    continue
                norm = _normalize_image(*got)
                if not norm:
                    continue
                if total + len(norm) > _MAX_TOTAL_IMAGE_BYTES:
                    logger.info("xlsx images: total-size cap hit — stopping at %d", len(images))
                    return images
                images.append(norm)
                total += len(norm)
                if len(images) >= _MAX_IMAGES:
                    logger.info("xlsx images: count cap (%d) hit", _MAX_IMAGES)
                    return images
    except Exception as exc:                                   # noqa: BLE001
        logger.info("xlsx images: partial failure (%s)", exc)
    return images


def xlsx_to_markdown(data: bytes) -> str:
    """Flatten ALL sheets to a coordinate-tagged markdown blob. Returns "" on total
    failure. (Backwards-compatible entry point — unchanged contract.)"""
    wb = _load_workbook(data)
    if wb is None:
        return ""
    try:
        return _markdown_from_wb(wb)
    finally:
        try:
            wb.close()
        except Exception:                                      # noqa: BLE001
            pass
        
def xls_to_markdown(data: bytes) -> str:
    """Legacy BIFF .xls -> markdown, one section per sheet. Uses xlrd (2.x reads .xls only)."""
    import io, pandas as pd
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, engine="xlrd", header=None)
    except Exception:                                             # noqa: BLE001
        return ""
    parts: list[str] = []
    for name, df in sheets.items():
        df = df.dropna(how="all").dropna(axis=1, how="all")
        if df.empty:
            continue
        rows = ["| " + " | ".join("" if pd.isna(c) else str(c) for c in row) + " |"
                for row in df.itertuples(index=False, name=None)]
        parts.append(f"### {name}\n" + "\n".join(rows))
    return "\n\n".join(parts)


def extract_images(data: bytes) -> list[bytes]:
    """All usable embedded images (PNG/JPEG bytes), capped in count and size.
    Returns [] when there are none or the workbook can't be read."""
    wb = _load_workbook(data)
    if wb is None:
        return []
    try:
        return _images_from_wb(wb)
    finally:
        try:
            wb.close()
        except Exception:                                      # noqa: BLE001
            pass


def xlsx_to_llm_input(data: bytes) -> tuple[str, list[bytes]]:
    """One workbook load -> (markdown, [image_bytes]). The caller sends the images
    to a vision model alongside the text when the list is non-empty; otherwise it
    uses the text-only path. Never raises."""
    wb = _load_workbook(data)
    if wb is None:
        return "", []
    try:
        return _markdown_from_wb(wb), _images_from_wb(wb)
    finally:
        try:
            wb.close()
        except Exception:                                      # noqa: BLE001
            pass