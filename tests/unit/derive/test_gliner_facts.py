"""DS-18 (ADR-090 item 6): the non-generative GLiNER fact extractor. Model-free: a fake GLiNER2
model with the SDK surface the structured client calls (``tests/unit/derive/gliner_fake.py``)."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gliner_fake import FakeBinding, FakeGliner

from memspine.derive.gliner_facts import GLINER_FACT_REVISION, GlinerFactExtractor
from memspine.derive.non_generative_facts import compile_grounded_facts
from memspine.schemas.durable.common import OwnerScope
from memspine.store.records import MemoryRecord, SourceInfo

NS = "a"
SCOPE = OwnerScope(tenant="default", owner=NS)
T0 = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)


def _record(content: str, *, role: str = "user", sender: str | None = "Mira") -> MemoryRecord:
    message: dict[str, object] = {"message_id": "m-1", "role": role}
    if sender is not None:
        message["sender"] = {"id": sender}
    return MemoryRecord(
        namespace=NS,
        memory_type="episodic",
        content=content,
        source=SourceInfo(role=role, channel="messages", message=message),
        source_at=T0,
    )


def _extractor(model: FakeGliner | None = None) -> GlinerFactExtractor:
    return GlinerFactExtractor(FakeBinding(model or FakeGliner()), model_id="gliner2:fake")


@pytest.mark.parametrize(
    ("content", "predicate", "obj", "entity_type"),
    [
        ("I live in Lisbon.", "resides_in", "Lisbon", "place"),
        ("Honestly, I work for Acme these days.", "employed_by", "Acme", "organisation"),
        ("Mira lives in Porto.", "resides_in", "Porto", "place"),
    ],
)
async def test_speaker_relation_becomes_a_canonical_envelope(
    content: str, predicate: str, obj: str, entity_type: str
) -> None:
    record = _record(content)
    result = await _extractor().extract_grounded_facts(record, SCOPE)
    assert result.generative_calls == 0 and not result.unresolved
    [fact] = result.facts
    assert (fact.subject, fact.predicate, fact.object) == ("Mira", predicate, obj)
    assert fact.assertion_owner == "Mira" and fact.producer_revision == GLINER_FACT_REVISION
    span = fact.evidence.span
    assert span is not None and content[span.start : span.end] == span.quote
    compiled = compile_grounded_facts(result, [record], SCOPE)
    [envelope] = compiled.envelopes
    assert envelope.registry is not None and envelope.registry.predicate == predicate
    assert envelope.registry.object_entity_type == entity_type
    assert envelope.producer is not None and envelope.producer.kind == "rules"
    assert envelope.assessment.modality == "asserted"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("I do not live in Porto.", "grounding_unsupported"),
        ("If I live in Lisbon, I will cycle to work.", "grounding_unsupported"),
        ("Do I live in Lisbon?", "grounding_unsupported"),
        ("Daniel said, I live in Porto.", "grounding_unsupported"),
        ("Bram lives in Lisbon.", "subject_not_speaker"),
    ],
)
async def test_abstentions_stay_unresolved_with_their_span(content: str, reason: str) -> None:
    record = _record(content)
    result = await _extractor().extract_grounded_facts(record, SCOPE)
    assert result.facts == ()
    [candidate] = result.unresolved
    assert candidate.reason == reason and candidate.source_id == record.record_id
    assert candidate.span is not None and candidate.producer_revision == GLINER_FACT_REVISION
    assert compile_grounded_facts(result, [record], SCOPE).envelopes == ()


async def test_ineligible_sources_never_reach_the_model() -> None:
    model = FakeGliner()
    extractor = _extractor(model)
    assistant = await extractor.extract_grounded_facts(
        _record("I live in Lisbon.", role="assistant"), SCOPE
    )
    no_owner = await extractor.extract_grounded_facts(
        _record("I live in Lisbon.", sender=None), SCOPE
    )
    assert [u.reason for u in assistant.unresolved] == ["source_not_user"]
    assert [u.reason for u in no_owner.unresolved] == ["missing_assertion_owner"]
    assert model.calls == 0


async def test_unmappable_payload_is_one_unresolved_candidate() -> None:
    result = await _extractor(FakeGliner(broken=True)).extract_grounded_facts(
        _record("I live in Lisbon."), SCOPE
    )
    assert result.facts == () and [u.reason for u in result.unresolved] == ["extractor_failed"]


async def test_entity_spans_come_back_as_unasserted_mentions() -> None:
    content = "I live in Lisbon and Bram works for Globex."
    result = await _extractor().extract_grounded_facts(_record(content), SCOPE)
    assert {(m.text, m.label) for m in result.mentions} >= {
        ("Lisbon", "location"),
        ("Bram", "person"),
        ("Globex", "organization"),
    }
    assert all(m.asserted is False for m in result.mentions)
    assert all(content[m.start : m.end] == m.text for m in result.mentions)
    # The naive "I ... works for Globex" pairing has another person between its endpoints:
    # unresolved, never the speaker's fact.
    assert [f.object for f in result.facts] == ["Lisbon"]
    assert [u.reason for u in result.unresolved] == ["ambiguous_support_span"]
