"""RM-157: the edge compiler turns claim role bindings into qualified role edges (pure part).

The end-to-end path (door, deposit, entity links, projection, graph_filter, replay,
erasure) is covered by ``tests/scenarios/test_scenario_role_edges.py``; this module pins the
compiler's own rules on a stored claim: which bindings bind, the skip reasons, the edge
shape, and the qualifiers and statement kind that pass through."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.derive.non_generative_facts import (
    FactStageResult,
    GroundedFactCandidate,
    compile_grounded_facts,
)
from memspine.derive.pipeline.stages.edge_compiler import (
    ROLE_PREFIX,
    ROLE_SKIP_REASONS,
    EdgeCompiler,
    edge_qualifiers_for,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.semantic import ClaimQualifier, RoleBinding
from memspine.store.records import MemoryRecord, RecordFingerprint, SourceInfo

NS = "tenant/ola"
SCOPE = OwnerScope(tenant="tenant", owner=NS)
TEXT = "I will send the quarterly report to Kim and Ana."


def _source() -> MemoryRecord:
    return MemoryRecord(
        record_id="src-1",
        namespace=NS,
        memory_type="episodic",
        content=TEXT,
        source=SourceInfo(
            role="user", channel="messages", message={"message_id": "m-1", "sender": {"id": "Ola"}}
        ),
    )


def _ev(record: MemoryRecord, quote: str | None) -> Evidence:
    turn = TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0)
    if quote is None:
        return Evidence(role="support", turn=turn, source_family="Ola")
    start = record.content.index(quote)
    return Evidence(
        role="support",
        turn=turn,
        span=Span(start=start, end=start + len(quote), quote=quote),
        source_family="Ola",
    )


def _claim_record() -> MemoryRecord:
    """The semantic record the deposit door writes for a committed_to claim."""
    src = _source()
    roles = (
        ("obligor", "Ola", "entity", "person", None),
        ("action", "send the quarterly report", "text", None, "send the quarterly report"),
        ("beneficiary", "Kim", "entity", "person", "Kim"),
        ("beneficiary", "Ana", "entity", "person", "Ana"),
    )
    candidate = GroundedFactCandidate(
        scope=SCOPE,
        subject="Ola",
        predicate="committed_to",
        object="quarterly report",
        object_kind="entity",
        object_entity_type="document",
        modality="asserted",
        assertion_owner="Ola",
        evidence=_ev(src, TEXT),
        role_bindings=tuple(
            RoleBinding(
                binding_id=f"rb-{i}",
                role_name=name,
                role_version=1,
                value_kind=kind,  # type: ignore[arg-type]
                value=value,
                entity_type=etype,  # type: ignore[arg-type]
                evidence=[_ev(src, quote)],
            )
            for i, (name, value, kind, etype, quote) in enumerate(roles)
        ),
        qualifiers={"priority": ClaimQualifier(kind="text", text="high")},
    )
    [env] = compile_grounded_facts(FactStageResult(facts=(candidate,)), [src], SCOPE).envelopes
    return MemoryRecord(
        record_id="claim-1",
        namespace=NS,
        memory_type="semantic",
        content=env.text,
        entity="Ola",
        tags=[
            "kind:event",
            "rel:committed_to",
            "dst:quarterly report",
            "non_generative",
        ],
        source=SourceInfo(role="derived", channel="window_fact", parents=[src.record_id]),
        valid_from=datetime(2026, 10, 1, 9, tzinfo=UTC),
        fingerprint=RecordFingerprint(envelope=env.model_dump(mode="json")),
    )


LINKS = {
    "ola": "ent:tenant/ola:ola",
    "quarterly report": "ent:tenant/ola:report",
    "kim": "ent:tenant/ola:kim",
}


def test_door_keeps_role_bindings_qualifiers_and_statement_kind() -> None:
    env = _claim_record().fingerprint.envelope
    assert env is not None
    assert env["statement_kind"] == "assertion"
    assert env["qualifiers"] == {
        "priority": {"kind": "text", "text": "high", "amount": None, "flag": None}
    }
    assert sorted(b["role_name"] for b in env["role_bindings"]) == [
        "action",
        "beneficiary",
        "beneficiary",
        "obligor",
    ]


def test_entity_role_fillers_become_role_edges_from_the_assertion_node() -> None:
    record = _claim_record()
    compiled = EdgeCompiler().compile([record], NS, entity_ids={record.record_id: LINKS})
    edges = {(e.label, e.dst): e for e in compiled.delta.edges if e.kind == "role"}
    assertion = edges[("subject", LINKS["ola"])].src
    assert edges[("obligor", LINKS["ola"])].src == assertion
    assert edges[("beneficiary", LINKS["kim"])].src == assertion
    assert {label for label, _ in edges} == {"subject", "object", "obligor", "beneficiary"}
    # Ana has no entity link and the action is text: skipped with a reason, never invented
    assert compiled.role_skips == {
        record.record_id: (
            ("action", "rb-1", "not_an_entity"),
            ("beneficiary", "rb-3", "no_entity_link"),
        )
    }
    assert set(ROLE_SKIP_REASONS) >= {r for *_, r in compiled.role_skips[record.record_id]}
    assert f"{ROLE_PREFIX}beneficiary".startswith("role:")


def test_role_edges_carry_the_claim_qualifiers() -> None:
    record = _claim_record()
    compiled = EdgeCompiler().compile([record], NS, entity_ids={record.record_id: LINKS})
    meta = compiled.meta[record.record_id]
    by_label = {e.label: edge_qualifiers_for(e, meta) for e in compiled.delta.edges}
    role, subject = by_label["beneficiary"], by_label["subject"]
    assert role.event_role == "beneficiary"
    assert role.model_copy(update={"event_role": "subject"}) == subject
    assert role.statement_kind == "assertion"
    assert role.claim_qualifiers == {"priority": "high"}
    assert role.topical_domain is not None and role.topical_domain.startswith("work")
    assert role.valid_from is not None
    # the projection edge carries them too
    assert by_label["committed_to"].claim_qualifiers == {"priority": "high"}


def test_replayed_compile_gives_the_same_role_edges() -> None:
    record = _claim_record()
    first = EdgeCompiler().compile([record], NS, entity_ids={record.record_id: LINKS})
    again = EdgeCompiler().compile([record], NS, entity_ids={record.record_id: dict(LINKS)})
    assert first.delta.edges == again.delta.edges
    assert first.role_skips == again.role_skips


def test_a_claim_without_bindings_compiles_no_role_edges() -> None:
    record = _claim_record().model_copy(update={"fingerprint": RecordFingerprint()})
    compiled = EdgeCompiler().compile([record], NS, entity_ids={record.record_id: LINKS})
    assert {e.label for e in compiled.delta.edges if e.kind == "role"} == {"subject", "object"}
    assert not compiled.role_skips
