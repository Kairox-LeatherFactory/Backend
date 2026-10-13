"""
================================================================================
core/pagination.py — ONE page envelope and ONE way to ask for a page
================================================================================

WHY THIS EXISTS

Five in-scope routers (production, clients, cutting, dashboard, users) declared
no `limit`, `offset` or `page` parameter at all. Those endpoints return the whole
table. That is survivable while the factory has run for a few weeks and fatal
after a year: a list of every production event is a request that times out, a
worker process holding hundreds of megabytes, and a pooled database connection
held for the whole of it — and the pool is this service's real concurrency
ceiling.

The routers that DO paginate had each invented their own shape:

    {total, count, items}                        (wages)
    {page, page_size, total, pages, items}       (barcode)

so a frontend has to special-case each one, and there is no single place to fix
a paging bug. This module is the one shape and the one way to ask for it.

WHAT IT IS NOT

It does not retrofit the existing shapes. `RunPiecePage`, `LedgerPage`,
`BarcodeHistoryPage` and the label pages are published contracts that a
frontend is reading today, and quietly changing a response shape breaks a screen
without breaking a test. New and newly-paginated endpoints use `Page`; the older
ones migrate when their consumer is ready.

OFFSET, NOT CURSOR

Deliberate, for now. Offset paging is what a jump-to-page list screen needs and
what every one of these surfaces actually renders. It degrades on very deep
offsets, because the database still walks the skipped rows — if a surface ever
pages tens of thousands of rows deep, that one moves to a keyset cursor. None of
them do today, and a cursor API that nothing needs is a contract to maintain for
nothing.
================================================================================
"""
from __future__ import annotations

from typing import Generic, Sequence, TypeVar

from fastapi import Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

T = TypeVar("T")

# The cap exists so a caller cannot ask for the whole table by sending
# ?limit=999999 — which would reintroduce exactly the problem paging solves.
MAX_LIMIT = 200
DEFAULT_LIMIT = 50


class PageParams:
    """FastAPI dependency: `params: PageParams = Depends()`.

    A class rather than two loose Query arguments so every paginated route
    declares the same two parameters, with the same bounds and the same
    documentation, and so a later change (a cap, a cursor) lands in one place.
    """

    def __init__(
        self,
        limit: int = Query(
            DEFAULT_LIMIT, ge=1, le=MAX_LIMIT,
            description=f"Rows to return (1-{MAX_LIMIT}).",
        ),
        offset: int = Query(
            0, ge=0, description="Rows to skip before returning `limit` rows.",
        ),
    ) -> None:
        self.limit = limit
        self.offset = offset


class Page(BaseModel, Generic[T]):
    """One page of `T`, plus what the caller needs to render a pager.

    `total` is the size of the WHOLE result set, not of this page — it is what a
    list screen shows as "1-50 of 3,184" and what it needs to know how many
    pages exist. It costs a second COUNT query; that is the price of a pager, and
    it is far cheaper than shipping every row.

    `has_more` is derived rather than left to the caller, because
    `offset + len(items) < total` is exactly the kind of off-by-one every client
    gets to reimplement otherwise.
    """

    items: list[T] = Field(default_factory=list)
    total: int = Field(description="Rows matching the query, ignoring paging.")
    limit: int
    offset: int
    count: int = Field(description="Rows in THIS page — len(items).")
    has_more: bool = Field(description="True when another page follows.")

    @classmethod
    def of(cls, items: Sequence[T], *, total: int, params: PageParams) -> "Page[T]":
        """Build a page from a slice and the total. The one construction path."""
        items = list(items)
        return cls(
            items=items,
            total=total,
            limit=params.limit,
            offset=params.offset,
            count=len(items),
            has_more=(params.offset + len(items)) < total,
        )


async def paginate(db: AsyncSession, stmt, params: PageParams) -> tuple[list, int]:
    """Run `stmt` as one page and count the whole match. Returns (rows, total).

    TWO QUERIES, ONE SOURCE OF TRUTH. The count is derived from the SAME
    statement as the page, wrapped as a subquery, so the two cannot disagree
    about which rows they are talking about. Hand-writing a separate COUNT is
    how a pager ends up claiming 40 rows over 3 pages while the filters actually
    match 12 — and it does that silently, because both queries succeed.

    The ORDER BY is stripped from the count. It changes nothing about how many
    rows match and, on Postgres, sorting a set you are only going to count is
    wasted work.

    THE SLICE HAPPENS IN SQL, not in Python. Fetching everything and slicing the
    list would shrink the response but not the query: the database would still
    build the whole result and ship it over the wire, which is the part that
    holds the pooled connection open and is the actual cost here.
    """
    total = int(await db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)
    rows = list((await db.execute(
        stmt.limit(params.limit).offset(params.offset))).scalars())
    return rows, total


async def _count_of(db: AsyncSession, stmt) -> int:
    return int(await db.scalar(
        select(func.count()).select_from(stmt.order_by(None).subquery())) or 0)


async def paginate_rows(db: AsyncSession, stmt, params: PageParams) -> tuple[list, int]:
    """As `paginate`, for a statement that selects several columns.

    `paginate` calls `.scalars()`, which keeps only the FIRST column — correct
    for `select(Model)` and silently destructive for
    `select(Piece, Operation.code, ...)`, where it would hand back bare Pieces
    and drop the joined columns with no error. This returns whole rows.
    """
    total = await _count_of(db, stmt)
    rows = (await db.execute(stmt.limit(params.limit).offset(params.offset))).all()
    return list(rows), total
