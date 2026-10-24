"""BI-06 deterministic fact candidate stage."""

from __future__ import annotations

from memspine.derive.non_generative_facts import (
    FactStageResult,
    GroundedFactCandidate,
    compile_grounded_facts,
    mine_grounded_facts,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.semantic import RoleBinding
from memspine.semantics import SemanticRegistry, load_builtin_registry
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


def test_candidate_is_registry_validated_and_compiled_to_canonical_envelope() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    record = _record("I work for Acme.")
    result = mine_grounded_facts(record, scope)

    compiled = compile_grounded_facts(result, [record], scope)

    assert not compiled.unresolved and len(compiled.envelopes) == 1
    envelope = compiled.envelopes[0]
    assert envelope.attribute == "employed_by"
    assert envelope.assertion_key.rel == "employed_by"
    assert envelope.evidence[0].span is not None
    semantic = envelope.semantic
    assert semantic is not None
    assert semantic.predicate == "employed_by"
    assert semantic.object_entity_type == "organisation"
    assert semantic.registry_digest


def _evidence(record: MemoryRecord, quote: str, *, role: str = "support") -> Evidence:
    start = record.content.index(quote)
    return Evidence(
        role=role,
        turn=TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0),
        span=Span(start=start, end=start + len(quote), quote=quote),
        source_family="alice",
    )


def _preference_candidate(
    record: MemoryRecord, scope: OwnerScope, bindings: tuple[RoleBinding, ...]
) -> GroundedFactCandidate:
    support = _evidence(record, record.content)
    return GroundedFactCandidate(
        scope=scope,
        subject="alice",
        predicate="prefers",
        object="tea",
        object_kind="text",
        modality="asserted",
        assertion_owner="alice",
        evidence=support,
        role_bindings=bindings,
        producer_revision="fixture:1",
    )


def test_typed_role_bindings_accept_required_roles_and_exact_evidence() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    record = _record("I prefer tea to coffee.")
    support = _evidence(record, record.content)
    bindings = (
        RoleBinding(
            binding_id="preferred-1",
            role_name="preferred",
            role_version=1,
            value_kind="text",
            value="tea",
            evidence=(support,),
        ),
        RoleBinding(
            binding_id="alternative-1",
            role_name="alternative",
            role_version=1,
            value_kind="text",
            value="coffee",
            evidence=(support,),
        ),
    )

    compiled = compile_grounded_facts(
        FactStageResult(facts=(_preference_candidate(record, scope, bindings),)),
        [record],
        scope,
    )

    assert len(compiled.envelopes) == 1 and not compiled.unresolved
    semantic = compiled.envelopes[0].semantic
    assert semantic is not None
    assert [item.role_name for item in semantic.role_bindings] == ["preferred", "alternative"]


def test_missing_or_over_cardinality_role_binding_stays_unresolved() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    record = _record("I prefer tea to coffee.")
    support = _evidence(record, record.content)
    repeated = tuple(
        RoleBinding(
            binding_id=f"preferred-{index}",
            role_name="preferred",
            role_version=1,
            value_kind="text",
            value="tea",
            evidence=(support,),
        )
        for index in range(2)
    )

    compiled = compile_grounded_facts(
        FactStageResult(facts=(_preference_candidate(record, scope, repeated),)),
        [record],
        scope,
    )

    assert not compiled.envelopes
    assert compiled.unresolved[0].reason == "semantic_validation_failed"


def test_role_without_required_exact_span_stays_unresolved() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    record = _record("I prefer tea to coffee.")
    turn_only = Evidence(
        role="binding",
        turn=TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0),
        source_family="alice",
    )
    support = _evidence(record, record.content)
    bindings = (
        RoleBinding(
            binding_id="preferred-1",
            role_name="preferred",
            role_version=1,
            value_kind="text",
            value="tea",
            evidence=(turn_only,),
        ),
        RoleBinding(
            binding_id="alternative-1",
            role_name="alternative",
            role_version=1,
            value_kind="text",
            value="coffee",
            evidence=(support,),
        ),
    )

    compiled = compile_grounded_facts(
        FactStageResult(facts=(_preference_candidate(record, scope, bindings),)),
        [record],
        scope,
    )

    assert not compiled.envelopes
    assert compiled.unresolved[0].reason == "semantic_validation_failed"


def test_explicit_negative_candidate_preserves_negative_modality() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    record = _record("I like coffee.")
    candidate = mine_grounded_facts(record, scope).facts[0].model_copy(
        update={"polarity": "negative"}
    )

    compiled = compile_grounded_facts(
        FactStageResult(facts=(candidate,)), [record], scope
    )

    assert compiled.envelopes[0].assessment.modality == "negated"


def test_contextual_cardinality_requires_declared_scope_before_acceptance() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    record = _record("I like coffee.")
    candidate = mine_grounded_facts(record, scope).facts[0]
    spec = load_builtin_registry().predicates["likes"].model_copy(
        update={"cardinality": "contextual", "cardinality_scope": ("context",)}
    )
    registry = SemanticRegistry([spec], [], [])

    missing = compile_grounded_facts(
        FactStageResult(facts=(candidate,)), [record], scope, registry=registry
    )
    accepted = compile_grounded_facts(
        FactStageResult(
            facts=(candidate.model_copy(update={"cardinality_scope": {"context": "food"}}),)
        ),
        [record],
        scope,
        registry=registry,
    )

    assert missing.unresolved[0].reason == "semantic_validation_failed"
    assert accepted.envelopes[0].semantic is not None
    assert accepted.envelopes[0].semantic.cardinality_scope == {"context": "food"}


def test_cross_scope_or_changed_citation_is_rejected_before_compilation() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    record = _record("I work for Acme.")
    candidate = mine_grounded_facts(record, scope).facts[0]
    changed = candidate.model_copy(
        update={
            "evidence": candidate.evidence.model_copy(
                update={"span": Span(start=0, end=1, quote="X")}
            )
        }
    )

    invalid = compile_grounded_facts(FactStageResult(facts=(changed,)), [record], scope)
    foreign = compile_grounded_facts(
        FactStageResult(facts=(candidate,)),
        [record.model_copy(update={"namespace": "tenant/bob"})],
        scope,
    )

    assert invalid.unresolved[0].reason == "invalid_evidence"
    assert foreign.unresolved[0].reason == "scope_mismatch"


def test_same_scope_context_is_cited_as_binding_not_support() -> None:
    scope = OwnerScope(tenant="tenant", owner="tenant/alice")
    source = _record("I work for Acme.")
    context = _record("Acme is the company we discussed.").model_copy(
        update={"record_id": "record-2"}
    )
    candidate = mine_grounded_facts(source, scope).facts[0].model_copy(
        update={"context_evidence": (_evidence(context, "Acme", role="binding"),)}
    )

    compiled = compile_grounded_facts(
        FactStageResult(facts=(candidate,)), [source, context], scope
    )

    assert [item.role for item in compiled.envelopes[0].evidence] == ["support", "binding"]
