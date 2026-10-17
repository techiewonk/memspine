from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from memspine.providers.contracts import BuildResult, ProviderRef, ResourceRef, StorageBuildResult
from memspine.providers.settings import EmptySettings


def test_provider_and_resource_refs_are_immutable_and_validated() -> None:
    provider = ProviderRef("graph", "neo4j")
    resource = ResourceRef("neo4j-driver", "owned")

    assert provider.contract_version == 1
    with pytest.raises(FrozenInstanceError):
        provider.name = "other"  # type: ignore[misc]
    with pytest.raises(ValueError, match="contract_version"):
        ProviderRef("graph", "neo4j", 0)
    with pytest.raises(ValueError, match="resource_id"):
        ResourceRef(" ", "borrowed")
    assert resource.ownership == "owned"


def test_build_results_are_typed_with_sql_as_a_first_class_field() -> None:
    provider = ProviderRef("storage", "sqlite")
    resource = ResourceRef("sql-engine", "owned")
    service = object()
    result = BuildResult(service, provider, (resource,), frozenset({"transactions"}))
    sql = object()
    storage_result = StorageBuildResult(
        service, provider, (resource,), frozenset({"transactions"}), sql
    )

    assert result.service is service
    assert storage_result.sql is sql


def test_provider_settings_can_be_validated_before_build() -> None:
    settings = EmptySettings.model_validate({})
    assert settings.model_dump() == {}
    with pytest.raises(ValidationError):
        EmptySettings.model_validate({"unrecognized": True})
