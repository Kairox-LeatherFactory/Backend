"""
Generate ONE Postman collection per service into `postman/`.

WHY ONE FILE PER SERVICE AND NOT ONE BIG ONE
    `make_postman_collection.py` still writes the single combined collection, and
    that one is right for "import everything and go". This script is for the other
    way people actually work: one developer owns the store screens, another owns
    wages, and neither wants to scroll past 160 requests to find theirs. A file
    per service is also what a diff can show — when the cutting contract changes,
    exactly one file moves.

WHY GENERATED AND NOT HAND-WRITTEN
    The app publishes its own contract at /openapi.json. A hand-maintained
    collection is a second copy that drifts the moment a route changes, and a
    stale collection is WORSE than none: it fails in ways that look like server
    bugs, so the frontend developer files a ticket against the backend for a
    request the backend never had.

    Re-run this after changing routes. It needs no database and takes a second.

USAGE
    python scripts/make_postman_per_service.py
    -> writes postman/<service>.postman_collection.json, one per service
    Postman -> Import -> Folder -> postman/
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

ROOT = pathlib.Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "postman"

# Reuse the request-building that already works rather than writing it twice.
from scripts.make_postman_collection import _example, _request  # noqa: E402

_METHODS = ("get", "post", "put", "patch", "delete")

# ─────────────────────────────────────────────────────────────────────────────
# THE SERVICE REGISTRY LIVES IN ONE PLACE, AND IT IS NOT THIS FILE.
#
# `scripts/docgen/services.py` holds which OpenAPI tags belong to which service,
# because two generators read it: this one (Postman collections) and
# `scripts/make_service_docs.py` (the Word system guides + API references). With
# a copy in each, adding a tag to one and forgetting the other is invisible —
# the routes simply are not there for anyone downstream.
#
# `check_coverage()` turns a tag that belongs to no service into a loud failure
# rather than a silently missing collection.
# ─────────────────────────────────────────────────────────────────────────────
from scripts.docgen.services import (   # noqa: E402
    DRAWER_HISTORY_PATHS, EXTRAS, SERVICES, check_coverage,
)


def _shell(name: str, description: str) -> dict:
    return {
        "info": {
            "name": name,
            "description": description,
            "schema": ("https://schema.getpostman.com/json/collection/"
                       "v2.1.0/collection.json"),
        },
        "auth": {"type": "bearer",
                 "bearer": [{"key": "token", "value": "{{token}}",
                             "type": "string"}]},
        "variable": [
            {"key": "base_url", "value": "http://127.0.0.1:8000",
             "type": "string"},
            {"key": "token", "value": "", "type": "string"},
        ],
        "item": [],
    }


_SETUP = (
    "\n\nSETUP\n"
    "  1. Import the whole folder and SELECT the KairoX environment\n"
    "     (top-right). That is what shares one token across every\n"
    "     collection — a collection variable is visible only to the\n"
    "     collection that set it.\n"
    "  2. Run `user` → Login. Its test script stores `access_token` for\n"
    "     you; there is nothing to copy and paste.\n"
    "  3. Every request here inherits bearer auth from that token.\n\n"
    "`base_url` defaults to http://127.0.0.1:8000.\n\n"
    "Optional query params are present but DISABLED, so each request runs\n"
    "as-is. File uploads are real form-data rows — click Select Files on\n"
    "the `file` row. Generated from the app's own OpenAPI schema by\n"
    "`python scripts/make_postman_per_service.py` — re-run it after changing\n"
    "routes rather than editing requests by hand."
)


def _environment() -> dict:
    """One environment, shared by every collection in the folder.

    WHY THIS EXISTS. Login's test script can only write a collection variable
    into the collection that ran it, so logging in under `user` left the other
    seventeen collections on an empty token and every request in them 401'd —
    which reads as a broken auth guard rather than a missing paste. Selecting
    this environment gives them one shared `token` to write into, so one login
    covers the whole folder.
    """
    return {
        "name": "KairoX · local",
        "values": [
            {"key": "base_url", "value": "http://127.0.0.1:8000",
             "type": "default", "enabled": True},
            {"key": "token", "value": "", "type": "secret", "enabled": True},
        ],
        "_postman_variable_scope": "environment",
    }


def _drawer_collection(spec: dict) -> dict:
    """The drawer module is WITHDRAWN, and this file says so.

    Leaving the service out entirely would be the worse choice: somebody with an
    old collection would keep firing `/api/v1/drawers/*` at a 404 and reasonably
    conclude the server was broken. A collection that opens with the reason, and
    carries the two reads that DO still exist, answers the question instead.
    """
    coll = _shell(
        "KairoX · Drawer (WITHDRAWN)",
        "THE DRAWER MODULE IS DELETED. `/api/v1/drawers/*` is gone — not "
        "renamed, not deprecated.\n\n"
        "WHY. There were 200 physical drawers. A style releases 100+ garments, "
        "the run stalls mid-chain, and the next 50 have nowhere to go; "
        "re-allocating by hand was too complicated for the DM to actually do. "
        "The drawer was a bottleneck that earned nothing.\n\n"
        "WHAT REPLACED IT. The store is now a STATE ON THE GARMENT, so it has "
        "no capacity and nothing runs out. Use "
        "`store.postman_collection.json`:\n"
        "    POST /drawers/store-scan   ->  POST /store/scan   (one scan fewer)\n"
        "    POST /drawers/send         ->  POST /store/send\n"
        "    GET  /drawers/by-code/{c}  ->  GET  /store/pieces/{piece_code}\n"
        "    GET  /drawers              ->  GET  /store/pieces\n"
        "    GET  /drawers/pool         ->  (nothing — there is no pool)\n\n"
        "The two requests below replace the drawer detail/movement screens. "
        "They are keyed by the GARMENT, which is what an operator can scan. "
        "read FROZEN history for audit; they are not live state." + _SETUP)
    for path in sorted(DRAWER_HISTORY_PATHS):
        ops = spec["paths"].get(path, {})
        for method, op in ops.items():
            if method in _METHODS:
                coll["item"].append(_request(path, method, op, spec))
    return coll


def build() -> list[tuple[pathlib.Path, dict, int]]:
    from app.main import app

    spec = app.openapi()

    # A tag that belongs to no service gets no collection AND no documentation,
    # silently. Fail loudly instead — see docgen/services.check_coverage.
    orphans = check_coverage(spec)
    if orphans:
        raise SystemExit(
            "ERROR: these OpenAPI tags belong to no service, so they would get "
            "no collection:\n  - " + "\n  - ".join(orphans) +
            "\nAdd them to scripts/docgen/services.py.")

    by_tag: dict[str, list] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method not in _METHODS:
                continue
            for tag in (op.get("tags") or ["Other"]):
                by_tag.setdefault(tag, []).append(_request(path, method, op, spec))

    by_path: dict[str, list] = {}
    for path, ops in spec["paths"].items():
        for method, op in ops.items():
            if method in _METHODS:
                by_path.setdefault(path, []).append(_request(path, method, op, spec))

    written = []
    for stem, tags, blurb in SERVICES:
        items = []
        for tag in tags:
            items.extend(by_tag.get(tag, []))
        extra = []
        for path in sorted(EXTRAS.get(stem, ())):
            extra.extend(by_path.get(path, []))
        items.extend(extra)
        if not items:
            print(f"  !! {stem}: no routes for tags {tags} — skipped")
            continue
        coll = _shell(f"KairoX · {stem.capitalize()}", blurb + _SETUP)
        # Grouped by tag when a service spans several, flat when it is one —
        # a folder containing everything is just a wrapper nobody opens.
        if len(tags) > 1:
            coll["item"] = [
                {"name": tag,
                 "item": sorted(by_tag.get(tag, []), key=lambda i: i["name"])}
                for tag in tags if by_tag.get(tag)
            ]
            if extra:
                coll["item"].append(
                    {"name": "Also used on this screen",
                     "item": sorted(extra, key=lambda i: i["name"])})
        else:
            coll["item"] = sorted(items, key=lambda i: i["name"])
        written.append((OUT_DIR / f"{stem}.postman_collection.json", coll,
                        len(items)))

    drawer = _drawer_collection(spec)
    written.append((OUT_DIR / "drawer.postman_collection.json", drawer,
                    len(drawer["item"])))
    return written


if __name__ == "__main__":
    OUT_DIR.mkdir(exist_ok=True)
    total = 0
    for path, coll, n in build():
        path.write_text(json.dumps(coll, indent=2), encoding="utf-8")
        total += n
        print(f"  {path.name:48s} {n:3d} requests")
    env_path = OUT_DIR / "KairoX.postman_environment.json"
    env_path.write_text(json.dumps(_environment(), indent=2), encoding="utf-8")
    print(f"  {env_path.name:48s}     environment")
    n_coll = len(list(OUT_DIR.glob('*.postman_collection.json')))
    print(f"\nwrote {n_coll} collections + 1 environment, "
          f"{total} requests total, into {OUT_DIR.relative_to(ROOT)}/")
