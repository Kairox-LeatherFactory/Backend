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
# service -> (filename stem, OpenAPI tags, one line saying what it is for)
#
# The names are the FACTORY's words, not the router's: somebody looking for the
# store's requests should not have to know the tag is lowercase "store" while
# Wages is capitalised. Auth is folded into `user` because you cannot use any
# other collection until you have logged in.
# ─────────────────────────────────────────────────────────────────────────────
SERVICES: list[tuple[str, list[str], str]] = [
    ("analytics", ["Analytics"],
     "Factory overview, the order/style explorer, stage spread, freight-risk "
     "alerts and one garment's whole life story. READ-ONLY — analytics owns no "
     "tables and never writes."),
    ("dashboard", ["Dashboard"],
     "The per-role manager screens: cutting, lining, stitching, store and the "
     "Direct Manager's own. Every one is a read."),
    ("production", ["Production"],
     "POST /production/log is the whole floor's logging surface — the caller "
     "sends an ACTOR and TARGETS and never a stage. Also the two correction "
     "endpoints for a record that named the wrong worker."),
    ("employee", ["Employees"],
     "The roster and one worker. Shop-floor workers get NO login — creating one "
     "needs only a name, a designation and a wage type, and the response carries "
     "the barcode so the card can be printed immediately."),
    ("attendance", ["Attendance"],
     "Two doors — the card scan at the gate and the manual fallback — plus the "
     "corrections. PATCH with a new employee_id RE-ALLOCATES the day and moves "
     "that day's production events with it."),
    ("import", ["Imports"],
     "Breakdown upload: preview, commit, edit, then RELEASE. Release is the "
     "mint — it creates the pieces and their barcodes and cannot be undone."),
    ("material", ["Materials", "MaterialSuppliers", "Style material spec"],
     "Lots, stock, receiving (with per-hide leather sheets), the supplier "
     "directory, and the style's material recipe."),
    ("barcode", ["Barcode"],
     "GET /barcode/resolve is every scan's front door: unknown code 404, retired "
     "card 410 Gone, otherwise the code's type and a live payload for it."),
    ("jobwork", ["job work"],
     "Garments sent to an outside factory. A dispatched piece cannot be scanned "
     "in-house; the return logs the stage against the VENDOR, so it advances the "
     "garment without generating a wage."),
    ("user", ["Authentication", "Users"],
     "Login and the logins themselves. START HERE: run login, paste "
     "access_token into the collection's `token` variable, and every other "
     "collection is authorised."),
    ("cutting", ["cutting"],
     "The grid that replaced the cutting manager's spreadsheet. One row is ONE "
     "garment; hides are allocated to it, edited freely while DRAFT, then frozen "
     "by an audited approval."),
    ("client", ["Clients"],
     "Buyers, their orders, and the styles under them."),
    ("store", ["store"],
     "Scan employee + piece — two scans, not three. Send releases a batch into "
     "line-stitching, which is the ONLY gate LINE_STITCHING has."),
    ("wage", ["Wages"],
     "Rates, runs and the ledger. A CLOSED run is a frozen snapshot and is never "
     "recomputed. Visible to HR, DM and MD only."),
    ("inspection", ["inspections"],
     "Reject and rework, recorded against the stage RESPONSIBLE for the defect "
     "rather than the stage it was found at. The DM approves the re-walk."),
]

# Endpoints that belong to a service's SCREEN but carry another module's tag.
# `PATCH /employees/{id}/barcode` is tagged Barcode because the barcode module
# owns the code; the person who needs it is on the employee screen, reissuing a
# card somebody lost. It appears in both collections rather than neither.
EXTRAS: dict[str, set[str]] = {
    "employee": {"/api/v1/employees/{employee_id}/barcode"},
    "store": {"/api/v1/materials/issues"},
    "cutting": {"/api/v1/materials/lots", "/api/v1/materials/receive"},
}

# `drawer` is not a tag, because the module is withdrawn. It still gets a file —
# see _drawer_collection() for why.
DRAWER_HISTORY_PATHS = {
    "/api/v1/dashboard/store/drawers/{drawer_id}",
    "/api/v1/dashboard/store/drawers/{drawer_id}/movement",
}


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
    "  1. Set `base_url` (default http://127.0.0.1:8000).\n"
    "  2. Import `user.postman_collection.json` and run Login.\n"
    "  3. Paste `access_token` from that response into this collection's\n"
    "     `token` variable. Every request here inherits bearer auth.\n\n"
    "Optional query params are present but DISABLED, so each request runs\n"
    "as-is. Generated from the app's own OpenAPI schema by\n"
    "`python scripts/make_postman_per_service.py` — re-run it after changing\n"
    "routes rather than editing requests by hand."
)


def _drawer_collection(spec: dict) -> dict:
    """The drawer module is WITHDRAWN, and this file says so.

    Leaving the service out entirely would be the worse choice: somebody with an
    old collection would keep firing `/api/v1/drawers/*` at a 404 and reasonably
    conclude the server was broken. A collection that opens with the reason, and
    carries the two reads that DO still exist, answers the question instead.
    """
    coll = _shell(
        "KairoX · Drawer (WITHDRAWN)",
        "THE DRAWER MODULE IS GONE. `/api/v1/drawers/*` is unrouted — not "
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
        "The two requests below are the only drawer-shaped endpoints left. They "
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
    print(f"\nwrote {len(list(OUT_DIR.glob('*.json')))} collections, "
          f"{total} requests total, into {OUT_DIR.relative_to(ROOT)}/")
