"""
================================================================================
tests/test_extraction_coerce.py — regression: _coerce + order-envelope shape
================================================================================

Guards the CRIT fix for "order extraction fails on scanned & consolidated sheets".
The failure was that Gemini, told to "emit one line per row", returns a TOP-LEVEL
JSON ARRAY (or wraps the rows under a non-`lines` key). The old `_coerce` sliced
from the first `{` to the last `}`, mangling `[{...},{...}]` into invalid JSON and
dropping every line -> `is_empty_order` -> native_pdf_extraction_failed.

No API key / no network — `_coerce` is pure, and the order path is driven with a
monkeypatched `_llm_invoke` so the pipeline shape is exercised deterministically.
================================================================================
"""
import json

from app.modules.procurement.classifier import _coerce
from app.modules.bom import extraction
from app.modules.bom.extraction import llm_extract_order


def test_coerce_top_level_array_wraps_as_lines():
    raw = '[{"model": "SP74003", "sizes": {"M": 5, "L": 3}}]'
    out = _coerce(raw)
    assert out == {"lines": [{"model": "SP74003", "sizes": {"M": 5, "L": 3}}]}


def test_coerce_array_inside_json_fence():
    raw = '```json\n[{"model": "X", "sizes": {"S": 2}}]\n```'
    out = _coerce(raw)
    assert out == {"lines": [{"model": "X", "sizes": {"S": 2}}]}


def test_coerce_plain_object_unchanged():
    raw = '{"order_number": "PO-1", "lines": [{"model": "A", "sizes": {"M": 1}}]}'
    assert _coerce(raw) == json.loads(raw)


def test_coerce_prose_wrapped_object_still_sliced():
    raw = 'Here is the result:\n{"lines": [{"model": "A", "sizes": {"M": 1}}]}\nDone.'
    assert _coerce(raw) == {"lines": [{"model": "A", "sizes": {"M": 1}}]}


def test_coerce_garbage_returns_none():
    assert _coerce("not json at all") is None
    assert _coerce("") is None


def test_order_from_top_level_array_recovers_quantities(monkeypatch):
    """End-to-end shape: a bare-array model response must yield a non-empty order
    with recomputed totals (the exact path that used to return 0 lines)."""
    raw_array = '[{"model": "SP74003", "color": "BLACK", "sizes": {"M": 5, "L": 3}}]'
    monkeypatch.setattr(extraction, "_llm_invoke",
                        lambda kind, payload, prompt, **kw: ("gemini", raw_array))
    order = llm_extract_order("text", "irrelevant-markdown")
    assert order is not None
    assert order.order_qty == 8
    assert order.per_size_qty == {"M": 5, "L": 3}
    assert order.lines[0].model == "SP74003"
