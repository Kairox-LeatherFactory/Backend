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
    · file-upload endpoints as real form-data rows, the file field typed as a
      file so Postman shows the picker — an upload with no body 422s on a field
      the tester cannot see
    · {{base_url}} and {{token}} collection variables, bearer auth inherited by
      every request; Login CAPTURES its own token into that variable, so there
      is no copy-paste step to forget
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


# THE PAGING PARAMS, AND WHY THEY GET A SPECIAL CASE.
# Every other optional query param is a FILTER — sending an empty or guessed one
# changes what a request means, so those ship disabled. limit/offset do not:
# they are the endpoint's own defaults written out, so enabling them changes
# nothing about the result except that the pager is visibly in play. Shipping
# them disabled meant nobody ever exercised paging through the collections.
# A value here also replaces the schema's generic example (integer -> 0), because
# `limit=0` is a 422 on every route that declares it (PageParams caps at ge=1).
_PAGING_DEFAULTS = {"limit": 50, "offset": 0, "page": 1, "page_size": 50}


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
            example = _PAGING_DEFAULTS.get(name, example)
            query.append({
                "key": name,
                "value": "" if example is None else str(example),
                "description": p.get("description", ""),
                # Optional params start disabled so a request runs as-is —
                # EXCEPT the paging ones. A disabled param is not sent at all,
                # so every collection shipped `limit` and `offset` greyed out,
                # and anyone who opened a list request, saw the boxes, and
                # pressed Send got an unpaged response. It read as "pagination
                # is not working anywhere" when nothing had ever been asked for.
                # They are safe to send on every request that declares them —
                # they are the endpoint's own defaults.
                "disabled": (not p.get("required", False)
                             and name not in _PAGING_DEFAULTS),
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

    content = (op.get("requestBody") or {}).get("content") or {}
    body_schema = content.get("application/json", {}).get("schema")
    if body_schema:
        item["request"]["header"].append(
            {"key": "Content-Type", "value": "application/json"})
        item["request"]["body"] = {
            "mode": "raw",
            "raw": json.dumps(_example(body_schema, spec), indent=2),
            "options": {"raw": {"language": "json"}},
        }
    elif "multipart/form-data" in content:
        # An upload endpoint used to generate with NO body at all, so the
        # tester got a 422 that named a field they could not see. FastAPI
        # describes the form in a `Body_*` schema; turn it into real Postman
        # form-data rows — file rows as `type: file` so the picker appears.
        form = _resolve(content["multipart/form-data"].get("schema", {}), spec)
        rows = []
        for name, sub in (form.get("properties") or {}).items():
            resolved = _resolve(sub, spec)
            is_file = (resolved.get("format") == "binary"
                       or resolved.get("contentMediaType")
                       == "application/octet-stream")
            if is_file:
                rows.append({"key": name, "type": "file", "src": [],
                             "description": "Pick a file."})
            else:
                value = _example(sub, spec)
                rows.append({"key": name, "type": "text",
                             "value": "" if value is None else str(value),
                             "disabled": name not in (form.get("required") or [])})
        # Postman sets the multipart boundary itself; a hand-set Content-Type
        # header here would override it and break the upload.
        item["request"]["body"] = {"mode": "formdata", "formdata": rows}

    # Login is the one request whose RESULT every other request needs. Copying
    # the token by hand is the step people skip, and then every 401 looks like
    # a broken route. Capture it instead.
    if path.endswith("/auth/login") and method == "post":
        item["event"] = [{
            "listen": "test",
            "script": {"type": "text/javascript", "exec": [
                "// Stores access_token so you never paste it by hand.",
                "//",
                "// It goes into the ENVIRONMENT when one is selected, because a",
                "// collection variable is only visible to the collection that",
                "// set it -- log in once in `user` and the other 17 collections",
                "// would still 401. Select the KairoX environment and one login",
                "// authorises all of them. The collection variable is written",
                "// too, so this still works with no environment selected.",
                "if (pm.response.code === 200) {",
                "    const body = pm.response.json();",
                "    if (body.access_token) {",
                "        pm.collectionVariables.set('token', body.access_token);",
                "        if (pm.environment.name) {",
                "            pm.environment.set('token', body.access_token);",
                "        }",
                "        console.log('token stored'",
                "            + (pm.environment.name",
                "               ? ' in environment ' + pm.environment.name",
                "               : ' in this collection only -- select the KairoX'",
                "                 + ' environment to share it'));",
                "    }",
                "}",
                "pm.test('login returns 200 + access_token', function () {",
                "    pm.response.to.have.status(200);",
                "    pm.expect(pm.response.json()).to.have.property('access_token');",
                "});",
            ]},
        }]
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
