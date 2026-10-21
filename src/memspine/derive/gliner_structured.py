"""Optional GLiNER2.5 structured candidate extraction.

This adapter stops at typed, source-cited candidates. It does not create or
write ``AssertionEnvelope`` records: mentions and relation proposals must pass
the existing canonical grounding and integration stages before persistence.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memspine.derive.ground import (
    Cite,
    CitedSpan,
    Claim,
    GroundingResult,
    WindowTurn,
    check_grounding,
    turn_key,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef

__all__ = [
    "FieldSchema",
    "GLiNER25StructuredExtractor",
    "RecordCandidate",
    "RecordSchema",
    "RelationCandidate",
    "RelationSchema",
    "SourceContext",
    "StructuredExtractionError",
    "StructuredExtractionSchema",
    "TypedFieldCandidate",
]

_LABEL = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_RESERVED_NAMES = frozenset(
    {
        "assertion_key",
        "confidence",
        "grounding",
        "namespace",
        "owner",
        "owner_id",
        "record_id",
        "sender",
        "sender_id",
        "speaker_id",
        "source_id",
        "source_version",
        "status",
        "scope",
        "tenant",
        "trust",
        "write_id",
    }
)


class StructuredExtractionError(ValueError):
    """A schema or model payload cannot be mapped without losing ownership."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FieldSchema(_StrictModel):
    name: str = Field(min_length=1, max_length=64)
    type_label: str = Field(min_length=1, max_length=64)
    description: str = ""
    multiple: bool = False
    required: bool = False
    threshold: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("name", "type_label")
    @classmethod
    def _safe_label(cls, value: str) -> str:
        if not _LABEL.fullmatch(value) or value in _RESERVED_NAMES:
            raise ValueError(f"invalid or reserved schema label {value!r}")
        return value


class RecordSchema(_StrictModel):
    name: str = Field(min_length=1, max_length=64)
    description: str = ""
    fields: tuple[FieldSchema, ...] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _safe_name(cls, value: str) -> str:
        if not _LABEL.fullmatch(value) or value in _RESERVED_NAMES:
            raise ValueError(f"invalid or reserved schema label {value!r}")
        return value

    @model_validator(mode="after")
    def _unique_fields(self) -> RecordSchema:
        names = [item.name for item in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("record field names must be unique")
        return self


class RelationSchema(_StrictModel):
    name: str = Field(min_length=1, max_length=64)
    head_type: str = Field(min_length=1, max_length=64)
    tail_type: str = Field(min_length=1, max_length=64)
    description: str = ""

    @field_validator("name", "head_type", "tail_type")
    @classmethod
    def _safe_label(cls, value: str) -> str:
        if not _LABEL.fullmatch(value) or value in _RESERVED_NAMES:
            raise ValueError(f"invalid or reserved schema label {value!r}")
        return value


class StructuredExtractionSchema(_StrictModel):
    entity_types: tuple[str, ...] = ()
    records: tuple[RecordSchema, ...] = ()
    relations: tuple[RelationSchema, ...] = ()
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)

    @field_validator("entity_types")
    @classmethod
    def _unique_entity_types(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for name in value:
            if not _LABEL.fullmatch(name) or name in _RESERVED_NAMES:
                raise ValueError(f"invalid or reserved entity type {name!r}")
        if len(value) != len(set(value)):
            raise ValueError("entity types must be unique")
        return value

    @model_validator(mode="after")
    def _unique_names_and_relation_types(self) -> StructuredExtractionSchema:
        names = [*(record.name for record in self.records), *(rel.name for rel in self.relations)]
        if len(names) != len(set(names)):
            raise ValueError("record and relation names must be unique")
        available = set(self.entity_types)
        if any(
            rel.head_type not in available or rel.tail_type not in available
            for rel in self.relations
        ):
            raise ValueError("relation endpoints must use declared entity types")
        if not self.records and not self.relations and not self.entity_types:
            raise ValueError("at least one entity, record or relation schema is required")
        return self

    def to_sdk_schema(self) -> dict[str, Any]:
        """Build an intermediate schema dict for conversion via the SDK Schema API."""
        out: dict[str, Any] = {}
        if self.entity_types:
            out["entities"] = list(self.entity_types)
        if self.records:
            out["structures"] = {
                record.name: {
                    "mode": "natural",
                    "anchor": record.fields[0].name,
                    "fields": [
                        {
                            "name": field.name,
                            "dtype": "list" if field.multiple else "str",
                            **({"description": field.description} if field.description else {}),
                            **(
                                {"threshold": field.threshold}
                                if field.threshold is not None
                                else {}
                            ),
                            **(
                                {"cardinality": "one_or_more" if field.multiple else "required_one"}
                                if field.required
                                else {}
                            ),
                        }
                        for field in record.fields
                    ],
                }
                for record in self.records
            }
        if self.relations:
            out["relations"] = {rel.name: rel.description for rel in self.relations}
        return out


class SourceContext(_StrictModel):
    """Authoritative owner and source metadata for exactly one source turn."""

    scope: OwnerScope
    turn: TurnRef
    sender_id: str = Field(min_length=1, max_length=256)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def _has_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("source text must not be blank")
        return value

    @property
    def source_family(self) -> str:
        return self.sender_id

    @property
    def turn_key(self) -> str:
        return turn_key(self.turn)

    def evidence(self, span: Span) -> Evidence:
        return Evidence(
            role="support",
            turn=self.turn,
            span=span,
            source_family=self.source_family,
        )


class TypedFieldCandidate(_StrictModel):
    field_name: str
    type_label: str
    text: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    span: Span
    evidence: Evidence


class RecordCandidate(_StrictModel):
    """Untrusted model proposal with immutable source ownership attached."""

    schema_name: str
    scope: OwnerScope
    source_id: str
    source_version: int = Field(ge=1)
    sender_id: str
    fields: tuple[TypedFieldCandidate, ...]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    asserted: Literal[False] = False


class RelationCandidate(_StrictModel):
    """An untrusted relation proposal with its pair score, when the SDK returns one."""

    relation_type: str
    scope: OwnerScope
    source_id: str
    source_version: int = Field(ge=1)
    sender_id: str
    head: TypedFieldCandidate
    tail: TypedFieldCandidate
    relation_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    asserted: Literal[False] = False


class StructuredCandidates(_StrictModel):
    source: SourceContext
    source_fingerprint: str = Field(min_length=64, max_length=64)
    records: tuple[RecordCandidate, ...] = ()
    relations: tuple[RelationCandidate, ...] = ()
    mentions: tuple[TypedFieldCandidate, ...] = ()

    def ground_claim(
        self,
        source: SourceContext,
        text: str,
        *,
        subject: str | None = None,
        object: str | None = None,
        modality: Literal[
            "asserted", "negated", "hypothetical", "planned", "reported", "question"
        ] = "asserted",
    ) -> GroundingResult:
        """Run caller-authored claim text through MemSpine's canonical rule checker.

        Extraction candidates alone never become assertions. Callers supply the
        complete claim to ground; this helper cites the exact candidate spans.
        """
        if (
            _source_fingerprint(source) != self.source_fingerprint
            or _source_fingerprint(self.source) != self.source_fingerprint
        ):
            raise StructuredExtractionError(
                "grounding source must exactly match the source bound to these candidates"
            )
        candidates = [*self.mentions]
        for record in self.records:
            candidates.extend(record.fields)
        for relation in self.relations:
            candidates.extend((relation.head, relation.tail))
        spans = {
            (candidate.span.start, candidate.span.end)
            for candidate in candidates
            if candidate.evidence.turn == self.source.turn
        }
        cites = tuple(
            Cite(
                role="support",
                turn=self.source.turn_key,
                span=CitedSpan(start=start, end=end, quote=self.source.text[start:end]),
            )
            for start, end in sorted(spans)
        )
        return check_grounding(
            Claim(text=text, subject=subject, object=object, modality=modality, cites=cites),
            {
                self.source.turn_key: WindowTurn(
                    key=self.source.turn_key,
                    text=self.source.text,
                    speaker=self.source.sender_id,
                )
            },
        )


class _Extractor(Protocol):
    def extract(
        self,
        text: str,
        schema: Any,
        *,
        threshold: float,
        include_confidence: bool,
        include_spans: bool,
    ) -> Mapping[str, Any]: ...


class GLiNER25StructuredExtractor:
    """Map one-turn SDK output to typed candidates without opening a write path."""

    def __init__(self, extractor: _Extractor, schema: StructuredExtractionSchema) -> None:
        self._extractor = extractor
        self.schema = schema

    def extract(self, source: SourceContext) -> StructuredCandidates:
        """Extract candidates from one independently-owned turn.

        A batch/window adapter is deliberately not provided: callers must keep
        source text and offsets pinned to one source turn until a multi-turn
        mapping can prove each span's owner.
        """
        # SourceContext is frozen, but its nested durable values are mutable.
        # Snapshot before invoking the model so caller mutation cannot rebind
        # evidence or citation text after extraction.
        source = source.model_copy(deep=True)
        schema_arg: Any = self.schema.to_sdk_schema()
        if hasattr(self._extractor, "create_schema"):
            try:
                from gliner2 import Schema
            except ImportError as exc:  # pragma: no cover - loaded model owns the optional SDK
                raise RuntimeError(
                    "GLiNER2 model is loaded but its Schema API is unavailable"
                ) from exc
            schema_arg = Schema.from_dict(schema_arg)
        payload = self._extractor.extract(
            source.text,
            schema_arg,
            threshold=self.schema.threshold,
            include_confidence=True,
            include_spans=True,
        )
        if not isinstance(payload, Mapping):
            raise StructuredExtractionError("GLiNER2.5 returned a non-mapping payload")
        allowed_top = {
            "entities",
            "relation_extraction",
            *(record.name for record in self.schema.records),
        }
        extras = set(payload) - allowed_top
        if extras:
            raise StructuredExtractionError(f"unexpected SDK result keys: {sorted(extras)}")
        return self._map_payload(payload, source)

    def _map_payload(
        self, payload: Mapping[str, Any], source: SourceContext
    ) -> StructuredCandidates:
        mentions = self._map_entities(payload.get("entities", {}), source)
        records = self._map_records(payload, source)
        relations = self._map_relations(payload.get("relation_extraction", {}), mentions, source)
        return StructuredCandidates(
            source=source,
            source_fingerprint=_source_fingerprint(source),
            records=tuple(records),
            relations=tuple(relations),
            mentions=tuple(mentions),
        )

    def _map_entities(self, raw: Any, source: SourceContext) -> list[TypedFieldCandidate]:
        if not isinstance(raw, Mapping):
            raise StructuredExtractionError("entities must be a type-to-span mapping")
        allowed = set(self.schema.entity_types)
        if set(raw) - allowed:
            raise StructuredExtractionError(
                f"unsupported entity labels: {sorted(set(raw) - allowed)}"
            )
        out: list[TypedFieldCandidate] = []
        for label, values in raw.items():
            for value in _as_values(values):
                out.append(_field_candidate(str(label), str(label), value, source))
        return out

    def _map_records(
        self, payload: Mapping[str, Any], source: SourceContext
    ) -> list[RecordCandidate]:
        out: list[RecordCandidate] = []
        for record_schema in self.schema.records:
            raw_records = payload.get(record_schema.name, [])
            if not isinstance(raw_records, list):
                raise StructuredExtractionError(f"{record_schema.name} must be a list of records")
            known_fields = {field.name: field for field in record_schema.fields}
            for raw_record in raw_records:
                if not isinstance(raw_record, Mapping):
                    raise StructuredExtractionError(f"{record_schema.name} item must be a mapping")
                if set(raw_record) - {*known_fields, "confidence"}:
                    raise StructuredExtractionError(
                        f"unexpected fields for {record_schema.name}: {sorted(set(raw_record) - set(known_fields))}"
                    )
                fields: list[TypedFieldCandidate] = []
                for name, field_schema in known_fields.items():
                    raw_value = raw_record.get(name)
                    values = _as_values(raw_value)
                    if field_schema.required and not values:
                        raise StructuredExtractionError(
                            f"required field {record_schema.name}.{name} is missing"
                        )
                    if not field_schema.multiple and len(values) > 1:
                        raise StructuredExtractionError(
                            f"scalar field {record_schema.name}.{name} returned multiple spans"
                        )
                    fields.extend(
                        _field_candidate(name, field_schema.type_label, value, source)
                        for value in values
                    )
                if fields:
                    out.append(
                        RecordCandidate(
                            schema_name=record_schema.name,
                            scope=source.scope,
                            source_id=source.turn.source_id,
                            source_version=source.turn.source_version,
                            sender_id=source.sender_id,
                            fields=tuple(fields),
                            confidence=(
                                _confidence(raw_record["confidence"], "record")
                                if "confidence" in raw_record
                                else None
                            ),
                        )
                    )
        return out

    def _map_relations(
        self,
        raw: Any,
        mentions: Sequence[TypedFieldCandidate],
        source: SourceContext,
    ) -> list[RelationCandidate]:
        if not isinstance(raw, Mapping):
            raise StructuredExtractionError("relation_extraction must be a type-to-pairs mapping")
        schemas = {item.name: item for item in self.schema.relations}
        if set(raw) - set(schemas):
            raise StructuredExtractionError(
                f"unsupported relation labels: {sorted(set(raw) - set(schemas))}"
            )
        out: list[RelationCandidate] = []
        for name, pairs in raw.items():
            if not isinstance(pairs, list):
                raise StructuredExtractionError(f"relation {name} must map to a list")
            spec = schemas[name]
            for pair in pairs:
                if (
                    not isinstance(pair, Mapping)
                    or set(pair)
                    - {
                        "head",
                        "tail",
                        "confidence",
                    }
                    or not {"head", "tail"}.issubset(pair)
                ):
                    raise StructuredExtractionError(
                        f"relation {name} needs head and tail endpoints"
                    )
                head = _field_candidate("head", spec.head_type, pair["head"], source)
                tail = _field_candidate("tail", spec.tail_type, pair["tail"], source)
                if not _has_typed_mention(mentions, head):
                    raise StructuredExtractionError(
                        f"relation {name} head is not a typed source mention"
                    )
                if not _has_typed_mention(mentions, tail):
                    raise StructuredExtractionError(
                        f"relation {name} tail is not a typed source mention"
                    )
                out.append(
                    RelationCandidate(
                        relation_type=name,
                        scope=source.scope,
                        source_id=source.turn.source_id,
                        source_version=source.turn.source_version,
                        sender_id=source.sender_id,
                        head=head,
                        tail=tail,
                        relation_confidence=(
                            _confidence(pair["confidence"], "relation")
                            if "confidence" in pair
                            else None
                        ),
                    )
                )
        return out


def _as_values(value: Any) -> list[Mapping[str, Any]]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    if any(not isinstance(item, Mapping) for item in values):
        raise StructuredExtractionError("span values must include text, confidence and offsets")
    return values


def _confidence(value: Any, kind: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StructuredExtractionError(f"{kind} confidence must be numeric")
    result = float(value)
    if not 0.0 <= result <= 1.0:
        raise StructuredExtractionError(f"{kind} confidence must be between zero and one")
    return result


def _source_fingerprint(source: SourceContext) -> str:
    payload = json.dumps(
        source.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _field_candidate(
    field_name: str, type_label: str, raw: Mapping[str, Any], source: SourceContext
) -> TypedFieldCandidate:
    if set(raw) != {"text", "confidence", "start", "end"}:
        raise StructuredExtractionError(
            "SDK spans must contain exactly text, confidence, start and end"
        )
    text = raw["text"]
    confidence = raw["confidence"]
    start, end = raw["start"], raw["end"]
    if not isinstance(text, str) or not text.strip():
        raise StructuredExtractionError("span text must be non-empty")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise StructuredExtractionError("span confidence must be numeric")
    if (
        isinstance(start, bool)
        or not isinstance(start, int)
        or isinstance(end, bool)
        or not isinstance(end, int)
    ):
        raise StructuredExtractionError("span offsets must be integer character offsets")
    if not (0 <= start < end <= len(source.text)):
        raise StructuredExtractionError("span offsets are outside the pinned source text")
    exact = source.text[start:end]
    if exact != text:
        raise StructuredExtractionError(
            "span text does not exactly match the pinned source offsets"
        )
    span = Span(start=start, end=end, quote=exact)
    return TypedFieldCandidate(
        field_name=field_name,
        type_label=type_label,
        text=text,
        confidence=float(confidence),
        span=span,
        evidence=source.evidence(span),
    )


def _has_typed_mention(
    mentions: Sequence[TypedFieldCandidate], endpoint: TypedFieldCandidate
) -> bool:
    return any(
        mention.type_label == endpoint.type_label
        and mention.text == endpoint.text
        and mention.span == endpoint.span
        and mention.evidence.turn == endpoint.evidence.turn
        for mention in mentions
    )
