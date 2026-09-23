"""
================================================================================
scripts/docgen/apiref.py — builds one API-REFERENCE .docx for one service
================================================================================
Every fact on the page comes from `introspect.py` (the live app), so the
document cannot describe a route the server does not have.

The shape of one endpoint section, in the order a developer needs it:

    METHOD /path
    What it does            one line, then the route's own notes
    Who can call it         the REAL allow-list, read off the dependency graph
    Path parameters         name / type / required / meaning
    Query parameters        same, with defaults
    Request body            field table + a complete example you can paste
    Response                status, field table, a complete example
    Errors                  what can come back and what it means
    cURL                    a runnable line

Written for people whose first language is not English: short sentences, one
idea per row, and an example for everything.
================================================================================
"""
from __future__ import annotations

import re

from scripts.docgen import introspect as I
from scripts.docgen.responses import RESPONSES
from scripts.docgen.word import ACCENT, DocBuilder, SUB

_ROLE_WORDS = {
    "managing_director": "Managing Director",
    "direct_manager": "Direct Manager",
    "cutting_manager": "Cutting Manager",
    "lining_manager": "Lining Manager",
    "stitching_manager": "Stitching Manager",
    "store_manager": "Store Manager",
    "supervisor": "Supervisor",
    "security": "Security",
    "hr": "HR",
    "merchandiser": "Merchandiser",
    "client": "Client",
    "viewer": "Viewer",
    "employee": "Employee (legacy)",
}

_STATUS_WORDS = {
    "200": "OK — the call worked.",
    "201": "Created — a new row was written.",
    "204": "No content — it worked and there is nothing to send back.",
    "400": "Bad request — the values you sent do not make sense together.",
    "401": "Not logged in — the token is missing, wrong or expired.",
    "403": "Wrong role — this login is not allowed to do this.",
    "404": "Not found — no row with that id or code.",
    "409": "Conflict — the data is not in a state that allows this.",
    "410": "Gone — the code existed once and was retired.",
    "422": "Validation error — a field is missing or has the wrong type.",
    "500": "Server error — quote `request_id` from the body to support.",
}


def one_liner(entry: dict, limit: int = 110) -> str:
    """The first sentence of the route's own notes — better than `List Pieces`."""
    text = (entry.get("description") or "").strip()
    if text:
        first = text.split("\n\n")[0]
        first = " ".join(first.split())
        cut = first.find(". ")
        if 20 < cut < limit:
            first = first[:cut + 1]
        if len(first) > limit:
            first = first[:limit].rsplit(" ", 1)[0] + "…"
        return first
    return entry.get("summary") or "—"


def role_words(roles: list[str]) -> str:
    return ", ".join(_ROLE_WORDS.get(r, r) for r in roles)


# ─────────────────────────────────────────────────────────────────────────────
# Docstring → Word
# ─────────────────────────────────────────────────────────────────────────────
def _looks_preformatted(block: str) -> bool:
    """True for the aligned `field   explanation` blocks used in the routers."""
    lines = [ln for ln in block.split("\n") if ln.strip()]
    if len(lines) < 2:
        return False
    aligned = sum(1 for ln in lines if re.search(r"\S {2,}\S", ln))
    indented = sum(1 for ln in lines if ln.startswith("    "))
    return aligned >= 2 or indented >= 2


def write_description(doc: DocBuilder, text: str) -> None:
    if not text.strip():
        return
    for block in re.split(r"\n\s*\n", text.strip()):
        stripped = block.strip("\n")
        lines = [ln for ln in stripped.split("\n")]
        if all(re.match(r"^\s*[-*•]\s+", ln) for ln in lines if ln.strip()):
            for line in lines:
                if line.strip():
                    doc.bullet(re.sub(r"^\s*[-*•]\s+", "", line))
        elif _looks_preformatted(stripped):
            doc.code(stripped, size=8.5)
        else:
            doc.para(" ".join(ln.strip() for ln in lines if ln.strip()))


# ─────────────────────────────────────────────────────────────────────────────
# Endpoint section
# ─────────────────────────────────────────────────────────────────────────────
def _params(op: dict, spec: dict, where: str) -> list[list[str]]:
    rows = []
    for param in op.get("parameters") or []:
        if param.get("in") != where:
            continue
        schema = param.get("schema", {})
        default = schema.get("default")
        note = param.get("description", "") or ""
        if default is not None:
            note = f"Default `{default}`. {note}".strip()
        rows.append([
            f"`{param.get('name')}`",
            I.type_name(schema, spec),
            "yes" if param.get("required") else "no",
            note.strip(),
        ])
    return rows


def _curl(method: str, path: str, op: dict, spec: dict, base: str) -> str:
    url = base + path
    query = [p for p in (op.get("parameters") or [])
             if p.get("in") == "query" and p.get("required")]
    if query:
        url += "?" + "&".join(f"{p['name']}=VALUE" for p in query)
    parts = [f'curl -X {method} "{url}" \\',
             '  -H "Authorization: Bearer $TOKEN" \\']
    content = (op.get("requestBody") or {}).get("content") or {}
    if "application/json" in content:
        body = I.pretty(I.example(content["application/json"].get("schema", {}), spec))
        parts.append('  -H "Content-Type: application/json" \\')
        parts.append("  -d '" + body + "'")
    elif "multipart/form-data" in content:
        form = I.resolve(content["multipart/form-data"].get("schema", {}), spec)
        for name, sub in (form.get("properties") or {}).items():
            resolved = I.resolve(sub, spec)
            if (resolved.get("format") == "binary"
                    or resolved.get("contentMediaType")
                    == "application/octet-stream"):
                parts.append(f'  -F "{name}=@/path/to/file" \\')
            else:
                parts.append(f'  -F "{name}=VALUE" \\')
    elif "application/x-www-form-urlencoded" in content:
        form = I.resolve(content["application/x-www-form-urlencoded"].get("schema", {}),
                         spec)
        for name in (form.get("properties") or {}):
            parts.append(f'  -d "{name}=VALUE" \\')
    last = parts[-1].rstrip()
    parts[-1] = last[:-1].rstrip() if last.endswith("\\") else last
    return "\n".join(parts)


def _write_override(doc: DocBuilder, override: dict) -> None:
    """Render a hand-documented response for a route with no `response_model`.

    ~40% of the routes here return a plain dict the service builds, so FastAPI
    publishes no response schema for them and OpenAPI can say nothing about the
    body. Leaving that blank is not acceptable in a reference — the frontend
    still has to render the thing — so those shapes are written out by hand in
    `docgen/responses.py`, read from the service code, and marked as such.
    """
    if override.get("note"):
        doc.para(override["note"])
    if override.get("fields"):
        doc.table(["Field", "Type", "Meaning"],
                  [[f"`{f[0]}`", f[1], f[2]] for f in override["fields"]],
                  widths=[2.1, 1.5, 3.5])
    if "example" in override:
        doc.para("**Example response**")
        doc.code(I.pretty(override["example"]))
    doc.para("This route does not declare a response model in the code, so the "
             "shape above was read from the service and written by hand. It is "
             "the only part of this document that is not machine-generated.",
             size=9, color=SUB)


def endpoint_section(doc: DocBuilder, entry: dict, spec: dict, gate: dict,
                     base_url: str) -> None:
    method, path, op = entry["method"], entry["path"], entry["op"]

    doc.h3(f"{method}  {path}")
    if entry["summary"]:
        doc.para(entry["summary"], bold=True, color=ACCENT)

    doc.h4("What it does")
    override = RESPONSES.get(f"{method} {path}")
    if entry["description"]:
        write_description(doc, entry["description"])
    elif override and override.get("note"):
        # The route carries no docstring, but its response was written up by
        # hand — that note is the best description this endpoint has.
        doc.para(override["note"])
    else:
        doc.para("This route has no notes of its own. Its name and its fields "
                 "are the whole of what it does.", color=SUB)

    # ── who may call it ──────────────────────────────────────────────────────
    doc.h4("Who can call it")
    if not gate.get("auth"):
        doc.para("**No token needed.** This route is open.")
    else:
        gates = gate.get("gates") or []
        if not gates:
            doc.para("**Any logged-in user** with a valid token. There is no "
                     "role check on this route.")
        for item in gates:
            allowed = role_words(item["roles"])
            if item["kind"] == "exact":
                extra = ("" if "managing_director" in item["roles"]
                         else " The Managing Director also passes — they pass "
                              "every gate in this system.")
                doc.para(f"**Allowed roles:** {allowed}.{extra} **The Direct "
                         f"Manager is refused here**, even though they are "
                         f"normally a superuser: this is a separation-of-duties "
                         f"gate, and the person who prepares the document must "
                         f"not be the person who approves it.")
            else:
                doc.para(f"**Allowed roles:** {allowed}. Managing Director and "
                         f"Direct Manager always pass (they are superusers).")
        if gate.get("blocked_legacy"):
            doc.para("The legacy `employee` role is blocked on this router.",
                     color=SUB, size=9.5)

    # ── parameters ───────────────────────────────────────────────────────────
    rows = _params(op, spec, "path")
    if rows:
        doc.h4("Path parameters (part of the URL)")
        doc.table(["Name", "Type", "Required", "Meaning"], rows,
                  widths=[1.5, 1.4, 0.8, 3.4])

    rows = _params(op, spec, "query")
    if rows:
        doc.h4("Query parameters (after the `?`)")
        doc.table(["Name", "Type", "Required", "Meaning"], rows,
                  widths=[1.5, 1.4, 0.8, 3.4])

    rows = _params(op, spec, "header")
    if rows:
        doc.h4("Header parameters")
        doc.table(["Name", "Type", "Required", "Meaning"], rows,
                  widths=[1.5, 1.4, 0.8, 3.4])

    # ── request body ─────────────────────────────────────────────────────────
    body = op.get("requestBody") or {}
    content = body.get("content") or {}
    if content:
        doc.h4("Request body")
        media = next(iter(content))
        doc.para(f"Content-Type: `{media}`"
                 + ("  ·  **required**" if body.get("required") else
                    "  ·  optional"))
        schema = content[media].get("schema", {})
        if media == "multipart/form-data":
            form = I.resolve(schema, spec)
            required = set(form.get("required") or [])
            rows = []
            for name, sub in (form.get("properties") or {}).items():
                resolved = I.resolve(sub, spec)
                is_file = (resolved.get("format") == "binary"
                           or resolved.get("contentMediaType")
                           == "application/octet-stream")
                rows.append([
                    f"`{name}`",
                    "file (upload)" if is_file else I.type_name(sub, spec),
                    "yes" if name in required else "no",
                    (sub.get("description") or resolved.get("description")
                     or ("Pick the file to upload." if is_file else "")).strip(),
                ])
            doc.table(["Form field", "Type", "Required", "Meaning"], rows,
                      widths=[1.5, 1.4, 0.8, 3.4])
        else:
            rows = [[f"`{r['field']}`", r["type"], r["required"], r["description"]]
                    for r in I.flatten_fields(schema, spec)]
            if rows:
                doc.table(["Field", "Type", "Required", "Meaning"], rows,
                          widths=[1.9, 1.35, 0.75, 3.1])
            doc.para("**Example body**")
            doc.code(I.pretty(I.example(schema, spec)))

    # ── responses ────────────────────────────────────────────────────────────
    doc.h4("Response")
    responses = op.get("responses") or {}
    override = RESPONSES.get(f"{method} {path}")
    success = [code for code in responses if code.startswith("2")]
    for code in sorted(success):
        detail = responses[code]
        schema = ((detail.get("content") or {}).get("application/json") or {}
                  ).get("schema")
        doc.para(f"**{code}** — {_STATUS_WORDS.get(code, detail.get('description', ''))}")
        if schema:
            rows = [[f"`{r['field']}`", r["type"], r["description"]]
                    for r in I.flatten_fields(schema, spec,
                                              show_defaults=False)]
            if rows:
                doc.table(["Field", "Type", "Meaning"], rows,
                          widths=[2.1, 1.5, 3.5])
            doc.para("**Example response**")
            doc.code(I.pretty(I.example(schema, spec)))
        elif override:
            _write_override(doc, override)
        elif code == "204":
            doc.para("No body at all — an empty `204` means it worked.",
                     color=SUB)
        else:
            doc.para("This route does not declare a response model, and its "
                     "shape has not been written up yet. Call it once and read "
                     "the body — then add it to `scripts/docgen/responses.py` "
                     "so the next person does not have to.", color=SUB)

    others = sorted(code for code in responses if not code.startswith("2"))
    lines = []
    if gate.get("auth"):
        lines.append(["401", _STATUS_WORDS["401"]])
        if gate.get("gates"):
            lines.append(["403", _STATUS_WORDS["403"]])
    for code in others:
        text = responses[code].get("description") or ""
        lines.append([code, _STATUS_WORDS.get(code, text) if not text or
                      text.lower() in ("validation error", "successful response")
                      else text])
    seen = set()
    unique = []
    for code, text in lines:
        if code in seen:
            continue
        seen.add(code)
        unique.append([code, text])
    if unique:
        doc.para("**Errors**")
        doc.table(["Status", "What it means"], unique, widths=[0.9, 6.2])
        doc.para("Every error body is `{\"detail\": \"...\", \"request_id\": "
                 "\"...\"}`. Quote the `request_id` when you report a problem.",
                 size=9.5, color=SUB)

    doc.h4("Try it with cURL")
    doc.code(_curl(method, path, op, spec, base_url))
    doc.rule()
