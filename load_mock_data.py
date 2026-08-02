"""
load_mock_data.py — loads mock_data.json into the real Postgres DB.
 
USAGE (from your project root, in VS Code's integrated terminal):
 
    python load_mock_data.py
 
WHAT THIS ASSUMES
  - You run this from the repo root (the directory containing `app/`), so
    `from app.core.database import ...` resolves. If your engine/session
    live somewhere else, fix the import in `get_engine()` below — that is
    the ONE place this script talks to your DB setup.
  - Migrations have already created every table (you confirmed this).
  - mock_data.json (from gen_mock_data.py) sits next to this script.
 
WHAT THIS DOES
  1. Imports every model module so all tables are registered on Base.metadata
     (we insert via Core, using the real table objects — this guarantees
     column names/types match exactly what your models define, no drift
     from hand-copied table names).
  2. Reads mock_data.json, converts UUID / date / datetime strings back to
     real Python objects (Core inserts don't auto-coerce for asyncpg).
  3. Inserts every table in FK-dependency order inside one transaction,
     using ON CONFLICT DO NOTHING — so re-running this script is always
     safe. Rows that already exist (matching on primary key OR any unique
     constraint, e.g. app_user.phone) are silently skipped. Nothing is
     ever deleted or overwritten, and existing data in your DB is never
     touched.
  4. Special-cases piece <-> drawer, which reference each other: pieces are
     inserted with drawer_id=NULL first, then drawers, then piece.drawer_id
     is patched with an UPDATE (only for pieces inserted in this run).
 
If anything fails partway, the whole transaction rolls back — safe to fix
and re-run.
"""
import asyncio
import json
import uuid
from datetime import date, datetime
from pathlib import Path
 
from sqlalchemy import Table
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection
 
# ── 1. Wire up your project's engine ────────────────────────────────────
# Adjust this import if your engine/session object has a different name or
# location. It must be an SQLAlchemy AsyncEngine (the modules use AsyncSession
# throughout, e.g. drawers/router.py).
from app.core.database import async_engine  # noqa: E402
 
# Import every model module so Base.metadata knows about all the tables we
# need to insert into. Adjust these if your package path differs from
# `app.modules.<name>`.
from app.core.database import Base  # noqa: E402
import app.modules.employees.models  # noqa: F401,E402
import app.modules.clients.models  # noqa: F401,E402
import app.modules.users.models  # noqa: F401,E402
import app.modules.bom.models  # noqa: F401,E402
import app.modules.procurement.models  # noqa: F401,E402
import app.modules.inventory.models  # noqa: F401,E402
import app.modules.supplier_po.models  # noqa: F401,E402
import app.modules.production.models  # noqa: F401,E402
import app.modules.barcode.models  # noqa: F401,E402
import app.modules.wages.models  # noqa: F401,E402
 
MOCK_DATA_PATH = Path(__file__).parent / "mock_data.json"
 
# ── 2. Insertion order (parents before children) ────────────────────────
# One flat list of table names in an order that never violates a FK.
# `piece` and `drawer` are handled specially (see load()) because they
# reference each other.
TABLE_ORDER = [
    "employee",
    "client", "client_order", "style", "sku",
    "app_user",
    "garment_type", "submission", "client_template",
    "bom", "bom_item",
    "inventory_item", "inventory_check", "inventory_check_line",
    "inventory_reservation", "material_alias", "uom_conversion",
    "supplier", "supplier_supply_history", "purchase_order", "po_item",
    "po_response", "po_tracking_event", "production_tracking",
    "operation", "operation_access", "style_operation",
    "material_supplier", "material_lot",
    # piece + drawer handled specially, then:
    "production_event", "material_reservation", "supplier_order",
    "material_receipt", "barcode_registry",
    "rate", "wage_run", "wage_line_detail", "wage_line",
]
 
UUID_SUFFIX = "_id"
 
 
def _coerce(table_name: str, col_name: str, value):
    """Turn JSON-native values back into the Python types Core expects."""
    if value is None:
        return None
    # UUID primary keys and every *_id foreign key.
    if col_name == "id" or col_name.endswith(UUID_SUFFIX):
        try:
            return uuid.UUID(value)
        except (ValueError, TypeError, AttributeError):
            return value  # not actually a uuid string (rare column named *_id)
    # ISO datetime strings we wrote as "...T..Z"
    if isinstance(value, str) and value.endswith("Z") and "T" in value:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    # Plain ISO date strings "YYYY-MM-DD"
    if isinstance(value, str) and len(value) == 10 and value.count("-") == 2:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return value
    return value
 
 
def _rows_for(table_name: str, tables_json: dict) -> list[dict]:
    """mock_data.json is grouped by module, not by table -- flatten + coerce."""
    for module_tables in tables_json.values():
        if table_name in module_tables:
            rows = module_tables[table_name]
            return [
                {k: _coerce(table_name, k, v) for k, v in row.items()}
                for row in rows
            ]
    return []
 
 
async def _insert_skip_existing(conn: AsyncConnection, table: Table, rows: list[dict]) -> int:
    """
    Insert rows, silently skipping any that collide with an existing row on
    ANY unique constraint (primary key, unique index, etc. — e.g.
    app_user.phone). Never deletes or overwrites anything. Returns the
    number of rows actually inserted (rows that were skipped are not
    counted).
    """
    if not rows:
        return 0
    stmt = pg_insert(table).values(rows).on_conflict_do_nothing()
    result = await conn.execute(stmt)
    return result.rowcount
 
 
def _walk_and_remap(obj, remap: dict):
    """Recursively replace any string UUID in obj that's a key in remap."""
    if isinstance(obj, dict):
        return {k: _walk_and_remap(v, remap) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_and_remap(v, remap) for v in obj]
    if isinstance(obj, str) and obj in remap:
        return remap[obj]
    return obj
 
 
async def _build_app_user_remap(conn, data: dict) -> dict[str, str]:
    """
    Some staff app_user rows (Direct Manager, Cutting Manager, etc.) may already
    exist in the DB under a DIFFERENT id than mock_data.json uses -- e.g. seeded
    earlier by scripts/seed.py, which is phone-idempotent but uses its own ids.
 
    ON CONFLICT DO NOTHING would then skip inserting the mock's app_user row
    (phone collision) while every other mock table still references the mock's
    id for that person -- causing FK violations downstream.
 
    Fix: for every mock app_user row whose phone already exists in the DB under
    a different id, build {mock_id (str) -> real_db_id (str)} and rewrite every
    reference to that mock id, everywhere in the loaded JSON, BEFORE inserting.
    The mock's app_user row for that phone is then dropped entirely (the real
    row already exists and is left untouched).
    """
    from sqlalchemy import text
 
    mock_app_user_rows = _rows_for("app_user", data)
    if not mock_app_user_rows:
        return {}
 
    phones = [row["phone"] for row in mock_app_user_rows if row.get("phone")]
    if not phones:
        return {}
 
    result = await conn.execute(
        text("SELECT id, phone FROM app_user WHERE phone = ANY(:phones)"),
        {"phones": phones},
    )
    existing_by_phone = {phone: str(db_id) for db_id, phone in result.fetchall()}
 
    remap: dict[str, str] = {}
    for row in mock_app_user_rows:
        phone = row.get("phone")
        mock_id = str(row["id"])
        real_id = existing_by_phone.get(phone)
        if real_id and real_id != mock_id:
            remap[mock_id] = real_id
    return remap
 
 
async def load():
    data = json.loads(MOCK_DATA_PATH.read_text())
 
    async with async_engine.begin() as conn:
        # Resolve any staff/app_user identity collisions (e.g. against
        # scripts/seed.py's staff accounts) and rewrite the whole dataset to
        # point at the real existing ids instead of the mock's own ids.
        remap = await _build_app_user_remap(conn, data)
        if remap:
            print(f"  remapping {len(remap)} app_user id(s) to existing DB rows "
                  f"(matched by phone):")
            for mock_id, real_id in remap.items():
                print(f"    {mock_id}  ->  {real_id}")
            data = _walk_and_remap(data, remap)
            # Drop the mock's own app_user rows for the phones we just remapped
            # -- the real row already exists, so there's nothing left to insert
            # for them (avoids relying on ON CONFLICT DO NOTHING to skip it,
            # which is correct anyway but this keeps intent explicit/logged).
            for module_tables in data.values():
                if "app_user" in module_tables:
                    module_tables["app_user"] = [
                        row for row in module_tables["app_user"]
                        if str(row["id"]) not in remap
                    ]
 
        # Normal tables, parents before children.
        for table_name in TABLE_ORDER:
            rows = _rows_for(table_name, data)
            if not rows:
                continue
            table = Base.metadata.tables[table_name]
            inserted = await _insert_skip_existing(conn, table, rows)
            skipped = len(rows) - inserted
            msg = f"  inserted {inserted:>3} -> {table_name}"
            if skipped:
                msg += f"   (skipped {skipped} already present)"
            print(msg)
 
        # piece <-> drawer: insert pieces with drawer_id nulled out, then
        # drawers, then patch piece.drawer_id back in (only for pieces we
        # actually inserted this run — existing pieces are left untouched).
        piece_rows = _rows_for("piece", data)
        if piece_rows:
            piece_table = Base.metadata.tables["piece"]
            deferred_drawer_links = {
                row["id"]: row["drawer_id"] for row in piece_rows if row.get("drawer_id")
            }
            insert_rows = [{**row, "drawer_id": None} for row in piece_rows]
            inserted = await _insert_skip_existing(conn, piece_table, insert_rows)
            skipped = len(piece_rows) - inserted
            msg = f"  inserted {inserted:>3} -> piece (drawer_id deferred)"
            if skipped:
                msg += f"   (skipped {skipped} already present)"
            print(msg)
 
            drawer_rows = _rows_for("drawer", data)
            if drawer_rows:
                drawer_table = Base.metadata.tables["drawer"]
                d_inserted = await _insert_skip_existing(conn, drawer_table, drawer_rows)
                d_skipped = len(drawer_rows) - d_inserted
                msg = f"  inserted {d_inserted:>3} -> drawer"
                if d_skipped:
                    msg += f"   (skipped {d_skipped} already present)"
                print(msg)
 
            # Only patch drawer_id for pieces that were newly inserted this
            # run; pieces already in the DB (skipped above) keep whatever
            # drawer_id they already have.
            newly_inserted_ids = {row["id"] for row in insert_rows} if inserted else set()
            patched = 0
            for piece_id, drawer_id in deferred_drawer_links.items():
                if piece_id not in newly_inserted_ids:
                    continue
                await conn.execute(
                    piece_table.update()
                    .where(piece_table.c.id == piece_id)
                    .values(drawer_id=drawer_id)
                )
                patched += 1
            if patched:
                print(f"  patched   {patched:>3} -> piece.drawer_id")
        else:
            drawer_rows = _rows_for("drawer", data)
            if drawer_rows:
                drawer_table = Base.metadata.tables["drawer"]
                d_inserted = await _insert_skip_existing(conn, drawer_table, drawer_rows)
                d_skipped = len(drawer_rows) - d_inserted
                msg = f"  inserted {d_inserted:>3} -> drawer"
                if d_skipped:
                    msg += f"   (skipped {d_skipped} already present)"
                print(msg)
 
    print("\nDone. Transaction committed.")
 
 
if __name__ == "__main__":
    asyncio.run(load())