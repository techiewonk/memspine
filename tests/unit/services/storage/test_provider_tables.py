"""``memspine.services.storage.provider_tables``: provider management tables."""

from __future__ import annotations

from memspine.services.storage.provider_metadata import DEPLOYMENT_METADATA
from memspine.services.storage.provider_tables import provider_metadata
from memspine.services.storage.sqlite.schema import metadata as substrate_metadata

PROVIDER_TABLES = {
    "provider_change_plans": ["plan_id"],
    "provider_change_operations": ["operation_id"],
    "provider_change_fences": ["deployment_id"],
    "provider_cleanup_fence_recoveries": ["recovery_id"],
    "provider_projection_generations": ["deployment_id", "logical_service_id", "generation_id"],
    "provider_projection_switches": ["switch_id"],
}


def test_provider_tables_share_the_substrate_metadata() -> None:
    assert provider_metadata is substrate_metadata
    assert provider_metadata is DEPLOYMENT_METADATA.metadata


def test_every_provider_table_is_registered_with_its_primary_key() -> None:
    for name, key in PROVIDER_TABLES.items():
        table = provider_metadata.tables[name]
        assert [c.name for c in table.primary_key.columns] == key


def test_every_provider_table_requires_a_payload() -> None:
    for name in set(PROVIDER_TABLES) - {"provider_change_fences"}:
        assert not provider_metadata.tables[name].c.payload.nullable


def test_one_operation_per_plan_and_one_recovery_per_proof() -> None:
    ops = provider_metadata.tables["provider_change_operations"]
    assert ops.c.plan_id.unique
    recoveries = provider_metadata.tables["provider_cleanup_fence_recoveries"]
    assert recoveries.c.proof_id.unique


def test_deployment_scoped_tables_index_the_deployment() -> None:
    for name in (
        "provider_change_plans",
        "provider_change_operations",
        "provider_cleanup_fence_recoveries",
        "provider_projection_switches",
    ):
        assert provider_metadata.tables[name].c.deployment_id.index
