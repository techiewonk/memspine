from __future__ import annotations

from memspine.providers.catalog import catalog, catalog_dicts


def test_catalog_is_derived_from_registered_descriptors() -> None:
    entries = catalog(category="embedding")
    assert entries
    assert {entry.name for entry in entries} >= {"hash", "fastembed"}
    assert all(entry.category == "embedding" for entry in entries)
    assert all(entry.contract_version == 1 for entry in entries)


def test_catalog_serialization_is_allowlisted_and_contains_no_settings() -> None:
    rows = catalog_dicts(category="embedding")
    assert rows
    forbidden = {"settings", "api_key", "password", "raw_memory"}
    for row in rows:
        assert forbidden.isdisjoint(row)
        assert "settings" not in str(row)


def test_catalog_can_include_selection_context_without_mutating_descriptors() -> None:
    entries = catalog(
        category="embedding",
        configured={"embedding": "hash"},
        resolved={"embedding": "hash"},
        selection_reasons={"embedding": "profile default"},
    )
    hash_entry = next(entry for entry in entries if entry.name == "hash")
    assert hash_entry.configured_provider == "hash"
    assert hash_entry.resolved_provider == "hash"
    assert hash_entry.selection_reason == "profile default"
