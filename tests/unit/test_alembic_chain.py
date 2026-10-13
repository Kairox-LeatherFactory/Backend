"""
UNIT · the alembic revision graph must be one unbroken line with exactly one head.

WHY THIS FILE EXISTS
    This broke twice, the same way both times, and cost a merge each time.

    In this repo a migration's FILENAME IS NOT ITS REVISION ID. The file
    `20260822_style_spec_optional_article.py` declares
    `revision = "20260822_style_article"`. Both `b7af20f6f3bb.py` and, later,
    `20260908_material_lot_used.py` were written with

        down_revision = "20260822_style_spec_optional_article"

    — the filename stem. No such revision exists, so the whole chain hanging
    off that line became unreachable from the root and `alembic upgrade head`
    died with

        Can't locate revision identified by '20260822_style_spec_optional_article'

    The second occurrence orphaned four BOM/procurement migrations written on a
    parallel branch, and only surfaced when the two branches were merged.

    A related trap, also already hit: `alembic_version.version_num` is
    VARCHAR(32). The id `20260911_order_style_pattern_fk_fix` is 36 characters,
    so alembic ran the DDL and then failed writing its own bookkeeping row,
    rolling the whole migration back.

THE INVARIANTS ASSERTED HERE
    1. Exactly one root (`down_revision = None`).
    2. Exactly one head (a revision nothing else points down to).
    3. No `down_revision` naming a revision that does not exist.
    4. No duplicate revision ids.
    5. No revision id longer than 32 characters.
    6. No branch points — every revision has at most one child, so the whole
       graph is a single line and every revision is reachable from the root.

    None of this needs a database or even alembic itself; it is a pure read of
    the files in alembic/versions.
"""
import pathlib
import re

import pytest

_VERSIONS = (pathlib.Path(__file__).resolve().parents[2] / "alembic" / "versions")

# alembic_version.version_num is VARCHAR(32); a longer id applies the DDL and
# then fails on its own bookkeeping row.
_MAX_REVISION_LEN = 32

_REVISION_RE = re.compile(r"^revision(?:\s*:[^=]*)?\s*=\s*[\"']([^\"']+)[\"']", re.M)
_DOWN_RE = re.compile(r"^down_revision(?:\s*:[^=]*)?\s*=\s*(.+?)\s*$", re.M)


def _load():
    """{revision: (filename, [down revisions])} for every migration on disk."""
    graph = {}
    for path in sorted(_VERSIONS.glob("*.py")):
        # several docstrings carry em-dashes; the Windows cp1252 default blows up
        src = path.read_text(encoding="utf-8", errors="replace")
        rev = _REVISION_RE.search(src)
        if rev is None:                      # not a migration module
            continue
        down = _DOWN_RE.search(src)
        assert down is not None, f"{path.name} declares a revision but no down_revision"
        graph.setdefault(rev.group(1), []).append(
            (path.name, re.findall(r"[\"']([^\"']+)[\"']", down.group(1)))
        )
    assert graph, f"no migrations found under {_VERSIONS}"
    return graph


@pytest.fixture(scope="module")
def graph():
    return _load()


def test_no_duplicate_revision_ids(graph):
    dupes = {rev: [f for f, _ in files] for rev, files in graph.items() if len(files) > 1}
    assert not dupes, f"revision id declared by more than one file: {dupes}"


def test_every_revision_id_fits_the_version_table(graph):
    too_long = {rev: len(rev) for rev in graph if len(rev) > _MAX_REVISION_LEN}
    assert not too_long, (
        f"revision ids longer than alembic_version.version_num's "
        f"VARCHAR({_MAX_REVISION_LEN}): {too_long}. The migration will run its "
        f"DDL and then fail writing its bookkeeping row."
    )


def test_no_down_revision_points_at_a_missing_revision(graph):
    """The filename-stem-instead-of-revision-id bug, caught at its source."""
    known = set(graph)
    dangling = [
        (fname, parent)
        for files in graph.values()
        for fname, parents in files
        for parent in parents
        if parent not in known
    ]
    hint = ""
    if any(p.removesuffix(".py") in {f.removesuffix(".py") for fs in graph.values() for f, _ in fs}
           for _, p in dangling):
        hint = ("  HINT: that string is a FILENAME, not a revision id — open the "
                "file and copy the value of its `revision = ...`.")
    assert not dangling, (
        "down_revision names a revision that does not exist: "
        + ", ".join(f"{f} -> '{p}'" for f, p in dangling) + hint
    )


def test_exactly_one_root(graph):
    roots = [rev for rev, files in graph.items()
             for _, parents in files if not parents]
    assert len(roots) == 1, f"expected exactly one root migration, found {roots}"


def test_exactly_one_head(graph):
    pointed_at = {p for files in graph.values() for _, parents in files for p in parents}
    heads = sorted(rev for rev in graph if rev not in pointed_at)
    assert len(heads) == 1, (
        f"expected exactly one head, found {heads}. Repoint the newer chain's "
        f"first migration onto the other head rather than running `alembic merge`."
    )


def test_the_chain_is_one_unbroken_line(graph):
    """No branch points, and every revision is reachable from the root."""
    children = {}
    for rev, files in graph.items():
        for _, parents in files:
            for parent in parents:
                children.setdefault(parent, []).append(rev)

    forks = {parent: kids for parent, kids in children.items() if len(kids) > 1}
    assert not forks, f"revision has more than one child, so the graph forks: {forks}"

    root = next(rev for rev, files in graph.items()
                for _, parents in files if not parents)
    seen, cur = [], root
    while cur is not None:
        assert cur not in seen[:-1], f"cycle in the revision graph at {cur}"
        seen.append(cur)
        kids = children.get(cur, [])
        cur = kids[0] if kids else None

    unreachable = sorted(set(graph) - set(seen))
    assert not unreachable, (
        f"{len(unreachable)} revision(s) unreachable from the root {root!r}: "
        f"{unreachable}"
    )
