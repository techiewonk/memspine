"""BI-06 deterministic fact candidate stage."""

from __future__ import annotations

from memspine.derive.non_generative_facts import mine_grounded_facts
from memspine.schemas.durable.common import OwnerScope
from memspine.store.records import MemoryRecord, SourceInfo

SCOPE = OwnerScope(tenant="tenant", owner="alice")


def _record(content: str, *, role: str = "user") -> MemoryRecord:
    return MemoryRecord(
        record_id="record-1",
        namespace="tenant/alice",
        memory_type="episodic",
        content=content,
        source=SourceInfo(
            role=role,
            channel="messages",
            message={"message_id": "message-1", "sender": {"id": "alice"}},
        ),
    )


def test_explicit_supported_rule_yields_exact_source_grounded_fact() -> None:
    record = _record("I work for Acme.")

    result = mine_grounded_facts(record, SCOPE)

    assert result.generative_calls == 0
    assert len(result.facts) == 1 and not result.unresolved
    fact = result.facts[0]
    assert (fact.subject, fact.predicate, fact.object) == ("alice", "employed_by", "Acme")
    assert fact.assertion_owner == "alice" and fact.modality == "asserted"
    assert fact.evidence.turn is not None
    assert fact.evidence.turn.source_id == record.record_id
    assert fact.evidence.turn.source_version == record.version
    assert fact.evidence.span is not None
    assert (
        record.content[fact.evidence.span.start : fact.evidence.span.end]
        == fact.evidence.span.quote
    )


def test_assistant_proposal_is_never_promoted_to_user_fact() -> None:
    result = mine_grounded_facts(_record("I work for Acme.", role="assistant"), SCOPE)

    assert not result.facts
    assert result.unresolved[0].reason == "assistant_proposal"


def test_quoted_reported_conditional_and_uncertain_text_yield_no_facts() -> None:
    texts = (
        'Bob said "I work for Acme."',
        "If I work for Acme, I will move.",
        "Maybe I work for Acme.",
        "I do not work for Acme.",
    )

    assert all(not mine_grounded_facts(_record(text), SCOPE).facts for text in texts)


def test_correction_stays_unresolved_instead_of_selecting_a_value() -> None:
    result = mine_grounded_facts(_record("I work for Acme—correction, I meant Contoso."), SCOPE)

    assert not result.facts
    assert result.unresolved[0].reason == "correction_requires_resolution"


def test_repeated_value_is_not_assigned_a_guessed_support_span() -> None:
    result = mine_grounded_facts(_record("Acme is nearby. I work for Acme."), SCOPE)

    assert not result.facts
    assert result.unresolved[0].reason == "ambiguous_support_span"


def test_unsupported_rule_output_remains_unresolved() -> None:
    result = mine_grounded_facts(_record("I am a vegetarian."), SCOPE)

    assert not result.facts
    assert result.unresolved[0].reason == "unsupported_predicate"


def test_compound_relation_is_unresolved_instead_of_overcaptured() -> None:
    result = mine_grounded_facts(_record("I work for Acme in Utrecht."), SCOPE)

    assert not result.facts
    assert result.unresolved[0].reason == "unsupported_relation"


def test_user_goal_is_typed_planned_not_asserted_world_state() -> None:
    result = mine_grounded_facts(_record("I plan to visit Kyoto."), SCOPE)

    assert len(result.facts) == 1
    assert result.facts[0].predicate == "has_goal"
    assert result.facts[0].modality == "planned"
