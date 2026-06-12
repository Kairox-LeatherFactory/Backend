"""
================================================================================
modules/procurement/sniffing.py — Content sniff + feature extraction (§3a, §5d)
================================================================================

This is the SYNC, CPU-bound work the router pushes into run_in_threadpool so the
async event loop is never blocked: determine the TRUE MIME (by content, never the
client header), enforce the allowlist, and extract the cheap structural features
the heuristic validator scores on.

WHAT IT EXTRACTS (DocFeatures)
    mime          sniffed MIME (libmagic if importable, else a signature sniffer)
    ext           normalized extension implied by the sniffed type
    size_bytes    len(data)
    page_count    PDF page count (else None)
    has_text_layer  PDF has extractable text (a scanned/handwritten PDF does NOT —
                    Beau Geste & Jackie real files both return False → escalate)
    text_blob     the searchable text: PDF text, or XLSX/CSV header+cell dump
    n_rows/n_cols spreadsheet shape (a wide numeric grid ⇒ MEASUREMENT_GRID; a
                  tall narrow key→value sheet ⇒ NARRATIVE_TECHPACK)
    numeric_ratio fraction of non-empty cells that are numeric (grid signal)

MIME ALLOWLIST (workflow doc: "PDF, XLSX, and CSV")
    application/pdf · the XLSX mime · text/csv. A mismatch between the sniffed type
    and the filename extension is itself a reject (unsupported_mime).
================================================================================
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field

PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
CSV_MIME = "text/csv"

ALLOWED_MIMES = {PDF_MIME, XLSX_MIME, CSV_MIME}
_EXT_FOR_MIME = {PDF_MIME: ".pdf", XLSX_MIME: ".xlsx", CSV_MIME: ".csv"}
# Which sniffed MIME each filename extension is allowed to map to.
_MIME_FOR_EXT = {
    ".pdf": PDF_MIME, ".xlsx": XLSX_MIME, ".xlsm": XLSX_MIME, ".csv": CSV_MIME,
}


class UnsupportedMime(ValueError):
    """Sniffed type is not on the allowlist, or contradicts the extension."""


class EmptyOrCorrupt(ValueError):
    """Zero-byte or unparseable upload."""


@dataclass
class DocFeatures:
    mime: str
    ext: str
    size_bytes: int
    page_count: int | None = None
    has_text_layer: bool = False
    is_scanned_pdf: bool = False
    text_blob: str = ""
    sheet_names: list[str] = field(default_factory=list)
    n_rows: int = 0
    n_cols: int = 0
    numeric_ratio: float = 0.0
    # Narrative-document signals (a PROSE doc is neither an order sheet nor a
    # measurement grid — both are structured/tabular). These let the heuristic
    # reject a process narrative that merely *quotes* real client refs, e.g. the
    # BOM workflow PDF (acceptance §9.1) — fingerprints identify WHO, not WHAT.
    word_count: int = 0
    prose_ratio: float = 0.0          # fraction of words living in >12-word sentences
    heading_hits: list[str] = field(default_factory=list)  # "Stage 1", "Executive Summary", ...


# ── MIME sniffing (content, not the client header) ──────────────────────────
def _sniff_mime(data: bytes) -> str:
    """Best-effort true-type detection. Tries libmagic if present; otherwise uses
    robust file signatures. Returns "" for an unrecognised type."""
    # Optional libmagic enhancement (absent on stock Windows — never required).
    try:
        import magic  # type: ignore

        guessed = magic.from_buffer(data, mime=True)
        if guessed in ALLOWED_MIMES:
            return guessed
        # Office files often sniff as application/zip via magic; fall through to
        # the zip-content probe below rather than trusting that.
        if guessed and guessed != "application/zip":
            return guessed
    except Exception:
        pass

    if data[:5] == b"%PDF-" or data[:4] == b"%PDF":
        return PDF_MIME
    if data[:4] == b"PK\x03\x04":
        # A ZIP — is it an OOXML spreadsheet?
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                names = set(zf.namelist())
                if "xl/workbook.xml" in names or any(n.startswith("xl/") for n in names):
                    return XLSX_MIME
        except zipfile.BadZipFile:
            return ""
        return ""
    # CSV / plain text: must decode and look delimited (not arbitrary binary).
    try:
        text = data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return ""
    if text.strip() and ("," in text or ";" in text or "\n" in text):
        if all(ord(c) >= 9 for c in text):     # no control bytes ⇒ plausibly text
            return CSV_MIME
    return ""


def sniff_and_extract(data: bytes, filename: str) -> DocFeatures:
    """Determine the true MIME, enforce the allowlist + extension agreement, and
    extract structural features. Raises UnsupportedMime / EmptyOrCorrupt."""
    if not data:
        raise EmptyOrCorrupt("empty upload (zero bytes)")

    mime = _sniff_mime(data)
    if mime not in ALLOWED_MIMES:
        raise UnsupportedMime(
            f"sniffed content type {mime or 'unknown'!r} is not one of PDF / XLSX / CSV"
        )

    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    expected = _MIME_FOR_EXT.get(ext)
    if expected is not None and expected != mime:
        raise UnsupportedMime(
            f"file extension {ext!r} disagrees with sniffed content type {mime!r} "
            f"(content sniff wins — possible mislabeled or disguised file)"
        )

    feats = DocFeatures(mime=mime, ext=_EXT_FOR_MIME[mime], size_bytes=len(data))
    if mime == PDF_MIME:
        _extract_pdf(data, feats)
    elif mime == XLSX_MIME:
        _extract_xlsx(data, feats)
    else:
        _extract_csv(data, feats)
    return feats


_HEADING_RE = re.compile(
    r"\b(?:Stage|Section|Chapter|Part|Appendix|Phase)\s+\d+\b", re.IGNORECASE
)
_DOC_MARKERS = (
    "executive summary", "table of contents", "this document",
    "confidential", "version date classification", "document type",
)


def _compute_prose_signals(blob: str, feats: DocFeatures) -> None:
    """Measure 'narrative-ness': flowing long sentences + document headings. A real
    order/spec sheet is terse and tabular; a process narrative is prose with section
    structure. Used by the validator to reject a quoting narrative on the heuristic
    path (so client fingerprints can't mislabel it as that client's order sheet)."""
    segments = re.split(r"[.!?\n]+", blob)
    total = long_words = 0
    for seg in segments:
        words = seg.split()
        total += len(words)
        if len(words) > 12:
            long_words += len(words)
    feats.word_count = total
    feats.prose_ratio = (long_words / total) if total else 0.0
    hits = list(dict.fromkeys(m.group(0) for m in _HEADING_RE.finditer(blob)))
    low = blob.lower()
    hits += [m for m in _DOC_MARKERS if m in low]
    feats.heading_hits = hits[:8]


# ── PDF ─────────────────────────────────────────────────────────────────────
def _extract_pdf(data: bytes, feats: DocFeatures) -> None:
    try:
        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(data))
        feats.page_count = len(reader.pages)
        chunks: list[str] = []
        for page in reader.pages[:8]:            # first 8 pages is plenty to classify
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        blob = "\n".join(chunks).strip()
        feats.text_blob = blob
        # A real text layer is more than whitespace/a few stray glyphs. Below this
        # bar we treat it as scanned/handwritten (Beau Geste, Jackie) → escalate.
        feats.has_text_layer = len(blob) >= 40
        feats.is_scanned_pdf = not feats.has_text_layer
        if feats.has_text_layer:
            _compute_prose_signals(blob, feats)
    except Exception as exc:
        raise EmptyOrCorrupt(f"unreadable PDF: {exc}") from exc


# ── XLSX ────────────────────────────────────────────────────────────────────
def _extract_xlsx(data: bytes, feats: DocFeatures) -> None:
    try:
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    except Exception as exc:
        raise EmptyOrCorrupt(f"unreadable spreadsheet: {exc}") from exc

    feats.sheet_names = list(wb.sheetnames)
    ws = wb[wb.sheetnames[0]]
    feats.n_rows = ws.max_row or 0
    feats.n_cols = ws.max_column or 0

    cells: list[str] = list(feats.sheet_names)
    numeric = total = 0
    # Scan a bounded window — enough to fingerprint, cheap on a 67×268 grid.
    for r, row in enumerate(ws.iter_rows(values_only=True)):
        if r >= 60:
            break
        for c in row[:40]:
            if c is None or c == "":
                continue
            total += 1
            if isinstance(c, (int, float)):
                numeric += 1
            else:
                cells.append(str(c))
    feats.numeric_ratio = (numeric / total) if total else 0.0
    feats.text_blob = "\n".join(cells)
    wb.close()


# ── CSV ─────────────────────────────────────────────────────────────────────
def _extract_csv(data: bytes, feats: DocFeatures) -> None:
    try:
        text = data.decode("utf-8", "replace")
    except Exception as exc:
        raise EmptyOrCorrupt(f"unreadable CSV: {exc}") from exc
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise EmptyOrCorrupt("CSV has no rows")
    feats.n_rows = len(rows)
    feats.n_cols = max((len(r) for r in rows), default=0)
    numeric = total = 0
    cells: list[str] = []
    for row in rows[:60]:
        for c in row[:40]:
            c = (c or "").strip()
            if not c:
                continue
            total += 1
            try:
                float(c.replace(",", ""))
                numeric += 1
            except ValueError:
                cells.append(c)
    feats.numeric_ratio = (numeric / total) if total else 0.0
    feats.text_blob = "\n".join(cells)
