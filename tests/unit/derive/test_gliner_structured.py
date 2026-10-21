from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from memspine.derive.gliner_structured import (
    GLiNER25StructuredExtractor,
    RecordSchema,
    RelationSchema,
    SourceContext,
    StructuredExtractionError,
    StructuredExtractionSchema,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import TurnRef


class FakeExtractor:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def extract(self, text: str, schema: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append((text, dict(schema), kwargs))
        return self.payload


@pytest.fixture
def source() -> SourceContext:
    return SourceContext(
        scope=OwnerScope(tenant="tenant-1", owner="user-7"),
        turn=TurnRef(source_id="message-42", source_version=3, seg_index=0),
        sender_id="mira",
        text="Mira moved to Porto in 2024.",
    )


@pytest.fixture
def schema() -> StructuredExtractionSchema:
    return StructuredExtractionSchema(
        entity_types=("person", "location"),
        records=(
            RecordSchema(
                name="move_event",
                fields=(
                    {"name": "subject", "type_label": "person", "required": True},
                    {"name": "destination", "type_label": "location", "required": True},
                    {"name": "year", "type_label": "year"},
                ),
            ),
        ),
        relations=(
            RelationSchema(
                name="moved_to",
                head_type="person",
                tail_type="location",
                description="A person moved to a location",
            ),
        ),
    )


def _good_payload() -> dict[str, Any]:
    return {
        "entities": {
            "person": [{"text": "Mira", "confidence": 0.93, "start": 0, "end": 4}],
            "location": [{"text": "Porto", "confidence": 0.88, "start": 14, "end": 19}],
        },
        "move_event": [
            {
                "subject": {"text": "Mira", "confidence": 0.93, "start": 0, "end": 4},
                "destination": {"text": "Porto", "confidence": 0.88, "start": 14, "end": 19},
                "year": {"text": "2024", "confidence": 0.91, "start": 23, "end": 27},
                "confidence": 0.92,
            }
        ],
        "relation_extraction": {
            "moved_to": [
                {
                    "head": {"text": "Mira", "confidence": 0.93, "start": 0, "end": 4},
                    "tail": {"text": "Porto", "confidence": 0.88, "start": 14, "end": 19},
                    "confidence": 0.87,
                }
            ]
        },
    }


def test_schema_builds_strict_configurable_sdk_shape_and_rejects_reserved_ownership_fields(
    schema: StructuredExtractionSchema,
) -> None:
    payload = schema.to_sdk_schema()

    assert payload["entities"] == ["person", "location"]
    assert payload["structures"]["move_event"]["mode"] == "natural"
    assert payload["structures"]["move_event"]["anchor"] == "subject"
    assert payload["structures"]["move_event"]["fields"][0] == {
        "name": "subject",
        "dtype": "str",
        "cardinality": "required_one",
    }
    assert payload["relations"] == {"moved_to": "A person moved to a location"}
    with pytest.raises(ValidationError, match="reserved"):
        RecordSchema(name="claim", fields=({"name": "source_id", "type_label": "person"},))
    with pytest.raises(ValidationError, match="declared entity types"):
        StructuredExtractionSchema(
            entity_types=("person",),
            relations=(RelationSchema(name="knows", head_type="person", tail_type="organization"),),
        )


def test_maps_sdk_candidate_types_confidences_offsets_and_source_ownership(
    source: SourceContext,
    schema: StructuredExtractionSchema,
) -> None:
    sdk = FakeExtractor(_good_payload())
    mapped = GLiNER25StructuredExtractor(sdk, schema).extract(source)

    assert len(sdk.calls) == 1
    text, sent_schema, options = sdk.calls[0]
    assert text == source.text
    assert sent_schema == schema.to_sdk_schema()
    assert options == {"threshold": 0.5, "include_confidence": True, "include_spans": True}
    assert [(m.type_label, m.text, m.confidence) for m in mapped.mentions] == [
        ("person", "Mira", 0.93),
        ("location", "Porto", 0.88),
    ]
    record = mapped.records[0]
    assert record.asserted is False
    assert record.scope == source.scope
    assert record.source_id == source.turn.source_id
    assert record.source_version == source.turn.source_version
    year = next(field for field in record.fields if field.field_name == "year")
    assert (year.type_label, year.text, year.confidence, year.span.start, year.span.end) == (
        "year",
        "2024",
        0.91,
        23,
        27,
    )
    assert year.evidence.turn == source.turn
    assert year.evidence.source_family == "mira"
    rel = mapped.relations[0]
    assert rel.relation_type == "moved_to"
    assert rel.relation_confidence == 0.87
    assert mapped.records[0].confidence == 0.92
    assert rel.asserted is False
    assert rel.head.confidence == 0.93 and rel.tail.confidence == 0.88


@pytest.mark.parametrize(
    "patch, message",
    [
        ({"entities": {"alien": []}}, "unsupported entity labels"),
        ({"relation_extraction": {"married_to": []}}, "unsupported relation labels"),
        ({"model_scope": "other"}, "unexpected SDK result keys"),
        ({"move_event": [{"namespace": {"text": "tenant-2"}}]}, "unexpected fields"),
        (
            {
                "move_event": [
                    {
                        "subject": {
                            "text": "Mira",
                            "confidence": 0.9,
                            "start": 0,
                            "end": 4,
                            "sender_id": "other",
                        }
                    }
                ]
            },
            "exactly text",
        ),
    ],
)
def test_rejects_unsupported_labels_and_model_authored_scope_or_sender(
    source: SourceContext,
    schema: StructuredExtractionSchema,
    patch: dict[str, Any],
    message: str,
) -> None:
    payload = _good_payload()
    payload.update(patch)

    with pytest.raises(StructuredExtractionError, match=message):
        GLiNER25StructuredExtractor(FakeExtractor(payload), schema).extract(source)


@pytest.mark.parametrize(
    "span, message",
    [
        ({"text": "Mira", "confidence": 0.9, "start": -1, "end": 3}, "outside"),
        ({"text": "Mira", "confidence": 0.9, "start": 0, "end": 5}, "does not exactly match"),
        ({"text": "Mira", "confidence": 0.9, "start": 0, "end": 4.0}, "integer character offsets"),
        ({"text": "Mira", "confidence": 1.1, "start": 0, "end": 4}, "confidence"),
    ],
)
def test_rejects_wrong_cross_source_offsets_and_invalid_confidence(
    source: SourceContext,
    schema: StructuredExtractionSchema,
    span: dict[str, Any],
    message: str,
) -> None:
    payload = _good_payload()
    payload["entities"]["person"] = [span]

    with pytest.raises((StructuredExtractionError, ValidationError), match=message):
        GLiNER25StructuredExtractor(FakeExtractor(payload), schema).extract(source)


@pytest.mark.parametrize("score", [True, float("nan"), float("inf"), float("-inf")])
@pytest.mark.parametrize("target", ["record", "relation"])
def test_record_and_relation_candidate_scores_reject_bool_and_nonfinite_values(
    source: SourceContext,
    schema: StructuredExtractionSchema,
    score: float,
    target: str,
) -> None:
    payload = _good_payload()
    if target == "record":
        payload["move_event"][0]["confidence"] = score
    else:
        payload["relation_extraction"]["moved_to"][0]["confidence"] = score

    with pytest.raises((StructuredExtractionError, ValidationError), match="confidence"):
        GLiNER25StructuredExtractor(FakeExtractor(payload), schema).extract(source)


def test_relation_proposal_stays_untrusted_until_canonical_grounding(source, schema) -> None:
    sdk = FakeExtractor(_good_payload())
    mapped = GLiNER25StructuredExtractor(sdk, schema).extract(source)

    supported = mapped.ground_claim(
        source,
        "Mira moved to Porto in 2024",
        subject="Mira",
        object="Porto",
    )
    contradicted = mapped.ground_claim(
        source,
        "Mira moved to Oslo in 2024",
        subject="Mira",
        object="Oslo",
    )

    assert mapped.relations[0].asserted is False
    assert supported.label in {"supported", "undecided"}
    assert contradicted.label in {"partly", "unsupported"}


@pytest.mark.parametrize(
    "changes",
    [
        {"scope": OwnerScope(tenant="tenant-other", owner="user-7")},
        {"scope": OwnerScope(tenant="tenant-1", owner="user-other")},
        {"sender_id": "another-sender"},
        {"text": "A different source with the same source ID."},
        {"turn": TurnRef(source_id="message-42", source_version=4, seg_index=0)},
    ],
)
def test_candidates_refuse_grounding_against_a_different_bound_source(source, schema, changes):
    mapped = GLiNER25StructuredExtractor(FakeExtractor(_good_payload()), schema).extract(source)
    alternate = source.model_copy(update=changes)

    with pytest.raises(StructuredExtractionError, match="exactly match"):
        mapped.ground_claim(alternate, "Mira moved to Porto in 2024")


@pytest.mark.parametrize("nested_field", ["scope", "turn"])
def test_candidates_snapshot_original_nested_source_before_caller_mutation(
    source: SourceContext,
    schema: StructuredExtractionSchema,
    nested_field: str,
) -> None:
    mapped = GLiNER25StructuredExtractor(FakeExtractor(_good_payload()), schema).extract(source)

    if nested_field == "scope":
        source.scope.tenant = "mutated-tenant"
    else:
        source.turn.source_version = 99

    assert source != mapped.source
    with pytest.raises(StructuredExtractionError, match="exactly match"):
        mapped.ground_claim(source, "Mira moved to Porto in 2024")


@pytest.mark.parametrize("nested_field", ["scope", "turn"])
def test_candidates_detect_mutation_of_retained_nested_source(
    source: SourceContext,
    schema: StructuredExtractionSchema,
    nested_field: str,
) -> None:
    mapped = GLiNER25StructuredExtractor(FakeExtractor(_good_payload()), schema).extract(source)

    if nested_field == "scope":
        mapped.source.scope.owner = "mutated-owner"
    else:
        mapped.source.turn.source_version = 99

    with pytest.raises(StructuredExtractionError, match="exactly match"):
        mapped.ground_claim(source, "Mira moved to Porto in 2024")


@pytest.mark.parametrize(
    "changes",
    [
        {"sender_id": ""},
        {"text": "   "},
    ],
)
def test_source_context_requires_a_real_sender_and_source_text(source, changes) -> None:
    with pytest.raises(ValidationError):
        source.model_copy(update=changes).model_validate(
            source.model_copy(update=changes).model_dump()
        )
