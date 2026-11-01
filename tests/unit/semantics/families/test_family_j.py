"""Family J: possession lifecycle and opinions (RM-62), domain ``personal``."""

from __future__ import annotations

import pytest

from families._harness import Claim, Door, Fact, FamilyContract, number, role, text
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers


class TestFamilyJ(FamilyContract):
    FAMILY = "possession_lifecycle"
    DOMAIN = "personal"
    ACTIVE = False
    POSITIVE = (
        Claim("acquired", object_entity_type="object"),
        Claim("disposed_of", object_entity_type="work"),
        Claim("lost", value_kind="text"),
        Claim("repaired", object_entity_type="software"),
        Claim(
            "returned",
            object_entity_type="document",
            roles=(role("recipient", "entity", "person"),),
        ),
        Claim(
            "rated",
            object_entity_type="place",
            qualifiers={"rating": number("4"), "sentiment": text("positive")},
            source_origin="external_catalogue",
        ),
    )
    NEGATIVE = (
        (
            Claim("rated", object_entity_type="work", qualifiers={"sentiment": text("great")}),
            "allowed values",
        ),
        (Claim("lost", object_entity_type="place"), "object entity type"),
        (
            Claim(
                "returned", object_entity_type="object", roles=(role("buyer", "entity", "person"),)
            ),
            "missing or unsupported predicate role",
        ),
        (
            Claim("acquired", object_entity_type="object", source_origin="form_input"),
            "source origin",
        ),
    )
    DOOR = Door("I lost my umbrella.", "lost", "umbrella", "entity", "object")
    DOOR_NEGATIVE = Door("I lost Utrecht.", "lost", "Utrecht", "entity", "place")
    FACT = Fact("Ilse", "lost", "Umbrella", kind="event")

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "RM-62 J stays proposed: acquired, disposed_of, lost and returned open/close "
            "owns and accept species objects, but owns cannot hold a species, so the "
            "explicit transition has no well-typed target (owner decision: add species to "
            "owns or drop it from the J events)"
        ),
    )
    def test_positive(self, registry: SemanticRegistry) -> None:
        super().test_positive(registry)

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        """An event that opens or closes a state only through an explicit transition must
        describe a value that state can hold (otherwise the transition has no target)."""
        for spec in registry.predicates.values():
            if registry.family_of("predicate", spec.name) != self.FAMILY or not spec.affects_state:
                continue
            state = registry.predicates[spec.affects_state]
            assert set(spec.value_kinds) <= set(state.value_kinds), spec.name
            assert set(spec.object_entity_types) <= set(state.object_entity_types), (
                f"{spec.name} accepts {sorted(set(spec.object_entity_types) - set(state.object_entity_types))}"
                f" objects that {state.name} cannot hold"
            )
