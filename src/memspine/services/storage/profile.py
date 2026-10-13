"""Storage profiles (F-16d, M-04 C): what a profile provides, and the two that ship.

A profile is the answer to "which stores back the event log, the record read model, the lineage
table, the outbox, vectors, lexical search and the graph, and which migration tree builds the SQL
ones". Both profiles share one event schema (the SQL tables are the same, the Alembic trees are
kept in parity by a test), so a store can move between profiles by replaying its event log.

``embedded``  SQLite file + LanceDB + Tantivy + (Ladybug | SQL adjacency). Today's default.
``server``    Postgres + pgvector when installed (else the LanceDB vector service) + Tantivy
              (Postgres full-text search is a later step) + SQL adjacency on the same Postgres
              (Neo4j stays an optional later graph, fed through the outbox, M-04 note).

This module only describes and resolves. It builds nothing, so importing it is free.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from memspine.observability import tap

if TYPE_CHECKING:
    from memspine.config.schema.storage import StorageConfig

__all__ = [
    "EMBEDDED",
    "SERVER",
    "ProfileName",
    "StorageProfile",
    "profile_by_name",
    "resolve_profile",
]

ProfileName = Literal["embedded", "server"]

_STORAGE = Path(__file__).parent


@dataclass(frozen=True)
class StorageProfile:
    """The stores one profile provides. Names are the config/provider names, not instances."""

    name: ProfileName
    #: ``storage.backend`` value that opens this profile's SQL database.
    sql_backend: str
    record_store: str
    event_log: str
    lineage: str
    outbox: str
    vector: str
    lexical: str
    graph: str
    #: Directory of the Alembic tree that builds the SQL tables of this profile.
    migration_tree: Path
    #: Revision id of the tree's single head is read from the tree, not duplicated here.
    notes: tuple[str, ...] = ()

    def provides(self) -> tuple[str, ...]:
        return ("record_store", "event_log", "lineage", "outbox", "vector", "lexical", "graph")

    def as_dict(self) -> dict[str, str]:
        return {k: getattr(self, k) for k in self.provides()} | {
            "name": self.name,
            "sql_backend": self.sql_backend,
        }


EMBEDDED = StorageProfile(
    name="embedded",
    sql_backend="sqlite",
    record_store="sqlite",
    event_log="sqlite",
    lineage="sqlite",
    outbox="sqlite",
    vector="lancedb",
    lexical="tantivy",
    graph="auto(ladybug|sqlite_adjacency)",
    migration_tree=_STORAGE / "sqlite" / "alembic",
)

SERVER = StorageProfile(
    name="server",
    sql_backend="postgres",
    record_store="postgres",
    event_log="postgres",
    lineage="postgres",
    outbox="postgres",
    vector="pgvector|lancedb",
    lexical="tantivy",
    graph="sqlite_adjacency(postgres)",
    migration_tree=_STORAGE / "postgres" / "alembic",
    notes=(
        "vector: pgvector is used when the package is installed, otherwise the LanceDB service",
        "lexical: Postgres full-text search is not wired yet; Tantivy stays the lexical leg",
    ),
)

_BY_NAME: dict[str, StorageProfile] = {EMBEDDED.name: EMBEDDED, SERVER.name: SERVER}


def profile_by_name(name: str) -> StorageProfile:
    try:
        return _BY_NAME[name]
    except KeyError:
        raise ValueError(f"unknown storage profile {name!r} (valid: embedded, server)") from None


def pgvector_available() -> bool:
    return importlib.util.find_spec("pgvector") is not None


def resolve_profile(storage: StorageConfig) -> StorageProfile:
    """The profile a storage config selects. ``storage.profile`` wins; a legacy
    ``backend: postgres`` with the default profile resolves to ``server``'s stores too."""
    chosen = storage.profile
    profile = _BY_NAME[chosen]
    if chosen == "embedded" and storage.backend == "postgres":
        profile = SERVER
    if tap.enabled():
        tap.event(
            "store.profile.resolved",
            profile=profile.name,
            requested=chosen,
            sql_backend=profile.sql_backend,
            pgvector=pgvector_available() if profile is SERVER else False,
        )
    return profile
