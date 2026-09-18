"""the store moves from the drawer onto the garment

Revision ID: 20260902_store_piece
Revises: 20260901_cutting_v2
Create Date: 2026-09-02

WHY THE DRAWER IS GOING
──────────────────────────────────────────────────────────────────────────────
There are 200 physical drawers. A style releases 100+ garments, stalls partway
down the chain, and the next 50 have nowhere to go — so the DM had to re-allocate
by hand, which is complicated enough that in practice it did not happen and
pieces sat on the "waiting for a drawer" list instead of moving.

The drawer bought nothing for that cost. Every fact it carried is a fact about
the GARMENT:

    drawer.state           → piece.store_state
    drawer.leather_in      → piece.leather_in
    drawer.lining_in       → piece.lining_in
    drawer.accessories_in  → piece.accessories_in
    drawer.received_at     → piece.store_received_at
    drawer.sended_at       → piece.store_sended_at

So the store stops being a place with a fixed number of slots and becomes a state
the garment is in, which has no capacity at all.

THE DATA IS COPIED, NOT DISCARDED
──────────────────────────────────────────────────────────────────────────────
Pieces are sitting in drawers right now. The copy below walks
`drawer.current_piece_id` — the live claim, the same link the merge gate reads —
so every garment mid-store keeps its exact position, its parts and its
timestamps. Nothing in flight is lost and nothing has to be re-scanned.

The VALUES are carried across unchanged (waiting / merged / holding_leather /
holding_lining / holding_both / received / sended). That is deliberate: the STORE
display overlay, the dashboard's `?state=` filter, the analytics buckets and the
label tables all read those strings, and translating them would have meant a
mapping layer nobody would maintain.

THE DRAWER TABLES ARE KEPT, AND KEPT READABLE
──────────────────────────────────────────────────────────────────────────────
Nothing is dropped. `drawer`, `piece.drawer_id` and the DRAWER barcodes stay
exactly as they are, unwritten from here on, so the movement history remains
auditable and this migration is reversible by simply not using the new columns.
Dropping them is a separate decision for a later release, once the new path has
run a full order through.

PORTABILITY (CLAUDE.md §13)
    No native PG enum — `store_state` is VARCHAR, so adding a state later never
    needs an ALTER TYPE. No JSONB, no ON CONFLICT. The boolean server_defaults
    are spelled the way 20260819 and 20260821 spell them, so SQLite and Postgres
    both take them.
"""
from alembic import op
import sqlalchemy as sa

revision = "20260902_store_piece"
down_revision = "20260901_cutting_v2"
branch_labels = None
depends_on = None

_NEW_COLUMNS = [
    ("store_state", sa.String(length=20), "waiting"),
    ("leather_in", sa.Boolean(), None),
    ("lining_in", sa.Boolean(), None),
    ("accessories_in", sa.Boolean(), None),
]


def upgrade() -> None:
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    false_ = sa.text("0" if is_sqlite else "false")

    op.add_column("piece", sa.Column("store_state", sa.String(length=20),
                                     nullable=False, server_default="waiting"))
    for name in ("leather_in", "lining_in", "accessories_in"):
        op.add_column("piece", sa.Column(name, sa.Boolean(), nullable=False,
                                         server_default=false_))
    for name in ("store_entered_at", "store_received_at", "store_sended_at"):
        op.add_column("piece", sa.Column(name, sa.DateTime(timezone=True),
                                         nullable=True))
    op.create_index("ix_piece_store_state", "piece", ["store_state"])

    # ── THE COPY ─────────────────────────────────────────────────────────────
    # Through drawer.current_piece_id, the LIVE claim. `piece.drawer_id` is the
    # assignment and the two can legitimately differ (a released piece keeps its
    # assignment while the drawer has moved on), so the claim is the one that
    # says what is physically in there right now — and it is the link the merge
    # gate itself reads, which is the behaviour being preserved.
    #
    # Guarded on the drawer table existing so this runs on a database built
    # after the drawer feature is eventually dropped.
    if "drawer" in sa.inspect(bind).get_table_names():
        op.execute("""
            UPDATE piece SET
                store_state = (
                    SELECT d.state FROM drawer d
                    WHERE d.current_piece_id = piece.id),
                leather_in = (
                    SELECT d.leather_in FROM drawer d
                    WHERE d.current_piece_id = piece.id),
                lining_in = (
                    SELECT d.lining_in FROM drawer d
                    WHERE d.current_piece_id = piece.id),
                accessories_in = (
                    SELECT d.accessories_in FROM drawer d
                    WHERE d.current_piece_id = piece.id),
                store_received_at = (
                    SELECT d.received_at FROM drawer d
                    WHERE d.current_piece_id = piece.id),
                store_sended_at = (
                    SELECT d.sended_at FROM drawer d
                    WHERE d.current_piece_id = piece.id)
            WHERE EXISTS (
                SELECT 1 FROM drawer d WHERE d.current_piece_id = piece.id)
        """)


def downgrade() -> None:
    """Drop the columns. The drawer tables were never touched, so the old path
    is still whole — which is the point of having kept them."""
    op.drop_index("ix_piece_store_state", table_name="piece")
    for name in ("store_sended_at", "store_received_at", "store_entered_at",
                 "accessories_in", "lining_in", "leather_in", "store_state"):
        op.drop_column("piece", name)
