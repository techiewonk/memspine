"""Family F: skills, learning and education (RM-62), domain ``learning``."""

from __future__ import annotations

from families._harness import Claim, Door, Fact, FamilyContract, role, text
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers


class TestFamilyF(FamilyContract):
    FAMILY = "learning_education"
    DOMAIN = "learning"
    ACTIVE = True
    POSITIVE = (
        Claim(
            "learning",
            object_entity_type="software",
            qualifiers={"field_of_study": text("databases")},
        ),
        Claim("teaches", object_entity_type="group", roles=(role("topic", "text"),)),
        Claim(
            "enrolled_in",
            object_entity_type="organisation",
            qualifiers={"qualification": text("MSc"), "field_of_study": text("biology")},
            source_origin="form_input",
        ),
        Claim("studied_at", object_entity_type="place"),
    )
    NEGATIVE = (
        (Claim("teaches", object_entity_type="person"), "missing or unsupported predicate role"),
        (Claim("studied_at", object_entity_type="person"), "object entity type"),
        (
            Claim(
                "learning", object_entity_type="concept", qualifiers={"qualification": text("x")}
            ),
            "not registered",
        ),
        (Claim("teaches", value_kind="text", roles=(role("topic", "text"),)), "value kind"),
    )
    DOOR = Door(
        "I teach Bram chess.",
        "teaches",
        "Bram",
        "entity",
        "person",
        roles=(("topic", "text", "chess", None),),
    )
    DOOR_NEGATIVE = Door("I teach Bram.", "teaches", "Bram", "entity", "person")
    FACT = Fact("Ilse", "enrolled_in", "Conservatory")

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        assert registry.predicates["learning"].lifecycle  # learning carries the RM-57 axes
        assert registry.predicates["teaches"].required_roles == ("topic",)
