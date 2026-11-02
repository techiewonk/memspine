"""RM-22 / RM-54 conservative deterministic rules of the rules-only producer (ADR-088
amendment 2026-11-02).

Each rule reads one authoritative source, pins every candidate to an exact sentence span and
never invents a value, a date or a target. What a rule cannot decide alone stays an
unresolved, unasserted annotation naming why:

- **move** (``I moved from X to Y [last month]``): ``move_origin`` and ``move_destination``
  claims plus the destination residence (multiple homes; nothing is replaced). The move time is
  kept at source precision (a month, a year), resolved only against an aware source time
  (D-100 Q-S2); no day is invented. A move without two named places is ``ambiguous_move``.
- **visit** (``I visited X``): a ``visited`` event claim, never a residence.
- **pronoun** (``I met Alex. He lives in Leeds.``): a third-person statement whose ``he`` /
  ``she`` subject binds to the one name of the immediately preceding sentence of the same
  source; none or several names is ``ambiguous_antecedent``.
- **measurement** (``The hike is 5 km long.``, ``The ticket costs EUR 12.34.``): a typed
  ``has_measurement`` claim with its property key, explicit unit or ISO 4217 code and the
  Decimal amount as written. An approximate value, a missing unit or currency, an unregistered
  unit or a value of no stated dimension stays unresolved; nothing is converted.
- **correction** (``I meant X, not Y``) and **cancellation** (``I cancel my X commitment``):
  a :class:`PendingClaimChange` that :func:`resolve_pending` binds to the one matching prior
  claim of the same sender, or keeps unresolved (target not found / ambiguous).
- **document** (an ingested chunk: ``Project X requires version 4``): a ``reported`` claim
  attributed to the document, never to the user who ingested it.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta, timezone, tzinfo
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from memspine.core.rule_miner import asserted_match
from memspine.derive.non_generative_facts import (
    ClaimTarget,
    GroundedFactCandidate,
    PendingClaimChange,
    UnresolvedFactCandidate,
    UnresolvedReason,
)
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, Span, TurnRef
from memspine.schemas.durable.semantic import RoleBinding, SemanticEnvelope
from memspine.schemas.durable.temporal import TemporalRef
from memspine.semantics import SemanticRegistry, load_builtin_registry
from memspine.store.records import MemoryRecord

__all__ = [
    "RULES_REVISION",
    "Mined",
    "document_rules",
    "message_rules",
    "pending_changes",
    "resolve_pending",
]

RULES_REVISION = "personal-rules:1"

#: A capitalised name of one to three words ("Lisbon", "New York", "Project X").
_NAME = r"(?-i:[A-Z][\w'-]*(?: [A-Z][\w'-]*){0,2})"
_MONTHS = (
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
)
_WHEN = (
    r"(?P<when>last month|last year|in (?:"
    + "|".join(m.capitalize() for m in _MONTHS)
    + r")(?: \d{4})?|in \d{4})"
)
_MOVE = re.compile(
    rf"\bI (?:just |recently )?moved from (?P<o>{_NAME}) to (?P<d>{_NAME})(?: {_WHEN})?\b",
    re.IGNORECASE,
)
#: A move statement the strict form does not cover ("moved here from my home country").
_MOVE_LOOSE = re.compile(r"\bI (?:just |recently )?moved (?:\w+ )?from\b", re.IGNORECASE)
_VISIT = re.compile(rf"\bI (?:just |recently )?visited (?P<v>{_NAME})", re.IGNORECASE)
_PRONOUN = re.compile(
    rf"^(?P<p>He|She) (?:(?:now |currently )?(?P<home>lives in)|(?P<work>works (?:at|for))"
    rf"|(?P<visit>visited)) (?P<v>{_NAME})\b"
)
_CAPITAL = re.compile(r"(?-i:\b[A-Z][a-z]+\b)")
_APPROX = r"(?P<approx>about |around |roughly |approximately |nearly |almost |over |under |~)?"
_AMOUNT = r"\d+(?:\.\d+)?"
_MEASURE = re.compile(
    rf"\b(?P<det>The|My|Our) (?P<s>[a-z]+(?: [a-z]+)?) (?:is|was) {_APPROX}"
    rf"(?P<n>{_AMOUNT}) ?(?P<u>[A-Za-z]+)(?: (?P<adj>long|away|tall|high|wide|deep))?"
    r"(?=[\s.,!?;]|$)",
    re.IGNORECASE,
)
_PRICE = re.compile(
    rf"\b(?P<det>The|My|Our) (?P<s>[a-z]+(?: [a-z]+)?) costs? {_APPROX}"
    rf"(?:(?P<c1>[A-Za-z]{{3}}) (?P<n1>{_AMOUNT})|(?P<n2>{_AMOUNT}) (?P<c2>[A-Za-z]{{3}})"
    rf"|(?P<sym>[$€£¥]) ?(?P<n3>{_AMOUNT})|(?P<n4>{_AMOUNT}))(?=[\s.,!?;]|$)",
    re.IGNORECASE,
)
#: dimension adjective -> registered measurement property key
_DIMENSION = {
    "long": "length",
    "away": "distance",
    "tall": "height",
    "high": "height",
    "wide": "width",
    "deep": "depth",
}
_CORRECTION = re.compile(
    rf"\bI meant (?P<new>{_NAME}),? (?:not|rather than) (?P<old>{_NAME})\b", re.IGNORECASE
)
_CANCEL = re.compile(
    r"\bI (?P<verb>cancel|cancelled|canceled|withdraw|withdrew|am cancelling|am withdrawing"
    r"|missed the deadline (?:of|for)) (?:my |the )?(?P<v>[^.,!?;:]{2,60}?) commitment\b",
    re.IGNORECASE,
)
_REQUIRES = re.compile(
    rf"(?:^|[.:;]\s+)(?P<s>{_NAME}) requires (?P<o>[^.,!?;:]{{2,60}}?)(?=\s*[.,!?;:]|$)"
)


class Mined:
    """What one rule pass over one source yields (facts, unresolved, pending changes)."""

    __slots__ = ("facts", "pending", "unresolved")

    def __init__(self) -> None:
        self.facts: list[GroundedFactCandidate] = []
        self.unresolved: list[UnresolvedFactCandidate] = []
        self.pending: list[PendingClaimChange] = []


# ------------------------------------------------------------------ helpers


def _local_tz(record: MemoryRecord) -> tzinfo | None:
    """The zone the source time was said in; None for a timezone-free source (D-100 Q-S2)."""
    at = record.source_at
    if at is None or at.utcoffset() is None:
        return None
    zone = record.source_tz
    if zone and zone != "unknown":
        try:
            return ZoneInfo(zone)
        except (ZoneInfoNotFoundError, ValueError):
            match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", zone)
            if match:
                sign = 1 if match[1] == "+" else -1
                delta = timedelta(hours=int(match[2]), minutes=int(match[3]))
                return timezone(sign * delta)
    return at.tzinfo


def event_ref(phrase: str | None, record: MemoryRecord) -> TemporalRef | None:
    """The event start a time phrase states, at its own precision. A relative phrase resolves
    only against an aware source time; a timezone-free source keeps the phrase as text."""
    if not phrase:
        return None
    text = " ".join(phrase.split())
    zone = _local_tz(record)
    at = record.source_at
    if zone is None or at is None:
        return TemporalRef(text=text, basis="none")
    local = at.astimezone(zone)
    low = text.lower()
    if low == "last month":
        year, month = (local.year, local.month - 1) if local.month > 1 else (local.year - 1, 12)
        start = datetime(year, month, 1, tzinfo=zone)
        return TemporalRef(text=text, at=start, precision="month", basis="source_time", anchor=at)
    if low == "last year":
        start = datetime(local.year - 1, 1, 1, tzinfo=zone)
        return TemporalRef(text=text, at=start, precision="year", basis="source_time", anchor=at)
    parts = low.split()
    if len(parts) == 2 and parts[1].isdigit():  # "in 2024"
        start = datetime(int(parts[1]), 1, 1, tzinfo=zone)
        return TemporalRef(text=text, at=start, precision="year", basis="explicit")
    if len(parts) == 3 and parts[1] in _MONTHS:  # "in September 2026"
        start = datetime(int(parts[2]), _MONTHS.index(parts[1]) + 1, 1, tzinfo=zone)
        return TemporalRef(text=text, at=start, precision="month", basis="explicit")
    # "in September": the year is not stated, so it is not resolved
    return TemporalRef(text=text, basis="none")


def _evidence(record: MemoryRecord, span: Span, owner: str) -> Evidence:
    return Evidence(
        role="support",
        turn=TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0),
        span=span,
        source_family=owner,
    )


def _note(
    record: MemoryRecord,
    reason: UnresolvedReason,
    attribute: str | None,
    value: str | None,
    span: Span | None,
) -> UnresolvedFactCandidate:
    return UnresolvedFactCandidate(
        source_id=record.record_id,
        source_version=record.version,
        reason=reason,
        attribute=attribute,
        value=value,
        span=span,
    )


#: A sentence mark; a full stop between two digits ("12.34", "4.0") is a decimal point.
_MARK = re.compile(r"[!?;\n]|\.(?!\d)|(?<!\d)\.")


def sentence_span(text: str, start: int, end: int) -> Span | None:
    """The sentence of ``text`` holding ``[start, end)`` (like :func:`support_span`, but a
    decimal point is not a sentence mark); None when a mark falls inside the range."""
    if not (0 <= start < end <= len(text)) or _MARK.search(text, start, end):
        return None
    marks = [m.end() for m in _MARK.finditer(text, 0, start)]
    left = marks[-1] if marks else 0
    stop = _MARK.search(text, end)
    right = stop.end() if stop else len(text)
    while left < start and text[left].isspace():
        left += 1
    return Span(start=left, end=right, quote=text[left:right])


def _span(record: MemoryRecord, match: re.Match[str], offset: int = 0) -> Span | None:
    return sentence_span(record.content, offset + match.start(), offset + match.end())


def _fact(
    record: MemoryRecord,
    scope: OwnerScope,
    owner: str,
    span: Span,
    *,
    predicate: str,
    obj: str,
    subject: str | None = None,
    object_entity_type: Literal["place", "organisation"] | None = "place",
    event_start: TemporalRef | None = None,
    context_evidence: Sequence[Evidence] = (),
) -> GroundedFactCandidate:
    source_at = record.source_at or record.valid_from
    return GroundedFactCandidate(
        scope=scope,
        subject=subject or owner,
        predicate=predicate,
        object=obj,
        object_kind="entity",
        object_entity_type=object_entity_type,
        modality="asserted",
        assertion_owner=owner,
        evidence=_evidence(record, span, owner),
        context_evidence=tuple(context_evidence),
        source_time=source_at.isoformat(),
        event_start=event_start,
        producer_revision=RULES_REVISION,
    )


def _sentences(text: str) -> list[tuple[int, int]]:
    """``[start, end)`` of every sentence of ``text`` (the marks ``.!?;`` and newlines)."""
    out: list[tuple[int, int]] = []
    start = 0
    for match in re.finditer(r"[.!?;\n]", text):
        if text[start : match.start()].strip():
            out.append((start, match.end()))
        start = match.end()
    if text[start:].strip():
        out.append((start, len(text)))
    return out


# ------------------------------------------------------------------ message rules


def _moves(record: MemoryRecord, scope: OwnerScope, owner: str, out: Mined) -> None:
    text = record.content
    strict = False
    for match in _MOVE.finditer(text):
        if not asserted_match(text, match):
            continue
        strict = True
        span = _span(record, match)
        if span is None:
            out.unresolved.append(_note(record, "ambiguous_move", "move", match["d"], None))
            continue
        when = event_ref(match["when"], record)
        out.facts.append(
            _fact(
                record,
                scope,
                owner,
                span,
                predicate="move_origin",
                obj=match["o"],
                event_start=when,
            )
        )
        out.facts.append(
            _fact(
                record,
                scope,
                owner,
                span,
                predicate="move_destination",
                obj=match["d"],
                event_start=when,
            )
        )
        # the destination is a home (multiple homes are allowed; the origin is not ended)
        out.facts.append(_fact(record, scope, owner, span, predicate="resides_in", obj=match["d"]))
    if not strict:
        for match in _MOVE_LOOSE.finditer(text):
            if asserted_match(text, match):
                out.unresolved.append(
                    _note(record, "ambiguous_move", "move", None, _span(record, match))
                )
                break


def _visits(record: MemoryRecord, scope: OwnerScope, owner: str, out: Mined) -> None:
    text = record.content
    for match in _VISIT.finditer(text):
        if not asserted_match(text, match) or match["v"].endswith(("'s", "\u2019s")):
            continue  # hedged, or a possessive ("visited Alex's flat") that names no place
        span = _span(record, match)
        if span is None:
            continue
        out.facts.append(_fact(record, scope, owner, span, predicate="visited", obj=match["v"]))


def _pronouns(record: MemoryRecord, scope: OwnerScope, owner: str, out: Mined) -> None:
    text = record.content
    sentences = _sentences(text)
    for index, (start, end) in enumerate(sentences):
        lead = len(text[start:end]) - len(text[start:end].lstrip())
        body = text[start + lead : end]
        match = _PRONOUN.match(body)
        if match is None or not asserted_match(body, match):
            continue
        offset = start + lead
        span = sentence_span(text, offset + match.start(), offset + match.end())
        predicate = "resides_in" if match["home"] else "employed_by" if match["work"] else "visited"
        names = _antecedents(text, sentences[index - 1], owner) if index > 0 else []
        if span is None or len(names) != 1:
            # no earlier sentence, no name or several names: the pronoun is not bound
            out.unresolved.append(
                _note(record, "ambiguous_antecedent", predicate, match["v"], span)
            )
            continue
        out.facts.append(
            _fact(
                record,
                scope,
                owner,
                span,
                predicate=predicate,
                obj=match["v"],
                subject=names[0],
                object_entity_type="organisation" if predicate == "employed_by" else "place",
            )
        )


def _antecedents(text: str, sentence: tuple[int, int], owner: str) -> list[str]:
    """The distinct capitalised names of one sentence, except its first word, ``I`` and the
    sender (a sentence-initial word is capitalised whatever it is)."""
    start, end = sentence
    body = text[start:end]
    first = re.match(r"\s*\S+", body)
    skip = start + first.end() if first else start
    names = [m.group() for m in _CAPITAL.finditer(text, skip, end) if m.group() not in ("I", owner)]
    return list(dict.fromkeys(names))


def _measurement(
    record: MemoryRecord,
    scope: OwnerScope,
    owner: str,
    match: re.Match[str],
    *,
    key: str,
    amount_text: str,
    unit: str | None = None,
    currency: str | None = None,
) -> GroundedFactCandidate | None:
    span = _span(record, match)
    if span is None:
        return None
    zone = _local_tz(record)
    at = record.source_at
    valid_time = at.astimezone(zone).date().isoformat() if zone and at else "unresolved"
    subject = match["s"].lower()
    try:
        amount = Decimal(amount_text)
    except InvalidOperation:
        return None
    return GroundedFactCandidate(
        scope=scope,
        subject=subject,
        subject_type="other",
        predicate="has_measurement",
        object=amount_text,
        object_kind="number",
        modality="asserted",
        assertion_owner=owner,
        evidence=_evidence(record, span, owner),
        attribute_key=key,
        unit=unit,
        currency=currency,
        amount=amount,
        cardinality_scope={
            "attribute_key": key,
            "valid_time": valid_time,
            "context": f"subject:{subject}",
        },
        source_time=(record.source_at or record.valid_from).isoformat(),
        producer_revision=RULES_REVISION,
    )


def _measurements(
    record: MemoryRecord,
    scope: OwnerScope,
    owner: str,
    out: Mined,
    registry: SemanticRegistry,
) -> None:
    text = record.content
    for match in _MEASURE.finditer(text):
        if not asserted_match(text, match):
            continue
        span = _span(record, match)
        value = match["n"]
        if match["approx"]:
            out.unresolved.append(_note(record, "approximate_value", "measurement", value, span))
            continue
        unit = match["u"]
        adjective = (match["adj"] or "").lower()
        key = _DIMENSION.get(adjective)
        if key is None:
            out.unresolved.append(
                _note(record, "ambiguous_measurement", "measurement", value, span)
            )
            continue
        prop = registry.properties.get(key)
        if prop is None or unit not in prop.allowed_units:
            # an unregistered or wrong-dimension unit (``kg`` long) is never coerced
            out.unresolved.append(_note(record, "missing_unit", key, value, span))
            continue
        fact = _measurement(record, scope, owner, match, key=key, amount_text=value, unit=unit)
        if fact is not None:
            out.facts.append(fact)
    for match in _PRICE.finditer(text):
        if not asserted_match(text, match):
            continue
        span = _span(record, match)
        value = match["n1"] or match["n2"] or match["n3"] or match["n4"]
        if match["approx"]:
            out.unresolved.append(_note(record, "approximate_value", "price", value, span))
            continue
        code = match["c1"] or match["c2"]
        if code is None or not code.isupper():
            # no code, a currency symbol ($ is not one currency) or a lower-case word
            out.unresolved.append(_note(record, "missing_currency", "price", value, span))
            continue
        fact = _measurement(
            record, scope, owner, match, key="price", amount_text=value, currency=code
        )
        if fact is not None:
            out.facts.append(fact)


def message_rules(
    record: MemoryRecord,
    scope: OwnerScope,
    owner: str,
    *,
    registry: SemanticRegistry | None = None,
) -> Mined:
    """The RM-22 / RM-54 rules over one eligible, user-authored message."""
    out = Mined()
    _moves(record, scope, owner, out)
    _visits(record, scope, owner, out)
    _pronouns(record, scope, owner, out)
    _measurements(record, scope, owner, out, registry or load_builtin_registry())
    return out


def pending_changes(record: MemoryRecord, scope: OwnerScope, owner: str) -> Mined:
    """An explicit correction or cancellation the source states (resolved later against the
    store); a correction cue the rules cannot read stays ``correction_requires_resolution``."""
    out = Mined()
    text = record.content
    message = record.source.message if isinstance(record.source.message, dict) else {}
    reply = message.get("reply_to")
    reply_to = reply if isinstance(reply, str) and reply else None
    for match in _CORRECTION.finditer(text):
        span = _span(record, match)
        if span is None or match["new"].casefold() == match["old"].casefold():
            continue
        out.pending.append(
            PendingClaimChange(
                kind="correction",
                scope=scope,
                owner=owner,
                evidence=_evidence(record, span, owner),
                old_value=match["old"],
                new_value=match["new"],
                reply_to=reply_to,
                producer_revision=RULES_REVISION,
            )
        )
    for match in _CANCEL.finditer(text):
        if not asserted_match(text, match):
            continue
        span = _span(record, match)
        if span is None:
            continue
        missed = match["verb"].lower().startswith("missed")
        out.pending.append(
            PendingClaimChange(
                kind="cancellation",
                scope=scope,
                owner=owner,
                evidence=_evidence(record, span, owner),
                old_value=" ".join(match["v"].split()),
                outcome="missed" if missed else "declined",
                reply_to=reply_to,
                producer_revision=RULES_REVISION,
            )
        )
    return out


# ------------------------------------------------------------------ document rules


def document_rules(record: MemoryRecord, scope: OwnerScope, owner: str) -> Mined:
    """RM-54: a document chunk's own statements, ``reported`` and attributed to the document.
    Only ``<Name> requires <resource>`` is read; it is never a user assertion, nor evidence of
    demonstrated compatibility."""
    out = Mined()
    text = record.content
    role = load_builtin_registry().roles["resource"]
    for match in _REQUIRES.finditer(text):
        if not asserted_match(text, match):
            continue
        start, rstart = match.start("s"), match.start("o")
        subject, resource = match["s"], match["o"]
        stop = re.search(r"[.!?;\n]", text[match.end("o") :])
        end = match.end("o") + (stop.end() if stop else 0)
        span = Span(start=start, end=end, quote=text[start:end])
        source_at = record.source_at or record.valid_from
        turn = TurnRef(source_id=record.record_id, source_version=record.version, seg_index=0)
        out.facts.append(
            GroundedFactCandidate(
                scope=scope,
                subject=subject,
                subject_type="project",
                predicate="requires",
                object=resource,
                object_kind="text",
                modality="reported",
                assertion_owner=owner,
                evidence=Evidence(role="support", turn=turn, span=span, source_family=owner),
                role_bindings=(
                    RoleBinding(
                        binding_id="rb-resource",
                        role_name="resource",
                        role_version=role.version,
                        value_kind="text",
                        value=resource,
                        evidence=[
                            Evidence(
                                role="support",
                                turn=turn,
                                span=Span(start=rstart, end=rstart + len(resource), quote=resource),
                                source_family=owner,
                            )
                        ],
                    ),
                ),
                source_time=source_at.isoformat(),
                source_origin="document",
                producer_revision=RULES_REVISION,
            )
        )
    return out


# ------------------------------------------------------------------ resolution


def _envelope(record: MemoryRecord) -> SemanticEnvelope | None:
    raw = record.fingerprint.envelope
    if not raw:
        return None
    try:
        return SemanticEnvelope.model_validate(raw)
    except ValueError:
        return None


def _cites_message(
    env: SemanticEnvelope, reply_to: str, message_of: Callable[[str], str | None]
) -> bool:
    return any(
        e.turn is not None and message_of(e.turn.source_id) == reply_to for e in env.evidence
    )


def resolve_pending(
    pending: PendingClaimChange,
    claims: Iterable[MemoryRecord],
    *,
    message_of: Callable[[str], str | None],
    records: Mapping[str, MemoryRecord],
) -> tuple[GroundedFactCandidate | None, UnresolvedFactCandidate | None, list[str]]:
    """Bind ``pending`` to the one active, asserted prior claim of the same sender it names.

    ``claims`` are the live (visible, active) canonical claims of the namespace;
    ``message_of`` maps a source record id to its message id (for ``reply_to``); ``records``
    the stored sources the resolved candidate cites. Returns the grounded candidate (with its
    :class:`ClaimTarget`), or the unresolved annotation, and the ids of the extra source
    records the candidate cites (the target's evidence)."""
    turn = pending.evidence.turn
    assert turn is not None
    correction = pending.kind == "correction"
    matches: list[tuple[MemoryRecord, SemanticEnvelope]] = []
    for record in claims:
        env = _envelope(record)
        if env is None or env.registry is None or env.object is None:
            continue
        if env.status != "active" or env.assessment.modality != "asserted":
            continue
        if env.provenance.principal != pending.owner:
            continue  # only the sender's own claim, never another principal's
        if env.object.surface.casefold() != pending.old_value.casefold():
            continue
        if env.registry.attribute_key is not None:
            continue  # a typed property value is not corrected by a name
        if correction and env.registry.object_kind != "entity":
            continue
        if not correction and (env.registry.predicate != "committed_to" or env.outcome != "none"):
            continue
        matches.append((record, env))
    if pending.reply_to is not None:
        replied = [m for m in matches if _cites_message(m[1], pending.reply_to, message_of)]
        matches = replied or matches
    attribute = "correction" if correction else "cancellation"
    if len(matches) != 1:
        reason: UnresolvedReason
        if correction:
            reason = "correction_target_ambiguous" if matches else "correction_target_not_found"
        else:
            reason = "cancellation_target_ambiguous" if matches else "cancellation_target_not_found"
        note = UnresolvedFactCandidate(
            source_id=turn.source_id,
            source_version=turn.source_version,
            reason=reason,
            attribute=attribute,
            value=pending.new_value or pending.old_value,
            span=pending.evidence.span,
            producer_revision=pending.producer_revision,
        )
        return None, note, []
    target, env = matches[0]
    typing = env.registry
    assert typing is not None and env.subject is not None and env.object is not None
    source = records.get(turn.source_id)
    source_at = source.source_at or source.valid_from if source is not None else None
    if correction:
        assert pending.new_value is not None
        candidate = GroundedFactCandidate(
            scope=pending.scope,
            subject=env.subject.surface,
            subject_type=typing.subject_type,
            predicate=typing.predicate,
            object=pending.new_value,
            object_kind=typing.object_kind,
            object_entity_type=typing.object_entity_type,
            modality="asserted",
            assertion_owner=pending.owner,
            evidence=pending.evidence,
            cardinality_scope=dict(typing.cardinality_scope),
            source_time=source_at.isoformat() if source_at is not None else None,
            target=ClaimTarget(action="supersede", record_id=target.record_id),
            producer_revision=pending.producer_revision,
        )
        return candidate, None, []
    cited = [e for e in env.evidence if e.counts_as_evidence and e.turn is not None]
    extra = sorted({e.turn.source_id for e in cited if e.turn is not None})
    candidate = GroundedFactCandidate(
        scope=pending.scope,
        subject=env.subject.surface,
        subject_type=typing.subject_type,
        predicate=typing.predicate,
        object=env.object.surface,
        object_kind=typing.object_kind,
        object_entity_type=typing.object_entity_type,
        modality="asserted",
        assertion_owner=pending.owner,
        evidence=pending.evidence,
        # the closed claim still cites the commitment it closes
        context_evidence=tuple(
            e.model_copy(update={"role": "binding"}) if e.role == "support" else e for e in cited
        ),
        role_bindings=tuple(env.role_bindings),
        cardinality_scope=dict(typing.cardinality_scope),
        source_time=source_at.isoformat() if source_at is not None else None,
        outcome=pending.outcome or "declined",
        target=ClaimTarget(action="close", record_id=target.record_id),
        producer_revision=pending.producer_revision,
    )
    return candidate, None, extra
