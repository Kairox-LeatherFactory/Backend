"""Export the OpenAPI spec to a file your frontend can mock against (Prism/MSW).

Run: python -m scripts.export_openapi  ->  openapi.json
Then your frontend dev runs:  prism mock openapi.json   (a full fake API, instantly)
"""
import os, sys, json
os.environ.setdefault("DATABASE_URL", "sqlite+pysqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "export-only")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import app

spec = app.openapi()
with open("openapi.json", "w") as f:
    json.dump(spec, f, indent=2)
print(f"Wrote openapi.json with {len(spec['paths'])} paths")
for p in sorted(spec["paths"]):
    methods = ",".join(m.upper() for m in spec["paths"][p])
    print(f"  {methods:12s} {p}")
