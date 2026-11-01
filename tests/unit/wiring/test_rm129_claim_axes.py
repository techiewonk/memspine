"""RM-129 / ADR-088: every claim is one ``SemanticEnvelope``; its modality, status and outcome
round-trip through storage, compile onto graph edges, and a non-asserted claim never reads as an
asserted fact. Model-free."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.derive.compile import CompileContext, WindowLine, compile_items
from memspine.derive.fact_policy import FactPurpose, is_evidence_fact
from memspine.derive.pipeline.stages.edge_compiler import (
    COMPILED,
    EdgeCompiler,
    compile_graph_edges,
)
from memspine.derive.write import write_derived
from memspine.schemas.durable import (
    AssertionKey,
    Assessment,
    Evidence,
    OwnerScope,
    SemanticEnvelope,
    TurnRef,
    occurrence_id,
    write_id,
)
from memspine.services.graph.qualifiers import AssertionModality, Polarity, edge_qualifiers
from memspine.store.records import MemoryRecord, RecordFingerprint, SourceInfo

NS = "acme/alice"
SCOPE = OwnerScope(tenant="acme", owner=NS)


def _claim(
    source_id: str, *, modality: str = "asserted", outcome: str = "none", **kw: Any
) -> SemanticEnvelope:
    key = AssertionKey(src="Alice", rel="works_on", dst="apollo", kind="state")
    return SemanticEnvelope(
        write_id=write_id(SCOPE, "c", 0, "Alice works on Apollo"),
        assertion_key=key,
        occurrence_id=occurrence_id("c", [source_id], key),
        scope=SCOPE,
        text="Alice works on Apollo",
        kind="state",
        attribute="works_on",
        evidence=[
            Evidence(
                role="support" if modality == "asserted" else "binding",
                turn=TurnRef(source_id=source_id, source_version=1, seg_index=0),
                source_family="user:alice",
            )
        ],
        assessment=Assessment(source_trust=0.8, binding="bound", modality=modality),
        outcome=outcome,
        src_class="user_stated",
        **kw,
    )


def _record(claim: SemanticEnvelope, *, parents: list[str], rid: str | None = None) -> MemoryRecord:
    extra = {"record_id": rid} if rid else {}
    return MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=claim.text,
        entity="Alice",
        attribute="works_on",
        tags=["kind:state", "rel:works_on", "dst:Apollo"],
        source=SourceInfo(role="derived", channel="extract_graph", parents=parents),
        fingerprint=RecordFingerprint(envelope=claim.model_dump(mode="json")),
        **extra,
    )


def test_a_model_path_claim_validates_as_semantic_envelope() -> None:
    ctx = CompileContext(
        scope=SCOPE,
        source_id="t1",
        window={
            1: WindowLine(
                turn_id="t1:1",
                source_id="t1",
                source_version=1,
                seg_index=0,
                role="user",
                speaker="alice",
                source_at=None,
                content="I will run the Lisbon marathon.",
            )
        },
        prompt_hash="ph-model-1",
    )
    out = compile_items(
        [
            {
                "text": "Alice plans to run the Lisbon marathon",
                "kind": "goal",
                "s": "Alice",
                "r": "has_goal",
                "o": "Lisbon marathon",
                "mode": "planned",
                "neg": False,
                "lines": [1],
                "span": "I will run the Lisbon marathon.",
            }
        ],
        ctx,
    )
    assert len(out.envelopes) == 1, out
    claim = out.envelopes[0]
    assert isinstance(claim, SemanticEnvelope)
    assert claim.producer is not None and claim.producer.kind == "model"
    assert claim.producer.revision == "ph-model-1"
    again = SemanticEnvelope.model_validate(claim.model_dump(mode="json"), strict=False)
    assert again == claim
    assert claim.assessment.modality == "planned" and not claim.asserts_fact


@pytest.mark.parametrize("modality", ["planned", "negated", "hypothetical", "question"])
def test_non_asserted_claims_never_read_as_asserted_facts(modality: str) -> None:
    rec = _record(_claim("t1", modality=modality), parents=["t1"])
    for purpose in FactPurpose:
        if purpose is FactPurpose.EDGES:
            continue  # projected as a tentative / negated edge, never as a fact
        assert not is_evidence_fact(rec, purpose), purpose
    asserted = _record(_claim("t1"), parents=["t1"])
    assert is_evidence_fact(asserted, FactPurpose.RECONCILE)


def test_claim_axes_compile_onto_edges() -> None:
    done = _record(_claim("t1", modality="planned", outcome="done"), parents=["t1"])
    negated = _record(_claim("t2", modality="negated"), parents=["t2"])
    ids = {  # RM-130: the recorded entity links (canonical name -> opaque id)
        r.record_id: {n: f"ent:{NS}:id-{n}" for n in ("alice", "apollo")} for r in (done, negated)
    }
    compiled = EdgeCompiler().compile([done, negated], NS, entity_ids=ids)
    by_record: dict[str, list[Any]] = {}
    for edge in compiled.delta.edges:
        by_record.setdefault(edge.record_id, []).append(edge)
    from memspine.derive.pipeline.stages.edge_compiler import edge_qualifiers_for

    def quals(rec: MemoryRecord) -> list[Any]:
        found = [
            edge_qualifiers_for(e, compiled.meta[rec.record_id]) for e in by_record[rec.record_id]
        ]
        assert found
        return found

    for q in quals(done):
        assert q.assertion_modality is AssertionModality.INTENDED
        assert (q.lifecycle_status, q.lifecycle_outcome) == ("closed", "done")
    for q in quals(negated):
        assert q.polarity is Polarity.NEGATIVE
        assert q.lifecycle_outcome is None
    assert all(e.tentative for e in by_record[done.record_id])
    assert all(e.negated for e in by_record[negated.record_id])


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False},
        graph={"edge_compiler": True},
    )


async def test_claim_axes_round_trip_through_storage_onto_edges() -> None:
    eng = _engine()
    await eng.start()
    try:
        turn = await eng.write("I finished the Apollo work", namespace=NS, memory_type="episodic")
        claim = _claim(turn.record_id, modality="planned", outcome="done")
        rec = _record(claim, parents=[turn.record_id])
        # through the deposit door, so the WRITE event carries its RM-130 entity links
        rec = await write_derived(
            eng, eng._require_started(), NS, rec, "summary", "system", parents=[turn.record_id]
        )
        stored = await eng.get_envelope(rec.record_id, NS)
        assert stored is not None
        assert (stored.assessment.modality, stored.status, stored.outcome) == (
            "planned",
            "active",
            "done",
        )
        assert stored == claim
        assert (await compile_graph_edges(eng._pipeline_ctx()))["status"] == "ok"
        assert eng._graph is not None
        edges = [
            e
            for e in await eng._graph.edge_list(NS)
            if e.properties.get("compiled") == COMPILED
            and e.properties.get("record_id") == rec.record_id
        ]
    finally:
        await eng.stop()
    assert edges
    for edge in edges:
        q = edge_qualifiers(edge.properties)
        assert q is not None
        assert q.assertion_modality is AssertionModality.INTENDED
        assert (q.lifecycle_status, q.lifecycle_outcome) == ("closed", "done")
