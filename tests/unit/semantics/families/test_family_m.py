"""Family M: travel and trips (RM-63), domain ``travel``.

M adds only the ``transport_mode`` and ``participation_mode`` qualifiers on the core
visit/stay/attendance predicates, so its facts are ``visited`` claims."""

from __future__ import annotations

import pytest

from families._harness import Claim, Door, Fact, FamilyContract, text
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers, QualifierFilter


class TestFamilyM(FamilyContract):
    FAMILY = "travel_modes"
    DOMAIN = "travel"
    ACTIVE = True
    POSITIVE = (
        Claim(
            "visited",
            object_entity_type="place",
            qualifiers={"transport_mode": text("train"), "participation_mode": text("in_person")},
        ),
        Claim(
            "attended",
            object_entity_type="event",
            qualifiers={"participation_mode": text("remote")},
        ),
        Claim(
            "temporarily_present_in",
            object_entity_type="place",
            qualifiers={"transport_mode": text("car")},
        ),
    )
    NEGATIVE = (
        (
            Claim(
                "attended",
                object_entity_type="event",
                qualifiers={"participation_mode": text("virtual")},
            ),
            "allowed values",
        ),
        (
            Claim(
                "participated_in",
                object_entity_type="event",
                qualifiers={"transport_mode": text("bus")},
            ),
            "not registered",
        ),
        (Claim("visited", object_entity_type="person"), "object entity type"),
    )
    DOOR = Door("I visited Porto.", "visited", "Porto", "entity", "place")
    DOOR_NEGATIVE = Door("I visited Bram.", "visited", "Bram", "entity", "person")
    FACT = Fact("Ilse", "visited", "Porto", tags=("participation_mode:in_person",), kind="event")

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "RM-63 M stays proposed: participation_mode is a declared edge qualifier with a "
            "graph filter, but no path carries a claim's mode onto the compiled edge (the "
            "edge compiler never fills it; transport_mode has no edge qualifier at all)"
        ),
    )
    def test_positive(self, registry: SemanticRegistry) -> None:
        super().test_positive(registry)

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        """``participation_mode`` is a declared edge qualifier (RM-41 P2) with its own graph
        filter, so an M claim's mode must reach the compiled edge."""
        mode = QualifierFilter(participation_modes=frozenset({"in_person"}))
        assert any(mode.matches(q) for q in qualifiers), "no edge carries participation_mode"
