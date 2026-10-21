"""Focused domain boundaries; no Engine, model or backend is started."""

from __future__ import annotations

import json
import subprocess
import sys
from decimal import Decimal
from importlib.resources import files

import pytest
from pydantic import ValidationError

from memspine.semantics import PredicateSpec, SemanticRegistry, load_builtin_registry


@pytest.fixture
def registry() -> SemanticRegistry:
    return load_builtin_registry()


def test_selected_catalog_is_packaged_without_extraction_activation(registry):
    assert len(registry.predicates) == 53
    assert len(registry.properties) == 44
    assert "located_in" in registry.predicates
    assert "event_count" in registry.properties
    assert all(
        not p.extraction_rule_ids and not p.closed_world for p in registry.predicates.values()
    )
    assert all(
        p.transition_policy == "explicit_only"
        for p in registry.predicates.values()
        if p.semantics == "state"
    )
    with pytest.raises(TypeError):
        registry.predicates["made_up"] = registry.predicates["resides_in"]
    with pytest.raises(ValidationError):
        registry.predicates["resides_in"].name = "changed"


def test_registry_digest_is_order_independent_and_version_sensitive(registry):
    reverse = SemanticRegistry(
        reversed(list(registry.predicates.values())),
        reversed(list(registry.properties.values())),
        reversed(list(registry.roles.values())),
    )
    assert reverse.digest == registry.digest
    changed = registry.predicates["resides_in"].model_copy(update={"version": 2})
    with pytest.raises(ValueError, match="unsupported.*version"):
        SemanticRegistry([changed], [], [])


def test_duplicate_names_and_unknown_role_definitions_are_rejected(registry):
    spec = registry.predicates["resides_in"]
    with pytest.raises(ValueError, match="duplicate"):
        SemanticRegistry([spec, spec], [], [])
    changed = spec.model_copy(update={"role_names": ("invented",)})
    with pytest.raises(ValueError, match="unknown role"):
        SemanticRegistry([changed], [], [])


def test_strict_definition_rejects_unknown_fields(registry):
    raw = registry.predicates["resides_in"].model_dump(mode="json")
    raw["implicit_truth"] = True
    with pytest.raises(ValidationError):
        PredicateSpec.model_validate_json(json.dumps(raw))


@pytest.mark.parametrize("subject", ["agent", "group", "organisation", "object"])
def test_human_birthplace_cannot_be_applied_to_software_or_objects(registry, subject):
    with pytest.raises(ValueError, match="subject"):
        registry.validate_compatibility(
            "born_in", subject_type=subject, value_kind="entity", object_entity_type="place"
        )


def test_residence_and_object_location_keep_distinct_meanings(registry):
    registry.validate_compatibility(
        "resides_in", subject_type="person", value_kind="entity", object_entity_type="place"
    )
    registry.validate_compatibility(
        "located_in", subject_type="object", value_kind="entity", object_entity_type="place"
    )
    with pytest.raises(ValueError):
        registry.validate_compatibility(
            "resides_in", subject_type="object", value_kind="entity", object_entity_type="place"
        )
    with pytest.raises(ValueError, match="unresolved"):
        registry.validate_compatibility("resides_in", subject_type="person", value_kind="entity")


def test_transfer_roles_are_shared_not_separate_inverse_events(registry):
    giver = registry.predicates["gave"]
    receiver = registry.predicates["received"]
    assert giver.role_names == receiver.role_names == ("giver", "recipient", "item")
    registry.validate_compatibility(
        "gave",
        subject_type="person",
        value_kind="entity",
        object_entity_type="object",
        role_names=giver.role_names,
    )
    with pytest.raises(ValueError, match="role"):
        registry.validate_compatibility(
            "gave",
            subject_type="person",
            value_kind="entity",
            object_entity_type="object",
            role_names=("giver", "item"),
        )
    assert registry.roles["recipient"].entity_types == ("person", "agent", "group", "organisation")


def test_comparison_requires_preferred_and_alternative_roles(registry):
    registry.validate_compatibility(
        "prefers", subject_type="person", value_kind="text", role_names=("preferred", "alternative")
    )
    with pytest.raises(ValueError, match="role"):
        registry.validate_compatibility(
            "prefers", subject_type="person", value_kind="text", role_names=("preferred",)
        )


def test_property_category_prevents_distance_becoming_descriptive_attribute(registry):
    registry.validate_compatibility(
        "has_measurement",
        subject_type="person",
        value_kind="number",
        attribute_key="distance",
        unit="km",
        amount=Decimal("5"),
    )
    with pytest.raises(ValueError, match="property"):
        registry.validate_compatibility(
            "has_attribute",
            subject_type="person",
            value_kind="number",
            attribute_key="distance",
            unit="km",
            amount=Decimal("5"),
        )


@pytest.mark.parametrize("unit", [None, "kg", "guessed_km"])
def test_measurements_require_supported_explicit_units(registry, unit):
    with pytest.raises(ValueError, match="unit"):
        registry.validate_compatibility(
            "has_measurement",
            subject_type="person",
            value_kind="number",
            attribute_key="distance",
            unit=unit,
            amount=Decimal("5"),
        )


@pytest.mark.parametrize(
    "amount", [Decimal("-1"), Decimal("1.5"), Decimal("NaN"), Decimal("Infinity")]
)
def test_count_rejects_negative_fractional_or_nonfinite_values(registry, amount):
    with pytest.raises(ValueError):
        registry.validate_compatibility(
            "has_quantity",
            subject_type="person",
            value_kind="number",
            attribute_key="event_count",
            amount=amount,
            context={"count_scope": "recorded_trips"},
        )


def test_count_scope_is_required_but_does_not_establish_completeness(registry):
    registry.validate_compatibility(
        "has_quantity",
        subject_type="person",
        value_kind="number",
        attribute_key="event_count",
        amount=Decimal(3),
        context={"count_scope": "recorded_trips"},
    )
    with pytest.raises(ValueError, match="context"):
        registry.validate_compatibility(
            "has_quantity",
            subject_type="person",
            value_kind="number",
            attribute_key="event_count",
            amount=Decimal(3),
        )


def test_currency_and_capacity_require_explicit_context(registry):
    registry.validate_compatibility(
        "has_measurement",
        subject_type="object",
        value_kind="number",
        attribute_key="price",
        amount=Decimal("12.34"),
        currency="EUR",
    )
    with pytest.raises(ValueError, match="currency"):
        registry.validate_compatibility(
            "has_measurement",
            subject_type="object",
            value_kind="number",
            attribute_key="price",
            amount=Decimal("12.34"),
        )
    with pytest.raises(ValueError, match="capacity"):
        registry.validate_compatibility(
            "has_measurement",
            subject_type="object",
            value_kind="number",
            attribute_key="capacity",
            amount=Decimal(5),
            unit="GB",
            context={"capacity_kind": "volume"},
        )


def test_no_dynamic_unknown_predicate_or_property(registry):
    with pytest.raises(KeyError):
        registry.validate_compatibility(
            "knows_everything", subject_type="person", value_kind="text"
        )
    with pytest.raises(KeyError):
        registry.validate_compatibility(
            "has_attribute",
            subject_type="person",
            value_kind="text",
            attribute_key="religion_guess",
        )


def test_package_import_and_catalog_need_no_models_providers_or_docs():
    code = """
import sys
from memspine.semantics import load_builtin_registry
registry = load_builtin_registry()
assert len(registry.predicates) == 53
assert not any(name.startswith(('torch', 'transformers', 'memspine.providers', 'memspine.clients', 'memspine.services')) for name in sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True, timeout=20)
    assert files("memspine.semantics").joinpath("catalog.json").is_file()
