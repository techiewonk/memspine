"""Family M: travel and trips (RM-63), domain ``travel``.

M adds only the ``transport_mode`` and ``participation_mode`` qualifiers on the core
visit/stay/attendance predicates, so its facts are ``visited`` claims. Both modes are carried
as claim qualifiers through the canonical fact door and compiled onto edges as typed,
filterable edge qualifiers (ADR-094 amendment 2026-11-02, RM-157)."""

from __future__ import annotations

from families._harness import NS, Claim, Door, Fact, FamilyContract, text
from memspine.derive.pipeline.stages.edge_compiler import EdgeCompiler, edge_qualifiers_for
from memspine.semantics import SemanticRegistry
from memspine.semantics.modes import PARTICIPATION_MODES, TRANSPORT_MODES
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
                "visited",
                object_entity_type="place",
                qualifiers={"transport_mode": text("teleport")},
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
    DOOR = Door(
        "I visited Porto by train.",
        "visited",
        "Porto",
        "entity",
        "place",
        qualifiers=(("participation_mode", "in_person"), ("transport_mode", "train")),
    )
    DOOR_NEGATIVE = Door("I visited Bram.", "visited", "Bram", "entity", "person")
    FACT = Fact(
        "Ilse",
        "visited",
        "Porto",
        tags=("participation_mode:in_person", "transport_mode:train"),
        kind="event",
    )

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        """Both modes are registry vocabularies, reach the compiled edge typed, and pass
        their own graph filters; the door keeps them on the canonical claim."""
        assert registry.properties["participation_mode"].allowed_values == PARTICIPATION_MODES
        assert registry.properties["transport_mode"].allowed_values == TRANSPORT_MODES
        mode = QualifierFilter(
            participation_modes=frozenset({"in_person"}), transport_modes=frozenset({"train"})
        )
        assert qualifiers and all(mode.matches(q) for q in qualifiers)
        assert not any(
            QualifierFilter(transport_modes=frozenset({"car"})).matches(q) for q in qualifiers
        )
        [claim] = self.DOOR.compile(registry).envelopes
        assert {k: v.text for k, v in claim.qualifiers.items()} == {
            "participation_mode": "in_person",
            "transport_mode": "train",
        }
        assert claim.statement_kind == "assertion"

    def test_door_rejects_an_unknown_mode(self, registry: SemanticRegistry) -> None:
        bad = Door(
            "I visited Porto by train.",
            "visited",
            "Porto",
            "entity",
            "place",
            qualifiers=(("transport_mode", "teleport"),),
        ).compile(registry)
        assert not bad.envelopes
        assert [u.reason for u in bad.unresolved] == ["semantic_validation_failed"]

    def test_unknown_tagged_mode_stays_unknown(self, registry: SemanticRegistry) -> None:
        """A tag the registry does not allow for the predicate never reaches the edge."""
        fact = Fact("Ilse", "visited", "Porto", tags=("transport_mode:teleport",), kind="event")
        record = fact.record()
        compiled = EdgeCompiler(registry=registry).compile(
            [record], NS, entity_ids=fact.entity_ids(record)
        )
        meta = compiled.meta[record.record_id]
        for edge in compiled.delta.edges:
            q = edge_qualifiers_for(edge, meta)
            assert q.transport_mode is None and q.claim_qualifiers is None
