"""#87: deterministic order for equal-score retrieval hits.

A vector or BM25 store returns its top ``k`` by score; among equal scores the
order is the store's own (LanceDB's scan order, Tantivy's segment layout, which
background merges change). When the ``k`` cut falls inside a run of equal scores,
which records get in can then differ between two runs on the same input. This
module re-fetches until the run is whole and orders every tied run by a caller
supplied key (the engine uses write order, then content), so the leg is a
function of the stored records alone.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from memspine.config import constants

__all__ = ["settle_ties"]


class _Hit(Protocol):
    @property
    def record_id(self) -> str: ...

    @property
    def score(self) -> float: ...


def _runs(hits: Sequence[_Hit]) -> list[tuple[int, int]]:
    """``[start, end)`` spans of two or more consecutive equal scores."""
    spans: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(hits) + 1):
        if i == len(hits) or hits[i].score != hits[start].score:
            if i - start > 1:
                spans.append((start, i))
            start = i
    return spans


async def settle_ties[H: _Hit](
    fetch: Callable[[int], Awaitable[list[H]]],
    k: int,
    key_of: Callable[[list[str]], Awaitable[dict[str, Any]]],
    max_factor: int = constants.LEG_TIE_FETCH_MAX_FACTOR,
) -> list[H]:
    """The top ``k`` of ``fetch`` with every run of equal scores in content order.

    ``fetch(n)`` returns at most ``n`` hits, best first. It is asked for ``k + 1``
    so a tie across the cut is visible; while the last fetched hit still ties the
    ``k``-th, the request doubles, up to ``max_factor x k``. ``key_of(ids)`` maps
    record ids to sortable keys; an id it leaves out sorts after the others (by
    id, which only matters for records the caller's gates drop anyway).

    With no ties this costs one extra row per fetch and no key lookups."""
    if k < 1:
        return []
    want = k + 1
    while True:
        hits = await fetch(want)
        if len(hits) < want or hits[k - 1].score != hits[-1].score:
            break
        if want >= k * max_factor:
            break  # bound reached: past it, the store's tie order stands
        want = min(want * 2, k * max_factor)
    runs = [(a, b) for a, b in _runs(hits) if a < k]
    if not runs:
        return hits[:k]
    ids = [hits[i].record_id for a, b in runs for i in range(a, b)]
    keys = await key_of(ids)
    settled = list(hits)

    def order(hit: H) -> tuple[int, Any]:
        key = keys.get(hit.record_id)
        return (0, key) if key is not None else (1, hit.record_id)

    for a, b in runs:
        settled[a:b] = sorted(settled[a:b], key=order)
    return settled[:k]
