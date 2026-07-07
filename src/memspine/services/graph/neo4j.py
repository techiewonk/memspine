"""Neo4j graph store stub — the production swap-in slot (structure plan tree).

The server-backed adapter (and its ``clients/neo4j.py`` connection client) is
out of scope for v0.1; constructing this always raises the D-10 error naming
``[neo4j]`` so the config value ``graph.provider: neo4j`` fails actionably
instead of half-working.
"""

from __future__ import annotations

from memspine.exceptions import MissingServiceError

__all__ = ["Neo4jGraphStore"]


class Neo4jGraphStore:
    def __init__(self) -> None:
        raise MissingServiceError("graph:neo4j", extra="neo4j")
