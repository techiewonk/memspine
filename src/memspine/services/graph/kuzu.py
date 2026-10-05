"""Kùzu graph store — DEPRECATED alias of the LadybugDB adapter, ``[kuzu]``.

Kùzu was archived upstream on 2025-10-10; LadybugDB is its maintained fork and
the graph engine for graph features (ADR-034). The two share one Cypher dialect,
so this store is the LadybugDB adapter over a :class:`KuzuClient` connection.
``graph.provider: kuzu`` keeps working for one release with a
``DeprecationWarning``; switch to ``ladybug`` (``pip install memspine[graph]``)
and run ``engine.rebuild()`` (the graph is a projection; Kùzu files are not
readable by LadybugDB).
"""

from __future__ import annotations

import warnings

from memspine.clients.kuzu import KuzuClient
from memspine.services.graph.ladybug import LadybugGraphStore

__all__ = ["KUZU_DEPRECATION", "KuzuGraphStore"]

KUZU_DEPRECATION = (
    "graph.provider='kuzu' is deprecated and will be removed in the next release: Kùzu "
    "was archived on 2025-10-10. Use graph.provider='ladybug' (pip install "
    "memspine[graph]) and run engine.rebuild() to re-project the graph (ADR-034)."
)


class KuzuGraphStore(LadybugGraphStore):
    """Deprecated: the LadybugDB adapter over an embedded Kùzu connection."""

    def __init__(self, client: KuzuClient) -> None:
        warnings.warn(KUZU_DEPRECATION, DeprecationWarning, stacklevel=2)
        super().__init__(client)
