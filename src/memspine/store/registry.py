"""Typed registries for the storage-side extension kinds (MOD-02): vector stores, lexical
indexes and graph stores.

A built-in is registered with ``None``: the engine builds it natively (it needs engine state such
as the SQLite client and the derived-projection base path). An enabled plug-in registers a factory
``factory(config, base_path, in_memory) -> store``; the engine calls it for any non-built-in name.
The names are the config values (``vector.backend``, ``read.lexical_provider``,
``graph.provider``) and so existing YAML keeps working.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memspine.plugins import Registry

__all__ = ["GRAPH_STORES", "LEXICAL_INDEXES", "VECTOR_STORES", "StoreFactory"]

#: ``factory(config, base_path, in_memory)``: what a plug-in store provides.
StoreFactory = Callable[[Any, str, bool], Any]

VECTOR_STORES: Registry[StoreFactory | None] = Registry("vector_stores")
LEXICAL_INDEXES: Registry[StoreFactory | None] = Registry("lexical_indexes")
GRAPH_STORES: Registry[StoreFactory | None] = Registry("graph_stores")

VECTOR_STORES.register("lance", None)

LEXICAL_INDEXES.register("tantivy", None)

GRAPH_STORES.register("sqlite_adjacency", None)
GRAPH_STORES.register("ladybug", None)
GRAPH_STORES.register("neo4j", None)  # stub: always raises MissingServiceError
