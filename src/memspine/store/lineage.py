"""The lineage table (F-16a, M-09): SQL access and a LIB-09 ``LineageGraph`` over it.

The table holds one row per (child, parent, role) edge. It is written only through the derived-write
door (``derive/write.py``) when ``write.lineage_table`` is on. The erasure planner's
``LineageGraph`` protocol is synchronous, so :class:`SqlLineageGraph` is a snapshot: ``await
SqlLineageGraph.load(engine, roots)`` reads the closure of the roots (ancestors and descendants) in
a few queries, and ``parents`` / ``children`` then answer from memory. It satisfies the protocol
structurally (store and trust are sibling layers, so nothing is imported from trust).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import and_, delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.schemas.durable.common import Fingerprint
from memspine.schemas.durable.evidence import Span
from memspine.schemas.durable.lineage import LineageEdge
from memspine.services.storage.sqlite.schema import lineage

__all__ = [
    "SqlLineageGraph",
    "add_edges",
    "children_of",
    "delete_edges",
    "load_edges",
    "parents_of",
]

_CHUNK = 400  # stay under SQLite's bound-parameter limit


def _row_to_edge(row: Any) -> LineageEdge:
    return LineageEdge(
        child=row.child_id,
        parent=row.parent_id,
        parent_version=row.source_version,
        role=row.role,
        span=Span.model_validate_json(row.span) if row.span else None,
        source_family=row.source_family,
        derivation=Fingerprint.model_validate_json(row.derivation) if row.derivation else None,
    )


def _sorted(rows: Iterable[Any]) -> list[LineageEdge]:
    return [_row_to_edge(r) for r in sorted(rows, key=lambda r: r.created_seq)]


async def add_edges(
    engine: AsyncEngine, edges: Sequence[LineageEdge], namespace: str | None = None
) -> int:
    """Insert edges; an existing (child, parent, role) edge is left as it is. Returns rows added."""
    if not edges:
        return 0
    insert = pg_insert if engine.dialect.name == "postgresql" else sqlite_insert
    added = 0
    async with engine.begin() as conn:
        for e in edges:
            stmt = (
                insert(lineage)
                .values(
                    child_id=e.child,
                    parent_id=e.parent,
                    role=e.role,
                    namespace=namespace,
                    span=e.span.model_dump_json() if e.span else None,
                    source_version=e.parent_version,
                    source_family=e.source_family,
                    derivation=e.derivation.model_dump_json() if e.derivation else None,
                )
                .on_conflict_do_nothing(index_elements=["child_id", "parent_id", "role"])
            )
            added += int((await conn.execute(stmt)).rowcount or 0)
    return added


async def delete_edges(engine: AsyncEngine, edges: Sequence[LineageEdge]) -> int:
    """Remove the given (child, parent, role) edges (erasure, T5-04). Returns rows removed."""
    removed = 0
    async with engine.begin() as conn:
        for e in edges:
            stmt = delete(lineage).where(
                and_(
                    lineage.c.child_id == e.child,
                    lineage.c.parent_id == e.parent,
                    lineage.c.role == e.role,
                )
            )
            removed += int((await conn.execute(stmt)).rowcount or 0)
    return removed


async def _select(
    engine: AsyncEngine, column: Any, ids: Sequence[str], roles: Sequence[str] | None
) -> list[Any]:
    rows: list[Any] = []
    async with engine.connect() as conn:
        for i in range(0, len(ids), _CHUNK):
            stmt = select(lineage).where(column.in_(ids[i : i + _CHUNK]))
            if roles is not None:
                stmt = stmt.where(lineage.c.role.in_(list(roles)))
            rows.extend((await conn.execute(stmt)).all())
    return rows


async def parents_of(
    engine: AsyncEngine, child_ids: Sequence[str], roles: Sequence[str] | None = None
) -> dict[str, list[LineageEdge]]:
    """Edges whose child is each id (child id -> edges in insertion order)."""
    out: dict[str, list[LineageEdge]] = {}
    for e in _sorted(await _select(engine, lineage.c.child_id, list(child_ids), roles)):
        out.setdefault(e.child, []).append(e)
    return out


async def children_of(
    engine: AsyncEngine, parent_ids: Sequence[str], roles: Sequence[str] | None = None
) -> dict[str, list[LineageEdge]]:
    """Edges whose parent is each id (parent id -> edges in insertion order)."""
    out: dict[str, list[LineageEdge]] = {}
    for e in _sorted(await _select(engine, lineage.c.parent_id, list(parent_ids), roles)):
        out.setdefault(e.parent, []).append(e)
    return out


async def load_edges(
    engine: AsyncEngine,
    roots: Iterable[str],
    *,
    up: bool = True,
    down: bool = True,
    max_depth: int = 32,
) -> list[LineageEdge]:
    """Edges reachable from ``roots`` toward ancestors (``up``) and/or descendants (``down``)."""
    seen: dict[tuple[str, str, str], LineageEdge] = {}
    start = set(roots)
    frontier_up, frontier_down = set(start), set(start)
    visited_up: set[str] = set()
    visited_down: set[str] = set()
    for _ in range(max_depth):
        nxt_up: set[str] = set()
        nxt_down: set[str] = set()
        if up and frontier_up:
            todo = sorted(frontier_up - visited_up)
            visited_up.update(todo)
            for edges in (await parents_of(engine, todo)).values():
                for e in edges:
                    seen[(e.child, e.parent, e.role)] = e
                    nxt_up.add(e.parent)
        if down and frontier_down:
            todo = sorted(frontier_down - visited_down)
            visited_down.update(todo)
            for edges in (await children_of(engine, todo)).values():
                for e in edges:
                    seen[(e.child, e.parent, e.role)] = e
                    nxt_down.add(e.child)
        frontier_up, frontier_down = nxt_up - visited_up, nxt_down - visited_down
        if not frontier_up and not frontier_down:
            break
    return [seen[k] for k in sorted(seen)]


class SqlLineageGraph:
    """A snapshot of the lineage table satisfying the LIB-09 ``LineageGraph`` protocol."""

    def __init__(self, edges: Iterable[LineageEdge] = ()) -> None:
        self._by_child: dict[str, dict[tuple[str, str], LineageEdge]] = {}
        self._by_parent: dict[str, dict[tuple[str, str], LineageEdge]] = {}
        for e in edges:
            self._by_child.setdefault(e.child, {})[(e.parent, e.role)] = e
            self._by_parent.setdefault(e.parent, {})[(e.child, e.role)] = e

    @classmethod
    async def load(
        cls, engine: AsyncEngine, roots: Iterable[str], *, max_depth: int = 32
    ) -> SqlLineageGraph:
        return cls(await load_edges(engine, roots, max_depth=max_depth))

    def parents(self, record_id: str) -> list[LineageEdge]:
        d = self._by_child.get(record_id, {})
        return [d[k] for k in sorted(d)]

    def children(self, record_id: str) -> list[LineageEdge]:
        d = self._by_parent.get(record_id, {})
        return [d[k] for k in sorted(d)]
