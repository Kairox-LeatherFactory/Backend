"""Export the OpenAPI spec to a file your frontend can mock against (Prism/MSW).

Run: python -m scripts.export_openapi  ->  <repo root>/openapi.json
Then your frontend dev runs:  prism mock openapi.json   (a full fake API, instantly)

THIS IS THE SAME FILE `scripts/make_postman_collection.py` WRITES, byte for byte
(both dump `app.openapi()` with indent=2, and json.dump escapes non-ASCII, so the
two agree). Use this one when all you want is the contract; use that one when you
also want the Postman collection rebuilt from it. Neither needs a database.

THREE THINGS THIS USED TO GET WRONG, all of which are why it now looks like this:

  · It ran at IMPORT time. Every statement was module-level, so merely importing
    `scripts.export_openapi` — which any audit or `--help` sweep across the
    folder does — silently overwrote openapi.json. Work belongs under __main__.

  · It wrote to the CURRENT DIRECTORY. Run from anywhere but the repo root it
    dropped openapi.json wherever you happened to be standing, and the real one
    stayed stale while the run reported success. It now writes to the repo root
    explicitly, and prints the absolute path it wrote.

  · Its placeholder SECRET_KEY was "export-only" — 11 characters. config.py
    enforces a 32-character minimum with NO environment exempt (F32/H5), so on
    any machine that did not already supply a key this script died on import
    with a RuntimeError about the key rather than exporting anything. The
    ephemeral key below is a real random one; nothing here signs a token with
    it, it exists only so Settings can be constructed.
"""
from __future__ import annotations

import json
import os
import pathlib
import secrets
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Constructing Settings requires a usable key and a database URL even though this
# script touches neither. setdefault, so a real environment always wins.
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", secrets.token_urlsafe(48))
sys.path.insert(0, str(ROOT))


def main() -> int:
    from app.main import app

    spec = app.openapi()
    out = ROOT / "openapi.json"
    out.write_text(json.dumps(spec, indent=2), encoding="utf-8")

    methods = ("get", "post", "put", "patch", "delete")
    n_ops = sum(1 for p in spec["paths"].values() for m in p if m in methods)
    print(f"Wrote {out}")
    print(f"  {len(spec['paths'])} paths, {n_ops} operations")
    for path in sorted(spec["paths"]):
        verbs = ",".join(m.upper() for m in spec["paths"][path] if m in methods)
        print(f"  {verbs:22s} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
