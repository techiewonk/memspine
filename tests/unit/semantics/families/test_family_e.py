"""Family E: feelings, beliefs and values (RM-62), domain ``personal``."""

from __future__ import annotations

from families._harness import Claim, Door, Fact, FamilyContract, role, text
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers


class TestFamilyE(FamilyContract):
    FAMILY = "feelings_beliefs"
    DOMAIN = "personal"
    ACTIVE = True
    POSITIVE = (
        Claim(
            "feels",
            object_entity_type="concept",
            roles=(role("cause", "text"),),
            qualifiers={"intensity": text("strong")},
        ),
        Claim(
            "believes",
            subject_type="organisation",
            value_kind="text",
            roles=(role("topic", "text"),),
        ),
        Claim("values", value_kind="text", source_origin="document"),
        Claim("worried_about", object_entity_type="person", qualifiers={"intensity": text("mild")}),
    )
    NEGATIVE = (
        (Claim("believes", object_entity_type="concept"), "incompatible subject or value kind"),
        (Claim("feels", subject_type="organisation", value_kind="text"), "incompatible subject"),
        (
            Claim("values", value_kind="text", qualifiers={"intensity": text("high")}),
            "not registered",
        ),
        (Claim("feels", value_kind="text", source_origin="tool_output"), "source origin"),
    )
    DOOR = Door("I feel anxious about the move.", "feels", "anxious", "text")
    DOOR_NEGATIVE = Door("I believe in Acme.", "believes", "Acme", "entity", "organisation")
    FACT = Fact("Ilse", "feels", "Anxious")

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        # attributed: held by the subject, never verified psychology or world truth
        for name in ("feels", "believes", "values", "worried_about"):
            assert registry.predicates[name].statement_kind == "attributed"
