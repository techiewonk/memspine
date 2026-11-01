"""Family N: tool and agent experience (RM-63), domain ``tools``."""

from __future__ import annotations

from families._harness import Claim, Door, Fact, FamilyContract, role, text
from memspine.semantics import SemanticRegistry
from memspine.services.graph.qualifiers import EdgeQualifiers, QualifierFilter


class TestFamilyN(FamilyContract):
    FAMILY = "tool_experience"
    DOMAIN = "tools"
    ACTIVE = True
    POSITIVE = (
        Claim(
            "attempted",
            subject_type="agent",
            value_kind="text",
            roles=(role("action", "text"), role("instrument", "entity", "software")),
            source_origin="tool_output",
        ),
        Claim(
            "failed_with",
            subject_type="software",
            value_kind="text",
            qualifiers={"error_type": text("timeout")},
            source_origin="system_event",
        ),
        Claim(
            "workaround_for",
            subject_type="software",
            object_entity_type="concept",
            source_origin="code_repository",
        ),
    )
    NEGATIVE = (
        (
            Claim("attempted", subject_type="agent", value_kind="text"),
            "missing or unsupported predicate role",
        ),
        (Claim("failed_with", subject_type="agent", object_entity_type="concept"), "value kind"),
        (
            Claim(
                "attempted",
                subject_type="agent",
                value_kind="text",
                roles=(role("action", "text"),),
                source_origin="email",
            ),
            "source origin",
        ),
        (
            Claim("workaround_for", subject_type="person", object_entity_type="concept"),
            "incompatible subject",
        ),
    )
    DOOR = Door(
        "I tried to deploy the service.",
        "attempted",
        "deploy",
        "text",
        roles=(("action", "text", "deploy the service", None),),
    )
    DOOR_NEGATIVE = Door("I tried to deploy the service.", "attempted", "deploy", "text")
    FACT = Fact(
        "Agent",
        "attempted",
        "Deploy",
        tags=("lifecycle:closed", "outcome:failed"),
        channel="agent_tool",
        kind="event",
    )

    def extra_positive(self, registry: SemanticRegistry, qualifiers: list[EdgeQualifiers]) -> None:
        """A tool-reported attempt keeps its tool_output origin and its failed outcome."""
        flt = QualifierFilter(
            origin_types=frozenset({"tool_output"}), lifecycle_outcomes=frozenset({"failed"})
        )
        assert all(flt.matches(q) for q in qualifiers)
        assert registry.predicates["attempted"].lifecycle
