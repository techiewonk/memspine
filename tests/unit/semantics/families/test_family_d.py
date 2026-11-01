"""Family D: health and wellbeing (RM-62), domain ``health``."""

from __future__ import annotations

from families._harness import Claim, Door, Fact, FamilyContract, role, text
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers


class TestFamilyD(FamilyContract):
    FAMILY = "health_wellbeing"
    DOMAIN = "health"
    ACTIVE = True
    POSITIVE = (
        Claim(
            "has_condition",
            value_kind="entity",
            object_entity_type="concept",
            qualifiers={"severity": text("mild"), "diagnosis_status": text("diagnosed")},
        ),
        Claim("allergic_to", object_entity_type="species", qualifiers={"severity": text("severe")}),
        Claim("intolerant_of", value_kind="text"),
        Claim("takes_medication", object_entity_type="object", source_origin="form_input"),
        Claim(
            "experienced_symptom",
            object_entity_type="concept",
            roles=(role("time", "temporal", evidence="temporal_ref"),),
        ),
    )
    NEGATIVE = (
        (Claim("has_condition", subject_type="organisation"), "incompatible subject"),
        (
            Claim(
                "has_condition",
                object_entity_type="concept",
                qualifiers={"diagnosis_status": text("certain")},
            ),
            "allowed values",
        ),
        (
            Claim(
                "allergic_to",
                object_entity_type="object",
                qualifiers={"diagnosis_status": text("diagnosed")},
            ),
            "not registered",
        ),
        (
            Claim("takes_medication", object_entity_type="object", source_origin="tool_output"),
            "source origin",
        ),
    )
    DOOR = Door("I am allergic to peanuts.", "allergic_to", "peanuts", "entity", "object")
    DOOR_NEGATIVE = Door("I am allergic to Utrecht.", "allergic_to", "Utrecht", "entity", "place")
    FACT = Fact("Ilse", "allergic_to", "Peanuts")

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        # RM-52: health claims always carry the health label, which can only tighten policy
        for q in qualifiers:
            assert q.sensitivity is not None and "health" in q.sensitivity
        for name in ("has_condition", "allergic_to", "intolerant_of", "takes_medication"):
            assert "health" in registry.sensitivity_for(name)
