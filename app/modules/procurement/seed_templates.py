"""
================================================================================
modules/procurement/seed_templates.py — Seed the client_template registry (§4)
================================================================================

Idempotently loads config/client_templates.yaml into the `client_template` table
(the same replace-on-key pattern stage-0 §2 mandates). Onboarding a new client is
ONE YAML entry + re-running the seed — no code change, no redeploy (acceptance
§9.3). Used by scripts/seed.py and importable by tests.
================================================================================
"""
from __future__ import annotations

import os

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.modules.procurement.models import ClientTemplate

# config/client_templates.yaml lives at the backend root.
_DEFAULT_YAML = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))),
    "config", "client_templates.yaml",
)


def load_template_rows(path: str | None = None) -> list[dict]:
    path = path or _DEFAULT_YAML
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or []


def seed_client_templates(db: Session, path: str | None = None) -> int:
    """Upsert every YAML entry by (client_code, doc_kind). Returns the count seeded."""
    rows = load_template_rows(path)
    count = 0
    for r in rows:
        code, kind = r["client_code"], r["doc_kind"]
        existing = db.scalar(
            select(ClientTemplate).where(
                ClientTemplate.client_code == code,
                ClientTemplate.doc_kind == kind,
            )
        )
        fields = dict(
            display_name=r.get("display_name"),
            language=r.get("language"),
            size_system=r.get("size_system"),
            currency=r.get("currency"),
            expected_layout=r.get("expected_layout"),
            spec_type_hint=r.get("spec_type_hint"),
            accepted_mime=r.get("accepted_mime"),
            anchors=r.get("anchors") or [],
            fingerprints=r.get("fingerprints") or [],
            grid_signals=r.get("grid_signals") or {},
            thresholds=r.get("thresholds") or {},
            is_active=r.get("is_active", True),
        )
        if existing:
            for k, v in fields.items():
                setattr(existing, k, v)
        else:
            db.add(ClientTemplate(client_code=code, doc_kind=kind, **fields))
        count += 1
    db.commit()
    return count
