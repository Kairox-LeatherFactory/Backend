"""
================================================================================
scripts/docgen/introspect.py — read the TRUTH out of the running app
================================================================================
Everything the API-reference documents say about a route is taken from here, not
typed by hand, because a hand-typed contract is a second copy that drifts.

Two sources:

  1. `app.openapi()` — paths, methods, summaries, descriptions (the route
     docstrings), parameters, request bodies and response schemas.

  2. The live `app.routes` objects — the ROLE GATE, which OpenAPI does not
     publish. `require_roles(*allowed)` returns a closure named `checker`; the
     allowed tuple is sitting in its `__closure__`, so the real allow-list can be
     read back off the dependency graph instead of guessed from a docstring.

Nothing in this module needs a database.
================================================================================
"""
from __future__ import annotations

import json
from typing import Any

_METHODS = ("get", "post", "put", "patch", "delete")

# Roles that pass EVERY require_roles gate (app/modules/users/deps.py).
SUPERUSER_ROLES = ("managing_director", "direct_manager")


# ─────────────────────────────────────────────────────────────────────────────
# 1. The app
# ─────────────────────────────────────────────────────────────────────────────
def load_app():
    """Import the FastAPI app with throw-away settings (no DB, no real key)."""
    import os
    import pathlib
    import sys

    root = pathlib.Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    os.environ.setdefault(
        "SECRET_KEY", "docs-generation-only-key-not-used-to-sign-anything-0123")
    from app.main import app  # noqa: E402

    return app


def route_roles(app) -> dict[tuple[str, str], dict]:
    """Map (METHOD, path) -> what the auth layer actually enforces.

    Returns for each route:
        auth            bool   a Bearer token is required at all
        blocked_legacy  bool   the router is wrapped in block_employees
        gates           list of {"kind": "any"|"exact", "roles": [...]}
    """
    from fastapi.routing import APIRoute

    out: dict[tuple[str, str], dict] = {}
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        info = {"auth": False, "blocked_legacy": False, "gates": []}
        seen: set[tuple] = set()

        def walk(dep):
            for sub in dep.dependencies:
                call = sub.call
                name = getattr(call, "__name__", "")
                qual = getattr(call, "__qualname__", "")
                if "require_roles.<locals>" in qual or \
                        "require_exact_roles.<locals>" in qual:
                    kind = "exact" if "require_exact_roles" in qual else "any"
                    roles: list[str] = []
                    for cell in (call.__closure__ or ()):
                        try:
                            value = cell.cell_contents
                        except ValueError:          # pragma: no cover
                            continue
                        if isinstance(value, (tuple, list, frozenset, set)):
                            roles = sorted(
                                str(getattr(x, "value", x)) for x in value)
                    key = (kind, tuple(roles))
                    if key not in seen:
                        seen.add(key)
                        info["gates"].append({"kind": kind, "roles": roles})
                    info["auth"] = True
                elif name == "block_employees":
                    info["blocked_legacy"] = True
                    info["auth"] = True
                elif name == "get_current_user":
                    info["auth"] = True
                elif name == "client_scope":
                    info["auth"] = True
                walk(sub)

        walk(route.dependant)
        for method in route.methods:
            if method.lower() in _METHODS:
                out[(method.upper(), route.path)] = info
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 2. Schema walking
# ─────────────────────────────────────────────────────────────────────────────
def resolve(schema: dict, spec: dict, depth: int = 0) -> dict:
    """Follow $ref / allOf / anyOf down to something with real properties.

    Depth-capped because a few schemas here are recursive (a notification points
    at a parent notification) and an uncapped walk never terminates.
    """
    if depth > 8 or not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return resolve(spec.get("components", {}).get("schemas", {}).get(name, {}),
                       spec, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        branches = schema.get(key)
        if branches:
            merged = {k: v for k, v in schema.items() if k != key}
            for branch in branches:
                got = resolve(branch, spec, depth + 1)
                if got and got.get("type") != "null":
                    merged.update({k: v for k, v in got.items()
                                   if k not in merged or k in ("type", "properties",
                                                               "items", "enum")})
                    return merged
            return merged
    return schema


def _nullable(schema: dict) -> bool:
    for key in ("anyOf", "oneOf"):
        for branch in schema.get(key, []) or []:
            if isinstance(branch, dict) and branch.get("type") == "null":
                return True
    return False


def type_name(schema: dict, spec: dict, depth: int = 0) -> str:
    """A short, human type label: `string`, `integer`, `array of object`, …"""
    raw = schema if isinstance(schema, dict) else {}
    null_ok = _nullable(raw)
    s = resolve(raw, spec, depth)
    label = ""
    if s.get("enum"):
        values = [str(v) for v in s["enum"] if v is not None]
        label = "enum: " + " | ".join(values[:12])
        if len(values) > 12:
            label += " | …"
    else:
        t = s.get("type")
        fmt = s.get("format")
        if t == "array":
            inner = type_name(s.get("items", {}), spec, depth + 1)
            label = f"array of {inner}"
        elif t == "object" or "properties" in s:
            label = "object"
        elif t == "string" and fmt:
            label = f"string ({fmt})"
        elif t:
            label = str(t)
        elif "additionalProperties" in s:
            label = "object (free-form)"
        else:
            label = "any"
    if null_ok and not label.startswith("enum"):
        label += " or null"
    elif null_ok:
        label += " or null"
    return label


def flatten_fields(schema: dict, spec: dict, prefix: str = "",
                   depth: int = 0, seen: tuple = (),
                   show_defaults: bool = True) -> list[dict]:
    """Turn a schema into flat table rows: field path, type, required, meaning.

    `rows[].piece_code` style paths keep a nested response readable in a Word
    table without four levels of indentation.
    """
    if depth > 4:
        return []
    ref = schema.get("$ref") if isinstance(schema, dict) else None
    if ref and ref in seen:
        return []
    if ref:
        seen = seen + (ref,)
    s = resolve(schema, spec, 0)
    if not s:
        return []

    if s.get("type") == "array" or "items" in s:
        items = s.get("items", {})
        inner = resolve(items, spec, 0)
        if inner.get("properties"):
            return flatten_fields(items, spec,
                                  prefix + "[]." if prefix else "[].",
                                  depth, seen, show_defaults)
        return []

    props = s.get("properties") or {}
    if not props:
        return []
    required = set(s.get("required") or [])
    rows: list[dict] = []
    for name, sub in props.items():
        path = f"{prefix}{name}"
        sub_resolved = resolve(sub, spec, 0)
        row = {
            "field": path,
            "type": type_name(sub, spec),
            "required": "yes" if name in required else "no",
            "description": (sub.get("description")
                            or sub_resolved.get("description") or "").strip(),
        }
        if show_defaults and "default" in sub and sub["default"] is not None:
            row["description"] = (
                f"Default `{sub['default']}`. " + row["description"]).strip()
        rows.append(row)

        if sub_resolved.get("properties"):
            rows.extend(flatten_fields(sub, spec, path + ".", depth + 1, seen,
                                       show_defaults))
        elif sub_resolved.get("type") == "array":
            inner = resolve(sub_resolved.get("items", {}), spec, 0)
            if inner.get("properties"):
                rows.extend(flatten_fields(sub_resolved.get("items", {}), spec,
                                           path + "[].", depth + 1, seen,
                                           show_defaults))
    return rows


_DEMO = {
    "uuid": "3f8c1b2a-0000-4a11-9f00-000000000001",
    "date": "2026-09-23",
    "date-time": "2026-09-23T09:15:00Z",
}


def example(schema: dict, spec: dict, depth: int = 0, name: str = "") -> Any:
    """A plausible, editable JSON value for one schema node."""
    s = resolve(schema, spec, depth)
    if not s or depth > 6:
        return None
    if "example" in s:
        return s["example"]
    if s.get("examples"):
        return s["examples"][0]
    if "default" in s and s["default"] is not None:
        return s["default"]
    if s.get("enum"):
        return s["enum"][0]

    t = s.get("type")
    if t == "object" or "properties" in s:
        props = s.get("properties") or {}
        if not props:
            return {}
        return {k: example(v, spec, depth + 1, k) for k, v in props.items()}
    if t == "array":
        return [example(s.get("items", {}), spec, depth + 1, name)]
    fmt = s.get("format")
    if fmt in _DEMO:
        return _DEMO[fmt]
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    if t == "null":
        return None
    lowered = name.lower()
    if "barcode" in lowered or lowered.endswith("_code") or lowered == "code":
        return "STRING-CODE"
    if "name" in lowered:
        return "string"
    return "string"


def pretty(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Operations
# ─────────────────────────────────────────────────────────────────────────────
def operations(spec: dict) -> list[dict]:
    """Flatten the spec into one record per (path, method), in path order."""
    ops = []
    for path in sorted(spec["paths"]):
        entry = spec["paths"][path]
        for method in _METHODS:
            op = entry.get(method)
            if not op:
                continue
            ops.append({
                "path": path,
                "method": method.upper(),
                "tags": op.get("tags") or ["Other"],
                "summary": (op.get("summary") or "").strip(),
                "description": (op.get("description") or "").strip(),
                "op": op,
            })
    return ops
