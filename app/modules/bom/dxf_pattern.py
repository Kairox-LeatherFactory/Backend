# -----------------------------------------------------------------------------
# LLM CLIENT INITIALIZATION
#
# WHY CONFIGURE THE TIMEOUT FROM SETTINGS?
#
# Previous implementation:
#
#     timeout=100
#
# used a hard-coded timeout for every Gemini request. This ignored the project's
# central configuration (settings.llm_request_timeout), so changing the timeout
# in config.py or via environment variables had no effect.
#
# The timeout is now read from:
#
#     settings.llm_request_timeout
#
# Benefits:
#
# • Single source of truth for all LLM request timeouts.
# • Production, staging, and local environments can use different values
#   without modifying code.
# • Large PDF/spec-sheet extraction requests can increase the timeout through
#   configuration only.
# • Prevents configuration drift where different modules use different timeout
#   values.
#
# This is a behavioral fix only. The request logic, retries, model selection,
# and response handling remain unchanged.
# -----------------------------------------------------------------------------


from __future__ import annotations
import logging

from dataclasses import dataclass, field

import ezdxf
from shapely.geometry import Polygon

logger = logging.getLogger(__name__)

SF_PER_CM2 = 1.0 / 929.0304
PARSER_VERSION = "dxf_pattern/1.0"

# Plausible longest-dimension band for a single garment cut piece, in cm.
# Used to auto-detect the coordinate unit when no spec measurement is supplied.
_PIECE_MAX_CM_LO = 10.0
_PIECE_MAX_CM_HI = 200.0
# unit_name -> centimetres per drawing unit
_UNIT_TO_CM = {"mm": 0.1, "cm": 1.0, "in": 2.54, "m": 100.0}

# canonical size order (alpha labels don't sort lexically: S < M < L < XL)
_ALPHA_RANK = {"XXS": 0, "XS": 1, "S": 2, "M": 3, "L": 4,
               "XL": 5, "XXL": 6, "XXXL": 7, "3XL": 7, "4XL": 8}


def _size_rank(s: str):
    """Sortable rank for a size label; None if unrecognised (skips ordering check)."""
    s2 = (s or "").upper().strip()
    if s2 in _ALPHA_RANK:
        return _ALPHA_RANK[s2]
    digits = "".join(c for c in s2 if c.isdigit())
    return int(digits) if digits else None


@dataclass
class PatternPiece:
    block: str           # block name, e.g. "P0006_04"
    name: str            # decoded PIECE name (cp932-recovered)
    fabric: str          # decoded FABRIC field
    size: str | None     # SIZE field as labelled in the file (e.g. "L")
    qty: int             # cut quantity for this piece
    net_area_sf: float   # cut-contour area (one instance), in sf
    longest_cm: float    # longest bbox dimension, for unit cross-checks


@dataclass
class ParsedPattern:
    source_system: str               # "creacompo" | "lectra" | "gerber" | "unknown"
    style: str | None
    master_size: str | None
    unit: str                        # detected drawing unit: mm | cm | in | m
    pieces: list                     # list[PatternPiece]
    area_matrix: dict                # {size: {"n_pieces", "net_sf", "net_qty_sf"}}  (ALL pieces)
    fabric_matrix: dict              # {size: {fabric: net_qty_sf}}  (per-fabric, for role attribution)
    parser_version: str = PARSER_VERSION
    warnings: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.pieces) and "manual_entry_required" not in self.warnings

    @property
    def fabrics(self) -> list:
        """Distinct fabric strings present (role mapping is the service's job)."""
        return sorted({p.fabric for p in self.pieces if p.fabric})


# ---- text decoding ---------------------------------------------------------

def _recover(text: str) -> str:
    """Recover a Shift-JIS (cp932) string that ezdxf decoded as cp1252.

    Real-world R12 exports from JP CAD declare $DWGCODEPAGE=ANSI_1252 but write
    cp932 bytes. ezdxf reads cp1252 + surrogateescape, so the original bytes are
    recoverable. ASCII (QUANTITY/SIZE/FABRIC labels, sizes) round-trips untouched.
    """
    if text.isascii():
        return text
    try:
        return text.encode("cp1252", "surrogateescape").decode("cp932", "replace")
    except Exception:
        return text


def _field(texts: list, *labels: str) -> str | None:
    """First TEXT value of the form 'LABEL: value' for ANY of the accepted
    labels, matched case-insensitively. Dialects differ: CreaCompo emits
    PIECE:/FABRIC:/SIZE:/QUANTITY: (uppercase); Lectra/Gerber emit
    'Piece Name:'/'Material:'/'Size:'/'Quantity:'. Pass every alias."""
    pres = [l.lower() + ":" for l in labels]
    for t in texts:
        tl = t.lower()
        for pre in pres:
            if tl.startswith(pre):
                return _recover(t).split(":", 1)[1].strip()
    return None


# ---- geometry --------------------------------------------------------------

def _block_texts(block) -> list:
    return [t.dxf.text for t in block.query("TEXT")]


def _cut_area_units2(block) -> tuple[float, float]:
    """Largest closed contour area in this block (the cut contour), plus its
    longest bounding-box dimension. Both in raw drawing units (units²/units).
    Reads POLYLINE and LWPOLYLINE; HATCH is deliberately ignored (fill, not cut line)."""
    best_area = 0.0
    best_longest = 0.0
    for e in block.query("POLYLINE LWPOLYLINE"):
        pts = _poly_points(e)
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid or poly.area <= 0:
            continue
        if poly.area > best_area:
            best_area = poly.area
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            best_longest = max(max(xs) - min(xs), max(ys) - min(ys))
    return best_area, best_longest


def _detect_source(header_texts: list) -> str:
    blob = " ".join(header_texts).lower()
    if "creacompo" in blob or "toray" in blob:
        return "creacompo"
    if "lectra" in blob or "modaris" in blob:
        return "lectra"
    if "gerber" in blob or "accumark" in blob:
        return "gerber"
    return "unknown"

def _poly_points(e) -> list[tuple[float, float]]:
    """Vertices as (x, y), for either old-style POLYLINE (vertex sub-entities)
    or LWPOLYLINE (packed point array)."""
    if e.dxftype() == "LWPOLYLINE":
        return [(x, y) for x, y, *_ in e.get_points("xy")]
    return [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]


def _detect_unit(insunits, longest_dims_units: list, expected_longest_cm: float | None) -> tuple[str, list]:
    """Pick mm/cm/in/m. The plausibility band is primary; $INSUNITS and the spec
    outseam are tie-breakers only. The outseam is a *coarse* hint — a split-leg
    pattern has no single piece equal to the full outseam — so it never raises a
    mismatch on its own; only an out-of-band result warns."""
    warns: list = []
    if not longest_dims_units:
        return "mm", warns
    biggest = max(longest_dims_units)

    # units whose conversion lands the biggest piece inside a plausible piece size
    candidates = [u for u in _UNIT_TO_CM
                  if _PIECE_MAX_CM_LO <= biggest * _UNIT_TO_CM[u] <= _PIECE_MAX_CM_HI]
    if not candidates:
        warns.append(f"dxf_unit_undetected:biggest_piece={biggest:.0f}units")
        return "mm", warns
    if len(candidates) == 1:
        return candidates[0], warns

    # ambiguous band: prefer the spec-outseam-closest candidate, else the
    # $INSUNITS hint, else the smallest-unit candidate.
    if expected_longest_cm:
        return min(candidates, key=lambda u: abs(biggest * _UNIT_TO_CM[u] - expected_longest_cm)), warns
    insunit_name = {1: "in", 4: "mm", 5: "cm", 6: "m"}.get(insunits)
    if insunit_name in candidates:
        return insunit_name, warns
    return min(candidates, key=lambda u: _UNIT_TO_CM[u]), warns


# ---- public entry point ----------------------------------------------------

def parse_pattern(path: str, *, expected_outseam_cm: float | None = None) -> ParsedPattern:
    """Read a block-nested pattern DXF into a ParsedPattern. Never raises."""
    try:
        doc = ezdxf.readfile(path)
    except ezdxf.DXFStructureError:
        from ezdxf import recover
        doc, auditor = recover.readfile(path)          # tolerant read of malformed exports
        if auditor.has_errors:
            logger.warning("dxf recovered with %d errors: %s", len(auditor.errors), path)

    msp = doc.modelspace()
    header_texts = [t.dxf.text for t in msp.query("TEXT")]
    source = _detect_source(header_texts)
    style = _field(header_texts, "DESIGN")
    master = _field(header_texts, "MASTER SIZE")

    # one pass over placed pieces (INSERT -> block); scale-aware for safety
    raw_pieces: list = []
    longest_units: list = []
    for ins in msp.query("INSERT"):
        block = doc.blocks.get(ins.dxf.name)
        if block is None:
            continue
        area_u2, longest_u = _cut_area_units2(block)
        if area_u2 <= 0:
            continue
        scale = abs(ins.dxf.xscale * ins.dxf.yscale) or 1.0
        texts = _block_texts(block)
        qty_raw = _field(texts, "QUANTITY", "Quantity")
        try:
            qty = max(1, int("".join(ch for ch in (qty_raw or "1") if ch.isdigit())))
        except ValueError:
            qty = 1
        raw_pieces.append({
            "block": ins.dxf.name,
            "name": _field(texts, "PIECE", "Piece Name") or ins.dxf.name,
            "fabric": _field(texts, "FABRIC", "Material") or "",
            "size": _field(texts, "SIZE", "Size"),
            "qty": qty,
            "area_u2": area_u2 * scale,
            "longest_u": longest_u * (abs(ins.dxf.xscale) or 1.0),
        })
        longest_units.append(longest_u * (abs(ins.dxf.xscale) or 1.0))

    if not raw_pieces:
        return ParsedPattern(source, style, master, "mm", [], {}, {},
                             warnings=["dxf_no_pieces:no closed block polylines found",
                                       "manual_entry_required"])

    unit, warns = _detect_unit(doc.header.get("$INSUNITS"), longest_units, expected_outseam_cm)
    cm_per_unit = _UNIT_TO_CM[unit]

    pieces: list = []
    matrix: dict = {}
    fabric_matrix: dict = {}
    for rp in raw_pieces:
        net_sf = rp["area_u2"] * (cm_per_unit ** 2) * SF_PER_CM2
        longest_cm = rp["longest_u"] * cm_per_unit
        size = rp["size"] or "_unsized"
        pieces.append(PatternPiece(rp["block"], rp["name"], rp["fabric"], rp["size"],
                                   rp["qty"], round(net_sf, 3), round(longest_cm, 1)))
        m = matrix.setdefault(size, {"n_pieces": 0, "net_sf": 0.0, "net_qty_sf": 0.0})
        m["n_pieces"] += 1
        m["net_sf"] += net_sf
        m["net_qty_sf"] += net_sf * rp["qty"]
        fb = fabric_matrix.setdefault(size, {})
        fb[rp["fabric"]] = fb.get(rp["fabric"], 0.0) + net_sf * rp["qty"]

    for m in matrix.values():
        m["net_sf"] = round(m["net_sf"], 2)
        m["net_qty_sf"] = round(m["net_qty_sf"], 2)
    for fb in fabric_matrix.values():
        for k in fb:
            fb[k] = round(fb[k], 2)

    # grading sanity: net_qty_sf must not decrease as true size grows.
    ranked = sorted(((_size_rank(s), s) for s in matrix if s != "_unsized"),
                    key=lambda t: (t[0] is None, t[0]))
    ranked = [(r, s) for r, s in ranked if r is not None]
    vals = [matrix[s]["net_qty_sf"] for _, s in ranked]
    if len(vals) >= 2 and any(b < a for a, b in zip(vals, vals[1:])):
        warns.append("dxf_grading_nonmonotonic:check size labels / cut-vs-sew line")

    return ParsedPattern(source, style, master, unit, pieces, matrix, fabric_matrix, warnings=warns)


def _net_for(pattern: ParsedPattern, size, fabrics) -> tuple[str | None, float]:
    """Resolve the size (falling back to master) and sum net_qty_sf, optionally
    restricted to a set of fabric strings (the leather pieces)."""
    key = str(size)
    if key not in pattern.area_matrix:
        key = pattern.master_size if pattern.master_size in pattern.area_matrix else None
    if key is None:
        return None, 0.0
    if fabrics is None:
        return key, pattern.area_matrix[key]["net_qty_sf"]
    fb = pattern.fabric_matrix.get(key, {})
    return key, round(sum(v for f, v in fb.items() if f in fabrics), 2)


def dcm_from_pattern(pattern: ParsedPattern, size, yield_factor: float,
                     fabrics: set | None = None) -> dict | None:
    """net_qty_sf(size) × yield -> DCM. `fabrics` restricts the sum to the leather
    pieces (use the FABRIC strings from `pattern.fabrics`, mapped to roles by the
    service). Falls back to master size if `size` is absent. None -> resolver falls
    through to the next source."""
    if not pattern.ok:
        return None
    key, net_qty = _net_for(pattern, size, fabrics)
    if key is None:
        return None
    return {"size": key, "net_qty_sf": net_qty, "yield_factor": yield_factor,
            "dcm_sf": round(net_qty * yield_factor, 1),
            "n_pieces": pattern.area_matrix[key]["n_pieces"]}


def implied_yield(pattern: ParsedPattern, size, confirmed_dcm_sf: float,
                  fabrics: set | None = None) -> float | None:
    """confirmed_dcm / net_qty_sf — the per-factory cutting/hide-waste multiplier
    implied by a CONFIRMED order. This is the calibration hook: feed each confirmed
    DCM back through here to learn the yield for the DXF path (instead of guessing
    it). A value far outside ~1.4–2.2 signals a role/units/cut-line issue to review."""
    key, net_qty = _net_for(pattern, size, fabrics)
    if key is None or net_qty <= 0:
        return None
    return round(confirmed_dcm_sf / net_qty, 3)