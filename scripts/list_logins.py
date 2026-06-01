"""READ-ONLY: list app_user phones/roles (NO password hashes) to find a login
to test with. Writes nothing."""
import asyncio, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from app.core.config import settings


async def main():
    engine = create_async_engine(settings.effective_async_url,
                                 connect_args={"statement_cache_size": 0})
    async with engine.connect() as conn:
        rows = (await conn.execute(text(
            "select role, count(*) from app_user group by role order by role"
        ))).all()
        print("Roles:")
        for r, c in rows:
            print(f"  {r}: {c}")
        mgr = (await conn.execute(text(
            "select name, phone, role::text, must_change_password, is_active "
            "from app_user where role::text = 'DIRECT_MANAGER' limit 3"
        ))).all()
        print("\nDirect managers (phone is the default password if never changed):")
        for name, phone, role, mcp, active in mgr:
            print(f"  name={name!r} phone={phone!r} active={active} must_change_password={mcp}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
