"""Family H: communication acts (RM-62), domain ``social``.

H adds only the ``speech_act`` qualifier on the core ``communicated_with`` predicate, so
its facts are ``communicated_with`` claims."""

from __future__ import annotations

from families._harness import Claim, Door, Fact, FamilyContract, text
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers

ACTS = ("ask", "answer", "inform", "request", "agree", "decline", "apologise", "thank")


class TestFamilyH(FamilyContract):
    FAMILY = "communication_acts"
    DOMAIN = "social"
    ACTIVE = True
    POSITIVE = tuple(
        Claim("communicated_with", object_entity_type="person", qualifiers={"speech_act": text(a)})
        for a in ACTS
    )
    NEGATIVE = (
        (
            Claim(
                "communicated_with",
                object_entity_type="person",
                qualifiers={"speech_act": text("gossip")},
            ),
            "allowed values",
        ),
        (
            Claim("visited", object_entity_type="place", qualifiers={"speech_act": text("ask")}),
            "not registered",
        ),
        (Claim("communicated_with", object_entity_type="place"), "object entity type"),
    )
    DOOR = Door("I talked with Bram.", "communicated_with", "Bram", "entity", "person")
    DOOR_NEGATIVE = Door(
        "I talked with Utrecht.", "communicated_with", "Utrecht", "entity", "place"
    )
    FACT = Fact("Ilse", "communicated_with", "Bram", kind="event")

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        assert registry.properties["speech_act"].allowed_values == ACTS
