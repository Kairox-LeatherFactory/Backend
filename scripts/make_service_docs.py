"""
================================================================================
scripts/make_service_docs.py — TWO Word documents per service, into docs/service
================================================================================
    docs/service/<STEM>_API_REFERENCE.docx   every endpoint, exact request and
                                             response bodies, generated from the
                                             app's own OpenAPI schema
    docs/service/<STEM>_SYSTEM_GUIDE.docx    how that service actually works,
                                             written by hand in
                                             docs/service/_src/<stem>_system_guide.md

WHY THE REFERENCE IS GENERATED AND THE GUIDE IS NOT
    A request body is a CONTRACT. Typed by hand it is a second copy that drifts
    the day somebody adds a field, and a wrong contract is worse than none: the
    frontend developer builds against it, gets a 422, and files a bug on a route
    that was never wrong. So every path, field, type, default and example in the
    reference is read out of the running app.

    A FLOW is not in the schema. Why the store is a state on the garment and not
    a place, why a closed wage run is never recomputed, which gate fails first —
    no generator can know that. Those are written down, in `_src/`, and this
    script renders them to Word next to the reference.

    The ROLE GATE sits in between: OpenAPI does not publish it, but the app knows
    it, so it is read off the live dependency graph (docgen/introspect.py).

USAGE
    python scripts/make_service_docs.py            # everything
    python scripts/make_service_docs.py store wage # just these services
    python scripts/make_service_docs.py --api      # references only
    python scripts/make_service_docs.py --guide    # guides only

    No database needed. Takes a few seconds. Re-run it after changing routes.
================================================================================
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "docs" / "service"
SRC_DIR = OUT_DIR / "_src"

from scripts.docgen import apiref, errors, introspect as I   # noqa: E402
from scripts.docgen.services import (EXTRAS, SERVICES, TITLES,  # noqa: E402
                                     check_coverage)
from scripts.docgen.word import ACCENT, SUB, DocBuilder, render_markdown  # noqa: E402

BASE_URL = "http://127.0.0.1:8000"
TODAY = dt.date.today().isoformat()

# Which Python the service is: used for the "errors this service can return"
# table, which OpenAPI cannot give us.
SOURCES: dict[str, list[str]] = {
    "user": ["app/modules/users"],
    "client": ["app/modules/clients"],
    "import": ["app/modules/imports"],
    "barcode": ["app/modules/barcode"],
    "employee": ["app/modules/employees"],
    "attendance": ["app/modules/attendance"],
    "material": ["app/modules/materials"],
    "cutting": ["app/modules/cutting"],
    "production": ["app/modules/production/router.py",
                   "app/modules/production/service.py",
                   "app/modules/production/corrections.py",
                   "app/modules/production/repository.py"],
    "store": ["app/modules/store"],
    "inspection": ["app/modules/production/inspection.py",
                   "app/modules/production/inspection_router.py"],
    "jobwork": ["app/modules/jobwork"],
    "wage": ["app/modules/wages"],
    "dashboard": ["app/modules/dashboard"],
    "analytics": ["app/modules/analytics"],
    "procurement": ["app/modules/procurement", "app/modules/bom",
                    "app/modules/inventory", "app/modules/supplier_po"],
    "system": ["app/modules/intelligence", "app/main.py"],
}


# ─────────────────────────────────────────────────────────────────────────────
def service_operations(stem: str, tags: list[str], ops: list[dict]) -> list[dict]:
    """The operations of one service: its tags, plus its borrowed endpoints."""
    wanted = set(tags)
    extra_paths = EXTRAS.get(stem, set())
    picked = [op for op in ops
              if wanted.intersection(op["tags"]) or op["path"] in extra_paths]
    order = {tag: index for index, tag in enumerate(tags)}
    return sorted(picked, key=lambda op: (order.get(op["tags"][0], 99),
                                          op["path"], op["method"]))


def _intro(doc: DocBuilder, stem: str) -> None:
    doc.h1("1. How to read this document", page_break=False)
    doc.para("This document lists **every endpoint** of this service. For each "
             "one you get: what it does, who is allowed to call it, every field "
             "you send, every field you get back, an example you can copy, and "
             "the errors it can return.")
    doc.para("It is **generated from the running application**, so it matches "
             "the server exactly. If the server changes, this file is rebuilt "
             "with `python scripts/make_service_docs.py`. Never edit it by hand.")

    doc.h2("The base address")
    doc.code(f"{BASE_URL}        local machine\n"
             "https://<your-staging-host>   staging\n\n"
             "Every path below already includes its prefix, for example\n"
             "  POST /api/v1/store/scan  ->  " + BASE_URL + "/api/v1/store/scan")

    doc.h2("Logging in (do this first)")
    doc.para("All routes except `/health`, `/ready` and `/` need a token. Get "
             "one from the login endpoint, then send it on every request.")
    doc.code('POST /api/v1/auth/login\n'
             'Content-Type: application/json\n'
             '\n'
             '{ "username": "9000000001", "password": "YourPassword" }\n'
             '\n'
             '-> 200\n'
             '{\n'
             '  "access_token": "eyJhbGciOiJIUzI1NiIs...",\n'
             '  "token_type": "bearer",\n'
             '  "role": "store_manager",\n'
             '  "name": "Ravi Kumar",\n'
             '  "user_id": "3f8c1b2a-0000-4a11-9f00-000000000001",\n'
             '  "must_change_password": false\n'
             '}\n'
             '\n'
             'Then on every other call:\n'
             '    Authorization: Bearer eyJhbGciOiJIUzI1NiIs...')
    doc.callout("`username` is the user's PHONE NUMBER, not an email. The token "
                "is valid for 24 hours. If `must_change_password` is `true`, "
                "send the user to the change-password screen before anything "
                "else.")

    doc.h2("Roles")
    doc.para("Each endpoint says which roles may call it. Two rules apply "
             "everywhere:")
    doc.bullet("**Managing Director** and **Direct Manager** are superusers. "
               "They pass every normal role gate even when they are not listed.")
    doc.bullet("A few gates are **separation-of-duties** gates (for example BOM "
               "approval). There the Direct Manager is refused on purpose. Those "
               "endpoints say so.")
    doc.para("A wrong role returns **403**, never an empty list. If a screen "
             "shows nothing, check the status code before you blame the data.")

    doc.h2("How errors come back")
    doc.para("Every failure — 400, 403, 404, 409, 410, 422, 500 — has the same "
             "shape:")
    doc.code('{\n'
             '  "detail": "Piece has not been released from the store yet",\n'
             '  "request_id": "0f2c7f0e-2b7a-4f1b-9a5a-9f0b3f0c7e11"\n'
             '}')
    doc.para("`detail` is the message you can show to the user. `request_id` is "
             "for the backend team — put it in the bug report and the exact "
             "request can be found in the server log.")
    doc.para("A **422** is different: `detail` is a **list**, one entry per bad "
             "field, so a form can mark the exact box that is wrong.")
    doc.code('{\n'
             '  "detail": [\n'
             '    {"type": "missing", "loc": ["body", "employee_id"],\n'
             '     "msg": "Field required"}\n'
             '  ],\n'
             '  "request_id": "..."\n'
             '}')

    doc.h2("Things that are true on every endpoint")
    doc.table(
        ["Topic", "Rule"],
        [["Ids", "All ids are **UUID** strings, for example "
                 "`3f8c1b2a-0000-4a11-9f00-000000000001`. The app generates "
                 "them, never the database."],
         ["Barcodes", "Where an endpoint accepts both an id and a barcode, send "
                      "**one of them**, not both. The router turns the barcode "
                      "into an id before any work starts."],
         ["Dates", "`work_date` and similar are plain dates: `2026-09-23`. "
                   "Timestamps are UTC ISO-8601: `2026-09-23T09:15:00Z`."],
         ["Numbers", "Quantities and money come back as JSON numbers. Leather is "
                     "in **dcm**, lining in **mtrs**, ribs in **kg**, everything "
                     "counted in **pcs**."],
         ["Paging", "List endpoints take `limit` and `offset` and return "
                    "`total`. Ask for the next page with "
                    "`offset = offset + limit`."],
         ["Empty result", "A list with no matches returns `200` and an empty "
                          "array — not a 404."]],
        widths=[1.2, 5.9])


def build_api_reference(stem: str, tags: list[str], blurb: str,
                        spec: dict, ops: list[dict], gates: dict) -> pathlib.Path:
    picked = service_operations(stem, tags, ops)
    title = TITLES.get(stem, stem.capitalize())

    doc = DocBuilder()
    doc.footer(f"KairoX ERP · {title} · API Reference · generated {TODAY}")
    doc.cover(
        "KairoX ERP — Leather Garment Factory", title, "API Reference",
        " ".join(blurb.split()),
        [("Service", stem),
         ("Endpoints in this document", str(len(picked))),
         ("OpenAPI tags", ", ".join(tags)),
         ("Base URL (local)", BASE_URL),
         ("Generated", TODAY),
         ("Generated by", "python scripts/make_service_docs.py"),
         ("Audience", "Frontend and backend developers")])

    _intro(doc, stem)

    # ── index ────────────────────────────────────────────────────────────────
    doc.h1("2. Every endpoint at a glance")
    doc.para("Find your endpoint here, then read its full section in part 3.")
    rows = []
    for entry in picked:
        gate = gates.get((entry["method"], entry["path"]), {})
        if not gate.get("auth"):
            who = "open (no token)"
        elif gate.get("gates"):
            who = apiref.role_words(gate["gates"][0]["roles"])
        else:
            who = "any logged-in user"
        rows.append([entry["method"], f"`{entry['path']}`",
                     apiref.one_liner(entry, 90), who])
    doc.table(["Method", "Path", "What it does", "Who can call it"], rows,
              widths=[0.65, 2.15, 2.5, 1.8], size=8.5)

    # ── detail ───────────────────────────────────────────────────────────────
    doc.h1("3. Endpoint reference")
    current_tag = None
    for entry in picked:
        if len(tags) > 1 and entry["tags"][0] != current_tag:
            current_tag = entry["tags"][0]
            doc.h2(current_tag)
        gate = gates.get((entry["method"], entry["path"]), {})
        apiref.endpoint_section(doc, entry, spec, gate, BASE_URL)

    # ── errors ───────────────────────────────────────────────────────────────
    found = errors.scan([ROOT / p for p in SOURCES.get(stem, [])])
    if found:
        doc.h1("4. Every error message this service can return")
        doc.para("Read from the service's own code. Use it to write the message "
                 "your screen shows the user. Text inside `{ }` is filled in by "
                 "the server with a real value.")
        doc.table(["Status", "Message the API returns", "Raised in"],
                  [[str(status), message or "(no message)", f"`{file}`"]
                   for status, message, file in found],
                  widths=[0.7, 5.0, 1.4], size=8.5)

    path = OUT_DIR / f"{stem.upper()}_API_REFERENCE.docx"
    doc.save(path)
    return path


def build_system_guide(stem: str, tags: list[str], blurb: str,
                       ops: list[dict], gates: dict) -> pathlib.Path | None:
    source = SRC_DIR / f"{stem}_system_guide.md"
    if not source.exists():
        print(f"  !! {stem}: no {source.relative_to(ROOT)} — guide skipped")
        return None

    picked = service_operations(stem, tags, ops)
    title = TITLES.get(stem, stem.capitalize())

    doc = DocBuilder()
    doc.footer(f"KairoX ERP · {title} · System Guide · updated {TODAY}")
    doc.cover(
        "KairoX ERP — Leather Garment Factory", title, "System Guide",
        " ".join(blurb.split()),
        [("Service", stem),
         ("What this document is", "How the service works, step by step"),
         ("Its companion", f"{stem.upper()}_API_REFERENCE.docx"),
         ("Endpoints in this service", str(len(picked))),
         ("Updated", TODAY),
         ("Audience", "Frontend and backend developers")])

    render_markdown(doc, source.read_text(encoding="utf-8"),
                    first_h1_breaks=True)

    # Appendix: the live endpoint list, so the guide can never fall behind the
    # app on WHICH routes exist (only on what they mean).
    doc.h1("Appendix — every endpoint in this service")
    doc.para("Generated from the running app on " + TODAY + ". Full request and "
             "response bodies are in the API Reference document.")
    rows = []
    for entry in picked:
        gate = gates.get((entry["method"], entry["path"]), {})
        if not gate.get("auth"):
            who = "open"
        elif gate.get("gates"):
            who = apiref.role_words(gate["gates"][0]["roles"])
        else:
            who = "any logged-in user"
        rows.append([entry["method"], f"`{entry['path']}`",
                     apiref.one_liner(entry, 90), who])
    doc.table(["Method", "Path", "What it does", "Who can call it"], rows,
              widths=[0.65, 2.15, 2.5, 1.8], size=8.5)

    path = OUT_DIR / f"{stem.upper()}_SYSTEM_GUIDE.docx"
    doc.save(path)
    return path


def _report_undocumented(ops: list[dict]) -> None:
    """Name every route that has neither a response_model nor a written shape.

    Those are the holes in the reference, so they are printed on every run
    rather than left to be discovered by a frontend developer.
    """
    from scripts.docgen.responses import RESPONSES

    holes = []
    for entry in ops:
        responses = entry["op"].get("responses") or {}
        declared = any(
            ((responses[code].get("content") or {}).get("application/json")
             or {}).get("schema")
            for code in responses if code.startswith("2"))
        if declared:
            continue
        key = f"{entry['method']} {entry['path']}"
        if key not in RESPONSES and "204" not in responses:
            holes.append(key)
    if holes:
        print(f"  note: {len(holes)} route(s) have no response_model and no "
              f"written shape in docgen/responses.py:")
        for key in holes:
            print(f"         {key}")
        print()


# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    want_api = "--guide" not in flags
    want_guide = "--api" not in flags

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    SRC_DIR.mkdir(parents=True, exist_ok=True)

    app = I.load_app()
    spec = app.openapi()
    (ROOT / "openapi.json").write_text(json.dumps(spec, indent=2),
                                       encoding="utf-8")
    orphans = check_coverage(spec)
    if orphans:
        print("ERROR: these OpenAPI tags belong to no service, so they would be")
        print("       documented nowhere. Add them to scripts/docgen/services.py:")
        for tag in orphans:
            print(f"         - {tag}")
        return 1

    ops = I.operations(spec)
    gates = I.route_roles(app)
    _report_undocumented(ops)

    total = 0
    for stem, tags, blurb in SERVICES:
        if args and stem not in args:
            continue
        if want_api:
            path = build_api_reference(stem, tags, blurb, spec, ops, gates)
            count = len(service_operations(stem, tags, ops))
            print(f"  {path.name:42s} {count:3d} endpoints")
            total += 1
        if want_guide:
            path = build_system_guide(stem, tags, blurb, ops, gates)
            if path:
                print(f"  {path.name:42s}     system guide")
                total += 1

    print(f"\nwrote {total} Word documents into "
          f"{OUT_DIR.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
