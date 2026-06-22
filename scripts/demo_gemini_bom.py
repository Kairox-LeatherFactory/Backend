"""
================================================================================
scripts/demo_gemini_bom.py — END-TO-END demo: upload → Gemini extract → BOM → PDF
================================================================================
A one-off driver that exercises the REAL Stage-1→2→3 services against in-memory
SQLite + local-FS storage, using a live Gemini key for the LLM/vision rungs:

  1. seed the Stage-2 registries (garment types, POM dict) + client templates + a
     Beau Geste client + a DM user;
  2. open a submission and UPLOAD both real sheets through the actual validation
     pipeline (scan→sniff→classify); a needs_manual_review verdict is force-accepted
     so the demo proceeds (logged as an override);
  3. generate the DRAFT BOM from the submission — the scanned ORDER PDF goes through
     Gemini VISION, the spec grid is parsed deterministically;
  4. confirm cutting → approve (materialises the order/style) → export the BOM PDF;
  5. copy the rendered PDF to var/demo-output/ and print the path.

The Gemini key is read from the GEMINI_API_KEY env var (set by the caller) — it is
never written to disk by this script.

Run:  GEMINI_API_KEY=... VIRUS_SCAN_ENABLED=false python -m scripts.demo_gemini_bom
================================================================================
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import uuid
from pathlib import Path

# ── env MUST be set before importing app.core.config (it caches settings) ─────
os.environ.setdefault("SECRET_KEY", "demo-secret")
os.environ.setdefault("VIRUS_SCAN_ENABLED", "false")     # no clamd in this demo
os.environ.setdefault("EMAIL_BACKEND", "noop")
os.environ.setdefault("NOTIFICATION_SWEEPER_ENABLED", "false")
os.environ.setdefault("PO_ESCALATION_SWEEPER_ENABLED", "false")
os.environ.setdefault("LOG_LEVEL", "INFO")
# Keep the Gemini free-tier per-minute / input-token budget in reach: the order
# extraction is the ONE vision call we care about, so send a single low-DPI page.
os.environ.setdefault("VISION_MAX_PAGES", "1")
os.environ.setdefault("OCR_DPI", "130")
# Let the Gemini client RETRY through a transient free-tier per-minute 429 (it honours
# the server's retry_delay) instead of giving up on the first hit and falling back.
os.environ.setdefault("LLM_MAX_RETRIES", "6")
os.environ.setdefault("LLM_REQUEST_TIMEOUT", "60")
# Seconds to pause after the upload phase so the per-minute Gemini window resets
# before the (token-heavy) order vision extraction. Override with GENERATE_DELAY.
GENERATE_DELAY = int(os.environ.get("GENERATE_DELAY", "20"))

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "var" / "demo-output"
STORAGE_DIR = OUT_DIR / "storage"
os.environ.setdefault("STORAGE_BACKEND", "local")
os.environ["LOCAL_STORAGE_DIR"] = str(STORAGE_DIR)

import yaml  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import app.core.database as dbmod  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.core.config import settings  # noqa: E402

# import every module's models so create_all builds the full schema
from app.core import models as _core_models  # noqa: E402,F401
from app.modules.users import models as _u  # noqa: E402,F401
from app.modules.clients import models as _c  # noqa: E402,F401
from app.modules.employees import models as _e  # noqa: E402,F401
from app.modules.production import models as _p  # noqa: E402,F401
from app.modules.wages import models as _w  # noqa: E402,F401
from app.modules.attendance import models as _a  # noqa: E402,F401
from app.modules.procurement import models as _pr  # noqa: E402,F401
from app.modules.bom import models as _bom  # noqa: E402,F401
from app.modules.inventory import models as _inv  # noqa: E402,F401
from app.modules.supplier_po import models as _spo  # noqa: E402,F401

from app.core.enums import UserRole  # noqa: E402
from app.modules.bom.models import GarmentType, PomDictionary  # noqa: E402
from app.modules.bom.seed_stage2 import _GARMENT_YAML, _POM_DICT_YAML  # noqa: E402
from app.modules.bom.service import BomService  # noqa: E402
from app.modules.clients.models import Client  # noqa: E402
from app.modules.procurement.models import ClientTemplate  # noqa: E402
from app.modules.procurement.seed_templates import load_template_rows  # noqa: E402
from app.modules.procurement.errors import UploadError  # noqa: E402
from app.modules.procurement.service import ProcurementService  # noqa: E402
from app.modules.users.models import User  # noqa: E402
from app.core.storage import get_storage, reset_storage_cache  # noqa: E402

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s")
log = logging.getLogger("demo")

ORDER_PDF = REPO / "data" / "Order-sheet-1.pdf"
SPEC_XLSX = REPO / "data" / "spec_sheet_1.xlsx"
PDF_MIME = "application/pdf"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def rule(title: str) -> None:
    print("\n" + "=" * 78 + f"\n  {title}\n" + "=" * 78)


async def seed(session) -> tuple[Client, User]:
    # garment types + POM dictionary (the Stage-2 reference registries)
    for r in yaml.safe_load(open(_GARMENT_YAML, encoding="utf-8")):
        session.add(GarmentType(code=r["code"], label=r.get("label"),
                                required_poms=r.get("required_poms") or [],
                                area_formula=r.get("area_formula") or {},
                                default_wastage_pct=r.get("default_wastage_pct")))
    await session.flush()
    for r in yaml.safe_load(open(_POM_DICT_YAML, encoding="utf-8")):
        session.add(PomDictionary(language=r["language"], source_term=r["source_term"],
                                  pom_code=r["pom_code"], weight=r.get("weight", 1)))
    # client validation templates (so the heuristic knows beau_geste's signals)
    for r in load_template_rows():
        session.add(ClientTemplate(
            client_code=r["client_code"], doc_kind=r["doc_kind"],
            display_name=r.get("display_name"), language=r.get("language"),
            size_system=r.get("size_system"), currency=r.get("currency"),
            expected_layout=r.get("expected_layout"), spec_type_hint=r.get("spec_type_hint"),
            accepted_mime=r.get("accepted_mime"), anchors=r.get("anchors") or [],
            fingerprints=r.get("fingerprints") or [], grid_signals=r.get("grid_signals") or {},
            thresholds=r.get("thresholds") or {}, is_active=r.get("is_active", True)))
    client = Client(name="Beau Geste / CRIMIE", country="Japan", code="BG", currency="USD")
    user = User(id=uuid.uuid4(), name="Demo DM", phone="9000000001",
                role=UserRole.DIRECT_MANAGER, password_hash="x", is_active=True)
    session.add_all([client, user])
    await session.commit()
    return client, user


async def upload(svc: ProcurementService, user, sub_id, kind: str, path: Path,
                 *, is_order: bool) -> None:
    data = path.read_bytes()
    fn = path.name
    method = svc.upload_order_sheet if is_order else svc.upload_spec_sheet
    # Pass override on the first call: a scanned PDF the classifier can only call
    # needs_manual_review is force-accepted in ONE round (no redundant re-run + no
    # second vision burst). A clean heuristic accept (the spec) ignores the flag.
    try:
        res = await method(user, sub_id, data, fn, override_manual_review=True)
        v = res.get("validation", {})
        forced = v.get("manual_override") or v.get("forced")
        print(f"  ✅ {kind}: {'force-accepted (manual override)' if forced else 'ACCEPTED'}"
              f" — method={v.get('method', 'heuristic')}")
    except UploadError as exc:
        # A HARD reject (wrong slot / not-the-kind / virus) is never overridable.
        print(f"  ❌ {kind}: hard-rejected '{exc.reason.value}' — cannot proceed")
        raise


async def main() -> int:
    if not ORDER_PDF.exists() or not SPEC_XLSX.exists():
        print(f"Missing demo files: {ORDER_PDF} / {SPEC_XLSX}")
        return 2
    if not settings.gemini_api_key:
        print("GEMINI_API_KEY is not set — export it before running this demo.")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    reset_storage_cache()

    engine = create_async_engine("sqlite+aiosqlite://",
                                 connect_args={"check_same_thread": False},
                                 poolclass=StaticPool)
    dbmod.async_engine = engine
    dbmod.AsyncSessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with dbmod.AsyncSessionLocal() as session:
        rule("1. SEED (garment types, POM dict, client templates, client, DM user)")
        client, user = await seed(session)
        print(f"  client={client.name} ({client.code})  gemini_model={settings.vision_model}")

        # Spend ZERO Gemini at the upload step (heuristic gate only; the scanned order is
        # force-accepted) so the ENTIRE free-tier per-minute budget is reserved for the one
        # call that matters — the order vision EXTRACTION at generate.
        svc = ProcurementService(session, classifier=None, vision_classifier=None)
        rule("2. OPEN SUBMISSION + UPLOAD both sheets (real validation pipeline)")
        sub = await svc.open_submission(user, client.id)
        print(f"  submission={sub.id}")
        await upload(svc, user, sub.id, "ORDER (scanned PDF → Gemini vision)",
                     ORDER_PDF, is_order=True)
        await upload(svc, user, sub.id, "SPEC  (xlsx measurement grid)",
                     SPEC_XLSX, is_order=False)

        status = await svc.get_submission_status(sub.id)
        print(f"  submission status = {status['status']}")

        if GENERATE_DELAY:
            print(f"\n  …pausing {GENERATE_DELAY}s so the Gemini per-minute window resets "
                  f"before the order vision extraction…")
            await asyncio.sleep(GENERATE_DELAY)

        rule("3. GENERATE BOM from submission (Gemini extracts the order)")
        gen = await svc.generate_bom_from_submission(user, sub.id)
        bom = gen["bom"]
        bom_id = uuid.UUID(bom["id"])
        order = gen.get("order", {})
        print(f"  BOM {bom['id']}  status={bom['status']}  revision={bom['revision']}")
        print(f"  order_qty={order.get('order_qty')}  per_size={order.get('per_size_qty')}")
        print(f"  order warnings={order.get('warnings')}")
        print(f"  extraction: POMs={gen['extraction']['poms']} "
              f"unresolved={len(gen['extraction']['unresolved'])}")
        print(f"  FOB/garment={bom['garment_fob_price']}  bulk_total={bom['bulk_total']}")
        print(f"  {len(bom['items'])} line items:")
        for it in bom["items"]:
            print(f"    - [{it['category']:>14}] {it['name'][:34]:34} "
                  f"qpg={it['qty_per_garment']} {it['uom'] or '':>4} "
                  f"@ {it['unit_price']}  dcm={it['dcm_source']}")

        rule("4. CONFIRM CUTTING → APPROVE → EXPORT PDF")
        bsvc = BomService(session)
        await bsvc.confirm_cutting(user, bom_id)
        print("  cutting confirmed → ready_for_review")
        await bsvc.approve_bom(user, bom_id)
        print("  approved (order/style breakdown materialised)")
        exp = await bsvc.export_bom(user, bom_id)
        print(f"  exported: status={exp['status']} sha={exp['sha256'][:12]} mime={exp['mime']}")

        # copy the rendered PDF out of object storage to a friendly path
        src_dir = STORAGE_DIR / "exports" / str(bom_id)
        pdfs = sorted(src_dir.glob("*"))
        if not pdfs:
            print("  ⚠️  no exported file found in storage")
            return 1
        dest = OUT_DIR / f"BOM-{bom_id}.pdf"
        shutil.copyfile(pdfs[-1], dest)
        rule("DONE")
        print(f"  📄 BOM PDF written to:\n     {dest}")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
