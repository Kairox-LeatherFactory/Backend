"""
================================================================================
scripts/docgen/errors.py — collect the REAL error messages a service can return
================================================================================
OpenAPI publishes 422 and the success shape and nothing else, because a
deliberate 409 raised deep in a service is invisible to it. Those are exactly
the errors the floor hits — "this garment's parts have not merged yet", "this
lot has no stock left" — and the frontend has to show a message for each one.

So this walks the service's own Python with `ast` and pulls out every
`HTTPException(...)`: the status code and, where it is a literal, the message.
An f-string message is rendered with its `{placeholders}` intact, which is
honest — the server really does interpolate a value there.

It is a source scan, not a proof: an error raised by a helper in another module
will be listed under that module. It is still far better than the nothing
OpenAPI gives you.
================================================================================
"""
from __future__ import annotations

import ast
import pathlib

_STATUS_NAMES = {
    "HTTP_400_BAD_REQUEST": 400, "HTTP_401_UNAUTHORIZED": 401,
    "HTTP_403_FORBIDDEN": 403, "HTTP_404_NOT_FOUND": 404,
    "HTTP_405_METHOD_NOT_ALLOWED": 405, "HTTP_409_CONFLICT": 409,
    "HTTP_410_GONE": 410, "HTTP_413_REQUEST_ENTITY_TOO_LARGE": 413,
    "HTTP_415_UNSUPPORTED_MEDIA_TYPE": 415,
    "HTTP_422_UNPROCESSABLE_ENTITY": 422, "HTTP_423_LOCKED": 423,
    "HTTP_429_TOO_MANY_REQUESTS": 429,
    "HTTP_500_INTERNAL_SERVER_ERROR": 500,
    "HTTP_501_NOT_IMPLEMENTED": 501, "HTTP_502_BAD_GATEWAY": 502,
    "HTTP_503_SERVICE_UNAVAILABLE": 503,
}


def _status(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    if isinstance(node, ast.Attribute):
        return _STATUS_NAMES.get(node.attr)
    if isinstance(node, ast.Name):
        return _STATUS_NAMES.get(node.id)
    return None


def _text(node: ast.AST) -> str:
    """Render a message node back to something readable."""
    if isinstance(node, ast.Constant):
        return str(node.value)
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                try:
                    out.append("{" + ast.unparse(part.value) + "}")
                except Exception:                        # pragma: no cover
                    out.append("{value}")
        return "".join(out)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _text(node.left) + _text(node.right)
    if isinstance(node, ast.Call):
        try:
            return ast.unparse(node)
        except Exception:                                # pragma: no cover
            return ""
    try:
        return ast.unparse(node)
    except Exception:                                    # pragma: no cover
        return ""


def scan(paths: list[pathlib.Path]) -> list[tuple[int, str, str]]:
    """Return sorted (status, message, file) for every HTTPException raised."""
    found: dict[tuple[int, str], str] = {}
    for path in paths:
        if path.is_dir():
            files = sorted(p for p in path.rglob("*.py")
                           if "__pycache__" not in str(p))
        else:
            files = [path]
        for file in files:
            try:
                tree = ast.parse(file.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):    # pragma: no cover
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = getattr(node.func, "id", None) or \
                    getattr(node.func, "attr", None)
                if name != "HTTPException":
                    continue
                status = detail = None
                if node.args:
                    status = _status(node.args[0])
                if len(node.args) > 1:
                    detail = _text(node.args[1])
                for kw in node.keywords:
                    if kw.arg == "status_code":
                        status = _status(kw.value)
                    elif kw.arg == "detail":
                        detail = _text(kw.value)
                if status is None:
                    continue
                message = " ".join((detail or "").split())
                if len(message) > 400:
                    message = message[:397] + "…"
                key = (status, message)
                found.setdefault(key, file.name)
    return sorted(((status, message, file)
                   for (status, message), file in found.items()),
                  key=lambda row: (row[0], row[1]))
