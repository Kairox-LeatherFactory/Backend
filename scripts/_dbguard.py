"""
================================================================================
scripts/_dbguard.py — refuse to run when the sync and async URLs disagree
================================================================================

WHAT HAPPENED, 2026-09-28
    Someone testing scripts/seed.py pointed DATABASE_URL at a scratch SQLite file
    and ran it. Forty-one employees, forty-one employee cards and seven staff
    logins were written to the LIVE Supabase database instead.

    Nothing was misconfigured and nothing crashed. `DATABASE_URL` redirects the
    SYNC engine only:

        # app/core/config.py::effective_async_url
        if self.async_database_url:      # ASYNC_DATABASE_URL, set in .env
            return self.async_database_url

    That return is unconditional, so the sync→async derivation beneath it — the
    branch that would have turned `sqlite+pysqlite://` into `sqlite+aiosqlite://`
    — is never reached while ASYNC_DATABASE_URL is set. And .env sets it.

    scripts/seed.py straddles both engines: its own steps are sync, but step 6
    (`seed_people`) delegates to the async scripts/seed_employees.py. So the run
    wrote most of its rows to the scratch file and the roster to production, and
    reported success. The summary even said "41 already present" on a table the
    operator could see was empty — because it was reading a different database
    than the one they were looking at.

WHY A GUARD AND NOT A DOCUMENTATION NOTE
    The failure is silent, it is one environment variable wide, and the person
    most likely to hit it is the person deliberately trying NOT to touch
    production. A comment cannot catch that; a startup check can. And the check
    does not need to know which database is the "right" one to be useful: a
    seeder that writes half its rows to one database and half to another is
    broken WHICHEVER of the two is production, so disagreement alone is enough
    to stop.

WHY IT IS NOT IN app/core/config.py
    The app itself is legitimately allowed to run with a pooled async URL and a
    direct sync URL that differ in host — Alembic uses the sync one and the
    pooler is a different endpoint. Only these scripts, which mix both engines
    over the same rows, need the two to be the same database.

USAGE
    from scripts._dbguard import assert_one_database
    assert_one_database("seed.py")        # at the top of main(), before any write

    Pass --allow-split-db (see `add_argument`) to proceed anyway; it prints what
    it is about to do rather than being silent about it.
================================================================================
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy.engine import make_url

_ENV_OVERRIDE = "KAIROX_ALLOW_SPLIT_DB"


def _mask(url: str) -> str:
    """Render a URL with the password removed, for printing."""
    try:
        return make_url(url).render_as_string(hide_password=True)
    except Exception:
        return "<unparseable URL>"


def _identity(url: str) -> tuple[str, str, str, str]:
    """The part of a URL that says WHICH DATABASE, ignoring the driver.

    `postgresql+psycopg2://u@h:5432/postgres` and
    `postgresql+asyncpg://u@h:5432/postgres` are the same database reached two
    ways, and must compare equal — the driver is exactly what legitimately
    differs between the sync and async URLs. What must match is the backend, the
    host, the port and the database name.

    For SQLite the "database" is a file path, and that is what is compared; a
    scratch file and a server are never going to look equal, which is the case
    this exists to catch.
    """
    u = make_url(url)
    backend = u.get_backend_name()
    host = (u.host or "").lower()
    port = str(u.port or "")
    name = u.database or ""
    if backend == "sqlite":
        # Normalise so /tmp/x.db and \tmp\x.db compare equal on Windows.
        name = os.path.normcase(os.path.abspath(name)) if name else ":memory:"
    return backend, host, port, name


def add_argument(parser: argparse.ArgumentParser) -> None:
    """Add the documented escape hatch to a script's own parser."""
    parser.add_argument(
        "--allow-split-db", action="store_true",
        help="proceed even though the sync and async URLs name different "
             "databases (the run will write some rows to each)")


def assert_one_database(script: str, *, allow_split: bool = False) -> None:
    """Exit non-zero unless both engines resolve to the same database.

    `allow_split` (or KAIROX_ALLOW_SPLIT_DB=1) downgrades the refusal to a loud
    warning, for the rare case where the split is genuinely intended.
    """
    from app.core.config import settings

    sync_url = settings.database_url
    async_url = settings.effective_async_url
    try:
        same = _identity(sync_url) == _identity(async_url)
    except Exception as exc:                       # unparseable → say so, don't guess
        print(f"WARNING [{script}]: could not compare the database URLs "
              f"({type(exc).__name__}: {exc}). Proceeding.", file=sys.stderr)
        return

    if same:
        return

    override = allow_split or os.environ.get(_ENV_OVERRIDE) == "1"
    banner = "=" * 78
    lines = [
        banner,
        f"{'PROCEEDING WITH' if override else 'REFUSING TO RUN'}: "
        f"{script} would write to TWO DIFFERENT DATABASES.",
        banner,
        f"  sync  (DATABASE_URL)        {_mask(sync_url)}",
        f"  async (ASYNC_DATABASE_URL)  {_mask(async_url)}",
        "",
        "  Sync steps would write to the first; async steps (and anything they",
        "  delegate to, such as seed_employees.py) to the second. Half the rows",
        "  would land in each, and the run would report success.",
        "",
        "  ASYNC_DATABASE_URL takes absolute precedence in",
        "  app/core/config.py::effective_async_url, so setting DATABASE_URL",
        "  alone does NOT redirect the async engine. To point this script at a",
        "  scratch database, set BOTH:",
        "",
        "     DATABASE_URL=sqlite+pysqlite:///./scratch.db",
        "     ASYNC_DATABASE_URL=sqlite+aiosqlite:///./scratch.db",
        "",
        "  or unset ASYNC_DATABASE_URL and let it be derived from the sync URL.",
        banner,
    ]
    print("\n".join(lines), file=sys.stderr)
    if not override:
        # The env var is named first because it is the one that ALWAYS works:
        # --allow-split-db exists only on the scripts that have an argument
        # parser, and seed_employees.py does not. Offering a flag that a given
        # script does not accept sends the reader to an argparse error.
        raise SystemExit(
            f"{script}: aborted before writing anything. Set {_ENV_OVERRIDE}=1 "
            f"(or pass --allow-split-db, where the script accepts it) if this "
            f"really is what you want.")
