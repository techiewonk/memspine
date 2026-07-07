"""LadybugDB graph store stub — the intended embedded DEFAULT once published (D-26).

ladybugdb (the kuzu community fork) is not on PyPI yet, so the ``[graph]``
extra is intentionally empty and this adapter cannot be constructed: v0.1
defaults to :class:`~memspine.services.graph.sqlite_adjacency.SQLiteAdjacencyGraph`
(zero-dep) with kuzu as the first-class alternative (``[kuzu]``). When the
pinned fork publishes, the real adapter replaces this stub and the extra gains
its requirement — the config value ``graph.provider: ladybug`` is already
reserved.
"""

from __future__ import annotations

from memspine.exceptions import MissingServiceError

__all__ = ["LadybugGraphStore"]


class LadybugGraphStore:
    def __init__(self) -> None:
        raise MissingServiceError("graph:ladybug", extra="graph")
