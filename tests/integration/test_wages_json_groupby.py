"""
INTEGRATION · GET /wages/runs must not GROUP BY a `json` column.

THE BUG THIS PINS, and why the rest of the suite could not see it.

`wage_run.unrated_snapshot` is a **json** column. PostgreSQL gives the `json`
type no equality operator — there is literally no way to ask whether two json
values are the same — so it may never appear in a GROUP BY:

    asyncpg.exceptions.UndefinedFunctionError:
    could not identify an equality operator for type json

`list_runs` selected twelve run columns beside three aggregates, so it had to
group by all twelve, snapshot included. Every call to GET /wages/runs on
Postgres was a 500. **SQLite is permissive about both GROUP BY and json
equality**, so the same statement ran green here for as long as it existed —
which is exactly why this test asserts on the SQL that is EMITTED rather than on
the rows that come back. A behavioural test cannot fail on SQLite, and this one
can.

The fix sums wage_line per run in a subquery and left-joins it, so the run's own
columns are never grouped at all.
"""
import datetime
import re

import pytest
from sqlalchemy import event

from app.core.enums import RunStatus, WageRunKind
from app.modules.wages.models import WageRun
from app.modules.wages.service import WageService

pytestmark = [pytest.mark.asyncio, pytest.mark.integrity]

# Columns that are `json` on PostgreSQL. A GROUP BY over any of them is a 500
# there, whatever SQLite says. Keep this list in step with the model.
JSON_COLUMNS = ("unrated_snapshot",)


def _group_by_clause(sql: str) -> str:
    """Everything between GROUP BY and the next top-level keyword."""
    m = re.search(r"GROUP BY(.*?)(ORDER BY|LIMIT|OFFSET|HAVING|$)", sql,
                  re.S | re.I)
    return m.group(1) if m else ""


@pytest.fixture
def captured(db):
    """Every statement this session emits, as raw SQL."""
    seen: list[str] = []
    engine = db.get_bind()          # already the sync Engine under the async one

    def _record(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    yield seen
    event.remove(engine, "before_cursor_execute", _record)


async def test_listing_runs_never_groups_by_the_json_snapshot(db, captured):
    """THE REGRESSION. Assert on the SQL, because SQLite will run either one."""
    db.add(WageRun(
        period_start=datetime.date(2026, 8, 1),
        period_end=datetime.date(2026, 9, 22),
        status=RunStatus.CLOSED, run_kind=WageRunKind.PIECE.value,
        scope_order_number="2222", unrated_snapshot=[]))
    await db.commit()

    runs = await WageService(db).list_runs(order_number="2222")
    assert len(runs) == 1, "the filtered run must still come back"

    selects = [s for s in captured if "wage_run" in s.lower()]
    assert selects, "the listing query was not captured"
    for sql in selects:
        clause = _group_by_clause(sql).lower()
        for col in JSON_COLUMNS:
            assert col not in clause, (
                f"GROUP BY carries the json column `{col}` — this is a 500 on "
                f"PostgreSQL ('could not identify an equality operator for type "
                f"json') however green it runs on SQLite.\n\n{sql}")


async def test_the_totals_are_still_right_per_run(db, captured):
    """The subquery must not change the numbers it replaced.

    A per-run LEFT JOIN onto a pre-aggregated subquery is only equivalent to the
    old GROUP BY if a run with NO lines still comes back with zeros rather than
    dropping out of the list.
    """
    empty = WageRun(
        period_start=datetime.date(2026, 8, 1),
        period_end=datetime.date(2026, 8, 15),
        status=RunStatus.OPEN, run_kind=WageRunKind.PIECE.value,
        scope_order_number="EMPTY-RUN", unrated_snapshot=[])
    db.add(empty)
    await db.commit()

    runs = await WageService(db).list_runs(order_number="EMPTY-RUN")
    assert len(runs) == 1, "a run with no wage lines must still be listed"
    row = runs[0]
    assert row["total_amount"] == 0.0
    assert row["total_pieces"] == 0
    assert row["employee_count"] == 0
