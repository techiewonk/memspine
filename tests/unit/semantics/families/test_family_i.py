"""Family I: transactions and finance (RM-62), domain ``finance``."""

from __future__ import annotations

from families._harness import Claim, Door, Fact, FamilyContract, role
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers

EUR_450 = role("amount", "number", unit="EUR", amount="450")


class TestFamilyI(FamilyContract):
    FAMILY = "transactions"
    DOMAIN = "finance"
    ACTIVE = True
    POSITIVE = (
        Claim(
            "purchased",
            object_entity_type="object",
            roles=(
                role("buyer", "entity", "person", evidence="envelope"),
                role("seller", "entity", "organisation"),
                role("item", "entity", "object"),
                EUR_450,
            ),
            source_origin="email",
        ),
        Claim(
            "sold",
            value_kind="text",
            roles=(role("seller", "entity", "person"), role("item", "entity", "object")),
        ),
        Claim(
            "paid",
            object_entity_type="person",
            roles=(
                role("giver", "entity", "person"),
                role("amount", "number", unit="EUR", amount="20"),
            ),
            source_origin="structured_record",
        ),
        Claim(
            "lent",
            object_entity_type="object",
            roles=(role("giver", "entity", "person"), role("item", "entity", "object")),
        ),
        Claim(
            "borrowed",
            object_entity_type="work",
            roles=(role("recipient", "entity", "person"), role("item", "entity", "work")),
            source_origin="document",
        ),
    )
    NEGATIVE = (
        (
            Claim("paid", object_entity_type="person", roles=(role("giver", "entity", "person"),)),
            "missing or unsupported predicate role",
        ),
        (
            Claim(
                "paid",
                object_entity_type="person",
                roles=(role("giver", "entity", "person"), role("amount", "number", amount="20")),
            ),
            "explicit unit",
        ),
        (
            Claim(
                "sold",
                value_kind="text",
                roles=(
                    role("seller", "entity", "person", evidence="envelope"),
                    role("item", "entity", "object"),
                ),
            ),
            "exact_span",
        ),
        (
            Claim(
                "purchased",
                object_entity_type="object",
                roles=(role("buyer", "entity", "person"), role("item", "entity", "object")),
                source_origin="tool_output",
            ),
            "source origin",
        ),
    )
    DOOR = Door(
        "I bought a bike from Bram.",
        "purchased",
        "bike",
        "entity",
        "object",
        roles=(("buyer", "entity", "I", "person"), ("item", "entity", "bike", "object")),
    )
    DOOR_NEGATIVE = Door(
        "I bought a bike from Bram.",
        "purchased",
        "bike",
        "entity",
        "object",
        roles=(("buyer", "entity", "I", "person"),),
    )
    FACT = Fact("Ilse", "purchased", "Bike", kind="event")

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        for q in qualifiers:  # RM-52: transactions always carry the financial label
            assert q.sensitivity is not None and "financial" in q.sensitivity
        sale = [p for p in registry.predicates.values() if p.event_family == "sale"]
        assert {p.name for p in sale} == {"purchased", "sold"}
