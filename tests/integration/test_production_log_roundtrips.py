"""
================================================================================
tests/integration/test_production_log_roundtrips.py — the scan path's DB cost
================================================================================
THE POOL, NOT THE CPU, IS WHAT RUNS OUT FIRST.

An in-flight request holds one pooled database connection for its entire
lifetime, so the number of requests this service can genuinely serve at once is
`workers x (pool_size + max_overflow)` — a small number. What decides whether a
100-user floor works is therefore how LONG each request holds its connection,
and that is dominated by round-trips, not by instructions.

`POST /production/log` is the whole floor's write surface and it is inherently a
BATCH: a manager scans a trolley, not a garment. So a per-piece query inside the
gate loop is multiplied by the batch size, and each one is a serial wait with the
connection held open.

This test pins the shape of that cost: the batched path must issue strictly fewer
statements than the per-piece path, and the gap must WIDEN with batch size. It is
a guard, not a benchmark — it asserts the growth curve, not a wall-clock number,
so it is stable on any machine.
================================================================================
"""
from datetime import date as _date

import pytest
from sqlalchemy import event

from app.modules.production.models import Piece
from app.modules.production.service import ProductionService
from tests.conftest import _log_stage


async def _noop():
    return None


async def _count_statements(db, svc, pieces, mgr, emp_id, preview=True):
    count = {"n": 0}
    bind = db.get_bind()
    sync_engine = getattr(bind, "sync_engine", bind)

    def _before(conn, cur, stmt, params, ctx, many):
        count["n"] += 1

    event.listen(sync_engine, "before_cursor_execute", _before)
    try:
        await svc.log_batch(user=mgr, employee_id=emp_id,
                            piece_ids=[p.id for p in pieces],
                            work_date=_date.today(), screen=None, preview=preview)
    finally:
        event.remove(sync_engine, "before_cursor_execute", _before)
    return count["n"]


async def _fused_batch(db, order_tree, operations, cutter, n, tag, seq0=0):
    """`n` pieces that have been leather-cut, so FUSING is the inferred stage."""
    sku = order_tree["sku"]
    made = []
    # seq is UNIQUE per sku (a real constraint), so each batch needs its own band
    for seq in range(seq0 + 1, seq0 + n + 1):
        p = Piece(code=f"{tag}-{seq:04d}", seq=seq, sku_id=sku.id,
                  current_operation_id=None)
        if hasattr(p, "needs_lining"):
            p.needs_lining = False
        db.add(p)
        made.append(p)
    await db.flush()
    for p in made:
        await _log_stage(db, operations, p, cutter[0].id, "LEATHER_CUTTING")
    await db.commit()
    return made


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_batching_the_sequence_gate_cuts_round_trips(
        db, order_tree, operations, cutter, stitching_mgr):
    """The sequence gate asks one question per piece; it must ask it ONCE.

    `_prefetch_sequence` loads every (piece, predecessor-op) event the gate will
    need in a single statement. Disabling it restores the old behaviour, so the
    two paths can be compared on identical data.
    """
    n = 40
    emp_id = cutter[0].id

    old_pieces = await _fused_batch(db, order_tree, operations, cutter, n, "OLD", seq0=0)
    slow = ProductionService(db)
    slow._prefetch_sequence = lambda *a, **k: _noop()     # pre-fix behaviour
    per_piece = await _count_statements(db, slow, old_pieces, stitching_mgr, emp_id)

    new_pieces = await _fused_batch(db, order_tree, operations, cutter, n, "NEW", seq0=1000)
    fast = ProductionService(db)
    batched = await _count_statements(db, fast, new_pieces, stitching_mgr, emp_id)

    assert batched < per_piece, (
        f"batching saved nothing: {batched} vs {per_piece} statements for {n} "
        "pieces — _prefetch_sequence is not being used")
    # One statement per piece removed, minus the single prefetch that replaced them.
    assert per_piece - batched >= n - 2, (
        f"expected to save about {n} statements, saved {per_piece - batched}")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_the_saving_grows_with_the_batch(
        db, order_tree, operations, cutter, stitching_mgr):
    """Per-piece cost must FALL as the batch grows — that is what makes a trolley
    scan cheaper than the garments in it scanned one at a time."""
    emp_id = cutter[0].id
    per_piece_cost = {}
    for i, n in enumerate((5, 40)):
        pieces = await _fused_batch(db, order_tree, operations, cutter, n,
                                    f"G{n}", seq0=5000 + i * 2000)
        svc = ProductionService(db)
        per_piece_cost[n] = await _count_statements(
            db, svc, pieces, stitching_mgr, emp_id) / n

    assert per_piece_cost[40] < per_piece_cost[5], (
        f"per-piece cost did not improve with batch size: {per_piece_cost}")


@pytest.mark.asyncio
@pytest.mark.integrity
async def test_the_write_path_is_where_the_saving_lands(
        db, order_tree, operations, cutter, stitching_mgr):
    """The REAL path — preview=False — is what the floor actually runs.

    Preview skips the write block, and the write block is where the rework check
    lives (“does this piece already have an event at the stage I am about to
    write?”). That question is the same (piece, op) lookup the sequence gate
    asks, so the same prefetch answers it — but only a non-preview run exercises
    it. Measured here because it is roughly double the saving preview shows.
    """
    n = 40
    emp_id = cutter[0].id

    old_pieces = await _fused_batch(db, order_tree, operations, cutter, n,
                                    "WOLD", seq0=20000)
    slow = ProductionService(db)
    slow._prefetch_sequence = lambda *a, **k: _noop()
    per_piece = await _count_statements(db, slow, old_pieces, stitching_mgr,
                                        emp_id, preview=False)

    new_pieces = await _fused_batch(db, order_tree, operations, cutter, n,
                                    "WNEW", seq0=30000)
    fast = ProductionService(db)
    batched = await _count_statements(db, fast, new_pieces, stitching_mgr,
                                      emp_id, preview=False)

    # Two lookups per piece removed (predecessor + target), so the write path
    # should come in comfortably under half.
    assert batched < per_piece * 0.75, (
        f"write path saved too little: {batched} vs {per_piece} for {n} pieces")
