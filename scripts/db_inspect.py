"""READ-ONLY Supabase/Postgres inspection. Connects, lists public tables and
row counts, and reports the server version. Writes NOTHING.

Run:  .venv\\Scripts\\python.exe -m scripts.db_inspect
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings


async def main():
    url = settings.effective_async_url
    # mask the password in any printout
    safe = url
    if "@" in safe and "://" in safe:
        head, tail = safe.split("://", 1)
        creds, host = tail.split("@", 1)
        if ":" in creds:
            user = creds.split(":", 1)[0]
            safe = f"{head}://{user}:****@{host}"
    print("=" * 74)
    print("SUPABASE / POSTGRES READ-ONLY INSPECTION")
    print("=" * 74)
    print(f"Async URL: {safe}")

    # asyncpg on the Supabase pooler: disable statement cache for safety.
    engine = create_async_engine(
        url, connect_args={"statement_cache_size": 0}, pool_pre_ping=True
    )
    try:
        async with engine.connect() as conn:
            ver = (await conn.execute(text("select version()"))).scalar()
            print(f"\n[OK] Connected. Server: {ver}")

            rows = (await conn.execute(text(
                "select table_name from information_schema.tables "
                "where table_schema='public' order by table_name"
            ))).scalars().all()
            print(f"\nPublic tables ({len(rows)}):")
            if not rows:
                print("  (none — schema is empty)")
            for t in rows:
                try:
                    c = (await conn.execute(text(f'select count(*) from "{t}"'))).scalar()
                except Exception as e:
                    c = f"err: {e}"
                print(f"  - {t:<28} rows={c}")
        print("\n[OK] Inspection complete. No data was modified.")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
