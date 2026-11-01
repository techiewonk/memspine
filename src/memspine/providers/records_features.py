"""Features a ``storage.records`` provider must offer (RM-112), checked before anything opens.

The records port (``services/storage/records_port.py``) is everything the engine needs from the
authoritative store. A few features go further and keep their state in SQL tables of the records
database: the outbox job book, the RM-111 WP5 side tables (sleep leases and watermarks, the cost
ledger, dead letters), the provider-change switch tables, and the providers that live inside the
SQL database (``sql_adjacency``, pgvector). A records provider that declares no ``sql``
capability (MongoDB) cannot serve them, so a configuration that asks for one is refused here,
naming every gap at once, instead of silently falling back to process memory or failing at the
first write. :func:`validate_records_features` runs in ``check_capabilities`` (container
bootstrap), after the locality rules.

RM-137: the intake ledger (accepted intents, message identities and the raw-to-derivation
discovery that feeds the outbox) is a capability port of its own
(``services/storage/intake_port.py``), declared as ``intake_ledger`` on the descriptor. A
records provider without it cannot run background derivation, so the configurations that need
it are refused here too, naming the capability.
"""

from __future__ import annotations

from collections.abc import Collection

from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.providers.registry import (
    ProviderDescriptor,
    get_registry,
    requirements_available,
)
from memspine.services.storage.intake_port import INTAKE_LEDGER_CAPABILITY

__all__ = [
    "RecordsFeatureError",
    "intake_ledger_gaps",
    "records_has_capability",
    "records_has_sql",
    "sql_feature_gaps",
    "validate_records_features",
]


class RecordsFeatureError(ConfigError):
    """The selected records provider cannot serve a configured feature."""


def records_has_capability(config: MemspineConfig, capability: str) -> bool:
    """Whether the selected records provider declares ``capability`` (True when unknown: the
    unknown name is reported by the provider build with the registry's valid list)."""
    descriptor = get_registry("storage_backends").find(config.storage.records.provider)
    if not isinstance(descriptor, ProviderDescriptor):
        return True
    return capability in descriptor.declared_capabilities


def records_has_sql(config: MemspineConfig) -> bool:
    """Whether the selected records provider declares SQL access."""
    return records_has_capability(config, "sql")


def _ladybug_installed() -> bool:
    descriptor = get_registry("graph_stores").find("ladybug")
    if not isinstance(descriptor, ProviderDescriptor):
        return False
    return requirements_available(descriptor)[0]


def sql_feature_gaps(config: MemspineConfig, *, enabled: Collection[str]) -> list[str]:
    """Every configured feature that needs a SQL records store, as ``key: reason`` lines."""
    gaps: list[str] = []
    if config.workers.outbox:
        gaps.append("workers.outbox: the outbox job book is a SQL table of the records database")
    if config.offline.sleep:
        gaps.append(
            "offline.sleep: the sleep leases, cycle watermarks and triggers are SQL side "
            "tables (RM-111 WP5); the blocking sleep() works on any records store"
        )
    if config.economy.ledger and config.economy.ledger_persist:
        gaps.append("economy.ledger_persist: the cost ledger is a SQL side table (RM-111 WP5)")
    if config.write.window_extract in ("facts", "facts_graph", "all"):
        gaps.append("write.window_extract: its dead-letter list is a SQL side table (RM-111 WP5)")
    if config.runtime.service_switches.projections.cleanup_interval_s is not None:
        gaps.append(
            "runtime.service_switches.projections: controlled projection switches keep their "
            "manifests in SQL tables of the records database"
        )
    if config.storage.vector.provider == "pgvector":
        gaps.append(
            "storage.vector.provider 'pgvector': it stores the vectors in the Postgres records "
            "database, so it needs storage.records.provider postgres (use lancedb or weaviate)"
        )
    if "associative" in enabled:
        graph = config.storage.graph.provider
        if graph == "sql_adjacency":
            gaps.append(
                "storage.graph.provider 'sql_adjacency': it keeps the graph in the SQL records "
                "database (use ladybug or neo4j)"
            )
        elif graph == "auto" and not _ladybug_installed():
            gaps.append(
                "storage.graph.provider 'auto': without a SQL records store it can only resolve "
                "to ladybug, which is not installed (pip install memspine[graph]), and never to "
                "sql_adjacency; install the extra or choose storage.graph.provider ladybug or neo4j"
            )
    return gaps


def intake_ledger_gaps(config: MemspineConfig) -> list[str]:
    """Every configured feature that needs the ``intake_ledger`` capability (RM-137)."""
    gaps: list[str] = []
    if config.write.window_extract in ("facts", "facts_graph", "all"):
        gaps.append(
            "write.window_extract: background derivation discovers committed intake intents "
            f"through the {INTAKE_LEDGER_CAPABILITY!r} capability of storage.records"
        )
    return gaps


def validate_records_features(config: MemspineConfig, *, enabled: Collection[str]) -> None:
    """Refuse, before any provider opens, a configuration whose features the records provider
    cannot serve (only a records provider without SQL or the intake ledger has gaps today)."""
    has_sql = records_has_sql(config)
    gaps = [] if has_sql else sql_feature_gaps(config, enabled=enabled)
    if not records_has_capability(config, INTAKE_LEDGER_CAPABILITY):
        gaps += intake_ledger_gaps(config)
    if gaps:
        provider = config.storage.records.provider
        reason = (
            f"does not declare the {INTAKE_LEDGER_CAPABILITY!r} capability"
            if has_sql
            else "has no SQL access"
        )
        raise RecordsFeatureError(
            f"storage.records.provider {provider!r} {reason}, so these configured "
            "features cannot run on it:\n  - " + "\n  - ".join(gaps)
        )
