"""
Generate a Postman collection (v2.1) from the live FastAPI OpenAPI schema.

WHY GENERATE IT INSTEAD OF HAND-MAINTAINING ONE
    The app already publishes its own contract at /openapi.json. A hand-written
    collection is a second copy of that contract which drifts the moment a route
    changes — and a stale collection is worse than none, because it fails in ways
    that look like server bugs. Re-running this script is how the collection stays
    true; it takes a second and needs no database.

WHAT YOU GET
    · one folder per tag (Wages, Dashboard, Barcode, …)
    · every operation, with its real path, query params and an example JSON body
      built from the endpoint's own request schema
    · {{base_url}} and {{token}} collection variables, bearer auth inherited by
      every request, so you log in once and everything is authorised
    · path parameters as {{style_code}}-style variables you fill in per request

USAGE
    python scripts/make_postman_collection.py
    -> writes postman_collection.json in the repo root
    Postman → Import → File → postman_collection.json
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT = ROOT / "postman_collection.json"

_METHODS = ("get", "post", "put", "patch", "delete")


def _resolve(schema: dict, spec: dict, depth: int = 0) -> dict:
    """Follow a $ref one level at a time. Depth-capped: some schemas here are
    recursive (a notification points at a parent notification), and an
    uncapped walk would not terminate."""
    if depth > 6 or not isinstance(schema, dict):
        return {}
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _resolve(spec.get("components", {}).get("schemas", {}).get(name, {}),
                        spec, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema and schema[key]:
            # first non-null branch — enough for an editable example
            for branch in schema[key]:
                got = _resolve(branch, spec, depth + 1)
                if got:
                    return got
    return schema


def _example(schema: dict, spec: dict, depth: int = 0):
    """A plausible, editable value for one schema node."""
    s = _resolve(schema, spec, depth)
    if not s or depth > 6:
        return None
    if "example" in s:
        return s["example"]
    if "default" in s:
        return s["default"]
    if "enum" in s and s["enum"]:
        return s["enum"][0]

    t = s.get("type")
    if t == "object" or "properties" in s:
        return {k: _example(v, spec, depth + 1)
                for k, v in (s.get("properties") or {}).items()}
    if t == "array":
        return [_example(s.get("items", {}), spec, depth + 1)]
    fmt = s.get("format")
    if fmt == "date":
        return "2026-08-01"
    if fmt == "date-time":
        return "2026-08-01T09:00:00Z"
    if fmt == "uuid":
        return "00000000-0000-0000-0000-000000000000"
    if t == "integer":
        return 0
    if t == "number":
        return 0
    if t == "boolean":
        return False
    return ""


def _request(path: str, method: str, op: dict, spec: dict) -> dict:
    # Path params become {{var}} so Postman surfaces them as editable variables.
    raw_path = path
    query, variables = [], []
    for p in op.get("parameters", []) or []:
        name = p.get("name")
        where = p.get("in")
        example = _example(p.get("schema", {}), spec)
        if where == "path":
            raw_path = raw_path.replace("{%s}" % name, "{{%s}}" % name)
            variables.append({"key": name, "value": "" if example is None else str(example)})
        elif where == "query":
            query.append({
                "key": name,
                "value": "" if example is None else str(example),
                "description": p.get("description", ""),
                # Optional params start disabled so a request runs as-is.
                "disabled": not p.get("required", False),
            })

    item = {
        "name": op.get("summary") or f"{method.upper()} {path}",
        "request": {
            "method": method.upper(),
            "header": [],
            "url": {
                "raw": "{{base_url}}" + raw_path,
                "host": ["{{base_url}}"],
                "path": [seg for seg in raw_path.strip("/").split("/") if seg],
            },
            "description": (op.get("description") or "").strip(),
        },
        "response": [],
    }
    if query:
        item["request"]["url"]["query"] = query
    if variables:
        item["request"]["url"]["variable"] = variables

    body_schema = (((op.get("requestBody") or {}).get("content") or {})
                   .get("application/json", {}).get("schema"))
    if body_schema:
        item["request"]["header"].append(
            {"key": "Content-Type", "value": "application/json"})
        item["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(_example(body_schema, spec), indent=2),
            "options": {"raw": {"language": "json"}},
        }
    return item


def build() -> dict:
    from app.main import app

    spec = app.openapi()
    (ROOT / "openapi.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")

    folders: dict[str, list] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method not in _METHODS:
                continue
            tag = (op.get("tags") or ["Other"])[0]
            folders.setdefault(tag, []).append(_request(path, method, op, spec))

    info = spec.get("info", {})
    return {
        "info": {
            "name": f"{info.get('title', 'API')} v{info.get('version', '1')}",
            "description": (
                "Generated from the app's own OpenAPI schema by\n"
                "`python scripts/make_postman_collection.py`. Re-run it after\n"
                "changing routes rather than editing requests by hand.\n\n"
                "SETUP\n"
                "  1. Set the `base_url` variable (default http://127.0.0.1:8000).\n"
                "  2. Run Authentication → login. Copy `access_token` from the\n"
                "     response into the `token` collection variable.\n"
                "  3. Every other request inherits bearer auth from the collection.\n\n"
                "Optional params are added but DISABLED so each request runs as-is."
            ),
            "schema": ("https://schema.getpostman.com/json/collection/"
                       "v2.1.0/collection.json"),
        },
        "auth": {"type": "bearer",
                 "bearer": [{"key": "token", "value": "{{token}}", "type": "string"}]},
        "variable": [
            {"key": "base_url", "value": "http://127.0.0.1:8000", "type": "string"},
            {"key": "token", "value": "", "type": "string"},
        ],
        "item": [
            {"name": tag, "item": sorted(items, key=lambda i: i["name"])}
            for tag, items in sorted(folders.items())
        ],
    }


if __name__ == "__main__":
    collection = build()
    OUT.write_text(json.dumps(collection, indent=2), encoding="utf-8")
    n = sum(len(f["item"]) for f in collection["item"])
    print(f"wrote {OUT.name}: {len(collection['item'])} folders, {n} requests")
    print(f"wrote openapi.json")
