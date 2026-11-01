"""DS-18 (ADR-090 item 6): a non-generative GLiNER2 fact extractor for the window path.

GLiNER2's structured extraction (D-94 / ADR-072) proposes typed, source-cited relation
candidates for ONE source turn (:class:`~memspine.derive.gliner_structured.
GLiNER25StructuredExtractor`). This module turns a candidate into a
:class:`~memspine.derive.non_generative_facts.GroundedFactCandidate` only when

* the source passes the shared eligibility gate (user-authored, a sender id, no correction cue);
* the relation maps to a registry predicate (``lives_in`` -> ``resides_in``, ``works_for`` ->
  ``employed_by``);
* the relation's head is the speaker (a first-person pronoun or the sender's own name): the
  subject role binds to the assertion owner, never to a third party;
* head and tail sit in one sentence, which becomes the exact support span;
* the canonical rule checker grounds the relation's claim against the pinned source
  (``StructuredCandidates.ground_claim``): negated, hypothetical, reported or questioned
  statements are not ``supported`` and stay unresolved.

Everything else is an unresolved candidate with its reason and span, never a fact. The result
then goes through the same canonical compile and validation as the rules-only producer
(``compile_grounded_facts``). The entity spans are returned as unasserted mentions for the
source's annotation batch (ADR-076): they never key a fact. No model generates text here.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Any, Protocol, TypeVar

from memspine.derive.gliner_mentions import MentionSpan
from memspine.derive.gliner_structured import (
    GLiNER25StructuredExtractor,
    RelationCandidate,
    RelationSchema,
    SourceContext,
    StructuredCandidates,
    StructuredExtractionError,
    StructuredExtractionSchema,
)
from memspine.derive.non_generative_facts import (
    FactStageResult,
    GroundedFactCandidate,
    UnresolvedFactCandidate,
    UnresolvedReason,
    assertion_owner,
    source_ineligible,
    support_span,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.semantics.specs import EntityType, ValueKind
from memspine.store.records import MemoryRecord

__all__ = [
    "GLINER_FACT_REVISION",
    "GLINER_FACT_SCHEMA",
    "RELATION_PREDICATES",
    "GlinerFactExtractor",
]

T = TypeVar("T")

#: The producer revision of every candidate this extractor yields.
GLINER_FACT_REVISION = "gliner-structured-facts:1"

#: The structured schema: speaker relations the registry can type.
GLINER_FACT_SCHEMA = StructuredExtractionSchema(
    entity_types=("person", "location", "organization"),
    relations=(
        RelationSchema(
            name="lives_in",
            head_type="person",
            tail_type="location",
            description="a person lives in a place",
        ),
        RelationSchema(
            name="works_for",
            head_type="person",
            tail_type="organization",
            description="a person works for an organization",
        ),
    ),
)


#: relation -> (predicate, object kind, object entity type, claim template)
RELATION_PREDICATES: Mapping[str, tuple[str, ValueKind, EntityType | None, str]] = {
    "lives_in": ("resides_in", "entity", "place", "{head} live in {tail}"),
    "works_for": ("employed_by", "entity", "organisation", "{head} work for {tail}"),
}

_FIRST_PERSON = frozenset({"i", "me", "myself"})
_SPACE = re.compile(r"\s+")


class _Binding(Protocol):
    async def call(self, callback: Callable[[Any], T]) -> T: ...


def _norm(text: str) -> str:
    return _SPACE.sub(" ", text).strip().casefold()


class GlinerFactExtractor:
    """The configured, non-generative fact extractor (``generative = False``) behind the
    window path's extractor hook. ``model`` is the GLiNER binding of the semantic extraction
    slot (its ``call`` runs one synchronous SDK operation off the event loop)."""

    generative = False

    def __init__(
        self,
        model: _Binding,
        *,
        model_id: str,
        schema: StructuredExtractionSchema = GLINER_FACT_SCHEMA,
    ) -> None:
        unknown = {r.name for r in schema.relations} - set(RELATION_PREDICATES)
        if unknown:
            raise ValueError(f"no registry predicate for relations {sorted(unknown)}")
        self._model = model
        self._schema = schema
        self.model_id = f"{model_id}+{GLINER_FACT_REVISION}"

    async def extract_grounded_facts(
        self, record: MemoryRecord, scope: OwnerScope
    ) -> FactStageResult:
        ineligible = source_ineligible(record)
        if ineligible is not None:
            return ineligible
        owner = assertion_owner(record)
        assert owner is not None
        if not record.content.strip():
            return FactStageResult()
        source = SourceContext(
            scope=scope,
            turn=TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0),
            sender_id=owner,
            text=record.content,
        )
        try:
            candidates = await self._model.call(
                lambda model: GLiNER25StructuredExtractor(model, self._schema).extract(source)
            )
        except StructuredExtractionError:
            # A payload that cannot be mapped without losing ownership: nothing from it.
            return FactStageResult(unresolved=(self._unresolved(record, "extractor_failed"),))
        facts: list[GroundedFactCandidate] = []
        unresolved: list[UnresolvedFactCandidate] = []
        for relation in candidates.relations:
            outcome = self._candidate(record, scope, owner, source, candidates, relation)
            if isinstance(outcome, GroundedFactCandidate):
                facts.append(outcome)
            else:
                unresolved.append(outcome)
        mentions = tuple(
            MentionSpan(
                text=m.text,
                label=m.type_label,
                start=m.span.start,
                end=m.span.end,
                confidence=m.confidence,
            )
            for m in candidates.mentions
        )
        return FactStageResult(facts=tuple(facts), unresolved=tuple(unresolved), mentions=mentions)

    def _candidate(
        self,
        record: MemoryRecord,
        scope: OwnerScope,
        owner: str,
        source: SourceContext,
        candidates: StructuredCandidates,
        relation: RelationCandidate,
    ) -> GroundedFactCandidate | UnresolvedFactCandidate:
        head, tail = relation.head, relation.tail
        start, end = min(head.span.start, tail.span.start), max(head.span.end, tail.span.end)
        rough = Span(start=start, end=end)
        mapped = RELATION_PREDICATES.get(relation.relation_type)
        if mapped is None:
            return self._unresolved(record, "unsupported_relation", relation, rough)
        predicate, object_kind, object_type, template = mapped
        if _norm(head.text) not in _FIRST_PERSON and _norm(head.text) != _norm(owner):
            # A relation about someone else: its subject role does not bind to the speaker.
            return self._unresolved(record, "subject_not_speaker", relation, rough, predicate)
        support = support_span(record.content, start, end)
        between = any(
            m.type_label == head.type_label
            and start < m.span.start
            and m.span.end < end
            and m.span != head.span
            for m in candidates.mentions
        )
        if support is None or between:
            # Another sentence or another person between the endpoints: the pairing is not
            # provably the speaker's.
            return self._unresolved(record, "ambiguous_support_span", relation, rough, predicate)
        # Two canonical checks: the relation's own claim (polarity: "I do not live in X" fails
        # it) and the whole support sentence as an asserted claim (modality cues: a condition,
        # a question or reported speech fail it).
        labels = {
            candidates.ground_claim(
                source, claim, subject=head.text, object=tail.text, modality="asserted"
            ).label
            for claim in (template.format(head=head.text, tail=tail.text), str(support.quote))
        }
        if labels != {"supported"}:
            return self._unresolved(record, "grounding_unsupported", relation, support, predicate)
        return GroundedFactCandidate(
            scope=scope,
            subject=owner,
            predicate=predicate,
            object=tail.text,
            object_kind=object_kind,
            object_entity_type=object_type,
            modality="asserted",
            assertion_owner=owner,
            evidence=Evidence(
                role="support",
                turn=source.turn,
                span=support,
                source_family=owner,
            ),
            source_time=(record.source_at or record.valid_from).isoformat(),
            producer_revision=GLINER_FACT_REVISION,
        )

    @staticmethod
    def _unresolved(
        record: MemoryRecord,
        reason: UnresolvedReason,
        relation: RelationCandidate | None = None,
        span: Span | None = None,
        attribute: str | None = None,
    ) -> UnresolvedFactCandidate:
        return UnresolvedFactCandidate(
            source_id=record.record_id,
            source_version=record.version,
            reason=reason,
            attribute=attribute or (relation.relation_type if relation is not None else None),
            value=relation.tail.text if relation is not None else None,
            span=span,
            producer_revision=GLINER_FACT_REVISION,
        )
