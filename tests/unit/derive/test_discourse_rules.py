"""RM-22 / RM-54 conservative deterministic rules (ADR-088 amendment 2026-11-02)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from memspine.derive.discourse_rules import event_ref, resolve_pending, sentence_span
from memspine.derive.non_generative_facts import (
    GroundedFactCandidate,
    compile_grounded_facts,
    mine_grounded_facts,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.semantic import PredicateTyping
from memspine.store.records import MemoryRecord, RecordFingerprint, SourceInfo

SCOPE = OwnerScope(tenant="tenant", owner="a")
IST = timezone(timedelta(hours=5, minutes=30))


def _msg(
    text: str,
    *,
    sender: str = "Asha",
    at: datetime | None = datetime(2026, 10, 1, 10, 0, tzinfo=IST),
    record_id: str = "rec-1",
    message_id: str = "m-1",
    reply_to: str | None = None,
) -> MemoryRecord:
    message: dict[str, object] = {"message_id": message_id, "sender": {"id": sender}}
    if reply_to is not None:
        message["reply_to"] = reply_to
    return MemoryRecord(
        record_id=record_id,
        namespace="a",
        memory_type="episodic",
        content=text,
        source_at=at,
        source=SourceInfo(role="user", channel="messages", message=message),
    )


def _compiled(record: MemoryRecord) -> tuple[list[tuple[str, str, str]], list[str]]:
    result = mine_grounded_facts(record, SCOPE)
    out = compile_grounded_facts(result, [record], SCOPE)
    claims = [
        (
            e.subject.surface if e.subject else "",
            str(e.attribute),
            e.object.surface if e.object else "",
        )
        for e in out.envelopes
    ]
    return claims, [u.reason for u in out.unresolved]


# ------------------------------------------------------------------ moves and visits


def test_move_binds_origin_destination_and_month_precision() -> None:
    record = _msg("I moved from Porto to Lisbon last month.")
    result = compile_grounded_facts(mine_grounded_facts(record, SCOPE), [record], SCOPE)
    by = {str(e.attribute): e for e in result.envelopes}
    assert set(by) == {"move_origin", "move_destination", "resides_in"}
    assert (by["move_origin"].object.surface, by["move_destination"].object.surface) == (  # type: ignore[union-attr]
        "Porto",
        "Lisbon",
    )
    dest = by["move_destination"]
    assert dest.assessment.time_precision == "month"
    assert dest.time.event is not None and dest.time.event.start is not None
    start = dest.time.event.start
    assert start.at == datetime(2026, 9, 1, tzinfo=IST) and start.basis == "source_time"
    # the residence is not dated by the move (no day invented)
    assert by["resides_in"].assessment.time_precision == "unknown"


def test_move_from_a_timezone_free_source_keeps_the_phrase_unresolved() -> None:
    record = _msg("I moved from Porto to Lisbon last month.", at=None)
    result = compile_grounded_facts(mine_grounded_facts(record, SCOPE), [record], SCOPE)
    dest = next(e for e in result.envelopes if e.attribute == "move_destination")
    assert dest.assessment.time_precision == "unknown"
    assert dest.time.event is not None and dest.time.event.start is not None
    assert (dest.time.event.start.text, dest.time.event.start.at) == ("last month", None)


def test_event_ref_precisions() -> None:
    record = _msg("x")
    year = event_ref("in 2024", record)
    month = event_ref("in September 2026", record)
    bare = event_ref("in September", record)
    assert year is not None and (year.precision, year.basis) == ("year", "explicit")
    assert month is not None and month.at == datetime(2026, 9, 1, tzinfo=IST)
    assert bare is not None and (bare.at, bare.precision) == (None, "unknown")
    last_year = event_ref("last year", record)
    assert last_year is not None and last_year.at == datetime(2025, 1, 1, tzinfo=IST)


def test_move_without_two_named_places_is_ambiguous() -> None:
    claims, reasons = _compiled(_msg("I moved here from my home country four years ago."))
    assert claims == [] and reasons == ["ambiguous_move"]


def test_visit_is_its_own_event_claim_never_a_residence() -> None:
    claims, reasons = _compiled(_msg("I visited the city of Lisbon last weekend."))
    assert claims == [("Asha", "visited", "Lisbon")] and reasons == []


@pytest.mark.parametrize("place_kind", ["city", "town", "village", "country", "island"])
def test_explicit_place_visit_is_source_bound(place_kind: str) -> None:
    record = _msg(f"I visited the {place_kind} of Brindle last weekend.")
    result = compile_grounded_facts(mine_grounded_facts(record, SCOPE), [record], SCOPE)
    [env] = result.envelopes
    assert env.attribute == "visited"
    assert env.object is not None and env.object.surface == "Brindle"
    assert env.registry is not None and env.registry.object_entity_type == "place"
    assert env.evidence[0].span is not None
    assert env.evidence[0].span.quote == record.content
    assert not result.unresolved


@pytest.mark.parametrize("name", ["Alex", "Lisbon", "Jordan"])
def test_bare_visit_target_has_unresolved_type(name: str) -> None:
    claims, reasons = _compiled(_msg(f"I visited {name} last weekend."))
    assert claims == [] and reasons == ["ambiguous_visit_object"]


@pytest.mark.parametrize(
    ("relation", "name"),
    [("friend", "Alex"), ("aunt", "Maya"), ("colleague", "Alex")],
)
def test_explicit_person_visit_does_not_become_a_place(relation: str, name: str) -> None:
    claims, reasons = _compiled(_msg(f"I visited my {relation} {name} last weekend."))
    assert claims == [] and reasons == ["visit_object_not_place"]


def test_possessive_visit_does_not_type_its_owner_as_a_place() -> None:
    claims, reasons = _compiled(_msg("I visited Alex's flat last weekend."))
    assert claims == [] and reasons == ["ambiguous_visit_object"]


def test_untyped_visit_annotation_keeps_exact_source_span() -> None:
    record = _msg("I visited Jordan last weekend.")
    result = compile_grounded_facts(mine_grounded_facts(record, SCOPE), [record], SCOPE)
    assert not result.envelopes
    [note] = result.unresolved
    assert note.reason == "ambiguous_visit_object"
    assert (note.source_id, note.source_version) == (record.record_id, record.version)
    assert note.span is not None and (note.span.start, note.span.end) == (0, len(record.content))


@pytest.mark.parametrize(
    "text",
    [
        "Maybe I visited the city of Lisbon.",
        "If I visited the city of Lisbon, I would know.",
        "Did I visit the city of Lisbon?",
        "I never visited the city of Lisbon.",
        "I did not visit the city of Lisbon.",
        "I visited the city of Brindle, hypothetically speaking.",
        "I visited the city of Brindle in a dream.",
        "I visited the city of Brindle, but not really.",
        "I visited the city of Brindle only in my imagination.",
        "I visited the city of Brindle, hypothetically.",
        "I visited the city of Brindle, but I did not actually go.",
        "I visited the city of Brindle, but I never went there.",
        "I visited the city of Brindle, however I didn't travel there.",
        "I visited the city of Brindle, but I did not make it.",
        "I visited the city of Brindle, but I did not go to Brindle.",
        "I visited the city of Brindle, but I never traveled to the city of Brindle.",
        "I visited the city of Brindle, but it was only in a dream.",
        "I visited the city of Brindle, but that was just in my imagination.",
        "I visited the city of Brindle, but that was hypothetical.",
        "I visited the city of Brindle, but I only imagined it.",
    ],
)
def test_hedged_visits_yield_nothing(text: str) -> None:
    assert _compiled(_msg(text)) == ([], [])


@pytest.mark.parametrize(
    "text",
    [
        "I visited the city of Brindle, but I did not stay overnight.",
        "I visited the city of Brindle, but I never saw the museum.",
        "I visited the city of Brindle and dreamed of going again.",
        "I visited the city of Brindle, but hypothetically I could live there.",
        "I visited the city of Brindle, but I did not go to the museum.",
        "I visited the city of Brindle, but I never went to the castle.",
        "I visited the city of Brindle, but I did not travel onward to Paris.",
        "I visited the city of Brindle, but I never visited Paris.",
        "I visited the city of Brindle, but the castle was only in a dream.",
        "I visited the city of Brindle, but I only imagined the castle.",
        "I visited the city of Brindle, but it was raining.",
    ],
)
def test_visit_contrast_about_another_action_stays_asserted(text: str) -> None:
    assert _compiled(_msg(text)) == ([("Asha", "visited", "Brindle")], [])


# ------------------------------------------------------------------ pronouns


def test_pronoun_binds_the_one_name_of_the_previous_sentence() -> None:
    record = _msg("I met Alex yesterday. He lives in Leeds.")
    result = compile_grounded_facts(mine_grounded_facts(record, SCOPE), [record], SCOPE)
    [env] = result.envelopes
    assert env.subject is not None and env.subject.surface == "Alex"
    assert env.provenance.principal == "Asha"  # the sender asserts it about Alex
    assert env.evidence[0].span is not None and env.evidence[0].span.quote == "He lives in Leeds."


def test_pronoun_visit_requires_an_explicit_place_object() -> None:
    claims, reasons = _compiled(_msg("I met Robin yesterday. He visited Alex."))
    assert claims == [] and reasons == ["ambiguous_visit_object"]
    claims, reasons = _compiled(_msg("I met Robin yesterday. He visited the city of Brindle."))
    assert claims == [("Robin", "visited", "Brindle")] and reasons == []
    claims, reasons = _compiled(
        _msg("I met Robin yesterday. He visited the city of Brindle in a dream.")
    )
    assert claims == [] and reasons == []
    claims, reasons = _compiled(
        _msg("I met Robin yesterday. He visited the city of Brindle, but it was only in a dream.")
    )
    assert claims == [] and reasons == []


def test_visit_person_cue_cannot_change_a_residence_object() -> None:
    claims, reasons = _compiled(_msg("I met Robin yesterday. He lives in my friend Alex."))
    assert claims == [] and reasons == []


@pytest.mark.parametrize(
    "text",
    [
        "I met Alex in Cambridge yesterday. He lives in Leeds.",  # two names
        "He lives in Leeds.",  # no earlier sentence
        "I met them yesterday. She lives in Leeds.",  # no name
    ],
)
def test_ambiguous_antecedent_stays_unresolved(text: str) -> None:
    claims, reasons = _compiled(_msg(text))
    assert claims == [] and reasons == ["ambiguous_antecedent"]


# ------------------------------------------------------------------ measurements


def test_typed_measurements_keep_unit_currency_and_decimal() -> None:
    for text, key, unit, currency, amount in (
        ("The hike is 5 km long.", "length", "km", None, Decimal("5")),
        ("The lake is 2.5 km away.", "distance", "km", None, Decimal("2.5")),
        ("The ticket costs EUR 12.34.", "price", None, "EUR", Decimal("12.34")),
        ("The ticket costs 12.34 USD.", "price", None, "USD", Decimal("12.34")),
    ):
        record = _msg(text)
        [env] = compile_grounded_facts(
            mine_grounded_facts(record, SCOPE), [record], SCOPE
        ).envelopes
        typing = env.registry
        assert typing is not None
        assert (typing.attribute_key, typing.unit, typing.currency, typing.amount) == (
            key,
            unit,
            currency,
            amount,
        )
        assert isinstance(typing.amount, Decimal)
        assert env.evidence[0].span is not None and env.evidence[0].span.quote == text


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("The hike is about 5 km long.", "approximate_value"),
        ("The ticket costs 12.34.", "missing_currency"),
        ("The ticket costs €12.34.", "missing_currency"),
        ("The ticket costs 12 eur.", "missing_currency"),
        ("The hike is 5 kg long.", "missing_unit"),
        ("The hike is 5 km.", "ambiguous_measurement"),
    ],
)
def test_unclear_measurements_stay_unresolved(text: str, reason: str) -> None:
    claims, reasons = _compiled(_msg(text))
    assert claims == [] and reasons == [reason]


def test_sentence_span_keeps_decimal_points_inside() -> None:
    text = "Hi. The ticket costs EUR 12.34. Bye."
    start = text.index("The")
    span = sentence_span(text, start, text.index("12.34") + 5)
    assert span is not None and span.quote == "The ticket costs EUR 12.34."


# ------------------------------------------------------------------ registry pass-through


def _measure_candidate(record: MemoryRecord, **typed: object) -> GroundedFactCandidate:
    base: dict[str, object] = {
        "scope": SCOPE,
        "subject": "hike",
        "subject_type": "event",
        "predicate": "has_measurement",
        "object": "5",
        "object_kind": "number",
        "modality": "asserted",
        "assertion_owner": "Asha",
        "evidence": mine_grounded_facts(record, SCOPE).facts[0].evidence,
        "cardinality_scope": {"valid_time": "2026-10-01", "context": "trip"},
    }
    return GroundedFactCandidate.model_validate({**base, **typed})


def test_compiler_passes_typed_values_to_the_registry() -> None:
    from memspine.derive.non_generative_facts import FactStageResult

    record = _msg("The hike is 5 km long.")
    good = _measure_candidate(record, attribute_key="distance", unit="km", amount=Decimal("5"))
    for bad in (
        _measure_candidate(record, attribute_key="distance", unit="kg", amount=Decimal("5")),
        _measure_candidate(record, attribute_key="distance", amount=Decimal("5")),
        _measure_candidate(record, attribute_key="distance", unit="km", amount=Decimal("6")),
        _measure_candidate(record, unit="km", amount=Decimal("5")),
    ):
        out = compile_grounded_facts(FactStageResult(facts=(bad,)), [record], SCOPE)
        assert not out.envelopes
        assert [u.reason for u in out.unresolved] == ["semantic_validation_failed"]
    out = compile_grounded_facts(FactStageResult(facts=(good,)), [record], SCOPE)
    [env] = out.envelopes
    assert env.registry is not None and env.registry.cardinality_scope["attribute_key"] == (
        "distance"
    )
    assert env.kind == "event"  # an append-only measurement never supersedes another


def test_predicate_typing_rejects_a_unit_without_an_amount() -> None:
    with pytest.raises(ValueError, match="typed amount"):
        PredicateTyping(
            predicate="has_measurement",
            predicate_version=1,
            registry_digest="0" * 64,
            subject_type="event",
            object_kind="number",
            cardinality="contextual",
            unit="km",
        )


# ------------------------------------------------------------------ corrections and cancellations


def _claim(
    record_id: str,
    source: MemoryRecord,
    *,
    owner: str = "Asha",
    obj: str = "Lisbon",
    predicate: str = "resides_in",
) -> MemoryRecord:
    result = mine_grounded_facts(_msg(f"I live in {obj}."), SCOPE) if predicate else None
    assert result is not None
    candidate = result.facts[0].model_copy(
        update={
            "assertion_owner": owner,
            "subject": owner,
            "evidence": result.facts[0].evidence.model_copy(
                update={
                    "turn": result.facts[0].evidence.turn.model_copy(  # type: ignore[union-attr]
                        update={"source_id": source.record_id}
                    )
                }
            ),
        }
    )
    from memspine.derive.non_generative_facts import FactStageResult

    [env] = compile_grounded_facts(FactStageResult(facts=(candidate,)), [source], SCOPE).envelopes
    return MemoryRecord(
        record_id=record_id,
        namespace="a",
        memory_type="semantic",
        content=env.text,
        source=SourceInfo(role="assistant", channel="mining", parents=[source.record_id]),
        fingerprint=RecordFingerprint(envelope=env.model_dump(mode="json")),
    )


def _messages(*records: MemoryRecord) -> dict[str, str]:
    return {r.record_id: str((r.source.message or {}).get("message_id")) for r in records}


def test_correction_resolves_to_the_senders_one_matching_claim() -> None:
    lisbon = _msg("I live in Lisbon.", record_id="src-1", message_id="x-1")
    fix = _msg("Correction, I meant Coimbra, not Lisbon.", record_id="src-2", reply_to="x-1")
    [pending] = mine_grounded_facts(fix, SCOPE).pending
    claims = [_claim("claim-1", lisbon)]
    names = _messages(lisbon, fix)
    candidate, note, extra = resolve_pending(
        pending, claims, message_of=names.get, records={"src-2": fix}
    )
    assert note is None and extra == [] and candidate is not None
    assert (candidate.predicate, candidate.object, candidate.subject) == (
        "resides_in",
        "Coimbra",
        "Asha",
    )
    assert candidate.target is not None and candidate.target.record_id == "claim-1"
    assert candidate.target.action == "supersede"


def test_correction_never_targets_another_principal_or_guesses() -> None:
    lisbon = _msg("I live in Lisbon.", record_id="src-1", sender="Bob")
    fix = _msg("Correction, I meant Coimbra, not Lisbon.", record_id="src-2")
    [pending] = mine_grounded_facts(fix, SCOPE).pending
    names = _messages(lisbon, fix)
    _, note, _ = resolve_pending(
        pending, [_claim("c-1", lisbon, owner="Bob")], message_of=names.get, records={}
    )
    assert note is not None and note.reason == "correction_target_not_found"
    two = [_claim(f"c-{n}", _msg("I live in Lisbon.", record_id=f"s-{n}")) for n in (1, 2)]
    _, note, _ = resolve_pending(pending, two, message_of=names.get, records={})
    assert note is not None and note.reason == "correction_target_ambiguous"


def test_unreadable_correction_stays_unresolved() -> None:
    result = mine_grounded_facts(_msg("I work for Acme, correction, I meant Contoso."), SCOPE)
    assert not result.facts and not result.pending
    assert [u.reason for u in result.unresolved] == ["correction_requires_resolution"]


def test_cancellation_is_pending_with_declined_or_missed() -> None:
    [cancel] = mine_grounded_facts(_msg("I cancel my quarterly report commitment."), SCOPE).pending
    [missed] = mine_grounded_facts(
        _msg("I missed the deadline for my quarterly report commitment."), SCOPE
    ).pending
    assert (cancel.kind, cancel.old_value, cancel.outcome) == (
        "cancellation",
        "quarterly report",
        "declined",
    )
    assert missed.outcome == "missed"


def test_cancellation_without_a_commitment_is_not_found() -> None:
    record = _msg("I cancel my quarterly report commitment.")
    [pending] = mine_grounded_facts(record, SCOPE).pending
    _, note, _ = resolve_pending(pending, [], message_of=lambda _: None, records={})
    assert note is not None and note.reason == "cancellation_target_not_found"


# ------------------------------------------------------------------ documents


def _chunk(text: str) -> MemoryRecord:
    return MemoryRecord(
        record_id="chunk-1",
        namespace="a",
        memory_type="resource",
        content=text,
        source=SourceInfo(
            role="user", channel="ingest", doc_path="/notes/RELEASE-4.0.md", message_id="chunk:0"
        ),
    )


def test_document_statement_is_reported_and_attributed_to_the_document() -> None:
    chunk = _chunk("Release 4.0: Project X requires version 4 of the runtime.")
    out = compile_grounded_facts(mine_grounded_facts(chunk, SCOPE), [chunk], SCOPE)
    [env] = out.envelopes
    assert env.assessment.modality == "reported" and not env.asserts_fact
    assert env.provenance.principal == "document:RELEASE-4.0.md"
    assert env.src_class == "document"
    assert [b.role_name for b in env.role_bindings] == ["resource"]


def test_document_chunk_is_not_a_user_source() -> None:
    from memspine.derive.non_generative_facts import source_ineligible

    gate = source_ineligible(_chunk("I live in Lisbon."))
    assert gate is not None and gate.unresolved[0].reason == "source_not_user"
    # and a document's first-person text never becomes a user fact
    assert not mine_grounded_facts(_chunk("I live in Lisbon."), SCOPE).facts


def test_a_reported_claim_from_a_message_is_never_compiled() -> None:
    from memspine.derive.non_generative_facts import FactStageResult

    record = _msg("I live in Lisbon.")
    fact = mine_grounded_facts(record, SCOPE).facts[0].model_copy(update={"modality": "reported"})
    out = compile_grounded_facts(FactStageResult(facts=(fact,)), [record], SCOPE)
    assert not out.envelopes
